import os, sys, tempfile, time, json
from pathlib import Path
from types import SimpleNamespace

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["HOME"] = tempfile.mkdtemp()
SCR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import mediapipe
import dcs_headtracker as core
import dcs_headtracker_gui as gui

QtWidgets, MainWindow = gui.build_ui()
from PySide6 import QtCore
app = QtWidgets.QApplication([])
QtWidgets.QMessageBox.critical = staticmethod(lambda *a, **k: None)

fails = 0
def check(name, ok, extra=""):
    global fails
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}"); fails += (not ok)

def pump(sec):
    t = time.time()
    while time.time() - t < sec:
        app.processEvents(); time.sleep(0.01)

# ======================= compute_home_state (pure)
H = gui.compute_home_state
base = dict(state="tracking", found=True, paused=False, error="", ot_installed=True, ot_running=True,
            out_live=True, game_attached=False, dcs_running=False, auto_dcs=True)
def hs(**kw):
    d = dict(base); d.update(kw); return H(**d)
r = hs(state="error", error="Cannot open camera 0"); check("state: camera error -> bad + retry", r["level"] == "bad" and r["action"] == "retry" and "Cannot open" in r["sub"])
r = hs(ot_running=False, ot_installed=False); check("state: not installed -> bad + install", r["level"] == "bad" and r["action"] == "install")
r = hs(ot_running=False, ot_installed=True); check("state: installed, not running -> warn + open_ot", r["level"] == "warn" and r["action"] == "open_ot" and "Starting opentrack" in r["title"])
r = hs(ot_running=False, ot_installed=True, manage_ot=False); check("state: unmanaged opentrack not running -> asks user", "not running" in r["title"] and r["action"] == "open_ot")
r = hs(state="starting"); check("state: starting -> warn", r["level"] == "warn" and "camera" in r["title"].lower())
r = hs(state="loading"); check("state: loading -> warn", r["level"] == "warn")
r = hs(state="demo"); check("state: demo -> info", r["level"] == "info")
r = hs(state="idle", auto_dcs=True); check("state: idle + auto -> waiting for DCS", r["level"] == "idle" and "waiting for DCS" in r["title"] and r["action"] == "start")
r = hs(state="idle", auto_dcs=False); check("state: idle manual -> Ready", r["title"] == "Ready" and r["action"] == "start")
r = hs(state="stopped", dcs_running=True); check("state: stopped while DCS up + auto -> starting", r["level"] == "warn")
r = hs(state="stopped", dcs_running=True, auto_dcs=False); check("state: DCS up but auto off -> plain Ready", r["title"] == "Ready")
r = hs(paused=True); check("state: paused -> warn", r["level"] == "warn" and "Paused" in r["title"])
r = hs(found=False); check("state: no face -> warn", r["level"] == "warn" and "face" in r["title"].lower())
r = hs(); check("state: tracking + live -> ok", r["level"] == "ok" and r["title"] == "Tracking" and "DCS is connected" not in r["sub"])
r = hs(game_attached=True); check("state: game attached noted", "DCS is connected" in r["sub"])
r = hs(out_live=False, dcs_running=True); check("state: DCS up, no data -> warn + open_ot", r["level"] == "warn" and r["action"] == "open_ot")
r = hs(out_live=False, dcs_running=True, ot_needs_restart=True); check("state: DCS up, no data, config changed -> restart_ot", r["action"] == "restart_ot")
r = hs(out_live=False, dcs_running=False); check("state: tracking, waiting for DCS -> ok", r["level"] == "ok" and "DCS" in r["sub"])

# ======================= fakes
class FakeMon:
    def __init__(self): self.live = False; self.game_attached = False; self.polls = 0
    def poll(self): self.polls += 1; return None

