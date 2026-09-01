"""The `TraceRecorder` -- the one thing that produces trace steps.

It assigns `seq` (1-based, monotonic across every step type) and stamps `ts` from an
injected `Clock`, never the wall clock (ADR 0008). Steps accumulate in memory during the
run; the orchestrator persists them with the terminal state in one transaction
(TRACEABILITY.md, STORAGE.md).

The recorder is injected, not global, so a test asserts on `recorder.steps` directly.
"""

from collections.abc import Callable
from typing import Any, Literal

from support_assistant.clock import Clock
from support_assistant.enums import HandoffReason, Intent, TicketStatus
from support_assistant.tracing.models import (
    FinalDecision,
    GroundingCheck,
    IntentClassified,
    LLMDecision,
    ToolCallStep,
    ToolError,
    ToolResultStep,
    TraceStep,
    Violation,
)


def as_tool_error(exc: Exception) -> ToolError:
    """Map a caught exception to the `ToolError` a failed `tool_result` step carries.

    ADR 0010: the loop's catch is deliberately broad, so this records *any* exception
    that reached the call site -- `NoDataAvailable` and `UserNotFound` included, not only
    `ToolExecutionError`. The stack trace goes to the structured log, not here.
    """
    return ToolError(type=type(exc).__name__, message=str(exc))


class TraceRecorder:
    """Accumulates the ordered, timestamped steps that answer "why did the AI say this?".

    One method per step type, matching the calls in PIPELINE.md's `run_pipeline`. Every
    method goes through `_record()`, so `seq` has no gaps and `ts` increases with `seq`
    under any real clock -- the property a contract test in `test_tracing.py` pins.
    """

    def __init__(
        self, clock: Clock, *, on_step: Callable[[TraceStep], None] | None = None
    ) -> None:
        """`on_step`, if given, is called with each step as it is recorded -- the seam the
        orchestrator uses to emit a structured log line per step
        ([OBSERVABILITY.md](../observability/OBSERVABILITY.md)). It is injected rather than
        imported so `tracing/` stays ignorant of the logger: the trace and the log are
        different records for different readers, and only their production point is shared.
        """
        self._clock = clock
        self._on_step = on_step
        self._steps: list[TraceStep] = []
        self._seq = 0

    @property
    def steps(self) -> list[TraceStep]:
        """The steps recorded so far, oldest first. A copy -- callers persist or assert on
        it, they do not mutate the recorder's list."""
        return list(self._steps)

    def _record(self, cls: type[TraceStep], **fields: Any) -> None:
        """Assign the next `seq`, stamp `ts` from the injected clock, append, then notify
        `on_step`. The one place a step is built -- so no method can forget the ordering
        fields, the clock, or the hook."""
        self._seq += 1
        step = cls(seq=self._seq, ts=self._clock.now(), **fields)
        self._steps.append(step)
        if self._on_step is not None:
            self._on_step(step)

    def intent_classified(self, intent: Intent, matched_keywords: list[str]) -> None:
        self._record(IntentClassified, intent=intent, matched_keywords=matched_keywords)

    def llm_decision(
        self,
        iteration: int,
        decision: Literal["tool_call", "reply", "handoff"],
        tool: str | None = None,
    ) -> None:
        """What the model chose this iteration.

        Takes the decision and (for a tool call) the tool name rather than an LLM step
        object: the `ToolCall` / `Reply` / `Handoff` types arrive with `llm/` in a later
        phase, and nothing in `tracing/` may depend on `llm/` (ARCHITECTURE.md). The
        orchestrator adapts its own `step` at the call site. `LLMDecision`'s validator
        rejects a `tool_call` with no tool, or a non-tool-call that names one.
        """
        self._record(LLMDecision, iteration=iteration, decision=decision, tool=tool)

    def tool_call(self, tool: str, args: dict[str, Any]) -> None:
        self._record(ToolCallStep, tool=tool, args=args)

    def tool_result(
        self,
        tool: str,
        summary: dict[str, Any] | None = None,
        error: ToolError | None = None,
    ) -> None:
        """After a tool returned or raised. `ok` follows from whether an `error` is
        present -- the caller never states it, so the two cannot disagree."""
        self._record(
            ToolResultStep, tool=tool, ok=error is None, summary=summary, error=error
        )

    def grounding_check(self, literals_checked: int, violations: list[Violation]) -> None:
        """After the reply was rendered. `passed` follows from whether `violations` is
        empty; `literals_checked` is recorded separately because `passed: true,
        literals_checked: 0` is a check that never really inspected anything."""
        self._record(
            GroundingCheck,
            passed=not violations,
            literals_checked=literals_checked,
            violations=violations,
        )

    def final_decision(
        self,
        outcome: TicketStatus,
        reason: HandoffReason | None = None,
        detail: str | None = None,
    ) -> None:
        """Exactly once, always last. The `FinalDecision` model validator enforces the
        outcome/reason invariant, so an incoherent terminal state cannot be recorded."""
        self._record(FinalDecision, outcome=outcome, reason=reason, detail=detail)
