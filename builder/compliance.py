# SPDX-FileCopyrightText: 2026 GodSeon
# SPDX-License-Identifier: GPL-2.0-or-later
"""License notices and corresponding source for the VM image (used by build_assets.py).

The image is an aggregate: Linux, BusyBox, the Alpine packages and the firmware keep their own
licenses. Following ThruRNDIS_VM_Assets' distribution policy (docs/LICENSING.md there), every
build records what went into the image and ships, next to the kernel and initramfs,

  compliance/packages.json           every component: package, version, license, Alpine aports
                                     commit, and the image files it provides
  compliance/THIRD_PARTY_NOTICES.md  the same as a readable table, firmware by WHENCE entry
  compliance/licenses/<origin>/...   license, copying and notice texts from the sources
  compliance/SOURCE.md               where the corresponding source is and how to rebuild

and, as a separate file, the corresponding-source archive (vm_assets-sources.tar.gz): this
repository, then for every Alpine origin its aports recipe at the commit the package was built
from (APKBUILD, patches, configs) and the upstream distfiles it names, checked against the
recipe's sha512sums; the rtw88 source and patches when kernel/ has any. Firmware is binary
only (linux-firmware has no source to give); its WHENCE file and licence texts are included.

Runs inside the Alpine builder container, which has network access, with stdlib only.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import gzip
import hashlib
import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile

APORTS = "https://gitlab.alpinelinux.org/alpine/aports"
APORTS_REPOS = ("main", "community", "testing")
FIRMWARE_GIT = "https://git.kernel.org/pub/scm/linux/kernel/git/firmware/linux-firmware.git"
DISTFILES = "https://distfiles.alpinelinux.org/distfiles"
# mirrors for when the endpoints above refuse automated clients
APORTS_GITHUB = "https://api.github.com/repos/alpinelinux/aports"
APORTS_RAW = "https://raw.githubusercontent.com/alpinelinux/aports"
FIRMWARE_MIRROR = "https://gitlab.com/kernel-firmware/linux-firmware"
USER_AGENT = "godseon-vm-builder/0.2"
LICENSE_NAMES = ("COPYING*", "COPYRIGHT*", "LICENSE*", "LICENCE*", "NOTICE*", "UNLICENSE*")
# Linux keeps its license texts in LICENSES/ and names them from every source file's SPDX line.
TREE_LICENSE_DIRS = ("LICENSES/",)
BINARY_ONLY_ORIGINS = {"linux-firmware"}


def log(message: str) -> None:
    print(f"compliance: {message}", flush=True)


def fail(message: str) -> "NoReturn":
    print(f"compliance: {message}", file=sys.stderr)
    sys.exit(1)


def http_get(url: str, timeout: int = 120) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def http_download(url: str, destination: pathlib.Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    partial = destination.with_name(destination.name + ".part")
    with urllib.request.urlopen(request, timeout=300) as response, partial.open("wb") as output:
        shutil.copyfileobj(response, output, 1 << 20)
    partial.rename(destination)


def file_hash(path: pathlib.Path, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------- apk database


@dataclasses.dataclass
class Package:
    name: str
    version: str
    license: str
    origin: str
    commit: str
    url: str
    files: list[str]
    # what the image takes from it: initramfs, kernel, modules, firmware
    used_for: list[str] = dataclasses.field(default_factory=list)
    image_files: list[str] = dataclasses.field(default_factory=list)


def parse_apk_db(text: str) -> dict[str, Package]:
    """/lib/apk/db/installed: one block per package, "K:value" lines."""
    packages: dict[str, Package] = {}
    for block in text.split("\n\n"):
        fields: dict[str, str] = {}
        files: list[str] = []
        directory = ""
        for line in block.splitlines():
            key, _, value = line.partition(":")
            if key == "F":
                directory = value
            elif key == "R":
                files.append(f"{directory}/{value}" if directory else value)
            elif len(key) == 1 and key not in fields:
                fields[key] = value
        if "P" not in fields:
            continue
        packages[fields["P"]] = Package(
            name=fields["P"], version=fields.get("V", ""), license=fields.get("L", ""),
            origin=fields.get("o", fields["P"]), commit=fields.get("c", ""), url=fields.get("U", ""),
            files=files)
    return packages


def inventory(root_db: str, host_db: str, root_files: set[str], kernel_package: str,
              firmware: list[str], modules: list[str], release: str) -> list[Package]:
    """The packages whose files are in the image. root_files are the initramfs paths that
    came from apk (relative, no leading slash), firmware the lib/firmware-relative paths."""
    selected: list[Package] = []
    for package in parse_apk_db(root_db).values():
        present = sorted(path for path in package.files if path in root_files)
        if present:
            package.used_for.append("initramfs")
            package.image_files = present
            selected.append(package)

    host = parse_apk_db(host_db)
    if kernel_package not in host:
        fail(f"{kernel_package} is not installed in the builder")
    kernel = host[kernel_package]
    kernel.used_for += ["kernel", "modules"]
    kernel.image_files = ["Image-lts (from boot/vmlinuz-stable)"] + [
        f"lib/modules/{release}/{module}" for module in modules]
    selected.append(kernel)

    owner = {}
    for package in host.values():
        for path in package.files:
            if path.startswith("lib/firmware/"):
                owner[path.removeprefix("lib/firmware/")] = package
    by_package: dict[str, list[str]] = {}
    for path in firmware:
        package = owner.get(path)
        if package is None:
            fail(f"no installed package owns firmware {path}")
        by_package.setdefault(package.name, []).append(f"lib/firmware/{path}")
    for name, files in sorted(by_package.items()):
        package = host[name]
        package.used_for.append("firmware")
        package.image_files = sorted(files)
        selected.append(package)
    return sorted(selected, key=lambda p: p.name)


# ---------------------------------------------------------------------------- aports recipes


# GODSEON_COMPLIANCE_MIRRORS=1 skips the primary endpoints, to try the mirrors (as on GitHub's
# runners, which gitlab.alpinelinux.org answers with a bot check).
_BLOCKED: set[str] = ({"aports", "linux-firmware"} if os.environ.get("GODSEON_COMPLIANCE_MIRRORS") == "1"
                      else set())


def web_blocked(error: Exception) -> bool:
    """True for answers that mean "no automated clients here" rather than "no such file"."""
    return not isinstance(error, urllib.error.HTTPError) or error.code in (403, 418, 429, 503)


def github_aports_dir(path: str, commit: str, destination: pathlib.Path) -> bool:
    """Copies aports/<path> at <commit> from github.com/alpinelinux/aports; False when the
    mirror has no such directory. One git trees call lists the directory with file modes
    (install scripts link to each other, pre-upgrade -> pre-install), the files come from
    raw.githubusercontent.com. GITHUB_TOKEN, when set (GitHub Actions), lifts the API's rate
    limit; it is sent to api.github.com only."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    request = urllib.request.Request(f"{APORTS_GITHUB}/git/trees/{commit}:{path}?recursive=1", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            listing = json.loads(response.read())
    except urllib.error.HTTPError as error:
        if error.code in (404, 422):
            return False
        raise
    if listing.get("truncated"):
        fail(f"the GitHub tree of aports/{path} is truncated")
    for entry in listing.get("tree", []):
        relative = pathlib.PurePosixPath(entry["path"])
        if entry["type"] != "blob" or relative.is_absolute() or ".." in relative.parts:
            continue
        data = http_get(f"{APORTS_RAW}/{commit}/{path}/{entry['path']}")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.unlink(missing_ok=True)
        if entry["mode"] == "120000":
            link = data.decode()
            if "/" in link:
                fail(f"aports/{path}/{entry['path']} links outside its directory: {link}")
            target.symlink_to(link)
        else:
            target.write_bytes(data)
            target.chmod(0o755 if entry["mode"] == "100755" else 0o644)
    return True


