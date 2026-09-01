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
as the trace step it mirrors. The formatter falls back to its own clock only for the
handful of non-step logs (application startup).

Ticket `subject` and `body` are never logged: customer text of unknown sensitivity,
already stored with the ticket, and copying it into an aggregator widens exposure for no
diagnostic gain.
"""

import json
import logging
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from support_assistant.enums import TicketStatus
from support_assistant.tracing.models import (
    FinalDecision,
    GroundingCheck,
    IntentClassified,
    LLMDecision,
    ToolCallStep,
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

    converter = time.gmtime

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": getattr(record, "event_ts", None) or self._fallback_ts(record),
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

    def _fallback_ts(self, record: logging.LogRecord) -> str:
        return f"{self.formatTime(record, '%Y-%m-%dT%H:%M:%S')}.{int(record.msecs):03d}Z"


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
    """A trace step as (event name, level, fields). A handoff and a withheld reply are
    `warning`; everything else is `info`."""
    if isinstance(step, IntentClassified):
        return "intent_classified", logging.INFO, {
            "intent": step.intent.value,
            "matched_keywords": list(step.matched_keywords),
        }
    if isinstance(step, LLMDecision):
        fields: dict[str, object] = {"iteration": step.iteration, "decision": step.decision}
        if step.tool is not None:
            fields["tool"] = step.tool
        return "llm_decision", logging.INFO, fields
    if isinstance(step, ToolCallStep):
        return "tool_call", logging.INFO, {"tool": step.tool, "args": step.args}
    if isinstance(step, ToolResultStep):
        if step.ok:
            return "tool_result", logging.INFO, {"tool": step.tool, "ok": True}
        return "tool_result", logging.WARNING, {
            "tool": step.tool,
            "ok": False,
            "error": step.error.type if step.error else None,
        }
    if isinstance(step, GroundingCheck):
        return "grounding_check", logging.INFO if step.passed else logging.WARNING, {
            "passed": step.passed,
            "literals_checked": step.literals_checked,
            "violations": len(step.violations),
        }
    if isinstance(step, FinalDecision):
        if step.outcome is TicketStatus.HANDED_OFF:
            return "handoff", logging.WARNING, {
                "reason": step.reason.value if step.reason else None,
                "detail": step.detail,
            }
        return "final_decision", logging.INFO, {"outcome": step.outcome.value}
    # The step union is closed (tracing/models.py); a new member is described here or not
    # logged.
    raise ValueError(f"no log mapping for trace step {type(step).__name__}")


_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}

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
    name = (level or os.environ.get("LOG_LEVEL") or "info").lower()
    return _LEVELS.get(name, logging.INFO)
