"""The wire contract: what a request must look like, and what a response says.

Separate from `domain.Ticket` on purpose. The domain model is what the system believes;
these are what the outside world may send and is shown. Serving the domain model directly
would publish every future internal field the day it was added.

Separate models, but **not** separate bounds: the three text fields reuse `UserId`,
`SubjectText` and `BodyText` from `domain`, so the edge and the persisted model cannot
disagree about what is too long.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, TypeAdapter, field_serializer

from support_assistant.domain import BodyText, SubjectText, UserId
from support_assistant.enums import HandoffReason, TicketStatus
from support_assistant.tracing.models import TraceStep

_STEP = TypeAdapter(TraceStep)
"""The same adapter `storage/sqlite.py` persists through, used here to serialise. One
definition of a step's JSON shape, whichever direction it is travelling."""


class CreateTicketRequest(BaseModel):
    """A ticket as a customer submits it.

    `extra="forbid"`: a client that misspells `body` is told so, rather than having its
    ticket accepted with an empty one and answered from nothing.
    """

    model_config = ConfigDict(extra="forbid")

    user_id: UserId
    subject: SubjectText
    body: BodyText


class TicketAccepted(BaseModel):
    """The `202` body: the id to poll with, and `processing` -- the honest answer to "is it
    done?", since the pipeline has been scheduled, not run (ADR 0001)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    status: TicketStatus


class TicketView(BaseModel):
    """Everything an agent needs to answer "why did the AI say this?", in one response.

    `subject` and `body` are not echoed back: whoever reads a ticket already has the
    customer's words. What they do not have is what the system did with them.
    """

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: str
    user_id: str
    status: TicketStatus

    reply: str | None
    handoff_reason: HandoffReason | None
    """Mutually exclusive; exactly one is non-null in a terminal state (ADR 0005). Both are
    served as `null` rather than omitted, because API.md promises the keys are there."""

    created_at: datetime
    updated_at: datetime
    """When the ticket arrived, and when the run finished -- so the pair is also how long
    the pipeline took."""

    trace: list[TraceStep]
    """Empty for a `processing` ticket, not partial: steps are written with the terminal
    state in one transaction (STORAGE.md)."""

    @field_serializer("trace")
    def _served_steps(self, trace: list[TraceStep]) -> list[dict[str, Any]]:
        """Steps as TRACEABILITY.md writes them: aliased, and without their empty fields.

        `by_alias` makes `Violation.literal_class` appear as `class`. `exclude_none` drops
        the fields a step does not have, because the trace is read by a human under time
        pressure and a column of nulls on every step is noise.
        """
        return [
            _STEP.dump_python(step, mode="json", by_alias=True, exclude_none=True)
            for step in trace
        ]


class Health(BaseModel):
    """`/health`'s body. A named state rather than a bare `200`, so a reader of the
    response can see which one was reported."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "unavailable"]
    """Exactly the two states API.md documents, so the pair is published in the OpenAPI
    schema and a third cannot be invented without changing the contract."""
