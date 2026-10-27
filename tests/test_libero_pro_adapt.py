from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from adapters.libero import PRO_SUITES
from pac_harness.libero_pro_adapt import run_curriculum, select_episodes, validate_episodes
from pac_harness.storage import loads, write_json


def benchmark():
    return {"tasks": [{"suite": suite, "task_id": task, "available_initial_states": 50,
                       "instruction": f"task {task}"} for suite in PRO_SUITES for task in range(10)]}


class LiberoProAdaptTests(unittest.TestCase):
    def test_sampling_is_reproducible_balanced_and_disjoint(self):
        episodes = select_episodes(benchmark(), 1234)
        self.assertEqual(episodes, select_episodes(benchmark(), 1234))
        self.assertNotEqual(episodes, select_episodes(benchmark(), 4321))
        self.assertEqual(len(episodes), 30)
        self.assertTrue(all(3 <= e["init_state_id"] < 50 for e in episodes))
        for suite in PRO_SUITES:
            self.assertEqual(len({e["task_id"] for e in episodes if e["suite"] == suite}), 5)
        episodes[0]["init_state_id"] = 2
        with self.assertRaisesRegex(ValueError, "overlaps"):
            validate_episodes(episodes)

    def test_each_review_finishes_before_next_experiment_and_resume_skips_both(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "adapt"
            data = benchmark()
            plan = {"benchmark": data, "episodes": select_episodes(data, 12), "simulator_seed": 0}
            write_json(directory / "config.json", {})
            order = []

            def child(call, **kwargs):
                if call[3] == "pac_harness.libero_eval":
                    self.assertEqual(len(order) % 2, 0)
                    item = plan["episodes"][len(order) // 2]
                    target = directory / item["episode_dir"]
                    write_json(target / "result.json", {"success": False, "steps": 10,
                               "runtime": {"status": "episode_exhausted"}})
                    write_json(target.parent / "summary.json", {})
                    order.append("experiment")
                else:
                    self.assertEqual(order[-1], "experiment")
                    config_arg = call[call.index("--config") + 1]
                    self.assertFalse(Path(config_arg).is_absolute())
                    # Exercise the real Analyzer config-path contract, rather
                    # than allowing the mocked process to accept any path.
                    from pac_harness.storage import confined
                    self.assertEqual(loads(confined(root, config_arg).read_text(encoding="utf-8")), {})
                    target = Path(call[call.index("--run-dir") + 1])
                    write_json(target / "review/report.json", {"status": "applied", "lesson_keys": ["rule"]})
                    order.append("review")
                return SimpleNamespace(returncode=0)

            with patch("pac_harness.libero_pro_adapt.preflight", return_value=data), patch(
                    "pac_harness.libero_pro_adapt.subprocess.run", side_effect=child), patch("builtins.print"):
                self.assertEqual(run_curriculum(root, directory, plan), 0)
                self.assertEqual(order, ["experiment", "review"] * 30)
                self.assertEqual(run_curriculum(root, directory, plan), 0)
                self.assertEqual(len(order), 60)
            status = loads((directory / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["reviews_completed"], 30)
            self.assertEqual(status["status"], "completed")
            self.assertTrue((directory / "knowledge-final/sha256.json").exists())

    def test_failed_review_preserves_state_and_stops_before_next_episode(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "adapt"
            data = benchmark()
            plan = {"benchmark": data, "episodes": select_episodes(data, 12), "simulator_seed": 0}
            write_json(directory / "config.json", {})
            calls = []

            def child(call, **kwargs):
                calls.append(call)
                target = directory / plan["episodes"][0]["episode_dir"]
                if call[3] == "pac_harness.libero_eval":
                    write_json(target / "result.json", {"success": False, "steps": 10,
                               "runtime": {"status": "episode_exhausted"}})
                    write_json(target.parent / "summary.json", {})
                    return SimpleNamespace(returncode=0)
                write_json(target / "review/report.json", {"status": "failed", "stage": "memory_compaction",
                           "error": "Model HTTP 500", "diagnostics": {"stage": "http_status", "http_status": 500}})
                return SimpleNamespace(returncode=1)

            with patch("pac_harness.libero_pro_adapt.preflight", return_value=data), patch(
                    "pac_harness.libero_pro_adapt.subprocess.run", side_effect=child), patch("builtins.print"), patch(
                    "pac_harness.libero_pro_adapt.time.sleep"):
                with self.assertRaisesRegex(RuntimeError, "Analyzer incomplete"):
                    run_curriculum(root, directory, plan)
            self.assertEqual(len(calls), 6)
            status = loads((directory / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["experiments_finished"], 1)
            self.assertEqual(status["reviews_completed"], 0)
            self.assertEqual(status["status"], "failed")
            self.assertFalse((directory / "knowledge-final").exists())


if __name__ == "__main__":
    unittest.main()
