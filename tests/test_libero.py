from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from adapters.libero import LiberoEnvironment
from pac_harness.libero_eval import SuccessDetector
from pac_harness.storage import loads, write_json


class FakeSim:
    def get_state(self):
        return self
    def flatten(self):
        return self
    def tolist(self):
        return [42]


class FakeEnv:
    def __init__(self, success_after=99, fail=False):
        self.calls = 0
        self.success_after = success_after
        self.fail = fail
        self.env = SimpleNamespace(sim=FakeSim())
    def step(self, vector):
        self.calls += 1
        if self.fail:
            raise RuntimeError('simulator interrupted')
        return {}, 0, False, {}
    def check_success(self):
        return self.calls >= self.success_after


class LiberoTests(unittest.TestCase):
    def make(self, directory, **kwargs):
        adapter = LiberoEnvironment.__new__(LiberoEnvironment)
        adapter.path = Path(directory) / 'state.json'
        adapter.pending = Path(directory) / 'pending.json'
        adapter.data = {'steps': 0, 'success': False, 'receipts': {}}
        adapter.max_steps = 3
        adapter.chunk_size = 10
        adapter.env = FakeEnv(**kwargs)
        adapter.observe = lambda: {'success': adapter.data['success'], 'truncated': adapter.data['steps'] >= adapter.max_steps}
        return adapter

    def action(self, n=1):
        return {'name': 'robot_action', 'arguments': {'actions': [[0]*7 for _ in range(n)]}}

    def test_invalid_actions(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = self.make(directory)
            for vectors in ([], [[0]*6], [[float('nan')]*7], [[2]*7], [[True]*7], [[0]*7]*11):
                with self.assertRaises(ValueError):
                    adapter.validate({'name': 'robot_action', 'arguments': {'actions': vectors}}, adapter.observe())

    def test_stops_on_success_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = self.make(directory, success_after=2)
            result = adapter.execute(self.action(5), 'one')
            self.assertTrue(result['success'])
            self.assertEqual(result['executed_steps'], 2)
            self.assertEqual(adapter.execute(self.action(5), 'one'), result)
            self.assertEqual(adapter.env.calls, 2)
            self.assertEqual(adapter.reconcile('one')['status'], 'completed')

    def test_horizon(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = self.make(directory)
            self.assertEqual(adapter.execute(self.action(5), 'one')['executed_steps'], 3)
            self.assertFalse(adapter.data['success'])

    def test_terminal_success_is_authoritative_even_at_horizon(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = self.make(directory, success_after=2)
            self.assertIsNone(adapter.terminal_result())
            adapter.execute(self.action(5), 'one')
            adapter.max_steps = adapter.data['steps']
            result = adapter.terminal_result()
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(result['reason'], 'task_success')
            self.assertEqual(result['completion_source'], 'libero.check_success')
            self.assertEqual(result['steps'], 2)

    def test_terminal_horizon_without_success_remains_exhausted(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = self.make(directory)
            adapter.execute(self.action(5), 'one')
            self.assertEqual(adapter.terminal_result()['status'], 'episode_exhausted')

    def test_unknown_outcome_never_replayed(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = self.make(directory, fail=True)
            with self.assertRaises(RuntimeError):
                adapter.execute(self.action(), 'one')
            self.assertEqual(adapter.reconcile('one')['status'], 'unknown')
            self.assertTrue(adapter.execute(self.action(), 'two')['outcome_unknown'])
            self.assertEqual(adapter.env.calls, 1)

    def test_detector_uses_predicate_not_claim(self):
        report = SuccessDetector().respond({'after': {'success': False}, 'action': {'name': 'done'}}, None, None)
        self.assertFalse(report['task_complete'])

    def test_batch_resume_keeps_finished_results_and_resumes_interrupted_episode(self):
        from pac_harness.libero_eval import main
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / 'logs/batch'
            config = {'adapter': {'settings': {'max_steps': 99}},
                      'models': {'planner': {'model': 'test'}, 'detector': {'model': 'test'}}}
            write_json(root / 'config.json', config)
            records = []
            for task_id, status in [(0, 'completed'), (1, 'interrupted')]:
                record = {'task_id': task_id, 'episode': 0, 'success': True,
                          'runtime': {'status': status}}
                records.append(record)
                directory = output / f'task-{task_id:03d}-episode-000'
                write_json(directory / 'result.json', record)
                write_json(directory / 'task_state.json', {'turn': 80})
            write_json(output / 'summary.json', {'suite': 'libero_spatial', 'mode': 'evaluation',
                'seed': 4, 'detector': 'official', 'settings': {'max_steps': 500},
                'task_ids': [0, 1, 2], 'episodes_per_task': 1, 'planner': 'test', 'results': records})
            original = (output / 'task-000-episode-000/result.json').read_bytes()
            settings = []
            def create(**kwargs):
                settings.append(kwargs['config'])
                return SimpleNamespace(task=SimpleNamespace(language='place the bowl'),
                    max_steps=500, data={'success': True, 'steps': 222})
            with patch('pac_harness.libero_eval.load_suite', return_value=SimpleNamespace(n_tasks=3)), patch(
                    'pac_harness.libero_eval.load_init_states', return_value=[0]), patch(
                    'pac_harness.libero_eval.LiberoEnvironment', side_effect=create), patch(
                    'pac_harness.libero_eval.Harness.run', return_value={'status': 'completed'}) as run, patch(
                    'pac_harness.post_run.maybe_review', return_value=None), patch('builtins.print'):
                code = main(['--root', str(root), '--config', 'config.json', '--resume', 'logs/batch'])
            self.assertEqual(code, 0)
            self.assertEqual([x['task_id'] for x in settings], [1, 2])
            self.assertTrue(all(x['max_steps'] == 500 and x['seed'] == 4 for x in settings))
            self.assertEqual([c.kwargs['resume'] for c in run.call_args_list], [True, False])
            self.assertEqual((output / 'task-000-episode-000/result.json').read_bytes(), original)
            summary = loads((output / 'summary.json').read_text())
            self.assertEqual(summary['episodes'], 3)
            self.assertEqual(summary['successes'], 3)
            self.assertEqual([x['task_id'] for x in summary['results']], [0, 1, 2])

    def test_batch_resume_rejects_protocol_change(self):
        from pac_harness.libero_eval import main
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_json(root / 'logs/batch/summary.json', {'mode': 'evaluation', 'suite': 'libero_spatial',
                'task_ids': [0], 'episodes_per_task': 1, 'seed': 0, 'detector': 'model',
                'settings': {'max_steps': 500}})
            with patch('sys.stderr'), self.assertRaises(SystemExit):
                main(['--root', str(root), '--resume', 'logs/batch', '--max-steps', '200'])
