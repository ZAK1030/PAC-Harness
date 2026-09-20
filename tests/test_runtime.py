from copy import deepcopy
from contextlib import nullcontext
from pathlib import Path
import tempfile
import unittest

from examples.list_sorting import DemoDetector, DemoPlanner, ListEnvironment
from pac_harness.agents import Agent
from pac_harness.runtime import Harness
from pac_harness.storage import RunLock, loads, write_json
from pac_harness.to_user import ToUser
from pac_harness.tools import Knowledge


class FaultEnvironment(ListEnvironment):
    def __init__(self, directory, *, crash=False, unknown=False, known_failure=False, fail_after=False):
        super().__init__(directory, [3, 1, 2])
        self.calls = 0
        self.crash, self.unknown = crash, unknown
        self.known_failure, self.fail_after = known_failure, fail_after

    def observe(self):
        if self.calls and self.fail_after:
            self.fail_after = False
            raise OSError("observation unavailable")
        return super().observe()

    def execute(self, action, request_id):
        self.calls += 1
        if self.unknown:
            return {"ok": False, "outcome_unknown": True}
        if self.known_failure:
            self.known_failure = False
            return {"ok": False, "reason": "nothing changed"}
        fail_after = self.fail_after
        self.fail_after = False
        result = super().execute(action, request_id)
        self.fail_after = fail_after
        if self.crash:
            raise TimeoutError("result lost after commit")
        return result


class FailingDetector(DemoDetector):
    def __init__(self, count=1):
        self.failures = count
        self.calls = 0

    def respond(self, *args):
        self.calls += 1
        if self.failures:
            self.failures -= 1
            raise TimeoutError("post timeout")
        return super().respond(*args)


