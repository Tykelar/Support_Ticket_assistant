"""The shared vocabulary: tickets, the records the tools return, and the decision types.

These live at the package root because several components need them, and putting them in
any one component would force sideways imports (ARCHITECTURE.md). CONTEXT.md defines the
terms; this is where they become types.
"""

import secrets
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from support_assistant.enums import (
    HandoffReason,
    Intent,
    InvoiceStatus,
    ReplyTemplate,
    SessionStatus,
    TicketStatus,
)
from support_assistant.tracing.models import TraceStep

__all__ = [
    "BodyText",
    "ChargingSession",
    "Classification",
    "FixtureRecord",
    "Handoff",
    "HandoffReason",
    "Intent",
    "Invoice",
    "InvoiceStatus",
    "Observation",
    "Reply",
    "ReplyTemplate",
    "SessionStatus",
    "Step",
    "SubjectText",
    "Ticket",
    "TicketStatus",
    "ToolCall",
    "ToolResult",
    "User",
    "UserId",
    "format_amount",
    "new_ticket_id",
]
"""The enums live in `enums.py` so `tracing.models` can name them without importing this
module back (ADR 0011). They are re-exported here because a reader looking for `Intent`
should find it beside `Ticket`."""

TICKET_ID_BYTES = 16
"""128 bits. The ticket id is the only thing protecting a trace (API.md), so it must be
unguessable -- not a sequence, and not a timestamped UUID."""


def new_ticket_id() -> str:
    """A fresh ticket id: `t_` followed by 32 hex characters."""
    return f"t_{secrets.token_hex(TICKET_ID_BYTES)}"


def format_amount(value: Decimal) -> str:
    """How a money or kWh amount is written in a reply -- always two decimal places.

    Lives here because `guardrails/` and `llm/` both write amounts and may not import each
    other. Text form only: grounding compares numbers as `Decimal`, so `42.1` and `42.10`
    are the same fact.
    """
    return f"{value:.2f}"


# --------------------------------------------------------------------------------------
# What the tools return
# --------------------------------------------------------------------------------------


class FixtureRecord(BaseModel):
    """Base for the records the tools read out of the fixtures.

    `extra="forbid"`: an unexpected field means a malformed fixture, which is a TOOL_ERROR
    handoff rather than something to ignore. Fixture rows carry a `user_id` filter key
    that the loaders strip before validating, so it is not a field here.
    """

    model_config = ConfigDict(extra="forbid")


class User(FixtureRecord):
    """A person who uses the EV-charging app and can raise a ticket."""

    user_id: str
    name: str
    language: str
    """Read and traced, but replies are English only -- a scope cut (ADR 0006)."""

    plan: str


class ChargingSession(FixtureRecord):
    """One use of a charging station by a user."""

    session_id: str
    station: str
    kwh: Decimal
    cost: Decimal
    status: SessionStatus
    started_at: datetime


class Invoice(FixtureRecord):
    """A request for payment issued to a user."""

    invoice_id: str
    amount: Decimal
    """`Decimal`, never `float`: an amount through binary floating point would render as a
    number the fixture does not contain, and grounding would withhold the reply."""

    currency: str
    status: InvoiceStatus
    issued_at: datetime


class ToolResult(BaseModel):
    """What a tool returned for one call, as the rest of the system sees it.

    Tools return domain types; the registry wraps them into this uniform shape, so
    summarisation and `FactSet` projection each have one code path. Lives here rather than
    in `tools/` because `llm/` and `guardrails/` consume it and may not import `tools/`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: str
    """Which tool produced this."""

    records: list[User | ChargingSession | Invoice]
    """Always a list -- `get_user` yields one element, the collection tools yield many.
    Elements keep their concrete subclass, so downstream rules can dispatch on shape."""


# --------------------------------------------------------------------------------------
# What the model classifies
# --------------------------------------------------------------------------------------


class Classification(BaseModel):
    """What `LLMClient.classify_intent` returns: the intent, and the evidence for it.

    The evidence travels with the intent because only the classifier knows it
    ([ADR 0012](../../docs/adr/0012-classification-carries-its-own-evidence.md)).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    intent: Intent

    matched_keywords: tuple[str, ...] = ()
    """Why the classifier decided this. Defaulted rather than required: a real provider has
    no such evidence, and obliging it to supply some would get invented evidence."""


