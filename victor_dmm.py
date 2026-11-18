#!/usr/bin/env python3
"""
victor_dmm.py - real-time reader/logger for VICTOR USB multimeters
(Victor 70C, 86C, 86D, ... with the USB-HID cable, USB ID 1244:d237).

Pure Python standard library -- no third-party packages needed.
Runs on Linux (hidraw), Windows (hid.dll via ctypes) and macOS (IOKit via ctypes).

Usage examples:
    python3 victor_dmm.py                       # show live readings
    python3 victor_dmm.py -o log.csv            # show and log to CSV
    python3 victor_dmm.py -o log.csv -i 1       # log at most one sample per second
    python3 victor_dmm.py -o log.csv -d 3600    # log for one hour, then stop
    python3 victor_dmm.py --list                # list detected meters

Protocol: the cable sends 14-byte HID reports, which are a scrambled version
of the Fortune Semiconductor FS9922-DMM4 serial protocol. Decoding follows
libsigrok (src/serial_hid_victor.c and src/dmm/fs9922.c).
"""

import argparse
import csv
import datetime as _dt
import math
import os
import queue
import sys
import threading
import time

__version__ = "1.0.0"

VID, PID = 0x1244, 0xD237
PACKET_LEN = 14


# --------------------------------------------------------------------------
# Protocol decoding
# --------------------------------------------------------------------------

_OBFUSCATION = b"jodenxunickxia"
_SHUFFLE = (6, 13, 5, 11, 2, 7, 9, 8, 3, 10, 12, 0, 4, 1)


def _bit_reverse(b):
    return int(f"{b:08b}"[::-1], 2)


def unscramble(raw):
    """Turn a 14-byte Victor HID report into a raw FS9922 packet (or None)."""
    if len(raw) != PACKET_LEN or not any(raw):
        return None  # the cable emits all-zero packets now and then
    out = bytearray(PACKET_LEN)
    for i in range(PACKET_LEN):
        out[PACKET_LEN - 1 - _SHUFFLE[i]] = _bit_reverse((raw[i] - _OBFUSCATION[i]) & 0xFF)
    return bytes(out)


class Reading:
    """One decoded meter reading."""

    __slots__ = ("value", "unit", "quantity", "display", "flags", "bargraph", "packet")

    def __init__(self, value, unit, quantity, display, flags, bargraph, packet):
        self.value = value          # float in SI base unit (inf = overload "OL")
        self.unit = unit            # e.g. "V", "A", "Ohm", "Hz", "F", "°C", "%", "hFE"
        self.quantity = quantity    # e.g. "voltage", "current", ...
        self.display = display      # as shown on the LCD, e.g. "12.34 mV"
        self.flags = flags          # list like ["DC", "AUTO", "HOLD"]
        self.bargraph = bargraph    # int or None
        self.packet = packet        # decoded FS9922 packet bytes

    @property
    def overload(self):
        return math.isinf(self.value)


_PREFIX = {"n": -9, "µ": -6, "m": -3, "": 0, "k": 3, "M": 6}