class FakeLink:
    OPENTRACK_URL = "https://example.invalid/opentrack"
    def __init__(self, installed=True, installer=None, changed=False):
        self.installed, self.installer, self.changed = installed, installer, changed
        self.calls = []; self.mon = FakeMon()
    def find_opentrack(self): return Path("C:/ot/opentrack.exe") if self.installed else None
    def find_installer(self): return self.installer
    def configure_opentrack(self, port): self.calls.append(("cfg", port)); return True, "ok", self.changed
    def launch(self, p): self.calls.append(("launch",)); return True, ""
    def kill_opentrack(self): self.calls.append(("kill",)); return True
    def OutputMonitor(self): return self.mon
    def n(self, what): return sum(1 for c in self.calls if c[0] == what)

class FakeCam:
    def __init__(self, *_): pass
    def read(self, timeout=0.5):
        time.sleep(1 / 30)
        f = np.full((480, 640, 3), 40, np.uint8)
        import cv2
        cv2.circle(f, (320, 240), 90, (200, 200, 200), -1)
        return f
    def close(self): pass
class FakeLM:
    def detect_for_video(self, img, ts):
        M = np.eye(4); M[:3, :3] = core.R_from_euler(0, 0, 0) * 5.0; M[:3, 3] = [0, 0, -40]
        return SimpleNamespace(facial_transformation_matrixes=[M], face_landmarks=[[SimpleNamespace(x=0.5, y=0.5)] * 5])
    def close(self): pass

up = {"ot": False, "dcs": False}
def probe(name):
    n = name.lower()
    return (up["ot"] and n == "opentrack.exe") or (up["dcs"] and n == "dcs.exe")

def fresh(link, **kw):
    cfg = gui.make_cfg(); cfg.port = 45998
    eng = gui.Engine(cfg, camera_factory=lambda c: FakeCam(), landmarker_factory=lambda c: FakeLM())
    win = MainWindow(cfg, eng, hotkeys=False, process_probe=probe, watch_interval=0.05, use_tray=False, link=link, **kw)
    opened = []
    win._start_file = lambda p: (opened.append(("file", str(p))), True)[1]
    win._open_url = lambda u: opened.append(("url", u))
    win.show(); win.opened = opened
    return cfg, eng, win

# ======================= home: opentrack not installed
up.update(ot=False, dcs=False)
link = FakeLink(installed=False)
cfg, eng, win = fresh(link)
pump(0.5)
check("home: starts on the Home page", win.stack.currentIndex() == 0 and win.width() == 460)
check("home: not installed -> red card + Install button", win.card.property("level") == "bad" and "not installed" in win.card_title.text() and win.card_btn.isVisibleTo(win) and win.card_btn.text() == "Install opentrack", f"({win.card_title.text()})")
check("home: opentrack pill red", win.pills["opentrack"].property("level") == "bad")
win.card_btn.click(); pump(0.1)
check("install click, no installer on disk -> opens download page", win.opened == [("url", gui.OPENTRACK_URL)], f"({win.opened})")
link.installer = Path("C:/Users/x/Desktop/opentrack-setup.exe"); win.opened.clear()
win.card_btn.click(); pump(0.1)
check("install click with downloaded installer -> runs it", win.opened and win.opened[0][0] == "file" and "opentrack-setup.exe" in win.opened[0][1], f"({win.opened})")
check("install gives plain next-step note", "everything else is automatic" in win.home_note.text())
win.close(); pump(0.1)

# ======================= home: installed but not running; open button
up.update(ot=False, dcs=False)
link = FakeLink(installed=True)
cfg, eng, win = fresh(link)
pump(0.5)
check("home: installed, not running -> amber, 'Starting opentrack'", win.card.property("level") == "warn" and "Starting opentrack" in win.card_title.text(), f"({win.card_title.text()})")
win.card_btn.click(); pump(0.1)
check("Open opentrack = configure + launch", link.n("cfg") == 1 and link.n("launch") == 1 and link.calls[0] == ("cfg", 45998), f"({link.calls})")
win.close(); pump(0.1)

