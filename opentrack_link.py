"""
Glue between DCS Head Tracker and opentrack: configure it, launch it, and check that data
really comes out of it.  Everything here is plain functions with injectable OS pieces so it
can be unit-tested away from Windows.

Facts used (read from the opentrack source, tag opentrack-2026.1.0 / master 2026-10):
  * profiles are .ini files in  <Documents>\\opentrack-2.3\\
  * the active profile name and the "start tracking when <exe> runs" list live in the
    registry under  HKCU\\Software\\opentrack-2.3   (values: settings-filename,
    executable-detector-enabled, executable-list)
  * executable-list = records separated by 0x1E, each record  "<exe>" 0x1F "<profile.ini>"
  * UDP tracker module is called "udp", its section is [udp-tracker] (key: port)
  * the freetrack output publishes a shared memory block named FT_SharedMem (struct FTHeap)
"""
from __future__ import annotations

import os
import shutil
import struct
import subprocess
from pathlib import Path

OT_ORG = "opentrack-2.3"
PROFILE_NAME = "DCS-HeadTracker.ini"
FT_HEAP = "FT_SharedMem"
RS, US = "\x1e", "\x1f"
OPENTRACK_URL = "https://github.com/opentrack/opentrack/releases/latest"
FT_DATA_FMT = "<Iii" + "f" * 6          # DataID, CamWidth, CamHeight, Yaw Pitch Roll X Y Z
FT_GAMEID_OFFSET = 92                    # int32 GameID follows the 92-byte FTData block


# ----------------------------------------------------------------------------
# Locations
# ----------------------------------------------------------------------------
def documents_dir() -> Path:
    """The real 'Documents' folder (follows OneDrive redirection), like Qt's DocumentsLocation."""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                            ("Data3", wintypes.WORD), ("Data4", wintypes.BYTE * 8)]

            folder_id = GUID(0xFDD39AD0, 0x238F, 0x46AF,
                             (wintypes.BYTE * 8)(0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7))
            out = ctypes.c_wchar_p()
            shell32 = ctypes.windll.shell32
            shell32.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD,
                                                     wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p)]
            if shell32.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None, ctypes.byref(out)) == 0:
                p = out.value
                ctypes.windll.ole32.CoTaskMemFree(out)
                if p:
                    return Path(p)
        except Exception:  # noqa: BLE001
            pass
    return Path.home() / "Documents"


def find_opentrack() -> "Path | None":
    """opentrack.exe in the usual install folders."""
    bases = [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
             os.environ.get("ProgramW6432"),
             os.environ.get("LOCALAPPDATA") and os.path.join(os.environ["LOCALAPPDATA"], "Programs")]
    seen = set()
    for b in bases:
        if not b or b in seen:
            continue
        seen.add(b)
        try:
            for p in Path(b).glob("opentrack*/opentrack.exe"):
                return p
        except OSError:
            pass
    return None


def find_installer(extra_dirs=()) -> "Path | None":
    """An opentrack-*-setup.exe the user already downloaded (Desktop / Downloads)."""
    home = Path.home()
    dirs = [Path(d) for d in extra_dirs] + [
        documents_dir().parent / "Desktop", home / "Desktop", home / "Downloads",
        home / "OneDrive" / "Desktop", home / "OneDrive" / "桌面"]
    for d in dirs:
        try:
            for p in sorted(d.glob("opentrack*setup*.exe"), reverse=True):
                return p
        except OSError:
            pass
    return None


# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
def profile_text(port: int = 4242, tray: bool = True) -> str:
    """The opentrack profile: UDP input -> freetrack output, no extra filter (we filter)."""
    lines = [
        "[modules]",
        "tracker-dll=udp",
        "protocol-dll=freetrack",
        "filter-dll=",
        "",
        "[udp-tracker]",
        f"port={int(port)}",
        "",
        "[opentrack-ui]",
        f"use-system-tray={'true' if tray else 'false'}",
        f"start-in-tray={'true' if tray else 'false'}",
        "center-at-startup=true",
        "",
    ]
    return "\n".join(lines)


def write_profile(port: int = 4242, docs: "Path | None" = None) -> "tuple[Path, bool]":
    """Write our profile. Returns (path, changed). A previous different file is kept as .bak once."""
    d = (docs or documents_dir()) / OT_ORG
    d.mkdir(parents=True, exist_ok=True)
    p = d / PROFILE_NAME
    text = profile_text(port)
    if p.exists():
        old = p.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
        if old == text:
            return p, False
        bak = p.with_suffix(".ini.bak")
        if not bak.exists():
            shutil.copy2(p, bak)
    p.write_text(text, encoding="utf-8", newline="\n")
    return p, True


def executable_list(exes=("DCS.exe", "dcs.exe"), profile: str = PROFILE_NAME) -> str:
    return RS.join(f"{e}{US}{profile}" for e in dict.fromkeys(exes))


