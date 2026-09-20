from __future__ import annotations

import math
from typing import Protocol


class Environment(Protocol):
    def observe(self) -> dict: ...
    def actions(self) -> list[dict]: ...
    def validate(self, action: dict, observation: dict) -> None: ...
    def execute(self, action: dict, request_id: str) -> dict: ...
    def reconcile(self, request_id: str) -> dict: ...
    def tools(self) -> list: ...
    def close(self) -> None: ...


def validate_value(value, schema, path="arguments"):
    kinds = {
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "boolean": lambda item: type(item) is bool,
        "integer": lambda item: type(item) is int,
        "number": lambda item: type(item) in (int, float) and math.isfinite(item),
        "null": lambda item: item is None,
    }
    kind = schema.get("type")
    if kind not in kinds or not kinds[kind](value):
        raise ValueError(f"{path}: expected {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path}: value outside enum")
    if kind in {"integer", "number"}:
        if "minimum" in schema and value < schema["minimum"]:
            raise ValueError(f"{path}: below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise ValueError(f"{path}: above maximum")
    if kind == "object":
        properties = schema.get("properties", {})
        if set(schema.get("required", [])) - value.keys():
            raise ValueError(f"{path}: missing required fields")
        if schema.get("additionalProperties") is False and value.keys() - properties.keys():
            raise ValueError(f"{path}: unexpected fields")
        for name, child in properties.items():
            if name in value:
                validate_value(value[name], child, path + "." + name)
    if kind == "array" and "items" in schema:
        for index, item in enumerate(value):
            validate_value(item, schema["items"], f"{path}[{index}]")


def validate_plan(value, actions):
    if not isinstance(value, dict) or set(value) != {"reasoning", "remaining_plan", "action"}:
        raise ValueError("Planner must return reasoning, remaining_plan, action")
    if not isinstance(value["reasoning"], str) or not value["reasoning"].strip():
        raise ValueError("Planner reasoning is required")
    if not isinstance(value["remaining_plan"], list) or any(not isinstance(item, str) for item in value["remaining_plan"]):
        raise ValueError("remaining_plan must be a string list")
    action = value["action"]
    if not isinstance(action, dict) or set(action) != {"name", "arguments"}:
        raise ValueError("Action must contain name and arguments")
    options = {item["name"]: item for item in actions}
    options["done"] = {"parameters": {"type": "object", "properties": {}, "additionalProperties": False}}
    if not isinstance(action["name"], str) or action["name"] not in options:
        raise ValueError("Unknown action")
    validate_value(action["arguments"], options[action["name"]]["parameters"])


def validate_report(value):
    required = {"status", "reason", "evidence", "task_complete", "progress", "facts"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("Detector report fields are invalid")
    if value["status"] not in {"verified", "uncertain", "anomaly"}:
        raise ValueError("Invalid Detector status")
    if not isinstance(value["reason"], str) or not value["reason"].strip():
        raise ValueError("Detector reason is required")
    if not isinstance(value["evidence"], list) or not value["evidence"] or any(not isinstance(item, str) or not item.strip() for item in value["evidence"]):
        raise ValueError("Detector evidence is required")
    if type(value["task_complete"]) is not bool or not isinstance(value["facts"], dict):
        raise ValueError("Invalid task_complete/facts")
    if value["progress"] not in {"improved", "unchanged", "worsened", "unknown"}:
        raise ValueError("Invalid progress")
    if value["task_complete"] and value["status"] != "verified":
        raise ValueError("Only verified evidence can establish completion")