def decode(pkt):
    """Decode a 14-byte FS9922 packet. Returns Reading or raises ValueError."""
    if len(pkt) != PACKET_LEN:
        raise ValueError("bad length")
    if pkt[0] not in b"+-" or pkt[12:14] != b"\r\n":
        raise ValueError("bad framing")
    sign = -1 if pkt[0] == ord("-") else 1
    digits = pkt[1:5]
    overload = digits == b"?0:?"
    if not overload and not digits.isdigit():
        raise ValueError("bad digits %r" % digits)
    dp = {ord("0"): 0, ord("1"): 3, ord("2"): 2, ord("4"): 1}.get(pkt[6])
    if dp is None:
        raise ValueError("bad decimal point byte")

    b7, b8, b9, b10, b11 = pkt[7], pkt[8], pkt[9], pkt[10], pkt[11]
    f = lambda byte, bit: bool(byte & (1 << bit))
    auto, dc, ac, rel, hold, bpn = f(b7, 5), f(b7, 4), f(b7, 3), f(b7, 2), f(b7, 1), f(b7, 0)
    z1, z2, vmax, vmin, apo, bat, nano, z3 = (f(b8, i) for i in range(7, -1, -1))
    micro, milli, kilo, mega, beep, diode, percent, z4 = (f(b9, i) for i in range(7, -1, -1))
    volt, amp, ohm, hfe, hertz, farad, celsius, fahr = (f(b10, i) for i in range(7, -1, -1))

    prefixes = [p for p, on in (("n", nano), ("µ", micro), ("m", milli), ("k", kilo), ("M", mega)) if on]
    if len(prefixes) > 1:
        raise ValueError("multiple prefixes")
    prefix = prefixes[0] if prefixes else ""

    unit, quantity = "", ""
    for on, u, q in ((volt or diode, "V", "voltage"), (amp, "A", "current"), (ohm, "Ohm", "resistance"),
                     (hfe, "hFE", "gain"), (hertz, "Hz", "frequency"), (farad, "F", "capacitance"),
                     (celsius, "°C", "temperature"), (fahr, "°F", "temperature"),
                     (percent, "%", "duty cycle")):
        if on:
            unit, quantity = u, q
            break

    if overload:
        value = math.inf * sign
        number = ("-" if sign < 0 else "") + "OL"
    else:
        n = int(digits)
        number = ("-" if sign < 0 else "") + (f"{n / 10**dp:.{dp}f}" if dp else str(n))
        value = sign * n * 10.0 ** (-dp + _PREFIX[prefix])
        value = float(f"{value:.6g}")  # strip float noise, 4 digits is plenty
    display = f"{number} {prefix}{unit}".strip()

    flags = [name for name, on in (("AC", ac), ("DC", dc), ("AUTO", auto), ("HOLD", hold), ("REL", rel),
                                   ("MAX", vmax), ("MIN", vmin), ("DIODE", diode), ("BEEP", beep),
                                   ("BAT_LOW", bat), ("APO", apo)) if on]
    bargraph = ((-1 if b11 & 0x80 else 1) * (b11 & 0x7F)) if bpn else None
    return Reading(value, unit, quantity, display, flags, bargraph, bytes(pkt))


# --------------------------------------------------------------------------
# Platform HID back-ends. Each yields raw input reports (bytes) via read().
# --------------------------------------------------------------------------

class HidError(Exception):
    pass


class LinuxHid:
    """Uses the kernel hidraw interface (/dev/hidrawN)."""

    @staticmethod
    def enumerate():
        base = "/sys/class/hidraw"
        found = []
        if not os.path.isdir(base):
            return found
        for name in sorted(os.listdir(base)):
            try:
                with open(os.path.join(base, name, "device", "uevent")) as fh:
                    info = dict(line.strip().split("=", 1) for line in fh if "=" in line)
            except OSError:
                continue
            parts = info.get("HID_ID", "").split(":")
            if len(parts) == 3 and int(parts[1], 16) == VID and int(parts[2], 16) == PID:
                found.append(("/dev/" + name, info.get("HID_NAME", "")))
        return found

    def __init__(self, path):
        try:
            self.fd = os.open(path, os.O_RDONLY)
        except PermissionError:
            raise HidError(
                f"Permission denied opening {path}.\n"
                "Install the udev rule once (see README), e.g.:\n"
                "  sudo cp 70-victor-dmm.rules /etc/udev/rules.d/ && "
                "sudo udevadm control --reload && sudo udevadm trigger\n"
                "then unplug/replug the meter.")
        self.path = path

    def read(self, timeout=0.5):
        import select
        if not select.select([self.fd], [], [], timeout)[0]:
            return None
        data = os.read(self.fd, 64)
        if not data:
            raise HidError("device disconnected")
        return data

    def close(self):
        os.close(self.fd)


