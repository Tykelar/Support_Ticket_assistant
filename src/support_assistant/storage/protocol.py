"""The `TicketRepository` protocol and the two failures it raises.

Three methods, because the pipeline only ever does three things: record a new ticket,
read one back, and write a terminal state (STORAGE.md,
[ADR 0003](../../../docs/adr/0003-sqlite-behind-a-repository-protocol.md)).

Defining storage as a protocol is what keeps the orchestrator ignorant of SQLite: it is
handed a repository, and the in-memory double implements the same three methods. It is
also the seam a queue or Postgres would later attach at.
"""

from typing import Protocol

from support_assistant.domain import Ticket
from support_assistant.enums import HandoffReason, TicketStatus
from support_assistant.tracing.models import TraceStep


class TicketAlreadyExists(Exception):
    """`create` was called with an id the repository already holds.

    A ticket id is 128 bits of randomness (`domain.new_ticket_id`), so a collision is not
    a real scenario -- this is the guard that turns a re-`create` bug into an immediate
    error rather than a silently overwritten ticket and a lost trace.
    """


class TicketNotFound(Exception):
    """`finalise` was called for an id the repository does not hold.

    Raised rather than ignored: a silent no-op would leave the orchestrator believing it
    had written a terminal state that nothing recorded, which is exactly the
    stuck-in-`processing` outcome ADR 0005's catch-all exists to prevent.
    """


class TicketRepository(Protocol):
    """Where tickets and their traces live.

    Implementations take a `Clock` so `finalise` can stamp `updated_at` without reading
    the wall clock
    ([ADR 0008](../../../docs/adr/0008-injected-clock-with-advancing-test-double.md)).
    """

    def create(self, ticket: Ticket) -> None:
        """Record a new ticket, status `processing`.

        `ticket.trace` is ignored -- a new ticket has no trace, and during the run the
        steps live in the `TraceRecorder` (STORAGE.md). Raises `TicketAlreadyExists`.
        """
        ...

    def get(self, ticket_id: str) -> Ticket | None:
        """The ticket, with `trace` populated, or `None` if there is no such id.

        `None` rather than an exception: "does this ticket exist?" is a question the API
        asks on every `GET`, and a 404 is a normal answer (API.md). The trace comes back
        with it, so `GET /tickets/{id}` answers requirement 5 from one call.
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

        One call, not four setters. That atomicity is what makes it impossible to observe
        a ticket that is `replied` but whose trace is still being written, or `handed_off`
        carrying a stale reply -- which is why the signature looks like this. There is no
        `update_status` and no partial write: a run reaches a terminal state or it does
        not. Raises `TicketNotFound`.
        """
        ...
