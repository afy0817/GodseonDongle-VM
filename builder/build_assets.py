#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Afcoo
# SPDX-FileCopyrightText: 2026 GodSeon
# SPDX-License-Identifier: GPL-2.0-or-later
"""Build the GodseonDongle-VM guest kernel + initramfs inside an Alpine aarch64 container.

The layout follows ThruRNDIS_VM_Assets (BusyBox init, RAM-only root, same
guest scripts and console markers) and adds the USB Wi-Fi stack: cfg80211,
mac80211, the USB Wi-Fi drivers listed in drivers.conf with their firmware,
wireless-regdb, wpa_supplicant, USB/IP and a busybox httpd control API. One image
serves ThruRNDIS-Wi-Fi (booted by ThruRNDIS) and GodseonDongle (its own VM); the
guest tells them apart from the kernel command line (initramfs/thrurndis-wifi-mode).
compliance.py then writes the license notices and the corresponding-source archive.

Derived from Afcoo/ThruRNDIS_VM_Assets script/lib/build_assets.py: the newc cpio writer,
the kernel image extraction and the initramfs layout come from there.

Run through build.sh; it expects linux-stable, wireless-regdb, kmod and the
firmware packages named in drivers.conf to be installed in the container already.
"""

import argparse
import dataclasses
import gzip
import hashlib
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
import zlib

import compliance

ROOT_PACKAGES = [
    "busybox", "busybox-extras", "iproute2", "nftables", "tcpdump",
    "wpa_supplicant", "iw",
    # godseon mode: the dongle arrives over USB/IP from the macOS host
    "linux-tools-usbip",
]

# Upstream ThruRNDIS seeds first, then the Wi-Fi stack; the USB Wi-Fi
# drivers come from drivers.conf. modprobe resolves the dependency
# closure; crypto modules are requested at runtime by mac80211 and
# wpa_supplicant (CCMP/GCMP/BIP/SAE), so they are seeded explicitly.
BASE_MODULE_SEEDS = [
    "virtio_pci", "virtio_mmio", "virtio_console", "virtio_net", "af_packet",
    "xhci_hcd", "xhci_pci", "usbnet", "cdc_ether", "cdc_subset", "rndis_host",
    "nf_tables", "nf_conntrack", "nf_nat", "nft_chain_nat",
    "nft_ct", "nft_nat", "nft_masq",
    "cfg80211", "mac80211",
    "ccm", "gcm", "cmac", "ctr", "ghash_ce",
    "ecdh_generic", "aes_ce_ccm",
    "vhci_hcd",
]

BASE_FIRMWARE_GLOBS = [
    "regulatory.db",
    "regulatory.db.p7s",
]

# rtw88 is rebuilt with the patches in kernel/ (each file says why),
# from the upstream source of the exact stable release the linux-stable package carries.
KERNEL_GIT = "https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git"
RTW88_TREE = "drivers/net/wireless/realtek/rtw88"


@dataclasses.dataclass
class Driver:
    module: str
    packages: list[str]
    files: list[str]
    # (vendor, product, reference vendor, reference product)
    new_ids: list[tuple[str, str, str, str]]


USB_ID = r"([0-9a-f]{4}):([0-9a-f]{4})"


def read_drivers(path: pathlib.Path) -> list[Driver]:
    """Parses drivers.conf (format described in that file)."""
    drivers: list[Driver] = []
    for number, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        module, *fields = line.split()
        if not re.fullmatch(r"[a-z0-9_]+", module):
            fail(f"{path.name}:{number}: bad module name {module!r}")
        driver = Driver(module, [], [], [])
        for field in fields:
            key, _, value = field.partition("=")
            items = [item for item in value.split(",") if item]
            if key == "pkg" and all(re.fullmatch(r"[a-z0-9_.-]+", item) for item in items):
                driver.packages += items
            elif key == "files" and all(re.fullmatch(r"[A-Za-z0-9_./*?-]+", item) for item in items):
                driver.files += items
            elif key == "newid":
                for item in items:
                    match = re.fullmatch(f"{USB_ID}/{USB_ID}", item.lower())
                    if not match:
                        fail(f"{path.name}:{number}: newid must look like 0bda:b831/0bda:b851, got {item!r}")
                    driver.new_ids.append(match.groups())
            else:
                fail(f"{path.name}:{number}: cannot read {field!r}")
        drivers.append(driver)
    if not drivers:
        fail(f"{path.name} lists no drivers")
    return drivers

