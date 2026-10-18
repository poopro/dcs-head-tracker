#!/usr/bin/env python3
"""
DCS Head Tracker -- GUI (PySide6).  Double-click run_gui.bat, or build an .exe with build_exe.bat.

Tracking engine runs in a background thread; the window only polls its latest snapshot,
so a slow UI can never delay the head-tracking packets.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

import opentrack_link
from dcs_headtracker import (
    Camera, PoseProcessor, Sender, clean_rotation, create_landmarker, ensure_model,
    install_hotkeys, parse_args, NOSE_TIP,
)

SETTINGS_PATH = Path.home() / ".dcs_headtracker.json"
SAVED_KEYS = ("camera", "width", "height", "port", "yaw_gain", "pitch_gain", "roll_gain",
              "smooth", "beta", "deadzone", "invert_yaw", "invert_pitch", "invert_roll",
              "translation", "auto_dcs", "stop_with_dcs", "beep", "run_at_login",
              "hotkey_center", "dcs_process", "manage_opentrack", "first_run_done")
DEFAULT_SMOOTH = 44  # -> min_cutoff ~= 1.0
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "DCS-HeadTracker"


def smooth_to_cutoff(s: float) -> float:
    """Smoothing slider 0..100 -> One-Euro min_cutoff 3.0 (snappy) .. 0.25 (very smooth)."""
    return 3.0 * (0.25 / 3.0) ** (s / 100.0)


# ----------------------------------------------------------------------------
# Automation helpers
# ----------------------------------------------------------------------------
def process_running(name: str) -> bool:
    """True if a process called `name` (case-insensitive, e.g. DCS.exe) is running."""
    n = name.strip().lower()
    if not n:
        return False
    try:
        import psutil
        return any((p.info.get("name") or "").lower() == n
                   for p in psutil.process_iter(["name"]))
    except ImportError:
        pass
    if os.name == "nt":  # fallback without psutil
        try:
            out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {name}", "/NH"],
                                 capture_output=True, text=True, timeout=5,
                                 creationflags=0x08000000).stdout.lower()  # CREATE_NO_WINDOW
            return n in out
        except (OSError, subprocess.SubprocessError):
            return False
    return False


class ProcessWatcher:
    """Polls for a process in a background thread; the GUI just reads `.running`."""

    def __init__(self, get_name, probe=process_running, interval: float = 2.0):
        self.get_name, self.probe, self.interval = get_name, probe, interval
        self.running = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.running = bool(self.probe(self.get_name()))
            except Exception:  # noqa: BLE001
                self.running = False
            self._stop.wait(self.interval)


OPENTRACK_PROCESS = "opentrack.exe"
OPENTRACK_URL = opentrack_link.OPENTRACK_URL


# ---- global hotkeys via the Windows RegisterHotKey API (no extra package, no hooks)
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY = 0x0312


def parse_hotkey(text: str):
    """'alt+c' -> (modifiers, virtual-key) or None if it cannot be understood."""
    names = {"alt": MOD_ALT, "ctrl": MOD_CONTROL, "control": MOD_CONTROL,
             "shift": MOD_SHIFT, "win": MOD_WIN, "windows": MOD_WIN}
    mods, key = 0, None
    for part in (p.strip().lower() for p in text.split("+")):
        if not part:
            return None
        if part in names:
            mods |= names[part]
        elif key is None:
            key = part
        else:
            return None
    if key is None:
        return None
    if len(key) == 1 and key.isalnum():
        return mods, ord(key.upper())
    if key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        return mods, 0x70 + int(key[1:]) - 1
    return None


class WinHotkeys:
    """RegisterHotKey wrapper. `api` is anything with RegisterHotKey/UnregisterHotKey (user32)."""

    def __init__(self, api="auto"):
        if api == "auto":
            api = None
            if os.name == "nt":
                try:
                    import ctypes
                    api = ctypes.windll.user32
                except Exception:  # noqa: BLE001
                    api = None
        self.api = api
        self._cb: dict[int, object] = {}
        self._next = 0x4C00

    def register(self, text: str, callback) -> "tuple[bool, str]":
        if self.api is None:
            return False, "not Windows"
        parsed = parse_hotkey(text)
        if parsed is None:
            return False, f"cannot understand '{text}'"
        mods, vk = parsed
        hid = self._next
        self._next += 1
        if not self.api.RegisterHotKey(None, hid, mods | MOD_NOREPEAT, vk):
            return False, f"{text.upper()} is already used by another program"
        self._cb[hid] = callback
        return True, ""

    def clear(self) -> None:
        for hid in list(self._cb):
            try:
                self.api.UnregisterHotKey(None, hid)
            except Exception:  # noqa: BLE001
                pass
        self._cb.clear()

    def dispatch(self, hid: int) -> bool:
        cb = self._cb.get(hid)
        if cb is None:
            return False
        cb()
        return True


def autostart_command() -> str:
    """Command line Windows should run at login (hidden in the tray)."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --minimized'
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")  # no console window
    return f'"{pyw if pyw.exists() else exe}" "{Path(__file__).resolve()}" --minimized'


def set_run_at_login(enabled: bool) -> tuple[bool, str]:
    """Add/remove the HKCU Run entry. Returns (ok, message)."""
    try:
        import winreg
    except ImportError:
        return False, "Start with Windows is only available on Windows."
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if enabled:
                winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, autostart_command())
            else:
                try:
                    winreg.DeleteValue(k, RUN_NAME)
                except FileNotFoundError:
                    pass
        return True, ""
    except OSError as e:
        return False, f"Could not update the Windows startup entry: {e}"


def beep() -> None:
    """Short confirmation tone for recenter (audible in-game). Windows only."""
    try:
        import winsound
    except ImportError:
        return
    threading.Thread(target=winsound.Beep, args=(880, 70), daemon=True).start()


def icon_path() -> "Path | None":
    """headtracker.ico next to the script, or inside the PyInstaller bundle."""
    for base in (getattr(sys, "_MEIPASS", None), Path(__file__).resolve().parent):
        if base:
            p = Path(base) / "headtracker.ico"
            if p.exists():
                return p
    return None


def make_cfg(argv=None):
    cfg = parse_args(argv or [])
    cfg.auto_dcs = True
    cfg.stop_with_dcs = True
    cfg.beep = True
    cfg.run_at_login = False
    cfg.dcs_process = "DCS.exe"
    cfg.manage_opentrack = True
    cfg.first_run_done = False
    cfg.smooth = DEFAULT_SMOOTH
    cfg.min_cutoff = smooth_to_cutoff(cfg.smooth)
    try:
        saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        for k in SAVED_KEYS:
            if k in saved:
                setattr(cfg, k, saved[k])
        cfg.min_cutoff = smooth_to_cutoff(cfg.smooth)
    except (OSError, ValueError):
        pass
    return cfg


def save_cfg(cfg) -> None:
    try:
        SETTINGS_PATH.write_text(json.dumps({k: getattr(cfg, k) for k in SAVED_KEYS}, indent=1),
                                 encoding="utf-8")
    except OSError:
        pass


