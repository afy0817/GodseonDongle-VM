# Guest kernel patches

`builder/build_assets.py` rebuilds rtw88 from the upstream source of the exact stable
release the `linux-stable` package carries (fetched once into `dist/cache/`), applies every
`rtw88-*.patch` here with `patch -p1`, and puts the rebuilt modules into the initramfs in place
of the packaged ones; the corresponding-source archive then carries that rtw88 source and the
patches. With no patch here (the current state) the packaged modules are used as they are.
`GODSEON_RTW88_PATCHES=0 ./build.sh` builds without the patches for comparisons.

Tried on 2026-11-17 and dropped, because neither lowered the USB traffic of an RTL8822BU on a
5 GHz channel shared with Wi-Fi 6 access points:

- setting `BIT_CBSSID_DATA` in `REG_RCR` while associated (rtw88 never sets it);
- clearing control subtype 2 (802.11ax Trigger) from `REG_RXFLTMAP1`.

Counting in `rtw_usb_rx_handler` showed that about 90% of the dongle's bulk IN transfers at
idle are broadcast 802.11ax Trigger frames, which this 802.11ac chip passes up whatever the
filter says. GodseonDongle's helper now drops those before they reach the VM (see
`helper/godseon-dongled.m` in GodseonDongle, "rtw88 receive filter"). Under ThruRNDIS
(ThruRNDIS-Wi-Fi) nothing filters them.