def fetch_recipe(origin: str, commit: str, cache: pathlib.Path) -> tuple[str, pathlib.Path]:
    """aports/<repo>/<origin> at <commit>, cached as cache/aports/<commit>/<repo>/<origin>.
    GitLab's archive of that one directory first; if GitLab turns the builder away, Alpine's
    GitHub mirror of aports (its contents API lists the directory at that commit)."""
    for repo in APORTS_REPOS:
        target = cache / "aports" / commit / repo / origin
        if (target / "APKBUILD").is_file():
            return repo, target
    if "aports" not in _BLOCKED:
        for repo in APORTS_REPOS:
            url = f"{APORTS}/-/archive/{commit}/aports-{commit}.tar.gz?path={repo}/{origin}"
            try:
                data = http_get(url)
            except (urllib.error.URLError, OSError) as error:
                if isinstance(error, urllib.error.HTTPError) and error.code == 404:
                    continue
                if web_blocked(error):
                    log(f"GitLab refused the archive ({error}); using the GitHub mirror of aports")
                    _BLOCKED.add("aports")
                    break
                raise
            prefix = f"aports-{commit}-{repo}-{origin}/{repo}/{origin}/"
            target = cache / "aports" / commit / repo / origin
            found = False
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
                for member in archive.getmembers():
                    if not (member.isfile() or member.issym()) or not member.name.startswith(prefix):
                        continue
                    relative = member.name[len(prefix):]
                    if not relative or ".." in pathlib.PurePosixPath(relative).parts:
                        continue
                    destination = target / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if member.issym():
                        # install scripts link to each other (pre-upgrade -> pre-install)
                        if "/" not in member.linkname:
                            destination.unlink(missing_ok=True)
                            destination.symlink_to(member.linkname)
                        continue
                    destination.write_bytes(archive.extractfile(member).read())
                    destination.chmod(0o755 if member.mode & 0o111 else 0o644)
                    found = found or relative == "APKBUILD"
            if found:
                return repo, target
            shutil.rmtree(target, ignore_errors=True)
        else:
            fail(f"no aports recipe for {origin} at {commit}")
    for repo in APORTS_REPOS:
        target = cache / "aports" / commit / repo / origin
        if github_aports_dir(f"{repo}/{origin}", commit, target) and (target / "APKBUILD").is_file():
            return repo, target
        shutil.rmtree(target, ignore_errors=True)
    fail(f"no aports recipe for {origin} at {commit}")


