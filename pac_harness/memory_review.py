"""Evidence-checked analyzer consolidation of accumulated manipulation advice."""
from __future__ import annotations

import hashlib
from pathlib import Path

from .experience import Experience, MEMORY_PATH, ROLE_MEMORY_PATHS, compact_rule
from .model_response import ModelError
from .redaction import redact
from .storage import Journal, RunLock, confined, dumps, loads, write_json


CONSOLIDATION_INSTRUCTION = """Review the accumulated lesson catalog against the
operator's priorities. Return at most FOUR focused reusable findings, possibly
fewer. Each combined condition+lesson+verification+recheck_when must fit 300
characters in complete English clauses. Do not mechanically truncate text.
Each finding must explicitly set priority and applicability. Use adapter
applicability for these object/operation procedures and short object keywords;
Set keyword_match any when listing alternative objects or destinations. With
all, every keyword must co-occur in a real source instruction. Empty keywords
with all are appropriate for a general placement/contact procedure whose
condition states exactly when to apply it.
do not carry a source suite's layout, pixel, height or coordinate assumption
across tasks. Distinguish grasping an object from using it as a destination.
Critical rules must state a concrete preventive action and an observable test.
Give Detector explicit deviations to report promptly, rather than only warnings
to verify later. For plate lifting, record the demonstrated limitation or
missing evidence; leave untested successful-grasp claims as hypothesis.
The catalog's existing rule text is advice under review, NOT ground truth.
Evidence excerpts have been checked against original events. They retain their
run and original event indices; use the supplied global event_index for citations.
Use supersedes to consolidate covered duplicate or conflicting catalog keys.
Only retire rules whose useful procedures and exceptions the replacement covers;
do not retire unrelated rules merely to reduce file length. Hypotheses cannot
retire supported advice. Reuse a stable existing ID when the procedure is the
same. Do not add generic protocol reminders or dilute operator priorities.
code_suggestions may identify remaining gaps but must not claim implementation.
This is a text-evidence consolidation, not a new visual review or robot test.
"""


def validate_consolidation(reply, payload):
    if len(reply["findings"]) > 4:
        raise ValueError("Consolidation must select at most four focused findings")
    known = {item["key"]: item for item in payload["lesson_catalog"]}
    for item in reply["findings"]:
        if len(compact_rule(item)) > 300:
            raise ValueError("Consolidated rules must fit 300 characters without truncation")
        if "priority" not in item or item.get("applicability") != "adapter":
            raise ValueError("Consolidation requires explicit priority and adapter applicability")
        words = item['task_keywords']
        if item.get('keyword_match', 'all') == 'any' and not words:
            raise ValueError("Alternative keyword matching requires nonempty keywords")
        if (words and item.get('keyword_match', 'all') == 'all' and payload.get('task_instructions')
                and not any(all(word.casefold() in task.casefold() for word in words)
                            for task in payload['task_instructions'])):
            raise ValueError("Conjunctive keywords match no source task; use keyword_match any for alternatives, or [] for a general conditional procedure")
        for key in item.get("supersedes", []):
            old = known.get(key)
            if (old is None or item["confidence"] != "supported"
                    or not set(old["roles"]).issubset(item["roles"])):
                raise ValueError("Supersedes must cite covered catalog keys with supported advice for all original roles")


