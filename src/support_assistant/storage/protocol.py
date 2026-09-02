"""The `TicketRepository` protocol and the two failures it raises.

Three methods, because the pipeline only ever does three things: record a new ticket, read
one back, write a terminal state (STORAGE.md,
[ADR 0003](../../../docs/adr/0003-sqlite-behind-a-repository-protocol.md)).

A protocol keeps the orchestrator ignorant of SQLite, and is the seam a queue or Postgres
would attach at.
"""

from typing import Protocol

from support_assistant.domain import Ticket
from support_assistant.enums import HandoffReason, TicketStatus
from support_assistant.tracing.models import TraceStep


class TicketAlreadyExists(Exception):
    """`create` was called with an id the repository already holds.

    A ticket id is 128 bits of randomness, so this is a re-`create` bug rather than a
    collision -- and overwriting would lose a trace.
    """


class TicketNotFound(Exception):
    """`finalise` was called for an id the repository does not hold.

    Raised rather than ignored: a silent no-op would leave the orchestrator believing it
    wrote a terminal state that nothing recorded.
    """


class TicketRepository(Protocol):
    """Where tickets and their traces live.

    Implementations take a `Clock` so `finalise` can stamp `updated_at` without reading the
    wall clock (ADR 0008).
    """

    def create(self, ticket: Ticket) -> None:
        """Record a new ticket, status `processing`.

        `ticket.trace` is ignored: during the run the steps live in the `TraceRecorder`.
        Raises `TicketAlreadyExists`.
        """
        ...

    def get(self, ticket_id: str) -> Ticket | None:
        """The ticket, with `trace` populated, or `None` if there is no such id.

        `None` rather than an exception: a 404 is a normal answer (API.md). The trace comes
        back with it, so `GET /tickets/{id}` needs no second call.
        """
        ...

    def finalise(
        self,
        ticket_id: str,
        status: TicketStatus,
        reply: str | None,
        handoff_reason: HandoffReason | None,
        trace: list[TraceStep],
    ) -> None:
        """Write the terminal state and the trace in a single transaction.

        One call, not four setters: that is what makes it impossible to observe a ticket
        that is `replied` with a half-written trace, or `handed_off` carrying a stale
        reply. No `update_status`, no partial write. Raises `TicketNotFound`.
        """
        ...