def recipe_sources(recipe: pathlib.Path) -> tuple[list[tuple[str, str]], dict[str, str]]:
    """[(file name, url or "")] from the APKBUILD's source=, and {file name: sha512}."""
    script = r'''
CARCH=aarch64 CTARGET_ARCH=aarch64 CBUILD=aarch64-alpine-linux-musl CHOST=$CBUILD CTARGET=$CBUILD
startdir=$PWD srcdir=$PWD/src pkgdir=$PWD/pkg
. ./APKBUILD >/dev/null 2>&1
printf '%s\n' "== source"
for item in $source; do printf '%s\n' "$item"; done
printf '%s\n' "== sha512sums"
printf '%s\n' "$sha512sums"
'''
    result = subprocess.run(["sh", "-c", script], cwd=recipe, capture_output=True, text=True, check=True)
    section = ""
    sources: list[tuple[str, str]] = []
    sums: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if line.startswith("== "):
            section = line[3:]
            continue
        line = line.strip()
        if not line:
            continue
        if section == "source":
            name, separator, url = line.partition("::")
            if not separator:
                url = line if "://" in line else ""
                name = url.rsplit("/", 1)[-1] if url else line
            sources.append((name, url))
        elif section == "sha512sums":
            digest, _, name = line.partition("  ")
            if name:
                sums[name.strip()] = digest.strip()
    return sources, sums


_STABLE_BRANCHES: list[str] = []


def stable_branches() -> list[str]:
    """v3.N directories of the distfiles mirror, newest first (at most eight)."""
    if not _STABLE_BRANCHES:
        try:
            page = http_get(f"{DISTFILES}/").decode()
        except (urllib.error.URLError, OSError):
            return []
        found = {int(minor) for minor in re.findall(r'href="v3\.(\d+)/"', page)}
        _STABLE_BRANCHES.extend(f"v3.{minor}" for minor in sorted(found, reverse=True)[:8])
    return _STABLE_BRANCHES


