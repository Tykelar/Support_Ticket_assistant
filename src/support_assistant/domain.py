"""The shared vocabulary: tickets, the records the tools return, and the enums that fix
the system's contracts.

These live at the package root rather than inside a component because several components
need them, and putting them in any one of those would force the sideways imports the
dependency direction in ARCHITECTURE.md rules out. Definitions of each term are in
CONTEXT.md; this module is where they become types.
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
"""The enums are defined in `enums.py` so that `tracing.models` can name them without
importing this module back. They are re-exported here because this is where the domain
vocabulary lives, and a reader looking for `Intent` should find it beside `Ticket`.
`HandoffReason` is among them (ADR 0011): it is one of ARCHITECTURE.md section 4's
cross-system contracts, and keeping it in `guardrails/` put this module underneath a
component package."""

TICKET_ID_BYTES = 16
"""128 bits. The ticket id is the only thing protecting a trace (API.md), so it must be
unguessable and non-enumerable -- not a sequence, and not a timestamped UUID."""


def new_ticket_id() -> str:
    """A fresh ticket id: `t_` followed by 32 hex characters."""
    return f"t_{secrets.token_hex(TICKET_ID_BYTES)}"


def format_amount(value: Decimal) -> str:
    """How a money or kWh amount is written in a reply and in `FactSet.allowed_literals()`
    -- always two decimal places.

    Lives here because `guardrails/factset.py` and `llm/templates.py` both write amounts
    and may not import each other (ARCHITECTURE.md section 3). It fixes only the *text
    form*: grounding compares numeric literals as `Decimal`, so `42.1` and `42.10` are the
    same fact whichever way they are written.
    """
    return f"{value:.2f}"


# --------------------------------------------------------------------------------------
# What the tools return
# --------------------------------------------------------------------------------------


class FixtureRecord(BaseModel):
    """Base for the records the tools read out of the fixtures.

    `extra="forbid"` because an unexpected field means a malformed fixture, and a
    malformed fixture is a TOOL_ERROR handoff -- not something to quietly ignore. Note
    that the fixture files carry a `user_id` on every row as the filter key; the loaders
    strip it before validating, so it is deliberately not a field here.
    """

    model_config = ConfigDict(extra="forbid")


class User(FixtureRecord):
    """A person who uses the EV-charging app and can raise a ticket."""

    user_id: str
    name: str
    language: str
    """`pt`, `en`, `fr` in the fixtures. Read and traced, but replies are English only --
    a documented scope cut (ADR 0006), not an oversight."""

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
    """`Decimal`, never `float`. Grounding compares numeric literals as Decimals, so an
    amount that arrived through binary floating point would render as a number the
    fixture does not contain."""

    currency: str
    status: InvoiceStatus
    issued_at: datetime


class ToolResult(BaseModel):
    """What a tool returned for one call, as the rest of the system sees it.

    The tools themselves return domain types -- `get_user(user_id) -> User`. The *registry*
    wraps that into this uniform shape, so result summarisation (TRACEABILITY.md) and
    `FactSet` projection (GUARDRAILS.md) each have one code path: `records` is always a
    list, and `get_user` simply yields one element. Per-tool rules still dispatch on `tool`.

    It lives here rather than in `tools/` because `llm/` and `guardrails/` both consume it
    and ARCHITECTURE.md forbids them importing `tools/`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: str
    """Which tool produced this."""

    records: list[User | ChargingSession | Invoice]
    """Always a list -- `get_user` yields one element, the collection tools yield many.
    Pydantic keeps each element at its concrete subclass (`revalidate_instances` defaults
    to never), so a downstream per-tool rule can still dispatch on the concrete shape."""


# --------------------------------------------------------------------------------------
# What the model classifies
# --------------------------------------------------------------------------------------


