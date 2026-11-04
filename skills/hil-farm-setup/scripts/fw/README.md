# Renesas uPD720201 firmware

`renesas_usb_fw.mem` is firmware 2.0.2.6 for the uPD720201/202 xHCI controllers on the farm's USB cards. setup
installs it as `/lib/firmware/renesas_usb_fw.mem`.

| Field  | Value |
|--------|---|
| Size   | 13012 bytes |
| sha256 | `177560c224c73d040836b17082348036430ecf59e8a32d7736ce2b80b2634f97`, the pin setup checks (`farm.toml`) |
| Source | [`UPDATE.mem`](https://raw.githubusercontent.com/o61o/uPD72020x-Firmware/1cf8542aa64d816301ea286a8937d9b09ebcbc99/UPDATE.mem) in [o61o/uPD72020x-Firmware](https://github.com/o61o/uPD72020x-Firmware), commit `1cf8542` |

The file is byte for byte the `UPDATE.mem` of Renesas' `k2026fwup1.exe` updater package, whose tools carry Renesas
Electronics Corporation Authenticode signatures; the blob itself is unsigned.

Proprietary Renesas firmware, under the Renesas licence in that repository's `License.rtf`, not this repository's MIT
licence.

The running version is bits 23:8 of PCI config register `0x6c` (`setpci -s <bdf> 0x6c.l`; uPD720201/202 User's
Manual: Hardware Rev.9.02, Table 3-52); bits 31:24 and 7:0 are reserved. A controller running this file returned
`00202609`. `renesas_fw_check.sh` reads it on every controller.
