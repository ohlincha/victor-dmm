# victor-dmm

Real-time display, plotting and logging for **VICTOR USB multimeters** (Victor 70C, 86C,
86D and other models using the USB-HID cable with USB ID `1244:d237`).

- Live reading, auto-scaling plot, min/max/mean statistics.
- Logs to CSV: every reading (~2–3 per second) or at a fixed interval. Each line is
  written to disk immediately.
- Windows, macOS and Linux. No drivers needed. The installers bundle everything
  they need.

## Install

Download the file for your system from the
[Releases page](https://github.com/ohlincha/victor-dmm/releases/latest):

| System  | File | How |
|---------|------|-----|
| Windows | `VictorDMM.exe` | Just run it. No installation. If SmartScreen warns, choose *More info → Run anyway*. |
| macOS   | `VictorDMM-macOS.dmg` | Open it and drag *VICTOR DMM* to Applications. The first time, right-click the app → *Open* (it is not notarized by Apple). |
| Linux (Debian/Ubuntu) | `victor-dmm_<version>_all.deb` | `sudo apt install ./victor-dmm_*_all.deb`, then start *VICTOR DMM* from the menu or run `victor-dmm`. |

On the meter, turn on the PC/USB output (on most Victor models, long-press the
REL/USB button until the USB/RS232 symbol appears on the display).

### Running from source (any OS)

You need Python ≥ 3.7 with tkinter, which is included in the python.org installers
(on Debian/Ubuntu: `sudo apt install python3-tk`). No other packages are required.

```sh
python3 victor_dmm_gui.py      # graphical app
python3 victor_dmm.py --help   # command-line logger
```

On Linux, also install the udev rule once so your user can read the meter without root.
The `.deb` does this for you.

```sh
sudo cp 70-victor-dmm.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger
```

## Command-line logger

```sh
victor-dmm-cli                     # show live readings   (or: python3 victor_dmm.py)
victor-dmm-cli -o log.csv          # ... and save them to log.csv
victor-dmm-cli -o log.csv -i 1     # save at most one sample per second
victor-dmm-cli -o log.csv -d 3600  # stop after one hour
victor-dmm-cli -o log.csv -a       # append to an existing file
victor-dmm-cli --list              # list connected meters
```

Use `--delimiter ';'` for European-locale Excel, or `--delimiter tab` for TSV.

## CSV format

```
timestamp,elapsed_s,value,unit,display,quantity,flags
2026-11-18T16:02:11.412+01:00,0.000,0.1234,V,123.4 mV,voltage,DC AUTO
```

- `value` is in SI base units (V, A, Ohm, Hz, F, °C, …). An overload ("OL") is written as `inf`.
- `display` is the reading as shown on the meter's LCD.

## Using it from Python

```python
from victor_dmm import Meter

m = Meter()
while True:
    t, r = m.get()
    print(t, r.value, r.unit, r.display, r.flags)
```

## Troubleshooting

- **Linux: "Permission denied"**: install the udev rule (see above), then unplug and
  replug the meter.
- **macOS: cannot open device**: allow the app under *System Settings → Privacy &
  Security → Input Monitoring*.
- **Connected but no data**: the meter's USB output is off, or the meter has auto-powered off.

## Building

Pushing a tag `vX.Y.Z` makes GitHub Actions build all three installers and attach them
to a release (see `.github/workflows/build.yml`). The Windows and macOS builds use
PyInstaller. To build locally:

```sh
packaging/linux/build_deb.sh                                   # Linux .deb
pyinstaller --onefile --windowed --name VictorDMM victor_dmm_gui.py   # Windows/macOS
```

## Protocol

The cable sends 14-byte HID reports, which are a scrambled form of the Fortune
Semiconductor FS9922-DMM4 protocol. Each byte has a fixed pattern subtracted, its bits
reversed, and the bytes reordered. Decoding follows [libsigrok](https://sigrok.org/)
(`src/serial_hid_victor.c`, `src/dmm/fs9922.c`).

## License

MIT
