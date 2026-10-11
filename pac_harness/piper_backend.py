"""Native piper_sdk 0.6.2 / C_PiperInterface_V2 adapter (SocketCAN).

CAN has no motion request ID. Use one exclusive controller; completion is based
on fresh status/motor/pose/joint feedback, never on EndPoseCtrl's return value.
"""
from copy import deepcopy
import math
import time

from .robot_backends import RobotState
from .sdk_motion import CheckedSDKBackend, positive


class PiperSDKBackend(CheckedSDKBackend):
    def __init__(self, config, *, sdk=None, **kwargs):
        super().__init__(config, name="piper", dof=6, **kwargs)
        self.sdk = sdk
        self.speed_percent = config.get("speed_percent", 10)
        if type(self.speed_percent) is not int or not 1 <= self.speed_percent <= 100:
            raise ValueError("speed_percent must be an integer from 1 to 100")
        self.max_age = positive(config.get("max_feedback_age_s", .5), "max_feedback_age_s")
        self.can_name = config.get("can_name", "can0")
        if not isinstance(self.can_name, str) or not self.can_name:
            raise ValueError("can_name must be a nonempty interface name")

    def connect(self):
        if self._connected:
            return
        if self.sdk is None:
            from piper_sdk import C_PiperInterface_V2
            self.sdk = C_PiperInterface_V2(can_name=self.can_name, judge_flag=True,
                can_auto_init=True, start_sdk_joint_limit=False, start_sdk_gripper_limit=False)
        # Skip SDK robot initialization writes; only open CAN/read threads.
        self.sdk.ConnectPort(piper_init=False)
        self._connected = True
        deadline = self.clock() + positive(self.config.get("connect_timeout_s", 3), "connect_timeout_s")
        try:
            while True:
                try:
                    self.state()
                    return
                except (RuntimeError, ValueError):
                    if self.clock() >= deadline:
                        raise
                    self.sleep(self.poll)
        except BaseException:
            self.close()
            raise

    def close(self):
        if self._connected:
            try:
                self.sdk.DisconnectPort()
            finally:
                self._connected = False

    def enable(self):
        if not self._connected or self._uncertain:
            raise RuntimeError("Connect and inspect any unknown outcome before enabling")
        current = self.state()
        if current.fault or current.extra["idle"] is not True:
            raise RuntimeError("Piper must be idle and fault-free before explicit enable")
        self.sdk.EnableArm(7)  # Explicit user/application call, never from connect.
        deadline = self.clock() + self.timeout
        while self.clock() < deadline:
            if self.state().enabled:
                self.command_enabled = True
                return
            self.sleep(self.poll)
        raise TimeoutError("Piper enable feedback timed out")

    def state(self):
        if not self._connected:
            return RobotState(False, False)
        status = deepcopy(self.sdk.GetArmStatus())
        pose = deepcopy(self.sdk.GetArmEndPoseMsgs())
        joints = deepcopy(self.sdk.GetArmJointMsgs())
        motors = deepcopy(self.sdk.GetArmLowSpdInfoMsgs())
        stamps = [x.time_stamp for x in (status, pose, joints, motors)]
        now = time.time()
        if any(type(s) not in (int, float) or not math.isfinite(s) or not 0 <= now-s <= self.max_age for s in stamps):
            raise RuntimeError("Piper feedback missing, stale or clock invalid")
        p = pose.end_pose
        xyz = [getattr(p, k) / 1_000_000 for k in ("X_axis", "Y_axis", "Z_axis")]
        rpy = [math.radians(getattr(p, k) / 1000) for k in ("RX_axis", "RY_axis", "RZ_axis")]
        q = [math.radians(getattr(joints.joint_state, f"joint_{i}") / 1000) for i in range(1, 7)]
        s = status.arm_status
        fault = f"Piper arm_status={int(s.arm_status)}, err_code={int(s.err_code)}" if int(s.arm_status) or int(s.err_code) else None
        enabled = True
        motor_faults = ("voltage_too_low", "motor_overheating", "driver_overcurrent", "driver_overheating",
                        "collision_status", "driver_error_status", "stall_status")
        for i in range(1, 7):
            motor = getattr(motors, f"motor_{i}")
            flags = motor.foc_status
            enabled = enabled and flags.driver_enable_status is True
            if any(getattr(flags, key) for key in motor_faults):
                fault = f"Piper motor {i} protection/fault"
        return RobotState(True, enabled, q, xyz+rpy, fault=fault, extra={**self.frame_metadata(),
            "feedback_stamps": stamps, "idle": int(s.motion_status) == 0,
            "ctrl_mode": int(s.ctrl_mode), "mode_feed": int(s.mode_feed), "sampled_at_ns": int(min(stamps)*1e9)})

    def _send(self, kind, target, deadline):
        mode = 0x02 if kind == "pose" else 0x01  # MOVE L / MOVE J, not MOVE P.
        self.sdk.MotionCtrl_2(ctrl_mode=0x01, move_mode=mode, move_spd_rate_ctrl=self.speed_percent, is_mit_mode=0)
        if kind == "pose":
            values = [round(x*1_000_000) for x in target[:3]] + [round(math.degrees(x)*1000) for x in target[3:]]
            self.sdk.EndPoseCtrl(*values)
        else:
            self.sdk.JointCtrl(*[round(math.degrees(x)*1000) for x in target])
        self._expected_mode = mode

    def _ready(self, state, *, idle=False):
        super()._ready(state, idle=idle)
        if state.extra["ctrl_mode"] not in (0, 1):
            raise RuntimeError("Piper is controlled in a different mode")

    def _completion_ready(self, state):
        return (super()._completion_ready(state) and state.extra["ctrl_mode"] == 1
                and state.extra["mode_feed"] == self._expected_mode)

    def _arrival_evidence(self, before, measured, kind, target):
        # CAN lacks request IDs: a previously cached "arrived" bit must not
        # certify an ignored small command already inside the arrival tolerance.
        if kind == "pose":
            requested = math.dist(before.pose[:3], target[:3])
            return math.dist(before.pose[:3], measured.pose[:3]) >= min(requested/2, 2e-6)
        requested = max(abs(a-b) for a, b in zip(before.joints, target))
        return (requested == 0 or max(abs(a-b) for a, b in zip(before.joints, measured.joints))
                >= min(requested/2, math.radians(.002)))

    def _stop_native(self):
        if self._connected:
            self.sdk.MotionCtrl_1(emergency_stop=0x01)  # Never issue resume/reset.


def create(*, root, config):
    return PiperSDKBackend(config)
