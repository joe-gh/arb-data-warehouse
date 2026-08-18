"""Structured metadata-only logging for the in-app agent."""

import json
import logging
from typing import Any


logger = logging.getLogger("arb_logo_admin.agent")

ALLOWED_FIELDS = frozenset({
    "event",
    "request_id",
    "session_id",
    "turn_id",
    "user_login",
    "tool_name",
    "change_set_id",
    "revision",
    "status",
    "duration_ms",
    "input_tokens",
    "output_tokens",
    "tool_call_count",
    "result_bytes",
    "provider_status",
    "count",
    "age_days",
})


def _safe(value: Any) -> str | int | float | bool | None:
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return str(value)[:255]


def log_event(event: str, **fields: Any) -> None:
    """Log only explicitly allowlisted metadata; silently drop all content."""

    payload = {"event": str(event)[:100]}
    for key, value in fields.items():
        if key in ALLOWED_FIELDS and key != "event":
            payload[key] = _safe(value)
    logger.info(json.dumps(payload, sort_keys=True, separators=(",", ":")))
