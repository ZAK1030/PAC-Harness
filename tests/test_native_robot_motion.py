"""Vendor-shaped doubles: no robot connection or installed SDK required."""
import math
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from pac_harness.franka_backend import FrankaFR3SDKBackend
from pac_harness.piper_backend import PiperSDKBackend
from pac_harness.sdk_motion import pose_close, pose_from_column_major, quaternion


def settings(dof=6, **extra):
    return {"joint_min_rad": [-3]*dof, "joint_max_rad": [3]*dof,
            "xyz_min_m": [-1, -1, .1], "xyz_max_m": [1, 1, 1], "pose_reference": "tcp",
            "command_enabled": True, "move_timeout_s": .3, "poll_interval_s": .01,
            "settle_time_s": .02, "hostname": "fake-fr3", "collision_torque_threshold_nm": 10,
            "collision_force_threshold_n": 15, **extra}


class Clock:
    t = 0.
    def now(self): return self.t
    def sleep(self, amount): self.t += amount


class PiperSDK:
    def __init__(self):
        self.calls = []; self.pose = [0, 0, 300000, 0, 0, 0]; self.q = [0]*6
        self.stamp = time.time()-.001
        self.freeze = False; self.arrive = True; self.fault = False; self.enabled = True
        self.mode = 1; self.mode_ack = True; self.loss = False; self.motion_status = 0
    def ConnectPort(self, **kwargs): self.calls.append(("connect", kwargs))
    def DisconnectPort(self): self.calls.append(("disconnect",))
    def EnableArm(self, mask): self.calls.append(("enable", mask)); self.enabled = True
    def GetArmStatus(self):
        if not self.freeze: self.stamp = time.time()-.001
        return NS(time_stamp=self.stamp, arm_status=NS(arm_status=int(self.fault), err_code=0,
            ctrl_mode=1, mode_feed=self.mode, motion_status=self.motion_status))
    def GetArmEndPoseMsgs(self):
        return NS(time_stamp=self.stamp, end_pose=NS(**dict(zip(
            ("X_axis", "Y_axis", "Z_axis", "RX_axis", "RY_axis", "RZ_axis"), self.pose))))
    def GetArmJointMsgs(self):
        return NS(time_stamp=self.stamp, joint_state=NS(**{f"joint_{i+1}": q for i, q in enumerate(self.q)}))
    def GetArmLowSpdInfoMsgs(self):
        fields = {key: False for key in ("voltage_too_low", "motor_overheating", "driver_overcurrent",
                  "driver_overheating", "collision_status", "driver_error_status", "stall_status")}
        fields["driver_enable_status"] = self.enabled
        return NS(time_stamp=self.stamp, **{f"motor_{i}": NS(foc_status=NS(**fields)) for i in range(1, 7)})
    def MotionCtrl_2(self, **kwargs):
        self.calls.append(("mode", kwargs))
        if self.mode_ack: self.mode = kwargs["move_mode"]
    def EndPoseCtrl(self, *target):
        self.calls.append(("pose", target))
        if self.arrive: self.pose = list(target)
        if self.loss: raise TimeoutError("CAN send outcome unknown")
    def JointCtrl(self, *target): self.calls.append(("joints", target)); self.q = list(target)
    def MotionCtrl_1(self, **kwargs): self.calls.append(("stop", kwargs))


def matrix(pose):
    x, y, z, w = quaternion(pose[3:])
    rotation = [[1-2*(y*y+z*z), 2*(x*y-z*w), 2*(x*z+y*w)],
                [2*(x*y+z*w), 1-2*(x*x+z*z), 2*(y*z-x*w)],
                [2*(x*z-y*w), 2*(y*z+x*w), 1-2*(x*x+y*y)]]
    rows = [[*rotation[i], pose[i]] for i in range(3)] + [[0, 0, 0, 1]]
    return [rows[r][c] for c in range(4) for r in range(4)]


class FrankySDK:
    RobotMode = NS(Idle="idle", Move="move", Reflex="reflex")
    ReferenceType = NS(Absolute="absolute")
    RealtimeConfig = NS(Enforce="enforce")
    def __init__(self, robot): self.robot = robot; self.connect_calls = []
    def Robot(self, host, **kwargs): self.connect_calls.append((host, kwargs)); return self.robot
    def Affine(self, translation, quat): return NS(translation=translation, quaternion=quat)
    def CartesianMotion(self, target, **kwargs): return NS(kind="pose", target=target, settings=kwargs)
    def JointMotion(self, target, **kwargs): return NS(kind="joints", target=target, settings=kwargs)


