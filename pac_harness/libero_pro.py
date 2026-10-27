"""Run the six LIBERO-Pro suites and export a native-success comparison row."""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import io
from pathlib import Path
import subprocess
import time
import uuid

from adapters.libero import PRO_SUITES, load_suite, load_init_states
from .libero_pro_setup import SOURCE_REVISION, DATASET_REVISION
from .storage import loads, write_json

COLUMNS = ("Object / Position", "Object / Task", "Goal / Position", "Goal / Task",
           "Spatial / Position", "Spatial / Task")
K1_REFERENCE_COMMIT = "ee46363101fcf3ef87182fb2dbad99a92ce77fc0"
K1_PROTOCOLS = {
    "k1-180": {"task_ids": list(range(10)), "init_state_ids": [0, 1, 2]},
    "k1-18": {"task_ids": [0, 1, 2], "init_state_ids": [0]},
}


def comparison_scope(protocol):
    """Identify the published case sets, without claiming identical methods/data."""
    if protocol["mode"] != "evaluation" or set(protocol["suites"]) != set(PRO_SUITES):
        return None
    for name, selection in K1_PROTOCOLS.items():
        if all(protocol[key] == values for key, values in selection.items()):
            return name
    return None


def knowledge_hashes(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for directory in ("prompts", "memories", "skills")
            for p in sorted((root / directory).rglob("*")) if p.is_file()}


def preflight(root, suites, task_ids, initials):
    """Validate pinned inputs and actual task language before any model request."""
    spec = importlib.util.find_spec("libero")
    if spec is None:
        raise RuntimeError("Run bash scripts/setup_libero_pro.sh first")
    source = Path(next(iter(spec.submodule_search_locations))).parent
    revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if revision != SOURCE_REVISION:
        raise ValueError("Imported LIBERO source differs from the pinned Pro revision; use scripts/run_libero_pro.sh")
    from libero.libero import get_libero_path
    from libero.libero.envs.bddl_utils import get_problem_info
    data = Path(get_libero_path("init_states")).parent
    manifest = loads((data / "manifest.json").read_text())
    if manifest["source_revision"] != SOURCE_REVISION or manifest["dataset_revision"] != DATASET_REVISION:
        raise ValueError("LIBERO-Pro data revision mismatch; rerun scripts/setup_libero_pro.sh")
    expected = {item["path"]: item["sha256"] for item in manifest["files"]}
    records = []
    for name in suites:
        suite = load_suite(name, root / "maintenance/libero-pro-config")
        for task_id in task_ids:
            if not 0 <= task_id < suite.n_tasks:
                raise ValueError(f"Invalid task ID {task_id} in {name}")
            task = suite.get_task(task_id)
            bddl = Path(suite.get_task_bddl_file_path(task_id))
            initial = Path(get_libero_path("init_states")) / task.problem_folder / task.init_states_file
            hashes = {}
            for kind, path in (("bddl_files", bddl), ("init_files", initial)):
                relative = f"{kind}/{task.problem_folder}/{path.name}"
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                if digest != expected.get(relative):
                    raise ValueError(f"Modified or unpinned benchmark input: {path}")
                hashes[kind] = digest
            states = load_init_states(suite, task_id)
            if max(initials) >= len(states):
                raise ValueError(f"Initial-state index unavailable: {name} task {task_id}")
            instruction = get_problem_info(str(bddl))["language_instruction"]
            records.append({"suite": name, "task_id": task_id, "base_task_name": task.name,
                            "instruction": instruction, "available_initial_states": len(states),
                            "input_sha256": hashes})
    return {"source_revision": SOURCE_REVISION, "dataset_revision": DATASET_REVISION,
            "dataset_endpoint": manifest["endpoint"], "tasks": records}


