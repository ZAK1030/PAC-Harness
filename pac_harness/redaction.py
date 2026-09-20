import re

from .codex_errors import _redact as redact_error


def redact(value):
    if isinstance(value, dict):
        return {str(key): "[REDACTED]" if re.search(r"password|secret|credential|authorization|api.?key|access.?token|refresh.?token", str(key), re.I)
                else redact(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return "\n".join(redact_error(line) for line in value.split("\n"))
    return value
