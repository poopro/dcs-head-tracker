#!/usr/bin/env python3
"""
dcs_headtracker.py  --  webcam head tracker for DCS World (via opentrack)

Pipeline:  webcam -> MediaPipe Face Landmarker (head pose matrix)
           -> recenter -> deadzone -> One-Euro filter -> gain curve
           -> UDP (6 little-endian doubles) -> opentrack "UDP over network"
           -> opentrack output "freetrack 2.0 Enhanced" -> DCS World

Run:   python dcs_headtracker.py              (live tracking, preview window)
       python dcs_headtracker.py --demo       (no camera; sends a test sweep)
       python dcs_headtracker.py --selftest   (offline math / packet checks)
       python dcs_headtracker.py --list-cameras
"""
from __future__ import annotations

import argparse
import math
import os
import socket
import struct
import sys
import threading
import time
import urllib.request
from pathlib import Path

import numpy as np

MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/face_landmarker/"
    "face_landmarker/float16/latest/face_landmarker.task"
)
NOSE_TIP = 1  # landmark index used only to anchor the preview axes


# ----------------------------------------------------------------------------
# Math
# ----------------------------------------------------------------------------
def rot_x(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]], dtype=float)


def rot_y(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]], dtype=float)


def rot_z(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], dtype=float)


def R_from_euler(a: float, b: float, c: float) -> np.ndarray:
    """R = Ry(a) @ Rx(b) @ Rz(c)   (a=yaw about Y, b=pitch about X, c=roll about Z)."""
    return rot_y(a) @ rot_x(b) @ rot_z(c)


def euler_from_R(R: np.ndarray) -> tuple[float, float, float]:
    """Inverse of R_from_euler. Returns (a, b, c) in radians."""
    b = math.asin(max(-1.0, min(1.0, -R[1, 2])))
    a = math.atan2(R[0, 2], R[2, 2])
    c = math.atan2(R[1, 0], R[1, 1])
    return a, b, c


