import math, os, socket, struct, sys, tempfile, threading, time
from types import SimpleNamespace

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["HOME"] = tempfile.mkdtemp()           # keep settings file out of the way
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import mediapipe  # pre-import so timing in this test is deterministic
import dcs_headtracker as core
import dcs_headtracker_gui as gui

QtWidgets, MainWindow = gui.build_ui()
app = QtWidgets.QApplication([])
QtWidgets.QMessageBox.critical = staticmethod(lambda *a, **k: print("   [dialog]", a[2]))

# ---- UDP listener
srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); srv.bind(("127.0.0.1", 0)); srv.settimeout(0.2)
PORT = srv.getsockname()[1]
packets = []
stop = False
def listen():
    while not stop:
        try:
            d, _ = srv.recvfrom(1024); packets.append((time.time(), struct.unpack("<6d", d)))
        except socket.timeout:
            pass
threading.Thread(target=listen, daemon=True).start()

# ---- fakes
class FakeCam:
    def __init__(self, *_): self.n = 0
    def read(self, timeout=0.5):
        time.sleep(1 / 30)
        f = np.full((480, 640, 3), 40, np.uint8)
        import cv2
        cv2.circle(f, (320, 240), 90, (200, 200, 200), -1)
        cv2.putText(f, "FAKE CAM", (230, 60), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 255), 2)
        return f
    def close(self): pass

class FakeLM:
    """frontal for 0.6 s, then head turned 30 deg to the user's right."""
    def __init__(self): self.t0 = None
    def detect_for_video(self, img, ts):
        if self.t0 is None: self.t0 = ts
        yaw_user = 0.0 if ts - self.t0 < 600 else 30.0
        M = np.eye(4); M[:3, :3] = core.R_from_euler(math.radians(-yaw_user), 0, 0) * 5.0
        M[:3, 3] = [0, 0, -40]
        return SimpleNamespace(facial_transformation_matrixes=[M],
                               face_landmarks=[[SimpleNamespace(x=0.5, y=0.5)] * 5])
    def close(self): pass

def pump(sec):
    t = time.time()
    while time.time() - t < sec:
        app.processEvents(); time.sleep(0.01)

def last_yaw(since):
    xs = [p[3] for t, p in packets if t > since]
    return float(np.mean(xs[-10:])) if xs else None

fails = 0
def check(name, ok, extra=""):
    global fails
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}"); fails += (not ok)

cfg = gui.make_cfg(); cfg.port = PORT
eng = gui.Engine(cfg, camera_factory=lambda c: FakeCam(), landmarker_factory=lambda c: FakeLM())
win = MainWindow(cfg, eng, hotkeys=False)
win.show(); pump(0.3)

# 1. start tracking
win._toggle(); pump(3.2)
s = eng.snapshot()
check("state is tracking", s.state == "tracking", f"(state={s.state})")
check("packets flowing", len(packets) > 40, f"({len(packets)} packets)")
y = last_yaw(time.time() - 0.3)
exp = core.shape(30.0, cfg.yaw_gain, cfg.ramp)
check("30deg right turn -> large positive yaw with gain", y is not None and abs(y - exp) < 4, f"(yaw={y:.1f}, expected ~{exp:.1f})")
check("status label says Tracking", "Tracking" in win.status.text(), f"('{win.status.text()}')")
check("preview pixmap set", win.preview.pixmap() is not None and not win.preview.pixmap().isNull())
check("yaw bar updated", win.bars["Yaw"].v > 20, f"(bar={win.bars['Yaw'].v:.1f})")
win.grab().save("/tmp/claude-0/-home-claude/9f512055-4b06-56c2-8bc4-c7d154314844/scratchpad/gui_tracking.png")

# 2. invert checkbox flips live
win.checks["invert_yaw"].setChecked(True); pump(1.5)
y2 = last_yaw(time.time() - 0.3)
check("invert yaw flips sign live", y2 is not None and y2 < -(exp - 4), f"(yaw={y2:.1f})")
win.checks["invert_yaw"].setChecked(False)

# 3. recenter makes the held 30deg pose the new zero
win.center_btn.click(); pump(1.5)
y3 = last_yaw(time.time() - 0.3)
check("recenter -> yaw back to ~0", y3 is not None and abs(y3) < 3, f"(yaw={y3:.2f})")

# 4. pause sends zeros
win.pause_btn.setChecked(True); pump(0.6)
check("pause sends zeros", all(abs(v) < 1e-9 for v in packets[-1][1]))
win.pause_btn.setChecked(False)

# 5. slider changes cfg live
win.sliders["yaw_gain"].set(3.0); win.sliders["yaw_gain"].changed.emit(3.0)
win.sliders["smooth"].set(80); win.sliders["smooth"].changed.emit(80)
check("slider -> cfg", cfg.yaw_gain == 3.0 and abs(cfg.min_cutoff - gui.smooth_to_cutoff(80)) < 1e-9)

# 6. stop
win._toggle(); pump(0.5)
check("engine stopped & zeros sent", not eng.running and all(abs(v) < 1e-9 for v in packets[-1][1]))
check("button back to Start", win.start_btn.text() == "Start tracking")

# 7. error path (camera cannot open) is shown in the UI
def bad_cam(c): raise RuntimeError("Cannot open camera 0. Try --camera 1")
eng2 = gui.Engine(cfg, camera_factory=bad_cam, landmarker_factory=lambda c: FakeLM())
win2 = MainWindow(cfg, eng2, hotkeys=False); win2.show()
win2._toggle(); pump(0.8)
check("error surfaced in status", "Cannot open camera" in win2.status.text(), f"('{win2.status.text()}')")
check("button reset after error", win2.start_btn.text() == "Start tracking")

# 8. model-download failure (SystemExit) is shown, camera was opened first
opened = []
class Cam2(FakeCam):
    def __init__(self, *_): opened.append(1)
def bad_model(c): raise SystemExit("Model download failed: no network")
eng3 = gui.Engine(cfg, camera_factory=lambda c: Cam2(), landmarker_factory=bad_model)
win3 = MainWindow(cfg, eng3, hotkeys=False); win3.show()
win3._toggle(); pump(0.8)
check("camera opened BEFORE model load", opened == [1])
check("download failure shown", "Model download failed" in win3.status.text())

# 9. demo sweep
packets.clear()
win2.demo_btn.click(); pump(1.5)
ys = [p[3] for _, p in packets]
check("test sweep sends data without camera", len(ys) > 50 and max(ys) > 20, f"({len(ys)} pkts, max yaw {max(ys):.1f})")
win2.grab().save("/tmp/claude-0/-home-claude/9f512055-4b06-56c2-8bc4-c7d154314844/scratchpad/gui_demo.png")
win2._toggle(); pump(0.3)

# 10. settings persisted
win.close(); pump(0.1)
check("settings saved to disk", gui.SETTINGS_PATH.exists() and "yaw_gain" in gui.SETTINGS_PATH.read_text())

stop = True
print("\nALL PASSED" if not fails else f"\n{fails} FAILED")
sys.exit(1 if fails else 0)
