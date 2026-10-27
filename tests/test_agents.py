from pathlib import Path
import tempfile
import unittest

from pac_harness.agents import Agent, ChatClient, ModelError
from pac_harness.model_response import ModelOutputError
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
        (self.root / "private.txt").write_text("private", encoding="utf-8")
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

class ResponsesTests(unittest.TestCase):
    def response(self, status='completed'):
        from io import BytesIO
        from pac_harness.storage import dumps
        return BytesIO(dumps({'status': status, 'output': [
            {'type': 'reasoning', 'summary': []},
            {'type': 'message', 'content': [{'type': 'output_text', 'text': '{"ok":true}'}]}]}).encode())

    def test_responses_protocol_images_and_privacy(self):
        captured = []
        def opener(request, timeout):
            captured.append(request)
            return self.response()
        client = ChatClient({'model': 'gpt-5.5', 'wire_api': 'responses', 'api_key_env': '',
                             'reasoning_effort': 'high'}, opener=opener)
        reply = client.complete([{'role': 'user', 'content': [
            {'type': 'text', 'text': 'Return JSON'},
            {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,TEST'}}]}])
        self.assertTrue(reply['ok'])
        body = loads(captured[0].data)
        self.assertFalse(body['store'])
        self.assertEqual(body['reasoning'], {'effort': 'high'})
        self.assertEqual(body['input'][0]['content'][1]['type'], 'input_image')
        self.assertEqual(body['text']['format']['type'], 'json_object')
        self.assertTrue(captured[0].full_url.endswith('/responses'))

    def test_incomplete_responses_rejected(self):
        client = ChatClient({'model': 'test', 'wire_api': 'responses', 'api_key_env': '', 'max_transport_retries': 0},
                            opener=lambda *a, **k: self.response('incomplete'))
        with self.assertRaises(ModelError):
            client.complete([])


class ResponseRecoveryTests(unittest.TestCase):
    def envelope(self, text='{"ok":true}', **extra):
        from pac_harness.storage import dumps
        value = {'status': 'completed', 'id': 'response1', 'model': 'test',
                 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': text}]}], **extra}
        return dumps(value).encode()

    def client(self, replies, **settings):
        from io import BytesIO
        replies = iter(replies)
        self.requests, self.delays, self.events = [], [], []
        def opener(request, timeout):
            self.requests.append(request)
            item = next(replies)
            if isinstance(item, Exception):
                raise item
            return BytesIO(item)
        return ChatClient({'model': 'test', 'wire_api': 'responses', 'api_key_env': '', **settings},
                          opener=opener, sleeper=self.delays.append)

    def event(self, name, **fields):
        self.events.append((name, fields))

    def test_attempt_usage_is_logged_for_valid_and_invalid_json(self):
        usage = {'input_tokens': 100, 'output_tokens': 50, 'total_tokens': 150,
                 'input_tokens_details': {'cached_tokens': 20},
                 'output_tokens_details': {'reasoning_tokens': 30}}
        for text in ('{"ok":true}', '{'):
            with self.subTest(text=text):
                client = self.client([self.envelope(text, usage=usage)])
                try:
                    client.complete([], on_event=self.event)
                except ModelOutputError:
                    pass
                starts = [f['diagnostics'] for e, f in self.events if e == 'model_attempt_start']
                ends = [f['diagnostics'] for e, f in self.events if e == 'model_attempt_end']
                self.assertEqual(len(starts), 1)
                self.assertEqual(len(ends), 1)
                self.assertEqual(starts[0]['attempt_id'], ends[0]['attempt_id'])
                self.assertEqual(ends[0]['usage'], usage)
                self.assertGreaterEqual(ends[0]['elapsed_s'], 0)
                self.assertEqual(ends[0]['outcome'], 'completed' if text.endswith('}') else 'failed')

    def test_interrupted_transport_attempt_has_timing_and_unknown_usage(self):
        client = self.client([KeyboardInterrupt()])
        # The scripted opener returns Exception instances, so inject directly.
        def opener(*args, **kwargs):
            raise KeyboardInterrupt()
        client.opener = opener
        with self.assertRaises(KeyboardInterrupt):
            client.complete([], on_event=self.event)
        ends = [f['diagnostics'] for e, f in self.events if e == 'model_attempt_end']
        self.assertEqual(ends[0]['outcome'], 'interrupted')
        self.assertIn('elapsed_s', ends[0])
        self.assertNotIn('usage', ends[0])

    def test_html_200_retried_inside_request_and_diagnosed(self):
        client = self.client([b'<html>upstream unavailable</html>', self.envelope()])
        self.assertEqual(client.complete([], on_event=self.event), {'ok': True})
        self.assertEqual(self.delays, [1])
        diagnostics = next(fields['diagnostics'] for event, fields in self.events if event == 'model_retry')
        self.assertEqual(diagnostics['stage'], 'http_body')
        self.assertIn('<html>', diagnostics['response_preview'])
        self.assertEqual(len(self.requests), 2)
        self.assertFalse(loads(self.requests[0].data)['stream'])

    def test_transport_retry_bound_and_auth_failure_not_retried(self):
        client = self.client([b'', b'', b''])
        with self.assertRaises(ModelError) as caught:
            client.complete([], on_event=self.event)
        self.assertEqual(caught.exception.stage, 'http_body')
        self.assertEqual(self.delays, [1, 2])
        from urllib.error import HTTPError
        client = self.client([HTTPError('https://test', 401, 'unauthorized', {}, None)])
        with self.assertRaisesRegex(ModelError, 'HTTP 401'):
            client.complete([])
        self.assertEqual(self.delays, [])

    def test_sse_completed_response_accepted_but_partial_deltas_rejected(self):
        from pac_harness.storage import dumps
        event = dumps({'type': 'response.completed', 'response': loads(self.envelope())})
        raw = ('event: response.completed\ndata: '+event+'\n\ndata: [DONE]\n\n').encode()
        self.assertTrue(self.client([raw]).complete([])['ok'])
        partial = b'event: response.output_text.delta\ndata: {"delta":"{\\"ok\\":true}"}\n\n'
        with self.assertRaisesRegex(ModelError, 'without a final response'):
            self.client([partial], max_transport_retries=0).complete([])

    def test_requested_stream_stops_at_terminal_event_and_rejects_partial_output(self):
        from io import BytesIO
        from pac_harness.storage import dumps
        event = dumps({'type': 'response.completed', 'response': loads(self.envelope())})
        raw = ('event: response.output_text.delta\ndata: {"delta":"partial"}\n\n'
               'event: response.completed\ndata: ' + event + '\n\n').encode()
        class StreamResponse(BytesIO):
            headers = {'content-type': 'text/event-stream'}
            status = 200
            def read(self, *args):
                raise AssertionError('Must read streaming events incrementally')
            def readline(self, *args):
                if self.tell() == len(raw):
                    raise AssertionError('Must not wait for connection close after completion')
                return super().readline(*args)
        captured = []
        def opener(request, timeout):
            captured.append(request)
            return StreamResponse(raw)
        client = ChatClient({'model': 'test', 'wire_api': 'responses', 'api_key_env': '', 'stream': True},
                            opener=opener)
        self.assertEqual(client.complete([]), {'ok': True})
        self.assertTrue(loads(captured[0].data)['stream'])
        partial = BytesIO(b'event: response.output_text.delta\ndata: {"delta":"{}"}\n\n')
        partial.headers = {'content-type': 'text/event-stream'}
        client = ChatClient({'model': 'test', 'wire_api': 'responses', 'api_key_env': '',
                             'stream': True, 'max_transport_retries': 0}, opener=lambda *a, **k: partial)
        with self.assertRaisesRegex(ModelError, 'without a final response'):
            client.complete([])

    def test_requested_stream_does_not_accept_failed_or_incomplete_terminal_response(self):
        from io import BytesIO
        from pac_harness.storage import dumps
        for status in ('failed', 'incomplete'):
            response = loads(self.envelope(status=status))
            raw = ('event: response.' + status + '\ndata: ' + dumps(
                {'type': 'response.' + status, 'response': response}) + '\n\n').encode()
            stream = BytesIO(raw)
            stream.headers = {'content-type': 'text/event-stream'}
            client = ChatClient({'model': 'test', 'wire_api': 'responses', 'api_key_env': '',
                                 'stream': True, 'max_transport_retries': 0}, opener=lambda *a, **k: stream)
            with self.subTest(status=status), self.assertRaisesRegex(ModelError, 'incomplete'):
                client.complete([])

    def test_stream_upstream_error_is_diagnosed_as_provider_failure(self):
        raw = b'event: error\ndata: {"type":"error","code":"upstream_error","message":"Upstream service temporarily unavailable"}\n\n'
        client = self.client([raw], max_transport_retries=0)
        with self.assertRaises(ModelError) as caught:
            client.complete([])
        self.assertEqual(caught.exception.stage, 'provider_error')
        self.assertTrue(caught.exception.retryable)
        self.assertIn('Upstream service temporarily unavailable', caught.exception.diagnostics['provider_error'])

    def test_complete_markdown_fence_and_bom_are_unwrapped(self):
        client = self.client([self.envelope('\ufeff```json\n{"ok":true}\n```')])
        self.assertTrue(client.complete([])['ok'])

    def test_invalid_output_is_repaired_with_original_tool_evidence(self):
        from pac_harness.storage import dumps
        raw_tool = self.envelope(dumps({'tool_calls': [{'name': 'lookup', 'arguments': {}}]}))
        client = self.client([raw_tool, self.envelope('{"ok":'), self.envelope('{"ok":true}')])
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            box = Toolbox()
            reads = []
            box.register(Tool('lookup', 'read', parameters(), lambda: reads.append(1) or {'value': 42}))
            journal = Journal(root/'run')
            agent = Agent('planner', client, Knowledge(root))
            self.assertTrue(agent.respond({}, box, journal)['ok'])
            self.assertEqual(reads, [1])
            last = loads(self.requests[-1].data)['input']
            self.assertIn('"value": 42', str(last))
            self.assertIn('could not be decoded', last[-1]['content'])
            events = [loads(line) for line in journal.path.read_text(encoding="utf-8").splitlines()]
            self.assertTrue(any(e['event']=='format_repair' for e in events))
            error = next(e for e in events if e['event']=='model_error')
            self.assertEqual(error['diagnostics']['stage'], 'model_output')

    def test_empty_model_output_diagnosed_and_not_confused_with_http_body(self):
        client = self.client([self.envelope('')])
        with self.assertRaises(ModelOutputError) as caught:
            client.complete([])
        self.assertEqual(caught.exception.diagnostics['stage'], 'model_output')
        self.assertEqual(caught.exception.diagnostics['parse_error'], 'empty output')
        self.assertEqual(self.delays, [])

    def test_duplicate_fields_and_multiple_objects_never_accepted(self):
        for text in ('{"ok":true,"ok":false}', '{} {}', 'Result: {"ok":true}'):
            with self.subTest(text=text), self.assertRaises(ModelOutputError):
                self.client([self.envelope(text)]).complete([])

    def test_completed_native_read_calls_are_normalized(self):
        raw = self.envelope('', output=[{'type':'function_call', 'name':'lookup', 'arguments':'{}'}])
        self.assertEqual(self.client([raw]).complete([]),
                         {'tool_calls': [{'name':'lookup', 'arguments':{}}]})

    def test_refusal_and_truncated_output_cannot_be_parsed_as_action(self):
        raw = self.envelope('', output=[{'type':'message', 'content':[{'type':'refusal','refusal':'No'}]}])
        with self.assertRaises(ModelError) as caught:
            self.client([raw]).complete([])
        self.assertEqual(caught.exception.stage, 'refusal')
        raw = self.envelope('{"ok":true}', status='incomplete', incomplete_details={'reason':'max_output_tokens'})
        with self.assertRaises(ModelError) as caught:
            self.client([raw], max_transport_retries=0).complete([])
        self.assertEqual(caught.exception.stage, 'response_status')

    def test_invalid_output_repair_is_bounded(self):
        client = self.client([self.envelope('{')]*3)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            with self.assertRaisesRegex(ModelError, 'JSON repair budget exhausted'):
                Agent('planner', client, Knowledge(root)).respond({}, Toolbox(), Journal(root/'run'))
        self.assertEqual(len(self.requests), 3)

    def test_error_diagnostics_redact_secret_and_image_bytes(self):
        from unittest.mock import patch
        raw = b'<html>secret-value Bearer TOKEN sk-user-xyz data:image/png;base64,AAAA=</html>'
        client = self.client([raw], api_key_env='TEST_KEY', max_transport_retries=0)
        with patch.dict('os.environ', {'TEST_KEY': 'secret-value'}), self.assertRaises(ModelError) as caught:
            client.complete([])
        text = str(caught.exception.diagnostics)
        for value in ('secret-value', 'TOKEN', 'sk-user-xyz', 'AAAA='):
            self.assertNotIn(value, text)


if __name__ == "__main__":
    unittest.main()
