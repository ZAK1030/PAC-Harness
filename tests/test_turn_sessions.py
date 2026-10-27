from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from examples.list_sorting import ListEnvironment, DemoDetector
from pac_harness.agents import Agent, ChatClient
from pac_harness.runtime import Harness
from pac_harness.storage import Journal, loads, dumps
from pac_harness.tools import Knowledge, Tool, parameters


class Client:
    def __init__(self, replies):
        self.replies, self.messages = iter(replies), []

    def complete(self, messages):
        self.messages.append(deepcopy(messages))
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return reply


class TurnSessionTests(unittest.TestCase):
    def test_detector_json_repair_does_not_replay_completed_action(self):
        from io import BytesIO
        from examples.list_sorting import DemoPlanner
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = ListEnvironment(root/'run', [2, 1])
            outputs = iter(['{"status":', dumps({'status': 'verified', 'reason': 'sorted',
                'evidence': ['document'], 'task_complete': False, 'progress': 'improved', 'facts': {}})])
            def opener(request, timeout):
                return BytesIO(dumps({'status': 'completed', 'output': [{'type': 'message',
                    'content': [{'type': 'output_text', 'text': next(outputs)}]}]}).encode())
            client = ChatClient({'model': 'test', 'wire_api': 'responses', 'api_key_env': ''}, opener=opener)
            result = Harness(root, env, DemoPlanner(), Agent('detector', client, Knowledge(root)),
                identity={}, output=lambda _: None).run('sort', root/'run', max_turns=1)
            self.assertEqual(result['status'], 'budget_exhausted')
            events = [loads(line) for line in (root/'run/events.jsonl').read_text().splitlines()]
            self.assertEqual(sum(e['event']=='action_result' for e in events), 1)
            self.assertEqual(sum(e['event']=='turn_completed' for e in events), 1)
            repair = next(e for e in events if e['event']=='format_repair')
            self.assertEqual(repair['role'], 'detector')
            self.assertFalse(any(e['event']=='retry' for e in events))

    def test_malformed_json_repaired_before_one_action_dispatch(self):
        from io import BytesIO
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = ListEnvironment(root/'run', [2, 1])
            action = env.actions()[0]
            key = next(iter(action['parameters']['properties']))
            plan = {'reasoning': 'sort', 'remaining_plan': [],
                    'action': {'name': action['name'], 'arguments': {key: [1, 2]}}}
            outputs = iter(['{"action":', dumps(plan)])
            def opener(request, timeout):
                return BytesIO(dumps({'status': 'completed', 'output': [{'type': 'message',
                    'content': [{'type': 'output_text', 'text': next(outputs)}]}]}).encode())
            client = ChatClient({'model': 'test', 'wire_api': 'responses', 'api_key_env': ''}, opener=opener)
            result = Harness(root, env, Agent('planner', client, Knowledge(root)), DemoDetector(),
                identity={}, output=lambda _: None).run('sort', root/'run', max_turns=1)
            self.assertEqual(result['status'], 'budget_exhausted')
            events = [loads(line) for line in (root/'run/events.jsonl').read_text().splitlines()]
            self.assertEqual(sum(e['event']=='action_pending' for e in events), 1)
            self.assertEqual(sum(e['event']=='action_result' for e in events), 1)
            self.assertFalse(any(e['event']=='retry' for e in events))
            self.assertEqual({e['turn'] for e in events}, {1})

    def test_tools_format_repair_pre_send_retry_and_post_share_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            class Environment(ListEnvironment):
                reads = sends = 0
                rejected = False
                def tools(self):
                    def read():
                        self.reads += 1
                        return {"unique_evidence": "retained"}
                    return [Tool("lookup", "read", parameters(), read)]
                def validate(self, action, observation):
                    if not self.rejected:
                        self.rejected = True
                        raise ValueError("Reconsider the first proposal")
                    return super().validate(action, observation)
                def execute(self, action, request_id):
                    self.sends += 1
                    return super().execute(action, request_id)
            env = Environment(root / 'run', [2, 1])
            action = {"reasoning": "sort", "remaining_plan": [],
                      "action": {"name": "replace", "arguments": {"values": [1, 2]}}}
            # Read actual sample schema so this regression exercises the real adapter.
            action["action"]["name"] = env.actions()[0]["name"]
            key = next(iter(env.actions()[0]["parameters"]["properties"]))
            action["action"]["arguments"] = {key: [1, 2]}
            client = Client([{"tool_calls": [{"name": "lookup", "arguments": {}}]},
                             {"reasoning": "need an action"}, action, action])
            planner = Agent("planner", client, Knowledge(root))
            class RetryDetector(DemoDetector):
                calls = 0
                def respond(self, *args):
                    self.calls += 1
                    if self.calls == 1:
                        raise TimeoutError('temporary post failure')
                    return super().respond(*args)
            harness = Harness(root, env, planner, RetryDetector(), identity={}, output=lambda _: None)
            result = harness.run('sort', root / 'run', max_turns=1)
            self.assertEqual(result['status'], 'budget_exhausted')
            self.assertEqual((env.reads, env.sends), (1, 1))
            self.assertIn('unique_evidence', str(client.messages[-1]))
            self.assertIn('Reconsider', str(client.messages[-1]))
            events = Journal(root / 'run').search(limit=100)
            self.assertTrue(any(e['event'] == 'format_repair' for e in events))
            self.assertEqual({e['turn'] for e in events}, {1})
            self.assertEqual(harness.state.data['turn'], 1)
            self.assertFalse(harness.state.data['turn_open'])

    def test_transport_retry_keeps_images_and_budget_new_turn_resets(self):
        from pac_harness.tools import Toolbox, ToolResult
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            box = Toolbox()
            image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,fixture"}}
            box.register(Tool('image', 'read', parameters(), lambda: ToolResult({'id': 'frame1'}, [image])))
            call = {"tool_calls": [{"name": "image", "arguments": {}}]}
            client = Client([call, TimeoutError(), {'answer': 1}, {'answer': 2}])
            agent = Agent('planner', client, Knowledge(root), max_tool_rounds=1)
            journal = Journal(root / 'run')
            with self.assertRaises(TimeoutError):
                agent.respond({'task_state': {'turn': 1}}, box, journal)
            agent.respond({'task_state': {'turn': 1}}, box, journal)
            self.assertIn('base64,fixture', str(client.messages[-1]))
            self.assertIn('budget exhausted', str(client.messages[-1]))
            agent.respond({'task_state': {'turn': 2}}, box, journal)
            self.assertNotIn('base64,fixture', str(client.messages[-1]))

    def test_legacy_log_is_append_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Journal(tmp)
            old = b'{"event":"old","turn":17}\n'
            journal.path.write_bytes(old)
            journal.turn = 18
            journal.event('tool')
            self.assertTrue(journal.path.read_bytes().startswith(old))
            self.assertEqual(loads(journal.path.read_text().splitlines()[-1])['turn'], 18)