# ======================= home: full flow with one click
up.update(ot=True, dcs=False)
link = FakeLink(installed=True)
cfg, eng, win = fresh(link)
pump(0.4)
check("home: ready & waiting for DCS, no extra button", win.card.property("level") == "idle" and "waiting for DCS" in win.card_title.text() and not win.card_btn.isVisibleTo(win), f"({win.card_title.text()})")
check("home: opentrack pill green, Output idle", win.pills["opentrack"].property("level") == "ok" and win.pills["Output"].property("level") == "idle")
win.home_start.click(); pump(2.0)
check("one click starts the engine", eng.running and win.home_start.text() == "Stop" and win.home_start.property("running") == "true")
check("settings-page Start button synced", win.start_btn.text() == "Stop")
check("home: tracking but no data yet -> OK, waits for DCS", win.card.property("level") == "ok" and win.card_title.text() == "Tracking", f"({win.card_title.text()})")
check("home: camera pill green", win.pills["Camera"].property("level") == "ok")
check("home preview shows the camera", win.home_preview.pixmap() is not None and not win.home_preview.pixmap().isNull())
check("home bars follow the pose", win.home_bars["Yaw"].v == win.bars["Yaw"].v)
link.mon.live = True; link.mon.game_attached = True; pump(0.8)
check("home: output live + game attached", win.pills["Output"].property("level") == "ok" and "DCS is connected" in win.card_sub.text(), f"({win.card_sub.text()})")
check("OutputMonitor is polled", link.mon.polls >= 3, f"({link.mon.polls})")
n0 = win._press_count; win.home_center.click(); pump(0.1)
check("home Recenter button works", win._press_count == n0 + 1)
win.home_start.click(); pump(0.4)
check("one click stops", not eng.running and win.home_start.text() == "Start tracking")

# ---- DCS up, no data -> warn, restart flow
link.mon.live = False; up["dcs"] = True; win._ot_needs_restart = True
cfg.auto_dcs = False
win.home_start.click(); pump(1.5)
up["dcs"] = False; up["dcs"] = True; pump(0.6)
check("DCS up & no data -> amber + Restart button", win.card.property("level") in ("warn",) and win.card_btn.isVisibleTo(win) and win.card_btn.text() in ("Restart opentrack", "Open opentrack"), f"({win.card_title.text()} / {win.card_btn.text()})")
win._ot_needs_restart = True; pump(0.1)
if win.card_btn.text() == "Restart opentrack":
    link.calls.clear(); win.card_btn.click(); pump(1.5)
    check("Restart = kill, rewrite settings, relaunch", [c[0] for c in link.calls] == ["kill", "cfg", "launch"], f"({link.calls})")
else:
    check("Restart button shown", False, f"({win.card_btn.text()})")
win.close(); pump(0.1)

# ======================= _ensure_opentrack rules
up.update(ot=False, dcs=False)
link = FakeLink(installed=True)
cfg, eng, win = fresh(link)
win._ensure_opentrack()
check("ensure: not running -> configure then launch once", [c[0] for c in link.calls] == ["cfg", "launch"], f"({link.calls})")
win._ensure_opentrack()
check("ensure: does not launch twice within 15 s", link.n("launch") == 1)
up["ot"] = True; link.calls.clear(); win._ot_last_launch = 0
link.changed = False; win._ensure_opentrack()
check("ensure: running + unchanged config -> nothing to do", link.n("launch") == 0 and not win._ot_needs_restart)
link.changed = True; win._ensure_opentrack()
check("ensure: running + changed config -> flags restart, never kills", link.n("launch") == 0 and link.n("kill") == 0 and win._ot_needs_restart)
cfg.manage_opentrack = False; link.calls.clear(); up["ot"] = False; win._ot_last_launch = 0
win._ensure_opentrack()
check("ensure: auto-setup off -> hands off", link.calls == [])
cfg.manage_opentrack = True; link.installed = False; win._ensure_opentrack()
check("ensure: not installed -> nothing", link.calls == [] and not win._ot_installed)
win.close(); pump(0.1)

