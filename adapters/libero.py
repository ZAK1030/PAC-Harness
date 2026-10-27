"""LIBERO environment bridge; imports simulator dependencies only on construction."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import math
import shutil

from pac_harness.storage import loads, write_json
from pac_harness.tools import parameters

SUITES = {"libero_spatial": 220, "libero_object": 280, "libero_goal": 300,
          "libero_10": 520, "libero_90": 400}
PRO_SUITES = tuple(f"libero_{category}_{variant}" for category in
                   ("object", "goal", "spatial") for variant in ("swap", "task"))
SUITES.update({name: 2400 for name in PRO_SUITES})


def load_suite(name, config_directory):
    if name not in SUITES:
        raise ValueError(f"Unsupported suite: {name}")
    os.environ.setdefault("MUJOCO_GL", "egl")
    # Upstream's first import prompts on stdin. Write isolated paths beforehand.
    spec = importlib.util.find_spec("libero")
    if spec is None:
        raise RuntimeError("LIBERO is missing; run bash scripts/setup_libero.sh")
    import yaml
    package = Path(next(iter(spec.submodule_search_locations))) / "libero"
    location = Path(os.environ.setdefault("LIBERO_CONFIG_PATH", str(Path(config_directory).resolve())))
    location.mkdir(parents=True, exist_ok=True)
    file = location / "config.yaml"
    if not file.exists():
        file.write_text(yaml.safe_dump({"benchmark_root": str(package),
            "bddl_files": str(package / "bddl_files"), "init_states": str(package / "init_files"),
            "assets": str(package / "assets"), "datasets": str(package.parent / "datasets")}), encoding="utf-8")
    from libero.libero import benchmark
    registered = benchmark.get_benchmark_dict()
    if name not in registered:
        raise RuntimeError(f"{name} is unavailable in the imported LIBERO installation. "
                           "For LIBERO-Pro use bash scripts/run_libero_pro.sh after "
                           "bash scripts/setup_libero_pro.sh; do not replace the classic installation.")
    return registered[name](task_order_index=0)


def load_init_states(suite, task_id):
    # Official .pruned_init files serialize NumPy arrays, not model weights.
    import torch
    from libero.libero import get_libero_path
    task = suite.get_task(task_id)
    path = Path(get_libero_path("init_states")) / task.problem_folder / task.init_states_file
    return torch.load(path, map_location="cpu", weights_only=False)


class LiberoEnvironment:
    def __init__(self, *, root, run_directory, config):
        import numpy as np
        from PIL import Image
        self.np, self.Image = np, Image
        self.root = Path(root).resolve()
        self.directory = Path(run_directory).resolve()
        self.directory.relative_to(self.root)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.suite_name = config.get("suite", "libero_spatial")
        suite = load_suite(self.suite_name, self.root / "maintenance/libero-config")
        self.task_id = int(config.get("task_id", 0))
        if not 0 <= self.task_id < suite.n_tasks:
            raise ValueError("task_id outside suite")
        self.task = suite.get_task(self.task_id)
        self.max_steps = int(config.get("max_steps", SUITES[self.suite_name]))
        self.chunk_size = int(config.get("chunk_size", 10))
        if self.max_steps < 1 or self.chunk_size < 1:
            raise ValueError("max_steps and chunk_size must be positive")
        enabled = config.get("calibrated_motion", True)
        if type(enabled) is not bool:
            raise ValueError("calibrated_motion must be boolean")
        self.motion = None
        from libero.libero.envs import OffScreenRenderEnv
        self.env = OffScreenRenderEnv(bddl_file_name=suite.get_task_bddl_file_path(self.task_id),
            camera_heights=int(config.get("image_size", 256)), camera_widths=int(config.get("image_size", 256)),
            camera_depths=enabled, horizon=self.max_steps + int(config.get("settle_steps", 10)),
            ignore_done=True)
        try:
            # Pro's task variants reuse base filenames but change the BDDL instruction.
            # The actual environment instruction, not Task.language, is authoritative.
            instruction = self.env.language_instruction
            if isinstance(instruction, (list, tuple)):
                instruction = " ".join(instruction)
            if not isinstance(instruction, str) or not instruction.strip():
                raise ValueError("LIBERO environment has no task instruction")
            self.task = self.task._replace(language=instruction.strip())
            if enabled:
                from .libero_motion import pixel_center_rendering
                self.env.env.set_xml_processor(pixel_center_rendering)
            self.env.seed(int(config.get("seed", 0)))
            self.env.reset()
            self.path = self.directory / "libero_state.json"
            self.pending = self.directory / "libero_pending.json"
            if self.path.exists():
                self.data = loads(self.path.read_text(encoding="utf-8"))
                self.obs = self.env.set_init_state(np.asarray(self.data["sim_state"]))
            else:
                states = load_init_states(suite, self.task_id)
                initial = int(config.get("init_state_id", 0))
                if not 0 <= initial < len(states):
                    raise ValueError("init_state_id outside official initial states")
                self.obs = self.env.set_init_state(states[initial])
                for _ in range(int(config.get("settle_steps", 10))):
                    self.obs, _, _, _ = self.env.step([0, 0, 0, 0, 0, 0, -1])
                self.data = {"steps": 0, "success": bool(self.env.check_success()), "receipts": {}}
                self._save()
            self.data.setdefault("gripper_command", -1)
            if enabled:
                from .libero_motion import create_motion
                self.motion = create_motion(self, config.get("cartesian", {}))
        except BaseException:
            self.env.close()
            raise

    def _save(self):
        self.data["sim_state"] = self.env.env.sim.get_state().flatten().tolist()
        write_json(self.path, self.data)

    def observe(self):
        if self.motion is not None:
            result = self.motion.observe(artifact_prefix=f".observations/step-{self.data['steps']:06d}")
            result.update(instruction=self.task.language, suite=self.suite_name, task_id=self.task_id,
                steps=self.data["steps"], max_steps=self.max_steps, success=self.data["success"],
                truncated=self.data["steps"] >= self.max_steps,
                motion_guidance={"coarse": (
                    "Choose move_calibrated with a current RGB pixel and a base-frame approach offset; "
                    "one action commands the full target, without a coarse distance cap. "
                    "Stage above the object before descending. Internal physical steps count toward the horizon."
                    if self.motion.config.get("calibrated_direct", False) else
                    "Choose move_calibrated with a current RGB pixel and a base-frame approach offset; repeat bounded approaches with fresh images."),
                    "fine": "After coarse arrival acquire a fresh image and use move_relative for fine corrections; delta_m uses ROBOT BASE axes, not image axes.",
                    "gripper": "Cartesian moves preserve gripper command and TCP orientation. Use robot_action to open/close or rotate; its deltas use WORLD axes.",
                    "limits": self.motion.safety,
                    "arrival_is_task_success": False})
            self._prune_observations()
            return result
        artifacts = []
        for key in ("agentview_image", "robot0_eye_in_hand_image"):
            if key in self.obs:
                path = self.directory / ".observations" / f"step-{self.data['steps']:06d}-{key}.png"
                path.parent.mkdir(parents=True, exist_ok=True)
                # MuJoCo offscreen observations are vertically flipped.
                if not path.exists():
                    self.Image.fromarray(self.np.ascontiguousarray(self.obs[key][::-1])).save(path)
                artifacts.append({"path": path.relative_to(self.root).as_posix(), "description": key,
                                  "camera": key.removesuffix("_image"), "type": "image"})
        self._prune_observations()
        robot = {k: self.np.asarray(self.obs[k]).tolist() for k in
                 ("robot0_eef_pos", "robot0_eef_quat", "robot0_gripper_qpos", "robot0_joint_pos") if k in self.obs}
        return {"instruction": self.task.language, "suite": self.suite_name, "task_id": self.task_id,
                "steps": self.data["steps"], "max_steps": self.max_steps,
                "success": self.data["success"], "truncated": self.data["steps"] >= self.max_steps,
                "robot": robot, "artifacts": artifacts}

    def _prune_observations(self):
        """Keep both cameras for the latest two states for before/after checks.

        The cache survives restart so a pending Detector can inspect its before
        image. Repeated reads of one simulator state reuse immutable files.
        """
        files = list((self.directory / ".observations").glob("step-*.png"))
        states = sorted({file.name.split("-")[1] for file in files})
        keep = set(states[-2:])
        for file in files:
            if file.name.split("-")[1] not in keep:
                file.unlink()

    def record_action_frame(self, *, turn, action, execution, observation, request_id):
        """Archive exactly one agentview image per dispatched planner turn."""
        artifact = next(a for a in observation["artifacts"] if a.get("camera") == "agentview")
        path = self.directory / "frames" / f"turn-{turn:03d}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            shutil.copyfile(self.root / artifact["path"], path)
        record = {"turn": turn, "path": path.relative_to(self.root).as_posix(), "camera": "agentview",
            "stage": "after_action", "sim_steps": observation["steps"], "request_id": request_id,
            "action": action, "executed_steps": (execution or {}).get("executed_steps", 0),
            "success": observation["success"], "observation_id": observation.get("observation_id")}
        index = self.directory / "frames.json"
        records = loads(index.read_text(encoding="utf-8")) if index.exists() else {}
        records.setdefault(str(turn), record)
        write_json(index, records)
        return records[str(turn)]

    def actions(self):
        return (self.motion.actions() if getattr(self, "motion", None) is not None else []) + [{"name": "robot_action", "description": "Execute normalized OSC_POSE action chunk in WORLD axes. Each vector: dx,dy,dz,rx,ry,rz,gripper in [-1,1]; gripper -1 opens, +1 closes. Use for gripper/orientation; prefer calibrated Cartesian actions for translation. No automatic rescaling.",
                 "parameters": parameters({"actions": {"type": "array", "items": {"type": "array", "items": {
                     "type": "number", "minimum": -1, "maximum": 1}}}}, ("actions",))}]

    def validate(self, action, observation):
        if observation["success"] or observation["truncated"]:
            raise ValueError("Episode ended; submit done")
        if action.get("name") in ("move_calibrated", "move_relative"):
            if getattr(self, "motion", None) is None:
                raise ValueError("Calibrated motion is disabled")
            self.motion.validate(action, observation)
            frame = self.motion.frames.get(action["arguments"]["observation_id"])
            if frame is None or frame.state.get("sim_steps") != self.data["steps"]:
                raise ValueError("Simulator advanced since the selected image; acquire fresh evidence")
            return
        if action.get("name") != "robot_action":
            raise ValueError("Unknown LIBERO action")
        vectors = action.get("arguments", {}).get("actions")
        if not isinstance(vectors, list) or not 1 <= len(vectors) <= self.chunk_size:
            raise ValueError("Action chunk must contain 1..chunk_size vectors")
        for vector in vectors:
            if not isinstance(vector, list) or len(vector) != 7 or any(
                type(v) not in (int, float) or not math.isfinite(v) or not -1 <= v <= 1 for v in vector):
                raise ValueError("Each action must contain seven finite values in [-1,1]")

    def _step(self, vector):
        self.obs, _, _, _ = self.env.step(vector)
        self.data["steps"] += 1
        self.data["gripper_command"] = float(vector[-1])
        self.data["success"] = bool(self.env.check_success())

    def execute(self, action, request_id):
        if request_id in self.data["receipts"]:
            return dict(self.data["receipts"][request_id])
        if self.pending.exists():
            return {"ok": False, "outcome_unknown": True}
        before = self.observe()
        self.validate(action, before)
        cartesian = action["name"] in ("move_calibrated", "move_relative")
        candidate = self.motion.candidate(action, before) if cartesian else None
        write_json(self.pending, {"request_id": request_id, "candidate": candidate})
        if cartesian:
            from .libero_motion import execute_candidate
            result = execute_candidate(self, candidate)
        else:
            count = 0
            for vector in action["arguments"]["actions"]:
                self._step(vector)
                count += 1
                if self.data["success"] or self.data["steps"] >= self.max_steps:
                    break
            result = {"ok": True, "executed_steps": count, "success": self.data["success"]}
        self.data["receipts"][request_id] = result
        self._save()
        self.pending.unlink()
        return dict(result)

    def reconcile(self, request_id):
        if request_id in self.data["receipts"]:
            if self.pending.exists():
                self.pending.unlink()
            return {"status": "completed", "result": self.data["receipts"][request_id]}
        return {"status": "unknown" if self.pending.exists() else "not_executed"}

    def terminal_result(self):
        if self.data["success"]:
            return {"status": "completed", "reason": "task_success", "success": True,
                    "steps": self.data["steps"], "completion_source": "libero.check_success"}
        if not self.data["success"] and self.data["steps"] >= self.max_steps:
            return {"status": "episode_exhausted", "steps": self.data["steps"]}
        return None

    def tools(self):
        return self.motion.tools() if getattr(self, "motion", None) is not None else []

    def close(self):
        self.env.close()


def create(**kwargs):
    return LiberoEnvironment(**kwargs)
