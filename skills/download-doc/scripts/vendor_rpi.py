#!/usr/bin/env python3
"""Raspberry Pi adapter (RP2040 / RP2350 silicon and the Pico boards).

Unlike the other three vendors there is **no usable index**. datasheets.raspberrypi.com
serves the files but has no listing; the Product Information Portal at
pip.raspberrypi.com renders its document lists client-side, so the category HTML
contains no document links; there is no JSON API (`.json` -> 406, `/api/` -> 404) and
no object-store listing (`?list-type=2` -> 301).

So enumeration here is **probing a known-name list, not reading a catalogue**, and the
distinction matters: a document Raspberry Pi publishes under a name not in CATALOGUE
is invisible to this adapter and will never be reported missing. Every entry below was
verified to return a real PDF, so there are no phantom entries — but absence proves
nothing. Say so in any coverage summary rather than letting "17 documents" read as
"the catalogue".

Revisions: Raspberry Pi publishes no version field, which would make every document
permanently uncheckable. The HTTP `Last-Modified` header is used instead — a real,
comparable date that changes when the vendor republishes, and one `parse_rev` already
understands in RFC-1123 form.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from doclib import Doc, last_modified   # noqa: E402

# Both RP2040 and RP2350 carry a USB 1.1 device/host controller, and every board here
# is built on one, so a USB filter selects the whole catalogue. None means "no
# narrowing needed" rather than "unknown".
USB_SCOPE = "all"        # sentinel: every part qualifies. None would mean "unknown".
USB_SCOPE_NOTE = "every RP2040/RP2350 part has a USB controller, so nothing is excluded"

BASE = "https://datasheets.raspberrypi.com"
AUTHOR = "Raspberry Pi Ltd"

# path, kind, families, human title. All verified present 2026-08-15.
CATALOGUE = [
    ("rp2040/rp2040-datasheet.pdf",        "datasheet",        ["RP2040"], "RP2040 Datasheet"),
    ("rp2040/rp2040-product-brief.pdf",    "data-brief",       ["RP2040"], "RP2040 Product Brief"),
    ("rp2040/hardware-design-with-rp2040.pdf", "user-manual",  ["RP2040"], "Hardware design with RP2040"),
    ("rp2350/rp2350-datasheet.pdf",        "datasheet",        ["RP2350"], "RP2350 Datasheet"),
    ("rp2350/rp2350-product-brief.pdf",    "data-brief",       ["RP2350"], "RP2350 Product Brief"),
    ("rp2350/hardware-design-with-rp2350.pdf", "user-manual",  ["RP2350"], "Hardware design with RP2350"),
    ("pico/pico-datasheet.pdf",            "datasheet",        ["Pico"],   "Raspberry Pi Pico Datasheet"),
    ("pico/pico-product-brief.pdf",        "data-brief",       ["Pico"],   "Raspberry Pi Pico Product Brief"),
    ("picow/pico-w-datasheet.pdf",         "datasheet",        ["Pico W"], "Raspberry Pi Pico W Datasheet"),
    ("picow/pico-w-product-brief.pdf",     "data-brief",       ["Pico W"], "Raspberry Pi Pico W Product Brief"),
    ("pico/pico-2-datasheet.pdf",          "datasheet",        ["Pico 2"], "Raspberry Pi Pico 2 Datasheet"),
    ("pico/pico-2-product-brief.pdf",      "data-brief",       ["Pico 2"], "Raspberry Pi Pico 2 Product Brief"),
    ("picow/pico-2-w-datasheet.pdf",       "datasheet",        ["Pico 2 W"], "Raspberry Pi Pico 2 W Datasheet"),
    ("pico/getting-started-with-pico.pdf", "user-manual",      ["Pico"],   "Getting started with Raspberry Pi Pico-series"),
    ("pico/raspberry-pi-pico-c-sdk.pdf",   "programming-manual", ["Pico"], "Raspberry Pi Pico-series C/C++ SDK"),
    ("pico/raspberry-pi-pico-python-sdk.pdf", "programming-manual", ["Pico"], "Raspberry Pi Pico-series Python SDK"),
    ("picow/connecting-to-the-internet-with-pico-w.pdf", "user-manual", ["Pico W"],
     "Connecting to the Internet with Raspberry Pi Pico W"),
    # Broadcom SoCs: TinyUSB's broadcom_32bit/64bit ports target these, and Raspberry Pi
    # is where their peripheral manuals are actually published — no Broadcom adapter
    # needed. BCM2835's document is not on this host under any probed name.
    ("bcm2711/bcm2711-peripherals.pdf", "reference-manual", ["BCM2711"],
     "BCM2711 ARM Peripherals"),
    ("bcm2836/bcm2836-peripherals.pdf", "reference-manual", ["BCM2836"],
     "BCM2836 ARM-local Peripherals"),
]

# RP2040 and RP2350 errata are appendices *inside* their datasheets, not separate
# documents — there is no rp2040-errata.pdf to fetch (probed, 404). Asking this
# adapter for --types errata therefore correctly returns nothing.


def enumerate_docs(families=None, types=None, refresh=False) -> list:
    want = {f.lower() for f in families} if families else None
    docs = []
    for path, kind, fams, title in CATALOGUE:
        if types and kind not in types:
            continue
        if want and not any(f.lower() in want for f in fams):
            continue
        url = f"{BASE}/{path}"
        docs.append(Doc(
            vendor="rpi", doc_id=Path(path).stem, doc_type=kind,
            version=last_modified(url), title=title, url=url, author=AUTHOR,
            family=list(fams), desc=title,
            # No document-code scheme exists, so nothing on page 1 can confirm identity.
            verify_id=False,
            # These docs predate the skill in this library and were filed by hand under
            # the vendor's own cover titles, sometimes with a date suffix.
            aliases=[title, Path(path).stem.replace("-", " ")]))
    return docs


if __name__ == "__main__":
    kinds = sys.argv[1].split(",") if len(sys.argv) > 1 else None
    for d in enumerate_docs(types=kinds):
        print(f"  {d.doc_id:<44} {d.doc_type:<20} {','.join(d.family):<9} {d.version or '?'}")