UPSTREAM_SCRIPTS = [
    "init-rndis", "init-network", "init-console", "eth0-usb0-gateway",
]
THRURNDIS_WIFI_SCRIPTS = [
    "uplink-watcher", "init-wifi", "thrurndis-wifi-config", "wlan-supervisor",
    "wpa-action", "wlan0-dhcp", "thrurndis-wifi-httpd", "usb-tx-watcher", "usb-recover",
    "usbip-attach",
]

# usb-tx-watcher reads the xHCI debugfs rings of ThruRNDIS's USB passthrough and idles in
# godseon mode (USB/IP has none); usbip-attach idles without godseon.usbip= (thrurndis mode).
INITTAB = """::sysinit:/etc/init.d/rcS
::wait:/usr/local/sbin/init-rndis
::wait:/usr/local/sbin/init-wifi
::wait:/usr/local/sbin/init-network
::respawn:/sbin/syslogd -n -O /run/thrurndis-wifi/messages -s 256 -b 1
::respawn:/usr/local/sbin/uplink-watcher
::respawn:/usr/local/sbin/wlan-supervisor
::respawn:/usr/local/sbin/usb-tx-watcher
::respawn:/usr/local/sbin/thrurndis-wifi-httpd
::respawn:/usr/local/sbin/usbip-attach
hvc0::respawn:/usr/local/sbin/init-console
::restart:/sbin/init
::ctrlaltdel:/bin/umount -a -r
"""


def fail(message: str) -> "NoReturn":
    print(f"build_assets: {message}", file=sys.stderr)
    sys.exit(1)


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    print("+", " ".join(command), flush=True)
    return subprocess.run(command, check=True, **kwargs)


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def install_root_packages(root: pathlib.Path) -> tuple[list[str], str]:
    """Installs ROOT_PACKAGES; returns name-version lines and the apk database (for
    compliance.py) before the package manager's own files are dropped from the image."""
    run([
        "apk", "--root", str(root), "--initdb", "--arch", "aarch64",
        "--keys-dir", "/etc/apk/keys",
        "--repositories-file", "/etc/apk/repositories",
        "--no-scripts", "--no-cache", "--quiet", "add", *ROOT_PACKAGES,
    ])
    listing = run(["apk", "--root", str(root), "info", "-v"], capture_output=True, text=True)
    packages = sorted(line.strip() for line in listing.stdout.splitlines() if line.strip())
    database = (root / "lib/apk/db/installed").read_text()
    for relative in ("lib/apk", "etc/apk", "var/cache", "usr/share/apk",
                     "usr/share/man", "usr/share/doc", "usr/include"):
        shutil.rmtree(root / relative, ignore_errors=True)
    return packages, database


def symlink(root: pathlib.Path, relative: str, target: str) -> None:
    path = root / relative
    if path.exists() or path.is_symlink():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(target)


def install_busybox_applets(root: pathlib.Path) -> None:
    for name, binary in (("busybox", "/bin/busybox"), ("busybox-extras", "/bin/busybox-extras")):
        manifest = root / "etc/busybox-paths.d" / name
        if not manifest.is_file() or not (root / binary.lstrip("/")).is_file():
            fail(f"{name} applet manifest or binary missing")
        for line in manifest.read_text().splitlines():
            relative = line.strip().lstrip("/")
            if relative and relative != binary.lstrip("/"):
                symlink(root, relative, binary)
    symlink(root, "usr/bin/busybox", "/bin/busybox")
    symlink(root, "usr/bin/sh", "/bin/busybox")


def add_file(root: pathlib.Path, relative: str, data: bytes, mode: int) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.exists():
        path.unlink()
    path.write_bytes(data)
    path.chmod(mode)


