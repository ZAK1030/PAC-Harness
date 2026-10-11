"""Calibrated RGB-D approach and visual relative correction in one adapter."""
from collections import OrderedDict
from dataclasses import asdict
import math
from pathlib import Path
import time

import numpy as np

from .coordinates import convert_pose, require_workspace, resolve_pose_reference
from .devices import load_device
from .grounding import Grounder, GroundingError
from .rgbd import Observation
from .robot_backends import _vector, receipt_evidence
from .storage import confined, loads, write_json
from .tools import Tool, parameters


def positive(value, name):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return float(value)


def pose_matches(before, after, translation=.0005, orientation=.001):
    return (math.dist(before[:3], after[:3]) <= translation
            and max(abs((a-b+math.pi) % (2*math.pi)-math.pi) for a, b in zip(before[3:], after[3:])) <= orientation)


def approach_candidate(grounding, pose, safety, hardware):
    """Bound an approach ray at first entry into the fine radius of the object.

    Object distance excludes the approach offset, as in Harness-GPT. A ray
    toward an offset waypoint may miss the fine sphere; it must not stop early.
    """
    reference = grounding["provenance"]["receiver_pose_reference"]
    current_tcp = convert_pose(pose, reference, "tcp", hardware)[:3]
    obj = grounding["object_xyz_base_m"]
    threshold = positive(safety.get("fine_entry_distance_m", .05), "fine_entry_distance_m")
    if math.dist(current_tcp, obj) <= threshold + 1e-9:
        raise ValueError("Within fine entry distance; use current evidence for move_relative")
    delta = [a-b for a, b in zip(grounding["target_xyz_m"], pose[:3])]
    distance = math.dist(delta, [0, 0, 0])
    if distance <= 1e-7:
        raise ValueError("Approach waypoint reached; choose fresh evidence or a new approach offset")
    cap = positive(safety.get("coarse_step_m", .05), "coarse_step_m")
    relative = [a-b for a, b in zip(current_tcp, obj)]
    projection = sum(a*b/distance for a, b in zip(relative, delta))
    discriminant = projection**2 - (sum(x*x for x in relative) - threshold**2)
    if projection < 0 and discriminant > 1e-18:
        entry = -projection - math.sqrt(discriminant)
        if 0 <= entry <= distance:
            cap = min(cap, entry)
    applied = [x * min(1, cap/distance) for x in delta]
    if math.dist(applied, [0, 0, 0]) <= 1e-7:
        raise ValueError("No coarse increment remains; replan in fine mode")
    return {"target_pose": [pose[i] + applied[i] for i in range(3)] + pose[3:],
            "delta_m": applied, "distance_to_object_m": math.dist(current_tcp, obj),
            "distance_to_approach_target_m": distance, "motion_mode": "coarse"}