def clean_rotation(M: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """4x4 MediaPipe facial transformation matrix -> (orthonormal R, t)."""
    A = np.asarray(M, dtype=float)[:3, :3]
    A = A / np.linalg.norm(A, axis=0, keepdims=True)  # strip uniform scale
    U, _, Vt = np.linalg.svd(A)
    R = U @ Vt
    if np.linalg.det(R) < 0:
        U[:, -1] *= -1
        R = U @ Vt
    return R, np.asarray(M, dtype=float)[:3, 3].copy()


class OneEuro:
    """One Euro filter (Casiez et al. 2012): low lag when moving, steady when still."""

    def __init__(self, min_cutoff: float = 1.0, beta: float = 0.04, d_cutoff: float = 1.0):
        self.min_cutoff, self.beta, self.d_cutoff = min_cutoff, beta, d_cutoff
        self.reset()

    def reset(self) -> None:
        self.t_prev: float | None = None
        self.x_prev = 0.0
        self.dx_prev = 0.0

    @staticmethod
    def _alpha(cutoff: float, dt: float) -> float:
        tau = 1.0 / (2.0 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / dt)

    def __call__(self, x: float, t: float) -> float:
        if self.t_prev is None:
            self.t_prev, self.x_prev, self.dx_prev = t, x, 0.0
            return x
        dt = max(t - self.t_prev, 1e-4)
        dx = (x - self.x_prev) / dt
        a_d = self._alpha(self.d_cutoff, dt)
        dx_hat = a_d * dx + (1 - a_d) * self.dx_prev
        cutoff = self.min_cutoff + self.beta * abs(dx_hat)
        a = self._alpha(cutoff, dt)
        x_hat = a * x + (1 - a) * self.x_prev
        self.t_prev, self.x_prev, self.dx_prev = t, x_hat, dx_hat
        return x_hat


def soft_deadzone(v: float, dz: float) -> float:
    return math.copysign(max(abs(v) - dz, 0.0), v)


def shape(v: float, gain: float, ramp: float) -> float:
    """Gain is 1.0 at the centre (precise) and grows to `gain` at |v| >= ramp degrees."""
    f = 1.0 + (gain - 1.0) * min(abs(v) / ramp, 1.0)
    return v * f


# ----------------------------------------------------------------------------
# Pose processing
# ----------------------------------------------------------------------------
class PoseProcessor:
    """Camera-space head pose (R, t) -> opentrack-style (x, y, z, yaw, pitch, roll).

    Conventions of the OUTPUT (checked in --selftest):
        yaw   > 0  head turned to the user's RIGHT
        pitch > 0  head tilted UP
        roll  > 0  head tilted toward the user's RIGHT shoulder
        x > 0 user moved right, y > 0 up, z > 0 leaning toward the screen (cm)
    Each axis can be flipped with the --invert-* flags (or inside opentrack).
    """

    def __init__(self, cfg):
        self.cfg = cfg
        mk = lambda: OneEuro(cfg.min_cutoff, cfg.beta)  # noqa: E731
        self.f_rot = [mk(), mk(), mk()]
        tmk = lambda: OneEuro(cfg.min_cutoff * 0.5, cfg.beta * 0.5)  # noqa: E731
        self.f_pos = [tmk(), tmk(), tmk()]
        self.ref_R: np.ndarray | None = None
        self.ref_t: np.ndarray | None = None
        self.last = (0.0,) * 6
        self._center_requested = True

    def request_center(self) -> None:
        self._center_requested = True

    def update(self, R: np.ndarray, t: np.ndarray, now: float):
        cfg = self.cfg
        # Settings are read live so a GUI can change them while tracking.
        sg = lambda inv: -1.0 if inv else 1.0  # noqa: E731
        self.sign = [sg(cfg.invert_yaw), sg(cfg.invert_pitch), sg(cfg.invert_roll)]
        self.tsign = [sg(cfg.invert_x), sg(cfg.invert_y), sg(cfg.invert_z)]
        for f in self.f_rot:
            f.min_cutoff, f.beta = cfg.min_cutoff, cfg.beta
        for f in self.f_pos:
            f.min_cutoff, f.beta = cfg.min_cutoff * 0.5, cfg.beta * 0.5
        if self._center_requested or self.ref_R is None:
            self.ref_R, self.ref_t = R.copy(), t.copy()
            for f in self.f_rot + self.f_pos:
                f.reset()
            self._center_requested = False

        D = self.ref_R.T @ R  # rotation relative to the centred pose, in head frame
        a, b, c = euler_from_R(D)
        yaw, pitch, roll = -math.degrees(a), -math.degrees(b), math.degrees(c)
        rot = [yaw * self.sign[0], pitch * self.sign[1], roll * self.sign[2]]
        rot = [soft_deadzone(v, cfg.deadzone) for v in rot]
        rot = [f(v, now) for f, v in zip(self.f_rot, rot)]
        gains = (cfg.yaw_gain, cfg.pitch_gain, cfg.roll_gain)
        limits = (180.0, 90.0, 90.0)
        rot = [max(-lim, min(lim, shape(v, g, cfg.ramp))) for v, g, lim in zip(rot, gains, limits)]

        if cfg.translation:
            d = t - self.ref_t
            pos = [-d[0] * self.tsign[0], d[1] * self.tsign[1], d[2] * self.tsign[2]]
            pos = [f(v, now) * cfg.trans_gain for f, v in zip(self.f_pos, pos)]
            pos = [max(-30.0, min(30.0, v)) for v in pos]
        else:
            pos = [0.0, 0.0, 0.0]

        self.last = (pos[0], pos[1], pos[2], rot[0], rot[1], rot[2])
        return self.last


class Sender:
    """6 little-endian doubles (x, y, z cm; yaw, pitch, roll deg) -> opentrack UDP input."""

    def __init__(self, host: str, port: int):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addr = (host, port)
        self.count = 0          # packets handed to the OS
        self.errors = 0         # sends that raised (e.g. Windows "connection reset" when nothing listens)
        self.last_error = ""

    def send(self, pose) -> None:
        try:
            self.sock.sendto(struct.pack("<6d", *pose), self.addr)
            self.count += 1
        except OSError as e:    # never let a missing receiver kill the tracking loop
            self.errors += 1
            self.last_error = str(e)


# ----------------------------------------------------------------------------
# Camera + model
# ----------------------------------------------------------------------------
class Camera:
    """Grabs frames in a thread and always hands out the newest one (no queue lag)."""

    def __init__(self, index: int, width: int, height: int, fps: int):
        import cv2

        self.cv2 = cv2
        backend = cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY
        self.cap = cv2.VideoCapture(index, backend)
        if not self.cap.isOpened():
            raise RuntimeError(
                f"Cannot open camera {index}. Try --camera 1, close other apps using the webcam, "
                "or run --list-cameras."
            )
        self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self.cap.set(cv2.CAP_PROP_FPS, fps)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self._frame = None
        self._seq = 0
        self._lock = threading.Lock()
        self._new = threading.Event()
        self._stop = False
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop:
            ok, frame = self.cap.read()
            if not ok:
                time.sleep(0.01)
                continue
            with self._lock:
                self._frame = frame
                self._seq += 1
            self._new.set()

    def read(self, timeout: float = 0.5):
        if not self._new.wait(timeout):
            return None
        self._new.clear()
        with self._lock:
            return self._frame

    def size(self) -> tuple[int, int]:
        return (int(self.cap.get(self.cv2.CAP_PROP_FRAME_WIDTH)),
                int(self.cap.get(self.cv2.CAP_PROP_FRAME_HEIGHT)))

    def close(self) -> None:
        self._stop = True
        self._thread.join(timeout=1.0)
        self.cap.release()


def bundled_model() -> str | None:
    """face_landmarker.task shipped next to the script / inside the PyInstaller exe."""
    bases = [getattr(sys, "_MEIPASS", None)]
    bases.append(os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
                 else os.path.dirname(os.path.abspath(__file__)))
    for base in bases:
        if base:
            p = Path(base) / "face_landmarker.task"
            if p.exists() and p.stat().st_size > 1_000_000:
                return str(p)
    return None


def ensure_model(path_arg: str | None) -> str:
    if path_arg:
        return path_arg
    b = bundled_model()
    if b:
        return b
    p = Path.home() / ".cache" / "dcs_headtracker" / "face_landmarker.task"
    if p.exists() and p.stat().st_size > 1_000_000:
        return str(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading face model (~4 MB) to {p} ...")
    tmp = p.with_suffix(".part")
    try:
        urllib.request.urlretrieve(MODEL_URL, tmp)
        tmp.replace(p)
    except Exception as e:  # noqa: BLE001
        raise SystemExit(
            f"Model download failed: {e}\n"
            f"Download it manually from:\n  {MODEL_URL}\n"
            f"and run again with --model <path to face_landmarker.task>"
        )
    return str(p)


def create_landmarker(model_path: str):
    from mediapipe.tasks import python as mp_python
    from mediapipe.tasks.python import vision

    options = vision.FaceLandmarkerOptions(
        base_options=mp_python.BaseOptions(model_asset_path=model_path),
        running_mode=vision.RunningMode.VIDEO,
        num_faces=1,
        min_face_detection_confidence=0.5,
        min_face_presence_confidence=0.5,
        min_tracking_confidence=0.5,
        output_face_blendshapes=False,
        output_facial_transformation_matrixes=True,
    )
    return vision.FaceLandmarker.create_from_options(options)


def list_cameras() -> None:
    import cv2

    backend = cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY
    for i in range(6):
        cap = cv2.VideoCapture(i, backend)
        if cap.isOpened():
            ok, frame = cap.read()
            h, w = (frame.shape[:2] if ok else (0, 0))
            print(f"camera {i}: opened, {w}x{h}")
        cap.release()


# ----------------------------------------------------------------------------
# Hotkeys (optional, global: work while DCS has focus)
# ----------------------------------------------------------------------------
def install_hotkeys(cfg, on_center, on_pause) -> bool:
    """Register global hotkeys. Returns True on success."""
    try:
        import keyboard  # pip install keyboard
    except ImportError:
        print("[hotkeys] 'keyboard' not installed -> global hotkeys off "
              "(pip install keyboard). Preview-window keys still work.")
        return False
    try:
        keyboard.clear_all_hotkeys()  # lets the GUI re-register after a change
        keyboard.add_hotkey(cfg.hotkey_center, on_center)
        keyboard.add_hotkey(cfg.hotkey_pause, on_pause)
        print(f"[hotkeys] recenter = {cfg.hotkey_center}, pause = {cfg.hotkey_pause}")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[hotkeys] could not register global hotkeys: {e}")
        return False


# ----------------------------------------------------------------------------
# Modes
# ----------------------------------------------------------------------------
def direction_words(yaw: float, pitch: float, roll: float) -> str:
    th = 3.0
    parts = []
    if abs(yaw) > th:
        parts.append("RIGHT" if yaw > 0 else "LEFT")
    if abs(pitch) > th:
        parts.append("UP" if pitch > 0 else "DOWN")
    if abs(roll) > th:
        parts.append("tilt-R" if roll > 0 else "tilt-L")
    return " ".join(parts) or "centre"


def run_demo(cfg) -> None:
    sender = Sender(cfg.host, cfg.port)
    print(f"DEMO: sweeping yaw/pitch to {cfg.host}:{cfg.port}  (Ctrl+C to stop)")
    t0 = time.perf_counter()
    try:
        while True:
            t = time.perf_counter() - t0
            pose = (0.0, 0.0, 0.0,
                    60.0 * math.sin(2 * math.pi * 0.20 * t),
                    25.0 * math.sin(2 * math.pi * 0.13 * t),
                    0.0)
            sender.send(pose)
            time.sleep(1 / 60)
    except KeyboardInterrupt:
        sender.send((0.0,) * 6)


def run_live(cfg) -> None:
    import cv2
    import mediapipe as mp

    cam = Camera(cfg.camera, cfg.width, cfg.height, cfg.fps)  # camera first: its light turns on
    try:
        landmarker = create_landmarker(ensure_model(cfg.model))
    except BaseException:
        cam.close()
        raise
    proc = PoseProcessor(cfg)
    sender = Sender(cfg.host, cfg.port)
    state = {"paused": False}

    def toggle_pause():
        state["paused"] = not state["paused"]
        print("[pause]", "ON (sending centre)" if state["paused"] else "OFF")

    def do_center():
        proc.request_center()
        print("[center] next frame becomes the new centre")

    install_hotkeys(cfg, do_center, toggle_pause)
    print(f"Tracking -> UDP {cfg.host}:{cfg.port}. Look at the screen centre and press "
          f"{cfg.hotkey_center} (or 'c' in the preview) to recenter. 'q' quits.")

    t_start = time.perf_counter()
    last_ts = -1
    n_frames = n_found = 0
    t_stat = t_start
    lat_ms = 0.0
    try:
        while True:
            frame = cam.read()
            if frame is None:
                continue
            t0 = time.perf_counter()
            ts = int((t0 - t_start) * 1000)
            if ts <= last_ts:
                ts = last_ts + 1
            last_ts = ts
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)  # NOT mirrored: signs depend on it
            res = landmarker.detect_for_video(
                mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), ts)
            n_frames += 1

            found = bool(res.facial_transformation_matrixes)
            R = None
            if found:
                n_found += 1
                R, t = clean_rotation(res.facial_transformation_matrixes[0])
                if state["paused"]:
                    pose = (0.0,) * 6
                else:
                    pose = proc.update(R, t, t0)
            else:
                pose = (0.0,) * 6 if state["paused"] else proc.last  # freeze when lost
            sender.send(pose)
            lat_ms = 0.9 * lat_ms + 0.1 * (time.perf_counter() - t0) * 1000

            if time.perf_counter() - t_stat > 1.0:
                fps = n_frames / (time.perf_counter() - t_stat)
                print(f"\r{fps:5.1f} fps  face {100*n_found/max(n_frames,1):3.0f}%  "
                      f"proc {lat_ms:4.1f} ms  yaw {pose[3]:+6.1f}  pitch {pose[4]:+6.1f}  "
                      f"roll {pose[5]:+6.1f}   ", end="", flush=True)
                n_frames = n_found = 0
                t_stat = time.perf_counter()

            if not cfg.no_preview:
                disp = cv2.flip(frame, 1)  # mirror for display only
                h, w = disp.shape[:2]
                if found and R is not None:
                    lm = res.face_landmarks[0][NOSE_TIP]
                    p0 = (int((1 - lm.x) * w), int(lm.y * h))
                    for k, col in enumerate(((0, 0, 255), (0, 255, 0), (255, 128, 0))):
                        v = R[:, k]
                        p1 = (p0[0] + int(-v[0] * 70), p0[1] + int(-v[1] * 70))
                        cv2.line(disp, p0, p1, col, 2)
                txt = ("PAUSED" if state["paused"] else
                       f"yaw {pose[3]:+5.1f}  pitch {pose[4]:+5.1f}  roll {pose[5]:+5.1f}   "
                       f"[{direction_words(*pose[3:])}]")
                cv2.putText(disp, txt, (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                            (0, 255, 255) if found else (0, 0, 255), 2)
                if not found:
                    cv2.putText(disp, "NO FACE", (10, 52), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                                (0, 0, 255), 2)
                try:
                    cv2.imshow("dcs_headtracker  (c=center  p=pause  q=quit)", disp)
                    key = cv2.waitKey(1) & 0xFF
                except cv2.error:
                    print("\n[preview] this OpenCV build has no window support "
                          "(opencv-python-headless) -> preview off. Use the GUI instead.")
                    cfg.no_preview = True
                    continue
                if key in (ord("q"), 27):
                    break
                if key == ord("c"):
                    do_center()
                if key == ord("p"):
                    toggle_pause()
    except KeyboardInterrupt:
        pass
    finally:
        print()
        sender.send((0.0,) * 6)
        cam.close()
        landmarker.close()
        cv2.destroyAllWindows()


# ----------------------------------------------------------------------------
# Self test (no camera, no mediapipe needed)
# ----------------------------------------------------------------------------
def selftest() -> int:
    fails = 0

    def check(name: str, ok: bool, extra: str = "") -> None:
        nonlocal fails
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {extra}")
        fails += 0 if ok else 1

    # 1. Euler round trip
    rng = np.random.default_rng(0)
    worst = 0.0
    for _ in range(500):
        a, b, c = rng.uniform(-math.radians(170), math.radians(170)), \
            rng.uniform(-math.radians(80), math.radians(80)), \
            rng.uniform(-math.radians(80), math.radians(80))
        a2, b2, c2 = euler_from_R(R_from_euler(a, b, c))
        worst = max(worst, abs(a - a2), abs(b - b2), abs(c - c2))
    check("euler round-trip (500 random poses)", worst < 1e-9, f"(max err {worst:.1e} rad)")

    # 2. scale/orthogonality cleanup of a MediaPipe-style 4x4
    R0 = R_from_euler(0.3, -0.2, 0.1)
    M = np.eye(4)
    M[:3, :3] = R0 * 7.5
    M[:3, 3] = [1, 2, -40]
    R1, t1 = clean_rotation(M)
    check("clean_rotation strips scale", np.allclose(R1, R0, atol=1e-9) and np.allclose(t1, [1, 2, -40]))

    # 3. sign conventions (camera frame: x image-right, y up, z toward camera)
    class Cfg:  # minimal config
        min_cutoff, beta, deadzone, ramp = 1e7, 0.0, 0.0, 45.0  # filter ~ pass-through
        yaw_gain = pitch_gain = roll_gain = 1.0
        translation = True
        trans_gain = 1.0
        invert_yaw = invert_pitch = invert_roll = False
        invert_x = invert_y = invert_z = False

    def pose_for(a_deg=0.0, b_deg=0.0, c_deg=0.0, t=(0, 0, -40)):
        p = PoseProcessor(Cfg())
        p.update(np.eye(3), np.array([0, 0, -40.0]), 0.0)  # centre on frontal pose
        R = R_from_euler(*(math.radians(v) for v in (a_deg, b_deg, c_deg)))
        return p.update(R, np.array(t, float), 0.01)

    nose = lambda R: R @ np.array([0.0, 0.0, 1.0])  # noqa: E731  (face looks along +z)
    up = lambda R: R @ np.array([0.0, 1.0, 0.0])  # noqa: E731
    # user turns RIGHT -> nose moves toward image-left (x<0) -> yaw must be > 0
    Rr = R_from_euler(math.radians(-30), 0, 0)
    check("turn right: nose goes image-left", nose(Rr)[0] < 0)
    check("turn right -> yaw = +30", abs(pose_for(a_deg=-30)[3] - 30.0) < 0.01,
          f"(yaw={pose_for(a_deg=-30)[3]:.2f})")
    # user looks UP -> nose moves +y -> pitch must be > 0
    Ru = R_from_euler(0, math.radians(-25), 0)
    check("look up: nose goes +y", nose(Ru)[1] > 0)
    check("look up -> pitch = +25", abs(pose_for(b_deg=-25)[4] - 25.0) < 0.01,
          f"(pitch={pose_for(b_deg=-25)[4]:.2f})")
    # tilt toward user's RIGHT shoulder -> head 'up' vector moves image-left (x<0) -> roll > 0
    Rt = R_from_euler(0, 0, math.radians(20))
    check("tilt right: head-up goes image-left", up(Rt)[0] < 0)
    check("tilt right -> roll = +20", abs(pose_for(c_deg=20)[5] - 20.0) < 0.01,
          f"(roll={pose_for(c_deg=20)[5]:.2f})")
    # translation: user moves right (image-left, dx<0) -> x>0 ; leans in (dz>0) -> z>0
    px = pose_for(t=(-5, 0, -40))
    pz = pose_for(t=(0, 0, -35))
    check("move right 5 cm -> x = +5", abs(px[0] - 5.0) < 0.01, f"(x={px[0]:.2f})")
    check("lean toward screen 5 cm -> z = +5", abs(pz[2] - 5.0) < 0.01, f"(z={pz[2]:.2f})")

    # 4. centring is relative and in the head frame
    p = PoseProcessor(Cfg())
    Rn = R_from_euler(math.radians(10), math.radians(-15), 0)  # neutral pose is not frontal
    p.update(Rn, np.array([0, 0, -40.0]), 0.0)
    out = p.update(Rn, np.array([0, 0, -40.0]), 0.01)
    check("recentre at non-frontal pose -> zeros", max(abs(v) for v in out) < 1e-6)
    p.request_center()
    Rm = R_from_euler(math.radians(30), math.radians(-15), 0)
    p.update(Rm, np.array([0, 0, -40.0]), 0.02)  # becomes new centre
    out = p.update(Rm @ R_from_euler(math.radians(-20), 0, 0), np.array([0, 0, -40.0]), 0.03)
    check("after recentre, 20deg right turn reads +20 yaw", abs(out[3] - 20.0) < 0.01, f"(yaw={out[3]:.2f})")

    # 5. filter: steady under noise, follows a step
    f = OneEuro(min_cutoff=1.0, beta=0.04)
    noisy = [f(rng.normal(0, 0.3), i / 60) for i in range(600)]
    check("One-Euro attenuates 0.3deg noise", np.std(noisy[100:]) < 0.15, f"(std {np.std(noisy[100:]):.3f})")
    f = OneEuro(min_cutoff=1.0, beta=0.04)
    for i in range(30):
        f(0.0, i / 60)
    v = [f(40.0, (30 + i) / 60) for i in range(30)]
    check("One-Euro follows a 40deg step within 0.5 s", v[-1] > 38.0, f"(reached {v[-1]:.1f} in 0.5 s)")

    # 6. gain curve
    check("gain 1.0 at centre, full gain at ramp",
          abs(shape(1.0, 2.0, 45.0) - 1.0222) < 1e-3 and abs(shape(45.0, 2.0, 45.0) - 90.0) < 1e-9)

    # 7. UDP packet over loopback
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("127.0.0.1", 0))
    srv.settimeout(1.0)
    Sender("127.0.0.1", srv.getsockname()[1]).send((1.5, -2.5, 3.5, 10.0, -20.0, 30.0))
    data, _ = srv.recvfrom(1024)
    check("UDP packet = 48 bytes, 6 LE doubles", len(data) == 48 and
          struct.unpack("<6d", data) == (1.5, -2.5, 3.5, 10.0, -20.0, 30.0))
    srv.close()

    print("\nALL PASSED" if fails == 0 else f"\n{fails} FAILED")
    return 1 if fails else 0


