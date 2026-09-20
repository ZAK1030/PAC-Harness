"""Inherit Codex model routing without loading unrelated tools or shell policy."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import re
import tomllib
from urllib.parse import urlsplit, urlunsplit


class CodexConnectionError(ValueError):
    pass


def _read(path):
    try:
        return tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raise CodexConnectionError(f"无法读取 Codex 模型连接配置：{path}") from None


def _toml(value):
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(k) + "=" + _toml(v) for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ",".join(_toml(v) for v in value) + "]"
    if isinstance(value, (str, bool, int, float)):
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    raise CodexConnectionError("Codex 模型连接配置包含不支持的字段类型")


def _merge(base, overlay):
    result = deepcopy(base)
    for key, value in overlay.items():
        result[key] = (_merge(result[key], value) if isinstance(result.get(key), dict)
                       and isinstance(value, dict) else deepcopy(value))
    return result


def model_connection(settings, environ=None):
    """Return CLI overrides, selected auth environment and a credential-free audit.

    auth.json/keyring remain owned by Codex. Never read/copy them, and do not
    inherit hooks, MCP servers, plugins or execution permissions from user config.
    """
    environ = os.environ if environ is None else environ
    codex_dir = Path(environ.get("CODEX_HOME") or Path.home() / ".codex")
    path = codex_dir / "config.toml"
    config = _read(path) if path.is_file() else {}
    profile = settings.get("profile", config.get("profile"))
    if profile:
        if not isinstance(profile, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", profile):
            raise CodexConnectionError("Codex profile 名称无效")
        profile_path = codex_dir / f"{profile}.config.toml"
        overlay = (_read(profile_path) if profile_path.is_file()
                   else config.get("profiles", {}).get(profile))
        if not isinstance(overlay, dict):
            raise CodexConnectionError(f"找不到 Codex 模型 profile：{profile}")
        config = _merge(config, overlay)

    provider = config.get("model_provider", "openai")
    if not isinstance(provider, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", provider):
        raise CodexConnectionError("Codex model_provider 无效")
    arguments, auth_env = [], {}

    def setting(key, value):
        arguments.extend(["-c", key + "=" + _toml(value)])

    # Only model/routing/auth selection is inherited. Execution stays isolated.
    for key in ("model", "model_reasoning_effort", "cli_auth_credentials_store"):
        if key in config:
            setting(key, config[key])
    if settings.get("model"):
        setting("model", settings["model"])
    if settings.get("reasoning_effort"):
        setting("model_reasoning_effort", settings["reasoning_effort"])
    setting("model_provider", provider)
    if provider == "openai":
        for key in ("openai_base_url", "chatgpt_base_url"):
            if key in config:
                setting(key, config[key])
        endpoint = config.get("openai_base_url", "https://api.openai.com/v1")
    else:
        source = config.get("model_providers", {}).get(provider)
        if not isinstance(source, dict) or not source.get("base_url"):
            raise CodexConnectionError(f"Codex provider {provider} 缺少 base_url；未回退到其他服务")
        if source.get("auth"):
            raise CodexConnectionError("ToUser 暂不执行 provider 的外部认证命令；请使用 Codex 登录或 env_key")
        for field in ("query_params", "aws"):
            if source.get(field):
                raise CodexConnectionError(f"ToUser 尚未接入 provider.{field}；未忽略该字段或回退到其他服务")
        allowed = {"name", "base_url", "wire_api", "requires_openai_auth", "env_key",
                   "env_key_instructions", "supports_websockets", "request_max_retries",
                   "stream_max_retries", "stream_idle_timeout_ms", "websocket_connect_timeout_ms"}
        selected = {key: deepcopy(value) for key, value in source.items() if key in allowed}
        endpoint = selected["base_url"]
        if selected.get("wire_api", "responses") != "responses":
            raise CodexConnectionError("ToUser 的 Codex provider 需要 Responses API")
        headers = deepcopy(source.get("env_http_headers", {}))
        # Keep literal credentials out of process arguments and diagnostic logs.
        for number, (header, value) in enumerate(source.get("http_headers", {}).items()):
            variable = f"HARNESS_CODEX_HEADER_{number}"
            auth_env[variable] = str(value)
            headers[header] = variable
        if source.get("experimental_bearer_token"):
            selected["env_key"] = "HARNESS_CODEX_PROVIDER_KEY"
            auth_env[selected["env_key"]] = str(source["experimental_bearer_token"])
        if headers:
            selected["env_http_headers"] = headers
        names = list(headers.values()) + ([selected["env_key"]] if selected.get("env_key") else [])
        for name in names:
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise CodexConnectionError("Codex provider 的认证环境变量名无效")
            if name not in auth_env and name in environ:
                auth_env[name] = environ[name]
        if selected.get("env_key") and selected["env_key"] not in auth_env:
            raise CodexConnectionError(f"Codex provider 所需环境变量未设置：{selected['env_key']}")
        setting("model_providers." + provider, selected)
    if "CODEX_API_KEY" in environ:
        auth_env["CODEX_API_KEY"] = environ["CODEX_API_KEY"]

    try:
        url = urlsplit(endpoint)
        if url.scheme not in {"http", "https"} or not url.hostname:
            raise ValueError
        clean_endpoint = urlunsplit((url.scheme, url.hostname + (f":{url.port}" if url.port else ""), url.path, "", ""))
    except (TypeError, ValueError):
        raise CodexConnectionError("Codex provider 的 API 地址无效") from None
    audit = {"config_source": str(path) if path.is_file() else "Codex defaults", "profile": profile,
             "model_provider": provider, "base_url": clean_endpoint,
             "model": settings.get("model", config.get("model", "Codex default")),
             "reasoning_effort": settings.get("reasoning_effort", config.get("model_reasoning_effort")),
             "scope": "model connection only; execution settings remain controlled by ToUser"}
    return arguments, auth_env, audit