class WindowsHid:
    """Uses hid.dll / setupapi.dll through ctypes (no driver install needed)."""

    def __init__(self, path):
        import ctypes
        from ctypes import wintypes
        self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.k32.CreateFileW.restype = wintypes.HANDLE
        self.k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                         wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        self.k32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                      ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        self.k32.CloseHandle.argtypes = [wintypes.HANDLE]
        GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
        FILE_SHARE_RW, OPEN_EXISTING = 3, 3
        h = self.k32.CreateFileW(path, GENERIC_READ | GENERIC_WRITE, FILE_SHARE_RW, None, OPEN_EXISTING, 0, None)
        if h in (None, wintypes.HANDLE(-1).value):
            h = self.k32.CreateFileW(path, GENERIC_READ, FILE_SHARE_RW, None, OPEN_EXISTING, 0, None)
        if h in (None, wintypes.HANDLE(-1).value):
            raise HidError(f"cannot open {path} (error {ctypes.get_last_error()})")
        self.h = h
        self.ctypes, self.wintypes = ctypes, wintypes
        self.report_len = self._input_report_len() or 65

    def _input_report_len(self):
        ctypes = self.ctypes
        hid = ctypes.WinDLL("hid")
        pp = ctypes.c_void_p()
        hid.HidD_GetPreparsedData.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        if not hid.HidD_GetPreparsedData(self.h, ctypes.byref(pp)):
            return None
        caps = (ctypes.c_ushort * 64)()  # HIDP_CAPS is 64 bytes of USHORTs
        hid.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        hid.HidP_GetCaps(pp, caps)
        hid.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
        hid.HidD_FreePreparsedData(pp)
        return caps[2]  # Usage, UsagePage, InputReportByteLength, ...

    @staticmethod
    def enumerate():
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("d1", wintypes.DWORD), ("d2", wintypes.WORD), ("d3", wintypes.WORD),
                        ("d4", ctypes.c_ubyte * 8)]

        class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("guid", GUID), ("flags", wintypes.DWORD),
                        ("reserved", ctypes.c_void_p)]

        class HIDD_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("Size", wintypes.ULONG), ("VendorID", wintypes.USHORT),
                        ("ProductID", wintypes.USHORT), ("VersionNumber", wintypes.USHORT)]

        hid = ctypes.WinDLL("hid")
        setupapi = ctypes.WinDLL("setupapi")
        k32 = ctypes.WinDLL("kernel32")
        setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
        setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(GUID), wintypes.LPCWSTR, wintypes.HWND,
                                                  wintypes.DWORD]
        setupapi.SetupDiEnumDeviceInterfaces.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(GUID),
                                                         wintypes.DWORD, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA)]
        setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA), ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        hid.HidD_GetAttributes.argtypes = [wintypes.HANDLE, ctypes.POINTER(HIDD_ATTRIBUTES)]

        guid = GUID()
        hid.HidD_GetHidGuid(ctypes.byref(guid))
        DIGCF_PRESENT, DIGCF_DEVICEINTERFACE = 0x02, 0x10
        info = setupapi.SetupDiGetClassDevsW(ctypes.byref(guid), None, None, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
        found = []
        idx = 0
        while True:
            ifd = SP_DEVICE_INTERFACE_DATA()
            ifd.cbSize = ctypes.sizeof(SP_DEVICE_INTERFACE_DATA)
            if not setupapi.SetupDiEnumDeviceInterfaces(info, None, ctypes.byref(guid), idx, ctypes.byref(ifd)):
                break
            idx += 1
            need = wintypes.DWORD()
            setupapi.SetupDiGetDeviceInterfaceDetailW(info, ctypes.byref(ifd), None, 0, ctypes.byref(need), None)
            buf = ctypes.create_string_buffer(need.value + 4)
            # cbSize of SP_DEVICE_INTERFACE_DETAIL_DATA_W: 8 on 64-bit, 6 on 32-bit
            ctypes.c_uint32.from_buffer(buf).value = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
            if not setupapi.SetupDiGetDeviceInterfaceDetailW(info, ctypes.byref(ifd), buf, need, None, None):
                continue
            path = ctypes.wstring_at(ctypes.addressof(buf) + 4)
            h = k32.CreateFileW(path, 0, 3, None, 3, 0, None)  # no access rights: just query attributes
            if h in (None, wintypes.HANDLE(-1).value):
                continue
            attr = HIDD_ATTRIBUTES()
            attr.Size = ctypes.sizeof(HIDD_ATTRIBUTES)
            ok = hid.HidD_GetAttributes(h, ctypes.byref(attr))
            k32.CloseHandle(h)
            if ok and attr.VendorID == VID and attr.ProductID == PID:
                found.append((path, "VICTOR Multimeter"))
        setupapi.SetupDiDestroyDeviceInfoList(info)
        return found

    def read(self, timeout=None):  # blocking; close() from another thread unblocks it
        ctypes = self.ctypes
        buf = ctypes.create_string_buffer(self.report_len)
        n = self.wintypes.DWORD()
        if not self.k32.ReadFile(self.h, buf, self.report_len, ctypes.byref(n), None):
            raise HidError(f"read failed (error {ctypes.get_last_error()}) - device disconnected?")
        data = buf.raw[: n.value]
        return data[1:]  # strip the report-ID byte Windows always prepends

    def close(self):
        self.k32.CloseHandle(self.h)


class MacHid:
    """Uses IOKit's IOHIDManager through ctypes."""

    _lib = None

    @classmethod
    def _libs(cls):
        if cls._lib is None:
            import ctypes
            from ctypes import c_void_p, c_int32, c_long, c_char_p, c_uint32, c_double, c_bool
            iokit = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/IOKit.framework/IOKit")
            cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
            sig = {
                (cf, "CFStringCreateWithCString"): (c_void_p, [c_void_p, c_char_p, c_uint32]),
                (cf, "CFNumberGetValue"): (c_bool, [c_void_p, c_long, c_void_p]),
                (cf, "CFSetGetCount"): (c_long, [c_void_p]),
                (cf, "CFSetGetValues"): (None, [c_void_p, c_void_p]),
                (cf, "CFRelease"): (None, [c_void_p]),
                (cf, "CFRetain"): (c_void_p, [c_void_p]),
                (cf, "CFRunLoopGetCurrent"): (c_void_p, []),
                (cf, "CFRunLoopRunInMode"): (c_int32, [c_void_p, c_double, c_bool]),
                (cf, "CFStringGetCString"): (c_bool, [c_void_p, c_char_p, c_long, c_uint32]),
                (iokit, "IOHIDManagerCreate"): (c_void_p, [c_void_p, c_uint32]),
                (iokit, "IOHIDManagerSetDeviceMatching"): (None, [c_void_p, c_void_p]),
                (iokit, "IOHIDManagerCopyDevices"): (c_void_p, [c_void_p]),
                (iokit, "IOHIDDeviceGetProperty"): (c_void_p, [c_void_p, c_void_p]),
                (iokit, "IOHIDDeviceOpen"): (c_int32, [c_void_p, c_uint32]),
                (iokit, "IOHIDDeviceClose"): (c_int32, [c_void_p, c_uint32]),
                (iokit, "IOHIDDeviceScheduleWithRunLoop"): (None, [c_void_p, c_void_p, c_void_p]),
                (iokit, "IOHIDDeviceRegisterInputReportCallback"): (
                    None, [c_void_p, c_void_p, c_long, c_void_p, c_void_p]),
            }
            for (lib, name), (res, args) in sig.items():
                fn = getattr(lib, name)
                fn.restype, fn.argtypes = res, args
            cls._lib = (ctypes, iokit, cf)
        return cls._lib

    @classmethod
    def _cfstr(cls, s):
        _, _, cf = cls._libs()
        return cf.CFStringCreateWithCString(None, s.encode(), 0x08000100)

    @classmethod
    def _int_prop(cls, dev, key):
        ctypes, iokit, cf = cls._libs()
        k = cls._cfstr(key)
        ref = iokit.IOHIDDeviceGetProperty(dev, k)
        cf.CFRelease(k)
        if not ref:
            return None
        v = ctypes.c_int32()
        cf.CFNumberGetValue(ref, 3, ctypes.byref(v))  # kCFNumberSInt32Type
        return v.value

    @classmethod
    def _devices(cls):
        ctypes, iokit, cf = cls._libs()
        mgr = iokit.IOHIDManagerCreate(None, 0)
        iokit.IOHIDManagerSetDeviceMatching(mgr, None)
        devset = iokit.IOHIDManagerCopyDevices(mgr)
        devs = []
        if devset:
            n = cf.CFSetGetCount(devset)
            arr = (ctypes.c_void_p * n)()
            cf.CFSetGetValues(devset, arr)
            for d in arr:
                if cls._int_prop(d, "VendorID") == VID and cls._int_prop(d, "ProductID") == PID:
                    loc = cls._int_prop(d, "LocationID")
                    devs.append((f"usb-location-{loc:08x}" if loc is not None else "usb", cf.CFRetain(d)))
        return mgr, devs

    @classmethod
    def enumerate(cls):
        return [(path, "VICTOR Multimeter") for path, _ in cls._devices()[1]]

    def __init__(self, path):
        ctypes, iokit, cf = self._libs()
        self.mgr, devs = self._devices()
        match = [d for p, d in devs if p == path]
        if not match:
            raise HidError(f"device {path} not found")
        self.dev = match[0]
        if iokit.IOHIDDeviceOpen(self.dev, 0) != 0:
            raise HidError("IOHIDDeviceOpen failed (check System Settings > Privacy > Input Monitoring)")
        self.q = queue.Queue()
        self._buf = (ctypes.c_uint8 * 64)()
        CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_int32, ctypes.c_void_p, ctypes.c_uint32,
                              ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint8), ctypes.c_long)

        def on_report(ctx, result, sender, rtype, rid, report, length):
            self.q.put(bytes(report[:length]))

        self._cb = CB(on_report)  # keep reference alive
        self._scheduled = False
        self.ctypes = ctypes

    def read(self, timeout=0.5):
        ctypes, iokit, cf = self._libs()
        if not self._scheduled:  # must happen on the thread that runs the run loop
            iokit.IOHIDDeviceRegisterInputReportCallback(self.dev, self._buf, 64, self._cb, None)
            self._mode = ctypes.c_void_p.in_dll(cf, "kCFRunLoopDefaultMode")
            iokit.IOHIDDeviceScheduleWithRunLoop(self.dev, cf.CFRunLoopGetCurrent(), self._mode)
            self._scheduled = True
        try:
            return self.q.get_nowait()
        except queue.Empty:
            cf.CFRunLoopRunInMode(self._mode, timeout, True)
        try:
            return self.q.get_nowait()
        except queue.Empty:
            return None

    def close(self):
        _, iokit, _ = self._libs()
        iokit.IOHIDDeviceClose(self.dev, 0)


