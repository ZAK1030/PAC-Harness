"""Joint actions with partial targets, fresh feedback and durable no-replay receipts."""
from dataclasses import asdict
import math
from pathlib import Path
import time

from .devices import load_device
from .robot_backends import _vector, SimRobotBackend, receipt_evidence
from .storage import confined, loads, write_json
from .tools import parameters


def joint_target(current, arguments):
    current = _vector(current, len(current), "current joints")
    if set(arguments) - {"mode", "unit", "joints"}:
        raise ValueError("Unexpected joint_move arguments")
    if arguments.get("mode") not in {"relative", "absolute"} or arguments.get("unit") not in {"deg", "rad"}:
        raise ValueError("Specify relative/absolute mode and deg/rad unit")
    selected = arguments.get("joints")
    if not isinstance(selected, dict) or not selected:
        raise ValueError("Specify at least one joint")
    target = current.copy()
    for joint, angle in selected.items():
        if joint not in {f"J{i+1}" for i in range(len(current))}:
            raise ValueError("Unknown joint: " + str(joint))
        value = _vector([angle], 1, joint)[0]
        value = math.radians(value) if arguments["unit"] == "deg" else value
        index = int(joint[1:]) - 1
        target[index] = current[index] + value if arguments["mode"] == "relative" else value
    return _vector(target, len(current), "target joints")


class JointEnvironment:
    def __init__(self, backend, run_directory, *, motion_enabled=False, tolerance_rad=.002, output=print):
        if type(motion_enabled) is not bool:
            raise ValueError("motion_enabled must be boolean")
        if type(tolerance_rad) not in (int, float) or not math.isfinite(tolerance_rad) or tolerance_rad <= 0:
            raise ValueError("Invalid arrival tolerance")
        self.backend, self.directory = backend, Path(run_directory).resolve()
        self.motion_enabled, self.tolerance, self.output = motion_enabled, tolerance_rad, output

    def observe(self):
        return {"robot": asdict(self.backend.state()), "observed_at_ns": time.time_ns()}

    def actions(self):
        return [{"name": "joint_move", "description":
                 "Move selected joints relative to current feedback or to absolute angles. "
                 "Omitted joints keep CURRENT values; to return J6 only specify J6. Joint motion does not preserve TCP XYZ.",
                 "parameters": parameters({"mode": {"type": "string", "enum": ["relative", "absolute"]},
                     "unit": {"type": "string", "enum": ["deg", "rad"]},
                     "joints": {"type": "object", "properties": {
                         f"J{i+1}": {"type": "number"} for i in range(self.backend.dof)},
                         "additionalProperties": False}}, ("mode", "unit", "joints"))}]

    def validate(self, action, observation):
        if action.get("name") != "joint_move":
            raise ValueError("Unsupported robot action")
        if not self.motion_enabled:
            raise ValueError("Joint motion is disabled; explicitly configure motion_enabled")
        robot = observation["robot"]
        if robot.get("connected") is not True or robot.get("enabled") is not True or robot.get("fault"):
            raise ValueError("Robot must be connected, enabled and fault-free")
        current = _vector(robot.get("joints"), self.backend.dof, "current joints")
        self.backend.limits.check_joints(joint_target(current, action["arguments"]))

    def _receipt(self, request_id):
        if not isinstance(request_id, str) or not request_id or not request_id.isalnum():
            raise ValueError("Invalid request id")
        return confined(self.directory, "joint-receipts/" + request_id + ".json")

    def execute(self, action, request_id):
        path = self._receipt(request_id)
        if path.exists():
            saved = loads(path.read_text(encoding="utf-8"))
            return saved.get("result") or {"ok": False, "outcome_unknown": True}
        observation = self.observe()
        self.validate(action, observation)
        start = observation["robot"]["joints"]
        target = self.backend.limits.check_joints(joint_target(start, action["arguments"]))
        result = {"ok": False, "outcome_unknown": True, "initial_joints_rad": start, "target_joints_rad": target}
        # Persist before entering vendor code, including a possibly ambiguous send.
        write_json(path, {"status": "executing", "action": action, "result": result})
        self.output("joint_move start (deg): " + str([round(math.degrees(x), 4) for x in start]))
        self.output("joint_move target (deg): " + str([round(math.degrees(x), 4) for x in target]))
        try:
            checked = getattr(self.backend, "move_joints_checked", None)
            receipt = checked(start, target) if callable(checked) else self.backend.move_joints(target)
            result["backend_receipt"] = receipt_evidence(receipt)
            measured = self.backend.state()
            actual = _vector(measured.joints, self.backend.dof, "actual joints")
            result["actual_joint_positions_rad"] = actual
            self.output("joint_move actual (deg): " + str([round(math.degrees(x), 4) for x in actual]))
            error = max(abs(a-b) for a, b in zip(actual, target))
            result["max_joint_error_rad"] = error
            if (not isinstance(receipt, dict) or receipt.get("ok") is not True
                    or receipt.get("execution_outcome") != "completed" or measured.fault
                    or not measured.connected or error > self.tolerance):
                raise RuntimeError("Joint completion/feedback was not verified")
            result.update(ok=True, outcome_unknown=False, execution_outcome="completed")
        except Exception as exc:
            result["error_type"] = type(exc).__name__
            self.output("joint_move outcome unknown; no resend (" + type(exc).__name__ + ")")
        write_json(path, {"status": "completed" if result["ok"] else "unknown", "action": action, "result": result})
        return result

    def reconcile(self, request_id):
        path = self._receipt(request_id)
        if not path.exists():
            return {"status": "unknown"}
        saved = loads(path.read_text(encoding="utf-8"))
        return {"status": "completed", "result": saved["result"]} if saved["status"] == "completed" else {"status": "unknown"}

    def tools(self):
        return []

    def close(self):
        self.backend.close()


def create(*, root, run_directory, config):
    backend = load_device(config["robot"], root=root)
    try:
        backend.connect()  # Read-only for the bridge. Never automatically enable hardware.
        return JointEnvironment(backend, run_directory, motion_enabled=config.get("motion_enabled", False),
                                tolerance_rad=config.get("tolerance_rad", .002))
    except BaseException:
        backend.close()
        raise


def simulated_robot(*, root, config):
    robot = SimRobotBackend(dof=config.get("dof", 6), joints=config.get("joints"))
    robot.enable()
    return robot
