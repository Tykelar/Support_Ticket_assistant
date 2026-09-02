"""One contract suite, run against **both** `TicketRepository` implementations.

Reserved by TESTS.md, which is emphatic about why: a test double that has quietly drifted
from the real implementation is worse than no double, because it makes the whole suite
confidently wrong. Every test below is parametrised over `SqliteTicketRepository` and
`InMemoryTicketRepository`, so the double cannot drift without a failure
([STORAGE.md](../src/support_assistant/storage/STORAGE.md),
[ADR 0003](../docs/adr/0003-sqlite-behind-a-repository-protocol.md)).

The three methods are all the pipeline ever does: record a new ticket, read one back,
write a terminal state. `finalise` is one call rather than four setters so status, reply,
reason and trace land in a single transaction -- the tests here pin the resulting shape,
not the mechanism.

The one test that is *not* parametrised is at the bottom: the database's own `CHECK`
constraint, which is unreachable through the protocol because `Ticket`'s validator rejects
the same state first. It is the backstop, so it is tested where it lives.
"""

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from support_assistant.clock import DEFAULT_START, DEFAULT_TICK, FrozenClock
from support_assistant.domain import Ticket, new_ticket_id
from support_assistant.enums import HandoffReason, Intent, LiteralClass, TicketStatus
from support_assistant.storage.memory import InMemoryTicketRepository
from support_assistant.storage.protocol import (
    TicketAlreadyExists,
    TicketNotFound,
    TicketRepository,
)
from support_assistant.storage.sqlite import SqliteTicketRepository
from support_assistant.tracing.models import (
    FinalDecision,
    GroundingCheck,
    IntentClassified,
    ToolResultStep,
    TraceStep,
    Violation,
)

_CREATED = datetime(2026, 8, 31, 10, 14, tzinfo=UTC)


@pytest.fixture(params=["sqlite", "memory"])
def repo(request: pytest.FixtureRequest, tmp_path) -> Iterator[TicketRepository]:
    """The same suite, once per implementation.

    The SQLite one is file-backed rather than `:memory:` so the schema, the constraints
    and the JSON round-trip are all exercised the way the running service exercises them.
    """
    clock = FrozenClock()
    if request.param == "memory":
        yield InMemoryTicketRepository(clock)
        return
    repository = SqliteTicketRepository(tmp_path / "tickets.db", clock)
    yield repository
    repository.close()


def _ticket(**overrides) -> Ticket:
    fields = {
        "id": new_ticket_id(),
        "user_id": "u_002",
        "subject": "My payment failed",
        "body": "I got an email saying my last invoice couldn't be charged.",
        "created_at": _CREATED,
        "updated_at": _CREATED,
    }
    return Ticket(**(fields | overrides))


def _trace() -> list[TraceStep]:
    """One of every interesting step shape: an enum payload, a nested dict summary, a
    model list carrying a field whose serialised name is a Python keyword."""
    ts = datetime(2026, 8, 31, 10, 14, 1, tzinfo=UTC)
    return [
        IntentClassified(
            seq=1, ts=ts, intent=Intent.BILLING_QUESTION, matched_keywords=["invoice"]
        ),
        ToolResultStep(
            seq=2,
            ts=ts,
            tool="get_invoices",
            ok=True,
            summary={"count": 3, "statuses": {"paid": 2, "failed": 1}, "referenced": ["inv_204"]},
        ),
        GroundingCheck(
            seq=3,
            ts=ts,
            passed=False,
            literals_checked=4,
            violations=[
                Violation(
                    literal="99.00",
                    literal_class=LiteralClass.NUMBER,
                    reason="not present in FactSet or TEMPLATE_SAFE_LITERALS",
                )
            ],
        ),
        FinalDecision(
            seq=4,
            ts=ts,
            outcome=TicketStatus.HANDED_OFF,
            reason=HandoffReason.UNGROUNDED_REPLY,
            detail="ungrounded literals: 99.00 (number)",
        ),
    ]


