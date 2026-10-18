import math, os, sys, tempfile, time, types
from types import SimpleNamespace

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["HOME"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import mediapipe  # pre-import: keeps timing deterministic
import dcs_headtracker as core
import dcs_headtracker_gui as gui

QtWidgets, MainWindow = gui.build_ui()
from PySide6 import QtCore
app = QtWidgets.QApplication([])
QtWidgets.QMessageBox.critical = staticmethod(lambda *a, **k: None)

class FakeCam:
    def __init__(self, *_): pass
    def read(self, timeout=0.5):
        time.sleep(1 / 30); return np.full((480, 640, 3), 40, np.uint8)
    def close(self): pass
class FakeLM:
    def detect_for_video(self, img, ts):
        M = np.eye(4); M[:3, :3] = core.R_from_euler(0, 0, 0) * 5.0; M[:3, 3] = [0, 0, -40]
        return SimpleNamespace(facial_transformation_matrixes=[M],
                               face_landmarks=[[SimpleNamespace(x=0.5, y=0.5)] * 5])
    def close(self): pass

def pump(sec):
    t = time.time()
    while time.time() - t < sec:
        app.processEvents(); time.sleep(0.01)

fails = 0
def check(name, ok, extra=""):
    global fails
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}"); fails += (not ok)

def fresh(probe, **kw):
    cfg = gui.make_cfg(); cfg.port = 45999
    eng = gui.Engine(cfg, camera_factory=lambda c: FakeCam(), landmarker_factory=lambda c: FakeLM())
    win = MainWindow(cfg, eng, hotkeys=False, process_probe=probe, watch_interval=0.05, use_tray=False, **kw)
    win.show(); return cfg, eng, win

# ---------------- defaults
check("default recenter hotkey is alt+c", gui.make_cfg().hotkey_center == "alt+c")
check("auto-start / stop-with-dcs default on", gui.make_cfg().auto_dcs and gui.make_cfg().stop_with_dcs)

# ---------------- DCS auto start / stop
dcs = {"up": False, "name": None, "seen": set()}
def probe(name):
    dcs["seen"].add(name)
    if name.lower() != "opentrack.exe":
        dcs["name"] = name
    return dcs["up"] and name.lower() == "dcs.exe"
cfg, eng, win = fresh(probe)
pump(0.4)
check("idle while DCS is not running", not eng.running and win.start_btn.text() == "Start tracking")
dcs["up"] = True; pump(1.2)
check("DCS starts -> tracking starts by itself", eng.running and eng.snapshot().state == "tracking",
      f"(state={eng.snapshot().state})")
check("button shows Stop, label shows DCS running", win.start_btn.text() == "Stop" and "running" in win.dcs_label.text())
dcs["up"] = False; pump(0.8)
check("DCS closes -> tracking stops (camera released)", not eng.running and win.start_btn.text() == "Start tracking")

dcs["up"] = True; pump(1.0)
check("DCS starts again -> starts again", eng.running)
win._toggle(); pump(0.8)   # user presses Stop while DCS still runs
check("manual Stop is respected while DCS keeps running", not eng.running)
dcs["up"] = False; pump(0.4); dcs["up"] = True; pump(1.0)
check("...but the next DCS launch starts it again", eng.running)
win._toggle(); pump(0.3); dcs["up"] = False; pump(0.3)

# auto_dcs off
win.auto_checks["auto_dcs"].setChecked(False)
dcs["up"] = True; pump(1.0)
check("auto-start unchecked -> nothing starts", not eng.running)
dcs["up"] = False; pump(0.3); win.auto_checks["auto_dcs"].setChecked(True)

# manual start before DCS: must not be stopped when DCS closes
win._toggle(); pump(1.0)
dcs["up"] = True; pump(0.4); dcs["up"] = False; pump(0.6)
check("manually started tracking is NOT stopped when DCS closes", eng.running)
win._toggle(); pump(0.4)

# stop_with_dcs off
win.auto_checks["stop_with_dcs"].setChecked(False)
dcs["up"] = True; pump(1.0); dcs["up"] = False; pump(0.6)
check("stop_with_dcs unchecked -> keeps tracking after DCS closes", eng.running)
win._toggle(); pump(0.4); win.auto_checks["stop_with_dcs"].setChecked(True)

# DCS already running when the tracker app launches
dcs["up"] = True
cfgB, engB, winB = fresh(probe); pump(1.2)
check("DCS already running at app launch -> starts", engB.running)
winB.proc_edit.setText("Other.exe"); pump(0.3)
check("process name is editable and used by the watcher", "Other.exe" in dcs["seen"])
winB.close(); win.close()

