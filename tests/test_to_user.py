from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pac_harness.codex_backend import ToUserError
from pac_harness.storage import confined, loads, write_json
from pac_harness.to_user import ToUser


class Backend:
    def __init__(self, edit=None, validate=None, guidance=""):
        self.edit, self.validator, self.guidance = edit, validate, guidance

    def reply(self, stage, prompt, *, audit):
        if self.edit:
            self.edit(stage)
        return {"status": "edited" if self.edit else "reply", "message": "已分析。",
                "intent": "persistent_update" if self.edit else "task_guidance" if self.guidance else "analysis",
                "summary": "result", "session_guidance": self.guidance}

    def validate(self, stage, *, audit):
        if self.validator:
            self.validator(stage)
        return {"passed": True, "kind": "test_double"}


class ToUserTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "memories").mkdir()
        (self.root / "memories/shared.md").write_text("original", encoding="utf-8")

    def assistant(self, backend, requests=("fix", "c")):
        choices = iter(requests)
        return ToUser(self.root, backend=backend, input_fn=lambda prompt: next(choices), output_fn=lambda text: None)

    def test_edit_is_validated_applied_and_audited(self):
        backend = Backend(lambda stage: (stage / "memories/shared.md").write_text("updated", encoding="utf-8"))
        result = self.assistant(backend).run({"task": "test"})
        self.assertEqual(result["status"], "applied")
        self.assertFalse(result["restart_required"])
        self.assertEqual((self.root / "memories/shared.md").read_text(), "updated")
        self.assertTrue(list(Path(result["audit"]).glob("turn-*/changes.diff")))

    def test_failed_validation_does_not_apply_changes(self):
        def fail(stage):
            raise ToUserError("test failed")
        backend = Backend(lambda stage: (stage / "memories/shared.md").write_text("updated"), fail)
        self.assertEqual(self.assistant(backend).run({})["status"], "no_change")
        self.assertEqual((self.root / "memories/shared.md").read_text(), "original")

    def test_evidence_mutation_prevents_apply(self):
        def edit(stage):
            (stage / "diagnostics/context.json").write_text("{}")
            (stage / "memories/shared.md").write_text("updated")
        self.assertEqual(self.assistant(Backend(edit)).run({"task": "unchanged"})["status"], "no_change")
        self.assertEqual((self.root / "memories/shared.md").read_text(), "original")

    def test_concurrent_source_edit_is_preserved(self):
        def validate(stage):
            (self.root / "memories/shared.md").write_text("user edit")
        backend = Backend(lambda stage: (stage / "memories/shared.md").write_text("model edit"), validate)
        self.assertEqual(self.assistant(backend).run({})["status"], "no_change")
        self.assertEqual((self.root / "memories/shared.md").read_text(), "user edit")

    def test_tests_cannot_mutate_the_source_being_applied(self):
        backend = Backend(lambda stage: (stage / "memories/shared.md").write_text("model edit"),
                          lambda stage: (stage / "memories/shared.md").write_text("test edit"))
        self.assertEqual(self.assistant(backend).run({})["status"], "no_change")
        self.assertEqual((self.root / "memories/shared.md").read_text(), "original")

    def test_guidance_is_saved_before_reply_display_or_interrupt(self):
        saved = []
        assistant = self.assistant(Backend(guidance="persistent current-task correction"))
        def output(text):
            if text == "已分析。":
                self.assertEqual(saved, ["persistent current-task correction"])
                raise KeyboardInterrupt()
        assistant.output_fn = output
        with self.assertRaises(KeyboardInterrupt):
            assistant.run({}, on_guidance=saved.append)
        self.assertTrue(list((self.root / "maintenance").glob("*/report.json")))

    def test_partial_apply_failure_rolls_back_written_files(self):
        from pac_harness.storage import atomic_write
        def edit(stage):
            (stage / "memories/shared.md").write_text("updated")
            (stage / "memories/zzz.md").write_text("new")
        def faulty_write(path, raw):
            if Path(path) == self.root / "memories/zzz.md":
                raise OSError("write failed")
            return atomic_write(path, raw)
        with patch("pac_harness.to_user.atomic_write", side_effect=faulty_write):
            self.assertEqual(self.assistant(Backend(edit)).run({})["status"], "no_change")
        self.assertEqual((self.root / "memories/shared.md").read_text(), "original")
        self.assertFalse((self.root / "memories/zzz.md").exists())

    def test_stage_omits_logs_credentials_and_external_paths(self):
        (self.root / ".env").write_text("SECRET=value")
        (self.root / "logs").mkdir()
        (self.root / "logs/old.txt").write_text("old data")
        write_json(self.root / "config.json", {"api_key": "example-secret"})
        write_json(self.root / "config.local.json", {"api_key": "local-example-secret"})
        assistant = self.assistant(Backend())
        audit = self.root / "audit"
        audit.mkdir()
        stage, original, protected = assistant._stage(audit, {"before": {"artifacts": [{"path": "../outside.txt"}]}})
        self.assertFalse((stage / ".env").exists())
        self.assertFalse((stage / "logs").exists())
        self.assertFalse((stage / "config.local.json").exists())
        self.assertNotIn("example-secret", (stage / "config.json").read_text())
        self.assertIn("omitted", (stage / "diagnostics/evidence-index.json").read_text())
        self.assertTrue((stage / "diagnostics/project-map.json").exists())

    def test_project_map_is_protected_evidence(self):
        assistant = self.assistant(Backend())
        audit = self.root / "audit"
        audit.mkdir()
        stage, _, protected = assistant._stage(audit, {"task": "map"})
        (stage / "diagnostics/project-map.json").write_text("{}")
        with self.assertRaises(ToUserError):
            assistant._verify_protected(stage, protected)

    def test_path_validation_rejects_escape(self):
        for value in ("../outside", "C:/outside", "folder/../outside", "/outside", "folder\\outside"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                confined(self.root, value)


def response(*, intent="analysis", status="reply", message="已完成分析。", guidance=""):
    return {"intent": intent, "status": status, "message": message,
            "summary": message, "session_guidance": guidance}


class SequenceBackend:
    def __init__(self, *turns, validator=None):
        self.turns = iter(turns)
        self.calls, self.validations = [], []
        self.validator = validator

    def reply(self, stage, prompt, *, audit):
        self.calls.append({"stage": stage, "prompt": prompt, "audit": audit})
        answer = next(self.turns)
        return answer(stage) if callable(answer) else answer

    def validate(self, stage, *, audit):
        self.validations.append(audit)
        if self.validator:
            self.validator(stage, audit)
        return {"passed": True, "kind": "test_double"}


class ToUserMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "memories").mkdir()
        (self.root / "memories/shared.md").write_text("original", encoding="utf-8")
        self.output = []

    def assistant(self, backend, requests=("修改以后的行为", "c"), **config):
        choices = iter(requests)
        return ToUser(self.root, config, backend=backend, input_fn=lambda _: next(choices), output_fn=self.output.append)

    def edit(self, relative="memories/shared.md", content="scoped correction", **reply):
        def perform(stage):
            path = stage / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return response(intent="persistent_update", status="edited", **reply)
        return perform

    def report(self):
        return loads(next((self.root / "maintenance").glob("*/report.json")).read_text(encoding="utf-8"))

    def test_persistent_update_cannot_be_replaced_by_temporary_guidance(self):
        backend = SequenceBackend(response(intent="persistent_update", guidance="claimed permanent rule"), self.edit())
        saved = []
        result = self.assistant(backend, requests=("请永久记住",)).run({}, on_guidance=saved.append)
        self.assertEqual(result["status"], "applied")
        self.assertEqual(saved, [])
        self.assertEqual(result["exit_reason"], "changes_applied")
        self.assertEqual(result["unapplied_files"], [])
        self.assertEqual(len(backend.calls), 2)
        self.assertEqual((self.root / "memories/shared.md").read_text(), "scoped correction")

    def test_no_diff_is_repaired_without_another_user_message(self):
        backend = SequenceBackend(response(intent="persistent_update", status="edited", message="false success"), self.edit())
        result = self.assistant(backend, requests=("修改 memory",)).run({})
        self.assertNotIn("false success", self.output)
        self.assertIn("没有文件差异", backend.calls[1]["prompt"])
        self.assertTrue((Path(result["audit"]) / "turn-001/repair-001/changes.diff").exists())
        self.assertEqual(len(backend.validations), 1)

    def test_no_diff_failure_is_bounded_and_never_reports_applied(self):
        backend = SequenceBackend(*[response(intent="persistent_update", status="edited", message="false success")] * 3)
        result = self.assistant(backend).run({})
        self.assertEqual(len(backend.calls), 3)
        self.assertEqual(result["status"], "no_change")
        self.assertIn("last_error", result)
        self.assertNotIn("false success", self.output)
        self.assertEqual(backend.validations, [])

    def test_validation_output_is_available_to_automatic_repair(self):
        def validate(stage, audit):
            if (stage / "memories/shared.md").read_text() == "bad candidate":
                (audit / "offline-tests.txt").write_text("Expected scoped correction, got bad candidate")
                raise ToUserError("tests failed")
        def repair(stage):
            self.assertEqual((self.root / "memories/shared.md").read_text(), "original")
            self.assertIn("Expected scoped correction", (stage / "diagnostics/last-validation.txt").read_text())
            return self.edit()(stage)
        backend = SequenceBackend(self.edit(content="bad candidate"), repair, validator=validate)
        result = self.assistant(backend, requests=("修改规则",)).run({})
        self.assertEqual(result["status"], "applied")
        self.assertEqual(len(backend.validations), 2)
        self.assertIn("Expected scoped correction", backend.calls[1]["prompt"])

    def test_syntax_failure_is_repaired_before_tests_or_application(self):
        backend = SequenceBackend(self.edit("adapters/example.py", "def broken("), self.edit("adapters/example.py", "VALUE = 1\n"))
        result = self.assistant(backend, requests=("修复代码",)).run({})
        self.assertEqual(result["status"], "applied")
        self.assertTrue(result["restart_required"])
        self.assertEqual(len(backend.validations), 1)
        self.assertIn("adapters/example.py", backend.calls[1]["prompt"])

    def test_network_error_does_not_trigger_automatic_coding_retry(self):
        def unavailable(stage):
            raise ToUserError("connection failed")
        backend = SequenceBackend(unavailable)
        result = self.assistant(backend).run({})
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(result["status"], "no_change")
        self.assertIn("connection failed", result["last_error"])

    def test_concurrent_edit_does_not_trigger_automatic_overwrite(self):
        def concurrent(stage, audit):
            (self.root / "memories/shared.md").write_text("operator edit")
        backend = SequenceBackend(self.edit(), validator=concurrent)
        result = self.assistant(backend).run({})
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(result["status"], "no_change")
        self.assertEqual((self.root / "memories/shared.md").read_text(), "operator edit")

    def test_protected_evidence_remains_protected_during_repairs(self):
        def tamper(stage):
            (stage / "diagnostics/context.json").write_text("{}")
            return self.edit()(stage)
        backend = SequenceBackend(response(intent="persistent_update", status="edited"), tamper)
        result = self.assistant(backend).run({"task": "keep provenance"})
        self.assertEqual(result["status"], "no_change")
        self.assertEqual(len(backend.calls), 2)
        self.assertEqual((self.root / "memories/shared.md").read_text(), "original")

    def test_guidance_accumulates_once_and_natural_return_does_not_enter_task(self):
        backend = SequenceBackend(response(intent="task_guidance", guidance="A"), response(intent="task_guidance", guidance="A"),
                                  response(intent="task_guidance", guidance="B"), response(intent="return_to_task", guidance="UI only"))
        saved = []
        result = self.assistant(backend, requests=("one", "repeat", "two", "结束对话继续任务")).run({}, on_guidance=saved.append)
        self.assertEqual(saved, ["A", "B"])
        self.assertEqual(result["session_guidance"], "A\n\nB")
        self.assertTrue(result["guidance_persisted"])
        self.assertEqual(result["exit_reason"], "return_intent")

    def test_shortcut_question_does_not_exit_or_become_task_guidance(self):
        backend = SequenceBackend(response(intent="return_to_task", status="question", guidance="press Ctrl+G"), response())
        result = self.assistant(backend, requests=("如何退出", "继续讨论", "C")).run({})
        self.assertEqual(len(backend.calls), 2)
        self.assertEqual(result["exit_reason"], "return_command")
        self.assertEqual(result["session_guidance"], "")

    def test_ctrl_g_returns_without_model_call_or_staging(self):
        backend = SequenceBackend()
        result = self.assistant(backend, requests=("\x07",)).run({})
        self.assertEqual(result["exit_reason"], "ctrl_g")
        self.assertFalse(backend.calls)
        self.assertFalse((Path(result["audit"]) / "workspace").exists())

    def test_default_console_reader_drains_entry_key_once(self):
        with patch("pac_harness.to_user.drain_entry_hotkey") as drain, patch(
                "pac_harness.to_user.read_dialogue_input", side_effect=["分析", "\x07"]):
            assistant = ToUser(self.root, backend=SequenceBackend(response()), output_fn=self.output.append)
            self.assertEqual(assistant.run({})["exit_reason"], "ctrl_g")
        drain.assert_called_once_with()

    def test_custom_web_input_never_uses_console_reader(self):
        with patch("pac_harness.to_user.drain_entry_hotkey") as drain:
            self.assistant(SequenceBackend(), requests=("C",)).run({})
        drain.assert_not_called()

    def test_interruption_during_model_preserves_unanswered_request(self):
        def interrupt(stage):
            raise KeyboardInterrupt()
        backend = SequenceBackend(interrupt)
        with self.assertRaises(KeyboardInterrupt):
            self.assistant(backend, requests=("保留我的请求",)).run({})
        report = self.report()
        self.assertEqual(report["exit_reason"], "operator_interrupt")
        transcript = loads((Path(report["audit"]) / "dialogue.json").read_text(encoding="utf-8"))
        self.assertEqual(transcript, [{"role": "user", "content": "保留我的请求"}])

    def test_guidance_survives_interruption_at_next_input(self):
        saved = []
        inputs = iter(("本次先这样",))
        def read(_):
            try:
                return next(inputs)
            except StopIteration:
                self.assertEqual(saved, ["temporary correction"])
                raise KeyboardInterrupt()
        role = ToUser(self.root, backend=SequenceBackend(response(intent="task_guidance", guidance="temporary correction")),
                      input_fn=read, output_fn=self.output.append)
        with self.assertRaises(KeyboardInterrupt):
            role.run({}, on_guidance=saved.append)
        self.assertTrue(self.report()["guidance_persisted"])
        self.assertEqual(self.report()["session_guidance"], "temporary correction")

    def test_unbound_guidance_is_audited_without_claiming_checkpoint_save(self):
        result = self.assistant(SequenceBackend(response(intent="task_guidance", guidance="temporary"))).run({})
        self.assertFalse(result["guidance_persisted"])
        self.assertEqual(result["session_guidance"], "temporary")
        self.assertTrue(any("未绑定运行任务" in text for text in self.output))
        self.assertEqual((self.root / "memories/shared.md").read_text(), "original")

    def test_failed_guidance_callback_never_claims_guidance_saved(self):
        def fail(_):
            raise OSError("checkpoint unavailable")
        result = self.assistant(SequenceBackend(response(intent="task_guidance", guidance="temporary", message="false confirmation"))).run({}, on_guidance=fail)
        self.assertFalse(result["guidance_persisted"])
        self.assertEqual(result["guidance_entries"], [])
        self.assertNotIn("false confirmation", self.output)

    def test_callback_failure_after_code_apply_preserves_restart(self):
        def fail(_):
            raise OSError("checkpoint unavailable")
        backend = SequenceBackend(self.edit("adapters/example.py", "VALUE = 1\n", guidance="temporary"))
        result = self.assistant(backend, requests=("fix code",)).run({}, on_guidance=fail)
        self.assertEqual(result["status"], "applied")
        self.assertTrue(result["restart_required"])
        self.assertFalse(result["guidance_persisted"])
        self.assertEqual(result["changed_files"], ["adapters/example.py"])

    def test_natural_return_reports_unapplied_question_candidate(self):
        def question(stage):
            self.edit()(stage)
            return response(status="question", intent="persistent_update")
        backend = SequenceBackend(question, response(intent="return_to_task", message="false applied claim"))
        result = self.assistant(backend, requests=("改规则", "先返回")).run({})
        self.assertEqual(result["exit_reason"], "return_intent")
        self.assertEqual(result["unapplied_files"], ["memories/shared.md"])
        self.assertNotIn("false applied claim", self.output)
        self.assertEqual((self.root / "memories/shared.md").read_text(), "original")

    def test_partial_candidate_can_be_finished_after_question(self):
        def question(stage):
            self.edit()(stage)
            return response(intent="persistent_update", status="question")
        backend = SequenceBackend(question, response(intent="persistent_update", status="edited"))
        result = self.assistant(backend, requests=("改规则", "限定当前场景")).run({})
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["unapplied_files"], [])

    def test_unreported_edit_requires_correction_before_success(self):
        def wrong_status(stage):
            self.edit()(stage)
            return response(message="false success")
        backend = SequenceBackend(wrong_status, response(intent="persistent_update", status="edited"))
        result = self.assistant(backend, requests=("改规则",)).run({})
        self.assertEqual(result["status"], "applied")
        self.assertNotIn("false success", self.output)

    def test_python_inside_skill_requires_restart(self):
        result = self.assistant(SequenceBackend(self.edit("skills/helper/run.py", "VALUE = 1\n")), requests=("改技能代码",)).run({})
        self.assertTrue(result["restart_required"])

    def test_repair_budget_can_disable_automatic_retries(self):
        backend = SequenceBackend(response(intent="persistent_update", status="edited"))
        result = self.assistant(backend, max_repair_attempts=0).run({})
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(result["status"], "no_change")

    def test_invalid_repair_budget_is_rejected_at_construction(self):
        for value in (-1, 4, True, "2"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.assistant(SequenceBackend(), max_repair_attempts=value)


if __name__ == "__main__":
    unittest.main()