class FrankyRobot:
    def __init__(self):
        self.pose = [0, 0, .3, 0, 0, 0]; self.q = [0]*7; self.calls = []
        self.is_in_control = False; self.has_errors = False; self.joined = True; self.arrive = True
        self.freeze = False; self.stamp = 1.; self.mode = "idle"; self.loss = False
    @property
    def state(self):
        if not self.freeze: self.stamp += .001
        # Desired position deliberately differs: adapter must use measured O_T_EE/q.
        return NS(O_T_EE=matrix(self.pose), O_T_EE_d=matrix([0, 0, .35, 0, 0, 0]),
            q=self.q.copy(), robot_mode=self.mode, time=NS(to_sec=lambda: self.stamp))
    def move(self, motion, **kwargs):
        self.calls.append(("move", motion, kwargs)); self.is_in_control = True
        if self.arrive:
            if motion.kind == "pose": self.pose[:3] = motion.target.translation
            else: self.q = list(motion.target)
        if self.loss: raise TimeoutError("Move outcome unknown")
    def join_motion(self, timeout):
        self.calls.append(("join", timeout))
        if self.joined: self.is_in_control = False
        return self.joined
    def stop(self): self.calls.append(("stop",)); self.is_in_control = False


def candidate(backend, dz=.05):
    start = backend.state().pose
    return {"initial_pose": start, "target_pose": [start[0], start[1], start[2]+dz, *start[3:]],
            "delta_m": [0, 0, dz], "pose_reference": "tcp"}


