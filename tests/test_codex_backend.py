import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from pac_harness.codex_backend import CodexBackend, RESPONSE_SCHEMA, ToUserError, validate_reply


class CodexBackendTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.addCleanup(patch.stopall)
        patch("pac_harness.codex_backend.find_codex", return_value="codex").start()
        patch("pac_harness.codex_backend.model_connection", return_value=([], {}, {"model": "test"})).start()

    def test_codex_exec_continues_same_assistance_thread(self):
        commands = []
        def runner(command, **kwargs):
            commands.append(command)
            reply = {"status": "reply", "intent": "analysis", "message": "analysis", "summary": "result", "session_guidance": ""}
            events = [{"type": "thread.started", "thread_id": "thread-123"},
                      {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(reply)}}]
            return SimpleNamespace(returncode=0, stdout="\n".join(map(json.dumps, events)), stderr="")
        backend = CodexBackend(runner=runner)
        backend.reply(self.root, "prompt", audit=self.root)
        backend.reply(self.root, "followup", audit=self.root)
        self.assertIn("--output-schema", commands[0])
        self.assertIn("sandbox_workspace_write.network_access=false", commands[0])
        self.assertIn("resume", commands[1])
        self.assertIn("thread-123", commands[1])

    def test_invalid_structured_response_is_rejected(self):
        event = {"type": "item.completed", "item": {"type": "agent_message", "text": "not JSON"}}
        backend = CodexBackend(runner=lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(event), stderr=""))
        with self.assertRaises(ToUserError):
            backend.reply(self.root, "prompt", audit=self.root)

    def test_missing_or_malformed_intent_is_rejected_by_cli_backend(self):
        valid = {"status": "reply", "intent": "analysis", "message": "answer",
                 "summary": "result", "session_guidance": ""}
        missing = {key: value for key, value in valid.items() if key != "intent"}
        replies = [missing, *[{**valid, "intent": value}
                             for value in (None, 1, [], {}, "execute", "Analysis")]]
        for reply in replies:
            with self.subTest(reply=reply):
                event = {"type": "item.completed", "item": {
                    "type": "agent_message", "text": json.dumps(reply)}}
                backend = CodexBackend(runner=lambda *args, **kwargs: SimpleNamespace(
                    returncode=0, stdout=json.dumps(event), stderr=""))
                with self.assertRaises(ToUserError):
                    backend.reply(self.root, "prompt", audit=self.root)

    def test_each_supported_intent_survives_cli_response(self):
        for intent in RESPONSE_SCHEMA["properties"]["intent"]["enum"]:
            with self.subTest(intent=intent):
                reply = {"status": "edited" if intent == "persistent_update" else "reply",
                         "intent": intent, "message": "answer", "summary": "result",
                         "session_guidance": "step guidance" if intent == "task_guidance" else ""}
                event = {"type": "item.completed", "item": {
                    "type": "agent_message", "text": json.dumps(reply)}}
                backend = CodexBackend(runner=lambda *args, **kwargs: SimpleNamespace(
                    returncode=0, stdout=json.dumps(event), stderr=""))
                self.assertEqual(backend.reply(self.root, "prompt", audit=self.root), reply)

    def test_shared_validator_rejects_malformed_injected_backend_responses(self):
        valid = {"status": "reply", "intent": "analysis", "message": "answer",
                 "summary": "result", "session_guidance": ""}
        invalid = [None, [], "reply", {**valid, "unexpected": "field"},
                   {**valid, "status": "applied"}]
        for key in valid:
            invalid.extend(({name: value for name, value in valid.items() if name != key},
                            {**valid, key: []}, {**valid, key: None}))
        for reply in invalid:
            with self.subTest(reply=reply):
                with self.assertRaises(ToUserError):
                    validate_reply(reply)

    def test_validator_does_not_accept_zero_tests(self):
        backend = CodexBackend(runner=lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="TO_USER_TEST_COUNT=0", stderr=""))
        with self.assertRaises(ToUserError):
            backend.validate(self.root, audit=self.root)

    def test_validator_runs_fixed_offline_command(self):
        commands = []
        def runner(command, **kwargs):
            commands.append(command)
            self.assertEqual(kwargs["env"]["HARNESS_OFFLINE"], "1")
            self.assertFalse(kwargs["shell"])
            return SimpleNamespace(returncode=0, stdout="TO_USER_TEST_COUNT=3", stderr="")
        result = CodexBackend(runner=runner).validate(self.root, audit=self.root)
        self.assertTrue(result["passed"])
        self.assertTrue("permissions.harness_to_user.network.enabled=false" in commands[0] or "--unshare-net" in commands[0])

    def test_linux_settings_do_not_emit_windows_sandbox_option(self):
        with patch("pac_harness.codex_backend.os.name", "posix"):
            settings = CodexBackend()._settings()
        self.assertNotIn('windows.sandbox="unelevated"', settings)

    def test_linux_validator_uses_fixed_python_command(self):
        commands = []
        def runner(command, **kwargs):
            commands.append(command)
            return SimpleNamespace(returncode=0, stdout="TO_USER_TEST_COUNT=2", stderr="")
        with patch("pac_harness.codex_backend.os.name", "posix"), patch("pac_harness.codex_backend.shutil.which", return_value="bwrap"):
            result = CodexBackend(runner=runner).validate(self.root, audit=self.root)
        self.assertTrue(result["passed"])
        self.assertEqual(commands[0][0], "bwrap")
        self.assertIn("--unshare-net", commands[0])
        self.assertIn(__import__("sys").executable, commands[0])
        self.assertNotIn("permission-profile", commands[0])


if __name__ == "__main__":
    unittest.main()
