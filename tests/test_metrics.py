import json
from pathlib import Path
import tempfile
import unittest

from pac_harness.metrics import export_metrics, token_usage


class MetricsTests(unittest.TestCase):
    def test_chat_and_responses_usage_are_normalized_without_double_counting(self):
        usage = token_usage({'prompt_tokens': 100, 'completion_tokens': 50, 'total_tokens': 150,
            'prompt_tokens_details': {'cached_tokens': 20},
            'completion_tokens_details': {'reasoning_tokens': 30}})
        self.assertEqual(usage['input_tokens'], 100)
        self.assertEqual(usage['total_tokens'], 150)
        self.assertEqual(usage['reasoning_tokens'], 30)
        self.assertIsNone(token_usage(None)['total_tokens'])

    def test_missing_usage_stays_unknown_and_attempt_events_are_counted_once(self):
        with tempfile.TemporaryDirectory() as folder:
            batch = Path(folder)
            episode = batch/'suite/task-000-episode-000'
            episode.mkdir(parents=True)
            usage = {'input_tokens': 100, 'output_tokens': 50, 'total_tokens': 150}
            events = [
                {'time_ns': 1, 'event': 'model_response', 'role': 'planner', 'turn': 1,
                 'diagnostics': {'response_id': 'old', 'elapsed_s': 2}},
                {'time_ns': 2, 'event': 'model_attempt_start', 'role': 'planner', 'turn': 2,
                 'diagnostics': {'attempt_id': 'new', 'started_at_ns': 2}},
                {'time_ns': 3, 'event': 'model_response', 'role': 'planner', 'turn': 2,
                 'diagnostics': {'attempt_id': 'new', 'usage': usage}},
                {'time_ns': 4, 'event': 'model_attempt_end', 'role': 'planner', 'turn': 2,
                 'diagnostics': {'attempt_id': 'new', 'usage': usage, 'elapsed_s': 3, 'outcome': 'completed'}},
                {'time_ns': 5, 'event': 'model_error', 'role': 'planner', 'turn': 2,
                 'diagnostics': {'attempt_id': 'new', 'usage': usage}}]
            (episode/'events.jsonl').write_text('\n'.join(json.dumps(e) for e in events)+'\n')
            summary = export_metrics(batch)
            self.assertEqual(summary['observed_attempts'], 2)
            self.assertEqual(summary['total_tokens_reported_sum'], 150)
            self.assertEqual(summary['total_tokens_missing_attempts'], 1)
            self.assertIsNone(summary['total_tokens'])
            self.assertEqual(summary['model_request_s_recorded'], 5)
            self.assertTrue((batch/'metrics/episodes.csv').is_file())
            self.assertEqual(export_metrics(batch)['total_tokens_reported_sum'], 150)


if __name__ == '__main__':
    unittest.main()