def fetch_distfile(name: str, url: str, expected: str, distfiles: pathlib.Path,
                   alpine_branch: str) -> pathlib.Path:
    path = distfiles / name
    if path.is_file() and (not expected or file_hash(path, "sha512") == expected):
        return path
    # Alpine's distfiles mirror keeps each file under the branch that first built it, and some
    # upstream hosts no longer serve old releases (gmplib.org, for one), so the build branch,
    # the recipe's own URL, the GNU mirror for GNU URLs, then the newer stable branches.
    attempts = [f"{DISTFILES}/{alpine_branch}/{name}", url]
    gnu = re.match(r"https?://(?:ftp\.gnu\.org/gnu|ftpmirror\.gnu\.org|gmplib\.org/download)/(.+)$", url)
    if gnu:
        attempts.append(f"https://ftpmirror.gnu.org/{gnu.group(1)}")
    attempts += [f"{DISTFILES}/{branch}/{name}" for branch in stable_branches() if branch != alpine_branch]
    errors = []
    for candidate in attempts:
        if not candidate.startswith(("http://", "https://")):
            continue
        try:
            log(f"fetching {name} from {candidate}")
            http_download(candidate, path)
        except (urllib.error.URLError, OSError) as error:
            errors.append(f"{candidate}: {error}")
            continue
        if expected and file_hash(path, "sha512") != expected:
            errors.append(f"{candidate}: sha512 differs from the recipe")
            path.unlink()
            continue
        return path
    fail(f"cannot fetch {name}: " + "; ".join(errors))


# ---------------------------------------------------------------------------- license texts


def is_license_name(name: str) -> bool:
    base = name.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(base.upper(), pattern.upper()) for pattern in LICENSE_NAMES)


def wanted_member(name: str) -> bool:
    parts = name.split("/")
    # <top dir>/<file> or <top dir>/<one subdir>/<file>; Linux's LICENSES/ tree anywhere below top
    inner = "/".join(parts[1:])
    if any(inner.startswith(directory) for directory in TREE_LICENSE_DIRS):
        return True
    return len(parts) <= 3 and is_license_name(name)


def open_archive(path: pathlib.Path):
    """A streaming tarfile for .tar.{gz,bz2,xz,zst} and plain .tar, else None."""
    name = path.name
    if name.endswith((".tar.zst", ".tzst")):
        process = subprocess.Popen(["zstd", "-dc", str(path)], stdout=subprocess.PIPE)
        return tarfile.open(fileobj=process.stdout, mode="r|")
    if re.search(r"\.(tar(\.(gz|bz2|xz|lz|lzma))?|tgz|tbz2|txz)$", name):
        return tarfile.open(path, mode="r|*")
    return None


def extract_licenses(distfile: pathlib.Path, destination: pathlib.Path) -> list[str]:
    found: list[str] = []
    if distfile.name.endswith(".zip"):
        with zipfile.ZipFile(distfile) as archive:
            for info in archive.infolist():
                if not info.is_dir() and wanted_member(info.filename):
                    target = destination / info.filename
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.read(info))
                    found.append(info.filename)
        return found
    archive = open_archive(distfile)
    if archive is None:
        return found
    with archive:
        for member in archive:
            if not member.isfile() or not wanted_member(member.name):
                continue
            relative = pathlib.PurePosixPath(member.name)
            if relative.is_absolute() or ".." in relative.parts:
                continue
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.extractfile(member).read())
            found.append(member.name)
    return found


