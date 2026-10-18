import ctypes, ctypes.wintypes as wt, os, socket, sys, tempfile, time
from types import SimpleNamespace
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["HOME"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np, mediapipe
import dcs_headtracker as core
import dcs_headtracker_gui as gui

fails = 0
def check(name, ok, extra=""):
    global fails
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}"); fails += (not ok)

# ---- parse_hotkey
P = gui.parse_hotkey
check("alt+c", P("alt+c") == (gui.MOD_ALT, ord("C")))
check("ctrl+shift+c (any case/spacing)", P(" Ctrl + SHIFT + c ") == (gui.MOD_CONTROL | gui.MOD_SHIFT, ord("C")))
check("f10 / F24", P("f10") == (0, 0x79) and P("F24") == (0, 0x87))
check("win+1", P("win+1") == (gui.MOD_WIN, ord("1")))
check("rejects junk", all(P(x) is None for x in ("", "alt+", "alt+ctrl", "c+d", "f25", "alt+enter", "+")))

# ---- WinHotkeys with a fake user32
class FakeUser32:
    def __init__(self): self.reg, self.unreg, self.taken = [], [], set()
    def RegisterHotKey(self, hwnd, hid, mods, vk):
        if (mods & ~gui.MOD_NOREPEAT, vk) in self.taken: return 0
        self.reg.append((hid, mods, vk)); return 1
    def UnregisterHotKey(self, hwnd, hid): self.unreg.append(hid); return 1
api = FakeUser32()
hk = gui.WinHotkeys(api)
hit = []
ok, err = hk.register("alt+c", lambda: hit.append("c"))
check("register ok, NOREPEAT set", ok and api.reg[0][1] == (gui.MOD_ALT | gui.MOD_NOREPEAT) and api.reg[0][2] == ord("C"))
check("dispatch calls the callback", hk.dispatch(api.reg[0][0]) and hit == ["c"])
check("dispatch of unknown id is ignored", hk.dispatch(12345) is False)
api.taken.add((gui.MOD_CONTROL, ord("X")))
ok, err = hk.register("ctrl+x", lambda: None)
check("key already taken -> clear message", (not ok) and "already used" in err, f"({err})")
ok, err = hk.register("banana", lambda: None)
check("bad key text -> clear message", (not ok) and "cannot understand" in err)
hk.clear()
check("clear unregisters", api.unreg == [api.reg[0][0]] and hk.dispatch(api.reg[0][0]) is False)
check("no api (Linux) -> reports not Windows", gui.WinHotkeys(None).register("alt+c", lambda: 0) == (False, "not Windows"))

# ---- Sender survives a dead receiver
s = core.Sender("127.0.0.1", 9)
class Boom:
    def sendto(self, *a): raise ConnectionResetError("10054 forcibly closed")
s.sock = Boom(); s.send((0,)*6); s.send((0,)*6)
check("send error is swallowed and counted", s.count == 0 and s.errors == 2 and "10054" in s.last_error)
s2 = core.Sender("127.0.0.1", 9); s2.send((0,)*6)
check("successful send is counted", s2.count == 1)

# ---- GUI
QtWidgets, MainWindow = gui.build_ui()
app = QtWidgets.QApplication([])
QtWidgets.QMessageBox.critical = staticmethod(lambda *a, **k: None)
def pump(sec):
    t = time.time()
    while time.time() - t < sec: app.processEvents(); time.sleep(0.01)

class FakeCam:
    def __init__(self, *_): pass
    def read(self, timeout=0.5): time.sleep(1/30); return np.full((480, 640, 3), 40, np.uint8)
    def close(self): pass
class FakeLM:
    def detect_for_video(self, img, ts):
        M = np.eye(4); M[:3, :3] = core.R_from_euler(0, 0, 0) * 5.0; M[:3, 3] = [0, 0, -40]
        return SimpleNamespace(facial_transformation_matrixes=[M], face_landmarks=[[SimpleNamespace(x=.5, y=.5)] * 5])
    def close(self): pass

state = {"ot": False}
probe = lambda n: state["ot"] if n.lower() == "opentrack.exe" else False