class Classification(BaseModel):
    """What `LLMClient.classify_intent` returns: the intent, and the evidence for it.

    A bare `Intent` was the original signature (ADR 0006). It could not supply the
    `matched_keywords` the `intent_classified` trace step carries, and only the classifier
    knows them -- so the evidence travels with the intent rather than being reconstructed
    by the orchestrator from a component whose rules it does not own
    ([ADR 0012](../../docs/adr/0012-classification-carries-its-own-evidence.md)).

    It lives here, beside the decision union, for the same reason those do: `pipeline/`
    and `llm/` both name it, and neither may import the other's package.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    intent: Intent

    matched_keywords: tuple[str, ...] = ()
    """Why the classifier decided this. `FakeLLM` fills it with the keywords it matched;
    a real provider may leave it empty, which is why it defaults rather than being
    required -- a protocol that obliged an implementation to invent evidence would get
    invented evidence."""


# --------------------------------------------------------------------------------------
# What the model decides
# --------------------------------------------------------------------------------------
#
# The return type of `LLMClient.decide_next_step` (llm/LLM.md, ADR 0002): gather more
# data, reply, or give up. These live here rather than in `llm/` because `guardrails/`
# needs `Observation` for `FactSet.from_observations` and ARCHITECTURE.md section 3
# forbids `guardrails/` importing `llm/` -- the same reason `ToolResult` is here.
#
# The discriminator field is named `decision`, and only `ToolCall` carries a `tool`, so
# the orchestrator can unpack a step for the trace with
# `tool = step.tool if isinstance(step, ToolCall) else None` and the `LLMDecision` model's
# own validator stays satisfied (tool named iff `tool_call`).


class ToolCall(BaseModel):
    """Gather more data: run `tool` with `args`, then decide again with the result in
    history."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: Literal["tool_call"] = "tool_call"
    tool: str
    args: dict[str, Any]


class Reply(BaseModel):
    """Finish by rendering `template` from the `FactSet` the pipeline projects from
    history. Names the template only -- rendering and grounding are the orchestrator's
    step, not the model's (PIPELINE.md)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: Literal["reply"] = "reply"
    template: ReplyTemplate


class Handoff(BaseModel):
    """Give up: no reply, a human takes the ticket. First-class so the pipeline handles a
    model that decides to stop as an outcome rather than an error (ADR 0006). `FakeLLM`
    never returns this; a real model can."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: Literal["handoff"] = "handoff"
    reason: HandoffReason


Step = Annotated[ToolCall | Reply | Handoff, Field(discriminator="decision")]
"""One action the model chose (CONTEXT.md's *step*), discriminated by `decision`. A
persisted or transported step reconstructs to the right class rather than an untyped
dict."""


class Observation(BaseModel):
    """A tool call together with what it returned -- one entry of the history the model
    sees before choosing its next step (CONTEXT.md)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    step: ToolCall
    """A `ToolCall`, never `Reply` or `Handoff`: those terminate the loop, so only a tool
    call ever produces an observation (PIPELINE.md appends one solely in that branch)."""

    result: ToolResult


# --------------------------------------------------------------------------------------
# The ticket
# --------------------------------------------------------------------------------------


UserId = Annotated[str, Field(min_length=1)]
"""Who raised the ticket. Non-empty, and deliberately nothing more: whether the user
*exists* is a fact only a tool can establish, and establishing it is a pipeline step that
belongs in the trace -- so an unknown user is a `202` and a later `USER_NOT_FOUND`
handoff, not a rejection at the edge."""

SubjectText = Annotated[str, Field(min_length=1, max_length=200)]
BodyText = Annotated[str, Field(min_length=1, max_length=5000)]
"""The customer's words, bounded. Named types rather than bounds written out at each use,
because `api.schemas.CreateTicketRequest` validates the same three fields at the edge and
`Ticket` validates them again on the way to storage. Two models, one definition -- so
they cannot drift, and no test is needed to check that they have not.

This is the opposite case to `Ticket`'s validator and the table `CHECK` in STORAGE.md,
which are duplicated on purpose: those are two different enforcement technologies that
cannot share a definition. These are two Pydantic models in one process, and can."""


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
        Kept identical to the constraint on purpose -- two enforcement points that
        disagree would be worse than one.
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
                # `is not None` rather than a truthiness check: reply="" must fail too.
                # An empty reply is indistinguishable from one that failed to render,
                # and ADR 0005 says a handoff sends nothing at all.
                if self.reply is not None:
                    raise ValueError("a handed-off ticket has no reply, not even an empty one")
                if self.handoff_reason is None:
                    raise ValueError("a handed-off ticket carries exactly one reason")
        return self