class NativeMotionTests(unittest.TestCase):
    def backend(self, vendor):
        clock = Clock()
        if vendor == "piper":
            sdk = PiperSDK()
            backend = PiperSDKBackend(settings(), sdk=sdk, clock=clock.now, sleep=clock.sleep)
        else:
            sdk = FrankyRobot()
            backend = FrankaFR3SDKBackend(settings(7), sdk=FrankySDK(sdk), clock=clock.now, sleep=clock.sleep)
        backend.connect(); self.addCleanup(backend.close)
        return backend, sdk

    def test_piper_units_linear_command_and_measured_completion(self):
        backend, sdk = self.backend("piper")
        sdk.pose[3:] = [30000, -20000, 90000]
        motion = candidate(backend)
        receipt = backend.move_cartesian_checked(motion)
        self.assertEqual(receipt["execution_outcome"], "completed")
        self.assertEqual([c for c in sdk.calls if c[0] == 'pose'], [("pose", (0, 0, 350000, 30000, -20000, 90000))])
        self.assertEqual([c for c in sdk.calls if c[0] == 'mode'][0][1]["move_mode"], 2)
        self.assertAlmostEqual(receipt["actual_pose"][5], math.pi/2)
        self.assertEqual(sdk.calls[0], ("connect", {"piper_init": False}))
        self.assertFalse(any(c[0] == "enable" for c in sdk.calls))

    def test_fr3_absolute_quaternion_join_and_measured_completion(self):
        backend, robot = self.backend("fr3")
        robot.pose[3:] = [.3, -.2, 1.1]
        motion = candidate(backend)
        receipt = backend.move_cartesian_checked(motion)
        self.assertTrue(receipt["ok"])
        move = next(c for c in robot.calls if c[0] == "move")
        self.assertEqual(move[2], {"asynchronous": True})
        self.assertEqual(move[1].settings["reference_type"], "absolute")
        self.assertEqual(move[1].settings["relative_dynamics_factor"], 1.)  # Not factor squared.
        self.assertEqual(move[1].target.quaternion, quaternion(motion["target_pose"][3:]))
        self.assertTrue(any(c[0] == "join" and 0 < c[1] <= .3 for c in robot.calls))
        args = backend.sdk.connect_calls[0][1]
        self.assertEqual(args["realtime_config"], "enforce")
        self.assertEqual(args["default_force_threshold"], 15)

    def test_both_joint_counts_and_partial_target_keeps_other_joints(self):
        from pac_harness.joint_motion import joint_target
        for vendor in ("piper", "fr3"):
            with self.subTest(vendor=vendor):
                backend, sdk = self.backend(vendor)
                if vendor == "piper": sdk.q = [1000]*6
                else: sdk.q = [.1]*7
                current = backend.state().joints
                target = joint_target(current, {"mode": "relative", "unit": "deg", "joints": {"J6": 10}})
                result = backend.move_joints_checked(current, target)
                self.assertTrue(result["ok"])
                self.assertEqual(len(result["actual_joint_positions_rad"]), backend.dof)
                self.assertAlmostEqual(result["actual_joint_positions_rad"][0], current[0])
                if vendor == "fr3": self.assertEqual(result["actual_joint_positions_rad"][6], current[6])

    def test_nonarrival_frozen_feedback_or_failed_join_never_succeeds(self):
        for vendor in ("piper", "fr3"):
            for failure in ("arrive", "freeze", "join"):
                if vendor == "piper" and failure == "join": continue
                with self.subTest(vendor=vendor, failure=failure):
                    backend, sdk = self.backend(vendor)
                    motion = candidate(backend)
                    if failure == "freeze": sdk.freeze = True
                    elif failure == "join": sdk.joined = False
                    else: sdk.arrive = False
                    with self.assertRaises(TimeoutError): backend.move_cartesian_checked(motion)
                    self.assertTrue(backend._uncertain)
                    self.assertFalse(backend.command_enabled)
                    self.assertTrue(any(c[0] == "stop" for c in sdk.calls))
                    with self.assertRaises(RuntimeError): backend.move_cartesian_checked(motion)
                    self.assertEqual(len([c for c in sdk.calls if c[0] in ("pose", "move")]), 1)

    def test_no_send_for_changed_pose_reference_workspace_or_disabled_commands(self):
        for vendor in ("piper", "fr3"):
            for bad in ("changed", "reference", "workspace", "disabled", "orientation", "step", "delta"):
                with self.subTest(vendor=vendor, bad=bad):
                    backend, sdk = self.backend(vendor)
                    motion = candidate(backend)
                    if bad == "changed": motion["initial_pose"][0] += .01
                    if bad == "reference": motion["pose_reference"] = "flange"
                    if bad == "workspace": backend.xyz_max[2] = .31
                    if bad == "disabled": backend.command_enabled = False
                    if bad == "orientation": motion["target_pose"][5] = .1
                    if bad == "step": motion["target_pose"][2] += .1; motion["delta_m"][2] += .1
                    if bad == "delta": motion["delta_m"][0] = .01
                    with self.assertRaises((RuntimeError, ValueError)): backend.move_cartesian_checked(motion)
                    self.assertFalse(any(c[0] in ("pose", "move") for c in sdk.calls))

    def test_fault_and_piper_mode_not_acknowledged_are_not_completion(self):
        backend, sdk = self.backend("piper")
        sdk.mode_ack = False
        with self.assertRaises(TimeoutError): backend.move_cartesian_checked(candidate(backend))
        backend, robot = self.backend("fr3")
        robot.mode = "reflex"
        with self.assertRaises(RuntimeError): backend.move_cartesian_checked(candidate(backend))
        self.assertFalse(any(c[0] == "move" for c in robot.calls))

    def test_no_automatic_motion_permission_and_no_implicit_recovery(self):
        for vendor in ("piper", "fr3"):
            backend, sdk = self.backend(vendor)
            backend.command_enabled = False
            self.assertFalse(any(c[0] in ("pose", "move", "enable", "stop") for c in sdk.calls))
            if vendor == "piper": sdk.enabled = False
            backend.enable()
            self.assertTrue(backend.command_enabled)
            self.assertTrue(backend.state().enabled)

    def test_piper_stale_or_missing_initial_feedback_refused(self):
        sdk = PiperSDK(); sdk.freeze = True; sdk.stamp = 0
        clock = Clock()
        backend = PiperSDKBackend(settings(connect_timeout_s=.02), sdk=sdk, clock=clock.now, sleep=clock.sleep)
        with self.assertRaises(RuntimeError): backend.connect()
        self.assertFalse(backend._connected)
        self.assertEqual(sdk.calls[-1], ("disconnect",))

    def test_piper_ignored_small_command_cannot_borrow_previous_arrived_bit(self):
        backend, sdk = self.backend("piper")
        sdk.mode = 2; sdk.arrive = False
        # Target is inside positional tolerance of the old position. Fresh
        # timestamps plus old MOVE L/arrived status still do not prove motion.
        with self.assertRaises(TimeoutError): backend.move_cartesian_checked(candidate(backend, .0005))
        self.assertEqual(len([c for c in sdk.calls if c[0] == "pose"]), 1)

    def test_stop_does_not_resume_and_unknown_cannot_be_enabled(self):
        for vendor in ("piper", "fr3"):
            with self.subTest(vendor=vendor):
                backend, sdk = self.backend(vendor)
                sdk.loss = True
                with self.assertRaises(TimeoutError): backend.move_cartesian_checked(candidate(backend))
                with self.assertRaises(RuntimeError): backend.enable()
                if vendor == "piper":
                    stops = [c for c in sdk.calls if c[0] == "stop"]
                    self.assertEqual(stops, [("stop", {"emergency_stop": 1})])

    def test_rotation_matrix_roundtrip_and_invalid_transform(self):
        for rpy in ([.3, -.5, 1.2], [0, math.pi/2, .7], [.2, -math.pi/2, -.6]):
            pose = [.1, .2, .3, *rpy]
            self.assertTrue(pose_close(pose, pose_from_column_major(matrix(pose)), 1e-8, 1e-6))
        bad = matrix([0]*6); bad[0] = 2
        with self.assertRaises(ValueError): pose_from_column_major(bad)

    def test_explicit_config_limits_and_speed_mapping(self):
        for bad in (settings(joint_max_rad=[-3]*6), settings(pose_reference="guess"), settings(speed_percent=0)):
            with self.assertRaises(ValueError): PiperSDKBackend(bad)
        with self.assertRaises(ValueError): FrankaFR3SDKBackend(settings(7, collision_force_threshold_n=None))
        backend, _ = self.backend("piper")
        with self.assertRaises(ValueError): backend.move_pose([0, 0, .35, 0, 0, 0], speed=.1)