def backend():
    if sys.platform.startswith("linux"):
        return LinuxHid
    if sys.platform == "win32":
        return WindowsHid
    if sys.platform == "darwin":
        return MacHid
    raise HidError(f"unsupported platform {sys.platform}")


# --------------------------------------------------------------------------
# Reader thread: turns raw reports into decoded readings on a queue
# --------------------------------------------------------------------------

class Meter:
    def __init__(self, path=None, raw=False):
        be = backend()
        if path is None:
            devs = be.enumerate()
            if not devs:
                raise HidError("No VICTOR multimeter (USB 1244:d237) found. Is the cable plugged in "
                               "and the meter's USB/RS232 output enabled?")
            path = devs[0][0]
        self.path = path
        self.dev = be(path)
        self.raw = raw
        self.q = queue.Queue()
        self._buf = b""
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def close(self):
        """Stop the reader thread and release the device."""
        self._stop.set()
        if sys.platform == "win32":
            self.dev.close()  # unblocks the pending ReadFile
        else:
            self.thread.join(timeout=2)
            self.dev.close()

    def _run(self):
        try:
            while not self._stop.is_set():
                data = self.dev.read()
                if data is None:
                    continue
                t = time.time()
                for chunk in self._split(data):
                    pkt = unscramble(chunk)
                    if self.raw:
                        sys.stderr.write(f"raw {chunk.hex(' ')} -> {pkt.hex(' ') if pkt else '(skip)'}\n")
                    if pkt is None:
                        continue
                    try:
                        self.q.put((t, decode(pkt)))
                    except ValueError as e:
                        if self.raw:
                            sys.stderr.write(f"  decode error: {e}\n")
        except Exception as e:  # noqa: BLE001 - forward to main thread
            if not self._stop.is_set():
                self.q.put((time.time(), e))

    def _split(self, data):
        # Normally each report is exactly one 14-byte packet; be lenient otherwise.
        if len(data) == PACKET_LEN and not self._buf:
            return [data]
        self._buf += data
        out = []
        while len(self._buf) >= PACKET_LEN:
            out.append(self._buf[:PACKET_LEN])
            self._buf = self._buf[PACKET_LEN:]
        return out

    def get(self, timeout=None):
        t, item = self.q.get(timeout=timeout)
        if isinstance(item, Exception):
            raise item
        return t, item