def collect_memory(root, adapter=None):
    """Build a small review input from archived rules and verified outcome quotes.

    The analyzer cannot use archived prose as empirical evidence. Re-read each
    cited event from its original run; preserve original locations even after a
    previous consolidation assigned review-local indices.
    """
    from .post_run import _evidence_texts

    root = Path(root).resolve()
    archive = Experience(root).read()
    active = [item for item in archive["lessons"] if not item.get("retired_by")]
    adapters = {item["scope"]["adapter"] for item in active}
    if adapter is None:
        if len(adapters) != 1:
            raise ValueError("Specify --adapter when memory has zero or multiple adapters")
        adapter = next(iter(adapters))
    selected = [item for item in active if item["scope"]["adapter"] == adapter]
    files, events, origins, catalog, omissions, tasks, instructions = {}, [], {}, [], [], [], []
    event_ids = {}
    for item in selected:
        citations = []
        for source in reversed(item["sources"][-2:]):
            instruction = source.get('result', {}).get('instruction')
            if isinstance(instruction, str):
                instructions.append(instruction)
            source_count = 0
            for citation in source.get("evidence", [])[:4]:
                if source_count >= 2:
                    break
                origin = source.get("evidence_origins", {}).get(str(citation["event_index"]),
                    {"run": source["run"], "event_index": citation["event_index"]})
                run = origin["run"]
                try:
                    if run not in files:
                        path = confined(root, run + "/events.jsonl")
                        raw = path.read_bytes()
                        files[run] = (hashlib.sha256(raw).hexdigest(), raw.decode("utf-8").splitlines())
                    sha, lines = files[run]
                    index = origin["event_index"]
                    if type(index) is not int or not 1 <= index <= len(lines):
                        raise ValueError("Original event is unavailable")
                    event = loads(lines[index - 1])
                    if not any(citation["quote"] in text for text in _evidence_texts(event)):
                        raise ValueError("Archived quote no longer matches its original event")
                    key = (run, index, citation["quote"])
                    if key not in event_ids:
                        number = len(events) + 1
                        event_ids[key] = number
                        events.append(redact({"event_index": number, "event": event["event"],
                            "run": run, "source_event_index": index, "excerpts": [citation["quote"]]}))
                        origins[str(number)] = {"run": run, "event_index": index, "events_sha256": sha}
                    mapped = {"event_index": event_ids[key], "quote": citation["quote"]}
                    if mapped not in citations:
                        citations.append(mapped)
                        source_count += 1
                except (OSError, ValueError) as exc:
                    omissions.append({"lesson_key": item["key"], "run": run, "reason": str(exc)})
        if not citations:
            continue
        # Recover the full, pre-compaction wording when local shortening cut a
        # clause. This is context only; all empirical claims still need quotes.
        full_rule = compact_rule(item)
        latest_run = item["sources"][-1]["run"]
        try:
            report = loads(confined(root, latest_run + "/review/report.json").read_text())
            full = next((v for v in report.get("review", {}).get("findings", []) if v["id"] == item["id"]), None)
            if full:
                full_rule = compact_rule(full)
        except (OSError, ValueError):
            pass
        catalog.append({"key": item["key"], "id": item["id"], "roles": item["roles"],
            "scope": item["scope"], "confidence": item["confidence"], "priority": item.get("priority", "normal"),
            "rule": full_rule, "evidence_event_indices": [c["event_index"] for c in citations]})
        tasks.extend(item["scope"]["task_keywords"])
    if not catalog:
        raise ValueError("No archived lessons with verifiable original evidence")
    used = {number for item in catalog for number in item["evidence_event_indices"]}
    events = [event for event in events if event["event_index"] in used]
    origins = {str(number): origins[str(number)] for number in used}
    fingerprint = hashlib.sha256(dumps([archive, catalog, events, sorted(set(instructions))]).encode()).hexdigest()
    return archive, {"scope": {"adapter": adapter, "suite": "*", "task": " | ".join(sorted(set(tasks + instructions)))},
        "lesson_catalog": catalog, "events": events, "evidence_origins": origins,
        "task_instructions": sorted(set(instructions)), "evidence_sha256": fingerprint, "omissions": omissions}