def firmware_file(path: str, tag: str, cache: pathlib.Path) -> bytes | None:
    """One file of linux-firmware at <tag>; None when it is missing or a directory (cgit
    answers a directory's plain URL with an HTML listing). From the official gitlab.com mirror
    when git.kernel.org turns the builder away."""
    if "linux-firmware" not in _BLOCKED:
        try:
            data = http_get(f"{FIRMWARE_GIT}/plain/{path}?h={tag}")
            return None if data.startswith(b"<html><head><title>/") else data
        except (urllib.error.URLError, OSError) as error:
            if isinstance(error, urllib.error.HTTPError) and error.code == 404:
                return None
            if not web_blocked(error):
                raise
            log(f"git.kernel.org refused the web view ({error}); using the gitlab.com mirror of linux-firmware")
            _BLOCKED.add("linux-firmware")
    try:
        return http_get(f"{FIRMWARE_MIRROR}/-/raw/{tag}/{path}")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def firmware_licenses(version: str, used: list[str], destination: pathlib.Path,
                      cache: pathlib.Path) -> list[dict]:
    """WHENCE and the licence files of linux-firmware at tag <version> that the image's
    firmware falls under; returns, for every firmware file in the image, its WHENCE driver
    entry and licence. linux-firmware keeps the licences in LICENSES/ (older tags: at the top)."""
    tag = version.split("-r")[0]
    store = cache / f"linux-firmware-{tag}"
    store.mkdir(parents=True, exist_ok=True)
    whence = store / "WHENCE"
    if not whence.is_file():
        data = firmware_file("WHENCE", tag, cache)
        if data is None:
            fail(f"linux-firmware {tag} has no WHENCE")
        whence.write_bytes(data)

    # WHENCE: entries separated by dashed lines; "File:"/"Link:" lines, then "Licence:".
    entries = []
    for block in re.split(r"\n-{10,}\n", whence.read_text(errors="replace")):
        driver = re.search(r"^Driver:\s*(.+)$", block, re.M)
        licence = re.search(r"^Licen[cs]e:\s*(.+?)(?:\n\n|\n[A-Z][a-z]+:|\Z)", block, re.M | re.S)
        files = re.findall(r"^(?:File|RawFile|Link):\s*(\S+)", block, re.M)
        if files:
            entries.append((driver.group(1).strip() if driver else "",
                            " ".join(licence.group(1).split()) if licence else "", files))
    table = []
    for path in used:
        relative = path.removeprefix("lib/firmware/")
        bare = re.sub(r"\.(zst|xz)$", "", relative)
        match = next(((d, l) for d, l, files in entries if bare in files or relative in files), None)
        if match is None:
            fail(f"{relative} is not listed in linux-firmware's WHENCE")
        table.append({"file": path, "driver": match[0], "licence": match[1]})

    # The licence files those entries name, plus the repository's own LICENSE files.
    wanted = {"LICENSE", "LICENSE-CRITERIA.md"}
    for row in table:
        wanted.update(re.findall(r"(?:LICEN[CS]E|GPL)[A-Za-z0-9_.+-]*[A-Za-z0-9_+-]", row["licence"]))
    wanted.discard("LicenseRef")
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(whence, destination / "WHENCE")
    for name in sorted(wanted):
        if name.startswith("LicenseRef"):
            continue
        cached = store / name
        if not cached.is_file():
            data = firmware_file(f"LICENSES/{name}", tag, cache) or firmware_file(name, tag, cache)
            if data is None:
                if name in ("LICENSE", "LICENSE-CRITERIA.md"):
                    continue
                fail(f"linux-firmware {tag} has no licence file {name}")
            cached.write_bytes(data)
        shutil.copyfile(cached, destination / name)
    return table


# ---------------------------------------------------------------------------- outputs


def notices_markdown(packages: list[Package], firmware_table: list[dict], rtw88: dict | None,
                     sources_name: str) -> str:
    lines = [
        "# Third-party notices",
        "",
        "The VM image (kernel `Image-lts` and `initramfs-thrurndis-wifi`) is an aggregate. Each",
        "component below keeps its own license; the scripts of this repository are",
        "GPL-2.0-or-later. License, copying and notice texts are in `licenses/<origin>/`, and the",
        f"corresponding source is the `{sources_name}` file published with this image (see",
        "`SOURCE.md`).",
        "",
        "| Package | Version | License | Alpine origin | aports commit | Used for |",
        "|---|---|---|---|---|---|",
    ]
    for package in packages:
        lines.append(f"| {package.name} | {package.version} | {package.license} | {package.origin} | "
                     f"`{package.commit[:12]}` | {', '.join(package.used_for)} |")
    if rtw88:
        lines += ["", f"rtw88 modules rebuilt from Linux {rtw88['tag']} with "
                  f"{', '.join(rtw88['patches'])} (GPL-2.0-only); source and patches are in the archive."]
    if firmware_table:
        lines += ["", "## Firmware", "",
                  "Binary firmware from linux-firmware, redistributed under the terms its WHENCE file",
                  "gives for each file. WHENCE and the licence files are in `licenses/linux-firmware/`.",
                  "", "| File | WHENCE driver | Licence |", "|---|---|---|"]
        for row in firmware_table:
            lines.append(f"| `{row['file']}` | {row['driver']} | {row['licence']} |")
    return "\n".join(lines) + "\n"


