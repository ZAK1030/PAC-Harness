from __future__ import annotations

from copy import deepcopy

from pac_harness.storage import loads, write_json
from pac_harness.tools import parameters


class ListEnvironment:
    def __init__(self, directory, items):
        if not isinstance(items, list) or any(type(item) is not int for item in items):
            raise ValueError("items must contain integers")
        self.path = directory / "example_environment.json"
        self.data = loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {
            "items": items, "target": sorted(items), "receipts": {}}

    def observe(self):
        return {"items": list(self.data["items"]), "target": list(self.data["target"])}

    def actions(self):
        return [{"name": "replace_items", "description": "Replace the list with a permutation of its current items.",
                 "parameters": parameters({"items": {"type": "array", "items": {"type": "integer"}}}, ("items",))}]

    def validate(self, action, observation):
        if sorted(action["arguments"]["items"]) != sorted(observation["items"]):
            raise ValueError("Action must preserve the current list's elements")

    def execute(self, action, request_id):
        if request_id in self.data["receipts"]:
            return deepcopy(self.data["receipts"][request_id])
        self.validate(action, self.observe())
        self.data["items"] = list(action["arguments"]["items"])
        result = {"ok": True, "items": list(self.data["items"])}
        self.data["receipts"][request_id] = result
        write_json(self.path, self.data)
        return deepcopy(result)

    def reconcile(self, request_id):
        if request_id in self.data["receipts"]:
            return {"status": "completed", "result": deepcopy(self.data["receipts"][request_id])}
        return {"status": "not_executed"}

    def tools(self):
        return []

    def close(self):
        pass


def create(*, root, run_directory, config):
    return ListEnvironment(run_directory, config.get("items", [4, 1, 3, 2]))


class DemoPlanner:
    def respond(self, payload, toolbox, journal):
        observation = payload["observation"]
        complete = observation["items"] == observation["target"]
        result = {"reasoning": "列表已达到目标，提交完成核对。" if complete else "一次替换为目标顺序，然后检查结果。",
                  "remaining_plan": [] if complete else ["验证排序结果"],
                  "action": {"name": "done", "arguments": {}} if complete else {
                      "name": "replace_items", "arguments": {"items": observation["target"]}}}
        journal.event("planner", report=result, backend="deterministic_demo")
        return result


class DemoDetector:
    def respond(self, payload, toolbox, journal):
        before, after = payload["before"], payload["after"]
        complete = after["items"] == after["target"]
        result = {"status": "verified" if complete else "anomaly", "reason": "按当前列表与目标顺序逐项比较。",
                  "evidence": [f"current={after['items']}; target={after['target']}"], "task_complete": complete,
                  "progress": "improved" if complete and before != after else "unchanged", "facts": {"sorted": complete}}
        journal.event("detector", report=result, backend="deterministic_demo")
        return result