# --------------------------------------------------------------------------
# Command line interface
# --------------------------------------------------------------------------

CSV_HEADER = ["timestamp", "elapsed_s", "value", "unit", "display", "quantity", "flags"]


def iso_time(t):
    return _dt.datetime.fromtimestamp(t).astimezone().isoformat(timespec="milliseconds")


def csv_row(t, elapsed, r):
    """One CSV row (matching CSV_HEADER) for reading r received at time t."""
    value = ("inf" if r.value > 0 else "-inf") if r.overload else repr(r.value)
    return [iso_time(t), f"{elapsed:.3f}", value, r.unit, r.display, r.quantity, " ".join(r.flags)]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Real-time reader/logger for VICTOR USB multimeters (1244:d237).")
    ap.add_argument("-o", "--output", help="write readings to this CSV file")
    ap.add_argument("-a", "--append", action="store_true", help="append to the output file instead of overwriting")
    ap.add_argument("-i", "--interval", type=float, default=0.0,
                    help="minimum seconds between logged samples (default: log every reading, ~2-3/s)")
    ap.add_argument("-d", "--duration", type=float, help="stop after this many seconds")
    ap.add_argument("-n", "--count", type=int, help="stop after this many logged samples")
    ap.add_argument("--delimiter", default=",", help="CSV delimiter (e.g. ';' for European Excel, '\\t' for TSV)")
    ap.add_argument("--device", help="device path (default: first meter found, see --list)")
    ap.add_argument("--list", action="store_true", help="list detected meters and exit")
    ap.add_argument("--raw", action="store_true", help="print raw/decoded packets to stderr (debugging)")
    ap.add_argument("-q", "--quiet", action="store_true", help="don't print readings to the terminal")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args(argv)
    delim = "\t" if args.delimiter in ("\\t", "tab") else args.delimiter

    try:
        if args.list:
            devs = backend().enumerate()
            for path, name in devs:
                print(f"{path}\t{name}")
            if not devs:
                print("no meters found", file=sys.stderr)
            return 0 if devs else 1
        meter = Meter(args.device, raw=args.raw)
    except HidError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    fh = writer = None
    if args.output:
        new_file = not (args.append and os.path.exists(args.output) and os.path.getsize(args.output) > 0)
        fh = open(args.output, "a" if args.append else "w", newline="", encoding="utf-8")
        writer = csv.writer(fh, delimiter=delim)
        if new_file:
            writer.writerow(CSV_HEADER)
            fh.flush()

    print(f"Reading from {meter.path}" + (f", logging to {args.output}" if args.output else "")
          + ". Press Ctrl+C to stop.", file=sys.stderr)

    start = time.time()
    t0 = None
    last_logged = -math.inf
    logged = 0
    stale_warned = False
    try:
        while True:
            if args.duration is not None and t0 is not None and time.time() - t0 >= args.duration:
                break
            try:
                t, r = meter.get(timeout=0.5)
            except queue.Empty:
                if t0 is None and not stale_warned and time.time() - start > 5:
                    print("No data yet - is the meter on and its PC/USB output enabled "
                          "(on most Victor models: long-press the REL/USB button)?", file=sys.stderr)
                    stale_warned = True
                continue
            if t0 is None:
                t0 = t
            if t - last_logged < args.interval:
                continue
            last_logged = t
            elapsed = t - t0
            ts = iso_time(t)
            flags = " ".join(r.flags)
            if writer:
                writer.writerow(csv_row(t, elapsed, r))
                fh.flush()
            if not args.quiet:
                print(f"{ts[11:23]}  {elapsed:9.2f} s  {r.display:>14}  {flags}", flush=True)
            logged += 1
            if args.count is not None and logged >= args.count:
                break
    except KeyboardInterrupt:
        pass
    except HidError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        if fh:
            fh.close()
            print(f"\nSaved {logged} samples to {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