# --------------------------------------------------------------------------------------
# create / get
# --------------------------------------------------------------------------------------


def test_a_created_ticket_reads_back_whole(repo: TicketRepository) -> None:
    ticket = _ticket()

    repo.create(ticket)

    stored = repo.get(ticket.id)
    assert stored is not None
    assert stored.id == ticket.id
    assert stored.user_id == ticket.user_id
    assert stored.subject == ticket.subject
    assert stored.body == ticket.body
    assert stored.status is TicketStatus.PROCESSING
    assert stored.reply is None
    assert stored.handoff_reason is None
    assert stored.created_at == _CREATED
    assert stored.updated_at == _CREATED


def test_an_unknown_id_reads_back_as_none(repo: TicketRepository) -> None:
    # `None`, not an exception: "does this ticket exist?" is a question the API asks on
    # every GET, and a 404 is a normal answer rather than a failure (API.md).
    assert repo.get(new_ticket_id()) is None


def test_a_new_ticket_has_no_trace(repo: TicketRepository) -> None:
    ticket = _ticket()
    repo.create(ticket)

    stored = repo.get(ticket.id)
    assert stored is not None
    assert stored.trace == []


def test_create_ignores_a_trace_on_the_ticket(repo: TicketRepository) -> None:
    # `Ticket.trace` is read-populated and write-ignored (STORAGE.md). During a run the
    # steps live in the recorder; `finalise` takes them as its own argument. Accepting
    # them here would give a ticket two ways to acquire a trace, and one of them would be
    # outside the single transaction that makes the write atomic.
    ticket = _ticket(trace=_trace())

    repo.create(ticket)

    stored = repo.get(ticket.id)
    assert stored is not None
    assert stored.trace == []


def test_creating_the_same_ticket_twice_is_refused(repo: TicketRepository) -> None:
    ticket = _ticket()
    repo.create(ticket)

    with pytest.raises(TicketAlreadyExists):
        repo.create(ticket)


# --------------------------------------------------------------------------------------
# finalise
# --------------------------------------------------------------------------------------


def test_finalising_to_replied_persists_the_reply_and_the_trace(repo: TicketRepository) -> None:
    ticket = _ticket()
    repo.create(ticket)
    trace = _trace()

    repo.finalise(ticket.id, TicketStatus.REPLIED, "Hi Ben, ...", None, trace)

    stored = repo.get(ticket.id)
    assert stored is not None
    assert stored.status is TicketStatus.REPLIED
    assert stored.reply == "Hi Ben, ..."
    assert stored.handoff_reason is None
    assert len(stored.trace) == len(trace)


def test_finalising_to_handed_off_persists_the_reason_and_no_reply(
    repo: TicketRepository,
) -> None:
    ticket = _ticket()
    repo.create(ticket)

    repo.finalise(ticket.id, TicketStatus.HANDED_OFF, None, HandoffReason.USER_NOT_FOUND, _trace())

    stored = repo.get(ticket.id)
    assert stored is not None
    assert stored.status is TicketStatus.HANDED_OFF
    assert stored.reply is None  # ADR 0005: not an empty string, not a holding message
    assert stored.handoff_reason is HandoffReason.USER_NOT_FOUND


def test_finalising_an_unknown_ticket_is_refused(repo: TicketRepository) -> None:
    # A silent no-op here would leave the pipeline believing it had written a terminal
    # state that nothing recorded -- the one thing `finalise` exists to make impossible.
    with pytest.raises(TicketNotFound):
        repo.finalise(new_ticket_id(), TicketStatus.REPLIED, "Hi Ben, ...", None, [])


def test_finalise_stamps_updated_at_and_leaves_created_at_alone(repo: TicketRepository) -> None:
    # Time comes from the injected clock, never `datetime.now()` (ADR 0008). The fixture's
    # FrozenClock starts at DEFAULT_START and advances one tick per reading.
    ticket = _ticket()
    repo.create(ticket)

    repo.finalise(ticket.id, TicketStatus.REPLIED, "Hi Ben, ...", None, [])

    stored = repo.get(ticket.id)
    assert stored is not None
    assert stored.created_at == _CREATED
    assert stored.updated_at == DEFAULT_START
    assert stored.updated_at != stored.created_at


