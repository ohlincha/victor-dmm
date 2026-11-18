#!/usr/bin/env python3
"""
victor_dmm_gui.py - graphical real-time display, plot and logger for VICTOR
USB multimeters (USB ID 1244:d237).

Uses only the Python standard library (tkinter). Needs victor_dmm.py in the
same directory.

    python3 victor_dmm_gui.py
"""

import collections
import csv
import math
import os
import queue
import sys
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import victor_dmm as vd  # noqa: E402

WINDOWS = [("30 s", 30), ("1 min", 60), ("5 min", 300), ("15 min", 900), ("1 h", 3600), ("All", None)]
MAX_POINTS = 200_000  # ~18 h at 3 readings/s

PREFIXES = [(1e9, "G"), (1e6, "M"), (1e3, "k"), (1, ""), (1e-3, "m"), (1e-6, "µ"), (1e-9, "n"), (1e-12, "p")]


def fmt_si(v, unit="", digits=4):
    """Format a value with an SI prefix, e.g. 0.01234 V -> '12.34 mV'."""
    if v is None or math.isnan(v):
        return "—"
    if math.isinf(v):
        return ("-" if v < 0 else "") + "OL"
    if v == 0:
        return f"0 {unit}".strip()
    for scale, p in PREFIXES:
        if abs(v) >= scale * 0.9995:
            break
    return f"{v / scale:.{digits}g} {p}{unit}".strip()


def nice_ticks(lo, hi, n=6):
    """Round-numbered tick positions covering [lo, hi]."""
    span = hi - lo
    if span <= 0:
        return [lo]
    raw = span / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    first = math.ceil(lo / step) * step
    ticks, x = [], first
    while x <= hi + step * 1e-9:
        ticks.append(0.0 if abs(x) < step * 1e-9 else x)
        x += step
    return ticks


def fmt_duration(s):
    s = int(s)
    return f"{s // 3600:d}:{s // 60 % 60:02d}:{s % 60:02d}"


class Plot(tk.Canvas):
    """Minimal auto-scaling time-series plot."""

    PAD_L, PAD_R, PAD_T, PAD_B = 78, 16, 12, 30

    def __init__(self, master, **kw):
        super().__init__(master, background="white", highlightthickness=0, **kw)
        self.bind("<Configure>", lambda e: self.redraw())
        self.data = []
        self.unit = ""
        self.t_range = (0, 30)

    def set_data(self, points, unit, t_range):
        self.data, self.unit, self.t_range = points, unit, t_range
        self.redraw()

    def redraw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        x0, x1, y0, y1 = self.PAD_L, w - self.PAD_R, self.PAD_T, h - self.PAD_B
        if x1 - x0 < 20 or y1 - y0 < 20:
            return
        t_lo, t_hi = self.t_range
        if t_hi <= t_lo:
            t_hi = t_lo + 1
        vals = [v for _, v in self.data]
        if vals:
            v_lo, v_hi = min(vals), max(vals)
            if v_hi == v_lo:
                d = abs(v_lo) * 0.01 or 1e-3
                v_lo, v_hi = v_lo - d, v_hi + d
            pad = (v_hi - v_lo) * 0.08
            v_lo, v_hi = v_lo - pad, v_hi + pad
        else:
            v_lo, v_hi = -1, 1

        tx = lambda t: x0 + (t - t_lo) / (t_hi - t_lo) * (x1 - x0)
        vy = lambda v: y1 - (v - v_lo) / (v_hi - v_lo) * (y1 - y0)

        grid, text = "#e4e4e4", "#444"
        for v in nice_ticks(v_lo, v_hi, max(2, (y1 - y0) // 50)):
            y = vy(v)
            self.create_line(x0, y, x1, y, fill=grid)
            self.create_text(x0 - 6, y, text=fmt_si(v, self.unit), anchor="e", fill=text, font=("TkDefaultFont", 9))
        for t in nice_ticks(t_lo, t_hi, max(2, (x1 - x0) // 90)):
            x = tx(t)
            self.create_line(x, y0, x, y1, fill=grid)
            label = f"{t:g} s" if t_hi - t_lo < 120 else fmt_duration(t)
            self.create_text(x, y1 + 4, text=label, anchor="n", fill=text, font=("TkDefaultFont", 9))
        self.create_rectangle(x0, y0, x1, y1, outline="#999")

        if not self.data:
            self.create_text((x0 + x1) / 2, (y0 + y1) / 2, text="waiting for data…", fill="#999")
            return
        # Thin out to at most ~2 points per pixel for speed.
        pts = self.data
        stride = max(1, len(pts) // max(1, 2 * (x1 - x0)))
        coords = []
        for t, v in pts[::stride]:
            coords += (tx(t), vy(v))
        if len(coords) >= 4:
            self.create_line(*coords, fill="#1f6fd1", width=2)
        cx, cy = tx(pts[-1][0]), vy(pts[-1][1])
        self.create_oval(cx - 3, cy - 3, cx + 3, cy + 3, fill="#1f6fd1", outline="")


class App(ttk.Frame):
    def __init__(self, root):
        super().__init__(root, padding=8)
        self.root = root
        self.meter = None
        self.t0 = None
        self.points = collections.deque(maxlen=MAX_POINTS)  # (t, value, unit)
        self.last = None
        self.dirty = False
        self.stats_reset()
        # recording state
        self.rec_fh = self.rec_writer = None
        self.rec_count = 0
        self.rec_t0 = None
        self.rec_last = -math.inf

        self.grid(sticky="nsew")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self._build()
        self.refresh_devices(auto_connect=True)
        self.after(100, self.poll)
        self.after(250, self.redraw_plot)

    # ---------- UI ----------
    def _build(self):
        # device bar
        bar = ttk.Frame(self)
        bar.grid(row=0, column=0, sticky="ew")
        ttk.Label(bar, text="Meter:").pack(side="left")
        self.dev_var = tk.StringVar()
        self.dev_box = ttk.Combobox(bar, textvariable=self.dev_var, state="readonly", width=40)
        self.dev_box.pack(side="left", padx=4)
        ttk.Button(bar, text="Refresh", command=self.refresh_devices).pack(side="left")
        self.conn_btn = ttk.Button(bar, text="Connect", command=self.toggle_connect)
        self.conn_btn.pack(side="left", padx=4)

        # big reading
        top = ttk.Frame(self)
        top.grid(row=1, column=0, sticky="ew", pady=(8, 4))
        top.columnconfigure(0, weight=1)
        self.reading_var = tk.StringVar(value="—")
        tk.Label(top, textvariable=self.reading_var, font=("Courier", 44, "bold"), anchor="w",
                 fg="#111").grid(row=0, column=0, sticky="w")
        self.flags_var = tk.StringVar()
        ttk.Label(top, textvariable=self.flags_var, font=("TkDefaultFont", 12)).grid(row=1, column=0, sticky="w")

        stats = ttk.Frame(top)
        stats.grid(row=0, column=1, rowspan=2, sticky="ne")
        self.stat_vars = {}
        for i, k in enumerate(("Min", "Max", "Mean", "N")):
            ttk.Label(stats, text=k + ":").grid(row=i, column=0, sticky="e")
            self.stat_vars[k] = tk.StringVar(value="—")
            ttk.Label(stats, textvariable=self.stat_vars[k], width=14, font=("Courier", 11)).grid(
                row=i, column=1, sticky="w")
        ttk.Button(stats, text="Reset", command=self.stats_reset_ui).grid(row=4, column=0, columnspan=2, pady=(4, 0))

        # plot
        pf = ttk.Frame(self)
        pf.grid(row=2, column=0, sticky="nsew")
        self.rowconfigure(2, weight=1)
        ctl = ttk.Frame(pf)
        ctl.pack(fill="x")
        ttk.Label(ctl, text="Time window:").pack(side="left")
        self.win_var = tk.StringVar(value="1 min")
        ttk.Combobox(ctl, textvariable=self.win_var, values=[w for w, _ in WINDOWS], state="readonly",
                     width=7).pack(side="left", padx=4)
        self.win_var.trace_add("write", lambda *a: self.mark_dirty())
        self.pause_var = tk.BooleanVar()
        ttk.Checkbutton(ctl, text="Pause plot", variable=self.pause_var,
                        command=self.mark_dirty).pack(side="left", padx=8)
        ttk.Button(ctl, text="Clear plot", command=self.clear_plot).pack(side="left")
        self.plot = Plot(pf, width=760, height=320)
        self.plot.pack(fill="both", expand=True, pady=4)

        # recording
        rec = ttk.LabelFrame(self, text="Logging to file", padding=6)
        rec.grid(row=3, column=0, sticky="ew", pady=(4, 0))
        rec.columnconfigure(1, weight=1)
        ttk.Label(rec, text="File:").grid(row=0, column=0, sticky="w")
        self.file_var = tk.StringVar(value=os.path.abspath(time.strftime("dmm_%Y%m%d_%H%M%S.csv")))
        self.file_entry = ttk.Entry(rec, textvariable=self.file_var)
        self.file_entry.grid(row=0, column=1, sticky="ew", padx=4)
        self.browse_btn = ttk.Button(rec, text="Browse…", command=self.browse)
        self.browse_btn.grid(row=0, column=2)
        opts = ttk.Frame(rec)
        opts.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        ttk.Label(opts, text="Interval (s, 0 = every reading):").pack(side="left")
        self.interval_var = tk.StringVar(value="0")
        self.interval_box = ttk.Spinbox(opts, from_=0, to=86400, increment=1, textvariable=self.interval_var, width=7)
        self.interval_box.pack(side="left", padx=4)
        self.append_var = tk.BooleanVar(value=False)
        self.append_chk = ttk.Checkbutton(opts, text="Append if file exists", variable=self.append_var)
        self.append_chk.pack(side="left", padx=8)
        self.rec_btn = ttk.Button(opts, text="● Start logging", command=self.toggle_record)
        self.rec_btn.pack(side="right")
        self.rec_var = tk.StringVar(value="Not logging")
        ttk.Label(opts, textvariable=self.rec_var).pack(side="right", padx=8)

        # status bar
        self.status_var = tk.StringVar()
        ttk.Label(self, textvariable=self.status_var, foreground="#555").grid(row=4, column=0, sticky="w",
                                                                              pady=(6, 0))

    def status(self, msg):
        self.status_var.set(msg)

    # ---------- device ----------
    def refresh_devices(self, auto_connect=False):
        try:
            devs = vd.backend().enumerate()
        except Exception as e:  # noqa: BLE001
            devs = []
            self.status(f"Device scan failed: {e}")
        self.devs = devs
        self.dev_box["values"] = [f"{p}  ({n})" if n else p for p, n in devs]
        if devs:
            self.dev_box.current(0)
            if not self.meter:
                self.status(f"Found {len(devs)} meter(s).")
            if auto_connect and not self.meter:
                self.connect()
        else:
            self.dev_var.set("")
            self.status("No VICTOR meter found. Plug in the USB cable and press Refresh.")

    def toggle_connect(self):
        self.disconnect() if self.meter else self.connect()

    def connect(self):
        idx = self.dev_box.current()
        if idx < 0:
            self.refresh_devices()
            idx = self.dev_box.current()
            if idx < 0:
                return
        path = self.devs[idx][0]
        try:
            self.meter = vd.Meter(path)
        except vd.HidError as e:
            self.meter = None
            messagebox.showerror("Cannot open meter", str(e))
            self.status(f"Cannot open {path}")
            return
        self.connected_at = time.time()
        self.got_data = False
        self.conn_btn.config(text="Disconnect")
        self.status(f"Connected to {path}. Waiting for data… (enable USB output on the meter if nothing appears)")

    def disconnect(self, reason=None):
        if self.meter:
            try:
                self.meter.close()
            except Exception:  # noqa: BLE001
                pass
        self.meter = None
        self.conn_btn.config(text="Connect")
        self.reading_var.set("—")
        self.flags_var.set("")
        self.status(reason or "Disconnected.")

    # ---------- data ----------
    def poll(self):
        if self.meter:
            try:
                for _ in range(100):
                    t, r = self.meter.get(timeout=0)
                    self.on_reading(t, r)
            except queue.Empty:
                pass
            except Exception as e:  # noqa: BLE001 - device error from reader thread
                self.disconnect(f"Connection lost: {e}")
                if self.rec_fh:
                    self.status(f"Connection lost: {e} — logging paused, file still open.")
            if self.meter and not self.got_data and time.time() - self.connected_at > 5:
                self.status("No data from meter. Is it switched on with USB/PC output enabled "
                            "(long-press REL/USB on most Victor models)?")
                self.connected_at = math.inf
        self.after(100, self.poll)

    def on_reading(self, t, r):
        if not self.got_data:
            self.got_data = True
            self.status(f"Receiving data from {self.meter.path}.")
        if self.t0 is None:
            self.t0 = t
        if self.last is not None and self.last.unit != r.unit:
            self.stats_reset()  # function switch: stats in different units make no sense
        self.last = r
        self.reading_var.set(r.display)
        self.flags_var.set("  ".join(filter(None, [r.quantity.capitalize()] + r.flags)))
        if not r.overload:
            self.points.append((t, r.value, r.unit))
            self.s_n += 1
            self.s_sum += r.value
            self.s_min = min(self.s_min, r.value)
            self.s_max = max(self.s_max, r.value)
            self.stat_vars["Min"].set(fmt_si(self.s_min, r.unit))
            self.stat_vars["Max"].set(fmt_si(self.s_max, r.unit))
            self.stat_vars["Mean"].set(fmt_si(self.s_sum / self.s_n, r.unit, 5))
            self.stat_vars["N"].set(str(self.s_n))
        self.mark_dirty()
        self.record(t, r)

    def stats_reset(self):
        self.s_n, self.s_sum, self.s_min, self.s_max = 0, 0.0, math.inf, -math.inf

    def stats_reset_ui(self):
        self.stats_reset()
        for v in self.stat_vars.values():
            v.set("—")

    def clear_plot(self):
        self.points.clear()
        self.t0 = None
        self.mark_dirty()

    def mark_dirty(self):
        self.dirty = True

    def redraw_plot(self):
        if self.dirty and not self.pause_var.get():
            self.dirty = False
            window = dict(WINDOWS).get(self.win_var.get())
            unit = self.last.unit if self.last else ""
            now = (self.points[-1][0] if self.points else time.time())
            t0 = self.t0 or now
            pts = [(t - t0, v) for t, v, u in self.points if u == unit and (window is None or t >= now - window)]
            if window is None:
                lo = pts[0][0] if pts else 0
                hi = max(pts[-1][0] if pts else 0, lo + 10)
            else:
                hi = max(now - t0, window)
                lo = hi - window
            self.plot.set_data(pts, unit, (lo, hi))
        self.after(250, self.redraw_plot)

    # ---------- logging ----------
    def browse(self):
        path = filedialog.asksaveasfilename(
            title="Log to file", defaultextension=".csv", initialfile=os.path.basename(self.file_var.get()),
            initialdir=os.path.dirname(self.file_var.get()),
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")], confirmoverwrite=False)
        if path:
            self.file_var.set(path)

    def toggle_record(self):
        self.stop_record() if self.rec_fh else self.start_record()

    def start_record(self):
        path = self.file_var.get().strip()
        try:
            self.rec_interval = max(0.0, float(self.interval_var.get() or 0))
        except ValueError:
            messagebox.showerror("Invalid interval", "The interval must be a number of seconds.")
            return
        if not path:
            return self.browse()
        append = self.append_var.get()
        exists = os.path.exists(path) and os.path.getsize(path) > 0
        if exists and not append and not messagebox.askyesno(
                "Overwrite file?", f"{path}\nalready exists. Overwrite it?\n\n"
                "(Tick “Append if file exists” to add to it instead.)"):
            return
        try:
            self.rec_fh = open(path, "a" if append else "w", newline="", encoding="utf-8")
        except OSError as e:
            messagebox.showerror("Cannot open file", str(e))
            return
        self.rec_writer = csv.writer(self.rec_fh)
        if not (append and exists):
            self.rec_writer.writerow(vd.CSV_HEADER)
            self.rec_fh.flush()
        self.rec_path = path
        self.rec_count = 0
        self.rec_t0 = None
        self.rec_last = -math.inf
        self.rec_btn.config(text="■ Stop logging")
        for w in (self.file_entry, self.browse_btn, self.interval_box, self.append_chk):
            w.state(["disabled"])
        self.update_rec_label()
        if not self.meter:
            self.status("Logging armed — connect the meter to start receiving data.")

    def stop_record(self):
        if self.rec_fh:
            self.rec_fh.close()
            self.status(f"Saved {self.rec_count} samples to {self.rec_path}")
        self.rec_fh = self.rec_writer = None
        self.rec_btn.config(text="● Start logging")
        for w in (self.file_entry, self.browse_btn, self.interval_box, self.append_chk):
            w.state(["!disabled"])
        self.rec_var.set("Not logging")
        # suggest a fresh file name for the next run
        d = os.path.dirname(self.file_var.get()) or os.getcwd()
        self.file_var.set(os.path.join(d, time.strftime("dmm_%Y%m%d_%H%M%S.csv")))

    def record(self, t, r):
        if not self.rec_fh or t - self.rec_last < self.rec_interval:
            return
        if self.rec_t0 is None:
            self.rec_t0 = t
        self.rec_last = t
        try:
            self.rec_writer.writerow(vd.csv_row(t, t - self.rec_t0, r))
            self.rec_fh.flush()
        except OSError as e:
            self.stop_record()
            messagebox.showerror("Write error", f"Logging stopped: {e}")
            return
        self.rec_count += 1
        self.update_rec_label()

    def update_rec_label(self):
        dur = (self.rec_last - self.rec_t0) if self.rec_t0 else 0
        self.rec_var.set(f"Logging: {self.rec_count} samples, {fmt_duration(dur)}")

    def on_close(self):
        if self.rec_fh and not messagebox.askokcancel("Quit", "Logging is running. Stop logging and quit?"):
            return
        self.stop_record()
        self.disconnect()
        self.root.destroy()


def main():
    root = tk.Tk()
    root.title("VICTOR DMM")
    root.minsize(640, 480)
    root.geometry("900x640")
    app = App(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
