"""Explicit device factories and optional OpenCV camera observations."""
import importlib
from pathlib import Path
import re
import time
import uuid

from .storage import atomic_write, confined


def load_device(spec, *, root):
    """Load trusted scenario code. Constructors must not enable or move robots."""
    factory = spec.get("factory", "")
    if not isinstance(factory, str) or not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*", factory):
        raise ValueError("device factory must be module:function")
    settings = spec.get("settings", {})
    if not isinstance(settings, dict):
        raise ValueError("device settings must be an object")
    module, name = factory.split(":")
    return getattr(importlib.import_module(module), name)(root=Path(root), config=dict(settings))


class OpenCVCamera:
    def __init__(self, source=0):
        if type(source) is not int and not isinstance(source, str):
            raise ValueError("camera source must be a device index or path/URL")
        import cv2  # Optional dependency; no device access at module import.
        self.cv = cv2
        self.capture = cv2.VideoCapture(source)
        if not self.capture.isOpened():
            self.capture.release()
            raise RuntimeError("Camera could not be opened; check device, permissions and driver")

    def read_png(self):
        ok, frame = self.capture.read()
        if not ok or frame is None:
            raise RuntimeError("Camera did not return a frame")
        ok, encoded = self.cv.imencode(".png", frame)
        if not ok:
            raise RuntimeError("Camera frame could not be encoded")
        return encoded.tobytes()

    def close(self):
        self.capture.release()


def opencv_camera(*, root, config):
    return OpenCVCamera(config.get("source", 0))


class CameraSet:
    def __init__(self, *, root, run_directory, cameras):
        self.root = Path(root).resolve()
        relative = Path(run_directory).resolve().relative_to(self.root).as_posix()
        self.folder = confined(self.root, relative + "/frames")
        self.devices = {}
        if not isinstance(cameras, dict) or not cameras:
            raise ValueError("Configure at least one named camera")
        try:
            for name, spec in cameras.items():
                if not re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", name):
                    raise ValueError("camera name must be a simple lowercase identifier")
                self.devices[name] = load_device(spec, root=self.root)
        except BaseException:
            self.close()
            raise

    def observe(self):
        artifacts = []
        for name, camera in self.devices.items():
            raw = camera.read_png()
            if not isinstance(raw, bytes) or not raw.startswith(b'\x89PNG\r\n\x1a\n') or len(raw) > 8_000_000:
                raise ValueError("Camera must return PNG bytes, at most 8 MB")
            path = self.folder / f"{name}-{uuid.uuid4().hex}.png"
            atomic_write(path, raw)
            artifacts.append({"path": path.relative_to(self.root).as_posix(), "camera": name,
                              "received_at_ns": time.time_ns(), "type": "image"})
        return {"artifacts": artifacts, "note": "Frames captured sequentially; receipt time is not exposure time"}

    def close(self):
        devices, self.devices = self.devices, {}
        for device in devices.values():
            try:
                device.close()
            except Exception:
                pass