# ----------------------------------------------------------------------------
# What the home screen should say (pure function, unit-tested)
# ----------------------------------------------------------------------------
def compute_home_state(*, state: str, found: bool, paused: bool, error: str, ot_installed: bool,
                       ot_running: bool, out_live: bool, game_attached: bool, dcs_running: bool,
                       auto_dcs: bool, ot_needs_restart: bool = False, manage_ot: bool = True) -> dict:
    """-> {level: ok|warn|bad|idle|info, title, sub, action}. action: None|install|open_ot|start|retry|restart_ot"""
    def r(level, title, sub, action=None):
        return {"level": level, "title": title, "sub": sub, "action": action}

    if state == "error":
        return r("bad", "Camera problem", error or "The camera could not be started.", "retry")
    if not ot_running:
        if not ot_installed:
            return r("bad", "opentrack is not installed",
                     "opentrack is the free program that hands head tracking to DCS. "
                     "Install it once and everything else is automatic.", "install")
        if not manage_ot:
            return r("warn", "opentrack is not running",
                     "Open it yourself, or turn on auto-setup in Settings.", "open_ot")
        return r("warn", "Starting opentrack...",
                 "It should appear in a few seconds. If not, press the button.", "open_ot")
    if state in ("starting", "loading"):
        return r("warn", "Starting camera...", "The camera light turns on first, then the face tracker loads.")
    if state == "demo":
        return r("info", "Test sweep running", "Sending a left-right sweep. Stop it to go back to the camera.")
    if state != "tracking":                       # idle / stopped
        if dcs_running and auto_dcs:
            return r("warn", "Starting...", "DCS is running, starting the camera.")
        if auto_dcs:
            return r("idle", "Ready - waiting for DCS",
                     "Tracking starts by itself when DCS opens. Nothing to do.", "start")
        return r("idle", "Ready", "Press Start to begin.", "start")
    if paused:
        return r("warn", "Paused", "Head tracking is paused. Press Pause again (F11) to resume.")
    if not found:
        return r("warn", "Can't see your face", "Face the camera in decent light. The view holds its last position.")
    if out_live:
        return r("ok", "Tracking", "Head data is flowing through opentrack." +
                 (" DCS is connected." if game_attached else ""))
    if dcs_running:
        return r("warn", "opentrack isn't passing data yet",
                 "Restart opentrack once so its auto-start setting applies, or press Start inside opentrack."
                 if ot_needs_restart else
                 "Press Start inside opentrack (its auto-start only works after a restart).",
                 "restart_ot" if ot_needs_restart else "open_ot")
    return r("ok", "Tracking", "Camera works. opentrack starts passing data when DCS opens.")


# ----------------------------------------------------------------------------
# Engine (thread)
# ----------------------------------------------------------------------------
@dataclass
class Snapshot:
    state: str = "idle"          # idle | starting | loading | tracking | demo | error | stopped
    message: str = ""
    found: bool = False
    pose: tuple = field(default_factory=lambda: (0.0,) * 6)
    fps: float = 0.0
    proc_ms: float = 0.0
    paused: bool = False
    frame: "np.ndarray | None" = None   # BGR preview, already mirrored
    seq: int = 0
    packets: int = 0             # UDP packets sent to opentrack so far


class Engine:
    def __init__(self, cfg, camera_factory=None, landmarker_factory=None):
        self.cfg = cfg
        self.camera_factory = camera_factory or (
            lambda c: Camera(c.camera, c.width, c.height, c.fps))
        self.landmarker_factory = landmarker_factory or (
            lambda c: create_landmarker(ensure_model(c.model)))
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._snap = Snapshot()
        self._center = False
        self._paused = False
        self.want_preview = True

    # -- control (thread-safe, callable from GUI or hotkey threads)
    def start(self, mode: str = "track") -> None:
        if self.running:
            return
        self._stop.clear()
        self._snap = Snapshot(state="starting", message="Starting...")
        target = self._run_demo if mode == "demo" else self._run_track
        self._thread = threading.Thread(target=target, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3.0)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def request_center(self) -> None:
        self._center = True
        if getattr(self.cfg, "beep", False):
            beep()

    def set_paused(self, v: bool) -> None:
        self._paused = bool(v)

    def toggle_pause(self) -> None:
        self._paused = not self._paused

    def snapshot(self) -> Snapshot:
        with self._lock:
            return Snapshot(**self._snap.__dict__)

    def _set(self, **kw) -> None:
        with self._lock:
            for k, v in kw.items():
                setattr(self._snap, k, v)

    # -- workers
    def _run_demo(self) -> None:
        cfg = self.cfg
        sender = Sender(cfg.host, cfg.port)
        t0 = time.perf_counter()
        self._set(state="demo", message=f"Sending test sweep to {cfg.host}:{cfg.port}")
        try:
            while not self._stop.is_set():
                t = time.perf_counter() - t0
                pose = (0.0, 0.0, 0.0, 60.0 * math.sin(2 * math.pi * 0.20 * t),
                        25.0 * math.sin(2 * math.pi * 0.13 * t), 0.0)
                sender.send(pose)
                self._set(pose=pose, found=True, packets=sender.count)
                time.sleep(1 / 60)
        finally:
            sender.send((0.0,) * 6)
            self._set(state="stopped", message="Stopped", pose=(0.0,) * 6, found=False)

    def _run_track(self) -> None:
        import cv2

        cfg = self.cfg
        sender = Sender(cfg.host, cfg.port)
        cam = lm = None
        proc = PoseProcessor(cfg)
        try:
            self._set(state="starting", message=f"Opening camera {cfg.camera}...")
            cam = self.camera_factory(cfg)          # camera first: its light turns on right away
            self._set(state="loading",
                      message="Loading face tracker (first run downloads a ~4 MB model)...")
            import mediapipe as mp                   # slow import (1-2 s) happens after the camera is open
            lm = self.landmarker_factory(cfg)
            self._set(state="tracking", message="")

            t_start = time.perf_counter()
            last_ts, misses = -1, 0
            n_frames = n_found = 0
            t_stat = t_start
            ms_avg = 0.0
            fps = 0.0
            seq = 0
            pose = (0.0,) * 6
            while not self._stop.is_set():
                frame = cam.read()
                if frame is None:
                    misses += 1
                    if misses >= 6:
                        raise RuntimeError(
                            "The camera opened but delivers no frames. Close other apps that use "
                            "it (Zoom, Teams, OBS...) or pick another camera.")
                    continue
                misses = 0
                t0 = time.perf_counter()
                ts = max(int((t0 - t_start) * 1000), last_ts + 1)
                last_ts = ts
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)  # NOT mirrored
                res = lm.detect_for_video(
                    mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), ts)
                n_frames += 1
                if self._center:
                    proc.request_center()
                    self._center = False

                found = bool(res.facial_transformation_matrixes)
                R = None
                if found:
                    n_found += 1
                    R, t = clean_rotation(res.facial_transformation_matrixes[0])
                    pose = (0.0,) * 6 if self._paused else proc.update(R, t, t0)
                else:
                    pose = (0.0,) * 6 if self._paused else proc.last  # freeze when lost
                sender.send(pose)
                ms_avg = 0.9 * ms_avg + 0.1 * (time.perf_counter() - t0) * 1000

                if self.want_preview:
                    disp = cv2.flip(frame, 1)
                    h, w = disp.shape[:2]
                    if found and R is not None:
                        lmk = res.face_landmarks[0][NOSE_TIP]
                        p0 = (int((1 - lmk.x) * w), int(lmk.y * h))
                        for k, col in enumerate(((0, 0, 255), (0, 255, 0), (255, 128, 0))):
                            v = R[:, k]
                            cv2.line(disp, p0,
                                     (p0[0] + int(-v[0] * 70), p0[1] + int(-v[1] * 70)), col, 2)
                    disp = cv2.resize(disp, (480, int(h * 480.0 / w)))
                    seq += 1
                else:
                    disp = None

                now = time.perf_counter()
                if now - t_stat > 0.5:
                    fps = n_frames / (now - t_stat)
                    n_frames = n_found = 0
                    t_stat = now
                self._set(found=found, pose=pose, fps=fps, proc_ms=ms_avg, paused=self._paused,
                          frame=disp, seq=seq, packets=sender.count)
        except SystemExit as e:  # ensure_model() reports download failures this way
            self._set(state="error", message=str(e.code))
        except Exception as e:  # noqa: BLE001
            self._set(state="error", message=str(e))
        finally:
            try:
                sender.send((0.0,) * 6)
            except OSError:
                pass
            if lm is not None:
                try:
                    lm.close()
                except Exception:  # noqa: BLE001
                    pass
            if cam is not None:
                cam.close()
            if self._snap.state != "error":
                self._set(state="stopped", message="Stopped")
            self._set(pose=(0.0,) * 6, found=False, frame=None)


