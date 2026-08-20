from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REDACTED = "***REDACTED***"
_SENSITIVE_KEY_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "private_key",
    "authorization",
    "cookie",
    "session",
    "credential",
)


def _sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def redact(value: Any, *, secret_values: Iterable[str] = ()) -> Any:
    secrets = tuple(secret for secret in secret_values if secret)

    def _redact(current: Any, key: str | None = None) -> Any:
        if key is not None and _sensitive_key(key):
            return REDACTED

        if isinstance(current, dict):
            return {
                str(child_key): _redact(child_value, str(child_key))
                for child_key, child_value in current.items()
            }
        if isinstance(current, (list, tuple)):
            return [_redact(item) for item in current]
        if isinstance(current, str):
            result = current
            for secret in secrets:
                result = result.replace(secret, REDACTED)
            return result
        return current

    return _redact(value)


class JsonlLogger:
    def __init__(
        self,
        path: str | Path,
        *,
        secret_values: Iterable[str] = (),
    ) -> None:
        self.path = Path(path)
        self.secret_values = tuple(value for value in secret_values if value)

    def log(self, level: str, event: str, **fields: Any) -> None:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": level.upper(),
            "event": event,
            **fields,
        }
        safe = redact(record, secret_values=self.secret_values)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(
                json.dumps(
                    safe,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )
