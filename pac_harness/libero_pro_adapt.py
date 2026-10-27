"""Run a reproducible, state-disjoint LIBERO-Pro adaptation curriculum."""
from __future__ import annotations

import argparse
from collections import Counter
import os
from pathlib import Path
import random
import secrets
import shutil
import subprocess
import sys
import time

from adapters.libero import PRO_SUITES
from .libero_pro import knowledge_hashes, preflight
from .storage import RunLock, loads, write_json


def select_episodes(benchmark, seed):
    """Select five distinct tasks per suite, never held-out state IDs 0..2."""
    rng = random.Random(seed)
    tasks = {(t["suite"], t["task_id"]): t for t in benchmark["tasks"]}
    by_suite = {}
    for suite in PRO_SUITES:
        chosen = []
        for task_id in rng.sample(range(10), 5):
            task = tasks[suite, task_id]
            if task["available_initial_states"] <= 3:
                raise ValueError(f"No adaptation states outside held-out states: {suite} task {task_id}")
            chosen.append({"suite": suite, "task_id": task_id,
                           "init_state_id": rng.randrange(3, task["available_initial_states"]),
                           "instruction": task["instruction"]})
        by_suite[suite] = chosen
    # Alternate suites so every suite receives one review before its next trial.
    episodes = []
    for round_id in range(5):
        for suite in PRO_SUITES:
            item = {**by_suite[suite][round_id], "index": len(episodes) + 1}
            item["batch_dir"] = f'{suite}/sample-{round_id+1:03d}'
            item["episode_dir"] = (f'{item["batch_dir"]}/task-{item["task_id"]:03d}'
                                   f'-episode-{item["init_state_id"]:03d}')
            episodes.append(item)
    validate_episodes(episodes)
    return episodes


def validate_episodes(episodes):
    identities = [(e["suite"], e["task_id"], e["init_state_id"]) for e in episodes]
    if len(identities) != 30 or len(set(identities)) != 30:
        raise ValueError("Adaptation requires 30 unique episodes")
    if Counter(e["suite"] for e in episodes) != Counter({suite: 5 for suite in PRO_SUITES}):
        raise ValueError("Adaptation requires five episodes per suite")
    if any(e["init_state_id"] < 3 for e in episodes):
        raise ValueError("Adaptation overlaps held-out evaluation states 0, 1, 2")
    for suite in PRO_SUITES:
        if len({e["task_id"] for e in episodes if e["suite"] == suite}) != 5:
            raise ValueError("Use five distinct tasks per suite")


def snapshot(root, destination):
    hashes = knowledge_hashes(root)
    for relative in hashes:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / relative, target)
    archive = root / "maintenance/analyzer/lessons.json"
    if archive.exists():
        target = destination / "maintenance/analyzer/lessons.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(archive, target)
    write_json(destination / "sha256.json", hashes)
    return hashes


