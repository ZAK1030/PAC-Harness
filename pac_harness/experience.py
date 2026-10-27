"""Scoped, evidence-linked experience shared across runs, never live state."""
from __future__ import annotations

import hashlib
import json
import re
import time

from .redaction import redact
from .storage import RunLock, atomic_write, confined, dumps, loads, write_json


MEMORY_PATH = "maintenance/analyzer/lessons.json"
ROLE_MEMORY_PATHS = {role: f"memories/{role}.analyzer.json" for role in ("planner", "detector")}
MAX_RULE_CHARS = 300
PRIORITIES = {"low": 0, "normal": 1, "critical": 2}


def validate_metadata(item):
    if not isinstance(item.get("priority", "normal"), str) or item.get("priority", "normal") not in PRIORITIES:
        raise ValueError("Invalid finding priority")
    if not isinstance(item.get("applicability", "suite"), str) or item.get("applicability", "suite") not in {"suite", "adapter"}:
        raise ValueError("Invalid finding applicability")
    if item.get("keyword_match", "all") not in ("all", "any"):
        raise ValueError("Invalid keyword matching mode")
    keys = item.get("supersedes", [])
    if (not isinstance(keys, list) or len(keys) > 200
            or any(not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{24}", key) for key in keys)
            or len(set(keys)) != len(keys)):
        raise ValueError("Invalid superseded lesson keys")


def compact_rule(item):
    return " ".join(item[name].strip() for name in
                    ("condition", "lesson", "verification", "recheck_when"))


def english_advice(item):
    return not any(re.search(r"[\u3400-\u4dbf\u4e00-\u9fff]", item[name])
                   for name in ("condition", "lesson", "verification", "recheck_when"))


def run_scope(payload):
    state = payload.get("task_state", payload)
    adapter = state.get("identity", {}).get("adapter", {})
    if not isinstance(adapter, dict):
        adapter = {}
    observation = payload.get("observation", payload.get("after", {})) or {}
    settings = adapter.get("settings", {})
    return {"adapter": adapter.get("factory", ""),
            "suite": observation.get("suite", settings.get("suite", "")),
            "task": state.get("task", observation.get("instruction", ""))}


class Experience:
    def __init__(self, root):
        self.root = root

    def read(self):
        path = confined(self.root, MEMORY_PATH)
        if not path.exists():
            return {"schema_version": 1, "lessons": []}
        if path.stat().st_size > 2_000_000:
            raise ValueError("Experience memory exceeds 2 MB")
        value = loads(path.read_text(encoding="utf-8"))
        if value.get("schema_version") != 1 or not isinstance(value.get("lessons"), list):
            raise ValueError("Invalid experience memory")
        return value

    def context(self, role, payload):
        scope = run_scope(payload)
        if not scope["adapter"]:
            return []
        selected = []
        path = confined(self.root, ROLE_MEMORY_PATHS[role])
        if not path.exists():
            return []
        if path.stat().st_size > 200_000:
            raise ValueError("Analyzer role memory exceeds 200 KB")
        candidates = []
        for index, item in enumerate(loads(path.read_text(encoding="utf-8"))):
            match = item["scope"]
            words = [word.casefold() in scope["task"].casefold() for word in match["task_keywords"]]
            relevant = any(words) if match.get("keyword_match") == "any" else all(words)
            if (match["adapter"] != scope["adapter"] or match["suite"] not in {scope["suite"], "*"}
                    or not relevant):
                continue
            candidates.append((PRIORITIES[item.get("priority", "normal")],
                sum(words), match["suite"] == scope["suite"], index, item))
        seen = set()
        for *_, item in sorted(candidates, key=lambda entry: entry[:-1], reverse=True):
            if item["id"] in seen:
                continue
            item = {"id": item["id"], "rule": item["rule"]}
            if len(item["rule"]) > MAX_RULE_CHARS:
                raise ValueError("Analyzer rule exceeds 300 characters")
            if sum(len(entry["rule"]) for entry in selected) + len(item["rule"]) > 1800:
                continue
            selected.append(item)
            seen.add(item["id"])
            if len(selected) >= 6:
                break
        return selected

    @staticmethod
    def advice(item):
        """Keep the procedure and provenance in context, full audit on disk."""
        value = {k: v for k, v in item.items() if k != "sources"}
        value["sources"] = [{"run": source["run"], "events_sha256": source["events_sha256"],
                             "evidence": source.get("evidence", [])[:4]} for source in item["sources"][-1:]]
        return value

    def merge(self, findings, scope, source, *, expected_archive_sha256=None):
        for item in findings:
            validate_metadata(item)
        if any(len(compact_rule(item)) > MAX_RULE_CHARS or not english_advice(item) for item in findings):
            raise ValueError("Analyzer rules must be English and at most 300 characters")
        # Serialize reviews from multiple workers; re-read inside the lock.
        with RunLock(confined(self.root, "maintenance/experience-write")):
            value = self.read()
            if (expected_archive_sha256 is not None
                    and hashlib.sha256(dumps(value).encode()).hexdigest() != expected_archive_sha256):
                raise ValueError("Experience changed during consolidation; rerun against the latest archive")
            lessons = value["lessons"]
            available = {item["key"]: item for item in lessons}
            # Validate replacements before making any changes. Only supported
            # analyzer findings may retire advice for the same adapter/roles.
            for finding in findings:
                for key in finding.get("supersedes", []):
                    old = available.get(key)
                    if (old is None or old["scope"]["adapter"] != scope["adapter"]
                            or finding["confidence"] != "supported"
                            or not set(old["roles"]).issubset(finding["roles"])):
                        raise ValueError("Supersession requires a supported replacement for the same adapter and roles")
            updated = []
            for finding in findings:
                match = {"adapter": scope["adapter"],
                         "suite": "*" if finding.get("applicability") == "adapter" else scope["suite"],
                         "task_keywords": sorted({word.casefold() for word in finding["task_keywords"]})}
                if finding.get("keyword_match") == "any":
                    match["keyword_match"] = "any"
                key = hashlib.sha256(dumps([finding["id"], match]).encode()).hexdigest()[:24]
                old = next((item for item in lessons if item["key"] == key), None)
                evidence = {**source, "evidence": finding["evidence"]}
                if "evidence_origins" in evidence:
                    evidence["evidence_origins"] = {str(c["event_index"]): source["evidence_origins"][str(c["event_index"])]
                        for c in finding["evidence"]}
                if old and any(s["run"] == source["run"] and s.get("reviewer") == source.get("reviewer")
                               and s.get("language") == source.get("language")
                               and s.get("feedback_sha256") == source.get("feedback_sha256")
                               and s.get("evidence_sha256", s["events_sha256"]) == source.get("evidence_sha256", source["events_sha256"])
                               for s in old["sources"]):
                    continue
                item = {**finding, "key": key, "scope": match, "updated_ns": time.time_ns(),
                        "sources": ([*old["sources"], evidence] if old else [evidence])[-10:]}
                if old:
                    item["roles"] = sorted(set(old["roles"]) | set(item["roles"]))
                item.pop("task_keywords")
                item.pop("evidence")
                # A later uncertain analysis cannot silently replace an established rule.
                if old and old["confidence"] == "supported" and item["confidence"] == "hypothesis":
                    item = {**old, "sources": item["sources"], "updated_ns": item["updated_ns"]}
                # Replaying an old episode must not resurrect advice already
                # replaced by a later evidence-backed consolidation.
                if old and old.get("retired_by"):
                    item["retired_by"] = old["retired_by"]
                for replaced in lessons:
                    if replaced["key"] in finding.get("supersedes", []) and replaced["key"] != key:
                        replaced["retired_by"] = key
                if old:
                    lessons.remove(old)
                lessons.append(redact(item))
                updated.append(key)
            # Retain bounded history; existing evidence remains in each run's report.
            value["lessons"] = lessons[-200:]
            while len(dumps(value).encode("utf-8")) > 1_500_000 and value["lessons"]:
                value["lessons"].pop(0)
            write_json(confined(self.root, MEMORY_PATH), value)
            self._write_roles(value)
            return updated

    def _write_roles(self, value, *, missing_only=False):
        written = []
        for role, path in ROLE_MEMORY_PATHS.items():
            target = confined(self.root, path)
            if missing_only and target.exists():
                continue
            selected = [item for item in value["lessons"]
                        if item["confidence"] == "supported" and not item.get("retired_by") and role in item["roles"]]
            if any(not english_advice(item) or len(compact_rule(item)) > MAX_RULE_CHARS for item in selected):
                raise ValueError("Archived analyzer rules must be English and at most 300 characters")
            rules = [{"id": item["id"], "scope": item["scope"], "priority": item.get("priority", "normal"),
                      "rule": compact_rule(item)} for item in selected]
            atomic_write(target, (json.dumps(rules, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
            written.append(path)
        return written

    def restore_missing(self):
        """Restore deleted role outputs from current archived analyzer advice."""
        with RunLock(confined(self.root, "maintenance/experience-write")):
            if not confined(self.root, MEMORY_PATH).exists():
                raise FileNotFoundError("Analyzer archive is missing")
            return self._write_roles(self.read(), missing_only=True)
