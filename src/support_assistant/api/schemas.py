"""The wire contract: what a request must look like, and what a response says.

These are deliberately separate from `domain.Ticket`. The domain model is what the system
believes about a ticket; these are what the outside world may send and is shown. Serving
the domain model directly would make every field an accidental part of the public API --
`subject` and `body` echoed back for no reason, and any future internal field published
the day it is added.

The length bounds *are* repeated from `Ticket`, and that duplication is checked:
`test_api.py::test_the_request_schema_repeats_the_domain_limits` compares the two field
by field, so the wire contract and the domain model cannot drift apart silently. Same
argument as `Ticket`'s validator and the table `CHECK` in STORAGE.md -- two enforcement
points, kept identical on purpose.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_serializer

from support_assistant.domain import Ticket
from support_assistant.enums import HandoffReason, TicketStatus
from support_assistant.tracing.models import TraceStep

_STEP = TypeAdapter(TraceStep)
"""The same adapter `storage/sqlite.py` persists through, used here to serialise. One
definition of a step's JSON shape, whichever direction it is travelling."""


class CreateTicketRequest(BaseModel):
    """A ticket as a customer submits it.

    `extra="forbid"` like `Ticket`: a client that misspells `body` is told so, rather than
    having its ticket accepted with an empty one and answered from nothing.
    """

    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1)
    """Not checked against the fixtures. Whether a user exists is a fact only a tool can
    establish, and establishing it is a pipeline step that belongs in the trace (API.md)
    -- so an unknown user is a `202` and a later `USER_NOT_FOUND` handoff, not a `400`."""

    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=5000)


class TicketAccepted(BaseModel):
    """The `202` body: the id, and the state the ticket is in when the response leaves.

    Two fields on purpose. The client needs the id to poll with, and `processing` is the
    honest answer to "is it done?" -- the pipeline has been scheduled, not run (ADR 0001).
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    status: TicketStatus


class TicketView(BaseModel):
    """Everything an agent needs to answer "why did the AI say this?", in one response.

    `subject` and `body` are not echoed back: whoever is reading a ticket already has the
    customer's words. What they do not have is what the system did with them.
    """

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: str
    user_id: str
    status: TicketStatus

    reply: str | None
    handoff_reason: HandoffReason | None
    """Mutually exclusive, and exactly one is non-null in a terminal state (ADR 0005).
    Both are served as `null` rather than omitted -- API.md's field contract promises the
    keys are always there, and `null` is the answer to "was there a reply?"."""

    created_at: datetime
    updated_at: datetime
    """When the ticket arrived, and when the run finished. Equal until `finalise` moves
    the second one, so the pair is also how long the pipeline took."""

    trace: list[TraceStep]
    """Empty for a `processing` ticket, not partial: steps are written with the terminal
    state in one transaction (STORAGE.md)."""

    @classmethod
    def of(cls, ticket: Ticket) -> "TicketView":
        return cls.model_validate(ticket)

    @field_serializer("trace")
    def _served_steps(self, trace: list[TraceStep]) -> list[dict[str, Any]]:
        """Steps as TRACEABILITY.md writes them: aliased, and without their empty fields.

        `by_alias` is what makes `Violation.literal_class` appear as `class`, the key the
        documented JSON uses -- the field is renamed in Python only because `class` is a
        keyword. `exclude_none` drops the fields a given step does not have, because the
        trace is read by a human under time pressure and a column of nulls on every step
        is noise. The top-level fields above are the opposite case and keep theirs.
        """
        return [
            _STEP.dump_python(step, mode="json", by_alias=True, exclude_none=True)
            for step in trace
        ]


class Health(BaseModel):
    """`/health`'s body. A string rather than a bare `200` so a reader of the response --
    or a log line -- can see which state was reported."""

    model_config = ConfigDict(extra="forbid")

    status: str
