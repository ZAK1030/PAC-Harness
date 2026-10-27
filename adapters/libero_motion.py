"""Native MuJoCo RGB-D calibration and physical OSC Cartesian execution.

No object poses or segmentation are exposed to the policy. The fixed and wrist
camera transforms come from the simulator; targets come only from RGB pixels
and optical-axis depth. All public Cartesian poses use the robot-base TCP.
"""
import math
import time
import uuid
import xml.etree.ElementTree as ET

import numpy as np

from pac_harness.cartesian_motion import CartesianEnvironment, positive
from pac_harness.rgbd import Observation
from pac_harness.robot_backends import RobotState
from pac_harness.storage import write_json

VIEWS = ("agentview", "robot0_eye_in_hand")


def pixel_center_rendering(xml):
    """Disable MSAA depth subsamples so depth refers to RGB pixel centres.

    MuJoCo's default four-sample resolve chooses a depth subsample rather than
    the pixel centre. That introduces millimetre biases on slanted surfaces.
    Only render quality changes; physics and official success are unchanged.
    """
    model = ET.fromstring(xml)
    visual = model.find("visual")
    if visual is None:
        visual = ET.SubElement(model, "visual")
    quality = visual.find("quality")
    if quality is None:
        quality = ET.SubElement(visual, "quality")
    quality.set("offsamples", "0")
    return ET.tostring(model, encoding="unicode")


def transform(position, rotation):
    result = np.eye(4)
    result[:3, :3] = np.asarray(rotation).reshape(3, 3)
    result[:3, 3] = position
    return result


def rotation_xyz(angles):
    # Explicit extrinsic xyz, matching Grounder and mat2euler(..., 'sxyz').
    # robosuite 1.4's euler2mat uses a different convention.
    roll, pitch, yaw = angles
    cr, sr, cp, sp, cy, sy = math.cos(roll), math.sin(roll), math.cos(pitch), math.sin(pitch), math.cos(yaw), math.sin(yaw)
    return np.array([[cy, -sy, 0], [sy, cy, 0], [0, 0, 1]]) @ np.array(
        [[cp, 0, sp], [0, 1, 0], [-sp, 0, cp]]) @ np.array(
        [[1, 0, 0], [0, cr, -sr], [0, sr, cr]])


def camera_intrinsics(sim, view, height, width):
    index = sim.model.camera_name2id(view)
    focal = .5 * height / np.tan(sim.model.cam_fovy[index] * np.pi / 360)
    # Pixel indices address centres (0 .. width-1), not raster boundaries.
    return np.array([[focal, 0, (width-1)/2], [0, focal, (height-1)/2], [0, 0, 1]])


def world_camera(sim, view):
    index = sim.model.camera_name2id(view)
    # MuJoCo camera: x right, y up, -z forward; CV: x right, y down, z forward.
    return transform(sim.data.cam_xpos[index], sim.data.cam_xmat[index]) @ np.diag([1, -1, -1, 1])


def metric_depth(sim, buffer):
    depth = np.asarray(buffer, dtype=float)
    if not np.isfinite(depth).all() or np.any(depth < 0) or np.any(depth > 1):
        raise ValueError("MuJoCo depth buffer must be finite in [0,1]")
    near = sim.model.vis.map.znear * sim.model.stat.extent
    far = sim.model.vis.map.zfar * sim.model.stat.extent
    return near / (1 - depth * (1 - near/far))


class SimRobot:
    def __init__(self, adapter):
        self.adapter = adapter
        self.robot = adapter.env.robots[0]
        self.sim = adapter.env.env.sim

    def world_base(self):
        index = self.sim.model.body_name2id(self.robot.robot_model.root_body)
        return transform(self.sim.data.body_xpos[index], self.sim.data.body_xmat[index])

    def world_tcp(self):
        index = self.robot.eef_site_id
        return transform(self.sim.data.site_xpos[index], self.sim.data.site_xmat[index])

    def state(self):
        from robosuite.utils.transform_utils import mat2euler
        pose = np.linalg.inv(self.world_base()) @ self.world_tcp()
        gripper = np.asarray(self.adapter.obs.get("robot0_gripper_qpos", []))
        return RobotState(connected=True, enabled=True,
            pose=[*pose[:3, 3].tolist(), *mat2euler(pose[:3, :3], axes="sxyz").tolist()],
            joints=np.asarray(self.adapter.obs.get("robot0_joint_pos", [])).tolist(),
            gripper_width=float(np.sum(np.abs(gripper))),
            extra={"pose_reference": "tcp", "coordinate_frame": "robot_base",
                   "gripper_command": self.adapter.data.get("gripper_command", -1)})


class SimRGBD:
    def __init__(self, adapter):
        self.adapter = adapter

    def capture(self, robot):
        adapter = self.adapter
        images, depths = {}, {}
        for view in VIEWS:
            # RGB and depth share native optics. Undo OpenGL's bottom-up rows
            # on BOTH streams before applying CV intrinsics / optical axes.
            images[view] = np.ascontiguousarray(adapter.obs[view + "_image"][::-1])
            depth = metric_depth(adapter.env.env.sim, adapter.obs[view + "_depth"])
            depths[view] = np.ascontiguousarray(depth[::-1].reshape(images[view].shape[:2]), dtype=float)
        return Observation(id=f"sim-{adapter.data['steps']}-{uuid.uuid4().hex}",
            captured_monotonic=time.monotonic(), stamp_ns=time.time_ns(),
            pose=robot.pose, images=images, depths=depths,
            metadata={"depth_aligned_to_rgb": False, "source": "native co-located MuJoCo RGB/depth"},
            state={"pose_frame": "tcp", "sim_steps": adapter.data["steps"]})