# Windows-API path through the real Qt event-filter code
api2 = FakeUser32()
gui.WinHotkeys = lambda *a, **k: _real(api2)
_real = type(hk)
cfg = gui.make_cfg(); cfg.port = 45998
eng = gui.Engine(cfg, camera_factory=lambda c: FakeCam(), landmarker_factory=lambda c: FakeLM())
win = MainWindow(cfg, eng, hotkeys=True, process_probe=probe, watch_interval=0.05, use_tray=False)
win.show(); pump(0.4)
check("hotkey registered through Windows API (alt+c and f11)",
      win.hotkey_method == "windows" and {r[2] for r in api2.reg} == {ord("C"), 0x7A}, f"({win.hotkey_method})")
check("hint says active", "Hotkeys active" in win.hint.text())

msg = wt.MSG(); msg.message = gui.WM_HOTKEY
cid = [r[0] for r in api2.reg if r[2] == ord("C")][0]
msg.wParam = cid
beeps = []; gui.beep = lambda: beeps.append(1)
res = win._hk_filter.nativeEventFilter(b"windows_generic_MSG", ctypes.addressof(msg))
check("WM_HOTKEY message -> recenter + beep + counted", res == (True, 0) and eng._center and beeps and win._press_count == 1)
msg.message = 0x0100  # some other message
check("other Windows messages pass through", win._hk_filter.nativeEventFilter(b"windows_generic_MSG", ctypes.addressof(msg)) == (False, 0))
check("non-Windows event type ignored", win._hk_filter.nativeEventFilter(b"xcb_generic_event_t", 0) == (False, 0))

win._update_checks(eng.snapshot()); pump(0.1)
check("check panel: hotkey 'received just now'", "received just now" in win.chk_hk.text() and "not running" in win.chk_hk.text(), f"('{win.chk_hk.text()}')")
check("check panel: opentrack missing is flagged red with instructions",
      "NOT running" in win.chk_ot.text() and "freetrack 2.0 Enhanced" in win.chk_ot.text() and "e06b6b" in win.chk_ot.styleSheet())
state["ot"] = True; pump(0.3); win._update_checks(eng.snapshot())
check("check panel: opentrack running -> green", "is running" in win.chk_ot.text() and "2fae7d" in win.chk_ot.styleSheet())

win._toggle(); pump(2.5)
snap = eng.snapshot()
import re as _re
_n = int(_re.search(r"(\d+)\s*$", win.chk_udp.text()).group(1))
check("packet counter flows into the UI", snap.packets > 20 and 20 < _n <= snap.packets, f"({win.chk_udp.text()})")
check("camera check says face found", "face found" in win.chk_cam.text(), f"({win.chk_cam.text()})")
win.key_test_btn.click(); pump(0.2)
check("Test recenter button == hotkey", win._press_count == 2)
win.grab().save("/tmp/claude-0/-home-claude/9f512055-4b06-56c2-8bc4-c7d154314844/scratchpad/gui_v4.png")
win._toggle(); pump(0.4)

# hotkey collision at startup -> falls back to keyboard hook, else shows clear error
api3 = FakeUser32(); api3.taken.add((gui.MOD_ALT, ord("C")))
gui.WinHotkeys = lambda *a, **k: _real(api3)
win2 = MainWindow(gui.make_cfg(), eng, hotkeys=True, process_probe=probe, watch_interval=1, use_tray=False)
check("Alt+C taken by another program -> clear error shown", win2.hotkey_method == "off" and "already used" in win2.hint.text(), f"('{win2.hint.text()}')")
win2._update_checks(eng.snapshot())
check("check panel shows hotkey not active", "not active" in win2.chk_hk.text())
win2.close()

# change hotkey re-registers
win.hk_edit.setText("ctrl+shift+c"); win.hk_edit.editingFinished.emit()
check("changing the hotkey unregisters old + registers new",
      api2.unreg and any(r[1] == (gui.MOD_CONTROL | gui.MOD_SHIFT | gui.MOD_NOREPEAT) for r in api2.reg))
win.close()

print("\nALL PASSED" if not fails else f"\n{fails} FAILED"); sys.exit(1 if fails else 0)