class CartesianEnvironment:
    def __init__(self, root, run_directory, backend, provider, config, *, output=print):
        self.root, self.directory = Path(root).resolve(), Path(run_directory).resolve()
        self.backend, self.provider, self.output = backend, provider, output
        self.config = dict(config)
        self.hardware = dict(config["hardware"])
        self.safety = dict(config["safety"])
        if type(config.get("motion_enabled", False)) is not bool:
            raise ValueError("motion_enabled must be boolean")
        # A coordinate reference is mandatory; do not inherit legacy guesses.
        if self.safety.get("workspace_reference") not in {"tcp", "flange"}:
            raise ValueError("Declare safety.workspace_reference as tcp or flange")
        grounding = dict(config["grounding"])
        for key in ("factory_calibration_path", "extrinsics_path"):
            grounding[key] = str(confined(self.root, grounding[key]))
        self.grounder = Grounder(grounding, self.hardware)
        self.calibration = self.grounder.validate_calibration_files()
        self.frames = OrderedDict()

    def observe(self):
        robot = self.backend.state()
        frame = self.provider.capture(robot)
        if not isinstance(frame, Observation) or not isinstance(frame.id, str) or not frame.id or frame.id in self.frames:
            raise ValueError("RGB-D provider must return a unique Observation snapshot")
        if type(frame.stamp_ns) is not int or frame.stamp_ns <= 0:
            raise ValueError("RGB-D observation must carry its capture timestamp")
        frame.pose = _vector(frame.pose, 6, "captured pose")
        if not pose_matches(frame.pose, _vector(robot.pose, 6, "live pose")):
            raise ValueError("RGB-D snapshot pose does not match robot feedback")
        self._fresh(frame)
        reference = resolve_pose_reference(self.hardware, frame.state)
        # Deep copy pixels to prevent a streaming camera from replacing evidence.
        frame.images = {k: np.array(v, copy=True) for k, v in frame.images.items()}
        frame.depths = {k: np.array(v, copy=True) for k, v in frame.depths.items()}
        artifacts, catalog = [], []
        from PIL import Image
        for view in self.calibration["cameras"]:
            rgb, depth = frame.images.get(view), frame.depths.get(view)
            if rgb is None or depth is None or rgb.ndim != 3 or rgb.shape[2] != 3 or rgb.dtype != np.uint8 or depth.ndim != 2:
                raise ValueError(f"Missing native uint8 RGB or metric depth for {view}")
            if not np.issubdtype(depth.dtype, np.floating):
                raise ValueError("Depth must be floating point metres, not millimetres")
            # Filenames use counts, not untrusted camera names or source IDs.
            relative = self.directory.relative_to(self.root).as_posix()
            path = confined(self.root, f"{relative}/frames/{time.time_ns()}-{len(artifacts)}.png")
            path.parent.mkdir(parents=True, exist_ok=True)
            Image.fromarray(rgb).save(path)
            artifacts.append({"path": path.relative_to(self.root).as_posix(), "type": "image",
                              "camera": view, "observation_id": frame.id})
            catalog.append({"view": view, "rgb_shape": list(rgb.shape), "depth_shape": list(depth.shape),
                            "pixel_coordinates": "native RGB; u right, v down; origin top-left"})
        self.frames[frame.id] = frame
        while len(self.frames) > 8:
            self.frames.popitem(last=False)
        return {"observation_id": frame.id, "stamp_ns": frame.stamp_ns, "robot": asdict(robot),
                "pose_reference": reference, "views": catalog, "artifacts": artifacts,
                "calibration": self.calibration}

    def _fresh(self, frame):
        captured = frame.captured_monotonic
        maximum = positive(self.config.get("max_observation_age_s", 30), "max_observation_age_s")
        if type(captured) not in (int, float) or not math.isfinite(captured) or not 0 <= time.monotonic()-captured <= maximum:
            raise ValueError("RGB-D observation is stale or has an invalid local capture age")

    def actions(self):
        common = {"observation_id": {"type": "string"}, "target": {"type": "string"}}
        return [
            {"name": "move_calibrated", "description":
             "Approach a selected native RGB pixel using registered depth and robot calibration. "
             "One bounded increment per action, not a whole trajectory. Model metric coordinates are forbidden.",
             "parameters": parameters({**common, "view": {"type": "string", "enum": list(self.calibration["cameras"])},
                 "pixel_uv": {"type": "array", "items": {"type": "number"}},
                 "approach_offset_m": {"type": "array", "items": {"type": "number"}}},
                 ("observation_id", "target", "view", "pixel_uv"))},
            {"name": "move_relative", "description":
             "Apply one visual correction in robot-base metres using current evidence. Orientation remains unchanged.",
             "parameters": parameters({**common, "delta_m": {"type": "array", "items": {"type": "number"}}},
                 ("observation_id", "target", "delta_m"))}]

    def candidate(self, action, observation):
        args = action["arguments"]
        selected = self.frames.get(args["observation_id"])
        if selected is None:
            raise ValueError("Selected RGB-D snapshot is unavailable; acquire fresh evidence")
        self._fresh(selected)
        if not isinstance(args["target"], str) or not args["target"].strip():
            raise ValueError("A semantic target is required")
        pose = _vector(observation["robot"]["pose"], 6, "live pose")
        if not pose_matches(selected.pose, pose):
            raise ValueError("Robot moved since the selected image; replan with fresh evidence")
        reference = resolve_pose_reference(self.hardware, selected.state)
        if observation["pose_reference"] != reference:
            raise ValueError("Pose reference changed since capture")
        if action["name"] == "move_calibrated":
            allowed = {"observation_id", "target", "view", "pixel_uv", "approach_offset_m"}
            if set(args) - allowed:
                raise GroundingError("Model metric coordinates cannot certify calibrated grounding")
            result = self.grounder.resolve({"action": {"target": args["target"], "grounding": {
                k: args[k] for k in ("view", "pixel_uv", "approach_offset_m") if k in args}}}, selected)
            candidate = approach_candidate(result, pose, self.safety, self.hardware)
            candidate["grounding"] = result
        elif action["name"] == "move_relative":
            if set(args) != {"observation_id", "target", "delta_m"}:
                raise ValueError("Relative correction requires only observation, target and delta_m")
            delta = _vector(args["delta_m"], 3, "visual delta_m")
            distance = math.dist(delta, [0, 0, 0])
            if not 1e-7 < distance <= positive(self.safety.get("fine_step_m", .01), "fine_step_m"):
                raise ValueError("Relative correction exceeds fine step or is zero")
            candidate = {"target_pose": [pose[i]+delta[i] for i in range(3)] + pose[3:],
                         "delta_m": delta, "motion_mode": "fine"}
        else:
            raise ValueError("Unsupported Cartesian action")
        candidate["workspace"] = require_workspace({"start": pose, "target": candidate["target_pose"]},
            self.safety, pose_reference=reference, hardware_config=self.hardware)
        candidate.update(initial_pose=pose, observation_id=selected.id, observation_stamp_ns=selected.stamp_ns,
                         pose_reference=reference)
        return candidate

    def validate(self, action, observation):
        if not self.config.get("motion_enabled", False):
            raise ValueError("Cartesian motion is disabled")
        robot = observation["robot"]
        if robot["connected"] is not True or robot["enabled"] is not True or robot.get("fault"):
            raise ValueError("Robot must be connected, enabled and fault-free")
        self.candidate(action, observation)

    def _path(self, request_id):
        if not isinstance(request_id, str) or not request_id or not request_id.isalnum():
            raise ValueError("Invalid request ID")
        return confined(self.directory, "cartesian-receipts/" + request_id + ".json")

    def execute(self, action, request_id):
        path = self._path(request_id)
        if path.exists():
            return loads(path.read_text(encoding="utf-8"))["result"]
        before = self.observe()
        self.validate(action, before)
        candidate = self.candidate(action, before)
        result = {"ok": False, "outcome_unknown": True, **candidate}
        write_json(path, {"status": "executing", "result": result})
        self.output("Cartesian start (m/rad): " + str(candidate["initial_pose"]))
        self.output("Cartesian target (m/rad): " + str(candidate["target_pose"]))
        try:
            checked = getattr(self.backend, "move_cartesian_checked", None)
            receipt = checked(candidate) if callable(checked) else self.backend.move_pose(candidate["target_pose"])
            result["backend_receipt"] = receipt_evidence(receipt)
            measured = self.backend.state()
            actual = _vector(measured.pose, 6, "actual pose")
            result["actual_pose"] = actual
            self.output("Cartesian actual (m/rad): " + str(actual))
            tolerance = positive(self.config.get("arrival_tolerance_m", .002), "arrival_tolerance_m")
            if (not isinstance(receipt, dict) or receipt.get("ok") is not True
                    or receipt.get("execution_outcome") != "completed" or measured.fault
                    or measured.connected is not True or not pose_matches(candidate["target_pose"], actual, tolerance)):
                raise RuntimeError("Cartesian completion and actual pose were not verified")
            result.update(ok=True, outcome_unknown=False, execution_outcome="completed")
        except Exception as exc:
            result["error_type"] = type(exc).__name__
            self.output("Cartesian outcome unknown; no resend (" + type(exc).__name__ + ")")
        write_json(path, {"status": "completed" if result["ok"] else "unknown", "result": result})
        return result

    def reconcile(self, request_id):
        path = self._path(request_id)
        if path.exists():
            saved = loads(path.read_text(encoding="utf-8"))
            if saved["status"] == "completed":
                return {"status": "completed", "result": saved["result"]}
        return {"status": "unknown"}

    def tools(self):
        def locate(observation_id, target, view, pixel_uv, approach_offset_m=None):
            if observation_id not in self.frames:
                raise ValueError("Unknown snapshot")
            self._fresh(self.frames[observation_id])
            return self.grounder.resolve({"action": {"target": target, "grounding": {
                "view": view, "pixel_uv": pixel_uv, "approach_offset_m": approach_offset_m or [0, 0, 0]}}}, self.frames[observation_id])
        return [Tool("locate_calibrated_target", "Compute a calibrated target without performing motion.",
                     self.actions()[0]["parameters"], locate)]

    def close(self):
        try:
            self.provider.close()
        finally:
            self.backend.close()


def create(*, root, run_directory, config):
    backend = load_device(config["robot"], root=root)
    provider = None
    try:
        backend.connect()
        provider = load_device(config["rgbd"], root=root)
        return CartesianEnvironment(root, run_directory, backend, provider, config)
    except BaseException:
        if provider is not None:
            provider.close()
        backend.close()
        raise
