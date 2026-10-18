import os
import configparser, struct, sys, tempfile, types
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import opentrack_link as L

fails = 0
def check(name, ok, extra=""):
    global fails
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}"); fails += (not ok)

# ---- profile text is valid INI with the keys opentrack reads
cp = configparser.ConfigParser(); cp.optionxform = str; cp.read_string(L.profile_text(4300))
check("modules: udp -> freetrack, no filter", cp["modules"]["tracker-dll"] == "udp" and cp["modules"]["protocol-dll"] == "freetrack" and cp["modules"]["filter-dll"] == "")
check("udp-tracker port", cp["udp-tracker"]["port"] == "4300")
check("tray start", cp["opentrack-ui"]["use-system-tray"] == "true" and cp["opentrack-ui"]["start-in-tray"] == "true")

# ---- write_profile
docs = Path(tempfile.mkdtemp())
p, changed = L.write_profile(4242, docs)
check("profile written under Documents\\opentrack-2.3", p == docs / "opentrack-2.3" / "DCS-HeadTracker.ini" and p.exists() and changed)
p2, changed2 = L.write_profile(4242, docs)
check("rewriting identical profile is a no-op", not changed2)
p.write_text("[modules]\ntracker-dll=pt\n")
p3, changed3 = L.write_profile(4242, docs)
bak = p.with_suffix(".ini.bak")
check("different existing file is backed up once, then replaced", changed3 and bak.exists() and "tracker-dll=pt" in bak.read_text() and "udp" in p.read_text())
p.write_text("[modules]\ntracker-dll=x\n"); L.write_profile(4242, docs)
check("backup is not overwritten by later rewrites", "tracker-dll=pt" in bak.read_text())

# ---- executable list matches opentrack's parser rules
def opentrack_split(s):
    ret = {}
    for pair in [x for x in s.split("\x1e") if x]:
        t = pair.split("\x1f")
        if len(t) != 2 or not t[0] or not t[1]: continue
        ret[t[0]] = t[1]
    return ret
lst = L.executable_list()
check("executable-list parses to DCS.exe/dcs.exe -> profile", opentrack_split(lst) == {"DCS.exe": "DCS-HeadTracker.ini", "dcs.exe": "DCS-HeadTracker.ini"}, repr(lst))

# ---- registry writes (fake winreg)
class Key:
    def __enter__(self): return self
    def __exit__(self, *a): return False
rec = {}
wr = types.SimpleNamespace(HKEY_CURRENT_USER="HKCU", KEY_SET_VALUE=2, REG_SZ=1,
    CreateKeyEx=lambda root, sub, res, acc: (rec.__setitem__("sub", (root, sub)), Key())[1],
    SetValueEx=lambda k, n, r, t, v: rec.__setitem__(n, (t, v)))
ok, msg = L.registry_configure(winreg_mod=wr)
check("registry: key path", ok and rec["sub"] == ("HKCU", "Software\\opentrack-2.3"))
check("registry: profile + detector enabled + list", rec["settings-filename"] == (1, "DCS-HeadTracker.ini")
      and rec["executable-detector-enabled"] == (1, "true") and rec["executable-list"][1] == lst)
def boom(*a): raise PermissionError("denied")
wr2 = types.SimpleNamespace(HKEY_CURRENT_USER="HKCU", KEY_SET_VALUE=2, REG_SZ=1, CreateKeyEx=boom, SetValueEx=boom)
ok, msg = L.registry_configure(winreg_mod=wr2)
check("registry failure is reported, not raised", (not ok) and "denied" in msg)
ok, msg, ch = L.configure_opentrack(4242, docs, wr)
check("configure_opentrack = profile + registry", ok)

# ---- FT shared memory decode
buf = bytearray(128)
struct.pack_into(L.FT_DATA_FMT, buf, 0, 77, 640, 480, 10.5, -3.0, 2.0, 1.0, 2.0, 3.0)
struct.pack_into("<i", buf, 92, 1234)
d = L.parse_ft_heap(bytes(buf))
check("FTHeap decode", d["data_id"] == 77 and abs(d["yaw"] - 10.5) < 1e-6 and abs(d["pitch"] + 3.0) < 1e-6 and d["game_id"] == 1234)
check("short buffer / None -> None", L.parse_ft_heap(b"abc") is None and L.parse_ft_heap(None) is None)
check("FTData block size is 92 bytes (GameID offset)", struct.calcsize("<Iii" + "f"*6 + "f"*6 + "f"*8) == 92)

# ---- OutputMonitor
t = {"now": 0.0}; state = {"buf": None}
mon = L.OutputMonitor(reader=lambda: state["buf"], now=lambda: t["now"], stale_after=1.5)
mon.poll()
check("no shared memory -> not exists / not live", not mon.exists and not mon.live)
def mk(did, gid=0):
    b = bytearray(128); struct.pack_into(L.FT_DATA_FMT, b, 0, did, 0, 0, 1, 2, 3, 0, 0, 0); struct.pack_into("<i", b, 92, gid); return bytes(b)
state["buf"] = mk(1); mon.poll()
check("shared memory present, first sample -> live", mon.exists and mon.live)
t["now"] = 1.0; state["buf"] = mk(2); mon.poll()
check("DataID changing -> live", mon.live)
t["now"] = 3.0; mon.poll()
check("DataID frozen > 1.5 s -> not live (opentrack stopped / not receiving)", mon.exists and not mon.live)
state["buf"] = mk(3, gid=55); mon.poll()
check("game id set -> game attached", mon.live and mon.game_attached)

# ---- installer finder
dd = Path(tempfile.mkdtemp()); (dd / "opentrack-2026.1.0-win32-setup.exe").write_bytes(b"MZ")
check("find_installer finds the downloaded setup", L.find_installer([dd]) == dd / "opentrack-2026.1.0-win32-setup.exe")
import os
home_save = os.environ.get("HOME"); os.environ["HOME"] = tempfile.mkdtemp()
check("find_installer: nothing there -> None", L.find_installer([Path(tempfile.mkdtemp())]) is None)
os.environ["HOME"] = home_save
check("read_ft_heap is safe off Windows", L.read_ft_heap() is None)
check("documents_dir fallback works", L.documents_dir().name == "Documents")

print("\nALL PASSED" if not fails else f"\n{fails} FAILED"); sys.exit(1 if fails else 0)
