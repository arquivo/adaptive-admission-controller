"""Structured JSON logging (`docs/implementation_plan.md` §6.2, FR-071).

`app/scoring.py` and `app/capacity.py` already call `logger.info(event,
extra={...})` for score decomposition and adaptive limit changes — those
calls were never rendered as JSON before this module existed, since nothing
configured a JSON formatter on the root logger. `configure_logging()` (called
once from `app.main.create_app()`) is what actually turns every one of those
existing calls, plus the new per-request admission/rejection events added in
this phase, into the one-JSON-line-per-event format FR-071 requires.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.interfaces import RequestContext

logger = logging.getLogger(__name__)

# Attributes every `logging.LogRecord` carries regardless of `extra` —
# anything else on the record came from a caller's `extra={...}` dict and
# belongs in the JSON payload.
_STANDARD_RECORD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_ATTRS:
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)


def _score_payload(ctx: RequestContext) -> dict[str, Any]:
    breakdown = ctx.score_breakdown
    if breakdown is None:
        return {"final": ctx.score}
    return {
        "base": breakdown.base,
        "penalty_ip": breakdown.penalty_ip,
        "penalty_net24": breakdown.penalty_net24,
        "penalty_net6": breakdown.penalty_net6,
        "penalty_asn": breakdown.penalty_asn,
        "penalty_country": breakdown.penalty_country,
        "penalty_user": breakdown.penalty_user,
        "final": breakdown.final,
    }


def log_admission_event(
    event: str,
    ctx: RequestContext,
    *,
    reason: str | None = None,
    queue_wait_ms: float | None = None,
    backend_latency_ms: float | None = None,
    status_code: int | None = None,
) -> None:
    """One JSON log line per admission decision (§6.2). `event` is
    `"admitted"` or `"rejected"` — `reason` mirrors the `reason` label on
    `admission_rejected_total` and is only set for the latter.
    """
    payload: dict[str, Any] = {
        "backend": ctx.backend,
        "user_class": ctx.user_class,
        "source_ip": ctx.source_ip,
        "asn": ctx.asn,
        "country": ctx.country,
        "country_exempt": ctx.score_breakdown.is_exempt if ctx.score_breakdown else None,
        "score": _score_payload(ctx),
        "cost": ctx.cost,
    }
    if reason is not None:
        payload["reason"] = reason
    if queue_wait_ms is not None:
        payload["queue_wait_ms"] = queue_wait_ms
    if backend_latency_ms is not None:
        payload["backend_latency_ms"] = backend_latency_ms
    if status_code is not None:
        payload["status_code"] = status_code
    logger.info(event, extra=payload)
