"""Client for the existing ROKAE receiver /joints and observation /call APIs.

No SDK import, process launching, automatic arming or automatic send retry.
"""
import math
import time
from urllib.request import Request, urlopen

from .robot_backends import RobotLimits, RobotState, _vector
from .storage import dumps, loads


class RokaeJointBridge:
    name, dof = "rokae_http", 6

    def __init__(self, config, *, request=None):
        self.action_url = config["action_url"].rstrip("/")
        self.observation_url = config["observation_url"].rstrip("/")
        lower = _vector(config["joint_min_rad"], 6, "joint_min_rad")
        upper = _vector(config["joint_max_rad"], 6, "joint_max_rad")
        if any(a >= b for a, b in zip(lower, upper)):
            raise ValueError("Invalid joint limits")
        self.limits = RobotLimits(tuple(lower), tuple(upper))
        self.request = request or self._http
        self.connected = False

    @staticmethod
    def _http(url, payload=None, timeout=5, *, packed=False):
        body = None
        if payload is not None:
            if packed:
                import msgpack
                body = msgpack.packb(payload, use_bin_type=True)
            else:
                body = dumps(payload).encode()
        request = Request(url, data=body,
                          headers={"Content-Type": "application/msgpack" if packed else "application/json",
                                   "Accept": "application/json"})
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
            if "msgpack" in response.headers.get("Content-Type", ""):
                import msgpack  # Optional for observation servers using MessagePack.
                return msgpack.unpackb(raw, raw=False)
            return loads(raw.decode("utf-8"))

    def _health(self):
        health = self.request(self.action_url + "/health")
        controller = health.get("controller", {})
        if (health.get("ok") is not True or health.get("execute") is not True
                or health.get("rearm_required") is True or controller.get("connected") is not True
                or controller.get("ok") is not True):
            raise RuntimeError("Receiver is unavailable or requires operator recovery")
        return health

    def connect(self):
        self._health()
        self.connected = True

    def close(self):
        self.connected = False  # Receiver lifecycle belongs to its local operator.

    def state(self):
        health = self._health()
        envelope = self.request(self.observation_url + "/call",
                                {"method": "hardware.read_state", "args": [], "kwargs": {}})
        if envelope.get("ok") is False:
            raise RuntimeError("Observation RPC failed")
        state = envelope.get("result", envelope)
        joints = _vector(state.get("joint_positions_rad"), 6, "joint feedback")
        # Timestamp originates on the robot host, not the client (which may be Linux).
        stamp = state.get("sampled_at_ns")
        if type(stamp) is not int or stamp <= 0:
            raise ValueError("Robot feedback requires sampled_at_ns")
        pose = state.get("pose", state.get("tcp_pose"))
        if pose is None:
            pose = state.get("flange_pose")
        pose = _vector(pose, 6, "robot pose")
        return RobotState(self.connected, health["controller"].get("armed") is True,
                          joints, pose, fault=None if state.get("operation_state") == "idle" else "robot not idle",
                          extra={**state, "sampled_at_ns": stamp, "operation_state": state.get("operation_state")})

    def move_cartesian_checked(self, candidate):
        """One bounded /action translation; never send a multi-step trajectory."""
        from .cartesian_motion import pose_matches, positive
        from .coordinates import resolve_pose_reference
        target = _vector(candidate["target_pose"], 6, "target pose")
        initial = _vector(candidate["initial_pose"], 6, "initial pose")
        delta = _vector(candidate["delta_m"], 3, "translation")
        mode = candidate["motion_mode"]
        if mode not in {"coarse", "fine"}:
            raise ValueError("Invalid motion mode")
        if not pose_matches([initial[i]+delta[i] for i in range(3)] + initial[3:], target, 1e-10, 1e-10):
            raise ValueError("Cartesian target must be the declared translation with unchanged orientation")
        before = self._health()
        controller = before["controller"]
        if before.get("heartbeat_required") is not False or controller.get("armed") is not True or controller.get("busy") is not False:
            raise RuntimeError("Receiver must be locally supervised, explicitly armed and idle")
        limits = controller.get("limits", {})
        if limits.get("translation_step_limits_enabled") is not False:
            per_axis = positive(limits.get("translation_per_axis_m"), "receiver axis limit")
            by_mode = limits.get("translation_norm_m_by_mode", {})
            norm = positive(by_mode.get(mode), "receiver mode limit")
            if max(abs(x) for x in delta) > per_axis + 1e-12 or math.dist(delta, [0, 0, 0]) > norm + 1e-12:
                raise ValueError("Translation exceeds receiver limits")
        speed = positive(limits.get("move_speed_mm_s"), "receiver linear speed")
        base = positive(limits.get("move_timeout_base_s"), "receiver timeout")
        factor = positive(limits.get("move_timeout_distance_factor"), "receiver timeout factor")
        timeout = max(base, 1000 * math.dist(delta, [0, 0, 0]) / speed * factor)
        source = self.state()
        if not source.enabled or source.fault or not pose_matches(initial, source.pose):
            raise RuntimeError("Robot moved since selected evidence or is not ready")
        if resolve_pose_reference({"pose_reference": "receiver_reported"}, source.extra) != candidate["pose_reference"]:
            raise RuntimeError("Receiver pose reference changed")
        old_sent = (controller.get("last_result") or {}).get("sent_at_ns", 0)
        stamp = max(time.monotonic_ns(), old_sent + 1 if type(old_sent) is int else 0)
        rows = [[*delta, 0., 0., 0.]] + [[0.] * 6 for _ in range(7)]
        reply = self.request(self.action_url + "/action", {
            "actions": rows, "state": source.pose, "motion_mode": mode, "fine_precision": False,
            "sent_at_ns": stamp, "observation_requested_at_ns": candidate["observation_stamp_ns"]},
            timeout=timeout+5, packed=True)
        after = self._health()
        current = after["controller"]
        instance = before.get("instance_id")
        if not isinstance(instance, str) or not instance or after.get("instance_id") != instance:
            raise RuntimeError("Receiver identity changed during translation")
        for field, increment in (("received", 1), ("executed", 1), ("rejected", 0)):
            if type(controller.get(field)) is not int or current.get(field) != controller[field] + increment:
                raise RuntimeError("Receiver counters do not match this command")
        if current.get("busy") is not False:
            raise RuntimeError("Receiver has not completed the command")
        for result in (reply, current.get("last_result")):
            if (not isinstance(result, dict) or result.get("ok") is not True or result.get("accepted") is not True
                    or result.get("execution_outcome") != "completed" or result.get("kind") != "action"
                    or result.get("status") != "completed" or result.get("sent_at_ns") != stamp
                    or result.get("request_id") != current["received"]
                    or result.get("instance_id", instance) != instance):
                raise RuntimeError("Cartesian completion receipt does not match request")
            received_target = _vector(result.get("target"), 6, "receiver Cartesian target")
            if not pose_matches(target, received_target, .002):
                raise RuntimeError("Receiver target does not match command")
        deadline = time.monotonic() + timeout
        while True:
            measured = self.state()
            if not measured.fault and pose_matches(target, measured.pose, .002):
                return {"ok": True, "execution_outcome": "completed", "actual_pose": measured.pose}
            if time.monotonic() >= deadline:
                raise TimeoutError("Cartesian feedback did not reach target")
            time.sleep(.05)

    def move_joints_checked(self, current, target):
        return self.move_joints(target, expected_start=current)

    def move_joints(self, joints, *, speed=None, expected_start=None):
        if speed is not None:
            raise ValueError("Joint speed is configured and enforced by the receiver")
        target = self.limits.check_joints(joints)
        before = self._health()
        controller = before["controller"]
        limits = controller.get("limits", {})
        if (controller.get("armed") is not True or controller.get("busy") is not False
                or limits.get("joint_commands_allowed") is not True):
            raise RuntimeError("Receiver must be explicitly armed, idle and support joint commands")
        if before.get("heartbeat_required") is not False:
            raise RuntimeError("This client requires the locally supervised receiver mode; heartbeat ownership is unsupported")
        if limits.get("workspace_enabled") is True:
            raise RuntimeError("Receiver joint motion requires FK workspace support")
        timeout, tolerance = limits.get("joint_timeout_s"), limits.get("joint_tolerance_rad")
        if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in (timeout, tolerance)):
            raise ValueError("Invalid receiver motion limits")
        source = self.state()
        if not source.enabled or source.fault:
            raise RuntimeError("Robot state is not ready")
        if expected_start is not None and max(abs(a-b) for a, b in zip(
                source.joints, _vector(expected_start, 6, "expected start"))) > .002:
            raise RuntimeError("Joint state changed before send; replan from current feedback")
        # Evidence age uses the robot-host timestamp; sent_at_ns is correlation.
        stamp = time.monotonic_ns()
        reply = self.request(self.action_url + "/joints", {
            "state": source.pose, "joint_positions_rad": source.joints,
            "target_joints_rad": target, "sent_at_ns": stamp,
            "observation_requested_at_ns": source.extra["sampled_at_ns"]}, timeout=timeout + 5)
        after = self._health()
        current = after["controller"]
        instance = before.get("instance_id")
        if not isinstance(instance, str) or not instance or after.get("instance_id") != instance:
            raise RuntimeError("Receiver identity changed during execution")
        for field, increment in (("received", 1), ("executed", 1), ("rejected", 0)):
            if type(controller.get(field)) is not int or current.get(field) != controller[field] + increment:
                raise RuntimeError("Receiver counters do not match this single command")
        if current.get("busy") is not False:
            raise RuntimeError("Receiver has not completed the command")
        for result in (reply, current.get("last_result")):
            if (not isinstance(result, dict) or result.get("ok") is not True or result.get("accepted") is not True
                    or result.get("execution_outcome") != "completed" or result.get("kind") != "joints"
                    or result.get("status") != "completed" or result.get("sent_at_ns") != stamp
                    or result.get("request_id") != current["received"]):
                raise RuntimeError("Completion receipt does not match this command")
            if result.get("instance_id", instance) != instance:
                raise RuntimeError("Receipt instance mismatch")
            received_target = _vector(result.get("target_joints_rad"), 6, "receipt target")
            actual = _vector(result.get("actual_joint_positions_rad"), 6, "receipt feedback")
            if max(abs(a-b) for a, b in zip(received_target, target)) > 1e-12 or max(abs(a-b) for a, b in zip(actual, target)) > tolerance:
                raise RuntimeError("Joint target or actual raw angles do not match")
        deadline = time.monotonic() + timeout
        while True:
            measured = self.state()
            if not measured.fault and max(abs(a-b) for a, b in zip(measured.joints, target)) <= tolerance:
                return {"ok": True, "execution_outcome": "completed", "actual_joint_positions_rad": measured.joints}
            if time.monotonic() >= deadline:
                raise TimeoutError("Joint feedback did not reach target")
            time.sleep(.05)


def create(*, root, config):
    return RokaeJointBridge(config)