def source_markdown(sources_name: str, release: dict, origins: list[dict], project_commit: str) -> str:
    lines = [
        "# Corresponding source",
        "",
        f"The source for this image ({release['name']} {release['version']}, kernel {release['kernel']})",
        f"is `{sources_name}`, published next to `vm_assets.zip` in the same GitHub Release of",
        "https://github.com/afy0817/GodseonDongle-VM and kept there for as long as the image is.",
        "The repository's automatic source archive does not replace it: it has none of the",
        "third-party sources.",
        "",
        "The archive holds",
        "",
        f"- `project/`: this repository{f' at {project_commit}' if project_commit else ''}, the image",
        "  builder and the guest scripts (GPL-2.0-or-later);",
        "- `aports/<repo>/<origin>/`: the Alpine recipe of every package in the image at the commit",
        "  it was built from (APKBUILD, patches, kernel configs, install scripts);",
        "- `distfiles/`: the upstream sources those recipes name, matching their sha512sums;",
        "- `kernel/` (only when the image has patched rtw88 modules): the rtw88 sources and patches;",
        "- `MANIFEST.json`: every file with its sha256, and which recipe uses which distfile.",
        "",
        "To rebuild a package, copy `distfiles/*` into abuild's `SRCDEST` and run `abuild -r` in",
        "its recipe directory on Alpine (aarch64). To rebuild the image, run `./build.sh` in",
        "`project/` (Docker, Alpine edge); `compliance/packages.json` names the exact versions.",
        "",
        "| Origin | Recipe | Distfiles |",
        "|---|---|---|",
    ]
    for origin in origins:
        files = ", ".join(f"`{name}`" for name in origin["distfiles"]) or "(recipe only)"
        lines.append(f"| {origin['origin']} | `aports/{origin['repo']}/{origin['origin']}` at "
                     f"`{origin['commit'][:12]}` | {files} |")
    return "\n".join(lines) + "\n"


def write_tar_gz(staging: pathlib.Path, output: pathlib.Path, top: str) -> None:
    """Deterministic order, root-owned, fixed mtime."""
    def reset(info: tarfile.TarInfo) -> tarfile.TarInfo:
        info.uid = info.gid = 0
        info.uname = info.gname = "root"
        info.mtime = 0
        return info

    partial = output.with_name(output.name + ".part")
    with partial.open("wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw,
                                                   compresslevel=6, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
            paths = sorted(staging.rglob("*"), key=lambda p: p.relative_to(staging).as_posix())
            for path in paths:
                archive.add(path, arcname=f"{top}/{path.relative_to(staging).as_posix()}",
                            recursive=False, filter=reset)
    partial.rename(output)


def project_tree(src: pathlib.Path, destination: pathlib.Path) -> None:
    skip = {".git", "dist", "__pycache__", ".DS_Store"}
    for path in sorted(src.rglob("*")):
        relative = path.relative_to(src)
        if any(part in skip for part in relative.parts) or not path.is_file():
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)


