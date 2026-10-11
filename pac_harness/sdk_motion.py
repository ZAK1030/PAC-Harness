"""Checked, single-command motion shared by optional native SDK backends.

Request deduplication across restarts belongs to Cartesian/JointEnvironment.
This layer serializes commands and latches uncertain outcomes until the operator
has inspected the robot. It never retries a vendor motion or clears a fault.
"""
import math
import threading
import time
import uuid

from .coordinates import EULER_CONVENTION
from .robot_backends import RobotLimits, _vector


def positive(value, name):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return float(value)


def quaternion(euler):
    r, p, y = [x / 2 for x in euler]
    cr, sr, cp, sp, cy, sy = math.cos(r), math.sin(r), math.cos(p), math.sin(p), math.cos(y), math.sin(y)
    return [sr*cp*cy-cr*sp*sy, cr*sp*cy+sr*cp*sy, cr*cp*sy-sr*sp*cy, cr*cp*cy+sr*sp*sy]


def pose_close(a, b, translation=.0005, orientation=.001):
    qa, qb = quaternion(a[3:]), quaternion(b[3:])
    angle = 2 * math.acos(min(1., abs(sum(x*y for x, y in zip(qa, qb)))))
    return math.dist(a[:3], b[:3]) <= translation and angle <= orientation


def pose_from_column_major(matrix):
    """libfranka O_T_EE -> base-frame metres/extrinsic xyz radians."""
    values = _vector(list(matrix), 16, "O_T_EE")
    m = [[values[4*c+r] for c in range(4)] for r in range(4)]
    if any(abs(m[3][i] - [0, 0, 0, 1][i]) > 1e-6 for i in range(4)):
        raise ValueError("O_T_EE must be a homogeneous transform")
    rot = [row[:3] for row in m[:3]]
    for i in range(3):
        for j in range(3):
            if abs(sum(rot[k][i]*rot[k][j] for k in range(3)) - int(i == j)) > 1e-5:
                raise ValueError("O_T_EE rotation must be orthonormal")
    det = sum(rot[0][i] * (rot[1][(i+1)%3]*rot[2][(i+2)%3] - rot[1][(i+2)%3]*rot[2][(i+1)%3]) for i in range(3))
    if abs(det-1) > 1e-5:
        raise ValueError("O_T_EE rotation must have determinant +1")
    pitch = math.atan2(-rot[2][0], math.hypot(rot[0][0], rot[1][0]))
    if math.hypot(rot[0][0], rot[1][0]) > 1e-8:
        roll, yaw = math.atan2(rot[2][1], rot[2][2]), math.atan2(rot[1][0], rot[0][0])
    else:
        roll, yaw = math.atan2(-rot[1][2], rot[1][1]), 0.
    return [m[0][3], m[1][3], m[2][3], roll, pitch, yaw]


