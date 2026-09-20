from __future__ import annotations

import argparse
import importlib
import os
from pathlib import Path
import sys
import time
import uuid

from .agents import Agent, ChatClient
from .console_hotkey import ConsoleHotkey
from .runtime import Harness
from .storage import RunLock, TaskState, confined, dumps, loads
from .to_user import ToUser
from .tools import Knowledge


def _main(argv=None):
    parser = argparse.ArgumentParser(description="PAC-Harness: scenario-independent Planner → action → Detector runtime")
    parser.add_argument("--root", default=".")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--task")
    parser.add_argument("--run-dir")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--demo", action="store_true", help="Use deterministic agents and the local list example; no API calls")
    parser.add_argument("--preview", action="store_true", help="Plan without dispatching a new action")
    parser.add_argument("--assist", action="store_true", help="Explicitly open ToUser for this project or run")
    parser.add_argument("--web", action="store_true", help="Open the local scene wizard and ToUser workbench")
    parser.add_argument("--port", type=int, default=8765)
    options = parser.parse_args(argv)
    root = Path(options.root).resolve()
    if options.web:
        from .workbench import serve
        serve(root, port=options.port)
        return 0
    config = loads(confined(root, options.config).read_text(encoding="utf-8"))
    if options.resume and not options.run_dir:
        parser.error("--resume requires --run-dir")
    relative = options.run_dir or f"logs/{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    directory = confined(root, relative)
    identity = {"root": str(root), "adapter": config["adapter"]}
    if options.assist:
        with RunLock(directory):
            checkpoint = directory / "task_state.json"
            state = None
            if checkpoint.exists():
                saved = loads(checkpoint.read_text(encoding="utf-8"))
                state = TaskState(checkpoint, saved["task"], identity, resume=True)
            result = ToUser(root, config.get("to_user")).run(
                state.context() if state else {"task": options.task or "Project assistance"},
                on_guidance=state.guidance if state else None)
        print(dumps(result))
        return 0
    task = options.task
    if options.resume and not task:
        task = loads((directory / "task_state.json").read_text(encoding="utf-8"))["task"]
    if not task:
        task = "将示例列表按升序排列。" if options.demo else None
    if not task:
        parser.error("--task is required")
    if options.preview and options.resume:
        parser.error("Preview uses a new run; it cannot resume a pending action")
    module_name, separator, factory_name = config["adapter"]["factory"].partition(":")
    if not separator:
        parser.error("adapter.factory must be module:function")
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    factory = getattr(importlib.import_module(module_name), factory_name)
    environment = factory(root=root, run_directory=directory, config=config["adapter"].get("settings", {}))
    if options.demo:
        if config["adapter"]["factory"] != "examples.list_sorting:create":
            parser.error("--demo supports only the bundled list example; configure models for other adapters")
        from examples.list_sorting import DemoPlanner, DemoDetector
        planner, detector = DemoPlanner(), DemoDetector()
    else:
        def role(name):
            settings = config["models"][name]
            if settings["model"] == "SET_YOUR_MODEL":
                parser.error("Configure models or use --demo")
            return Agent(name, ChatClient(settings), Knowledge(root), max_tool_rounds=settings.get("max_tool_rounds", 6))
        planner, detector = role("planner"), role("detector")
    enabled = config.get("to_user", {}).get("enabled", True)
    assistant = ToUser(root, config.get("to_user")) if enabled else None
    hotkey = ConsoleHotkey(enabled=enabled)
    harness = Harness(root, environment, planner, detector, identity=config["adapter"], to_user=assistant, hotkey=hotkey)
    if os.name == "nt":
        print("Ctrl+G：请求 ToUser（在当前模型请求/动作结束后的边界处理）；Ctrl+C：退出。")
    else:
        print("Linux：请使用 --assist 或网页工作台请求 ToUser；Ctrl+C：退出。")
    result = harness.run(task, directory, resume=options.resume, preview=options.preview,
                         max_turns=config.get("run", {}).get("max_turns", 40),
                         max_failures=config.get("run", {}).get("max_failures", 3))
    print(dumps(result))
    print("Run: " + str(directory))
    if result["status"] == "restart_required":
        command = [sys.executable, "-m", "pac_harness", "--root", str(root), "--config", options.config,
                   "--task", task, "--run-dir", relative, "--resume"]
        if options.demo:
            command.append("--demo")
        os.execv(sys.executable, command)
    return 0 if result["status"] in {"completed", "preview", "interrupted"} else 1


def main(argv=None):
    # Serialize CLI activity and web assistance for the entire project, not just a run.
    entry = argparse.ArgumentParser(add_help=False)
    entry.add_argument("--root", default=".")
    entry.add_argument("--web", action="store_true")
    options, _ = entry.parse_known_args(argv)
    if options.web:
        return _main(argv)
    with RunLock(confined(Path(options.root).resolve(), "maintenance/web-active")):
        return _main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