def registry_configure(exes=("DCS.exe", "dcs.exe"), profile: str = PROFILE_NAME,
                       winreg_mod=None) -> "tuple[bool, str]":
    """Select our profile and make opentrack start tracking whenever DCS.exe runs."""
    try:
        wr = winreg_mod
        if wr is None:
            import winreg as wr  # type: ignore
    except ImportError:
        return False, "registry is only available on Windows"
    try:
        key = wr.CreateKeyEx(wr.HKEY_CURRENT_USER, "Software\\" + OT_ORG, 0, wr.KEY_SET_VALUE)
        with key:
            wr.SetValueEx(key, "settings-filename", 0, wr.REG_SZ, profile)
            wr.SetValueEx(key, "executable-detector-enabled", 0, wr.REG_SZ, "true")
            wr.SetValueEx(key, "executable-list", 0, wr.REG_SZ, executable_list(exes, profile))
        return True, ""
    except OSError as e:
        return False, f"could not write opentrack settings: {e}"


def configure_opentrack(port: int = 4242, docs: "Path | None" = None,
                        winreg_mod=None) -> "tuple[bool, str, bool]":
    """Profile file + registry. Returns (ok, message, changed)."""
    try:
        path, changed = write_profile(port, docs)
    except OSError as e:
        return False, f"could not write the opentrack profile: {e}", False
    ok, msg = registry_configure(winreg_mod=winreg_mod)
    return ok, (msg or str(path)), changed


# ----------------------------------------------------------------------------
# Live check of opentrack's output (FreeTrack shared memory)
# ----------------------------------------------------------------------------
def parse_ft_heap(buf: bytes) -> "dict | None":
    """Decode the FTHeap block. None if too short."""
    need = struct.calcsize(FT_DATA_FMT)
    if buf is None or len(buf) < need:
        return None
    data_id, w, h, yaw, pitch, roll, x, y, z = struct.unpack_from(FT_DATA_FMT, buf, 0)
    game_id = struct.unpack_from("<i", buf, FT_GAMEID_OFFSET)[0] if len(buf) >= FT_GAMEID_OFFSET + 4 else 0
    return {"data_id": data_id, "yaw": yaw, "pitch": pitch, "roll": roll,
            "x": x, "y": y, "z": z, "game_id": game_id}


def read_ft_heap() -> "bytes | None":
    """Read FT_SharedMem (read-only). None when opentrack's freetrack output is not running."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        k32.OpenFileMappingW.restype = wintypes.HANDLE
        k32.OpenFileMappingW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        k32.MapViewOfFile.restype = ctypes.c_void_p
        k32.MapViewOfFile.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                      wintypes.DWORD, ctypes.c_size_t]
        k32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        FILE_MAP_READ = 0x0004
        h = k32.OpenFileMappingW(FILE_MAP_READ, False, FT_HEAP)
        if not h:
            return None
        try:
            size = 128
            ptr = k32.MapViewOfFile(h, FILE_MAP_READ, 0, 0, size)
            if not ptr:
                return None
            try:
                return ctypes.string_at(ptr, size)
            finally:
                k32.UnmapViewOfFile(ptr)
        finally:
            k32.CloseHandle(h)
    except Exception:  # noqa: BLE001
        return None


class OutputMonitor:
    """Tells whether opentrack's freetrack output is alive (DataID keeps changing)."""

    def __init__(self, reader=read_ft_heap, now=None, stale_after: float = 1.5):
        import time
        self.reader = reader
        self.now = now or time.monotonic
        self.stale_after = stale_after
        self.last_id = None
        self.last_change = None
        self.info: "dict | None" = None

    def poll(self) -> "dict | None":
        info = parse_ft_heap(self.reader())
        self.info = info
        t = self.now()
        if info is not None and info["data_id"] != self.last_id:
            self.last_id, self.last_change = info["data_id"], t
        return info

    @property
    def exists(self) -> bool:
        return self.info is not None

    @property
    def live(self) -> bool:
        return (self.info is not None and self.last_change is not None
                and self.now() - self.last_change <= self.stale_after)

    @property
    def game_attached(self) -> bool:
        return bool(self.info and self.info.get("game_id"))


def launch(path: Path) -> "tuple[bool, str]":
    try:
        subprocess.Popen([str(path)], cwd=str(path.parent))
        return True, ""
    except OSError as e:
        return False, str(e)


def kill_opentrack(run=subprocess.run) -> bool:
    """Force-close opentrack.exe (no save-on-exit, so it cannot overwrite our fresh profile)."""
    try:
        r = run(["taskkill", "/F", "/IM", "opentrack.exe"], capture_output=True, timeout=8,
                creationflags=0x08000000 if os.name == "nt" else 0)
        return getattr(r, "returncode", 1) == 0
    except (OSError, subprocess.SubprocessError):
        return False
