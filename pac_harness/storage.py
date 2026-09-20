from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import stat
import tempfile
import time


def dumps(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def loads(value):
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError(f"Duplicate JSON field: {key}")
            result[key] = item
        return result

    def invalid(value):
        raise ValueError(f"Non-finite JSON value: {value}")

    return json.loads(value, object_pairs_hook=pairs, parse_constant=invalid)


def confined(root, relative):
    root = Path(root).resolve()
    if not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative:
        raise ValueError("Expected a project-relative POSIX path")
    if relative.startswith("/") or any(part in {"", ".", ".."} for part in relative.split("/")):
        raise ValueError("Invalid relative path")
    path = root
    for part in relative.split("/"):
        path = path / part
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("Linked paths are not supported")
    if not path.resolve().is_relative_to(root):
        raise ValueError("Path escapes project")
    return path


def atomic_write(path, raw):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path, value):
    atomic_write(path, (dumps(value) + "\n").encode("utf-8"))


class RunLock:
    def __init__(self, directory):
        self.path = Path(directory) / "run.lock"

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.stream.write(b"0")
                self.stream.flush()
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            raise RuntimeError("Run directory is already in use") from None
        return self

    def __exit__(self, *args):
        self.stream.close()


class Journal:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "events.jsonl"

    def event(self, event, **fields):
        value = {"time_ns": time.time_ns(), "event": event, **fields}
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(dumps(value) + "\n")
            stream.flush()
        return value

    def search(self, query="", limit=10):
        if not self.path.exists():
            return []
        matches = []
        with self.path.open(encoding="utf-8") as stream:
            for line in stream:
                if query.casefold() in line.casefold():
                    try:
                        matches.append(loads(line))
                    except ValueError:
                        continue
                    matches = matches[-limit:]
        return matches


class TaskState:
    def __init__(self, path, task, identity, *, resume=False):
        self.path = Path(path)
        if resume:
            self.data = loads(self.path.read_text(encoding="utf-8"))
            if self.data.get("schema_version") != 1 or self.data.get("task") != task:
                raise ValueError("Checkpoint task/schema mismatch")
            if self.data.get("identity") != identity:
                raise ValueError("Checkpoint project/adapter configuration mismatch")
            if self.data["phase"] == "executing":
                self.data["phase"] = "execution_unknown"
        else:
            if self.path.exists():
                raise ValueError("Run already exists; use resume or a new directory")
            self.data = {
                "schema_version": 1, "task": task, "identity": identity,
                "phase": "ready", "turn": 0, "remaining_plan": [], "facts": {},
                "guidance": [], "history": [], "action": None, "execution": None,
                "detector": None, "before": None, "after": None, "request_id": None,
            }
        self.save()

    def save(self):
        write_json(self.path, self.data)

    def update(self, **fields):
        self.data.update(deepcopy(fields))
        self.save()

    def remember(self, event, **fields):
        self.data["history"].append({"event": event, "turn": self.data["turn"], **deepcopy(fields)})
        self.data["history"] = self.data["history"][-50:]
        self.save()

    def guidance(self, text):
        if text.strip():
            self.data["guidance"].append({"source": "user_via_to_user", "text": text})
            self.save()

    def context(self):
        return deepcopy(self.data)