def test_a_second_finalise_advances_updated_at(repo: TicketRepository) -> None:
    ticket = _ticket()
    repo.create(ticket)

    repo.finalise(ticket.id, TicketStatus.REPLIED, "first", None, [])
    repo.finalise(ticket.id, TicketStatus.REPLIED, "second", None, [])

    stored = repo.get(ticket.id)
    assert stored is not None
    assert stored.updated_at == DEFAULT_START + DEFAULT_TICK


def test_finalising_replaces_the_trace_rather_than_appending(repo: TicketRepository) -> None:
    # A re-run is a new ticket (PIPELINE.md), so this is defensive rather than a feature --
    # but a trace that silently doubled would be an audit record that says a step happened
    # twice when it happened once.
    ticket = _ticket()
    repo.create(ticket)

    repo.finalise(ticket.id, TicketStatus.REPLIED, "first", None, _trace())
    repo.finalise(ticket.id, TicketStatus.REPLIED, "second", None, _trace())

    stored = repo.get(ticket.id)
    assert stored is not None
    assert len(stored.trace) == len(_trace())


# --------------------------------------------------------------------------------------
# The trace round-trip
# --------------------------------------------------------------------------------------


def test_the_trace_reads_back_as_the_same_typed_steps(repo: TicketRepository) -> None:
    # The union is discriminated on `type`, so a persisted trace has to reconstruct to the
    # right classes rather than to untyped dicts (tracing/models.py). Equality on pydantic
    # models compares fields *and* class, so this one assert covers both.
    ticket = _ticket()
    repo.create(ticket)
    trace = _trace()

    repo.finalise(ticket.id, TicketStatus.HANDED_OFF, None, HandoffReason.UNGROUNDED_REPLY, trace)

    stored = repo.get(ticket.id)
    assert stored is not None
    assert stored.trace == trace


def test_the_trace_reads_back_in_seq_order(repo: TicketRepository) -> None:
    ticket = _ticket()
    repo.create(ticket)
    trace = list(reversed(_trace()))  # persisted out of order on purpose

    repo.finalise(ticket.id, TicketStatus.HANDED_OFF, None, HandoffReason.TOOL_ERROR, trace)

    stored = repo.get(ticket.id)
    assert stored is not None
    assert [step.seq for step in stored.trace] == [1, 2, 3, 4]


def test_a_step_timestamp_survives_with_its_timezone(repo: TicketRepository) -> None:
    ticket = _ticket()
    repo.create(ticket)
    trace = _trace()

    repo.finalise(ticket.id, TicketStatus.REPLIED, "Hi Ben, ...", None, trace)

    stored = repo.get(ticket.id)
    assert stored is not None
    assert stored.trace[0].ts == trace[0].ts
    assert stored.trace[0].ts.tzinfo is not None


def test_a_violation_survives_its_serialised_key(repo: TicketRepository) -> None:
    # `Violation.literal_class` serialises as `class` (TRACEABILITY.md owns that JSON
    # shape) because `class` is a Python keyword. A round-trip that dropped the alias
    # would lose the evidence for why a reply was withheld.
    ticket = _ticket()
    repo.create(ticket)

    repo.finalise(
        ticket.id, TicketStatus.HANDED_OFF, None, HandoffReason.UNGROUNDED_REPLY, _trace()
    )

    stored = repo.get(ticket.id)
    assert stored is not None
    check = stored.trace[2]
    assert isinstance(check, GroundingCheck)
    assert check.violations[0].literal == "99.00"
    assert check.violations[0].literal_class is LiteralClass.NUMBER