def summarize(directory, plan, *, status, current=None, error=None):
    records = []
    for item in plan["episodes"]:
        episode = directory / item["episode_dir"]
        result = loads((episode / "result.json").read_text(encoding="utf-8")) if (episode / "result.json").exists() else None
        review = loads((episode / "review/report.json").read_text(encoding="utf-8")) if (episode / "review/report.json").exists() else None
        finished = bool(result and result["runtime"]["status"] not in {"interrupted", "restart_required"})
        reviewed = bool(finished and review and review.get("status") in {"applied", "no_change"})
        records.append({**item, "experiment_finished": finished,
                        "success": result["success"] if result else None,
                        "runtime_status": result["runtime"]["status"] if result else None,
                        "review_status": review.get("status") if review else None,
                        "review_completed": reviewed,
                        "updated_rules": len(review.get("lesson_keys", [])) if reviewed else 0})
    value = {"mode": "adaptation", "status": status, "expected": 30,
             "experiments_finished": sum(r["experiment_finished"] for r in records),
             "reviews_completed": sum(r["review_completed"] for r in records),
             "successes": sum(r["success"] is True for r in records),
             "held_out_state_ids": [0, 1, 2], "current": current, "error": error,
             "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "episodes": records}
    write_json(directory / "status.json", value)
    return value


def _retryable_review(report):
    diagnostics = report.get("diagnostics", {}) if isinstance(report, dict) else {}
    if diagnostics.get("stage") in {"transport", "http_body", "provider_error"}:
        return True
    return diagnostics.get("http_status") in {408, 429, 500, 502, 503, 504}


def run_curriculum(root, directory, plan, *, attempts=5, retry_delay_s=30):
    config = directory / "config.json"
    validate_episodes(plan["episodes"])
    current = preflight(root, list(PRO_SUITES), list(range(10)), [0, 1, 2])
    if current != plan["benchmark"]:
        raise ValueError("Benchmark inputs differ from the saved adaptation plan")
    try:
        for item in plan["episodes"]:
            episode = directory / item["episode_dir"]
            cached_result = loads((episode / "result.json").read_text(encoding="utf-8")) if (episode / "result.json").exists() else None
            cached_review = loads((episode / "review/report.json").read_text(encoding="utf-8")) if (episode / "review/report.json").exists() else None
            if (cached_result and cached_result["runtime"]["status"] not in {"interrupted", "restart_required"}
                    and cached_review and cached_review.get("status") in {"applied", "no_change"}):
                print(f'Adaptation: preserving completed experiment and review {item["index"]}/30', flush=True)
                continue
            knowledge = directory / "knowledge-history" / f'episode-{item["index"]:03d}'
            before = knowledge / "before"
            if not before.exists():
                snapshot(root, before)
            label = f'{item["index"]}/30 {item["suite"]} task={item["task_id"]} state={item["init_state_id"]}'
            log = directory / f'episode-{item["index"]:03d}.log'
            if cached_result and cached_result["runtime"]["status"] not in {"interrupted", "restart_required"}:
                # A completed experiment must never be rerun merely because its
                # Analyzer review failed or was interrupted.  Resume the review
                # from the preserved result and review caches instead.
                result = cached_result
                print(f'Adaptation: preserving completed experiment {label}; retrying Analyzer', flush=True)
            else:
                print(f'Adaptation: {label}\nInstruction: {item["instruction"]}', flush=True)
                summarize(directory, plan, status="running", current={**item, "stage": "experiment"})
                batch = directory / item["batch_dir"]
                call = [sys.executable, "-u", "-m", "pac_harness.libero_eval", "--root", str(root),
                        "--config", str(config), "--suite", item["suite"], "--task-ids", str(item["task_id"]),
                        "--init-state-ids", str(item["init_state_id"]), "--seed", str(plan["simulator_seed"]),
                        "--detector", "model", "--no-post-run-review",
                        "--resume" if (batch / "summary.json").exists() else "--output", str(batch)]
                with log.open("a") as stream:
                    code = subprocess.run(call, cwd=root, stdout=stream, stderr=subprocess.STDOUT).returncode
                if code:
                    raise RuntimeError(f"Experiment exited {code}: {log}")
                result = loads((episode / "result.json").read_text(encoding="utf-8"))
                if result["runtime"]["status"] in {"interrupted", "restart_required"}:
                    raise RuntimeError(f"Experiment needs recovery: {episode}")
                print(f'Adaptation result: {label} success={result["success"]} '
                      f'steps={result["steps"]} stop={result["runtime"]["status"]}', flush=True)
            summarize(directory, plan, status="running", current={**item, "stage": "analyzer"})
            review_call = [sys.executable, "-u", "-m", "pac_harness.post_run", "--root", str(root),
                           "--config", config.relative_to(root).as_posix(), "--run-dir", str(episode)]
            for attempt in range(attempts):
                with log.open("a") as stream:
                    code = subprocess.run(review_call, cwd=root, stdout=stream, stderr=subprocess.STDOUT).returncode
                report = loads((episode / "review/report.json").read_text(encoding="utf-8")) if (episode / "review/report.json").exists() else {}
                if code == 0 and report.get("status") in {"applied", "no_change"}:
                    break
                if not _retryable_review(report):
                    print(f'Analyzer stopped on a non-retryable error: {report.get("error", "unknown failure")}; see {log}', flush=True)
                    break
                delay = min(retry_delay_s * (2 ** attempt), 300)
                print(f'Analyzer transient failure ({report.get("error", "request failed")}); '
                      f'retry {attempt+1}/{attempts} after {delay}s: {label}; see {log}', flush=True)
                if attempt + 1 < attempts:
                    time.sleep(delay)
            else:
                raise RuntimeError(f"Analyzer incomplete; completed caches preserved: {episode / 'review/report.json'}")
            snapshot(root, knowledge / "after")
            progress = summarize(directory, plan, status="running", current=None)
            print(f'Adaptation reviewed: {progress["reviews_completed"]}/30; '
                  f'updated rules={len(report.get("lesson_keys", []))}', flush=True)
        snapshot(root, directory / "knowledge-final")
        value = summarize(directory, plan, status="completed")
        if value["reviews_completed"] != 30:
            raise RuntimeError("Not all 30 Analyzer reviews completed")
        print(f'Adaptation complete; frozen knowledge: {directory / "knowledge-final"}', flush=True)
        return 0
    except BaseException as exc:
        summarize(directory, plan, status="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
                  error=f"{type(exc).__name__}: {exc}")
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    parser.add_argument("--config", default="config.libero-pro.json")
    parser.add_argument("--output")
    parser.add_argument("--resume")
    parser.add_argument("--sampling-seed", type=int)
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    if args.resume and (args.output or args.sampling_seed is not None):
        parser.error("Resume uses the saved plan; omit output and sampling seed")
    directory = (root / (args.resume or args.output or
                 f'logs/libero-pro-adapt-{time.strftime("%Y%m%d-%H%M%S")}')).resolve()
    directory.relative_to(root)
    if not args.resume:
        benchmark = preflight(root, list(PRO_SUITES), list(range(10)), [0, 1, 2])
        seed = args.sampling_seed if args.sampling_seed is not None else secrets.randbits(32)
        config = loads((root / args.config).read_text(encoding="utf-8"))
        config["analyzer"] = {**config.get("analyzer", {}), "enabled": True}
        config["to_user"] = {**config.get("to_user", {}), "enabled": False}
        plan = {"mode": "adaptation", "sampling_seed": seed, "simulator_seed": 0,
                "held_out_state_ids": [0, 1, 2], "benchmark": benchmark,
                "episodes": select_episodes(benchmark, seed)}
        directory.mkdir(parents=True, exist_ok=False)
        write_json(directory / "plan.json", plan)
        write_json(directory / "config.json", config)
        snapshot(root, directory / "knowledge-initial")
        lines = ["| # | Suite | Task | Initial state |", "|---:|---|---:|---:|"]
        lines.extend(f'| {e["index"]} | {e["suite"]} | {e["task_id"]} | {e["init_state_id"]} |'
                     for e in plan["episodes"])
        (directory / "selection.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        summarize(directory, plan, status="planned")
    else:
        plan = loads((directory / "plan.json").read_text(encoding="utf-8"))
        validate_episodes(plan["episodes"])
    print(f'Adaptation directory: {directory}; sampling seed: {plan["sampling_seed"]}', flush=True)
    if args.plan_only:
        return 0
    with RunLock(directory):
        write_json(directory / "worker.json", {"pid": os.getpid(), "started_at": time.time()})
        return run_curriculum(root, directory, plan)


if __name__ == "__main__":
    raise SystemExit(main())
