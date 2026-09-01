"""Trace step types.

An ordered list of these, persisted with the ticket, is the whole answer to "why did the
AI say this?". Every step carries `seq` and `ts`; the rest of the fields depend on the
type. Shapes and reasoning: TRACEABILITY.md.

`ts` always comes from an injected `Clock` (ADR 0008) -- these models never fill it in
themselves, which is why it has no default.

`Violation` is defined here rather than in `guardrails/` because it is a trace payload
whose JSON shape TRACEABILITY.md owns, and because a guardrail type named by this module
would put `guardrails` underneath `domain`
([ADR 0011](../../../docs/adr/0011-shared-vocabulary-below-the-components.md)).
"""

from datetime import datetime
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from support_assistant.enums import HandoffReason, Intent, LiteralClass, TicketStatus


class Violation(BaseModel):
    """One literal in a reply that no tool result accounts for.

    Recorded in the trace so a reader can see *which* claim was unsourced, not merely
    that the reply was withheld. Serialised with `class` as the key, matching the trace
    JSON in TRACEABILITY.md; the field is renamed here only because `class` is a Python
    keyword.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    literal: str
    """The offending text exactly as it appeared in the reply."""

    literal_class: LiteralClass = Field(alias="class")
    """How the literal was extracted."""

    reason: str
    """Why it failed -- typically absent from both the FactSet and the template's
    declared safe literals."""


class TraceStepBase(BaseModel):
    """Fields every step carries, because ordering and timing are properties of the
    trace as a whole rather than of any one kind of step."""

    model_config = ConfigDict(extra="forbid")

    seq: int = Field(ge=1)
    """1-based and monotonic within one ticket."""

    ts: datetime
    """When the step was recorded, from the injected Clock. Increases with `seq`."""


class IntentClassified(TraceStepBase):
    """Emitted once, before the loop."""

    type: Literal["intent_classified"] = "intent_classified"
    intent: Intent
    matched_keywords: list[str]
    """The evidence for the classification, so an agent can see *why* a ticket was
    categorised as it was -- not just that it was."""


class LLMDecision(TraceStepBase):
    """Emitted once per iteration: what the model chose.

    Recorded separately from `tool_call`, which is what the system did. Collapsing them
    would hide the case where the two diverge -- a rejected tool name, a validation
    failure before execution -- which is precisely the case worth seeing.

    `tool` is set exactly when `decision` is `tool_call`; the validator below enforces it,
    so -- as with `tool_result.ok` and `grounding_check.passed` -- the recorder's caller
    cannot produce an incoherent step.
    """

    type: Literal["llm_decision"] = "llm_decision"
    iteration: int = Field(ge=1)
    decision: Literal["tool_call", "reply", "handoff"]
    tool: str | None = None
    """Present only when the decision was a tool call."""

    @model_validator(mode="after")
    def _tool_named_iff_tool_call(self) -> Self:
        if self.decision == "tool_call" and self.tool is None:
            raise ValueError("a tool_call decision names the tool it called")
        if self.decision != "tool_call" and self.tool is not None:
            raise ValueError("only a tool_call decision carries a tool")
        return self


class ToolCallStep(TraceStepBase):
    """Emitted per tool invocation, before it runs."""

    type: Literal["tool_call"] = "tool_call"
    tool: str
    args: dict[str, Any]


class ToolError(BaseModel):
    """A failure, as the trace records it. The stack trace goes to the structured log
    instead: the trace answers why the AI said this, a traceback answers why the code
    broke."""

    model_config = ConfigDict(extra="forbid")

    type: str
    """The exception class name, e.g. `NoDataAvailable`."""

    message: str


class ToolResultStep(TraceStepBase):
    """Emitted per tool invocation, after it returns or raises.

    Carries a *summary*, never the payload: counts, the distribution over enumerated
    fields, and the identifiers the reply actually used. Enough to explain the reply,
    never more -- the trace is served over the API and retained for audit, so copying
    every field of every record into it multiplies exposure for no gain.
    """

    type: Literal["tool_result"] = "tool_result"
    tool: str
    ok: bool
    summary: dict[str, Any] | None = None
    error: ToolError | None = None


class GroundingCheck(TraceStepBase):
    """Emitted once, if a reply was drafted. Runs unconditionally after rendering."""

    type: Literal["grounding_check"] = "grounding_check"
    passed: bool
    literals_checked: int = Field(ge=0)
    violations: list[Violation] = Field(default_factory=list)
    """The specific offending literals -- the evidence for why a reply was withheld."""


class FinalDecision(TraceStepBase):
    """Emitted exactly once, always last.

    Written only by `finish_replied` and `finish_handoff`, so a terminal ticket without
    one is structurally impossible rather than merely unlikely (PIPELINE.md).
    """

    type: Literal["final_decision"] = "final_decision"
    outcome: TicketStatus
    reason: HandoffReason | None = None
    detail: str | None = None
    """What actually happened -- which user id was missing, which tool raised, which
    literal was ungrounded. A reason without detail explains the category but not the
    incident."""

    @model_validator(mode="after")
    def _outcome_and_reason_agree(self) -> Self:
        if self.outcome is TicketStatus.PROCESSING:
            raise ValueError("final_decision records a terminal outcome, not 'processing'")
        if self.outcome is TicketStatus.HANDED_OFF and self.reason is None:
            raise ValueError("a handoff carries exactly one reason")
        if self.outcome is TicketStatus.REPLIED and self.reason is not None:
            raise ValueError("a reply carries no handoff reason")
        return self


TraceStep = Annotated[
    IntentClassified
    | LLMDecision
    | ToolCallStep
    | ToolResultStep
    | GroundingCheck
    | FinalDecision,
    Field(discriminator="type"),
]
"""The closed set of step types, discriminated by `type`.

Closed on purpose: a step type nothing writes is either a typo or a step someone added
without documenting it, and reading a persisted trace back has to reconstruct the right
class rather than an untyped dict.
"""