class CheckedSDKBackend:
    def __init__(self, config, *, name, dof, clock=time.monotonic, sleep=time.sleep):
        self.config, self.name, self.dof = dict(config), name, dof
        self.clock, self.sleep = clock, sleep
        lower = _vector(config.get("joint_min_rad"), dof, "joint_min_rad")
        upper = _vector(config.get("joint_max_rad"), dof, "joint_max_rad")
        if any(a >= b for a, b in zip(lower, upper)):
            raise ValueError("joint_min_rad must be lower than joint_max_rad")
        self.limits = RobotLimits(tuple(lower), tuple(upper))
        self.xyz_min = _vector(config.get("xyz_min_m"), 3, "xyz_min_m")
        self.xyz_max = _vector(config.get("xyz_max_m"), 3, "xyz_max_m")
        if any(a >= b for a, b in zip(self.xyz_min, self.xyz_max)):
            raise ValueError("xyz_min_m must be lower than xyz_max_m")
        self.pose_reference = config.get("pose_reference")
        if self.pose_reference not in {"tcp", "flange"}:
            raise ValueError("Declare SDK pose_reference as tcp or flange")
        self.command_enabled = config.get("command_enabled", False)
        if type(self.command_enabled) is not bool:
            raise ValueError("command_enabled must be boolean")
        self.timeout = positive(config.get("move_timeout_s", 15), "move_timeout_s")
        self.poll = positive(config.get("poll_interval_s", .02), "poll_interval_s")
        self.settle = positive(config.get("settle_time_s", .2), "settle_time_s")
        self.position_tolerance = positive(config.get("arrival_tolerance_m", .001), "arrival_tolerance_m")
        self.angle_tolerance = positive(config.get("orientation_tolerance_rad", .001), "orientation_tolerance_rad")
        self.joint_tolerance = positive(config.get("joint_tolerance_rad", .002), "joint_tolerance_rad")
        self.max_step = positive(config.get("max_cartesian_step_m", .05), "max_cartesian_step_m")
        self._connected, self._uncertain = False, False
        self._lock = threading.Lock()

    def _workspace(self, pose):
        if any(not lo <= x <= hi for x, lo, hi in zip(pose[:3], self.xyz_min, self.xyz_max)):
            raise ValueError("pose outside SDK backend workspace")

    def _ready(self, state, *, idle=False):
        if not self.command_enabled or not self._connected or state.connected is not True or state.enabled is not True or state.fault:
            raise RuntimeError("Robot must be connected, enabled and fault-free")
        if idle and state.extra.get("idle") is not True:
            raise RuntimeError("Robot is busy or controller completion is unavailable")
        self.limits.check_joints(state.joints)
        self._workspace(_vector(state.pose, 6, "measured pose"))

    def _execute(self, kind, target, expected):
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("Another SDK command is active")
        try:
            if not self.command_enabled or self._uncertain:
                raise RuntimeError("SDK commands disabled or previous outcome unknown; inspect robot")
            before = self.state()
            self._ready(before, idle=True)
            if kind == "pose":
                if not pose_close(before.pose, expected):
                    raise ValueError("Robot moved since target calculation")
                self._workspace(before.pose); self._workspace(target)
                if not pose_close([*before.pose[:3], *target[3:]], before.pose, 1e-9, self.angle_tolerance):
                    raise ValueError("Calibrated SDK motion must preserve orientation")
                distance = math.dist(before.pose[:3], target[:3])
                if not 1e-7 < distance <= self.max_step + 1e-9:
                    raise ValueError("Cartesian motion is zero or exceeds max_cartesian_step_m")
            elif max(abs(a-b) for a, b in zip(before.joints, expected)) > self.joint_tolerance:
                raise ValueError("Joints changed since target calculation")
            command_id = uuid.uuid4().hex
            deadline = self.clock() + self.timeout
            # Set before the first SDK write; exceptions cannot trigger replay.
            self._uncertain = True
            try:
                self._send(kind, target, deadline)
                stable_since, last_stamps = None, before.extra["feedback_stamps"]
                while self.clock() < deadline:
                    measured = self.state()
                    self._ready(measured)
                    stamps = measured.extra["feedback_stamps"]
                    advanced = all(a > b for a, b in zip(stamps, last_stamps))
                    arrived = (pose_close(measured.pose, target, self.position_tolerance, self.angle_tolerance)
                               if kind == "pose" else max(abs(a-b) for a, b in zip(measured.joints, target)) <= self.joint_tolerance)
                    if advanced:
                        last_stamps = stamps
                        if arrived and self._completion_ready(measured) and self._arrival_evidence(before, measured, kind, target):
                            stable_since = self.clock() if stable_since is None else stable_since
                            if self.clock()-stable_since >= self.settle:
                                self._uncertain = False
                                return {"ok": True, "execution_outcome": "completed", "backend": self.name,
                                        "command_id": command_id, "kind": kind, "target": target,
                                        "actual_pose": measured.pose, "actual_joint_positions_rad": measured.joints,
                                        "completion_evidence": "SDK completion and advancing measured feedback"}
                        else:
                            stable_since = None
                    self.sleep(self.poll)
                raise TimeoutError("SDK completion/arrival feedback timed out")
            except BaseException:
                self.command_enabled = False
                try:
                    self._stop_native()
                except Exception:
                    pass  # Outcome remains unknown, including a failed stop.
                raise
        finally:
            self._lock.release()

    def move_cartesian_checked(self, candidate):
        if candidate.get("pose_reference") != self.pose_reference:
            raise ValueError("Candidate and SDK pose references disagree")
        start = _vector(candidate.get("initial_pose"), 6, "initial_pose")
        target = _vector(candidate.get("target_pose"), 6, "target_pose")
        delta = _vector(candidate.get("delta_m"), 3, "delta_m")
        if any(abs(start[i]+delta[i]-target[i]) > 1e-8 for i in range(3)):
            raise ValueError("Candidate delta and absolute target disagree")
        return self._execute("pose", target, start)

    def _completion_ready(self, state):
        return state.extra.get("idle") is True

    def _arrival_evidence(self, before, measured, kind, target):
        return True

    def move_pose(self, pose, *, speed=None):
        if speed is not None:
            raise ValueError("Configure native SDK speed; metres/second cannot be mapped to a percentage")
        return self._execute("pose", _vector(pose, 6, "pose"), self.state().pose)

    def move_joints_checked(self, current, target):
        return self._execute("joints", self.limits.check_joints(target), _vector(current, self.dof, "current joints"))

    def move_joints(self, joints, *, speed=None):
        if speed is not None:
            raise ValueError("Configure native SDK dynamics instead of an unverified radians/second mapping")
        return self.move_joints_checked(self.state().joints, joints)

    def stop(self):
        self.command_enabled = False
        self._stop_native()

    def open_gripper(self, width=None):
        raise NotImplementedError("Gripper requires its own device adapter")

    def close_gripper(self, width=None):
        raise NotImplementedError("Gripper requires its own device adapter")

    def frame_metadata(self):
        return {"pose_frame": "T_robot_base_from_" + self.pose_reference, "euler_convention": EULER_CONVENTION}