def summarize(directory, protocol):
    """Incomplete groups remain unscored; smoke runs never become model scores."""
    results, groups = [], {}
    mode = protocol["mode"]
    for suite in protocol["suites"]:
        expected = len(protocol["task_ids"]) * len(protocol["init_state_ids"])
        records = []
        for task_id in protocol["task_ids"]:
            for initial in protocol["init_state_ids"]:
                path = directory / suite / f"task-{task_id:03d}-episode-{initial:03d}/result.json"
                if not path.exists():
                    continue
                record = loads(path.read_text())
                if (record["task_id"] != task_id or record.get("init_state_id", initial) != initial):
                    raise ValueError(f"Result identity mismatch: {path}")
                if record["runtime"]["status"] in {"interrupted", "restart_required"}:
                    continue
                records.append({**record, "suite": suite})
        successes = sum(bool(r["success"]) for r in records)
        complete = len(records) == expected
        groups[suite] = {"expected": expected, "completed": len(records), "successes": successes,
                         "success_rate": successes / expected if complete and mode == "evaluation" else None,
                         "complete": complete}
        results.extend(records)
    expected = len(protocol["suites"]) * len(protocol["task_ids"]) * len(protocol["init_state_ids"])
    complete = len(results) == expected
    total_success = sum(bool(r["success"]) for r in results)
    scope = comparison_scope(protocol)
    standard = scope == "k1-180"
    summary = {"benchmark": "LIBERO-Pro", "mode": mode, "method": protocol["method"],
               "expected": expected, "completed": len(results), "successes": total_success,
               "success_rate": total_success / expected if complete and mode == "evaluation" else None,
               "complete": complete, "full_180_protocol": standard,
               "k1_case_scope": scope, "matched_18_case_scope": scope == "k1-18",
               "groups": groups, "results": results}
    write_json(directory / "summary.json", summary)
    values = [f'{100*groups[s]["success_rate"]:.1f}' if s in groups and
              groups[s]["success_rate"] is not None else "—" for s in PRO_SUITES]
    average = f'{100*summary["success_rate"]:.1f}' if summary["success_rate"] is not None else "—"
    headers = ["Method", *COLUMNS, "Average"]
    row = [protocol["method"], *values, average]
    markdown = "| " + " | ".join(headers) + " |\n|" + "---|" * len(headers) + "\n| " + " | ".join(row) + " |\n\n"
    markdown += f"Mode: {mode}. Finished: {len(results)}/{expected}. Unrun/interrupted groups are not scored.\n\n"
    markdown += "| Suite | Finished / expected | Native successes |\n|---|---:|---:|\n"
    for suite, group in groups.items():
        markdown += f'| {suite} | {group["completed"]}/{group["expected"]} | {group["successes"]} |\n'
    markdown += "\nAverage is populated only for a complete evaluation selection. Custom selections are not matched K1 evaluations.\n"
    if scope:
        markdown += f"\nK1 case scope: {scope}. Case selection alone does not establish identical benchmark data, methods or inference budgets.\n"
    (directory / "table.md").write_text(markdown, encoding="utf-8")
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(headers)
    writer.writerow(row)
    (directory / "table.csv").write_text(stream.getvalue(), encoding="utf-8", newline="")
    from .metrics import export_metrics
    export_metrics(directory)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    parser.add_argument("--config", default="config.libero-pro.json")
    parser.add_argument("--protocol", choices=K1_PROTOCOLS,
                        help="Fix K1's published 180-case sweep or 18-case Astra subset; does not call a model with --check")
    parser.add_argument("--suites", "--suite", nargs="+", choices=PRO_SUITES)
    parser.add_argument("--task-ids", nargs="+", type=int)
    initial_group = parser.add_mutually_exclusive_group()
    initial_group.add_argument("--init-state-ids", nargs="+", type=int)
    initial_group.add_argument("--episodes", type=int, help="Use consecutive stored states starting at zero")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--max-steps", type=int)
    parser.add_argument("--detector", choices=("model", "official"))
    parser.add_argument("--output")
    parser.add_argument("--resume", metavar="BATCH_DIR")
    parser.add_argument("--summarize", metavar="BATCH_DIR", help="Regenerate tables without simulation or model calls")
    parser.add_argument("--check", action="store_true", help="Validate task/state inputs without model calls")
    smoke = parser.add_mutually_exclusive_group()
    smoke.add_argument("--smoke", action="store_true")
    smoke.add_argument("--smoke-motion", action="store_true")
    review = parser.add_mutually_exclusive_group()
    review.add_argument("--post-run-review", dest="review", action="store_true", default=None)
    review.add_argument("--no-post-run-review", dest="review", action="store_false")
    args = parser.parse_args(argv)
    if args.protocol:
        if args.smoke or args.smoke_motion or args.review:
            parser.error("K1 comparison profiles require fixed-policy evaluation; smoke and online review are separate protocols")
        selected = {"suites": list(PRO_SUITES), **K1_PROTOCOLS[args.protocol]}
        if args.episodes is not None:
            if list(range(args.episodes)) != selected["init_state_ids"]:
                parser.error("--episodes conflicts with the selected K1 protocol")
        for key, values in selected.items():
            if getattr(args, key) is not None and getattr(args, key) != values:
                parser.error(f"--{key.replace('_', '-')} conflicts with the selected K1 protocol")
            setattr(args, key, list(values))
        args.review = False
    if args.max_steps is not None and args.max_steps < 1:
        parser.error("--max-steps must be positive")
    if args.episodes is not None and args.episodes < 1:
        parser.error("--episodes must be positive")
    root = Path(args.root).resolve()

    def local(value):
        p = Path(value)
        p = p.resolve() if p.is_absolute() else (root / p).resolve()
        p.relative_to(root)
        return p

    if args.summarize:
        directory = local(args.summarize)
        saved = loads((directory / "protocol.json").read_text())
        if args.protocol and comparison_scope(saved) != args.protocol:
            parser.error("Saved batch protocol mismatch: --protocol")
        result = summarize(directory, saved)
        print(f'LIBERO-Pro: {result["completed"]}/{result["expected"]}; table: {directory / "table.md"}')
        return 0
    if args.resume and (args.output or args.smoke or args.smoke_motion or args.check):
        parser.error("--resume cannot be combined with output, check or smoke modes")
    testing = args.smoke or args.smoke_motion
    if args.resume:
        directory = local(args.resume)
        protocol = loads((directory / "protocol.json").read_text())
        if args.protocol and comparison_scope(protocol) != args.protocol:
            parser.error("Resume protocol mismatch: --protocol")
        if protocol["mode"] != "evaluation":
            parser.error("Only evaluation runs can be resumed")
        overrides = {"suites": args.suites, "task_ids": args.task_ids, "init_state_ids": args.init_state_ids,
                     "seed": args.seed, "detector": args.detector, "post_run_review": args.review}
        if args.episodes is not None:
            overrides["init_state_ids"] = list(range(args.episodes))
        for key, value in overrides.items():
            if value is not None and value != protocol[key]:
                parser.error(f"Resume protocol mismatch: {key}")
        config = loads((directory / "config.json").read_text())
        if args.max_steps is not None and args.max_steps != config["adapter"]["settings"]["max_steps"]:
            parser.error("Resume protocol mismatch: max_steps")
        if not protocol["post_run_review"] and knowledge_hashes(root) != protocol["knowledge_sha256"]:
            parser.error("Knowledge changed since this fixed-policy batch started; restore the recorded files or start a new batch")
    else:
        config = loads(local(args.config).read_text())
        if args.max_steps is not None:
            config["adapter"]["settings"]["max_steps"] = args.max_steps
        suites = args.suites or list(PRO_SUITES)
        ids = args.task_ids if args.task_ids is not None else ([0] if testing else list(range(10)))
        initials = args.init_state_ids if args.init_state_ids is not None else list(range(
            args.episodes if args.episodes is not None else 1 if testing else 3))
        for name, values in (("suites", suites), ("task IDs", ids), ("initial-state IDs", initials)):
            if not values or len(set(values)) != len(values) or any(isinstance(v, int) and v < 0 for v in values):
                parser.error(f"Invalid or duplicate {name}")
        if testing and len(initials) != 1:
            parser.error("Smoke modes require exactly one initial-state index")
        benchmark = preflight(root, suites, ids, initials)
        if args.check:
            for task in benchmark["tasks"]:
                print(f'{task["suite"]} task={task["task_id"]}: {task["instruction"]}')
            print(f"Verified {len(benchmark['tasks'])} tasks; stored initial-state indices {initials}")
            if args.protocol:
                print(f"K1 case selection: {args.protocol}; {len(suites) * len(ids) * len(initials)} episodes. "
                      "See K1_ALIGNMENT.md for data provenance and remaining protocol differences.")
            return 0
        directory = local(args.output) if args.output else root / "logs" / f"libero-pro-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        directory.mkdir(parents=True, exist_ok=False)
        config["analyzer"] = {**config.get("analyzer", {}), "enabled": bool(args.review)}
        protocol = {"mode": "smoke_motion" if args.smoke_motion else "smoke" if args.smoke else "evaluation",
                    "suites": suites, "task_ids": ids, "init_state_ids": initials,
                    "seed": args.seed if args.seed is not None else 0, "detector": args.detector or "model",
                    "post_run_review": bool(args.review), "benchmark": benchmark,
                    "method": "PAC-Harness + " + config["models"]["planner"]["model"],
                    "knowledge_sha256": knowledge_hashes(root)}
        if args.protocol:
            protocol["comparison"] = {"profile": args.protocol,
                "reference_repository": "https://github.com/Robo-Harness/k1",
                "reference_commit": K1_REFERENCE_COMMIT,
                "reference_document": "docs/results.md",
                "scope": "Published case selection; K1 experiment data hashes/manifests are not released"}
        write_json(directory / "protocol.json", protocol)
        write_json(directory / "config.json", config)
    if args.resume:
        current = preflight(root, protocol["suites"], protocol["task_ids"], protocol["init_state_ids"])
        if current != protocol["benchmark"]:
            parser.error("Benchmark inputs changed since the batch started")
    from .libero_eval import main as evaluate
    try:
        for suite in protocol["suites"]:
            if not protocol["post_run_review"] and knowledge_hashes(root) != protocol["knowledge_sha256"]:
                raise ValueError("Knowledge changed during fixed-policy evaluation; start a new batch")
            child = directory / suite
            call = ["--root", str(root), "--config", str(directory / "config.json"), "--suite", suite,
                    "--task-ids", *map(str, protocol["task_ids"]),
                    "--init-state-ids", *map(str, protocol["init_state_ids"]),
                    "--seed", str(protocol["seed"]), "--detector", protocol["detector"],
                    "--post-run-review" if protocol["post_run_review"] else "--no-post-run-review"]
            if protocol["mode"] == "smoke":
                call.append("--smoke")
            elif protocol["mode"] == "smoke_motion":
                call.append("--smoke-motion")
            call.extend(["--resume" if (child / "summary.json").exists() else "--output", str(child)])
            code = evaluate(call)
            summarize(directory, protocol)
            if code:
                return code
    finally:
        summarize(directory, protocol)
    print(f"LIBERO-Pro results: {directory / 'table.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
