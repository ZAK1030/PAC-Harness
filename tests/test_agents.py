from pathlib import Path
import tempfile
import unittest

from pac_harness.agents import Agent, ChatClient, ModelError
from pac_harness.storage import Journal, loads
from pac_harness.tools import Knowledge, Tool, Toolbox, inspect_artifact, parameters


class ScriptClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.messages = []

    def complete(self, messages):
        self.messages.append(messages)
        return next(self.responses)


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.toolbox = Toolbox()
        self.toolbox.register(Tool("lookup", "Read data", parameters(), lambda: {"value": 42}))
        self.journal = Journal(self.root / "run")

    def test_tool_result_is_returned_to_agent_before_final(self):
        client = ScriptClient([{"tool_calls": [{"name": "lookup", "arguments": {}}]}, {"answer": 42}])
        agent = Agent("planner", client, Knowledge(self.root))
        self.assertEqual(agent.respond({}, self.toolbox, self.journal), {"answer": 42})
        self.assertIn('"value": 42', client.messages[1][-1]["content"])

    def test_tool_budget_stops_unbounded_information_loop(self):
        call = {"tool_calls": [{"name": "lookup", "arguments": {}}]}
        agent = Agent("detector", ScriptClient([call, call]), Knowledge(self.root), max_tool_rounds=1)
        with self.assertRaises(ModelError):
            agent.respond({}, self.toolbox, self.journal)

    def test_unknown_or_role_disallowed_tool_is_not_invoked(self):
        self.toolbox.register(Tool("planner_only", "read", parameters(), lambda: self.fail("invoked"), roles=("planner",)))
        with self.assertRaises(ValueError):
            self.toolbox.invoke("detector", "planner_only", {})
        with self.assertRaises(ValueError):
            self.toolbox.invoke("planner", "shell", {})

    def test_skill_path_cannot_escape_project(self):
        with self.assertRaises(ValueError):
            Knowledge(self.root).read_skill("../../outside")

    def test_image_is_sent_only_after_selected_read_and_not_logged_as_base64(self):
        import base64
        encoded = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6JUEAAAAASUVORK5CYII="
        (self.root / "sample.png").write_bytes(base64.b64decode(encoded))
        observations = [{"artifacts": [{"path": "sample.png"}]}]
        self.toolbox.register(Tool("inspect", "read image", parameters(), lambda: inspect_artifact(self.root, "sample.png", observations)))
        client = ScriptClient([{"tool_calls": [{"name": "inspect", "arguments": {}}]}, {"answer": "seen"}])
        Agent("detector", client, Knowledge(self.root)).respond({}, self.toolbox, self.journal)
        image = client.messages[1][-1]["content"][1]
        self.assertEqual(image["type"], "image_url")
        self.assertIn(encoded, image["image_url"]["url"])
        self.assertNotIn(encoded, self.journal.path.read_text(encoding="utf-8"))

    def test_unlisted_artifact_is_not_read(self):
        (self.root / "private.txt").write_text("private")
        with self.assertRaises(ValueError):
            inspect_artifact(self.root, "private.txt", [])

    def test_http_request_uses_env_secret_and_json_protocol(self):
        captured = []
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self):
                return b'{"choices":[{"finish_reason":"stop","message":{"content":"{\\"result\\":true}"}}]}'
        def opener(request, timeout):
            captured.append(request)
            return Response()
        from unittest.mock import patch
        with patch.dict("os.environ", {"TEST_MODEL_KEY": "test-value"}):
            client = ChatClient({"model": "test", "api_key_env": "TEST_MODEL_KEY"}, opener=opener)
            self.assertTrue(client.complete([{"role": "user", "content": "JSON please"}])["result"])
        self.assertEqual(captured[0].get_header("Authorization"), "Bearer test-value")
        self.assertEqual(loads(captured[0].data)["response_format"], {"type": "json_object"})

    def test_truncated_response_is_not_accepted(self):
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self):
                return b'{"choices":[{"finish_reason":"length","message":{"content":"{}"}}]}'
        client = ChatClient({"model": "test", "api_key_env": ""}, opener=lambda *args, **kwargs: Response())
        with self.assertRaises(ModelError):
            client.complete([])


if __name__ == "__main__":
    unittest.main()
