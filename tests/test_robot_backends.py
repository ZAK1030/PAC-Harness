import unittest

from pac_harness.robot_backends import (
    FrankaFR3Backend, PiperBackend, RobotLimits, SimRobotBackend,
)


class Driver:
    def __init__(self): self.calls = []
    def connect(self): self.calls.append(("connect",))
    def disconnect(self): self.calls.append(("disconnect",))
    def enable(self, value): self.calls.append(("enable", value))
    def stop(self): self.calls.append(("stop",))
    def move_joints(self, joints, speed=None): self.calls.append(("joints", joints, speed)); return "receipt"
    def move_pose(self, pose, speed=None): self.calls.append(("pose", pose, speed)); return "receipt"
    def open_gripper(self): self.calls.append(("open",))
    def close_gripper(self): self.calls.append(("close",))
    def state(self): return {"joints": [0] * 7, "pose": [0] * 6}


class BackendTests(unittest.TestCase):
    def test_fr3_requires_seven_joints_and_enable(self):
        driver = Driver()
        backend = FrankaFR3Backend(driver, RobotLimits(tuple([-3] * 7), tuple([3] * 7)))
        with self.assertRaises(RuntimeError): backend.move_joints([0] * 7)
        backend.connect(); backend.enable()
        receipt = backend.move_joints([0] * 7, speed=.5)
        self.assertTrue(receipt["ok"])
        self.assertEqual(driver.calls[-1][0], "joints")
        with self.assertRaises(ValueError): backend.move_joints([0] * 6)
        backend.stop(); backend.close()

    def test_piper_speed_and_pose_limits(self):
        driver = Driver(); limits = RobotLimits(tuple([-2] * 6), tuple([2] * 6), .8, .2)
        backend = PiperBackend(driver, limits); backend.connect(); backend.enable()
        with self.assertRaises(ValueError): backend.move_pose([0] * 6, speed=.3)
        backend.move_pose([0] * 6, speed=.1)
        self.assertEqual(backend.state().connected, True)

    def test_simulator_never_needs_vendor_driver(self):
        backend = SimRobotBackend(6); backend.connect(); backend.enable()
        backend.move_joints([.1] * 6); backend.move_pose([.1] * 6)
        self.assertEqual(backend.state().joints, [.1] * 6)
        self.assertEqual(backend.state().pose, [.1] * 6)

