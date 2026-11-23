<!--
SPDX-FileCopyrightText: 2026 GodSeon
SPDX-License-Identifier: GPL-2.0-or-later
-->

# GodseonDongle-VM

USB Wi-Fi 동글을 맥의 두 번째 무선랜으로 쓰게 해 주는 두 macOS 앱이 같이 쓰는 리눅스 VM 이미지(커널 + initramfs)예요.

- [ThruRNDIS-Wi-Fi](https://github.com/afy0817/ThruRNDIS-Wi-Fi): [ThruRNDIS](https://github.com/Afcoo/ThruRNDIS) 가 이 이미지를 "수동 VM 자산"으로 띄우고 동글을 USB 직통 전달로 넘겨요.
- [GodseonDongle](https://github.com/afy0817/GodseonDongle): 앱이 Virtualization.framework 로 직접 띄우고, 동글과 안드로이드 폰을 USB/IP 로 넘겨요.

> [!NOTE]
> Afcoo/ThruRNDIS 와 관계없는 비공식 프로젝트예요. 이미지의 구조와 기본 스크립트는 [Afcoo/ThruRNDIS_VM_Assets](https://github.com/Afcoo/ThruRNDIS_VM_Assets) ([`1176a41`](https://github.com/Afcoo/ThruRNDIS_VM_Assets/commit/1176a41ea2ae0269dcc01a8c1130967dbf2ca01b))에서 가져왔고, 그 위에 Wi-Fi 드라이버 · 펌웨어, wpa_supplicant, 제어 API, USB/IP 를 더했어요. 이 저장소는 GPL-2.0-or-later 예요.

앱 저장소(MIT)와 나눈 까닭: VM 이미지에 든 리눅스 · BusyBox 같은 GPL 바이너리는 소스와 라이선스 원문을 같이 배포해야 해요. 그 일을 이 저장소의 빌드와 릴리스가 맡고, 앱은 여기 릴리스를 받아 번들해요.

## 두 모드

부팅한 앱에 따라 커널 명령줄로 동작을 골라요. `thrurndis-wifi.mode=thrurndis|godseon` 이 없으면 `godseon.usbip=` 가 있을 때 godseon, 아니면 thrurndis 예요([initramfs/thrurndis-wifi-mode](initramfs/thrurndis-wifi-mode)).

| | thrurndis (ThruRNDIS-Wi-Fi) | godseon (GodseonDongle) |
|---|---|---|
| 동글이 오는 길 | ThruRNDIS 의 USB 직통 전달 (xHCI) | USB/IP (`godseon.usbip=gw/1-1`, vhci_hcd) |
| 맥 쪽 주소 | ThruRNDIS 호스트 링크 `192.168.100.2` (VM `192.168.100.1/24`) | wlan0: VZ NAT 브리지 주소, usb0: `10.94.8.2` (VM `10.94.8.1/30`) |
| 업링크 | 하나씩: usb0(폰)이 있으면 usb0, 없으면 wlan0 | wlan0 과 usb0 을 함께 (표 100, 101) |
| 제어 API | `eth0:8080` | `eth0:9408` |
| 송신 멈춤 감시 | `usb-tx-watcher` (xHCI 링) | 끔 (맥 쪽 도우미가 감시) |
| ThruRNDIS 콘솔 마커 | `THRURNDIS_RNDIS_ROUTE_READY` / `THRURNDIS_RNDIS_IPV4` | 같은 마커, 업링크마다 |

나머지(드라이버 · 펌웨어, wpa_supplicant, 고정 IP · 추가 경로, 쉴 때 CPU 를 줄인 감시 루프)는 두 모드가 같아요.

## 릴리스

`v` 태그를 올리면 GitHub Actions 가 이미지를 만들어 릴리스에 세 파일을 함께 올려요([.github/workflows/build.yml](.github/workflows/build.yml)). 올린 릴리스는 바꾸지 않고, 고친 이미지는 새 태그로 내요.

| 파일 | 내용 |
|---|---|
| `vm_assets.zip` | `vm_assets/`: `Image-lts`, `initramfs-thrurndis-wifi`, `manifest.json`, `SHA256SUMS`, `compliance/` |
| `vm_assets-sources.tar.gz` | 대응 소스: 이 저장소, 이미지에 든 Alpine 패키지마다 그 커밋의 aports 레시피와 원본 소스(distfile) |
| `SHA256SUMS` | 위 두 파일의 sha256 |

`compliance/` 에는 이런 것이 들어 있어요. 앱은 이 폴더째 번들해요.

- `THIRD_PARTY_NOTICES.md`: 패키지 · 버전 · 라이선스 · aports 커밋, 펌웨어는 파일마다 linux-firmware WHENCE 의 라이선스
- `licenses/<origin>/`: 소스에서 뽑은 라이선스 · 저작권 · NOTICE 원문, linux-firmware 의 WHENCE 와 LICENCE 파일
- `packages.json`: 같은 내용과, 패키지마다 이미지에 넣은 파일
- `SOURCE.md`: 대응 소스가 어디 있고 어떻게 다시 빌드하는지

펌웨어는 바이너리만 있어요(linux-firmware 에 소스가 없어요). WHENCE 가 정한 조건대로 재배포하고, 그 원문을 같이 넣어요.

## 빌드

Docker 가 필요해요(Apple Silicon 맥의 Docker Desktop, 또는 arm64 리눅스). Alpine edge aarch64 컨테이너에서 `linux-stable` 커널로 만들어요. 6.18 LTS 는 rtw89 의 USB 송신 흐름 제어 버그로 큰 업로드 중 멈춰서 stable 을 써요.

```bash
./build.sh
```

결과는 `dist/` 에 생겨요(`vm_assets/`, `vm_assets.zip`, `vm_assets-sources.tar.gz`, `SHA256SUMS`). 처음에는 distfile 과 aports 레시피를 내려받느라 시간이 걸리고, `dist/cache/` 에 남겨 두고 다음부터 다시 써요.

```bash
./build.sh --quick
```

고지 · 소스 없이 이미지만 만들어요. 스크립트를 고치며 시험할 때만 쓰고, 이걸로 만든 이미지는 배포하지 않아요.

| 환경 변수 | 뜻 |
|---|---|
| `GODSEON_VM_VERSION` | 이미지 버전 (기본: 날짜) |
| `GODSEON_VM_BUILDER_PULL=1` | 빌더 이미지를 새로 받아 커널 · 펌웨어를 최신으로 |
| `GODSEON_RTW88_PATCHES=0` | [kernel/](kernel/) 의 rtw88 패치 없이 (지금은 패치 없음) |
| `ALPINE_IMAGE` | 빌더의 Alpine 이미지 (기본 `alpine:edge`) |

### 앱에 로컬 빌드 넣기

두 앱의 `app/build_app.sh` 는 `vm-assets.env` 에 적힌 릴리스를 받아 sha256 을 확인하고 번들해요. 로컬 빌드를 넣으려면:

```bash
VM_ASSETS_DIR=../GodseonDongle-VM/dist/vm_assets ./app/build_app.sh
```

### 다른 동글 쓰기 (드라이버 추가)

1. 동글의 USB ID(`xxxx:yyyy`)를 확인해요. 앱의 안내 문구나 **시스템 정보 → USB** 에 나와요.
2. 어떤 드라이버가 맡는지 찾아요(빌더 이미지가 있어야 해서 `./build.sh` 를 한 번 돌린 뒤에).

   ```bash
   ./tools/find-driver.sh 0bda:b812
   ```

3. [drivers.conf](drivers.conf) 에 한 줄 넣어요. 형식은 그 파일 머리말에 있어요.
4. `./build.sh` 로 다시 만들고, 위처럼 `VM_ASSETS_DIR` 로 앱에 넣어요.

### 부팅 시험

[dev/vmrun.swift](dev/vmrun.swift) 는 ThruRNDIS 와 같은 장치 구성(VZ NAT, xHCI, hvc0)으로 이미지를 띄워 콘솔을 파일에 남겨요. USB 전달은 없어요.

```bash
swiftc -O -o dist/vmrun dev/vmrun.swift && codesign -s - --entitlements dev/vmrun.entitlements -f dist/vmrun
```

```bash
dist/vmrun dist/vm_assets/Image-lts dist/vm_assets/initramfs-thrurndis-wifi dist/console.log 60 thrurndis-wifi.token=test
```

## 폴더

| 폴더 | 내용 |
|---|---|
| `build.sh` | Docker 로 이미지 · 고지 · 소스 묶음을 만들어요 |
| `builder/` | `build_assets.py`(이미지), `compliance.py`(고지 · 소스 묶음), `Dockerfile` |
| `initramfs/` | VM 안 스크립트. `eth0-usb0-gateway` · `init-*` · `port-forwarding` · `rcS` 는 ThruRNDIS_VM_Assets 에서 가져와 고쳤어요(파일마다 고친 점을 적었어요) |
| `drivers.conf` | 넣을 USB Wi-Fi 드라이버와 펌웨어 패키지 |
| `kernel/` | rtw88 패치를 넣으면 그 소스로 모듈을 다시 빌드해요 |
| `tools/` | `find-driver.sh`: USB ID 로 드라이버 찾기 |
| `dev/` | 개발용 부팅 시험 도구 (MIT) |

## 라이선스

이 저장소의 빌더와 VM 스크립트는 GPL-2.0-or-later([LICENSE](LICENSE))예요. `dev/vmrun.swift` 만 MIT 예요. 파일마다 SPDX 머리말이 있고(머리말이 없는 파일은 [REUSE.toml](REUSE.toml)), 원문은 [LICENSES/](LICENSES/) 에 있어요. ThruRNDIS_VM_Assets 에서 온 파일에는 Afcoo 의 저작권 표기를 그대로 두었어요.

만든 VM 이미지는 여러 라이선스가 모인 묶음이에요. 리눅스, BusyBox, Alpine 패키지, 펌웨어는 각자의 라이선스를 그대로 따르고, 이 저장소의 GPL 이 그것들의 라이선스를 바꾸지 않아요. 각 구성 요소의 라이선스와 원문은 `compliance/`, 대응 소스는 같은 릴리스의 `vm_assets-sources.tar.gz` 에 있어요. GitHub 가 자동으로 만드는 소스 압축은 다른 패키지의 소스를 담지 않아서 대응 소스가 되지 않아요.
