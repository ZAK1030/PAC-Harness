"""Turn a model-selected RGB pixel into a calibrated robot-base candidate.

The model supplies native RGB pixels, never metric object coordinates. Depth
is in metres (the RGB-D provider explicitly converts it), is registered through the
factory depth/RGB transform, and is then mapped through the hand-eye transform.
This is geometry, not a proof of object identity or collision-free motion.
Planner selects the current target; Detector checks the completed action outcome.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from .rgbd import Observation
from .coordinates import resolve_pose_reference, convert_pose


class GroundingError(ValueError):
    pass


def _vector(value: Any, length: int, name: str) -> np.ndarray:
    try:
        result = np.asarray(value, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise GroundingError(f"{name} must contain {length} finite numbers") from exc
    if result.shape != (length,) or not np.isfinite(result).all():
        raise GroundingError(f"{name} must contain {length} finite numbers")
    if any(isinstance(item, (bool, np.bool_)) for item in value):
        raise GroundingError(f"{name} cannot contain booleans")
    return result


def _transform(value: Any, name: str) -> np.ndarray:
    try:
        matrix = np.asarray(value, dtype=float).reshape(4, 4)
    except (TypeError, ValueError) as exc:
        raise GroundingError(f"missing or invalid {name}") from exc
    rotation = matrix[:3, :3]
    if (not np.isfinite(matrix).all()
            or not np.allclose(matrix[3], [0, 0, 0, 1], atol=1e-7)
            or not np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-4)
            or not np.isclose(np.linalg.det(rotation), 1, atol=1e-4)):
        raise GroundingError(f"{name} must be a proper rigid transform in metres")
    return matrix


def _rotation(pose: np.ndarray) -> np.ndarray:
    roll, pitch, yaw = pose[3:]
    cr, sr, cp, sp, cy, sy = (math.cos(roll), math.sin(roll),
                             math.cos(pitch), math.sin(pitch),
                             math.cos(yaw), math.sin(yaw))
    return np.array([[cy, -sy, 0], [sy, cy, 0], [0, 0, 1]]) @ np.array(
        [[cp, 0, sp], [0, 1, 0], [-sp, 0, cp]]) @ np.array(
        [[1, 0, 0], [0, cr, -sr], [0, sr, cr]])


def _distortion(entry: dict) -> np.ndarray:
    coefficients = entry.get("distortion", [])
    if not coefficients:
        return np.zeros(8)
    values = _vector(coefficients, 8, "distortion")
    if not values.any():
        return values
    if (entry.get("distortion_order") != ["k1", "k2", "k3", "k4", "k5", "k6", "p1", "p2"]
            or entry.get("distortion_model_id") not in (3, 4)):
        raise GroundingError("unsupported distortion model or coefficient order")
    return values


def _distort(x: np.ndarray, y: np.ndarray, coefficients: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    k1, k2, k3, k4, k5, k6, p1, p2 = coefficients
    r2 = x * x + y * y
    denominator = 1 + r2 * (k4 + r2 * (k5 + r2 * k6))
    if np.any(np.abs(denominator) < 1e-10):
        raise GroundingError("distortion polynomial has a singularity")
    radial = (1 + r2 * (k1 + r2 * (k2 + r2 * k3))) / denominator
    return (x * radial + 2 * p1 * x * y + p2 * (r2 + 2 * x * x),
            y * radial + p1 * (r2 + 2 * y * y) + 2 * p2 * x * y)


def _undistort(x: np.ndarray, y: np.ndarray, coefficients: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    if not coefficients.any():
        return x, y
    undistorted_x, undistorted_y = x.copy(), y.copy()
    for _ in range(12):
        distorted_x, distorted_y = _distort(undistorted_x, undistorted_y, coefficients)
        undistorted_x += x - distorted_x
        undistorted_y += y - distorted_y
    check_x, check_y = _distort(undistorted_x, undistorted_y, coefficients)
    if (not np.isfinite(undistorted_x).all() or not np.isfinite(undistorted_y).all()
            or np.max(np.abs(check_x - x), initial=0) > 1e-7
            or np.max(np.abs(check_y - y), initial=0) > 1e-7):
        raise GroundingError("depth lens distortion inversion did not converge")
    return undistorted_x, undistorted_y


class Grounder:
    def __init__(self, config: dict, hardware_config: dict):
        self.config = dict(config)
        self.hardware_config = dict(hardware_config)
        views = self.config.get("views")
        if views is not None and (not isinstance(views, list) or not views or any(not isinstance(v, str) or not v for v in views) or len(set(views)) != len(views)):
            raise GroundingError("views must be distinct nonempty camera names")
        self._provenance: dict[str, dict] = {}

    def _load(self, key: str) -> dict:
        path_value = self.config.get(key)
        if not path_value:
            raise GroundingError(f"grounding.{key} is required")
        path = Path(path_value)
        try:
            raw = path.read_bytes()
            result = json.loads(raw)
        except (OSError, ValueError) as exc:
            raise GroundingError(f"cannot load {key}: {type(exc).__name__}") from exc
        if not isinstance(result, dict) or not isinstance(result.get("cameras"), dict) or not result["cameras"]:
            raise GroundingError(f"{key} must contain a nonempty cameras object")
        self._provenance[key] = {"path": str(path.resolve()), "sha256": hashlib.sha256(raw).hexdigest()}
        return result

    def validate_calibration_files(self) -> dict:
        """Check required camera extrinsics before spending model calls.

        This verifies configured data, not physical calibration accuracy.
        Runtime grounding still validates intrinsics, registration and images.
        """
        factory = self._load("factory_calibration_path")
        extrinsics = self._load("extrinsics_path")
        cameras = {}
        for view in self.config.get("views", list(factory["cameras"])):
            if not isinstance(factory["cameras"].get(view), dict):
                raise GroundingError(f"{view} factory calibration missing: {self.config['factory_calibration_path']}")
            entry = extrinsics["cameras"].get(view, {})
            if entry.get("calibrated") is not True:
                raise GroundingError(f"{view} robot extrinsic is not marked calibrated: {self.config['extrinsics_path']}")
            names = [key for key in ("T_tool_from_camera", "T_robot_base_from_camera") if key in entry]
            if len(names) != 1:
                raise GroundingError(f"{view} must declare exactly one camera mounting transform")
            name = names[0]
            _transform(entry.get(name), f"{view} {name}")
            cameras[view] = {"calibrated": True, "transform_name": name}
        return {"ok": True, "cameras": cameras, "files": dict(self._provenance),
                "scope": "file contents and rigid transforms; not physical accuracy"}

    def _intrinsics(self, entry: dict, kind: str, shape: tuple[int, int], observation: Observation, view: str) -> tuple[np.ndarray, np.ndarray]:
        values = entry.get(f"{kind}_intrinsics", {})
        source = [values.get("width"), values.get("height")]
        if any(isinstance(n, bool) or not isinstance(n, int) or n <= 0 for n in source):
            raise GroundingError(f"missing {view} {kind} calibration resolution")
        output = [shape[1], shape[0]]
        if output != source:
            resize = observation.metadata.get("transport_resize", {}).get(view, {})
            if (resize.get(f"{kind}_source_wh") != source
                    or resize.get(f"{kind}_output_wh") != output):
                raise GroundingError(f"{view} {kind} resolution changed without verified full-image resize metadata")
            if kind == "depth" and resize.get("depth_interpolation") != "nearest":
                raise GroundingError("metric grounding requires nearest-neighbor depth resizing")
        for field in ("cropped", "mirrored", "rotated"):
            flags = observation.metadata.get(field, {})
            if flags is True or (isinstance(flags, dict) and flags.get(view)):
                raise GroundingError(f"{view} {field} images do not match calibration")
        try:
            matrix = np.asarray(values.get("K_row_major_3x3"), dtype=float).reshape(3, 3).copy()
        except (TypeError, ValueError) as exc:
            raise GroundingError(f"invalid {view} {kind} intrinsics") from exc
        if (not np.isfinite(matrix).all() or matrix[0, 0] <= 0 or matrix[1, 1] <= 0
                or not np.allclose(matrix[2], [0, 0, 1]) or abs(matrix[0, 1]) > 1e-8 or abs(matrix[1, 0]) > 1e-8):
            raise GroundingError(f"invalid {view} {kind} intrinsics")
        # Scale each stream's own optics. This never aligns depth by stretching
        # its image to the RGB resolution: registration still uses the rigid T.
        matrix[0] *= output[0] / source[0]
        matrix[1] *= output[1] / source[1]
        return matrix, _distortion(values)

    def _receiver_reference(self, observation: Observation) -> str:
        try:
            return resolve_pose_reference(self.hardware_config, observation.state)
        except ValueError as exc:
            raise GroundingError(str(exc)) from exc

    def _camera_transform(self, view: str, observation: Observation, extrinsics: dict, reference: str, rotation: np.ndarray) -> np.ndarray:
        entry = extrinsics["cameras"].get(view, {})
        if entry.get("calibrated") is not True:
            raise GroundingError(f"{view} robot extrinsic is not marked calibrated")
        if "T_robot_base_from_camera" in entry and "T_tool_from_camera" not in entry:
            return _transform(entry.get("T_robot_base_from_camera"), f"{view} T_robot_base_from_camera")
        if "T_tool_from_camera" not in entry or "T_robot_base_from_camera" in entry:
            raise GroundingError(f"{view} must declare exactly one camera mounting transform")
        base_from_tool = np.eye(4)
        base_from_tool[:3, :3] = rotation
        base_from_tool[:3, 3] = convert_pose(observation.pose, reference, "tcp", self.hardware_config)[:3]
        return base_from_tool @ _transform(entry.get("T_tool_from_camera"), f"{view} T_tool_from_camera")

    def _tcp_offset(self) -> np.ndarray:
        return _vector(self.hardware_config.get("tcp_offset_tool_m"), 3, "flange-local tcp_offset_tool_m")

    def register_depth(self, observation: Observation, view: str) -> dict:
        """将原生米制深度投影到当前 RGB 像素，供定位与模型观测共同使用。

        仅保留各 RGB 像素最近的实测表面；未采到的像素保持 NaN，不做
        插值、背景替代或遮挡补全。返回的 Z 是 RGB 相机光轴深度，既不是
        到相机的欧氏距离，也不是机器人与障碍物之间的最小间隙。
        """
        if not isinstance(view, str) or view not in self._load("factory_calibration_path")["cameras"] or view not in self.config.get("views", [view]):
            raise GroundingError("grounding view is not a configured calibrated camera")
        rgb = np.asarray(observation.images.get(view))
        depth = np.asarray(observation.depths.get(view))
        if (rgb.ndim != 3 or rgb.shape[2] != 3 or depth.ndim != 2
                or not rgb.size or not depth.size):
            raise GroundingError(f"missing {view} RGB or depth image")
        if not np.issubdtype(depth.dtype, np.floating):
            raise GroundingError("Observation depth must already be floating-point metres")
        factory = self._load("factory_calibration_path")
        entry = factory["cameras"].get(view, {})
        if entry.get("mirrored") is not False:
            raise GroundingError("factory calibration must explicitly describe unmirrored images")
        if entry.get("depth_aligned_to_rgb") is not False:
            raise GroundingError("this projector requires native unaligned factory depth")
        aligned = observation.metadata.get("depth_aligned_to_rgb", False)
        if aligned is True or (isinstance(aligned, dict) and aligned.get(view) is True):
            raise GroundingError("already aligned depth cannot be re-registered as raw depth")
        rgb_k, rgb_distortion = self._intrinsics(entry, "rgb", rgb.shape[:2], observation, view)
        depth_k, depth_distortion = self._intrinsics(entry, "depth", depth.shape, observation, view)
        registration = entry.get("depth_to_rgb_extrinsic", {})
        if registration.get("direction") != "T_rgb_camera_from_depth_camera":
            raise GroundingError("depth-to-RGB transform direction is missing or ambiguous")
        rgb_from_depth = _transform(registration.get("T_row_major_4x4"), "T_rgb_camera_from_depth_camera")
        minimum_depth = float(self.config.get("min_depth_m", 0.05))
        maximum_depth = float(self.config.get("max_depth_m", 2.5))
        if (not math.isfinite(minimum_depth) or not math.isfinite(maximum_depth)
                or not 0 < minimum_depth < maximum_depth):
            raise GroundingError("depth range must be finite with 0 < min_depth_m < max_depth_m")
        valid = np.isfinite(depth) & (depth >= minimum_depth) & (depth <= maximum_depth)
        rows, columns = np.nonzero(valid)
        if not rows.size:
            raise GroundingError("no valid metric depth samples")
        z = depth[valid].astype(float)
        x, y = _undistort((columns - depth_k[0, 2]) / depth_k[0, 0],
                         (rows - depth_k[1, 2]) / depth_k[1, 1], depth_distortion)
        points = np.column_stack((x * z, y * z, z)) @ rgb_from_depth[:3, :3].T + rgb_from_depth[:3, 3]
        points = points[np.isfinite(points).all(axis=1) & (points[:, 2] > 0)]
        if not points.size:
            raise GroundingError("no depth samples project in front of RGB camera")
        distorted_x, distorted_y = _distort(points[:, 0] / points[:, 2], points[:, 1] / points[:, 2], rgb_distortion)
        rounded_u = np.rint(rgb_k[0, 0] * distorted_x + rgb_k[0, 2])
        rounded_v = np.rint(rgb_k[1, 1] * distorted_y + rgb_k[1, 2])
        # 在转整型之前剔除无效或视野外投影，避免极端离轴值溢出。
        in_view = (np.isfinite(rounded_u) & np.isfinite(rounded_v)
                   & (rounded_u >= 0) & (rounded_u < rgb.shape[1])
                   & (rounded_v >= 0) & (rounded_v < rgb.shape[0]))
        points = points[in_view]
        u = rounded_u[in_view].astype(np.int64)
        v = rounded_v[in_view].astype(np.int64)
        if not points.size:
            raise GroundingError("no registered depth projects inside native RGB image")
        # 同一 RGB 像素只保留最近表面，禁止背景覆盖前景遮挡物。
        order = np.argsort(points[:, 2], kind="stable")
        _, first = np.unique(v[order] * rgb.shape[1] + u[order], return_index=True)
        chosen = order[first]
        points, u, v = points[chosen], u[chosen], v[chosen]
        registered = np.full(rgb.shape[:2], np.nan, dtype=np.float64)
        registered[v, u] = points[:, 2]
        return {
            "depth_rgb_m": registered,
            "u": u,
            "v": v,
            "points_rgb_m": points,
            "provenance": {
                "factory_calibration_path": dict(self._provenance["factory_calibration_path"]),
                "view": view,
                "observation_id": observation.id,
                "observation_stamp_ns": observation.stamp_ns,
                "pixel_coordinates": "native RGB; origin top-left, u right, v down",
                "rgb_shape": list(rgb.shape), "depth_shape": list(depth.shape),
                "registration": "factory depth intrinsics + distortion + T_rgb_from_depth + RGB distortion/intrinsics",
                "depth_semantics": "metres along RGB camera optical axis (Z); not Euclidean range or body clearance",
                "source_depth_filter_m": [minimum_depth, maximum_depth],
                "valid_source_samples": int(valid.sum()),
                "registered_pixels": len(points),
                "valid_fraction": len(points) / registered.size,
                "missing_pixels": "NaN; no interpolation, hole filling or background substitution",
                "occlusion_rule": "nearest measured surface per native RGB pixel",
                "semantic_identity_verified": False,
            },
        }

    def resolve(self, plan: dict, observation: Observation) -> dict:
        action = plan.get("action", {})
        selection = action.get("grounding")
        if not isinstance(selection, dict):
            raise GroundingError("coarse move requires action.grounding with a native RGB pixel")
        forbidden = {"target_xyz_m", "object_xyz_base_m", "xyz", "xyz_m", "source"}
        if forbidden.intersection(selection) or {"target_xyz_m", "object_xyz_base_m"}.intersection(action):
            raise GroundingError("model-supplied metric coordinates cannot certify calibrated grounding")
        view = selection.get("view")
        if not isinstance(view, str) or view not in self._load("factory_calibration_path")["cameras"] or view not in self.config.get("views", [view]):
            raise GroundingError("grounding view is not a configured calibrated camera")
        pixel = _vector(selection.get("pixel_uv"), 2, "native RGB pixel_uv")
        offset = _vector(selection.get("approach_offset_m", [0, 0, 0]), 3, "base-frame approach_offset_m")
        offset_bound = self.config.get("max_approach_offset_m", 0.25)
        if type(offset_bound) not in (int, float) or not math.isfinite(offset_bound) or offset_bound < 0:
            raise GroundingError("invalid max_approach_offset_m")
        if np.linalg.norm(offset) > offset_bound:
            raise GroundingError("approach offset exceeds configured bound")
        target = action.get("target")
        if not isinstance(target, str) or not target.strip():
            raise GroundingError("grounding requires a semantic target")
        rgb = np.asarray(observation.images.get(view))
        depth = np.asarray(observation.depths.get(view))
        if rgb.ndim != 3 or rgb.shape[2] != 3 or depth.ndim != 2:
            raise GroundingError(f"missing {view} RGB or depth image")
        if not np.issubdtype(depth.dtype, np.floating):
            raise GroundingError("Observation depth must already be floating-point metres")
        if not (0 <= pixel[0] < rgb.shape[1] and 0 <= pixel[1] < rgb.shape[0]):
            raise GroundingError("selected pixel is outside native RGB image")
        extrinsics = self._load("extrinsics_path")
        registered = self.register_depth(observation, view)
        points, u, v = registered["points_rgb_m"], registered["u"], registered["v"]
        radius = self.config.get("sample_radius_px", 4)
        if type(radius) is not int or radius < 0 or radius > 10:
            raise GroundingError("sample_radius_px must be between 0 and 10")
        nearby = (np.abs(u - pixel[0]) <= radius) & (np.abs(v - pixel[1]) <= radius)
        points = points[nearby]
        if not points.size:
            raise GroundingError("no registered depth near selected RGB pixel; do not substitute background XYZ")
        spread = float(np.ptp(points[:, 2]))
        max_spread = self.config.get("max_depth_spread_m", 0.02)
        if type(max_spread) not in (int, float) or not math.isfinite(max_spread) or max_spread < 0:
            raise GroundingError("invalid max_depth_spread_m")
        if spread > max_spread:
            raise GroundingError(f"ambiguous depth surfaces near selected pixel: spread={spread:.4f} m")
        rgb_point = np.median(points, axis=0)
        pose = _vector(observation.pose, 6, "receiver pose")
        rotation = _rotation(pose)
        reference = self._receiver_reference(observation)
        base_from_camera = self._camera_transform(view, observation, extrinsics, reference, rotation)
        object_xyz = base_from_camera[:3, :3] @ rgb_point + base_from_camera[:3, 3]
        target_tcp = object_xyz + offset
        target_receiver = np.asarray(convert_pose(target_tcp.tolist() + pose[3:].tolist(), "tcp", reference, self.hardware_config)[:3])
        current_tcp = convert_pose(pose.tolist(), reference, "tcp", self.hardware_config)[:3]
        object_receiver = convert_pose(object_xyz.tolist() + pose[3:].tolist(), "tcp", reference, self.hardware_config)[:3]
        provenance = {
            **self._provenance, "view": view, "pixel_uv": pixel.tolist(),
            "pixel_coordinates": "native RGB; origin top-left, u right, v down",
            "rgb_shape": list(rgb.shape), "depth_shape": list(depth.shape),
            "registration": "factory depth intrinsics + distortion + T_rgb_from_depth + RGB distortion/intrinsics",
            "valid_samples": len(points), "sample_radius_px": radius,
            "depth_spread_m": spread, "point_rgb_m": rgb_point.tolist(),
            "receiver_pose_reference": reference,
            "receiver_pose_reference_config": self.hardware_config.get("pose_reference", "receiver_reported"),
            "approach_offset_m": offset.tolist(), "target_tcp_xyz_m": target_tcp.tolist(),
            "current_tcp_xyz_m": current_tcp,
            "object_target_receiver_xyz_m": object_receiver,
            "distance_to_object_m": math.dist(current_tcp, object_xyz.tolist()),
            "distance_to_approach_target_m": math.dist(current_tcp, target_tcp.tolist()),
            "distance_scope": "Configured TCP to calibrated object point; staging offset excluded from object distance. Not measured finger/glass clearance.",
            "semantic_identity_verified": False,
        }
        return {"source": "calibrated_rgbd", "observation_id": observation.id,
                "observation_stamp_ns": observation.stamp_ns, "target": target,
                "object_xyz_base_m": object_xyz.tolist(), "target_xyz_m": target_receiver.tolist(),
                "provenance": provenance}
