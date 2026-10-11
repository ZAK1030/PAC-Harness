"""Runnable offline two-mode robot example with analytical RGB-D fixtures."""
import time
import uuid

import numpy as np

from pac_harness.cartesian_motion import CartesianEnvironment
from pac_harness.coordinates import EULER_CONVENTION
from pac_harness.rgbd import Observation
from pac_harness.robot_backends import SimRobotBackend
from pac_harness.storage import write_json


class SyntheticRGBD:
    def capture(self, robot_state):
        # A fronto-parallel synthetic surface at camera Z=.5 m; this is not
        # physical camera evidence and carries no collision/identity proof.
        image = np.zeros((9, 9, 3), dtype=np.uint8)
        image[4, 4] = [255, 128, 0]
        return Observation(id=uuid.uuid4().hex, captured_monotonic=time.monotonic(),
            pose=robot_state.pose.copy(), stamp_ns=time.time_ns(), images={"overview": image},
            depths={"overview": np.full((9, 9), .5)}, metadata={},
            state={"pose_frame": "T_robot_base_from_tcp", "euler_convention": EULER_CONVENTION})

    def close(self):
        pass


def create(*, root, run_directory, config):
    # Fixture files stay in this run directory. Never overwrite user calibration.
    folder = run_directory / "simulation-calibration"
    matrix = np.eye(4).ravel().tolist()
    intrinsics = {"width": 9, "height": 9, "K_row_major_3x3": [100, 0, 4, 0, 100, 4, 0, 0, 1]}
    factory = {"cameras": {"overview": {"mirrored": False, "depth_aligned_to_rgb": False,
        "rgb_intrinsics": intrinsics, "depth_intrinsics": intrinsics,
        "depth_to_rgb_extrinsic": {"direction": "T_rgb_camera_from_depth_camera", "T_row_major_4x4": matrix}}}}
    write_json(folder / "factory.json", factory)
    write_json(folder / "extrinsics.json", {"cameras": {"overview": {"calibrated": True,
        "T_robot_base_from_camera": matrix}}})
    robot = SimRobotBackend(pose=[0, 0, .3, 0, 0, 0]); robot.connect(); robot.enable()
    settings = {"motion_enabled": True, "hardware": {"pose_reference": "tcp", "orientation_unit": "radians"},
        "safety": {"workspace_reference": "tcp", "xyz_min": [-1, -1, 0], "xyz_max": [1, 1, 1],
                   "coarse_step_m": .05, "fine_step_m": .01, "fine_entry_distance_m": .05},
        "grounding": {"views": ["overview"], "sample_radius_px": 0,
            "factory_calibration_path": (folder / "factory.json").relative_to(root).as_posix(),
            "extrinsics_path": (folder / "extrinsics.json").relative_to(root).as_posix()}}
    return CartesianEnvironment(root, run_directory, robot, SyntheticRGBD(), settings)


def main():
    import sys
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and not stream.isatty() and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    from pathlib import Path
    from pac_harness.runtime import Harness
    from pac_harness.storage import dumps
    root = Path.cwd().resolve()
    directory = root / "logs" / ("calibrated-demo-" + str(time.time_ns()))
    env = create(root=root, run_directory=directory, config={})
    class Planner:
        calls = 0
        def respond(self, payload, *_):
            self.calls += 1
            args = {"observation_id": payload["observation"]["observation_id"], "target": "synthetic_surface"}
            if self.calls == 1:
                action = {"name": "move_calibrated", "arguments": {**args, "view": "overview", "pixel_uv": [4, 4]}}
            elif self.calls == 2:
                action = {"name": "move_relative", "arguments": {**args, "delta_m": [.001, 0, 0]}}
            else:
                action = {"name": "done", "arguments": {}}
            return {"reasoning": "Offline two-mode demonstration", "remaining_plan": [], "action": action}
    class Detector:
        def respond(self, payload, *_):
            actual = payload["after"]["robot"]["pose"]
            goal = [0, 0, .35] if payload["action"]["name"] == "move_calibrated" else [.001, 0, .35]
            passed = np.allclose(actual[:3], goal, atol=1e-8)
            return {"status": "verified" if passed else "anomaly", "reason": "Compare simulated feedback with demonstration endpoint",
                    "evidence": ["Simulated actual XYZ: " + str(actual[:3])], "facts": {},
                    "progress": "improved" if passed else "unknown",
                    "task_complete": passed and payload["action"]["name"] == "done"}
    result = Harness(root, env, Planner(), Detector(), identity={"example": "synthetic RGB-D"}).run("offline two modes", directory)
    print(dumps(result))
    print("Run: " + str(directory))
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