def build(*, src: pathlib.Path, out: pathlib.Path, sources_out: pathlib.Path | None,
          cache: pathlib.Path, release: dict, packages: list[Package], firmware: list[str],
          rtw88: dict | None, alpine_branch: str, project_commit: str) -> dict:
    """Writes out/compliance and, unless sources_out is None, the source archive. Returns a
    summary for manifest.json."""
    compliance = out / "compliance"
    shutil.rmtree(compliance, ignore_errors=True)
    licenses = compliance / "licenses"
    licenses.mkdir(parents=True)
    distfiles = cache / "distfiles"
    distfiles.mkdir(parents=True, exist_ok=True)

    staging = pathlib.Path(tempfile.mkdtemp(prefix="sources-")) if sources_out else None
    origins: list[dict] = []
    license_files: dict[str, list[str]] = {}
    scanned: dict[str, list[str]] = {}
    seen: set[tuple[str, str]] = set()
    firmware_table: list[dict] = []

    for package in packages:
        key = (package.origin, package.commit)
        if key in seen:
            continue
        seen.add(key)
        if package.origin in BINARY_ONLY_ORIGINS:
            used = sorted(f for p in packages if p.origin == package.origin for f in p.image_files)
            firmware_table = firmware_licenses(package.version, used, licenses / package.origin, cache)
            license_files[package.origin] = sorted(p.name for p in (licenses / package.origin).iterdir())
            continue
        if not package.commit:
            fail(f"{package.name} records no aports commit")
        repo, recipe = fetch_recipe(package.origin, package.commit, cache)
        sources, sums = recipe_sources(recipe)
        remote = []
        for name, url in sources:
            if not url:
                if not (recipe / name).is_file():
                    fail(f"{package.origin}: local source {name} is not in the recipe")
                continue
            path = fetch_distfile(name, url, sums.get(name, ""), distfiles, alpine_branch)
            remote.append(path)
        found: list[str] = []
        target = licenses / package.origin
        for path in remote:
            if path.name not in scanned:
                scan = cache / "license-scan" / path.name
                done = scan / ".members"
                if done.is_file():
                    scanned[path.name] = [line for line in done.read_text().splitlines() if line]
                else:
                    log(f"reading license texts from {path.name}")
                    shutil.rmtree(scan, ignore_errors=True)
                    scanned[path.name] = extract_licenses(path, scan)
                    scan.mkdir(parents=True, exist_ok=True)
                    done.write_text("".join(f"{member}\n" for member in scanned[path.name]))
            for member in scanned[path.name]:
                destination = target / member
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(cache / "license-scan" / path.name / member, destination)
                found.append(member)
        for path in sorted(recipe.iterdir()):
            if path.is_file() and is_license_name(path.name):
                (target / "aports").mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target / "aports" / path.name)
                found.append(f"aports/{path.name}")
        if not found:
            log(f"note: no license file in the sources of {package.origin} ({package.license}); "
                "its terms are in the source headers")
        license_files[package.origin] = sorted(found)
        origins.append({"origin": package.origin, "repo": repo, "commit": package.commit,
                        "distfiles": [path.name for path in remote]})
        if staging:
            recipe_target = staging / "aports" / repo / package.origin
            shutil.copytree(recipe, recipe_target, symlinks=True, dirs_exist_ok=True)
            for path in remote:
                destination = staging / "distfiles" / path.name
                if not destination.exists():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(path, destination)

    if rtw88 and staging:
        shutil.copytree(rtw88["source"], staging / "kernel" / f"rtw88-{rtw88['tag']}",
                        ignore=shutil.ignore_patterns(".*"))
        for patch in rtw88["patch_paths"]:
            (staging / "kernel" / "patches").mkdir(parents=True, exist_ok=True)
            shutil.copyfile(patch, staging / "kernel" / "patches" / patch.name)

    shutil.copyfile(src / "LICENSE", compliance / "LICENSE.GPL-2.0-or-later")
    sources_name = "vm_assets-sources.tar.gz"
    rtw88_note = None if not rtw88 else {"tag": rtw88["tag"], "patches": [p.name for p in rtw88["patch_paths"]]}
    (compliance / "THIRD_PARTY_NOTICES.md").write_text(
        notices_markdown(packages, firmware_table, rtw88_note, sources_name))
    (compliance / "SOURCE.md").write_text(source_markdown(sources_name, release, origins, project_commit))
    (compliance / "packages.json").write_text(json.dumps({
        "image": release,
        "project_commit": project_commit,
        "packages": [dataclasses.asdict(p) | {"files": None, "license_files": license_files.get(p.origin, [])}
                     for p in packages],
        "origins": origins,
        "firmware": firmware_table,
        "rtw88": rtw88_note,
    }, indent=2) + "\n")

    summary = {"notices": "compliance/THIRD_PARTY_NOTICES.md", "source_archive": sources_name,
               "origins": len(origins)}
    if staging:
        project_tree(src, staging / "project")
        shutil.copytree(compliance, staging / "compliance")
        inventory_rows = []
        for path in sorted(staging.rglob("*")):
            if path.is_file():
                inventory_rows.append({"path": path.relative_to(staging).as_posix(),
                                       "sha256": file_hash(path, "sha256"), "size": path.stat().st_size})
        (staging / "MANIFEST.json").write_text(json.dumps(
            {"image": release, "project_commit": project_commit, "origins": origins,
             "files": inventory_rows}, indent=2) + "\n")
        log(f"writing {sources_out.name} ({len(inventory_rows)} files)")
        write_tar_gz(staging, sources_out, "vm_assets-sources")
        shutil.rmtree(staging)
        summary["source_archive_sha256"] = file_hash(sources_out, "sha256")
    return summary