# --------------------------------------------------------------------------------------
# What the model decides
# --------------------------------------------------------------------------------------
#
# The return type of `LLMClient.decide_next_step` (LLM.md, ADR 0002): gather more data,
# reply, or give up. Here rather than in `llm/` because `guardrails/` needs `Observation`
# and may not import `llm/` -- the same reason `ToolResult` is here.
#
# Only `ToolCall` carries a `tool`, which is what lets the orchestrator unpack a step for
# the trace and keep `LLMDecision`'s validator satisfied.


class ToolCall(BaseModel):
    """Gather more data: run `tool` with `args`, then decide again with the result in
    history."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: Literal["tool_call"] = "tool_call"
    tool: str
    args: dict[str, Any]


class Reply(BaseModel):
    """Finish by rendering `template` from the `FactSet`. Names the template only --
    rendering and grounding are the orchestrator's step, not the model's (PIPELINE.md)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: Literal["reply"] = "reply"
    template: ReplyTemplate


class Handoff(BaseModel):
    """Give up: no reply, a human takes the ticket. First-class so a model that stops is an
    outcome rather than an error (ADR 0006). `FakeLLM` never returns this; a real one can."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: Literal["handoff"] = "handoff"
    reason: HandoffReason


Step = Annotated[ToolCall | Reply | Handoff, Field(discriminator="decision")]
"""One action the model chose, discriminated by `decision`, so a persisted step
reconstructs to the right class rather than an untyped dict."""


class Observation(BaseModel):
    """A tool call together with what it returned -- one entry of the history the model
    sees before choosing its next step (CONTEXT.md)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    step: ToolCall
    """Never `Reply` or `Handoff`: those terminate the loop, so only a tool call ever
    produces an observation."""

    result: ToolResult


# --------------------------------------------------------------------------------------
# The ticket
# --------------------------------------------------------------------------------------


UserId = Annotated[str, Field(min_length=1)]
"""Who raised the ticket. Non-empty and nothing more: whether the user exists is a fact
only a tool can establish, and that belongs in the trace -- so an unknown user is a `202`
and a later `USER_NOT_FOUND` handoff, not a rejection at the edge."""

SubjectText = Annotated[str, Field(min_length=1, max_length=200)]
BodyText = Annotated[str, Field(min_length=1, max_length=5000)]
"""The customer's words, bounded. Named types rather than bounds repeated at each use:
`api.schemas.CreateTicketRequest` validates these fields at the edge and `Ticket`
validates them again before storage, and one definition cannot drift from itself."""


class Ticket(BaseModel):
    """A support request, and the record of what the service did about it."""

    model_config = ConfigDict(extra="forbid")

    id: str
    user_id: UserId
    subject: SubjectText
    body: BodyText

    status: TicketStatus = TicketStatus.PROCESSING
    reply: str | None = None
    handoff_reason: HandoffReason | None = None

    created_at: datetime
    updated_at: datetime

    trace: list[TraceStep] = Field(default_factory=list)
    """Everything the pipeline did, in order. Grows during the run and is persisted with
    the terminal state in one transaction (STORAGE.md)."""

    @model_validator(mode="after")
    def _status_reply_and_reason_agree(self) -> Self:
        """The application half of the three-way CHECK in STORAGE.md's schema.

        The database is the backstop; this stops the bad write being attempted at all.
        Kept identical to the constraint: two enforcement points that disagree would be
        worse than one.
        """
        match self.status:
            case TicketStatus.PROCESSING:
                if self.reply is not None or self.handoff_reason is not None:
                    raise ValueError("a processing ticket has neither a reply nor a reason")
            case TicketStatus.REPLIED:
                if not self.reply:
                    raise ValueError("a replied ticket has a reply")
                if self.handoff_reason is not None:
                    raise ValueError("a replied ticket has no handoff reason")
            case TicketStatus.HANDED_OFF:
                # `is not None`, not truthiness: reply="" must fail too. An empty reply is
                # indistinguishable from one that failed to render (ADR 0005).
                if self.reply is not None:
                    raise ValueError("a handed-off ticket has no reply, not even an empty one")
                if self.handoff_reason is None:
                    raise ValueError("a handed-off ticket carries exactly one reason")
        return self
