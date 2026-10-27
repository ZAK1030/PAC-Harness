"""Strict response decoding with separate transport and model-output errors."""
from __future__ import annotations

import hashlib
import re

from .storage import loads


class ModelError(RuntimeError):
    def __init__(self, message, *, stage="request", retryable=False, diagnostics=None):
        super().__init__(message)
        self.stage, self.retryable = stage, retryable
        self.diagnostics = {"stage": stage, **(diagnostics or {})}


class ModelOutputError(ModelError):
    def __init__(self, message, text, diagnostics):
        super().__init__(message, stage="model_output", diagnostics=diagnostics)
        self.text = text


def preview(text, secret="", limit=2000):
    """Never log authentication or embedded image bytes, including echoed ones."""
    if secret:
        text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"sk-[A-Za-z0-9_-]+", "[REDACTED]", text)
    text = re.sub(r"(?i)Bearer\s+[^\s\"<>]+", "Bearer [REDACTED]", text)
    text = re.sub(r"data:image/[^;\s]+;base64,[A-Za-z0-9+/=]+", "[IMAGE REDACTED]", text)
    return text if len(text) <= limit else text[:limit//2] + "\n[TRUNCATED]\n" + text[-limit//2:]


def parse_output(text, diagnostics, secret=""):
    if not isinstance(text, str):
        raise ModelError("Model output text has an invalid type", stage="response_schema", diagnostics=diagnostics)
    original = text
    text = text.strip().lstrip("\ufeff")
    # Only unwrap a single complete fence. Never extract or invent an action
    # from prose, truncated JSON, multiple objects, or ambiguous output.
    fenced = re.fullmatch(r"```(?:json)?\s*\n?(.*?)\n?```", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()
    try:
        value = loads(text)
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON object")
    except ValueError as exc:
        reason = "empty output" if not text else preview(str(exc), secret, 300)
        detail = {**diagnostics, "output_chars": len(original),
                  "output_preview": preview(original, secret), "parse_error": reason}
        raise ModelOutputError(f"Model output JSON invalid: {reason}", original, detail) from None
    return value


def read_response(response, *, streaming=False):
    """Consume SSE through its terminal event, never accept partial deltas.

    A relay may keep the connection open after completion. Stop at the explicit
    final event and let decode_envelope/decode_answer validate the actual result.
    """
    headers = getattr(response, "headers", {})
    if not streaming or not headers.get("content-type", "").startswith("text/event-stream"):
        return response.read()
    chunks, data_lines, event_name = [], [], ""
    while True:
        line = response.readline()
        if not line:
            break
        chunks.append(line)
        text = line.decode("utf-8", errors="replace").rstrip("\r\n")
        if text.startswith("event:"):
            event_name = text[6:].strip()
        elif text.startswith("data:"):
            data_lines.append(text[5:].lstrip())
        elif not text:
            if data_lines:
                try:
                    value = loads("\n".join(data_lines))
                    kind = value.get("type", event_name) if isinstance(value, dict) else ""
                except ValueError:
                    kind = ""
                if kind in {"response.completed", "response.incomplete", "response.failed", "error"}:
                    break
            data_lines, event_name = [], ""
    return b"".join(chunks)


def decode_envelope(raw, metadata, secret=""):
    diagnostics = {**metadata, "response_bytes": len(raw), "response_sha256": hashlib.sha256(raw).hexdigest()}
    text = raw.decode("utf-8-sig", errors="replace")
    try:
        if not text.strip():
            raise ValueError("empty HTTP body")
        if metadata.get("content_type", "").startswith("text/event-stream") or text.lstrip().startswith(("event:", "data:")):
            # Some relays return SSE despite stream=false. Use only an explicit
            # completed response, never partial text deltas.
            value = None
            event_name, data_lines = "", []
            for line in [*text.splitlines(), ""]:
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
                elif not line:
                    if data_lines and "\n".join(data_lines) != "[DONE]":
                        event = loads("\n".join(data_lines))
                        kind = event.get("type", event_name)
                        if kind in {"response.completed", "response.incomplete", "response.failed"}:
                            value = event["response"]
                        elif kind == "error":
                            raise ModelError("Model provider returned an SSE error", stage="provider_error", retryable=True,
                                diagnostics={**diagnostics, "provider_error": preview(str(event), secret)})
                    event_name, data_lines = "", []
            if value is None:
                raise ValueError("SSE ended without a final response")
            diagnostics["response_encoding"] = "sse"
        else:
            value = loads(text)
        if not isinstance(value, dict):
            raise ValueError("HTTP response must be a JSON object")
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        reason = preview(str(exc), secret, 300)
        detail = {**diagnostics, "response_preview": preview(text, secret), "parse_error": reason}
        raise ModelError(f"Model HTTP body invalid: {reason}", stage="http_body", retryable=True,
                         diagnostics=detail) from None
    if isinstance(value.get("usage"), dict):
        diagnostics["usage"] = value["usage"]
    if value.get("error"):
        detail = {**diagnostics, "provider_error": preview(str(value["error"]), secret)}
        raise ModelError("Model provider returned an error envelope", stage="provider_error",
                         retryable=True, diagnostics=detail)
    diagnostics.update(response_id=value.get("id"), returned_model=value.get("model"))
    return value, diagnostics


def decode_answer(value, wire_api, diagnostics, secret=""):
    if wire_api == "responses":
        output = value.get("output", [])
        if not isinstance(output, list) or any(not isinstance(item, dict) for item in output):
            raise ModelError("Model response output must be a list of objects", stage="response_schema",
                             diagnostics=diagnostics)
        diagnostics = {**diagnostics, "response_status": value.get("status"),
            "output_types": [item.get("type") for item in output]}
        if value.get("status") != "completed":
            raise ModelError("Model response was incomplete", stage="response_status", retryable=True,
                diagnostics={**diagnostics, "incomplete_details": value.get("incomplete_details")})
        texts, calls, refusals = [], [], []
        try:
            for item in output:
                if item.get("type") == "message":
                    for part in item.get("content", []):
                        if part.get("type") == "output_text":
                            texts.append(part["text"])
                        elif part.get("type") == "refusal":
                            refusals.append(part.get("refusal", ""))
                elif item.get("type") == "function_call":
                    calls.append({"name": item["name"],
                        "arguments": parse_output(item["arguments"], diagnostics, secret)})
            if refusals:
                raise ModelError("Model refused the request", stage="refusal",
                    diagnostics={**diagnostics, "refusal": preview("\n".join(refusals), secret)})
            content = "".join(texts) or value.get("output_text", "")
            if calls:
                if content.strip():
                    raise ModelError("Mixed native calls and final text are ambiguous", stage="response_schema",
                                     diagnostics=diagnostics)
                return {"tool_calls": calls}
        except (KeyError, TypeError, AttributeError) as exc:
            raise ModelError(f"Model response schema invalid: {type(exc).__name__}",
                             stage="response_schema", diagnostics=diagnostics) from None
    else:
        try:
            choice = value["choices"][0]
            if choice.get("finish_reason") not in {None, "stop"}:
                raise ModelError("Model response was incomplete", stage="response_status", retryable=True,
                                 diagnostics={**diagnostics, "finish_reason": choice.get("finish_reason")})
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelError(f"Model response schema invalid: {type(exc).__name__}", stage="response_schema",
                             diagnostics=diagnostics) from None
    return parse_output(content, diagnostics, secret)
