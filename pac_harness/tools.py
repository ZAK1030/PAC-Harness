from __future__ import annotations

from dataclasses import dataclass
import base64
import hashlib
from pathlib import Path
import re
from typing import Callable

from .contracts import validate_value
from .storage import confined, dumps


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Callable
    roles: tuple = ("planner", "detector")


@dataclass(frozen=True)
class ToolResult:
    data: dict
    content: list


class Toolbox:
    def __init__(self):
        self.tools = {}

    def register(self, tool):
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", tool.name) or tool.name in self.tools:
            raise ValueError("Invalid or duplicate tool name")
        self.tools[tool.name] = tool

    def catalog(self, role):
        return [{"name": tool.name, "description": tool.description, "parameters": tool.parameters}
                for tool in self.tools.values() if role in tool.roles]

    def invoke(self, role, name, arguments):
        if not isinstance(name, str) or name not in self.tools or role not in self.tools[name].roles:
            raise ValueError("Tool not available to this role")
        tool = self.tools[name]
        validate_value(arguments, tool.parameters)
        result = tool.handler(**arguments)
        dumps(result.data if isinstance(result, ToolResult) else result)
        if isinstance(result, ToolResult):
            dumps(result.content)
        return result


def parameters(properties=None, required=()):
    return {"type": "object", "properties": properties or {}, "required": list(required), "additionalProperties": False}


def inspect_artifact(root, path, observations):
    available = {artifact.get("path") for observation in observations if isinstance(observation, dict)
                 for artifact in observation.get("artifacts", []) if isinstance(artifact, dict)}
    if path not in available:
        raise ValueError("Artifact must be declared by the current or inspected observations")
    source = confined(root, path)
    if source.stat().st_size > 8_000_000:
        raise ValueError("Artifact exceeds 8 MB")
    raw = source.read_bytes()
    metadata = {"path": path, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    suffix = source.suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg"}:
        mime = "image/png" if suffix == ".png" else "image/jpeg"
        content = [{"type": "image_url", "image_url": {"url": f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")}}]
        return ToolResult(metadata, content)
    if suffix in {".txt", ".md", ".json", ".jsonl", ".csv"}:
        if len(raw) > 100_000:
            raise ValueError("Text artifact exceeds 100 KB; expose a targeted adapter read tool")
        return {**metadata, "text": raw.decode("utf-8-sig")}
    raise ValueError("Use an adapter read tool for this artifact type")


class Knowledge:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def text(self, relative):
        path = confined(self.root, relative)
        if not path.exists():
            return ""
        if path.stat().st_size > 200_000:
            raise ValueError("Knowledge file exceeds 200 KB")
        return path.read_text(encoding="utf-8")

    def skills(self):
        directory = confined(self.root, "skills")
        return [{"name": path.parent.name, "preview": self.text(path.relative_to(self.root).as_posix())[:500]}
                for path in sorted(directory.glob("*/SKILL.md"))]

    def read_skill(self, name):
        if not re.fullmatch(r"[a-z0-9_-]+", name):
            raise ValueError("Invalid skill name")
        value = self.text(f"skills/{name}/SKILL.md")
        if not value:
            raise ValueError("Skill does not exist")
        return {"name": name, "content": value}

    def context(self, role):
        return {
            "shared": self.text("memories/shared.md"),
            "role_memory": self.text(f"memories/{role}.md"),
            "instructions": self.text(f"prompts/{role}.md"),
            "skills": self.skills(),
        }