# ----------------------------------------------------------------------------
def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Webcam head tracker -> opentrack (UDP) -> DCS World")
    g = p.add_argument_group("camera / model")
    g.add_argument("--camera", type=int, default=0)
    g.add_argument("--width", type=int, default=640)
    g.add_argument("--height", type=int, default=480)
    g.add_argument("--fps", type=int, default=60)
    g.add_argument("--model", default=None, help="path to face_landmarker.task (auto-downloaded if omitted)")
    g.add_argument("--no-preview", action="store_true", help="no window (saves CPU in-game)")
    g = p.add_argument_group("output")
    g.add_argument("--host", default="127.0.0.1")
    g.add_argument("--port", type=int, default=4242)
    g = p.add_argument_group("feel")
    g.add_argument("--yaw-gain", type=float, default=2.2, help="gain reached at --ramp degrees (1.0 at centre)")
    g.add_argument("--pitch-gain", type=float, default=1.6)
    g.add_argument("--roll-gain", type=float, default=1.0)
    g.add_argument("--ramp", type=float, default=45.0, help="degrees at which full gain is reached")
    g.add_argument("--deadzone", type=float, default=0.3, help="degrees")
    g.add_argument("--min-cutoff", type=float, default=1.0, help="lower = smoother when still")
    g.add_argument("--beta", type=float, default=0.04, help="higher = less lag when moving fast")
    g.add_argument("--translation", action="store_true", help="also send x/y/z (off by default: noisy)")
    g.add_argument("--trans-gain", type=float, default=1.0)
    g = p.add_argument_group("axis signs")
    for ax in ("yaw", "pitch", "roll", "x", "y", "z"):
        g.add_argument(f"--invert-{ax}", action="store_true")
    g = p.add_argument_group("hotkeys (global, needs `pip install keyboard`)")
    g.add_argument("--hotkey-center", default="alt+c")
    g.add_argument("--hotkey-pause", default="f11")
    g = p.add_argument_group("modes")
    g.add_argument("--demo", action="store_true", help="send a test sweep, no camera")
    g.add_argument("--selftest", action="store_true")
    g.add_argument("--list-cameras", action="store_true")
    g.add_argument("--minimized", action="store_true", help="GUI: start hidden in the tray")
    return p.parse_args(argv)


def main() -> int:
    cfg = parse_args()
    if cfg.selftest:
        return selftest()
    if cfg.list_cameras:
        list_cameras()
        return 0
    if cfg.demo:
        run_demo(cfg)
        return 0
    run_live(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
