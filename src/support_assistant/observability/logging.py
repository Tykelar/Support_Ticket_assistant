"""Structured logs: one JSON object per line, one line per pipeline step, always carrying
`ticket_id`.

**Logs and traces are different records for different readers** (OBSERVABILITY.md). The
trace answers "why did the AI say this?" for a support agent and is served over the API;
the log answers "what is the system doing?" for an engineer. The overlap is deliberate --
the trace is written atomically at the end of a run, so a process that dies mid-run leaves
the log as the only evidence.

`log_step` is wired as `TraceRecorder`'s `on_step` hook by the orchestrator. The hook is
injected rather than imported, so `tracing/` stays below `observability/`
(ARCHITECTURE.md section 3).

A step log's `ts` is the step's own, from the injected `Clock` -- no wall clock is read
here (ADR 0008). Ticket `subject` and `body` are never logged: customer text is already
stored with the ticket, and copying it to an aggregator widens exposure for no gain.
"""

import json
import logging
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from support_assistant.enums import TicketStatus
from support_assistant.tracing.models import (
    FinalDecision,
    GroundingCheck,
    ToolResultStep,
    TraceStep,
)

_LOGGER = logging.getLogger("support_assistant")
_LOGGER.addHandler(logging.NullHandler())
"""Silent until `configure_logging` runs, so importing the package writes nothing."""

_ticket_id: ContextVar[str | None] = ContextVar("support_assistant_ticket_id", default=None)


@contextmanager
def ticket_scope(ticket_id: str) -> Iterator[None]:
    """Bind `ticket_id` for one run, so every step log inside carries it without the call
    sites passing it. Reset on exit, including on an exception."""
    token = _ticket_id.set(ticket_id)
    try:
        yield
    finally:
        _ticket_id.reset(token)


class JsonLogFormatter(logging.Formatter):
    """One JSON object per line: `ts`, `level`, `event`, `ticket_id` when bound, then the
    event's own fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": getattr(record, "event_ts", None) or self.formatTime(record),
            "level": record.levelname.lower(),
            "event": getattr(record, "event", None) or record.getMessage(),
        }
        ticket_id = getattr(record, "ticket_id", None)
        if ticket_id is not None:
            payload["ticket_id"] = ticket_id
        for key, value in (getattr(record, "fields", None) or {}).items():
            payload.setdefault(key, value)
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def log_step(step: TraceStep) -> None:
    """Emit one structured line for a recorded trace step. Wired as `TraceRecorder`'s
    `on_step` hook by the orchestrator."""
    event, level, fields = _describe(step)
    _LOGGER.log(
        level,
        event,
        extra={
            "event": event,
            "event_ts": step.ts.isoformat().replace("+00:00", "Z"),
            "ticket_id": _ticket_id.get(),
            "fields": fields,
        },
    )


def _describe(step: TraceStep) -> tuple[str, int, dict[str, object]]:
    """A trace step as (event name, level, fields).

    The default is the step's own `type` and model fields. Only the three cases where the
    log diverges from the trace carry code: a failed tool result and a failed grounding
    check drop their payload, and a handoff is renamed and reduced to its reason. Those
    log at `warning`, everything else at `info`, and an uncased step type still logs.
    """
    fields = step.model_dump(mode="json", exclude={"seq", "ts", "type"}, exclude_none=True)
    if isinstance(step, ToolResultStep):
        summary: dict[str, object] = {"tool": step.tool, "ok": step.ok}
        if step.ok:
            return "tool_result", logging.INFO, summary
        return "tool_result", logging.WARNING, {
            **summary,
            "error": step.error.type if step.error else None,
        }
    if isinstance(step, GroundingCheck):
        fields["violations"] = len(step.violations)
        return "grounding_check", logging.INFO if step.passed else logging.WARNING, fields
    if isinstance(step, FinalDecision) and step.outcome is TicketStatus.HANDED_OFF:
        return "handoff", logging.WARNING, {
            "reason": step.reason.value if step.reason else None,
            "detail": step.detail,
        }
    return step.type, logging.INFO, fields


_configured = False


def configure_logging(level: str | int | None = None) -> None:
    """Attach the JSON handler to the `support_assistant` logger. Idempotent.

    `propagate` is off so the service does not also emit each line through uvicorn's root
    handler. Level comes from `LOG_LEVEL` (PACKAGING.md), then `info`.
    """
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonLogFormatter())
    _LOGGER.addHandler(handler)
    _LOGGER.setLevel(_resolve_level(level))
    _LOGGER.propagate = False
    _configured = True


def _resolve_level(level: str | int | None) -> int:
    if isinstance(level, int):
        return level
    name = (level or os.environ.get("LOG_LEVEL") or "info").upper()
    return logging.getLevelNamesMapping().get(name, logging.INFO)
