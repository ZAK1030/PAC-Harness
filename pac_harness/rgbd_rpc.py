"""Read existing hardware.read_observation RGB-D RPC snapshots without motion."""
import base64
import io
import math
import time
import uuid
import zlib

import numpy as np
from PIL import Image

from .rgbd import Observation
from .rokae_bridge import RokaeJointBridge
from .robot_backends import _vector


def payload_bytes(value):
    if isinstance(value, str):
        return base64.b64decode(value, validate=True)
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    raise ValueError("RGB-D payload must contain bytes or base64")


class RGBDRPC:
    def __init__(self, config, *, request=None):
        self.url = config["observation_url"].rstrip("/")
        self.views = config["views"]
        if not isinstance(self.views, list) or not self.views or any(not isinstance(v, str) or not v for v in self.views):
            raise ValueError("Specify RGB-D camera names")
        self.request = request or RokaeJointBridge._http
        self.last_stamps = {}
        self.max_age_ms = config.get("max_camera_age_ms", 500)
        if type(self.max_age_ms) not in (int, float) or not math.isfinite(self.max_age_ms) or self.max_age_ms <= 0:
            raise ValueError("Invalid max_camera_age_ms")

    def capture(self, robot_state):
        started = time.monotonic()
        envelope = self.request(self.url + "/call", {"method": "hardware.read_observation", "args": [], "kwargs": {}})
        if not isinstance(envelope, dict) or envelope.get("ok") is False:
            raise ValueError("RGB-D RPC failed")
        raw = envelope.get("result", envelope)
        state = raw["robot_state"]
        stamp = raw.get("requested_at_ns", raw.get("observation_requested_at_ns"))
        if type(stamp) is not int or stamp <= 0:
            raise ValueError("RPC snapshot requires its robot-host request timestamp")
        images, depths = {}, {}
        metadata_keys = ("transport_resize", "cropped", "mirrored", "rotated", "depth_aligned_to_rgb",
                         "camera_timestamps_ns", "camera_age_ms")
        metadata = {k: raw[k] for k in metadata_keys if k in raw}
        for view in self.views:
            captured = raw.get("camera_timestamps_ns", {}).get(view)
            age = raw.get("camera_age_ms", {}).get(view)
            if (type(captured) is not int or captured <= self.last_stamps.get(view, 0)
                    or type(age) not in (int, float) or not math.isfinite(age) or not 0 <= age <= self.max_age_ms):
                raise ValueError(f"{view} image is frozen, stale or lacks capture metadata")
            with Image.open(io.BytesIO(payload_bytes(raw["images_jpeg"][view]))) as image:
                images[view] = np.asarray(image.convert("RGB"), dtype=np.uint8).copy()
            shape = raw["depth_shapes"][view]
            if not isinstance(shape, (tuple, list)) or len(shape) != 2 or any(type(n) is not int or n <= 0 for n in shape):
                raise ValueError("Invalid depth shape")
            size = shape[0] * shape[1] * 2
            if size > 64_000_000:
                raise ValueError("Depth snapshot is too large")
            decoder = zlib.decompressobj()
            decoded = decoder.decompress(payload_bytes(raw["depth_zlib_uint16_le"][view]), size+1)
            if len(decoded) != size or not decoder.eof or decoder.unused_data:
                raise ValueError("Depth payload size/shape mismatch")
            scale = raw.get("depth_scale_mm", {}).get(view)
            if type(scale) not in (int, float) or not math.isfinite(scale) or scale <= 0:
                raise ValueError("Depth scale must be explicitly provided in mm per raw unit")
            depths[view] = np.frombuffer(decoded, dtype="<u2").reshape(shape).astype(float) * (scale / 1000)
        # Update only after every view has decoded, preventing partial reads
        # from silently establishing a new synchronized snapshot.
        self.last_stamps.update({v: raw["camera_timestamps_ns"][v] for v in self.views})
        return Observation(id=uuid.uuid4().hex, captured_monotonic=started,
            pose=_vector(state.get("pose", state.get("tcp_pose")), 6, "snapshot robot pose"),
            stamp_ns=stamp, images=images, depths=depths, metadata=metadata, state=state)

    def close(self):
        pass  # RPC camera streams belong to the external service.


def create(*, root, config):
    return RGBDRPC(config)
