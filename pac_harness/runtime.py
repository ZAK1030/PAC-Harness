from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import uuid

from .contracts import validate_plan, validate_report
from .storage import Journal, RunLock, TaskState, dumps
from .tools import Knowledge, Tool, Toolbox, inspect_artifact, parameters


class Harness:
    def __init__(self, root, environment, planner, detector, *, identity,
                 to_user=None, hotkey=None, output=print):
        self.root = Path(root).resolve()
        self.environment, self.planner, self.detector = environment, planner, detector
        self.identity = {"root": str(self.root), "adapter": identity}
        self.to_user, self.hotkey, self.output = to_user, hotkey, output
        self.knowledge = Knowledge(self.root)

    def _observe(self):
        value = self.environment.observe()
        if not isinstance(value, dict):
            raise ValueError("Environment.observe must return a JSON object")
        dumps(value)
        self.current = deepcopy(value)
        self.journal.event("observation", observation=value)
        return deepcopy(value)

    def _tools(self):
        toolbox = Toolbox()
        toolbox.register(Tool("observe", "Read current environment state without performing an action.", parameters(), self._observe))
        toolbox.register(Tool("get_state", "Read the durable task state.", parameters(), self.state.context))
        toolbox.register(Tool("read_skill", "Read an optional scenario skill.",
                              parameters({"name": {"type": "string"}}, ("name",)), self.knowledge.read_skill))
        toolbox.register(Tool("inspect_artifact", "Read a declared text or image artifact on demand.",
                              parameters({"path": {"type": "string"}}, ("path",)),
                              lambda path: inspect_artifact(self.root, path, [self.current, self.state.data["before"], self.state.data["after"]])))
        toolbox.register(Tool("search_history", "Search this run's event history.", parameters({
            "query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 30}
        }), self.journal.search))
        for tool in self.environment.tools():
            toolbox.register(tool)
        return toolbox

    def _dialogue(self):
        if self.to_user is None or self.hotkey is None or not self.hotkey.pending():
            return None
        self.hotkey.consume()
        with self.hotkey.paused():
            result = self.to_user.run(self.state.context(), on_guidance=self.state.guidance)
        self.journal.event("to_user", result=result)
        return result if result.get("restart_required") else None

    def _post(self):
        after = self._observe()
        self.state.update(after=after)
        payload = {"task_state": self.state.context(), "before": self.state.data["before"],
                   "after": after, "action": self.state.data["action"], "execution": self.state.data["execution"]}
        report = self.detector.respond(payload, self._tools(), self.journal)
        validate_report(report)
        facts = deepcopy(self.state.data["facts"])
        for key, value in report["facts"].items():
            facts[key] = {"value": value, "source": "detector", "turn": self.state.data["turn"]}
        complete = self.state.data["action"]["name"] == "done" and report["task_complete"]
        self.state.update(detector=report, facts=facts, phase="completed" if complete else "ready")
        self.state.remember("step", action=self.state.data["action"], detector=report)
        self.output(f"Detector post: {report['status']} / {report['progress']} — {report['reason']}")

    def _reconcile(self):
        result = self.environment.reconcile(self.state.data["request_id"])
        self.journal.event("reconciliation", request_id=self.state.data["request_id"], result=result)
        execution = result.get("result")
        if (result.get("status") == "completed" and isinstance(execution, dict)
                and type(execution.get("ok")) is bool and execution.get("outcome_unknown") is not True):
            self.state.update(phase="post_pending", execution=result["result"])
        elif result.get("status") == "not_executed":
            self.state.remember("not_executed", action=self.state.data["action"])
            self.state.update(phase="ready")
        else:
            self.state.update(phase="execution_unknown")

    def run(self, task, directory, *, resume=False, max_turns=40, max_failures=3, preview=False):
        if not task.strip() or type(max_turns) is not int or max_turns < 1 or type(max_failures) is not int or max_failures < 1:
            raise ValueError("Task and positive run budgets are required")
        directory = Path(directory)
        with RunLock(directory):
            self.journal = Journal(directory)
            self.state = TaskState(directory / "task_state.json", task, self.identity, resume=resume)
            if self.hotkey is not None:
                self.hotkey.start()
            try:
                if self.state.data["phase"] == "execution_unknown":
                    self._reconcile()
                if self.state.data["phase"] == "execution_unknown":
                    return {"status": "execution_unknown", "reason": "Adapter could not establish the prior action outcome; no replay"}
                if self.state.data["phase"] not in {"ready", "post_pending", "completed"}:
                    raise ValueError("Unsupported checkpoint phase")
                failures = 0
                turns = 0
                while True:
                    update = self._dialogue()
                    if update:
                        return {"status": "restart_required", "changed_files": update.get("changed_files", [])}
                    if self.state.data["phase"] == "completed":
                        return {"status": "completed", "turns": self.state.data["turn"]}
                    try:
                        if self.state.data["phase"] == "post_pending":
                            self._post()
                            failures = 0
                            continue
                        if turns >= max_turns:
                            return {"status": "budget_exhausted", "turns": self.state.data["turn"]}
                        observation = self._observe()
                        actions = self.environment.actions()
                        if len({item["name"] for item in actions}) != len(actions) or any(item["name"] == "done" for item in actions):
                            raise ValueError("Adapter actions must be unique and cannot redefine done")
                        plan = self.planner.respond({"task_state": self.state.context(), "observation": observation,
                                                     "available_actions": actions}, self._tools(), self.journal)
                        validate_plan(plan, actions)
                        if self.to_user is not None and self.hotkey is not None and self.hotkey.pending():
                            continue
                        action = plan["action"]
                        before = self._observe()
                        if action["name"] != "done":
                            self.environment.validate(action, before)
                        if preview:
                            return {"status": "preview", "action": action}
                        turns += 1
                        self.state.update(turn=self.state.data["turn"] + 1, remaining_plan=plan["remaining_plan"],
                                          action=action, before=before, after=None, execution=None, detector=None,
                                          request_id=uuid.uuid4().hex,
                                          phase="post_pending" if action["name"] == "done" else "executing")
                        self.output(f"[{self.state.data['turn']}] Planner: {plan['reasoning']}")
                        if action["name"] != "done":
                            self.journal.event("action_pending", action=action, request_id=self.state.data["request_id"])
                            execution = self.environment.execute(deepcopy(action), self.state.data["request_id"])
                            if not isinstance(execution, dict) or type(execution.get("ok")) is not bool:
                                raise ValueError("Execution result must contain boolean ok")
                            dumps(execution)
                            if execution.get("outcome_unknown") is True:
                                self.state.update(phase="execution_unknown", execution=execution)
                                return {"status": "execution_unknown", "reason": "Adapter reported an unknown action outcome; no replay"}
                            self.state.update(phase="post_pending", execution=execution)
                            self.journal.event("action_result", action=action, execution=execution)
                            self.output("action " + action["name"] + ": " + dumps(execution))
                        failures = 0
                    except Exception as exc:
                        if self.state.data["phase"] == "executing":
                            self.state.update(phase="execution_unknown")
                            self.journal.event("execution_unknown", error_type=type(exc).__name__)
                            return {"status": "execution_unknown", "reason": "Execution interrupted; resume queries the adapter before any new action"}
                        failures += 1
                        self.journal.event("retry", phase=self.state.data["phase"], error_type=type(exc).__name__)
                        self.state.remember("feedback", reason=str(exc))
                        self.output(f"Retry {self.state.data['phase']}: {exc}")
                        if failures >= max_failures:
                            return {"status": "retry_exhausted", "phase": self.state.data["phase"], "error_type": type(exc).__name__}
            except KeyboardInterrupt:
                if self.state.data["phase"] == "executing":
                    self.state.update(phase="execution_unknown")
                return {"status": "interrupted", "phase": self.state.data["phase"]}
            finally:
                if self.hotkey is not None:
                    self.hotkey.close()
                self.environment.close()
