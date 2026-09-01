"""Structured logs: one JSON object per line, one line per pipeline step, always carrying
`ticket_id`.

**Logs and traces are not the same record and do not serve the same reader**
(OBSERVABILITY.md). The trace answers "why did the AI say this?" for a support agent and
is served over the API; the log answers "what is the system doing?" for an engineer and
goes to an aggregator. The overlap is deliberate -- the trace is written atomically at the
end of a run, so a process that dies mid-run leaves the log as the only evidence of what
happened.

The log line is produced from a real, recorded trace step via `log_step`, wired as
`TraceRecorder`'s `on_step` hook by the orchestrator. The recorder does not import this
module -- the hook is injected -- so `tracing/` stays below `observability/` in the
dependency graph (ARCHITECTURE.md section 3).

`ts` on a step log is the step's own `ts`, taken from the injected `Clock` -- no wall
clock is read here, so ADR 0008's grep guard stays green and a step log is as reproducible
as the trace step it mirrors. `log_step` always supplies it; the formatter reads the
stdlib clock only if some stray record without a step `ts` ever reaches this handler.

Ticket `subject` and `body` are never logged: customer text of unknown sensitivity,
already stored with the ticket, and copying it into an aggregator widens exposure for no
diagnostic gain.
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
"""Silent until `configure_logging` runs, so importing this package never writes anything
and the test suite stays quiet unless a test opts in."""

_ticket_id: ContextVar[str | None] = ContextVar("support_assistant_ticket_id", default=None)


@contextmanager
def ticket_scope(ticket_id: str) -> Iterator[None]:
    """Bind `ticket_id` for the duration of one run, so every step log inside carries it
    without the call sites having to pass it. Reset on exit, including on an exception, so
    a `ticket_id` never leaks into a later run's logs."""
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

    The default is the step's own `type` and its own model fields (the transform
    `api/schemas.py` serves the trace with). Only the three cases where the log
    deliberately diverges from the trace carry explicit code: a failed tool result and a
    failed grounding check drop their payload, and a handoff is renamed and reduced to its
    reason. Those three -- and a withheld reply -- log at `warning`; everything else at
    `info`. A step type with no case here still logs, generically.
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
    """Attach the JSON handler to the `support_assistant` logger. Idempotent -- called
    once from the app lifespan, and a no-op every time after.

    `propagate` is turned off so the running service does not also emit each line through
    whatever root handler uvicorn installed. Level defaults to `LOG_LEVEL` from the
    environment (PACKAGING.md), then `info`.
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