# ======================= startup tasks + DCS edge + install detected later
up.update(ot=False, dcs=False)
link = FakeLink(installed=True)
cfg, eng, win = fresh(link, startup_tasks=True)
pump(0.9)
check("startup: opentrack configured + launched automatically", link.n("cfg") == 1 and link.n("launch") == 1, f"({link.calls})")
up["ot"] = True; pump(0.3); link.calls.clear()
up["ot"] = False; win._ot_last_launch = 0     # user closed opentrack, then DCS starts
up["dcs"] = True; pump(0.6)
check("DCS start -> opentrack set up again if it is not running", link.n("launch") == 1, f"({link.calls})")
eng.stop(); win.close(); pump(0.1)

up.update(ot=False, dcs=False)
link = FakeLink(installed=False)
cfg, eng, win = fresh(link, startup_tasks=True)
pump(0.8)
check("not installed at startup: nothing launched", link.n("launch") == 0)
link.installed = True; win._ot_last_check = 0; pump(0.5)
check("installed later (while app open) -> set up automatically", link.n("launch") == 1 and link.n("cfg") == 1, f"({link.calls})")
win.close(); pump(0.1)

# ======================= first run
saved = {}
real_set, real_save = gui.set_run_at_login, gui.save_cfg
gui.set_run_at_login = lambda on: (saved.setdefault("login", []).append(on), (True, ""))[1]
gui.save_cfg = lambda c: saved.__setitem__("cfg", c.first_run_done)
up.update(ot=True, dcs=False)
cfg, eng, win = fresh(FakeLink(), first_run=True)
check("first run: start-with-Windows enabled", saved.get("login") == [True] and cfg.run_at_login and win.auto_checks["run_at_login"].isChecked())
check("first run: remembered, saved", cfg.first_run_done and saved.get("cfg") is True)
win.close(); pump(0.1)
gui.set_run_at_login = lambda on: (False, "no registry")
cfg, eng, win = fresh(FakeLink(), first_run=True)
check("first run: registry failure -> still marks done, run_at_login stays off", cfg.first_run_done and not cfg.run_at_login and not win.auto_checks["run_at_login"].isChecked())
win.close(); pump(0.1)
gui.set_run_at_login, gui.save_cfg = real_set, real_save
cfg, eng, win = fresh(FakeLink())
check("no first-run side effects by default", not cfg.first_run_done and not cfg.run_at_login)

# ======================= settings / home navigation
win.gear_btn.click(); pump(0.2)
check("gear opens Settings, window grows", win.stack.currentIndex() == 1 and win.width() >= 900, f"({win.width()})")
win.grab().save(os.path.join(SCR, "gui_settings_v5.png"))
win.back_btn.click(); pump(0.2)
check("Back returns to Home, window shrinks", win.stack.currentIndex() == 0 and win.width() == 460, f"({win.width()})")

# ======================= single instance IPC
name = "DCSHeadTrackerTest.%d" % os.getpid()
check("start_ipc listens", win.start_ipc(name))
check("no server -> notify says False", gui.notify_running_instance("nobody.listening.%d" % os.getpid()) is False)
win.hide(); pump(0.1)
res = {}
import threading
def second(): res["ok"] = gui.notify_running_instance(name)
th = threading.Thread(target=second); th.start()
t0 = time.time()
while th.is_alive() and time.time() - t0 < 3: pump(0.02)
pump(0.3)
check("second launch -> first instance shows its window", res.get("ok") is True and win.isVisible())
win.close(); pump(0.1)

# ======================= screenshot of the tracking home
up.update(ot=True, dcs=True)
link = FakeLink(); link.mon.live = True; link.mon.game_attached = True
cfg, eng, win = fresh(link)
win.home_start.click(); pump(2.0)
win.grab().save(os.path.join(SCR, "gui_home_v5.png"))
eng.stop(); win.close(); pump(0.1)

print("\nALL PASSED" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
