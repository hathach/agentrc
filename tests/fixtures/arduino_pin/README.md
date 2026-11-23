These are verbatim `.claude/skills/hil-contract/scripts/agentrc_pin.py` snapshots for testing old-bootstrap
compatibility with newly packed Arduino trees. Keep them unchanged.

- `tua.py`: adafruit/Adafruit_TinyUSB_Arduino `99387f5125ff7911250d179c2ee7806fea136c44`.
- `samd.py`: adafruit/ArduinoCore-samd `f17d5a9b3f2932f1088c65a758f2744f10a9e1fb`.

`tests/test_arduino_pin.py` runs each in a fresh interpreter against the current engines and both staged manifests.
