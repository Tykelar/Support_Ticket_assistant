"""The `TraceRecorder` -- the one thing that produces trace steps.

It assigns `seq` (1-based, monotonic across every step type) and stamps `ts` from an
injected `Clock`, never the wall clock (ADR 0008). Steps accumulate in memory during the
run; the orchestrator persists them with the terminal state in one transaction
(TRACEABILITY.md, STORAGE.md).

The recorder is injected, not global, so a test asserts on `recorder.steps` directly.
"""

from datetime import datetime
from typing import Any, Literal

from support_assistant.clock import Clock
from support_assistant.enums import Intent, TicketStatus
from support_assistant.guardrails.grounding import Violation
from support_assistant.guardrails.handoff import HandoffReason
from support_assistant.tracing.models import (
    FinalDecision,
    GroundingCheck,
    IntentClassified,
    LLMDecision,
    ToolCallStep,
    ToolError,
    ToolResultStep,
    TraceStep,
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
    method runs `_stamp()` first, so `seq` has no gaps and `ts` increases with `seq`
    under any real clock -- the property a contract test in `test_tracing.py` pins.
    """

    def __init__(self, ticket_id: str, clock: Clock) -> None:
        self._ticket_id = ticket_id
        self._clock = clock
        self._steps: list[TraceStep] = []
        self._seq = 0

    @property
    def steps(self) -> list[TraceStep]:
        """The steps recorded so far, oldest first. A copy -- callers persist or assert on
        it, they do not mutate the recorder's list."""
        return list(self._steps)

    def _stamp(self) -> tuple[int, datetime]:
        self._seq += 1
        return self._seq, self._clock.now()

    def intent_classified(self, intent: Intent, matched_keywords: list[str]) -> None:
        seq, ts = self._stamp()
        self._steps.append(
            IntentClassified(
                seq=seq, ts=ts, intent=intent, matched_keywords=matched_keywords
            )
        )

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
        orchestrator adapts its own `step` at the call site.
        """
        seq, ts = self._stamp()
        self._steps.append(
            LLMDecision(seq=seq, ts=ts, iteration=iteration, decision=decision, tool=tool)
        )

    def tool_call(self, tool: str, args: dict[str, Any]) -> None:
        seq, ts = self._stamp()
        self._steps.append(ToolCallStep(seq=seq, ts=ts, tool=tool, args=args))

    def tool_result(
        self,
        tool: str,
        summary: dict[str, Any] | None = None,
        error: ToolError | None = None,
    ) -> None:
        """After a tool returned or raised. `ok` follows from whether an `error` is
        present -- the caller never states it, so the two cannot disagree."""
        seq, ts = self._stamp()
        self._steps.append(
            ToolResultStep(
                seq=seq, ts=ts, tool=tool, ok=error is None, summary=summary, error=error
            )
        )

    def grounding_check(
        self, literals_checked: int, violations: list[Violation]
    ) -> None:
        """After the reply was rendered. `passed` follows from whether `violations` is
        empty; `literals_checked` is recorded separately because `passed: true,
        literals_checked: 0` is a check that never really inspected anything."""
        seq, ts = self._stamp()
        self._steps.append(
            GroundingCheck(
                seq=seq,
                ts=ts,
                passed=not violations,
                literals_checked=literals_checked,
                violations=violations,
            )
        )

    def final_decision(
        self,
        outcome: TicketStatus,
        reason: HandoffReason | None = None,
        detail: str | None = None,
    ) -> None:
        """Exactly once, always last. The `FinalDecision` model validator enforces the
        outcome/reason invariant, so an incoherent terminal state cannot be recorded."""
        seq, ts = self._stamp()
        self._steps.append(
            FinalDecision(
                seq=seq, ts=ts, outcome=outcome, reason=reason, detail=detail
            )
        )