# ----------------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------------
def build_ui():
    """Imported lazily so the engine can be used/tests run without Qt."""
    from PySide6 import QtCore, QtGui, QtWidgets
    import cv2

    QSS = """
    QWidget { background:#1b1e24; color:#e6e8eb; font-size:13px; }
    QGroupBox { border:1px solid #2e333c; border-radius:6px; margin-top:12px; padding:10px 8px 6px 8px; }
    QGroupBox::title { subcontrol-origin: margin; left:10px; padding:0 4px; color:#8f98a5; }
    QPushButton { background:#2b303a; border:1px solid #3a404c; border-radius:5px; padding:6px 12px; }
    QPushButton:hover { background:#343a46; }
    QPushButton:checked { background:#6b4e16; border-color:#c59a2b; }
    QPushButton#start { background:#1f7a55; border-color:#2fae7d; font-weight:600; font-size:14px; padding:10px; }
    QPushButton#start:hover { background:#25916a; }
    QPushButton#start[running="true"] { background:#9a3434; border-color:#d05555; }
    QComboBox, QSpinBox { background:#23272e; border:1px solid #3a404c; border-radius:4px; padding:3px 6px; }
    QSlider::groove:horizontal { height:4px; background:#3a404c; border-radius:2px; }
    QSlider::handle:horizontal { background:#4cc9a0; width:14px; margin:-6px 0; border-radius:7px; }
    QCheckBox::indicator { width:15px; height:15px; }
    QLabel#hint { color:#8f98a5; font-size:12px; }
    QFrame#card { background:#232830; border:1px solid #3a404c; border-radius:12px; }
    QFrame#card[level="ok"] { border:2px solid #2fae7d; }
    QFrame#card[level="warn"] { border:2px solid #c59a2b; }
    QFrame#card[level="bad"] { border:2px solid #d05555; }
    QFrame#card[level="info"] { border:2px solid #5aa7e0; }
    QFrame#card QLabel { background:transparent; }
    QLabel#cardtitle { font-size:22px; font-weight:700; }
    QLabel#cardsub { color:#a9b1bc; font-size:13px; }
    QLabel#pill { background:#23272e; border:1px solid #3a404c; border-radius:11px;
                  padding:3px 4px; color:#8f98a5; font-size:12px; }
    QLabel#pill[level="ok"] { color:#d8f5ea; background:#1c4d3b; border-color:#2fae7d; }
    QLabel#pill[level="warn"] { color:#fbeec5; background:#4d3d12; border-color:#c59a2b; }
    QLabel#pill[level="bad"] { color:#fbd6d6; background:#4d2020; border-color:#d05555; }
    QPushButton#bigstart { background:#1f7a55; border-color:#2fae7d; font-weight:700;
                           font-size:17px; padding:14px; border-radius:8px; }
    QPushButton#bigstart:hover { background:#25916a; }
    QPushButton#bigstart[running="true"] { background:#9a3434; border-color:#d05555; }
    QPushButton#cardaction { background:#2f6fb5; border-color:#4a90d9; font-weight:600; padding:8px; }
    QPushButton#cardaction:hover { background:#3a80cc; }
    QPushButton#gear { padding:4px 10px; font-size:15px; }
    QPushButton#recenter { padding:10px; font-size:14px; }
    """

    class CenterBar(QtWidgets.QWidget):
        def __init__(self, name: str, limit: float):
            super().__init__()
            self.name, self.limit, self.v = name, limit, 0.0
            self.setMinimumHeight(26)

        def setValue(self, v: float) -> None:
            self.v = v
            self.update()

        def paintEvent(self, _e):
            p = QtGui.QPainter(self)
            r = self.rect()
            p.fillRect(r, QtGui.QColor("#23272e"))
            mid = r.width() // 2
            frac = max(-1.0, min(1.0, self.v / self.limit))
            w = int(frac * mid)
            p.fillRect(QtCore.QRect(min(mid, mid + w), 0, abs(w), r.height()),
                       QtGui.QColor("#2fae7d"))
            p.setPen(QtGui.QColor("#3a404c"))
            p.drawLine(mid, 0, mid, r.height())
            p.setPen(QtGui.QColor("#ffffff"))
            p.drawText(r, QtCore.Qt.AlignCenter, f"{self.name}  {self.v:+.1f}°")

    class LabeledSlider(QtWidgets.QWidget):
        changed = QtCore.Signal(float)

        def __init__(self, title, lo, hi, value, fmt="{:.2f}", steps=200):
            super().__init__()
            self.lo, self.hi, self.fmt, self.steps = lo, hi, fmt, steps
            self.s = QtWidgets.QSlider(QtCore.Qt.Horizontal)
            self.s.setRange(0, steps)
            self.name = QtWidgets.QLabel(title)
            self.name.setMinimumWidth(110)
            self.val = QtWidgets.QLabel()
            self.val.setMinimumWidth(48)
            self.val.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            lay = QtWidgets.QHBoxLayout(self)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.addWidget(self.name)
            lay.addWidget(self.s, 1)
            lay.addWidget(self.val)
            self.set(value)
            self.s.valueChanged.connect(self._on)

        def _on(self, i):
            v = self.lo + (self.hi - self.lo) * i / self.steps
            self.val.setText(self.fmt.format(v))
            self.changed.emit(v)

        def set(self, v):
            self.s.blockSignals(True)
            self.s.setValue(int(round((v - self.lo) / (self.hi - self.lo) * self.steps)))
            self.s.blockSignals(False)
            self.val.setText(self.fmt.format(v))

    class MainWindow(QtWidgets.QMainWindow):
        def __init__(self, cfg=None, engine=None, hotkeys=True, process_probe=None,
                     watch_interval=2.0, use_tray=True, link=None, first_run=False,
                     startup_tasks=False):
            super().__init__()
            self.link = link or opentrack_link
            self._startup_tasks = startup_tasks
            try:
                self._ot_installed = self.link.find_opentrack() is not None
            except Exception:  # noqa: BLE001
                self._ot_installed = False
            self._ot_needs_restart = False
            self._ot_last_launch = 0.0
            self._ot_last_check = 0.0
            self._out_last_poll = 0.0
            self._ipc_server = None
            self.home_info = {}
            self.cfg = cfg or make_cfg()
            self.engine = engine or Engine(self.cfg)
            self.setWindowTitle("DCS Head Tracker")
            self.setStyleSheet(QSS)
            self._last_seq = -1
            self._err_shown = False
            self._really_quit = False
            self._tray_hinted = False
            self._prev_dcs = False
            self._auto_started = False
            self._hotkeys_enabled = hotkeys
            self._last_press = 0.0          # when the recenter hotkey last reached us
            self._press_count = 0
            self.hotkey_method = "off"      # "windows" | "keyboard" | "off"
            self.hotkey_error = ""
            self.winhk = WinHotkeys()
            self._hk_filter = None
            self.tray = None
            ip = icon_path()
            if ip is not None:
                self.setWindowIcon(QtGui.QIcon(str(ip)))
            self._build()
            self.timer = QtCore.QTimer(self)
            self.timer.timeout.connect(self._poll)
            self.timer.start(33)
            probe = process_probe or process_running
            self._probe = probe
            self.out_mon = self.link.OutputMonitor()
            self.watcher = ProcessWatcher(lambda: self.cfg.dcs_process, probe,
                                          interval=watch_interval).start()
            self.ot_watcher = ProcessWatcher(lambda: OPENTRACK_PROCESS, probe,
                                             interval=watch_interval).start()
            if use_tray:
                self._setup_tray()
            if self.winhk.api is not None:      # route WM_HOTKEY messages to WinHotkeys
                class _HkFilter(QtCore.QAbstractNativeEventFilter):
                    def __init__(self, hk):
                        super().__init__()
                        self.hk = hk

                    def nativeEventFilter(self, event_type, message):
                        try:
                            if "windows" in str(event_type):
                                import ctypes.wintypes as wt
                                msg = wt.MSG.from_address(int(message))
                                if msg.message == WM_HOTKEY and self.hk.dispatch(int(msg.wParam)):
                                    return True, 0
                        except Exception:  # noqa: BLE001
                            pass
                        return False, 0

                self._hk_filter = _HkFilter(self.winhk)
                QtCore.QCoreApplication.instance().installNativeEventFilter(self._hk_filter)
            self._register_hotkeys()
            self.stack.currentChanged.connect(self._page_changed)
            if first_run:
                self._first_run_setup()
            if startup_tasks:
                QtCore.QTimer.singleShot(400, self._startup)

        # ---- layout
        def _build(self):
            cfg = self.cfg
            root = QtWidgets.QWidget()
            grid = QtWidgets.QHBoxLayout(root)

            left = QtWidgets.QVBoxLayout()
            self.preview = QtWidgets.QLabel("Camera preview appears here")
            self.preview.setFixedSize(480, 360)
            self.preview.setAlignment(QtCore.Qt.AlignCenter)
            self.preview.setStyleSheet("background:#0f1115; border:1px solid #2e333c; color:#6c7480;")
            left.addWidget(self.preview)
            self.status = QtWidgets.QLabel("Idle - press Start tracking")
            self.status.setWordWrap(True)
            self.status.setMinimumHeight(40)
            left.addWidget(self.status)
            self.bars = {n: CenterBar(n, lim) for n, lim in
                         (("Yaw", 90.0), ("Pitch", 60.0), ("Roll", 45.0))}
            for b in self.bars.values():
                left.addWidget(b)
            self.stats = QtWidgets.QLabel("")
            self.stats.setObjectName("hint")
            left.addWidget(self.stats)

            g = QtWidgets.QGroupBox("Connection check")
            v = QtWidgets.QVBoxLayout(g)
            self.chk_cam = QtWidgets.QLabel("")
            self.chk_udp = QtWidgets.QLabel("")
            self.chk_ot = QtWidgets.QLabel("")
            self.chk_ot.setWordWrap(True)
            self.chk_hk = QtWidgets.QLabel("")
            self.chk_hk.setWordWrap(True)
            self.chk_dcs = QtWidgets.QLabel("")
            for w in (self.chk_cam, self.chk_udp, self.chk_ot, self.chk_hk, self.chk_dcs):
                v.addWidget(w)
            row = QtWidgets.QHBoxLayout()
            self.ot_btn = QtWidgets.QPushButton("Open opentrack")
            self.ot_btn.setToolTip("Starts opentrack if it is installed, otherwise opens its "
                                   "download page")
            self.ot_btn.clicked.connect(self._open_opentrack)
            self.key_test_btn = QtWidgets.QPushButton("Test recenter")
            self.key_test_btn.setToolTip("Same as pressing the hotkey: recenters and beeps")
            self.key_test_btn.clicked.connect(self._hotkey_pressed)
            row.addWidget(self.ot_btn)
            row.addWidget(self.key_test_btn)
            v.addLayout(row)
            left.addWidget(g)
            left.addStretch(1)
            grid.addLayout(left)

            right = QtWidgets.QVBoxLayout()
            grid.addLayout(right, 1)

            g = QtWidgets.QGroupBox("Camera && output")
            f = QtWidgets.QFormLayout(g)
            row = QtWidgets.QHBoxLayout()
            self.cam_box = QtWidgets.QComboBox()
            for i in range(4):
                self.cam_box.addItem(f"Camera {i}", i)
            self.cam_box.setCurrentIndex(max(0, min(self.cam_box.count() - 1, cfg.camera)))
            self.refresh_btn = QtWidgets.QPushButton("Scan")
            self.refresh_btn.setToolTip("Probe cameras 0-4 (the camera light may blink briefly)")
            self.refresh_btn.clicked.connect(self._scan)
            row.addWidget(self.cam_box, 1)
            row.addWidget(self.refresh_btn)
            f.addRow("Camera", row)
            self.res_box = QtWidgets.QComboBox()
            for w, h in ((640, 480), (1280, 720)):
                self.res_box.addItem(f"{w} x {h}", (w, h))
            self.res_box.setCurrentIndex(1 if cfg.width >= 1280 else 0)
            f.addRow("Resolution", self.res_box)
            self.port_spin = QtWidgets.QSpinBox()
            self.port_spin.setRange(1024, 65535)
            self.port_spin.setValue(cfg.port)
            f.addRow("opentrack UDP port", self.port_spin)
            right.addWidget(g)

            self.start_btn = QtWidgets.QPushButton("Start tracking")
            self.start_btn.setObjectName("start")
            self.start_btn.clicked.connect(self._toggle)
            right.addWidget(self.start_btn)
            row = QtWidgets.QHBoxLayout()
            self.center_btn = QtWidgets.QPushButton("Recenter")
            self.center_btn.clicked.connect(self.engine.request_center)
            self.pause_btn = QtWidgets.QPushButton("Pause")
            self.pause_btn.setCheckable(True)
            self.pause_btn.toggled.connect(self.engine.set_paused)
            self.demo_btn = QtWidgets.QPushButton("Test sweep")
            self.demo_btn.setToolTip("No camera: sends a left-right sweep so you can check "
                                     "opentrack and DCS")
            self.demo_btn.clicked.connect(self._demo)
            for b in (self.center_btn, self.pause_btn, self.demo_btn):
                row.addWidget(b)
            right.addLayout(row)

            g = QtWidgets.QGroupBox("Feel")
            v = QtWidgets.QVBoxLayout(g)
            specs = (
                ("yaw_gain", "Yaw gain", 1.0, 4.0, "{:.2f}x"),
                ("pitch_gain", "Pitch gain", 1.0, 4.0, "{:.2f}x"),
                ("roll_gain", "Roll gain", 0.5, 2.0, "{:.2f}x"),
                ("deadzone", "Deadzone", 0.0, 2.0, "{:.2f}°"),
                ("beta", "Responsiveness", 0.0, 0.2, "{:.3f}"),
            )
            self.sliders = {}
            for key, title, lo, hi, fmt in specs:
                s = LabeledSlider(title, lo, hi, getattr(cfg, key), fmt)
                s.changed.connect(lambda val, k=key: setattr(self.cfg, k, val))
                v.addWidget(s)
                self.sliders[key] = s
            s = LabeledSlider("Smoothing", 0, 100, cfg.smooth, "{:.0f}", steps=100)
            s.changed.connect(self._smooth)
            v.addWidget(s)
            self.sliders["smooth"] = s
            right.addWidget(g)

            g = QtWidgets.QGroupBox("Directions")
            row = QtWidgets.QHBoxLayout(g)
            self.checks = {}
            for key, title in (("invert_yaw", "Invert yaw"), ("invert_pitch", "Invert pitch"),
                               ("invert_roll", "Invert roll"), ("translation", "Lean (x/y/z)")):
                c = QtWidgets.QCheckBox(title)
                c.setChecked(bool(getattr(cfg, key)))
                c.toggled.connect(lambda val, k=key: setattr(self.cfg, k, bool(val)))
                row.addWidget(c)
                self.checks[key] = c
            right.addWidget(g)

            g = QtWidgets.QGroupBox("Automation")
            v = QtWidgets.QVBoxLayout(g)
            self.auto_checks = {}

            def add_check(key, title, handler=None):
                c = QtWidgets.QCheckBox(title)
                c.setChecked(bool(getattr(cfg, key)))
                c.toggled.connect(handler or (lambda val, k=key: setattr(self.cfg, k, bool(val))))
                self.auto_checks[key] = c
                return c

            row = QtWidgets.QHBoxLayout()
            row.addWidget(add_check("auto_dcs", "Start tracking automatically when"))
            self.proc_edit = QtWidgets.QLineEdit(cfg.dcs_process)
            self.proc_edit.setFixedWidth(90)
            self.proc_edit.setToolTip("Process name to watch (task manager name of the sim)")
            self.proc_edit.textChanged.connect(
                lambda t: setattr(self.cfg, "dcs_process", t.strip() or "DCS.exe"))
            row.addWidget(self.proc_edit)
            row.addWidget(QtWidgets.QLabel("starts"))
            row.addStretch(1)
            v.addLayout(row)
            v.addWidget(add_check("stop_with_dcs", "Stop tracking (camera off) when it closes"))
            v.addWidget(add_check("manage_opentrack", "Set up and start opentrack for me (recommended)"))
            v.addWidget(add_check("beep", "Beep when recentered"))
            v.addWidget(add_check("run_at_login", "Start with Windows (hidden in the tray)",
                                  self._run_at_login_toggled))
            row = QtWidgets.QHBoxLayout()
            row.addWidget(QtWidgets.QLabel("Recenter hotkey"))
            self.hk_edit = QtWidgets.QLineEdit(cfg.hotkey_center)
            self.hk_edit.setFixedWidth(110)
            self.hk_edit.setToolTip("e.g. alt+c, ctrl+shift+c, f10")
            self.hk_edit.editingFinished.connect(self._hotkey_edited)
            row.addWidget(self.hk_edit)
            self.dcs_label = QtWidgets.QLabel("DCS: not detected")
            row.addStretch(1)
            row.addWidget(self.dcs_label)
            v.addLayout(row)
            right.addWidget(g)

            self.hint = QtWidgets.QLabel("")
            self.hint.setObjectName("hint")
            self.hint.setWordWrap(True)
            right.addWidget(self.hint)
            tip = QtWidgets.QLabel("opentrack is configured automatically (UDP input on this port -> "
                                   "freetrack output, starts when DCS.exe runs). Profile: "
                                   "Documents\\opentrack-2.3\\DCS-HeadTracker.ini")
            tip.setObjectName("hint")
            tip.setWordWrap(True)
            right.addWidget(tip)
            right.addStretch(1)

            # ---- pages: Home (default, simple) and Settings (everything else)
            self.stack = QtWidgets.QStackedWidget()
            self.stack.addWidget(self._build_home())
            page = QtWidgets.QWidget()
            pl = QtWidgets.QVBoxLayout(page)
            pl.setContentsMargins(8, 8, 8, 8)
            top = QtWidgets.QHBoxLayout()
            self.back_btn = QtWidgets.QPushButton("←  Back")
            self.back_btn.clicked.connect(lambda: self.stack.setCurrentIndex(0))
            top.addWidget(self.back_btn)
            title = QtWidgets.QLabel("Settings")
            title.setStyleSheet("font-size:16px; font-weight:700;")
            top.addWidget(title)
            top.addStretch(1)
            pl.addLayout(top)
            self.settings_scroll = QtWidgets.QScrollArea()
            self.settings_scroll.setWidgetResizable(True)
            self.settings_scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
            self.settings_scroll.setWidget(root)
            pl.addWidget(self.settings_scroll, 1)
            self.stack.addWidget(page)
            self.setCentralWidget(self.stack)
            self._page_changed(0)

        def _build_home(self):
            cfg = self.cfg
            page = QtWidgets.QWidget()
            v = QtWidgets.QVBoxLayout(page)
            v.setContentsMargins(18, 14, 18, 14)
            v.setSpacing(10)

            top = QtWidgets.QHBoxLayout()
            t = QtWidgets.QLabel("DCS Head Tracker")
            t.setStyleSheet("font-size:17px; font-weight:700;")
            top.addWidget(t)
            top.addStretch(1)
            self.gear_btn = QtWidgets.QPushButton("⚙  Settings")
            self.gear_btn.setObjectName("gear")
            self.gear_btn.clicked.connect(lambda: self.stack.setCurrentIndex(1))
            top.addWidget(self.gear_btn)
            v.addLayout(top)

            self.card = QtWidgets.QFrame()
            self.card.setObjectName("card")
            self.card.setProperty("level", "idle")
            cl = QtWidgets.QVBoxLayout(self.card)
            cl.setContentsMargins(16, 14, 16, 14)
            cl.setSpacing(6)
            self.card_title = QtWidgets.QLabel("Starting...")
            self.card_title.setObjectName("cardtitle")
            self.card_title.setWordWrap(True)
            self.card_sub = QtWidgets.QLabel("")
            self.card_sub.setObjectName("cardsub")
            self.card_sub.setWordWrap(True)
            self.card_btn = QtWidgets.QPushButton("")
            self.card_btn.setObjectName("cardaction")
            self.card_btn.clicked.connect(self._home_action)
            self.card_btn.hide()
            cl.addWidget(self.card_title)
            cl.addWidget(self.card_sub)
            cl.addWidget(self.card_btn)
            self.card.setMinimumHeight(120)
            v.addWidget(self.card)

            pills = QtWidgets.QHBoxLayout()
            pills.setSpacing(6)
            self.pills = {}
            for name in ("Camera", "opentrack", "Output", "DCS"):
                p = QtWidgets.QLabel(name)
                p.setObjectName("pill")
                p.setAlignment(QtCore.Qt.AlignCenter)
                p.setProperty("level", "idle")
                pills.addWidget(p, 1)
                self.pills[name] = p
            v.addLayout(pills)

            self.home_start = QtWidgets.QPushButton("Start tracking")
            self.home_start.setObjectName("bigstart")
            self.home_start.setCursor(QtCore.Qt.PointingHandCursor)
            self.home_start.clicked.connect(self._toggle)
            v.addWidget(self.home_start)

            self.home_center = QtWidgets.QPushButton("Recenter  (Alt+C)")
            self.home_center.setObjectName("recenter")
            self.home_center.clicked.connect(self._hotkey_pressed)
            v.addWidget(self.home_center)

            self.home_preview = QtWidgets.QLabel("Camera preview appears here")
            self.home_preview.setFixedSize(404, 227)
            self.home_preview.setAlignment(QtCore.Qt.AlignCenter)
            self.home_preview.setStyleSheet(
                "background:#0f1115; border:1px solid #2e333c; border-radius:8px; color:#6c7480;")
            v.addWidget(self.home_preview, 0, QtCore.Qt.AlignHCenter)

            self.home_bars = {n: CenterBar(n, lim) for n, lim in
                              (("Yaw", 90.0), ("Pitch", 60.0), ("Roll", 45.0))}
            for b in self.home_bars.values():
                v.addWidget(b)

            self.home_note = QtWidgets.QLabel("")
            self.home_note.setObjectName("hint")
            self.home_note.setWordWrap(True)
            self.home_note.setAlignment(QtCore.Qt.AlignCenter)
            v.addWidget(self.home_note)
            v.addStretch(1)
            return page

        def _page_changed(self, idx):
            if idx == 0:
                self.setMinimumSize(0, 0)
                self.setFixedWidth(460)
                self.setMinimumHeight(700)
                self.setMaximumHeight(16777215)
                self.resize(460, 740)
            else:
                self.setMinimumWidth(0)
                self.setMaximumWidth(16777215)
                self.setMinimumSize(900, 700)
                self.resize(960, 800)

        # ---- actions
        def _smooth(self, s):
            self.cfg.smooth = s
            self.cfg.min_cutoff = smooth_to_cutoff(s)

        def _apply_connection(self):
            self.cfg.camera = int(self.cam_box.currentData())
            self.cfg.width, self.cfg.height = self.res_box.currentData()
            self.cfg.port = int(self.port_spin.value())

        def _set_running_ui(self, running: bool):
            self.start_btn.setText("Stop" if running else "Start tracking")
            self.start_btn.setProperty("running", "true" if running else "false")
            self.start_btn.style().unpolish(self.start_btn)
            self.start_btn.style().polish(self.start_btn)
            self.home_start.setText("Stop" if running else "Start tracking")
            self.home_start.setProperty("running", "true" if running else "false")
            self.home_start.style().unpolish(self.home_start)
            self.home_start.style().polish(self.home_start)
            for w in (self.cam_box, self.res_box, self.port_spin, self.refresh_btn,
                      self.demo_btn):
                w.setEnabled(not running)

        def _toggle(self):
            if self.engine.running:
                self._auto_started = False   # a manual stop is respected until DCS restarts
                self.engine.stop()
                self._set_running_ui(False)
                return
            self._apply_connection()
            self._err_shown = False
            self.engine.start("track")
            self._set_running_ui(True)

        def _demo(self):
            if self.engine.running:
                return
            self._apply_connection()
            self.engine.start("demo")
            self._set_running_ui(True)

        def _scan(self):
            import os
            backend = cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY
            found = []
            for i in range(5):
                cap = cv2.VideoCapture(i, backend)
                if cap.isOpened():
                    found.append(i)
                cap.release()
            self.cam_box.clear()
            for i in (found or [0]):
                self.cam_box.addItem(f"Camera {i}", i)
            self.status.setText(f"Found cameras: {found}" if found else
                                "No camera found. Check that it is plugged in and not in use.")

        # ---- polling
        def _set_status(self, text, color):
            self.status.setText(text)
            self.status.setStyleSheet(f"color:{color}; font-weight:600;")

        def _poll(self):
            s = self.engine.snapshot()
            visible = self.isVisible() and not self.isMinimized()
            self.engine.want_preview = visible      # no preview work while hidden in the tray
            if visible and s.frame is not None and s.seq != self._last_seq:
                self._last_seq = s.seq
                rgb = cv2.cvtColor(s.frame, cv2.COLOR_BGR2RGB)
                h, w, _ = rgb.shape
                img = QtGui.QImage(rgb.data, w, h, 3 * w, QtGui.QImage.Format_RGB888).copy()
                pm = QtGui.QPixmap.fromImage(img)
                for lab in (self.home_preview, self.preview):
                    lab.setPixmap(pm.scaled(lab.size(), QtCore.Qt.KeepAspectRatio,
                                            QtCore.Qt.SmoothTransformation))
            elif s.frame is None and s.state in ("idle", "stopped", "error", "demo"):
                for lab in (self.home_preview, self.preview):
                    lab.setPixmap(QtGui.QPixmap())
                    lab.setText("Camera preview appears here" if s.state != "demo"
                                else "Test sweep running (no camera)")
            for bar, hbar, val in zip(self.bars.values(), self.home_bars.values(), s.pose[3:]):
                bar.setValue(val)
                hbar.setValue(val)
            if self.pause_btn.isChecked() != s.paused and s.state == "tracking":
                self.pause_btn.blockSignals(True)
                self.pause_btn.setChecked(s.paused)
                self.pause_btn.blockSignals(False)

            if s.state == "idle":
                self._set_status("Idle - press Start tracking", "#8f98a5")
            elif s.state == "starting":
                self._set_status(s.message or "Opening camera...", "#e0b341")
            elif s.state == "loading":
                self._set_status(s.message, "#e0b341")
            elif s.state == "tracking":
                if s.paused:
                    self._set_status("Paused - sending centre", "#e0b341")
                elif s.found:
                    self._set_status("Tracking", "#2fae7d")
                else:
                    self._set_status("No face in view - holding last position", "#e06b6b")
            elif s.state == "demo":
                self._set_status(s.message, "#5aa7e0")
            elif s.state == "error":
                self._set_status("Error: " + s.message, "#e06b6b")
                if not self._err_shown:
                    self._err_shown = True
                    QtWidgets.QMessageBox.critical(self, "DCS Head Tracker", s.message)
            elif s.state == "stopped":
                self._set_status("Stopped", "#8f98a5")
            self.stats.setText(f"{s.fps:4.1f} fps   processing {s.proc_ms:4.1f} ms   "
                               f"-> UDP {self.cfg.host}:{self.cfg.port}" if s.state == "tracking"
                               else f"-> UDP {self.cfg.host}:{self.cfg.port}")

            if not self.engine.running and self.start_btn.text() == "Stop":
                self._set_running_ui(False)

            self._poll_opentrack()
            self._check_dcs()
            self._update_checks(s)
            self._update_home(s)
            if self.tray is not None:
                tip = "DCS Head Tracker - " + self.home_info.get("title", "")
                if self.tray.toolTip() != tip:
                    self.tray.setToolTip(tip)

        # ---- opentrack: detect, configure, launch
        def _poll_opentrack(self):
            now = time.monotonic()
            if now - self._out_last_poll >= 0.5:
                self._out_last_poll = now
                self.out_mon.poll()
            if self.ot_watcher.running:
                self._ot_installed = True
            elif now - self._ot_last_check >= 5.0:
                self._ot_last_check = now
                was = self._ot_installed
                try:
                    self._ot_installed = self.link.find_opentrack() is not None
                except Exception:  # noqa: BLE001
                    self._ot_installed = False
                if self._ot_installed and not was and self._startup_tasks:
                    self._ensure_opentrack()          # user just installed it: set it up for them
            if not self.ot_watcher.running:
                self._ot_needs_restart = False        # a fresh start reads the new settings

        def _ensure_opentrack(self):
            """Write our opentrack profile and make sure opentrack is running. Never loops."""
            if not self.cfg.manage_opentrack:
                return
            exe = self.link.find_opentrack()
            self._ot_installed = exe is not None
            if exe is None:
                return
            ok, msg, changed = self.link.configure_opentrack(self.cfg.port)
            if not ok:
                self._note(f"opentrack auto-setup problem: {msg}")
            running = bool(self._probe(OPENTRACK_PROCESS))
            if running:
                if changed:
                    self._ot_needs_restart = True
                return
            if time.monotonic() - self._ot_last_launch < 15.0:
                return                                # we just launched it; it is still coming up
            self._ot_last_launch = time.monotonic()
            okl, err = self.link.launch(exe)
            if not okl:
                self._note(f"Could not start opentrack: {err}")

        def _restart_opentrack(self):
            """User pressed 'Restart opentrack': force-close, rewrite settings, relaunch."""
            self.link.kill_opentrack()
            QtCore.QTimer.singleShot(900, self._relaunch_opentrack)

        def _relaunch_opentrack(self):
            self._ot_last_launch = 0.0
            self._ot_needs_restart = False
            exe = self.link.find_opentrack()
            if exe is None:
                return
            self.link.configure_opentrack(self.cfg.port)
            self._ot_last_launch = time.monotonic()
            okl, err = self.link.launch(exe)
            if not okl:
                self._note(f"Could not start opentrack: {err}")

        def _startup(self):
            self._ensure_opentrack()

        def _note(self, text):
            self.hint.setText(text)
            self.home_note.setText(text)

        # ---- home page
        _ACTION_TEXT = {"install": "Install opentrack", "open_ot": "Open opentrack",
                        "retry": "Try again", "restart_ot": "Restart opentrack"}

        def _update_home(self, s):
            st = compute_home_state(
                state=s.state, found=s.found, paused=s.paused, error=s.message,
                ot_installed=self._ot_installed, ot_running=self.ot_watcher.running,
                out_live=self.out_mon.live, game_attached=self.out_mon.game_attached,
                dcs_running=self.watcher.running, auto_dcs=self.cfg.auto_dcs,
                ot_needs_restart=self._ot_needs_restart, manage_ot=self.cfg.manage_opentrack)
            self.home_info = st
            if self.card.property("level") != st["level"]:
                self.card.setProperty("level", st["level"])
                self.card.style().unpolish(self.card)
                self.card.style().polish(self.card)
            self.card_title.setText(st["title"])
            self.card_sub.setText(st["sub"])
            act = st["action"]
            if act in self._ACTION_TEXT:
                self.card_btn.setText(self._ACTION_TEXT[act])
                self.card_btn.show()
            else:
                self.card_btn.hide()
            self._card_action = act

            def pill(name, level, text=None):
                p = self.pills[name]
                if text is not None:
                    p.setText(text)
                if p.property("level") != level:
                    p.setProperty("level", level)
                    p.style().unpolish(p)
                    p.style().polish(p)

            if s.state == "error":
                pill("Camera", "bad")
            elif s.state == "tracking":
                pill("Camera", "ok" if s.found else "warn")
            elif s.state in ("starting", "loading"):
                pill("Camera", "warn")
            else:
                pill("Camera", "idle")
            if self.ot_watcher.running:
                pill("opentrack", "ok")
            else:
                pill("opentrack", "warn" if self._ot_installed else "bad")
            pill("Output", "ok" if self.out_mon.live else "idle")
            pill("DCS", "ok" if self.watcher.running else "idle")

        def _home_action(self):
            act = getattr(self, "_card_action", None)
            if act == "install":
                self._install_opentrack()
            elif act == "open_ot":
                self._open_opentrack()
            elif act == "retry":
                self._err_shown = False
                if not self.engine.running:
                    self._apply_connection()
                    self.engine.start("track")
                    self._set_running_ui(True)
            elif act == "restart_ot":
                self._restart_opentrack()
            elif act == "start":
                self._toggle()

        def _start_file(self, path):
            try:
                os.startfile(str(path))                # type: ignore[attr-defined]  # Windows only
                return True
            except (AttributeError, OSError):
                return False

        def _open_url(self, url):
            import webbrowser
            webbrowser.open(url)

        def _install_opentrack(self):
            inst = self.link.find_installer()
            if inst is not None and self._start_file(inst):
                self._note("The opentrack installer opened. Finish it (Next, Next, Install), "
                           "then come back - everything else is automatic.")
                return
            self._open_url(OPENTRACK_URL)
            self._note("The opentrack download page opened. Download the setup .exe, run it, "
                       "then come back - everything else is automatic.")

        # ---- DCS auto start / stop
        def _check_dcs(self):
            dcs = self.watcher.running
            self.dcs_label.setText("DCS: running" if dcs else "DCS: not detected")
            self.dcs_label.setStyleSheet("color:%s;" % ("#2fae7d" if dcs else "#8f98a5"))
            if dcs and not self._prev_dcs:                      # DCS just started
                if self._startup_tasks:
                    self._ensure_opentrack()
                if self.cfg.auto_dcs and not self.engine.running:
                    self._apply_connection()
                    self._err_shown = False
                    self.engine.start("track")
                    self._set_running_ui(True)
                    self._auto_started = True
            elif not dcs and self._prev_dcs:                    # DCS just closed
                if self.cfg.stop_with_dcs and self._auto_started and self.engine.running:
                    self.engine.stop()
                    self._set_running_ui(False)
                self._auto_started = False
            self._prev_dcs = dcs

        # ---- hotkeys
        def _hotkey_pressed(self):
            """Recenter hotkey (any thread) or the 'Test recenter' button."""
            self._last_press = time.time()
            self._press_count += 1
            self.engine.request_center()

        def _pause_pressed(self):
            self.engine.toggle_pause()

        def _register_hotkeys(self):
            if self.winhk.api is not None:
                self.winhk.clear()
            self.hotkey_method, self.hotkey_error = "off", ""
            if self._hotkeys_enabled:
                ok_c, err = self.winhk.register(self.cfg.hotkey_center, self._hotkey_pressed)
                if ok_c:
                    self.hotkey_method = "windows"
                    self.winhk.register(self.cfg.hotkey_pause, self._pause_pressed)
                else:   # fall back to the `keyboard` package (low-level hook)
                    if install_hotkeys(self.cfg, self._hotkey_pressed, self._pause_pressed):
                        self.hotkey_method = "keyboard"
                    else:
                        self.hotkey_error = err if err and err != "not Windows" else \
                            "could not register it (pip install keyboard, or the key name is invalid)"
            else:
                self.hotkey_error = "disabled"
            self.hotkeys_ok = self.hotkey_method != "off"
            hk, pk = self.cfg.hotkey_center.upper(), self.cfg.hotkey_pause.upper()
            if self.hotkey_method == "windows":
                self.hint.setText(f"Hotkeys active: {hk} recenter, {pk} pause.")
            elif self.hotkey_method == "keyboard":
                self.hint.setText(
                    f"Hotkeys active (keyboard hook): {hk} recenter, {pk} pause. If DCS runs as "
                    "administrator, run this app as administrator too.")
            else:
                self.hint.setText(f"Hotkey {hk} is NOT active: {self.hotkey_error}. "
                                  "Use the Recenter button, or pick another key below.")
            self.home_center.setText(f"Recenter  ({hk})")
            if self.hotkey_method == "off":
                self.home_note.setText(f"{hk} hotkey is not active ({self.hotkey_error}). "
                                       "Use the Recenter button.")
            else:
                self.home_note.setText(f"{hk} recenter  ·  {pk} pause  ·  "
                                       "starts by itself when DCS opens")

        def _update_checks(self, s):
            def put(label, ok, text):  # ok: True green / False red / None grey
                mark = {True: "✔", False: "✘", None: "–"}[ok]
                color = {True: "#2fae7d", False: "#e06b6b", None: "#8f98a5"}[ok]
                label.setText(f"{mark} {text}")
                label.setStyleSheet(f"color:{color};")

            if s.state == "tracking":
                put(self.chk_cam, s.found, "Camera on, face found" if s.found
                    else "Camera on, but no face in view")
            elif s.state in ("starting", "loading"):
                put(self.chk_cam, None, "Camera starting...")
            else:
                put(self.chk_cam, None, "Camera off (press Start tracking)")
            put(self.chk_udp, True if s.packets > 0 else None,
                f"UDP packets sent to opentrack: {s.packets}" if s.packets else
                "No UDP packets sent yet")
            if self.ot_watcher.running:
                put(self.chk_ot, True, "opentrack is running")
            else:
                put(self.chk_ot, False,
                    "opentrack is NOT running, so DCS cannot receive anything. Start it with "
                    f"Input = UDP over network (port {self.cfg.port}), "
                    "Output = freetrack 2.0 Enhanced.")
            hk = self.cfg.hotkey_center.upper()
            age = time.time() - self._last_press
            if self._press_count and age < 5:
                extra = "" if self.engine.running else "  (tracker is not running, nothing to recenter)"
                put(self.chk_hk, True, f"{hk} received just now (#{self._press_count}){extra}")
            elif self.hotkey_method == "off":
                put(self.chk_hk, False, f"{hk} hotkey not active: {self.hotkey_error}")
            else:
                put(self.chk_hk, None, f"{hk} hotkey active ({self.hotkey_method}) - press it to test")
            put(self.chk_dcs, True if self.watcher.running else None,
                f"{self.cfg.dcs_process} running" if self.watcher.running
                else f"{self.cfg.dcs_process} not running")

        def _open_opentrack(self):
            exe = self.link.find_opentrack()
            if exe is None:
                self._ot_installed = False
                self._install_opentrack()
                return
            self._ot_installed = True
            if self.cfg.manage_opentrack:
                self.link.configure_opentrack(self.cfg.port)
            self._ot_last_launch = time.monotonic()
            ok, err = self.link.launch(exe)
            if not ok:
                self._note(f"Could not start opentrack: {err}")

        def _hotkey_edited(self):
            text = self.hk_edit.text().strip().lower()
            if text:
                self.cfg.hotkey_center = text
                self._register_hotkeys()

        def _run_at_login_toggled(self, on):
            ok, msg = set_run_at_login(bool(on))
            if ok:
                self.cfg.run_at_login = bool(on)
            else:
                self.auto_checks["run_at_login"].blockSignals(True)
                self.auto_checks["run_at_login"].setChecked(False)
                self.auto_checks["run_at_login"].blockSignals(False)
                self.cfg.run_at_login = False
                self._note(msg)

        # ---- first run: turn on everything a one-click app should have on
        def _first_run_setup(self):
            ok, msg = set_run_at_login(True)
            if ok:
                self.cfg.run_at_login = True
                c = self.auto_checks["run_at_login"]
                c.blockSignals(True)
                c.setChecked(True)
                c.blockSignals(False)
            self.cfg.first_run_done = True
            save_cfg(self.cfg)
            self.first_run_ran = True
            self.first_run_ok = ok

        # ---- single instance: a second launch just brings this window forward
        def start_ipc(self, name="DCSHeadTracker.ipc"):
            from PySide6 import QtNetwork
            QtNetwork.QLocalServer.removeServer(name)
            srv = QtNetwork.QLocalServer(self)
            if not srv.listen(name):
                return False

            def on_conn():
                while srv.hasPendingConnections():
                    sock = srv.nextPendingConnection()
                    sock.waitForReadyRead(200)
                    sock.readAll()
                    sock.disconnectFromServer()
                    self._show_window()

            srv.newConnection.connect(on_conn)
            self._ipc_server = srv
            return True

        # ---- tray
        def _setup_tray(self):
            self.tray = None
            if not QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
                return
            ip = icon_path()
            if ip is not None:
                icon = QtGui.QIcon(str(ip))
            else:  # fallback if the .ico file is missing
                pm = QtGui.QPixmap(64, 64)
                pm.fill(QtCore.Qt.transparent)
                p = QtGui.QPainter(pm)
                p.setRenderHint(QtGui.QPainter.Antialiasing)
                p.setBrush(QtGui.QColor("#2fae7d"))
                p.setPen(QtCore.Qt.NoPen)
                p.drawEllipse(4, 4, 56, 56)
                p.setBrush(QtGui.QColor("#1b1e24"))
                p.drawEllipse(22, 22, 20, 20)
                p.end()
                icon = QtGui.QIcon(pm)
                self.setWindowIcon(icon)
            self.tray = QtWidgets.QSystemTrayIcon(icon, self)
            menu = QtWidgets.QMenu()
            menu.addAction("Show window", self._show_window)
            menu.addAction("Recenter", self.engine.request_center)
            menu.addSeparator()
            menu.addAction("Quit", self._quit)
            self.tray.setContextMenu(menu)
            self._tray_menu = menu
            self.tray.activated.connect(
                lambda r: self._show_window()
                if r in (QtWidgets.QSystemTrayIcon.Trigger,
                         QtWidgets.QSystemTrayIcon.DoubleClick) else None)
            self.tray.show()
            QtWidgets.QApplication.instance().setQuitOnLastWindowClosed(False)

        def _show_window(self):
            self.showNormal()
            self.raise_()
            self.activateWindow()

        def _quit(self):
            self._really_quit = True
            self.close()
            QtWidgets.QApplication.instance().quit()

        def closeEvent(self, e):
            if self.tray is not None and not self._really_quit and self.tray.isVisible():
                self.hide()                      # keep running in the tray
                if not self._tray_hinted:
                    self._tray_hinted = True
                    self.tray.showMessage("DCS Head Tracker",
                                          "Still running in the tray. Right-click it to quit.")
                e.ignore()
                return
            self.timer.stop()
            self.watcher.stop()
            self.ot_watcher.stop()
            if self.winhk.api is not None:
                self.winhk.clear()
            self.engine.stop()
            self._apply_connection()
            save_cfg(self.cfg)
            if self.tray is not None:
                self.tray.hide()
            e.accept()

    return QtWidgets, MainWindow