def install_project_files(root: pathlib.Path, src: pathlib.Path, release: dict,
                          drivers: list[Driver]) -> None:
    for directory, mode in (
        ("bin", 0o755), ("sbin", 0o755), ("dev", 0o755),
        ("etc/init.d", 0o755), ("root", 0o700), ("run", 0o755),
        ("tmp", 0o1777), ("usr/local/libexec/thrurndis", 0o755),
        ("usr/local/libexec/thrurndis-wifi", 0o755),
        ("usr/local/sbin", 0o755), ("usr/local/share/thrurndis-wifi/www/cgi-bin", 0o755),
        ("var/log", 0o755),
    ):
        path = root / directory
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(mode)

    scripts = src / "initramfs"
    symlink(root, "init", "/sbin/init")
    add_file(root, "etc/inittab", INITTAB.encode(), 0o644)
    add_file(root, "etc/init.d/rcS", (scripts / "rcS").read_bytes(), 0o755)
    for name in UPSTREAM_SCRIPTS + THRURNDIS_WIFI_SCRIPTS:
        add_file(root, f"usr/local/sbin/{name}", (scripts / name).read_bytes(), 0o755)
    add_file(root, "usr/local/libexec/thrurndis/port-forwarding",
             (scripts / "port-forwarding").read_bytes(), 0o644)
    add_file(root, "usr/local/libexec/thrurndis-wifi/mode",
             (scripts / "thrurndis-wifi-mode").read_bytes(), 0o644)
    add_file(root, "usr/local/share/thrurndis-wifi/www/cgi-bin/api", (scripts / "api.cgi").read_bytes(), 0o755)
    add_file(root, "etc/resolv.conf", b"", 0o644)
    # init-wifi loads these and registers the extra USB IDs.
    lines = [f"load {driver.module}" for driver in drivers]
    lines += [f"newid {driver.module} {' '.join(new_id)}" for driver in drivers for new_id in driver.new_ids]
    add_file(root, "etc/thrurndis-wifi/drivers", ("\n".join(lines) + "\n").encode(), 0o644)
    # rtw88 switches RTL8822BU/8822CU dongles to USB 3 at probe. Passed through
    # ThruRNDIS that re-enumerates the dongle on the Mac, and at SuperSpeed its
    # transfers stall (-EPIPE), so the VM keeps restarting.
    add_file(root, "etc/modprobe.d/thrurndis-wifi.conf", b"options rtw88_usb switch_usb_mode=N\n", 0o644)
    add_file(root, "etc/thrurndis-wifi-release", (json.dumps(release, indent=2) + "\n").encode(), 0o644)


def wifi_usb_ids(root: pathlib.Path, release: str, drivers: list[Driver]) -> list[str]:
    """vendor:product of every device the Wi-Fi drivers claim by USB ID (the app lists these as
    dongles): the image's modules.alias entries for the drivers.conf modules, plus its newid=."""
    modules = {driver.module for driver in drivers}
    ids = set()
    alias = root / "lib/modules" / release / "modules.alias"
    for line in alias.read_text().splitlines():
        match = re.fullmatch(r"alias usb:v([0-9A-F]{4})p([0-9A-F]{4})\S* (\S+)", line.strip())
        if match and match.group(3) in modules:
            ids.add(f"{match.group(1)}:{match.group(2)}".lower())
    for driver in drivers:
        for vendor, product, _, _ in driver.new_ids:
            ids.add(f"{vendor}:{product}")
    if not ids:
        fail("modules.alias lists no USB IDs for the Wi-Fi drivers")
    return sorted(ids)


def extract_linux_image(vmlinuz: pathlib.Path, destination: pathlib.Path) -> None:
    data = vmlinuz.read_bytes()
    offset = data.find(b"\x1f\x8b\x08")
    if offset < 0:
        fail(f"gzip Linux Image payload not found in {vmlinuz}")
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    image = decompressor.decompress(data[offset:]) + decompressor.flush()
    if len(image) < 1024 * 1024 or image[56:60] != b"ARM\x64":
        fail(f"extracted payload from {vmlinuz} is not an arm64 Linux Image")
    destination.write_bytes(image)
    destination.chmod(0o644)


