"""Summarize Codex CLI failures without exposing credentials or retry chatter."""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit, urlunsplit


_URL = re.compile(r"https?://[^\s<>\"']+", re.I)
_KEY = re.compile(r"\bsk-[A-Za-z0-9_*.-]+", re.I)
_BEARER = re.compile(r"\bBearer\s+(?:\[[^\]]*\]|[^\s,;\"'<>)}\]]+)", re.I)
_CREDENTIAL = re.compile(
    r"(\b(?:api[-_ ]?key(?:\s+provided)?|access[-_ ]?token|authorization)"
    r"\b[\"']?\s*[:=]\s*)(?:Bearer\s+)?(?:\"[^\"]*\"|'[^']*'|\[[^\]]*\]|[^\s,;&)}\]]+)", re.I)
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_RECONNECT = re.compile(r"^Reconnecting(?:\.{3}|\u2026)?\s*\d+/\d+\s*(?:\((.*)\))?$", re.I | re.S)
_NOISE = re.compile(r"^(?:Reading prompt from stdin|Falling back from WebSockets to HTTPS transport)\b", re.I)
_GENERIC = re.compile(r"^(?:error|failed|turn failed|request failed|codex failed)[.!:]?$", re.I)


def _safe_url(match):
    value = match.group(0)
    try:
        parts = urlsplit(value)
        # Error summaries need the endpoint, never userinfo or query credentials.
        host = parts.netloc.rsplit("@", 1)[-1]
        return urlunsplit((parts.scheme, host, parts.path, "", ""))
    except ValueError:
        return "[URL 已隐藏]"


def _redact(text):
    value = _ANSI.sub("", text)
    value = _URL.sub(_safe_url, value)
    value = _CREDENTIAL.sub(lambda match: match.group(1) + "[密钥已隐藏]", value)
    value = _BEARER.sub("Bearer [密钥已隐藏]", value)
    value = _KEY.sub("[密钥已隐藏]", value)
    return " ".join(value.split())


def _message(value, depth=0):
    """Accept the CLI's string/dictionary error variants; skip malformed values."""
    if depth > 6:
        return ""
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    text = ""
    for name in ("message", "error", "detail", "text"):
        text = _message(value.get(name), depth + 1)
        if text:
            break
    metadata = []
    for name in ("code", "status_code", "status", "url", "endpoint"):
        item = value.get(name)
        if isinstance(item, str) and item.strip() or type(item) is int:
            metadata.append(f"{name}: {item}")
    return "; ".join(([text] if text else []) + metadata)


def _events(value, depth=0):
    if depth > 6:
        return
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (ValueError, RecursionError):
            for line in value.splitlines():
                try:
                    decoded = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                yield from _events(decoded, depth + 1)
        else:
            yield from _events(decoded, depth + 1)
    elif isinstance(value, dict):
        yield value
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _events(item, depth + 1)


def _structured_candidates(records):
    for event in _events(records):
        kind = event.get("type")
        if kind == "turn.failed":
            underlying = _message(event.get("error"))
            message = _message({**event, "message": underlying}) if underlying else _message(event)
            if message:
                yield 3, message
        item = event.get("item")
        if isinstance(item, dict) and (item.get("type") == "error" or item.get("error")):
            message = _message(item)
            if message:
                yield 2, message
        if kind == "error":
            message = _message(event)
            if message:
                yield 1, message


def _plain_candidates(value):
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if not isinstance(value, str):
        return
    for line in value.splitlines():
        line = _ANSI.sub("", line).strip()
        if not line:
            continue
        # Do not display successful agent messages or entire JSON audit records.
        try:
            json.loads(line)
        except (ValueError, RecursionError):
            yield 0, line


def _choose(candidates):
    useful, retries, generic = [], [], []
    for index, (priority, message) in enumerate(candidates):
        message = _ANSI.sub("", message).strip()
        if not message or _NOISE.match(message):
            continue
        reconnect = _RECONNECT.match(message)
        if reconnect:
            if reconnect.group(1):
                retries.append((priority, index, reconnect.group(1)))
        elif _GENERIC.fullmatch(message):
            generic.append((priority, index, message))
        else:
            useful.append((priority, index, message))
    choices = useful or retries or generic
    return max(choices)[2] if choices else ""


def codex_failure_detail(records, stderr, stdout="") -> str:
    """Return a bounded, redacted failure detail for UI/logging (no I/O).

    Prefer the final concrete turn.failed error, then item errors, then error
    events. stderr/stdout text is only a fallback. JSON, JSONL, mixed records,
    and malformed entries are accepted; progress messages are not diagnoses.
    """
    structured = list(_structured_candidates(records))
    message = _choose(structured)
    if not message:
        message = _choose(list(_structured_candidates(stdout)) + list(_structured_candidates(stderr)))
    if not message:
        message = _choose(list(_plain_candidates(stdout)) + list(_plain_candidates(stderr)))
    if not message:
        return "Codex 未提供具体错误详情；请查看本轮 codex-events.json。"
    safe = _redact(message)
    labels = []
    if re.search(r"\b401\b", safe):
        labels.append("HTTP 401")
    if re.search(r"invalid_api_key", safe, re.I):
        labels.append("invalid_api_key")
    if labels or re.search(r"incorrect api key|unauthorized", safe, re.I):
        heading = "API 密钥认证失败" + (f"（{'，'.join(labels)}）" if labels else "")
    elif re.search(r"\b403\b|permission_denied", safe, re.I):
        heading = "API 拒绝访问，请核对当前提供方和账号权限"
    elif re.search(r"\b429\b|rate_limit|insufficient_quota", safe, re.I):
        heading = "API 请求受限，请核对速率或可用额度"
    elif re.search(r"timed? ?out|timeout", safe, re.I):
        heading = "Codex 请求超时"
    else:
        heading = "Codex 调用失败"
    explicit = re.search(r"\b(?:url|endpoint)\s*[:=]\s*(https?://[^\s,;]+)", safe, re.I)
    urls = [match.group(0).rstrip(",.;)") for match in _URL.finditer(safe)]
    endpoint = explicit.group(1).rstrip(",.;)") if explicit else next(
        (url for url in urls if re.search(r"/(?:responses|chat/completions)(?:/|$)", url)), "")
    prefix = heading + (f"；请求端点：{endpoint}" if endpoint else "") + "。详情："
    # Redact before truncation so shortened credentials cannot escape masking.
    result = prefix + safe
    return result if len(result) <= 3000 else result[:2999] + "…"
