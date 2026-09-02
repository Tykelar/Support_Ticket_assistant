"""`InMemoryTicketRepository` -- a dict behind the same protocol, for tests.

Not a stub: `tests/test_repository_contract.py` runs one suite against it and against
`SqliteTicketRepository`, because a double that has drifted is worse than no double.

Tickets go in and come out as deep copies. SQLite hands back a freshly parsed row every
time, so a caller mutating what it read cannot affect the store; a dict of live objects
would let it, making the double more permissive than the real thing.
"""

from support_assistant.clock import Clock
from support_assistant.domain import Ticket
from support_assistant.enums import HandoffReason, TicketStatus
from support_assistant.storage.protocol import TicketAlreadyExists, TicketNotFound
from support_assistant.tracing.models import TraceStep


class InMemoryTicketRepository:
    """The test double. No I/O, no schema, same three methods."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._tickets: dict[str, Ticket] = {}

    def create(self, ticket: Ticket) -> None:
        if ticket.id in self._tickets:
            raise TicketAlreadyExists(ticket.id)
        # `trace=[]` rather than the ticket's own: the field is write-ignored (STORAGE.md).
        self._tickets[ticket.id] = ticket.model_copy(deep=True, update={"trace": []})

    def get(self, ticket_id: str) -> Ticket | None:
        stored = self._tickets.get(ticket_id)
        return None if stored is None else stored.model_copy(deep=True)

    def finalise(
        self,
        ticket_id: str,
        status: TicketStatus,
        reply: str | None,
        handoff_reason: HandoffReason | None,
        trace: list[TraceStep],
    ) -> None:
        stored = self._tickets.get(ticket_id)
        if stored is None:
            raise TicketNotFound(ticket_id)
        # Built through the model rather than mutated field by field, so the
        # status/reply/reason validator runs on the finished state.
        self._tickets[ticket_id] = Ticket(
            **stored.model_dump()
            | {
                "status": status,
                "reply": reply,
                "handoff_reason": handoff_reason,
                "updated_at": self._clock.now(),
                "trace": sorted(trace, key=lambda step: step.seq),
            }
        )