class AfterActionHotkey:
    """Request assistance once after an action, before its pending post check."""

    def __init__(self, environment):
        self.environment = environment
        self.consumed = False

    def start(self):
        pass

    def close(self):
        pass

    def pending(self):
        return bool(self.environment.calls) and not self.consumed

    def consume(self):
        self.consumed = True

    def paused(self):
        return nullcontext()


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.run = self.root / "logs/run"

    def harness(self, environment, detector=None, planner=None):
        return Harness(self.root, environment, planner or DemoPlanner(), detector or DemoDetector(),
                       identity="test", output=lambda text: None)

    def test_demo_completes_with_post_verified_done(self):
        environment = FaultEnvironment(self.run)
        result = self.harness(environment).run("sort", self.run)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(environment.calls, 1)
        state = loads((self.run / "task_state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["turn"], 2)
        self.assertTrue(state["facts"]["sorted"]["value"])

    def test_post_timeout_retries_only_detector(self):
        environment = FaultEnvironment(self.run)
        detector = FailingDetector()
        result = self.harness(environment, detector).run("sort", self.run)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(environment.calls, 1)
        self.assertEqual(detector.calls, 3)

    def test_observation_failure_after_send_does_not_repeat_action(self):
        environment = FaultEnvironment(self.run, fail_after=True)
        self.assertEqual(self.harness(environment).run("sort", self.run)["status"], "completed")
        self.assertEqual(environment.calls, 1)

    def test_resume_pending_post_does_not_repeat_action(self):
        environment = FaultEnvironment(self.run)
        result = self.harness(environment, FailingDetector(10)).run("sort", self.run, max_failures=1)
        self.assertEqual(result["phase"], "post_pending")
        resumed = FaultEnvironment(self.run)
        result = self.harness(resumed).run("sort", self.run, resume=True)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(resumed.calls, 0)

    def test_to_user_memory_update_reloads_for_both_agents_and_continues(self):
        (self.root / "memories").mkdir()
        (self.root / "memories/shared.md").write_text("OLD SHARED KNOWLEDGE", encoding="utf-8")

        class Backend:
            def reply(self, stage, prompt, *, audit):
                (stage / "memories/shared.md").write_text("NEW CONFIRMED KNOWLEDGE", encoding="utf-8")
                return {"status": "edited", "intent": "persistent_update", "message": "候选修改已完成",
                        "summary": "updated memory", "session_guidance": "Use the current sorted result."}

            def validate(self, stage, *, audit):
                return {"passed": True, "kind": "offline test double"}

        class Client:
            def __init__(self, demo):
                self.demo, self.requests = demo, []

            def complete(self, messages):
                self.requests.append(deepcopy(messages))
                return self.demo.respond(loads(messages[1]["content"]), None, self)

            def event(self, *args, **kwargs):
                pass

        environment = FaultEnvironment(self.run)
        clients = {"planner": Client(DemoPlanner()), "detector": Client(DemoDetector())}
        agents = {role: Agent(role, client, Knowledge(self.root)) for role, client in clients.items()}
        inputs = iter(["请更新长期记忆并继续任务"])
        assistant = ToUser(self.root, backend=Backend(), input_fn=lambda prompt: next(inputs),
                           output_fn=lambda message: None)
        harness = Harness(self.root, environment, agents["planner"], agents["detector"], identity="test",
                          to_user=assistant, hotkey=AfterActionHotkey(environment), output=lambda text: None)
        result = harness.run("sort", self.run)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(environment.calls, 1)
        self.assertIn("OLD SHARED KNOWLEDGE", clients["planner"].requests[0][0]["content"])
        self.assertIn("NEW CONFIRMED KNOWLEDGE", clients["planner"].requests[-1][0]["content"])
        for request in clients["detector"].requests:
            self.assertIn("NEW CONFIRMED KNOWLEDGE", request[0]["content"])
            state = loads(request[1]["content"])["task_state"]
            self.assertEqual(state["guidance"][0]["text"], "Use the current sorted result.")
        events = [loads(line) for line in (self.run / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        update = next(event["result"] for event in events if event["event"] == "to_user")
        self.assertEqual(update["status"], "applied")
        self.assertFalse(update["restart_required"])

    def test_to_user_code_restart_preserves_pending_action_without_replay(self):
        (self.root / "adapters").mkdir()
        (self.root / "adapters/sample.py").write_text("VERSION = 1\n", encoding="utf-8")

        class Backend:
            def reply(self, stage, prompt, *, audit):
                (stage / "adapters/sample.py").write_text("VERSION = 2\n", encoding="utf-8")
                return {"status": "edited", "intent": "persistent_update", "message": "候选代码修改已完成",
                        "summary": "updated code", "session_guidance": "Inspect the completed action first."}

            def validate(self, stage, *, audit):
                return {"passed": True, "kind": "offline test double"}

        environment = FaultEnvironment(self.run)
        inputs = iter(["修复代码并继续"])
        assistant = ToUser(self.root, backend=Backend(), input_fn=lambda prompt: next(inputs),
                           output_fn=lambda message: None)
        harness = Harness(self.root, environment, DemoPlanner(), DemoDetector(), identity="test",
                          to_user=assistant, hotkey=AfterActionHotkey(environment), output=lambda text: None)
        result = harness.run("sort", self.run)
        self.assertEqual(result["status"], "restart_required")
        self.assertEqual(environment.calls, 1)
        pending = loads((self.run / "task_state.json").read_text(encoding="utf-8"))
        self.assertEqual(pending["phase"], "post_pending")
        self.assertEqual(pending["before"]["items"], [3, 1, 2])
        self.assertEqual(pending["action"]["name"], "replace_items")
        self.assertTrue(pending["execution"]["ok"])

        class RecordingDetector(DemoDetector):
            payloads = []

            def respond(self, payload, *args):
                self.payloads.append(deepcopy(payload))
                return super().respond(payload, *args)

        detector = RecordingDetector()
        resumed = FaultEnvironment(self.run)
        result = self.harness(resumed, detector).run("sort", self.run, resume=True)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(resumed.calls, 0)
        post = detector.payloads[0]
        self.assertEqual(post["before"], pending["before"])
        self.assertEqual(post["action"], pending["action"])
        self.assertEqual(post["execution"], pending["execution"])
        self.assertEqual(post["task_state"]["request_id"], pending["request_id"])
        self.assertEqual(post["task_state"]["guidance"][0]["text"], "Inspect the completed action first.")

    def test_lost_result_is_reconciled_before_continuing(self):
        environment = FaultEnvironment(self.run, crash=True)
        result = self.harness(environment).run("sort", self.run)
        self.assertEqual(result["status"], "execution_unknown")
        resumed = FaultEnvironment(self.run)
        self.assertEqual(self.harness(resumed).run("sort", self.run, resume=True)["status"], "completed")
        self.assertEqual(resumed.calls, 0)

    def test_unresolved_dispatch_stays_unknown(self):
        environment = FaultEnvironment(self.run, unknown=True)
        self.assertEqual(self.harness(environment).run("sort", self.run)["status"], "execution_unknown")
        resumed = FaultEnvironment(self.run)
        resumed.reconcile = lambda request_id: {"status": "unknown"}
        self.assertEqual(self.harness(resumed).run("sort", self.run, resume=True)["status"], "execution_unknown")
        self.assertEqual(resumed.calls, 0)

    def test_known_action_failure_goes_to_post_then_replans(self):
        environment = FaultEnvironment(self.run, known_failure=True)
        self.assertEqual(self.harness(environment).run("sort", self.run)["status"], "completed")
        self.assertEqual(environment.calls, 2)

    def test_false_done_claim_is_not_success(self):
        class FalseDone:
            def respond(self, *args):
                return {"reasoning": "done", "remaining_plan": [], "action": {"name": "done", "arguments": {}}}
        environment = FaultEnvironment(self.run)
        result = self.harness(environment, planner=FalseDone()).run("sort", self.run, max_turns=2)
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertEqual(environment.calls, 0)

    def test_invalid_action_never_reaches_executor(self):
        class InvalidPlanner:
            def respond(self, *args):
                return {"reasoning": "unknown", "remaining_plan": [], "action": {"name": "invented", "arguments": {}}}
        environment = FaultEnvironment(self.run)
        result = self.harness(environment, planner=InvalidPlanner()).run("sort", self.run, max_failures=1)
        self.assertEqual(result["status"], "retry_exhausted")
        self.assertEqual(environment.calls, 0)

    def test_preview_never_dispatches(self):
        environment = FaultEnvironment(self.run)
        self.assertEqual(self.harness(environment).run("sort", self.run, preview=True)["status"], "preview")
        self.assertEqual(environment.calls, 0)

    def test_checkpoint_rejects_different_task_or_adapter(self):
        self.harness(FaultEnvironment(self.run)).run("sort", self.run, preview=True)
        with self.assertRaises(ValueError):
            self.harness(FaultEnvironment(self.run)).run("other", self.run, resume=True)
        harness = self.harness(FaultEnvironment(self.run))
        harness.identity["adapter"] = "different"
        with self.assertRaises(ValueError):
            harness.run("sort", self.run, resume=True)

    def test_process_crash_executing_marker_is_reconciled(self):
        harness = self.harness(FaultEnvironment(self.run))
        harness.run("sort", self.run, preview=True)
        state = deepcopy(harness.state.data)
        state.update(phase="executing", request_id="not-sent")
        write_json(self.run / "task_state.json", state)
        resumed = FaultEnvironment(self.run)
        self.assertEqual(self.harness(resumed).run("sort", self.run, resume=True)["status"], "completed")

    def test_run_lock_excludes_second_controller(self):
        with RunLock(self.run):
            with self.assertRaises(RuntimeError):
                with RunLock(self.run):
                    self.fail("second controller acquired the lock")

    def test_same_core_runs_a_different_action_and_observation_schema(self):
        class DocumentEnvironment:
            published = False
            def observe(self):
                return {"document": {"published": self.published}}
            def actions(self):
                return [{"name": "publish", "description": "Publish an in-memory test document",
                         "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}]
            def validate(self, action, observation):
                if action["name"] != "publish":
                    raise ValueError("unknown action")
            def execute(self, action, request_id):
                self.published = True
                return {"ok": True}
            def reconcile(self, request_id):
                return {"status": "unknown"}
            def tools(self):
                return []
            def close(self):
                pass
        class DocumentPlanner:
            def respond(self, payload, *args):
                complete = payload["observation"]["document"]["published"]
                return {"reasoning": "publish or verify", "remaining_plan": [],
                        "action": {"name": "done" if complete else "publish", "arguments": {}}}
        class DocumentDetector:
            def respond(self, payload, *args):
                return {"status": "verified", "reason": "document published", "evidence": ["published=true"],
                        "task_complete": payload["after"]["document"]["published"], "progress": "improved", "facts": {}}
        result = self.harness(DocumentEnvironment(), DocumentDetector(), DocumentPlanner()).run("publish", self.run)
        self.assertEqual(result["status"], "completed")


if __name__ == "__main__":
    unittest.main()
