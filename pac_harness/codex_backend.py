from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from .codex_connection import model_connection, CodexConnectionError
from .codex_errors import codex_failure_detail
from .redaction import redact as _redact

class ToUserError(RuntimeError):
    pass

RESPONSE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["status", "intent", "message", "summary", "session_guidance"],
    "properties": {
        "status": {"type": "string", "enum": ["reply", "question", "edited"]},
        "intent": {"type": "string", "enum": ["analysis", "task_guidance", "persistent_update", "return_to_task"]},
        "message": {"type": "string"},
        "summary": {"type": "string"},
        "session_guidance": {"type": "string"},
    },
}


def validate_reply(reply):
    """Validate both CLI replies and injected backends before using their intent."""
    if (not isinstance(reply, dict) or set(reply) != set(RESPONSE_SCHEMA["required"])
            or any(not isinstance(reply.get(key), str) for key in RESPONSE_SCHEMA["required"])
            or reply["status"] not in RESPONSE_SCHEMA["properties"]["status"]["enum"]
            or reply["intent"] not in RESPONSE_SCHEMA["properties"]["intent"]["enum"]):
        raise ToUserError("Codex 对话结果字段无效，未应用修改。")
    return reply


def _safe_env():
    """Keep process/runtime essentials; do not pass application API/arm credentials."""
    allowed = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
               "USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA", "PROGRAMFILES",
               "PROGRAMFILES(X86)", "PROGRAMDATA", "CONDA_PREFIX", "VIRTUAL_ENV",
               "CODEX_HOME", "LANG", "LC_ALL"}
    result = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    result.update(PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1",
                  HARNESS_OFFLINE="1", HARNESS_TO_USER="1")
    # Linux Codex installations may need these for locale and native libraries;
    # application secrets are still excluded.
    for key in ("TERM", "COLORTERM", "XDG_RUNTIME_DIR", "LD_LIBRARY_PATH"):
        if key in os.environ:
            result[key] = os.environ[key]
    return result


def find_codex(configured=None):
    if configured:
        path = Path(configured).expanduser()
        if path.is_file():
            return str(path.resolve())
        raise ToUserError("配置的 Codex CLI 不存在：" + str(path))
    found = shutil.which("codex")
    if found:
        return found
    candidates = sorted(Path.home().glob(
        ".vscode/extensions/openai.chatgpt-*/bin/*/codex*"), reverse=True)
    if candidates:
        return str(candidates[0])
    raise ToUserError("未找到本地 Codex CLI；请安装或设置 to_user.codex_path。")


class CodexBackend:
    """Machine-readable Codex sessions; subprocess injection keeps tests offline."""

    def __init__(self, config=None, runner=None):
        self.config = dict(config or {})
        self.runner = runner or subprocess.run
        self.thread_id = None

    def _settings(self):
        settings = ["-c", 'sandbox_mode="workspace-write"', "-c",
                    "sandbox_workspace_write.network_access=false", "-c",
                    'approval_policy="never"', "-c", 'web_search="disabled"']
        if os.name == "nt":
            settings += ["-c", 'windows.sandbox="unelevated"']
        return settings

    def reply(self, stage, prompt, *, audit, images=()):
        cli = find_codex(self.config.get("codex_path"))
        try:
            connection_args, auth_env, connection = model_connection(self.config)
        except CodexConnectionError as exc:
            raise ToUserError(str(exc)) from exc
        (audit / "codex-connection.json").write_text(
            json.dumps(connection, ensure_ascii=False, indent=2), encoding="utf-8")
        schema = audit / "response-schema.json"
        schema.write_text(json.dumps(RESPONSE_SCHEMA), encoding="utf-8")
        command = [cli, "exec", *connection_args, *self._settings()]
        if self.thread_id:
            command += ["resume", self.thread_id]
        else:
            command += ["--sandbox", "workspace-write", "--cd", str(stage)]
        command += ["--ignore-user-config", "--ignore-rules", "--skip-git-repo-check",
                    "--json", "--color", "never"] if not self.thread_id else [
                        "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check", "--json"]
        command += ["--output-schema", str(schema)]
        if self.config.get("model"):
            command += ["--model", str(self.config["model"])]
        for path in images:
            command += ["--image", str(path)]
        command += ["-"]
        try:
            result = self.runner(command, input=prompt, cwd=str(stage), env={**_safe_env(), **auth_env},
                                 text=True, encoding="utf-8", errors="replace", capture_output=True,
                                 timeout=self.config.get("timeout_s", 900), shell=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ToUserError("Codex 对话未完成：" + str(exc)) from exc
        records, message = [], None
        for line in (result.stdout or "").splitlines():
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(event, dict):
                continue
            records.append(_redact(event))
            if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
                self.thread_id = event["thread_id"]
            item = event.get("item", {})
            if (event.get("type") == "item.completed" and isinstance(item, dict)
                    and item.get("type") == "agent_message"):
                message = item.get("text")
        (audit / "codex-events.json").write_text(
            json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        if result.returncode:
            detail = codex_failure_detail(records, result.stderr, result.stdout)
            raise ToUserError(f"Codex 返回错误 {result.returncode}：{detail}\n详情：{audit / 'codex-events.json'}")
        try:
            reply = json.loads(message)
        except (ValueError, TypeError) as exc:
            raise ToUserError("Codex 没有返回完整的结构化对话结果，未应用修改。") from exc
        return validate_reply(reply)

    def validate(self, stage, *, audit):
        cli = find_codex(self.config.get("codex_path"))
        # The verifier is a fixed offline command, never a model-provided shell string.
        code = ("import pathlib,sys,unittest; "
                "suite=unittest.defaultTestLoader.discover('tests'); "
                "count=suite.countTestCases(); "
                "print('TO_USER_TEST_COUNT='+str(count)); "
                "result=unittest.TextTestRunner(verbosity=1).run(suite); "
                "sys.exit(0 if count and result.wasSuccessful() else 1)")
        # The staged workspace is already isolated. Windows uses the Codex
        # sandbox command; on POSIX run only the fixed test command in that
        # staging directory so the same verifier works without Windows profiles.
        if os.name == "nt":
            command = [cli, "sandbox", "--permission-profile", "harness_to_user",
                       "-c", 'permissions.harness_to_user.extends=":workspace"',
                       "-c", "permissions.harness_to_user.network.enabled=false", "--cd", str(stage),
                       sys.executable, "-X", "utf8", "-c", code]
        else:
            command = [sys.executable, "-X", "utf8", "-c", code]
        try:
            result = self.runner(command, cwd=str(stage), env=_safe_env(), capture_output=True,
                                 text=True, encoding="utf-8", errors="replace", shell=False,
                                 timeout=self.config.get("test_timeout_s", 180))
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ToUserError("离线验证无法完成，未应用修改：" + str(exc)) from exc
        output = (result.stdout or "") + (result.stderr or "")
        (audit / "offline-tests.txt").write_text(_redact(output), encoding="utf-8")
        if result.returncode or not re.search(r"TO_USER_TEST_COUNT=[1-9]\d*", output):
            raise ToUserError("离线测试或其隔离环境未通过，未应用修改。详见 " + str(audit / "offline-tests.txt"))
        return {"kind": "offline_unittest_in_sandbox", "passed": True,
                "log": str(audit / "offline-tests.txt")}