def copy_module_closure(release: str, root: pathlib.Path, seeds: list[str],
                        overrides: dict[str, pathlib.Path] | None = None) -> list[str]:
    source_dir = pathlib.Path("/lib/modules") / release
    selected: dict[str, pathlib.Path] = {}
    for seed in seeds:
        result = subprocess.run(
            ["modprobe", "-S", release, "--show-depends", seed],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            fail(f"kernel module {seed} not found: {result.stderr.strip()}")
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "insmod":
                path = pathlib.Path(parts[1])
                selected[path.relative_to(source_dir).as_posix()] = path

    target_dir = root / "lib/modules" / release
    for relative, path in sorted(selected.items()):
        destination = target_dir / relative.removesuffix(".gz").removesuffix(".zst")
        destination.parent.mkdir(parents=True, exist_ok=True)
        override = (overrides or {}).get(destination.name)
        if override:
            shutil.copyfile(override, destination)
        elif path.suffix == ".gz":
            with gzip.open(path, "rb") as compressed, destination.open("wb") as output:
                shutil.copyfileobj(compressed, output)
        else:
            shutil.copyfile(path, destination)
        destination.chmod(0o644)
    for name in ("modules.builtin", "modules.builtin.modinfo", "modules.order"):
        if (source_dir / name).is_file():
            shutil.copyfile(source_dir / name, target_dir / name)
    run(["depmod", "-b", str(root), release])
    return sorted(path.removesuffix(".gz") for path in selected)


def installed_kernel_version() -> str:
    """linux-stable-7.3.4-r0 -> 7.3.4, the upstream stable tag the package builds."""
    # "apk info -v" with no package lists every installed package as name-version.
    out = run(["apk", "info", "-v"], capture_output=True, text=True).stdout
    match = re.search(r"^linux-stable-(\d+\.\d+(?:\.\d+)?)-r\d+$", out, re.M)
    if not match:
        fail(f"cannot tell the linux-stable version from: {out.strip()}")
    return match.group(1)


def http_get(url: str) -> bytes:
    # kernel.org refuses urllib's default client name and answers browser-like ones
    # with a bot check, so name the builder plainly.
    request = urllib.request.Request(url, headers={"User-Agent": "godseon-vm-builder/0.1"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def fetch_rtw88_source(version: str, cache: pathlib.Path) -> pathlib.Path:
    """Downloads drivers/net/wireless/realtek/rtw88 at tag v<version> once into the cache."""
    tag = f"v{version}"
    target = cache / f"rtw88-{tag}"
    if (target / ".complete").is_file():
        return target
    page = http_get(f"{KERNEL_GIT}/tree/{RTW88_TREE}?h={tag}").decode()
    names = sorted(set(re.findall(rf"{re.escape(RTW88_TREE)}/([A-Za-z0-9_.-]+)\?h={re.escape(tag)}", page)))
    if "Makefile" not in names or "mac80211.c" not in names:
        fail(f"could not list the rtw88 sources of {tag}")
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True)
    for name in names:
        (target / name).write_bytes(http_get(f"{KERNEL_GIT}/plain/{RTW88_TREE}/{name}?h={tag}"))
    (target / ".complete").write_text("\n".join(names) + "\n")
    print(f"fetched {len(names)} rtw88 files for {tag}", flush=True)
    return target


def build_patched_rtw88(release: str, patches: list[pathlib.Path], cache: pathlib.Path) -> dict[str, pathlib.Path]:
    """Builds every rtw88 module the kernel config enables, with the patches applied."""
    source = fetch_rtw88_source(installed_kernel_version(), cache)
    build = pathlib.Path(tempfile.mkdtemp(prefix="rtw88-"))
    for item in source.iterdir():
        if item.is_file() and not item.name.startswith("."):
            shutil.copyfile(item, build / item.name)
    for patch in patches:
        run(["patch", "-p1", "--forward", "--batch", "-d", str(build), "-i", str(patch)])
    kbuild = pathlib.Path("/lib/modules") / release / "build"
    run(["make", "-C", str(kbuild), f"M={build}", f"-j{os.cpu_count() or 2}", "modules"],
        stdout=subprocess.DEVNULL)
    built = {}
    for module in sorted(build.glob("*.ko")):
        run(["strip", "--strip-debug", str(module)])
        built[module.name] = module
    if "rtw88_core.ko" not in built:
        fail("the patched rtw88 build produced no rtw88_core.ko")
    return built


def copy_firmware(root: pathlib.Path, modules_root: pathlib.Path, patterns: list[str]) -> list[str]:
    """Copies the listed patterns plus every firmware file a packaged module
    declares. Files may be plain, .zst or .xz (the kernel's FW_LOADER
    decompresses them); a declared file no package provides is reported but
    does not fail the build, as drivers declare files for every chip variant."""
    source = pathlib.Path("/lib/firmware")
    copied: list[str] = []

    def copy(path: pathlib.Path) -> None:
        relative = path.relative_to(source).as_posix()
        if relative in copied:
            return
        destination = root / "lib/firmware" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
        destination.chmod(0o644)
        copied.append(relative)

    for pattern in patterns:
        matches = sorted(source.glob(pattern))
        if not matches:
            fail(f"firmware pattern {pattern} matched nothing (is its pkg= in drivers.conf?)")
        for path in matches:
            copy(path)

    missing: list[str] = []
    for module in sorted(modules_root.rglob("*.ko")):
        result = run(["modinfo", "-F", "firmware", str(module)], capture_output=True, text=True)
        for name in (line.strip() for line in result.stdout.splitlines()):
            if not name:
                continue
            candidates = [source / name, source / f"{name}.zst", source / f"{name}.xz"]
            found = next((path for path in candidates if path.is_file()), None)
            if found:
                copy(found)
            else:
                missing.append(f"{module.stem}: {name}")
    for line in missing:
        print(f"build_assets: note: declared firmware not packaged ({line})", flush=True)
    return sorted(copied)


def cpio_header(inode: int, name: bytes, mode: int, size: int, nlink: int = 1,
                rdevmajor: int = 0, rdevminor: int = 0) -> bytes:
    fields = (inode, mode, 0, 0, nlink, 0, size, 0, 0, rdevmajor, rdevminor, len(name) + 1, 0)
    return ("070701" + "".join(f"{field:08x}" for field in fields)).encode()


def write_padding(handle, size: int) -> None:
    padding = (-size) % 4
    if padding:
        handle.write(b"\0" * padding)


def write_initramfs(root: pathlib.Path, destination: pathlib.Path) -> None:
    """newc cpio owned by root:root with fixed mtimes, gzip -9."""
    with tempfile.NamedTemporaryFile(delete=False) as raw:
        raw_path = pathlib.Path(raw.name)
        inode = 1
        entries = [pathlib.Path(".")]
        entries.extend(sorted((p.relative_to(root) for p in root.rglob("*")), key=lambda p: p.as_posix()))
        for relative in entries:
            path = root if relative == pathlib.Path(".") else root / relative
            name = relative.as_posix().encode()
            info = path.lstat()
            if path.is_symlink():
                data = os.readlink(path).encode()
                mode, nlink = stat.S_IFLNK | 0o777, 1
            elif path.is_dir():
                data = b""
                mode, nlink = stat.S_IFDIR | stat.S_IMODE(info.st_mode), 2
            elif path.is_file():
                data = path.read_bytes()
                mode, nlink = stat.S_IFREG | stat.S_IMODE(info.st_mode), 1
            else:
                fail(f"unsupported file in initramfs root: {relative}")
            header = cpio_header(inode, name, mode, len(data), nlink)
            inode += 1
            raw.write(header + name + b"\0")
            write_padding(raw, len(header) + len(name) + 1)
            raw.write(data)
            write_padding(raw, len(data))
        for name, major, minor, mode in (
            (b"dev/console", 5, 1, 0o600),
            (b"dev/null", 1, 3, 0o666),
            (b"dev/kmsg", 1, 11, 0o600),
        ):
            header = cpio_header(inode, name, stat.S_IFCHR | mode, 0, 1, major, minor)
            inode += 1
            raw.write(header + name + b"\0")
            write_padding(raw, len(header) + len(name) + 1)
        trailer = b"TRAILER!!!"
        header = cpio_header(inode, trailer, 0, 0)
        raw.write(header + trailer + b"\0")
        write_padding(raw, len(header) + len(trailer) + 1)
    with raw_path.open("rb") as source, destination.open("wb") as output:
        with gzip.GzipFile(filename="", mode="wb", fileobj=output, compresslevel=9, mtime=0) as compressed:
            shutil.copyfileobj(source, compressed)
    raw_path.unlink()
    destination.chmod(0o644)


def alpine_branch() -> str:
    """edge or v3.NN, from the builder's repositories (distfiles.alpinelinux.org uses the same)."""
    text = pathlib.Path("/etc/apk/repositories").read_text()
    match = re.search(r"/(edge|v\d+\.\d+)/(?:main|community)\b", text)
    if not match:
        fail(f"cannot tell the Alpine branch from /etc/apk/repositories: {text.strip()}")
    return match.group(1)


def image_files(root: pathlib.Path) -> set[str]:
    return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file() or path.is_symlink()}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", type=pathlib.Path, required=True)
    parser.add_argument("--out", type=pathlib.Path, required=True)
    parser.add_argument("--version", default="dev")
    parser.add_argument("--sources", default="", help="build.sh --sources-hash, kept in the manifest")
    parser.add_argument("--commit", default="", help="git commit of this repository")
    parser.add_argument("--cache", type=pathlib.Path, default=pathlib.Path("/cache"))
    parser.add_argument("--sources-out", type=pathlib.Path,
                        help="where to write the corresponding-source archive")
    parser.add_argument("--no-compliance", action="store_true",
                        help="skip notices and sources (quick local test builds only, not for release)")
    parser.add_argument("--no-kernel-patches", action="store_true",
                        help="stock rtw88 modules (for A/B comparisons)")
    args = parser.parse_args()

    releases = sorted(p.name for p in pathlib.Path("/lib/modules").iterdir())
    if len(releases) != 1:
        fail(f"expected one installed kernel, found {releases}")
    kernel_release = releases[0]

    work = pathlib.Path(tempfile.mkdtemp(prefix="godseon-vm-"))
    root = work / "root"
    root.mkdir()

    drivers = read_drivers(args.src / "drivers.conf")
    release_info = {
        "name": "GodseonDongle-VM",
        "version": args.version,
        "kernel": kernel_release,
        # "Afcoo/ThruRNDIS_VM_Assets <commit>": the upstream the guest scripts derive from
        "upstream": "@".join((args.src / "initramfs/UPSTREAM_COMMIT").read_text().split()),
        "modes": ["thrurndis", "godseon"],
    }
    packages, root_db = install_root_packages(root)
    apk_files = image_files(root)
    install_busybox_applets(root)
    install_project_files(root, args.src, release_info, drivers)
    patches = [] if args.no_kernel_patches else sorted((args.src / "kernel").glob("rtw88-*.patch"))
    overrides = build_patched_rtw88(kernel_release, patches, args.cache) if patches else {}
    modules = copy_module_closure(kernel_release, root, BASE_MODULE_SEEDS + [d.module for d in drivers],
                                  overrides)
    patched = sorted(m for m in modules if pathlib.Path(m).name.removesuffix(".zst") in overrides)
    firmware = copy_firmware(root, root / "lib/modules" / kernel_release,
                             BASE_FIRMWARE_GLOBS + [pattern for d in drivers for pattern in d.files])
    usb_ids = wifi_usb_ids(root, kernel_release, drivers)
    # apk files still in the image (install_root_packages dropped man pages and the like)
    apk_files &= image_files(root)

    args.out.mkdir(parents=True, exist_ok=True)
    for stale in args.out.iterdir():
        if stale.is_file():
            stale.unlink()
    # The name predates linux-stable; installed apps point ThruRNDIS at it.
    image = args.out / "Image-lts"
    # ThruRNDIS only accepts a manual asset folder whose ramdisk matches
    # initramfs-thrurndis-* and whose manifest.json has assetVersion 1.
    initramfs = args.out / "initramfs-thrurndis-wifi"
    extract_linux_image(pathlib.Path("/boot/vmlinuz-stable"), image)
    write_initramfs(root, initramfs)

    compliance_summary = None
    if args.no_compliance:
        shutil.rmtree(args.out / "compliance", ignore_errors=True)
    else:
        rtw88 = None
        if patches:
            version = installed_kernel_version()
            rtw88 = {"tag": f"v{version}", "source": fetch_rtw88_source(version, args.cache),
                     "patch_paths": patches}
        components = compliance.inventory(
            root_db, pathlib.Path("/lib/apk/db/installed").read_text(), apk_files, "linux-stable",
            firmware, modules, kernel_release)
        compliance_summary = compliance.build(
            src=args.src, out=args.out, sources_out=args.sources_out, cache=args.cache,
            release=release_info, packages=components, firmware=firmware, rtw88=rtw88,
            alpine_branch=alpine_branch(), project_commit=args.commit)

    # sources tells an app's build script whether this image matches the repository it has;
    # wifi_usb_ids are the dongles GodseonDongle offers in its device list.
    manifest = dict(assetVersion=1, schemaVersion=1, **release_info, commit=args.commit,
                    sources=args.sources, compliance=compliance_summary,
                    drivers=[d.module for d in drivers], wifi_usb_ids=usb_ids,
                    packages=packages, modules=modules, firmware=firmware,
                    kernel_patches=[p.name for p in patches], patched_modules=patched)
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    sums = "".join(f"{sha256(p)}  {p.name}\n" for p in (image, initramfs, args.out / "manifest.json"))
    (args.out / "SHA256SUMS").write_text(sums)
    shutil.rmtree(work)

    print(f"built {image.name} ({image.stat().st_size} bytes) and "
          f"{initramfs.name} ({initramfs.stat().st_size} bytes) for {kernel_release}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