def consolidate_memory(assistant, *, feedback, adapter=None, force=False):
    from .post_run import _cache_key, _text, validate_review

    _text(feedback, "user feedback", 8000)
    directory = confined(assistant.root, "maintenance/analyzer/consolidation")
    with RunLock(directory):
        archive, data = collect_memory(assistant.root, adapter)
        data["user_feedback"] = redact(feedback.strip())
        key = _cache_key(data, assistant.model, CONSOLIDATION_INSTRUCTION)
        run_dir = directory / key[:16]
        run_dir.mkdir(parents=True, exist_ok=True)
        # Origins are retained for audit and publication, not repeated in the
        # model input alongside the excerpts' original run/index labels.
        payload = {k: v for k, v in data.items() if k != "evidence_origins"}
        payload.update(mode="memory_consolidation", instruction=CONSOLIDATION_INSTRUCTION)
        # An interrupted contract repair is useful draft work, not a published
        # rule. Reuse it only against the same archive/feedback and revalidate
        # every citation before asking the model to repair it.
        for prior in sorted(directory.glob('*/input.json'), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                previous = loads(prior.read_text())
                if (previous.get('user_feedback') != data['user_feedback']
                        or loads((prior.parent / 'archive-before.json').read_text()) != archive):
                    continue
                journal_path = prior.parent / 'events.jsonl'
                candidates = [loads(line) for line in journal_path.read_text().splitlines()]
                draft = next((v['report'] for v in reversed(candidates) if v['event'] == 'review_candidate'), None)
                if draft:
                    validate_review(draft, data['events'], data['scope']['task'])
                    payload['draft_review'] = draft
                    payload['instruction'] += '\nRepair the supplied draft: retain its evidence-backed intent and citations, fit each complete rule within 300 characters, and correct keyword logic for retrieval. Spell out inside/outside same-wall finger contact when checking a bowl pinch. Do not replace concrete actions with generic warnings.'
                    assistant.output('analyzer: Reusing an unpublished draft; repairing rule length and retrieval conditions')
                    break
            except (OSError, ValueError):
                continue
        # Keep raw archives and prior role outputs recoverable for comparison.
        write_json(run_dir / "input.json", payload)
        write_json(run_dir / "evidence-origins.json", data["evidence_origins"])
        write_json(run_dir / "archive-before.json", archive)
        for role, path in ROLE_MEMORY_PATHS.items():
            target = confined(assistant.root, path)
            if target.exists():
                write_json(run_dir / f"{role}-before.json", loads(target.read_text()))
        path = run_dir / "candidate.json"
        report = {"status": "reviewing", "stage": "memory_consolidation", "model": assistant.model.get("model"),
            "user_feedback": data["user_feedback"], "changed_files": [], "omissions": data["omissions"]}
        write_json(run_dir / "report.json", report)
        try:
            if path.exists() and not force:
                reply = validate_review(loads(path.read_text()), data["events"], data["scope"]["task"])
                validate_consolidation(reply, payload)
            else:
                assistant.output(f"analyzer: Consolidating {len(data['lesson_catalog'])} lessons against operator priorities")
                reply = assistant._request(payload, [], Journal(run_dir), data["events"], data["scope"]["task"], run_dir)
                write_json(path, reply)
            changed = Experience(assistant.root).merge(reply["findings"], data["scope"],
                {"run": run_dir.relative_to(assistant.root).as_posix(), "events_sha256": data["evidence_sha256"],
                 "evidence_sha256": data["evidence_sha256"], "evidence_origins": data["evidence_origins"],
                 "reviewer": "analyzer memory consolidation", "language": "en", "model": assistant.model.get("model"),
                 "reasoning_effort": assistant.model.get("reasoning_effort"), "user_feedback": data["user_feedback"]},
                expected_archive_sha256=hashlib.sha256(dumps(archive).encode()).hexdigest())
            report.update(status="applied" if changed else "no_change", stage="completed", review=reply,
                changed_files=[MEMORY_PATH, *ROLE_MEMORY_PATHS.values()] if changed else [])
            assistant.output(f"analyzer: Consolidation complete; {len(changed)} findings, report: {run_dir / 'report.json'}")
        except (ModelError, OSError, ValueError, RuntimeError) as exc:
            report.update(status="failed", error=redact(str(exc)))
            assistant.output("analyzer: Consolidation failed; existing memory preserved: " + report["error"])
        except KeyboardInterrupt:
            report.update(status='interrupted')
            write_json(run_dir / 'report.json', report)
            raise
        write_json(run_dir / "report.json", report)
        return report