class NativeCalibratedIntegrationTests(unittest.TestCase):
    def test_both_backends_calibration_then_correction_and_restart_no_replay(self):
        try:
            from examples.calibrated_motion import create
        except ImportError:
            self.skipTest("Install robot-vision for RGB-D integration")
        for vendor in ("piper", "fr3"):
            with self.subTest(vendor=vendor), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                env = create(root=root, run_directory=root/'run', config={})
                env.backend.close(); clock = Clock()
                if vendor == "piper":
                    sdk = PiperSDK(); backend = PiperSDKBackend(settings(), sdk=sdk, clock=clock.now, sleep=clock.sleep)
                else:
                    sdk = FrankyRobot(); backend = FrankaFR3SDKBackend(settings(7), sdk=FrankySDK(sdk), clock=clock.now, sleep=clock.sleep)
                backend.connect(); env.backend = backend; env.output = lambda _: None
                try:
                    first = env.observe()
                    checked = backend.move_cartesian_checked
                    def checked_with_opaque_sdk_result(value):
                        reply = checked(value)
                        reply["result"] = object()
                        return reply
                    backend.move_cartesian_checked = checked_with_opaque_sdk_result
                    action = {"name": "move_calibrated", "arguments": {"observation_id": first["observation_id"],
                        "target": "surface", "view": "overview", "pixel_uv": [4, 4]}}
                    result = env.execute(action, "coarse")
                    self.assertTrue(result["ok"]); self.assertAlmostEqual(result["actual_pose"][2], .35)
                    self.assertEqual(result["backend_receipt"]["backend"], backend.name)
                    self.assertNotIn("result", result["backend_receipt"])
                    frame = env.observe()
                    fine = env.execute({"name": "move_relative", "arguments": {"observation_id": frame["observation_id"],
                        "target": "surface", "delta_m": [.001, 0, 0]}}, "fine")
                    self.assertTrue(fine["ok"]); self.assertAlmostEqual(fine["actual_pose"][0], .001)
                    frame = env.observe(); sdk.loss = True
                    lost_action = {"name": "move_calibrated", "arguments": {**action["arguments"], "observation_id": frame["observation_id"]}}
                    unknown = env.execute(lost_action, "lost")
                    self.assertTrue(unknown["outcome_unknown"])
                    before = len([c for c in sdk.calls if c[0] in ("pose", "move")])
                    # A newly created environment loses snapshots but consults disk before dispatch.
                    restarted = create(root=root, run_directory=root/'run', config={})
                    restarted.backend.close(); restarted.backend = backend; restarted.output = lambda _: None
                    self.assertEqual(restarted.execute(lost_action, "lost"), unknown)
                    self.assertEqual(restarted.reconcile("lost")["status"], "unknown")
                    self.assertEqual(len([c for c in sdk.calls if c[0] in ("pose", "move")]), before)
                    restarted.close()
                finally:
                    env.close()
