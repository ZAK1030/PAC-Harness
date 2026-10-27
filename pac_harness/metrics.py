"""Export measured runtime and provider-reported usage, preserving missing data."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import json
from pathlib import Path

from .storage import write_json


TOKEN_FIELDS = ("input_tokens", "output_tokens", "total_tokens", "cached_input_tokens", "reasoning_tokens")


def token_usage(raw):
    raw = raw if isinstance(raw, dict) else {}
    def count(value):
        return value if type(value) is int and value >= 0 else None
    def detail(name, alternate, key):
        value = raw.get(name, raw.get(alternate))
        return count(value.get(key)) if isinstance(value, dict) else None
    return {"input_tokens": count(raw.get("input_tokens", raw.get("prompt_tokens"))),
            "output_tokens": count(raw.get("output_tokens", raw.get("completion_tokens"))),
            "total_tokens": count(raw.get("total_tokens")),
            "cached_input_tokens": detail("input_tokens_details", "prompt_tokens_details", "cached_tokens"),
            "reasoning_tokens": detail("output_tokens_details", "completion_tokens_details", "reasoning_tokens")}


def usage_totals(requests):
    result = {"observed_attempts": len(requests)}
    for key in TOKEN_FIELDS:
        values = [r.get(key) for r in requests]
        known = [v for v in values if v is not None]
        result[key + "_reported_sum"] = sum(known)
        result[key + "_missing_attempts"] = len(values) - len(known)
        result[key] = sum(known) if len(known) == len(values) and values else None
    return result


def read_events(path):
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            # A running writer may not have finished its last line yet.
            continue
    return events


def episode_metrics(directory, relative):
    events = read_events(directory / "events.jsonl")
    attempts, legacy, sessions, turns = {}, set(), {}, []
    for event in events:
        kind, diag = event.get("event"), event.get("diagnostics", {})
        if kind in {"model_attempt_start", "model_attempt_end"}:
            key = diag["attempt_id"]
            row = attempts.setdefault(key, {"episode": relative, "attempt_id": key,
                "role": event.get("role"), "turn": event.get("turn"), "telemetry": "attempt"})
            row.update(diag)
            row.update(token_usage(diag.get("usage")))
        elif kind in {"model_response", "model_error", "model_retry"} and not diag.get("attempt_id"):
            identity = (event.get("turn"), event.get("role"),
                        diag.get("response_id") or json.dumps(diag, sort_keys=True))
            if identity in legacy:
                continue
            legacy.add(identity)
            key = f"legacy-{event['time_ns']}"
            attempts[key] = {"episode": relative, "attempt_id": key, "telemetry": "legacy",
                "role": event.get("role"), "turn": event.get("turn"), **diag,
                "outcome": "completed" if kind == "model_response" else "failed", **token_usage(diag.get("usage"))}
        if kind in {"episode_session_start", "episode_session_end"}:
            session = sessions.setdefault(event["session_id"], {})
            session.update(event)
        if kind in {"planner", "detector"} and "inference_s" in event:
            turns.append({"episode": relative, "role": kind, "turn": event.get("turn"),
                          "time_ns": event["time_ns"], "inference_s": event["inference_s"]})
    requests = list(attempts.values())
    data = json.loads((directory / "result.json").read_text(encoding="utf-8")) if (directory / "result.json").exists() else {}
    first = min((e["time_ns"] for e in events), default=None)
    last = max((e["time_ns"] for e in events), default=None)
    row = {"episode": relative, "success": data.get("success"),
           "status": data.get("runtime", {}).get("status", data.get("status", "running")),
           "first_event_ns": first, "last_event_ns": last,
           "observed_span_s": (last-first)/1e9 if first is not None else None,
           "measured_session_s": round(sum(s.get("elapsed_s", 0) for s in sessions.values()), 3),
           "session_timing_available": bool(sessions),
           "legacy_attempts": sum(r["telemetry"] == "legacy" for r in requests),
           "model_request_s_recorded": round(sum(r.get("elapsed_s", 0) for r in requests), 3),
           "planner_inference_s_recorded": round(sum(t["inference_s"] for t in turns if t["role"] == "planner"), 3),
           "completed_actions": sum(e.get("event") == "action_result" for e in events),
           **usage_totals(requests)}
    return row, requests, turns


def export_metrics(batch, output=None):
    batch = Path(batch).resolve()
    output = Path(output).resolve() if output else batch / "metrics"
    output.mkdir(parents=True, exist_ok=True)
    episodes, requests, turns = [], [], []
    for journal in sorted(batch.rglob("events.jsonl")):
        if not (journal.parent / "task_state.json").is_file():
            continue
        if output == journal.parent or output in journal.parents:
            continue
        row, calls, planning = episode_metrics(journal.parent, journal.parent.relative_to(batch).as_posix())
        episodes.append(row)
        requests.extend(calls)
        turns.extend(planning)
    starts = [e["first_event_ns"] for e in episodes if e["first_event_ns"] is not None]
    ends = [e["last_event_ns"] for e in episodes if e["last_event_ns"] is not None]
    summary = {"generated_at": datetime.now(timezone.utc).isoformat(), "episodes_observed": len(episodes),
        "finished_episodes": sum(e["status"] not in {"running", "interrupted", "restart_required"} for e in episodes),
        "observed_wall_span_s": round((max(ends)-min(starts))/1e9, 3) if starts else None,
        "model_request_s_recorded": round(sum(e["model_request_s_recorded"] for e in episodes), 3),
        "planner_inference_s_recorded": round(sum(e["planner_inference_s_recorded"] for e in episodes), 3),
        "measured_session_s": round(sum(e["measured_session_s"] for e in episodes), 3),
        "legacy_attempts": sum(e["legacy_attempts"] for e in episodes),
        **usage_totals(requests),
        "notes": ["Usage is provider-reported, not a local token estimate or a billing receipt.",
                  "Null means unavailable. Reported sums are partial when missing_attempts > 0.",
                  "Cached input and reasoning tokens are subsets; never add them again to total_tokens.",
                  "All observed transport attempts, format repairs and read-tool calls are included once.",
                  "Legacy attempt counts/times may omit interrupted requests; historical usage cannot be recovered from these logs.",
                  "Observed span includes pauses. Session timing excludes downtime but is only available after instrumentation.",
                  "Request time and Planner inference time overlap; do not add them together."]}
    write_json(output / "summary.json", summary)
    for name, rows in (("episodes", episodes), ("requests", requests), ("turns", turns)):
        fields = sorted({key for row in rows for key in row})
        with (output / f"{name}.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows({k: json.dumps(v) if isinstance(v, (dict, list)) else v for k,v in r.items()} for r in rows)
    (output / "summary.md").write_text(
        "# Runtime and token usage\n\n"
        f"Finished episodes: {summary['finished_episodes']}. Observed requests: {len(requests)}.\n\n"
        f"Recorded request time: {summary['model_request_s_recorded']:.3f} s. "
        f"Recorded Planner inference time: {summary['planner_inference_s_recorded']:.3f} s.\n\n"
        "| Metric | Provider-reported sum | Attempts with missing values | Exact total |\n|---|---:|---:|---:|\n" +
        "".join(f"| {key} | {summary[key+'_reported_sum']} | {summary[key+'_missing_attempts']} | "
                f"{summary[key] if summary[key] is not None else 'unknown'} |\n" for key in TOKEN_FIELDS) +
        "\n" + "\n".join("- " + note for note in summary["notes"]) + "\n", encoding="utf-8")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-dir", required=True)
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    result = export_metrics(args.batch_dir, args.output)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
