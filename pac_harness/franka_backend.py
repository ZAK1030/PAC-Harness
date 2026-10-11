"""FR3 Cartesian/7-joint motion using franky-control 2.x and libfranka.

franky owns the real-time trajectory loop. Python sends one absolute target,
joins with a bounded timeout, then checks measured O_T_EE/q and robot mode.
"""
from .robot_backends import RobotState, _vector
from .sdk_motion import CheckedSDKBackend, positive, pose_from_column_major, quaternion


class FrankaFR3SDKBackend(CheckedSDKBackend):
    def __init__(self, config, *, sdk=None, robot=None, **kwargs):
        super().__init__(config, name="franka_fr3", dof=7, **kwargs)
        if self.pose_reference != "tcp":
            raise ValueError("FR3 O_T_EE is the configured end-effector/TCP frame; pose_reference must be tcp")
        self.sdk, self.robot = sdk, robot
        self.hostname = config.get("hostname")
        if not isinstance(self.hostname, str) or not self.hostname:
            raise ValueError("FR3 hostname is required")
        self.factor = positive(config.get("relative_dynamics_factor", .05), "relative_dynamics_factor")
        if self.factor > 1:
            raise ValueError("relative_dynamics_factor must be <= 1")
        # franky installs these on connection: require explicit site-approved values.
        self.torque_threshold = positive(config.get("collision_torque_threshold_nm"), "collision_torque_threshold_nm")
        self.force_threshold = positive(config.get("collision_force_threshold_n"), "collision_force_threshold_n")

    def connect(self):
        if self._connected:
            return
        if self.sdk is None:
            import franky
            self.sdk = franky
        if self.robot is None:
            self.robot = self.sdk.Robot(self.hostname, relative_dynamics_factor=self.factor,
                default_torque_threshold=self.torque_threshold, default_force_threshold=self.force_threshold,
                realtime_config=self.sdk.RealtimeConfig.Enforce)
        self._connected = True
        try:
            self.robot.relative_dynamics_factor = self.factor
            self.state()  # No automatic error recovery, control start or motion.
        except BaseException:
            self.close()
            raise

    def close(self):
        try:
            if self._connected and self.robot.is_in_control:
                self._stop_native()
        finally:
            self._connected = False
            self.robot = None  # franky/libfranka releases its connection on destruction.

    def enable(self):
        if not self._connected or self._uncertain:
            raise RuntimeError("Connect and inspect any unknown outcome before enabling")
        current = self.state()
        if current.fault or not current.extra["idle"]:
            raise RuntimeError("FR3 must already be idle, fault-free and prepared in Desk/FCI")
        self.command_enabled = True  # Software permission only; never release hardware brakes.

    def state(self):
        if not self._connected:
            return RobotState(False, False)
        raw = self.robot.state
        pose = pose_from_column_major(raw.O_T_EE)  # Measured, never O_T_EE_d/c.
        joints = _vector(list(raw.q), 7, "measured q")
        stamp = positive(raw.time.to_sec(), "FR3 feedback time")
        mode = raw.robot_mode
        idle = mode == self.sdk.RobotMode.Idle and not self.robot.is_in_control
        normal = mode in (self.sdk.RobotMode.Idle, self.sdk.RobotMode.Move)
        fault = "FR3 controller fault or stopped/guiding/recovery mode" if self.robot.has_errors or not normal else None
        return RobotState(True, self.command_enabled and not fault, joints, pose, fault=fault,
            extra={**self.frame_metadata(), "feedback_stamps": [stamp], "idle": idle,
                   "robot_mode": str(mode), "controller_time_s": stamp})

    def _send(self, kind, target, deadline):
        if kind == "pose":
            motion = self.sdk.CartesianMotion(self.sdk.Affine(target[:3], quaternion(target[3:])),
                reference_type=self.sdk.ReferenceType.Absolute, relative_dynamics_factor=1.,
                return_when_finished=True)
        else:
            motion = self.sdk.JointMotion(target, reference_type=self.sdk.ReferenceType.Absolute,
                relative_dynamics_factor=1., return_when_finished=True)
        self.robot.move(motion, asynchronous=True)
        if self.robot.join_motion(timeout=max(0., deadline-self.clock())) is not True:
            raise TimeoutError("FR3 motion did not finish before timeout")

    def _stop_native(self):
        if self._connected:
            self.robot.stop()
            self.robot.join_motion(timeout=min(self.timeout, 1.))


def create(*, root, config):
    return FrankaFR3SDKBackend(config)