# ---------------- hotkeys: registration wiring (fake `keyboard` module)
rec = {"added": [], "cleared": 0}
fake_kb = types.ModuleType("keyboard")
fake_kb.clear_all_hotkeys = lambda: rec.__setitem__("cleared", rec["cleared"] + 1)
fake_kb.add_hotkey = lambda k, cb: rec["added"].append((k, cb))
sys.modules["keyboard"] = fake_kb
cfgC = gui.make_cfg(); cfgC.port = 45999
engC = gui.Engine(cfgC, camera_factory=lambda c: FakeCam(), landmarker_factory=lambda c: FakeLM())
winC = MainWindow(cfgC, engC, hotkeys=True, process_probe=lambda n: False, watch_interval=1, use_tray=False)
keys = [k for k, _ in rec["added"]]
check("alt+c registered as the recenter hotkey", "alt+c" in keys, f"({keys})")
check("hint mentions admin caveat", "administrator" in winC.hint.text())
cb = dict(rec["added"])["alt+c"]
gui_beeps = []; gui.beep = lambda: gui_beeps.append(1)
cb()  # simulate pressing Alt+C
check("Alt+C callback requests a recenter + beep", engC._center is True and len(gui_beeps) == 1)
winC.hk_edit.setText("ctrl+shift+c"); winC.hk_edit.editingFinished.emit()
keys2 = [k for k, _ in rec["added"]]
check("changing the hotkey re-registers it (old ones cleared)", "ctrl+shift+c" in keys2 and rec["cleared"] >= 2)
winC.auto_checks["beep"].setChecked(False); engC._center = False; engC.request_center()
check("beep can be turned off", len(gui_beeps) == 1)
winC.close()
del sys.modules["keyboard"]

# ---------------- start with Windows (registry), fake winreg
calls = []
class K:
    def __enter__(self): return self
    def __exit__(self, *a): return False
wr = types.ModuleType("winreg")
wr.HKEY_CURRENT_USER, wr.KEY_SET_VALUE, wr.REG_SZ = "HKCU", 2, 1
wr.OpenKey = lambda root, path, res, acc: (calls.append(("open", root, path)), K())[1]
wr.SetValueEx = lambda k, n, r, t, v: calls.append(("set", n, v))
wr.DeleteValue = lambda k, n: calls.append(("del", n))
sys.modules["winreg"] = wr
ok, msg = gui.set_run_at_login(True)
check("enable writes HKCU Run entry", ok and ("set", gui.RUN_NAME, gui.autostart_command()) in calls
      and calls[0][2] == r"Software\Microsoft\Windows\CurrentVersion\Run")
ok, msg = gui.set_run_at_login(False)
check("disable removes it", ok and ("del", gui.RUN_NAME) in calls)
del sys.modules["winreg"]
cmd = gui.autostart_command()
check("autostart command is hidden-in-tray", cmd.endswith("--minimized") and cmd.count('"') >= 4, f"({cmd})")
ok, msg = gui.set_run_at_login(True)  # no winreg on Linux
check("non-Windows: reported, not crashing", (not ok) and "Windows" in msg)
cfgD, engD, winD = fresh(lambda n: False)
winD.auto_checks["run_at_login"].setChecked(True)
check("checkbox reverts + explains when registry write fails",
      not winD.auto_checks["run_at_login"].isChecked() and "Windows" in winD.hint.text())
winD.close()

# ---------------- process helpers
check("process_running('') is False", gui.process_running("") is False)
check("unknown process -> False", gui.process_running("definitely-not-running-xyz.exe") is False)
def boom(_): raise RuntimeError("x")
w = gui.ProcessWatcher(lambda: "a", boom, interval=0.05).start(); time.sleep(0.2)
check("probe error -> treated as not running", w.running is False); w.stop()
import subprocess
p = subprocess.Popen(["sleep", "5"]); time.sleep(0.2)
try:
    import psutil
    check("psutil probe finds a real running process", gui.process_running("sleep") is True)
except ImportError:
    print("[SKIP] psutil not installed here")
p.kill()

# ---------------- single instance lock
path = os.path.join(tempfile.mkdtemp(), "x.lock")
l1, l2 = QtCore.QLockFile(path), QtCore.QLockFile(path)
check("second instance cannot take the lock", l1.tryLock(100) and not l2.tryLock(100))
l1.unlock()

print("\nALL PASSED" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
