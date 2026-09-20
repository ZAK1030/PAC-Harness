"""Common robot backend contract and optional Piper/FR3/ROKAE bridges.

The backends deliberately receive an already-created vendor driver.  This keeps
vendor SDK imports optional and makes connection, limits, and hardware tests
explicit at the scenario boundary.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Protocol


def _vector(value, size, name):
    if not isinstance(value, (list, tuple)) or len(value) != size:
        raise ValueError(f"{name} must contain {size} numbers")
    result = [float(x) for x in value]
    if not all(math.isfinite(x) for x in result):
        raise ValueError(f"{name} must contain finite numbers")
    return result


@dataclass(frozen=True)
class RobotLimits:
    """Conservative software limits; vendor controllers remain authoritative."""
    joint_min: tuple[float, ...]
    joint_max: tuple[float, ...]
    max_joint_speed: float = 1.0
    max_linear_speed: float = 0.25

    def check_joints(self, joints):
        values = _vector(joints, len(self.joint_min), "joints")
        if any(lo > value or value > hi for value, lo, hi in zip(values, self.joint_min, self.joint_max)):
            raise ValueError("joint target is outside configured software limits")
        return values

    def check_speed(self, speed, maximum, name):
        if speed is None:
            return None
        if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not math.isfinite(speed) or speed <= 0 or speed > maximum:
            raise ValueError(f"{name} must be > 0 and <= {maximum}")
        return float(speed)


@dataclass
class RobotState:
    connected: bool
    enabled: bool
    joints: list[float] | None = None
    pose: list[float] | None = None
    gripper_width: float | None = None
    fault: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class RobotBackend(Protocol):
    name: str
    dof: int
    def connect(self) -> None: ...
    def close(self) -> None: ...
    def enable(self) -> None: ...
    def stop(self) -> None: ...
    def state(self) -> RobotState: ...
    def move_joints(self, joints, *, speed=None) -> dict: ...
    def move_pose(self, pose, *, speed=None) -> dict: ...
    def open_gripper(self, width=None) -> dict: ...
    def close_gripper(self, width=None) -> dict: ...


class DriverBackend:
    """Common validation and duck-typed dispatch for a vendor driver."""
    def __init__(self, driver, *, name, dof, limits, require_enable=True):
        self.driver, self.name, self.dof, self.limits = driver, name, dof, limits
        self.require_enable = require_enable
        self._connected = False
        self._enabled = False

    def _call(self, names, *args, **kwargs):
        for name in names:
            method = getattr(self.driver, name, None)
            if callable(method):
                return method(*args, **kwargs)
        raise RuntimeError(f"{self.name} driver lacks any of: {', '.join(names)}")

    def connect(self):
        self._call(("connect", "ConnectPort", "initialize"))
        self._connected = True

    def close(self):
        if self._connected:
            try:
                self._call(("disconnect", "close", "ClosePort", "deinitialize"))
            finally:
                self._connected = self._enabled = False

    def enable(self):
        if not self._connected:
            raise RuntimeError("connect the robot before enabling it")
        self._call(("enable", "set_enabled", "setPowerState"), True)
        self._enabled = True

    def stop(self):
        self._call(("stop", "halt", "emergency_stop", "motion_abort"))
        self._enabled = False

    def _ready(self):
        if not self._connected or (self.require_enable and not self._enabled):
            raise RuntimeError("robot must be connected and enabled before motion")

    def move_joints(self, joints, *, speed=None):
        self._ready(); values = self.limits.check_joints(joints)
        speed = self.limits.check_speed(speed, self.limits.max_joint_speed, "joint speed")
        result = self._call(("move_joints", "moveJoint", "set_joint_positions", "move_joint_positions"), values, speed=speed)
        return {"ok": True, "backend": self.name, "kind": "joints", "joints": values, "result": result}

    def move_pose(self, pose, *, speed=None):
        self._ready(); values = _vector(pose, 6, "pose")
        speed = self.limits.check_speed(speed, self.limits.max_linear_speed, "linear speed")
        result = self._call(("move_pose", "moveL", "set_cartesian_pose", "move_cartesian"), values, speed=speed)
        return {"ok": True, "backend": self.name, "kind": "pose", "pose": values, "result": result}

    def open_gripper(self, width=None):
        self._ready(); result = self._call(("open_gripper", "gripper_open", "open"), width) if width is not None else self._call(("open_gripper", "gripper_open", "open"))
        return {"ok": True, "backend": self.name, "kind": "gripper_open", "result": result}

    def close_gripper(self, width=None):
        self._ready(); result = self._call(("close_gripper", "gripper_close", "close"), width) if width is not None else self._call(("close_gripper", "gripper_close", "close"))
        return {"ok": True, "backend": self.name, "kind": "gripper_close", "result": result}

    def state(self):
        raw = self._call(("state", "get_state", "read_state", "observe")) if self._connected else None
        if isinstance(raw, RobotState):
            return raw
        raw = raw if isinstance(raw, dict) else {}
        return RobotState(self._connected, self._enabled, raw.get("joints"), raw.get("pose"), raw.get("gripper_width"), raw.get("fault"), raw)


class RokaeBackend(DriverBackend):
    def __init__(self, driver, limits):
        super().__init__(driver, name="rokae", dof=6, limits=limits)


class PiperBackend(DriverBackend):
    def __init__(self, driver, limits):
        super().__init__(driver, name="piper", dof=6, limits=limits)


class FrankaFR3Backend(DriverBackend):
    def __init__(self, driver, limits):
        super().__init__(driver, name="franka_fr3", dof=7, limits=limits)


class SimRobotBackend(DriverBackend):
    """Deterministic backend for planner/tests; never touches hardware."""
    def __init__(self, dof=6, pose=None, joints=None):
        self._sim_state = {"joints": list(joints or [0.0] * dof), "pose": list(pose or [0.0] * 6), "gripper_width": 0.08}
        limits = RobotLimits(tuple([-math.pi] * dof), tuple([math.pi] * dof), 10.0, 10.0)
        super().__init__(self, name="sim", dof=dof, limits=limits, require_enable=False)
    def connect(self): self._connected = True
    def close(self): self._connected = self._enabled = False
    def enable(self): self._enabled = True
    def stop(self): self._enabled = False
    def state(self): return RobotState(self._connected, self._enabled, self._sim_state["joints"].copy(), self._sim_state["pose"].copy(), self._sim_state["gripper_width"])
    def move_joints(self, joints, *, speed=None): self._sim_state["joints"] = self.limits.check_joints(joints); return {"ok": True, "backend": "sim", "kind": "joints", "joints": self._sim_state["joints"]}
    def move_pose(self, pose, *, speed=None): self._sim_state["pose"] = _vector(pose, 6, "pose"); return {"ok": True, "backend": "sim", "kind": "pose", "pose": self._sim_state["pose"]}
    def open_gripper(self, width=None): self._sim_state["gripper_width"] = .08 if width is None else float(width); return {"ok": True, "backend": "sim", "width": self._sim_state["gripper_width"]}
    def close_gripper(self, width=None): self._sim_state["gripper_width"] = 0.0 if width is None else float(width); return {"ok": True, "backend": "sim", "width": self._sim_state["gripper_width"]}