IPC_NAME = "DCSHeadTracker.ipc"


def notify_running_instance(name: str = IPC_NAME) -> bool:
    """Tell an already-running instance to show its window. True if it answered."""
    try:
        from PySide6 import QtNetwork
        sock = QtNetwork.QLocalSocket()
        sock.connectToServer(name)
        if not sock.waitForConnected(800):
            return False
        sock.write(b"show")
        sock.waitForBytesWritten(500)
        sock.disconnectFromServer()
        return True
    except Exception:  # noqa: BLE001
        return False


def main() -> int:
    QtWidgets, MainWindow = build_ui()
    from PySide6 import QtCore

    if os.name == "nt":  # own taskbar identity, so the taskbar shows our icon, not python's
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("DCS.HeadTracker")
        except Exception:  # noqa: BLE001
            pass
    app = QtWidgets.QApplication(sys.argv)
    lock = QtCore.QLockFile(os.path.join(tempfile.gettempdir(), "dcs_headtracker.lock"))
    if not lock.tryLock(200):                    # one instance only: two would fight for the camera
        if not notify_running_instance():         # ask the running one to show itself
            QtWidgets.QMessageBox.information(
                None, "DCS Head Tracker",
                "DCS Head Tracker is already running (look in the system tray).")
        return 0
    cfg = make_cfg(sys.argv[1:])
    win = MainWindow(cfg, first_run=not cfg.first_run_done, startup_tasks=True)
    win.start_ipc(IPC_NAME)
    if win.cfg.minimized and win.tray is not None:
        pass                                     # start hidden in the tray
    elif win.cfg.minimized:
        win.showMinimized()
    else:
        win.show()
    code = app.exec()
    lock.unlock()
    return code


if __name__ == "__main__":
    sys.exit(main())
