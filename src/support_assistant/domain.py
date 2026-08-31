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
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from support_assistant.enums import Intent, InvoiceStatus, SessionStatus, TicketStatus
from support_assistant.guardrails.handoff import HandoffReason
from support_assistant.tracing.models import TraceStep

__all__ = [
    "ChargingSession",
    "FixtureRecord",
    "Intent",
    "Invoice",
    "InvoiceStatus",
    "SessionStatus",
    "Ticket",
    "TicketStatus",
    "ToolResult",
    "User",
    "new_ticket_id",
]
"""The enums are defined in `enums.py` so that `tracing.models` can name them without
importing this module back. They are re-exported here because this is where the domain
vocabulary lives, and a reader looking for `Intent` should find it beside `Ticket`."""

TICKET_ID_BYTES = 16
"""128 bits. The ticket id is the only thing protecting a trace (API.md), so it must be
unguessable and non-enumerable -- not a sequence, and not a timestamped UUID."""


def new_ticket_id() -> str:
    """A fresh ticket id: `t_` followed by 32 hex characters."""
    return f"t_{secrets.token_hex(TICKET_ID_BYTES)}"


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
# The ticket
# --------------------------------------------------------------------------------------


class Ticket(BaseModel):
    """A support request, and the record of what the service did about it."""

    model_config = ConfigDict(extra="forbid")

    id: str
    user_id: str = Field(min_length=1)
    """Not validated against the fixtures. Whether a user exists is a fact only a tool
    can establish, and establishing it is a pipeline step that belongs in the trace."""

    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=5000)

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
