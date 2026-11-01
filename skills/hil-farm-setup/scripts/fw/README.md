# Renesas uPD720201 firmware

`renesas_usb_fw.mem` is firmware 2.0.2.6 for the uPD720201/202 xHCI controllers on the farm's USB cards. setup
installs it as `/lib/firmware/renesas_usb_fw.mem`.

| Field   | Value                                                                                                     |
|---------|-----------------------------------------------------------------------------------------------------------|
| Size    | 13012 bytes                                                                                               |
| sha256  | `177560c224c73d040836b17082348036430ecf59e8a32d7736ce2b80b2634f97`                                        |
| md5     | `11b49c68a400564b704c6ef17a0e6c0a`                                                                        |
| Source  | `k2026fwup1.exe` (Windows self-extracting CAB, 1895424 bytes), station-drivers file 1348                  |
| Extract | `7z x` or `cabextract`; the blob inside is `UPDATE.mem`, dated 2013-10-17                                 |
| Same as | `UPDATE.mem` in <https://github.com/o61o/uPD72020x-Firmware>, byte for byte (git blob `47424d45`)         |

Source page: <https://www.station-drivers.com/index.php/en/component/remository/func-fileinfo/1348/lang,en-gb/>. The
package's updater tools carry Renesas Electronics Corporation Authenticode signatures; the blob itself is unsigned.

Proprietary Renesas firmware, under the Renesas licence in the package's `License.rtf`, not this repository's MIT
licence.

The running version is bits 23:8 of PCI config register `0x6c` (`setpci -s <bdf> 0x6c.l`; uPD720201/202 User's
Manual: Hardware Rev.9.02, Table 3-52); bits 31:24 and 7:0 are reserved. A controller running this file returned
`00202609`. `renesas_fw_check.sh` reads it on every controller.