def create_motion(adapter, settings):
    backend = SimRobot(adapter)
    sim = backend.sim
    factory, extrinsics = {}, {}
    for view in VIEWS:
        height, width = adapter.obs[view + "_image"].shape[:2]
        intrinsics = {"width": width, "height": height,
            "K_row_major_3x3": camera_intrinsics(sim, view, height, width).flatten().tolist()}
        factory[view] = {"mirrored": False, "depth_aligned_to_rgb": False,
            "rgb_intrinsics": intrinsics, "depth_intrinsics": intrinsics,
            "depth_to_rgb_extrinsic": {"direction": "T_rgb_camera_from_depth_camera",
                "T_row_major_4x4": np.eye(4).flatten().tolist()}}
        mounting = backend.world_tcp() if view == "robot0_eye_in_hand" else backend.world_base()
        key = "T_tool_from_camera" if view == "robot0_eye_in_hand" else "T_robot_base_from_camera"
        extrinsics[view] = {"calibrated": True,
            key: (np.linalg.inv(mounting) @ world_camera(sim, view)).flatten().tolist()}
    directory = adapter.directory / "calibration"
    write_json(directory / "factory.json", {"source": "MuJoCo native co-located RGB/depth optics",
        "pixel_centres": True, "offscreen_samples": int(sim.model.vis.quality.offsamples), "cameras": factory})
    write_json(directory / "extrinsics.json", {"source": "MuJoCo kinematics", "cameras": extrinsics})
    config = {
        "motion_enabled": True,
        "calibrated_direct": settings.get("calibrated_direct", True),
        "max_observation_age_s": positive(settings.get("max_observation_age_s", 600), "max_observation_age_s"),
        "hardware": {"pose_reference": "tcp", "orientation_unit": "radians", "tcp_offset_tool_m": [0, 0, 0]},
        "safety": {"workspace_reference": "tcp", "xyz_min": [-.3, -.8, -.1], "xyz_max": [1.3, .8, 1.4],
            "coarse_step_m": .05, "fine_step_m": .01, "fine_entry_distance_m": .05,
            **settings.get("safety", {})},
        "grounding": {"sample_radius_px": 1, "min_depth_m": .01, "max_depth_m": 5,
            **settings.get("grounding", {}),
            "factory_calibration_path": (directory / "factory.json").relative_to(adapter.root).as_posix(),
            "extrinsics_path": (directory / "extrinsics.json").relative_to(adapter.root).as_posix()}}
    # Reject malformed tuning before any policy action.
    for name in ("coarse_step_m", "fine_step_m", "fine_entry_distance_m"):
        positive(config["safety"][name], name)
    motion = CartesianEnvironment(adapter.root, adapter.directory, backend, SimRGBD(adapter), config)
    motion.move_max_steps = settings.get("move_max_steps", 40)
    if type(motion.move_max_steps) is not int or motion.move_max_steps < 1:
        raise ValueError("cartesian.move_max_steps must be a positive integer")
    motion.arrival_tolerance_m = positive(settings.get("arrival_tolerance_m", .002), "arrival_tolerance_m")
    motion.orientation_tolerance_rad = positive(settings.get("orientation_tolerance_rad", .02), "orientation_tolerance_rad")
    return motion


def execute_candidate(adapter, candidate):
    """Servo one absolute goal with dynamics, count each step, restore raw OSC.

    A measured miss is a known failed arrival, not an unknown command outcome.
    An exception leaves the adapter's durable pending marker for reconciliation.
    """
    from robosuite.utils.transform_utils import mat2quat, quat2axisangle
    motion = adapter.motion
    backend = motion.backend
    target = candidate["target_pose"]
    world = backend.world_base() @ transform(target[:3], rotation_xyz(target[3:]))
    vector = [*world[:3, 3].tolist(), *quat2axisangle(mat2quat(world[:3, :3])).tolist(),
              adapter.data.get("gripper_command", -1)]
    controller = backend.robot.controller
    previous = controller.use_delta
    count = 0
    arrived = False
    position_error = float(np.linalg.norm(backend.state().pose[:3] - np.asarray(target[:3])))
    relative_rotation = world[:3, :3].T @ backend.world_tcp()[:3, :3]
    orientation_error = math.acos(float(np.clip((np.trace(relative_rotation)-1)/2, -1, 1)))
    try:
        controller.use_delta = False
        for _ in range(motion.move_max_steps):
            if adapter.data["success"] or adapter.data["steps"] >= adapter.max_steps:
                break
            adapter._step(vector)
            count += 1
            actual = np.linalg.inv(backend.world_base()) @ backend.world_tcp()
            position_error = float(np.linalg.norm(actual[:3, 3] - target[:3]))
            relative_rotation = world[:3, :3].T @ backend.world_tcp()[:3, :3]
            orientation_error = math.acos(float(np.clip((np.trace(relative_rotation)-1)/2, -1, 1)))
            arrived = position_error <= motion.arrival_tolerance_m and orientation_error <= motion.orientation_tolerance_rad
            if arrived:
                break
    finally:
        controller.use_delta = previous
    result = {**candidate, "ok": arrived, "outcome_unknown": False,
        "execution_outcome": "completed", "executed_steps": count,
        "success": adapter.data["success"], "arrived": arrived,
        "gripper_command": vector[-1],
        "actual_pose": backend.state().pose, "position_error_m": position_error,
        "orientation_error_rad": orientation_error,
        "stop_reason": "arrived" if arrived else "task_success" if adapter.data["success"] else
            "episode_exhausted" if adapter.data["steps"] >= adapter.max_steps else "move_step_limit"}
    print(f"Cartesian {candidate['motion_mode']}: arrived={arrived} steps={count} error={position_error*1000:.2f} mm", flush=True)
    return result