def test_a_summary_dict_survives_nested(repo: TicketRepository) -> None:
    ticket = _ticket()
    repo.create(ticket)

    repo.finalise(ticket.id, TicketStatus.REPLIED, "Hi Ben, ...", None, _trace())

    stored = repo.get(ticket.id)
    assert stored is not None
    result = stored.trace[1]
    assert isinstance(result, ToolResultStep)
    assert result.summary == {
        "count": 3,
        "statuses": {"paid": 2, "failed": 1},
        "referenced": ["inv_204"],
    }


def test_a_summary_keeps_its_key_order_through_a_round_trip(repo: TicketRepository) -> None:
    # `summarise.py` emits the status distribution in enum-declaration order -- paid
    # before failed -- so a reader sees the same shape every time. Equality on dicts
    # ignores order, so without this a persist step that sorted keys would look correct
    # and quietly replace the documented ordering with alphabetical.
    ticket = _ticket()
    repo.create(ticket)

    repo.finalise(ticket.id, TicketStatus.REPLIED, "Hi Ben, ...", None, _trace())

    stored = repo.get(ticket.id)
    assert stored is not None
    result = stored.trace[1]
    assert isinstance(result, ToolResultStep)
    assert result.summary is not None
    assert list(result.summary["statuses"]) == ["paid", "failed"]


# --------------------------------------------------------------------------------------
# Persistence, and the database's own backstop -- SQLite only
# --------------------------------------------------------------------------------------


def test_a_ticket_survives_a_new_repository_instance(tmp_path) -> None:
    # The claim ADR 0003 makes: traces survive a restart, so the audit story holds up.
    # The in-memory double cannot show this, which is why it is not parametrised.
    path = tmp_path / "tickets.db"
    ticket = _ticket()

    writer = SqliteTicketRepository(path, FrozenClock())
    writer.create(ticket)
    writer.finalise(ticket.id, TicketStatus.REPLIED, "Hi Ben, ...", None, _trace())
    writer.close()

    reader = SqliteTicketRepository(path, FrozenClock())
    stored = reader.get(ticket.id)
    reader.close()

    assert stored is not None
    assert stored.status is TicketStatus.REPLIED
    assert stored.trace == _trace()


def test_the_database_refuses_a_handed_off_ticket_that_carries_a_reply(tmp_path) -> None:
    # STORAGE.md's three-way CHECK. ADR 0005's invariant holds until the one code path
    # that forgets it, so the database enforces it too -- and this is the only way to
    # reach that constraint, since `Ticket`'s own validator rejects the same state first.
    repository = SqliteTicketRepository(tmp_path / "tickets.db", FrozenClock())

    with pytest.raises(sqlite3.IntegrityError):
        repository.connection.execute(
            "INSERT INTO tickets (id, user_id, subject, body, status, reply, handoff_reason,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "t_bad",
                "u_002",
                "s",
                "b",
                "handed_off",
                "a reply that should not exist",
                "USER_NOT_FOUND",
                "2026-08-31T10:14:00+00:00",
                "2026-08-31T10:14:00+00:00",
            ),
        )

    repository.close()


def test_a_check_violation_on_create_is_not_reported_as_a_duplicate_id(tmp_path) -> None:
    """SQLite raises `IntegrityError` for the primary key *and* for the `CHECK`. Reporting
    both as `TicketAlreadyExists` would make the backstop name the wrong fault in exactly
    the case it exists for -- a code path that forgot ADR 0005's invariant.

    `model_construct` is what gets past `Ticket`'s own validator, which is the only reason
    the constraint is otherwise unreachable through `create`.
    """
    repository = SqliteTicketRepository(tmp_path / "tickets.db", FrozenClock())
    handed_off_with_a_reply = Ticket.model_construct(
        id=new_ticket_id(),
        user_id="u_002",
        subject="s",
        body="b",
        status=TicketStatus.HANDED_OFF,
        reply="a reply that should not exist",
        handoff_reason=None,
        created_at=DEFAULT_START,
        updated_at=DEFAULT_START,
        trace=[],
    )

    with pytest.raises(sqlite3.IntegrityError) as caught:
        repository.create(handed_off_with_a_reply)
    assert "CHECK constraint failed" in str(caught.value)

    repository.close()
