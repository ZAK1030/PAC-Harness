from collections import namedtuple
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from adapters.libero import LiberoEnvironment, PRO_SUITES
from pac_harness.libero_eval import main as evaluate
from pac_harness.libero_pro import main as evaluate_pro, summarize
from pac_harness.storage import loads, write_json


class LiberoProTests(unittest.TestCase):
    def protocol(self, mode="evaluation"):
        return {"mode": mode, "method": "test", "suites": list(PRO_SUITES),
                "task_ids": list(range(10)), "init_state_ids": [0, 1, 2]}

    def write_result(self, directory, suite, task, state, success, status="completed"):
        write_json(directory / suite / f"task-{task:03d}-episode-{state:03d}/result.json",
                   {"task_id": task, "init_state_id": state, "episode": state,
                    "success": success, "runtime": {"status": status}})

    def test_incomplete_and_interrupted_groups_have_no_reported_score(self):
        with tempfile.TemporaryDirectory() as temporary:
            p = Path(temporary)
            self.write_result(p, PRO_SUITES[0], 0, 0, True)
            self.write_result(p, PRO_SUITES[0], 0, 1, False, "interrupted")
            s = summarize(p, self.protocol())
            self.assertEqual(s["completed"], 1)
            self.assertEqual(s["expected"], 180)
            self.assertIsNone(s["success_rate"])
            self.assertIsNone(s["groups"][PRO_SUITES[0]]["success_rate"])
            self.assertNotIn("100.0", (p / "table.csv").read_text())

    def test_six_column_order_and_full_denominator(self):
        with tempfile.TemporaryDirectory() as temporary:
            p = Path(temporary)
            for suite, count in zip(PRO_SUITES, [24, 28, 21, 17, 22, 27]):
                for n in range(30):
                    self.write_result(p, suite, n // 3, n % 3, n < count)
            s = summarize(p, self.protocol())
            self.assertTrue(s["complete"])
            self.assertTrue(s["full_180_protocol"])
            self.assertEqual(s["successes"], 139)
            self.assertAlmostEqual(s["success_rate"], 139 / 180)
            self.assertIn("test,80.0,93.3,70.0,56.7,73.3,90.0,77.2", (p / "table.csv").read_text())
            # Identical artifacts cannot be turned into a model score in smoke mode.
            s = summarize(p, self.protocol("smoke"))
            self.assertIsNone(s["success_rate"])
            self.assertNotIn("77.2", (p / "table.csv").read_text())

    def test_sixty_single_state_configurations_have_their_own_average(self):
        with tempfile.TemporaryDirectory() as temporary:
            p = Path(temporary)
            protocol = {**self.protocol(), "init_state_ids": [0]}
            for suite_index, suite in enumerate(PRO_SUITES):
                for task in range(10):
                    self.write_result(p, suite, task, 0, suite_index * 10 + task < 45)
            s = summarize(p, protocol)
            self.assertEqual(s["expected"], 60)
            self.assertEqual(s["successes"], 45)
            self.assertEqual(s["success_rate"], 0.75)
            self.assertIsNone(s["k1_case_scope"])
            self.assertFalse(s["full_180_protocol"])
            self.assertEqual((p / "table.csv").read_text().splitlines()[1].split(",")[-1], "75.0")
            self.write_result(p, PRO_SUITES[0], 0, 0, False, "interrupted")
            s = summarize(p, protocol)
            self.assertIsNone(s["success_rate"])
            self.assertEqual((p / "table.csv").read_text().splitlines()[1].split(",")[-1], "—")

    def test_explicit_stored_state_ids_and_resume_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_json(root / "config.json", {"adapter": {"settings": {}},
                       "models": {"planner": {"model": "test"}, "detector": {"model": "test"}}})
            configs = []

            def create(**kwargs):
                configs.append(kwargs["config"])
                return SimpleNamespace(task=SimpleNamespace(language="changed task instruction"),
                                       max_steps=2400, data={"success": False, "steps": 2})

            with patch("pac_harness.libero_eval.load_suite", return_value=SimpleNamespace(n_tasks=10)), patch(
                    "pac_harness.libero_eval.load_init_states", return_value=list(range(50))), patch(
                    "pac_harness.libero_eval.LiberoEnvironment", side_effect=create), patch(
                    "pac_harness.libero_eval.Harness.run", return_value={"status": "episode_exhausted"}), patch(
                    "pac_harness.post_run.maybe_review", return_value=None), patch("builtins.print"):
                args = ["--root", str(root), "--config", "config.json", "--suite", PRO_SUITES[0],
                        "--task-ids", "0", "--init-state-ids", "2", "7", "--output", str(root / "batch")]
                self.assertEqual(evaluate(args), 0)
                self.assertEqual([c["init_state_id"] for c in configs], [2, 7])
                configs.clear()
                self.assertEqual(evaluate(["--root", str(root), "--config", "config.json", "--resume", "batch"]), 0)
                self.assertEqual(configs, [])
                with patch("sys.stderr"), self.assertRaises(SystemExit):
                    evaluate(["--root", str(root), "--resume", "batch", "--init-state-ids", "0", "1"])
            s = loads((root / "batch/summary.json").read_text())
            self.assertEqual(s["init_state_ids"], [2, 7])
            self.assertTrue((root / "batch/task-000-episode-007/result.json").exists())

    def test_k1_astra_subset_uses_eighteen_cases_and_its_own_average(self):
        with tempfile.TemporaryDirectory() as temporary:
            p = Path(temporary)
            protocol = {**self.protocol(), "task_ids": [0, 1, 2], "init_state_ids": [0]}
            for suite_index, suite in enumerate(PRO_SUITES):
                for task in range(3):
                    self.write_result(p, suite, task, 0, suite_index * 3 + task < 16)
            s = summarize(p, protocol)
            self.assertEqual(s["expected"], 18)
            self.assertEqual(s["successes"], 16)
            self.assertEqual(s["k1_case_scope"], "k1-18")
            self.assertFalse(s["full_180_protocol"])
            self.assertIn("88.9", (p / "table.csv").read_text())
            (p / PRO_SUITES[0] / "task-000-episode-000/result.json").unlink()
            s = summarize(p, protocol)
            self.assertIsNone(s["success_rate"])
            self.assertEqual((p / "table.csv").read_text().splitlines()[1].split(",")[-1], "—")

    def test_named_k1_profiles_freeze_cases_and_reject_incompatible_options(self):
        for name, tasks, states in (("k1-18", [0, 1, 2], [0]),
                                    ("k1-180", list(range(10)), [0, 1, 2])):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                write_json(root / "config.libero-pro.json", {"adapter": {"settings": {}},
                           "models": {"planner": {"model": "test"}}})
                with patch("pac_harness.libero_pro.preflight", return_value={"tasks": []}) as check, patch("builtins.print"):
                    self.assertEqual(evaluate_pro(["--root", str(root), "--protocol", name, "--check"]), 0)
                    self.assertEqual(check.call_args.args[1:], (list(PRO_SUITES), tasks, states))
                for conflicting in (["--task-ids", "4"], ["--init-state-ids", "9"],
                                    ["--post-run-review"], ["--smoke"]):
                    with patch("sys.stderr"), self.assertRaises(SystemExit):
                        evaluate_pro(["--root", str(root), "--protocol", name, *conflicting, "--check"])

    def test_named_k1_profile_is_saved_and_cannot_resume_a_different_case_set(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_json(root / "config.libero-pro.json", {"adapter": {"settings": {}},
                       "models": {"planner": {"model": "test"}}})
            with patch("pac_harness.libero_pro.preflight", return_value={"pinned": True}), patch(
                    "pac_harness.libero_eval.main", return_value=0), patch("builtins.print"):
                self.assertEqual(evaluate_pro(["--root", str(root), "--protocol", "k1-18", "--output", "batch"]), 0)
                saved = loads((root / "batch/protocol.json").read_text())
                self.assertEqual(saved["comparison"]["profile"], "k1-18")
                self.assertFalse(saved["post_run_review"])
                self.assertEqual(evaluate_pro(["--root", str(root), "--protocol", "k1-18", "--resume", "batch"]), 0)
                with patch("sys.stderr"), self.assertRaises(SystemExit):
                    evaluate_pro(["--root", str(root), "--protocol", "k1-180", "--resume", "batch"])
                with patch("sys.stderr"), self.assertRaises(SystemExit):
                    evaluate_pro(["--root", str(root), "--protocol", "k1-180", "--summarize", "batch"])

    def test_batch_protocol_resume_and_fixed_knowledge(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_json(root / "config.libero-pro.json", {"adapter": {"settings": {}},
                       "models": {"planner": {"model": "test"}}, "analyzer": {"enabled": True}})
            calls = []

            def run_child(args):
                calls.append(args)
                child = root / "batch" / PRO_SUITES[0]
                write_json(child / "summary.json", {"mode": "evaluation"})
                self.write_result(root / "batch", PRO_SUITES[0], 0, 2, True)
                return 0

            with patch("pac_harness.libero_pro.preflight", return_value={"pinned": True}), patch(
                    "pac_harness.libero_eval.main", side_effect=run_child), patch("builtins.print"):
                self.assertEqual(evaluate_pro(["--root", str(root), "--suite", PRO_SUITES[0],
                                              "--task-ids", "0", "--init-state-ids", "2",
                                              "--output", "batch"]), 0)
                saved = loads((root / "batch/protocol.json").read_text())
                self.assertEqual(saved["init_state_ids"], [2])
                self.assertFalse(saved["post_run_review"])
                self.assertEqual(saved["detector"], "model")
                self.assertIn("--no-post-run-review", calls[0])
                self.assertFalse(loads((root / "batch/config.json").read_text())["analyzer"]["enabled"])
                self.assertEqual(evaluate_pro(["--root", str(root), "--resume", "batch"]), 0)
                self.assertIn("--resume", calls[1])
                self.assertFalse(loads((root / "batch/summary.json").read_text())["full_180_protocol"])
                write_json(root / "memories/changed.json", {})
                with patch("sys.stderr"), self.assertRaises(SystemExit):
                    evaluate_pro(["--root", str(root), "--resume", "batch"])
                self.assertEqual(len(calls), 2)

    def test_first_environment_initialization_failure_leaves_resumable_protocol(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_json(root / "config.json", {"adapter": {"settings": {}},
                       "models": {"planner": {"model": "test"}}})
            with patch("pac_harness.libero_eval.load_suite", return_value=SimpleNamespace(n_tasks=10)), patch(
                    "pac_harness.libero_eval.load_init_states", return_value=list(range(50))), patch(
                    "pac_harness.libero_eval.LiberoEnvironment", side_effect=RuntimeError("renderer failed")):
                with self.assertRaisesRegex(RuntimeError, "renderer failed"):
                    evaluate(["--root", str(root), "--config", "config.json", "--suite", PRO_SUITES[0],
                              "--task-ids", "0", "--init-state-ids", "2", "--output", str(root / "batch")])
                saved = loads((root / "batch/summary.json").read_text())
                self.assertEqual(saved["episodes"], 0)
                self.assertEqual(saved["init_state_ids"], [2])

    def test_environment_instruction_and_native_horizon_override_filename(self):
        import importlib.util
        if not all(importlib.util.find_spec(m) for m in ("numpy", "PIL")):
            self.skipTest("Optional simulator observation dependencies unavailable")
        Task = namedtuple("Task", "name language")
        task = Task("open_middle_drawer", "open the middle drawer")
        suite = SimpleNamespace(n_tasks=10, get_task=lambda i: task,
                                get_task_bddl_file_path=lambda i: "changed.bddl")
        sim = SimpleNamespace(get_state=lambda: SimpleNamespace(flatten=lambda: SimpleNamespace(tolist=lambda: [42])))
        native = SimpleNamespace(language_instruction="open the bottom drawer", env=SimpleNamespace(sim=sim),
                                 seed=lambda s: None, reset=lambda: {}, set_init_state=lambda s: {},
                                 check_success=lambda: False, close=lambda: None)
        module = SimpleNamespace(OffScreenRenderEnv=lambda **kw: native)
        with tempfile.TemporaryDirectory() as temporary, patch.dict("sys.modules", {"libero.libero.envs": module}), patch(
                "adapters.libero.load_suite", return_value=suite), patch("adapters.libero.load_init_states", return_value=[[0]]):
            calls = []
            def factory(**kwargs):
                calls.append(kwargs)
                return native
            module.OffScreenRenderEnv = factory
            adapter = LiberoEnvironment(root=temporary, run_directory=Path(temporary)/"run", config={
                "suite": PRO_SUITES[0], "max_steps": 2400, "settle_steps": 0, "calibrated_motion": False})
            self.assertEqual(adapter.task.language, "open the bottom drawer")
            self.assertEqual(calls[0]["horizon"], 2400)
            self.assertTrue(calls[0]["ignore_done"])
            self.assertEqual(adapter.observe()["instruction"], "open the bottom drawer")


if __name__ == "__main__":
    unittest.main()
