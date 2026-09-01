"""The HTTP surface: validate, schedule, serve ticket state -- and nothing else.

Reserved by TESTS.md for `api/`. The two brief-required end-to-end files sit beside this
one and exercise the same endpoints against a real SQLite file; what this file pins is the
*contract*: the status codes, the field sets, what is rejected at the edge and -- more
interesting -- what deliberately is not.

Everything runs through `create_app(...)` with an in-memory repository and a `FrozenClock`,
so there is no file, no wall clock and no ambient state (ADR 0008). **One clock is shared
by the app and the repository**, so timestamps across a whole request are monotonic the
way they are in production, where both are the same `SystemClock`.

The test that matters most here is
`test_the_post_schedules_the_run_and_the_client_drains_it`: ADR 0001, TESTS.md and API.md
all rest on Starlette's `TestClient` draining background tasks before it returns the
response. That is a claim about somebody else's library, so it is asserted rather than
trusted -- if it stopped being true, every "POST then immediately GET" test below would
start passing for the wrong reason, or failing for a mysterious one.
"""

import re
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from support_assistant.api.app import create_app
from support_assistant.api.schemas import CreateTicketRequest
from support_assistant.clock import FrozenClock
from support_assistant.domain import (
    HandoffReason,
    Ticket,
    TicketStatus,
    new_ticket_id,
)
from support_assistant.enums import LiteralClass
from support_assistant.llm.fake import FakeLLM
from support_assistant.storage.memory import InMemoryTicketRepository
from support_assistant.tracing.models import FinalDecision, GroundingCheck, TraceStep, Violation

_NOW = datetime(2026, 8, 31, 10, 14, tzinfo=UTC)

_TICKET_ID = re.compile(r"^t_[0-9a-f]{32}$")
"""API.md: 128 bits of randomness, hex-encoded behind a `t_` prefix."""

_BILLING = {
    "user_id": "u_002",
    "subject": "My payment failed",
    "body": "I got an email saying my last invoice couldn't be charged. What happened?",
}

_DOCUMENTED_FIELDS = {
    "id",
    "user_id",
    "status",
    "reply",
    "handoff_reason",
    "created_at",
    "updated_at",
    "trace",
}
"""Exactly what `GET /tickets/{id}` returns, per API.md's field contract. `subject` and
`body` are deliberately not echoed back: the agent reading a ticket already has the
customer's words, and the response exists to answer "why did the AI say this?"."""


# --------------------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------------------


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock()


@pytest.fixture
def repository(clock: FrozenClock) -> InMemoryTicketRepository:
    return InMemoryTicketRepository(clock)


@pytest.fixture
def client(repository: InMemoryTicketRepository, clock: FrozenClock) -> Iterator[TestClient]:
    app = create_app(repository=repository, llm=FakeLLM(), clock=clock)
    with TestClient(app) as test_client:
        yield test_client


def _seed(repository: InMemoryTicketRepository, user_id: str = "u_002") -> Ticket:
    """A ticket the API did not create, so a `processing` state can be observed without
    racing the pipeline."""
    ticket = Ticket(
        id=new_ticket_id(),
        user_id=user_id,
        subject="Seeded",
        body="Placed straight into the repository",
        created_at=_NOW,
        updated_at=_NOW,
    )
    repository.create(ticket)
    return ticket


def _post(client: TestClient, **overrides: Any) -> httpx.Response:
    return client.post("/tickets", json=_BILLING | overrides)


def _bounds(model: type[BaseModel], field: str) -> tuple[int | None, int | None]:
    """The `(min_length, max_length)` a model declares for one field."""
    metadata = model.model_fields[field].metadata
    return (
        next((m.min_length for m in metadata if hasattr(m, "min_length")), None),
        next((m.max_length for m in metadata if hasattr(m, "max_length")), None),
    )


class _UnreachableDatabase:
    """A repository whose storage is down. Implements the whole protocol so it fails the
    way a real outage does -- at the call, not at construction."""

    _BROKEN = sqlite3.OperationalError("unable to open database file")

    def create(self, ticket: Ticket) -> None:
        raise self._BROKEN

    def get(self, ticket_id: str) -> Ticket | None:
        raise self._BROKEN

    def finalise(
        self,
        ticket_id: str,
        status: TicketStatus,
        reply: str | None,
        handoff_reason: HandoffReason | None,
        trace: list[TraceStep],
    ) -> None:
        raise self._BROKEN


# --------------------------------------------------------------------------------------
# POST /tickets
# --------------------------------------------------------------------------------------


def test_a_posted_ticket_is_accepted_with_its_id(client: TestClient) -> None:
    response = _post(client)

    assert response.status_code == 202  # not 201: the work it represents has not finished
    assert set(response.json()) == {"id", "status"}
    assert response.json()["status"] == "processing"


def test_the_id_is_unguessable_rather_than_a_sequence(client: TestClient) -> None:
    """The id is the only thing protecting a ticket's trace (API.md's security section),
    so an enumerable one would be a disclosure bug, not a cosmetic choice."""
    ids = {_post(client).json()["id"] for _ in range(5)}

    assert len(ids) == 5
    assert all(_TICKET_ID.match(ticket_id) for ticket_id in ids)


def test_the_ticket_is_persisted_before_the_response_returns(
    client: TestClient, repository: InMemoryTicketRepository
) -> None:
    """The `202` promises a resource exists. A client that reads the id back has to find
    it, whatever the pipeline is doing."""
    ticket_id = _post(client).json()["id"]

    stored = repository.get(ticket_id)
    assert stored is not None
    assert stored.user_id == _BILLING["user_id"]
    assert stored.subject == _BILLING["subject"]
    assert stored.body == _BILLING["body"]


def test_the_post_schedules_the_run_and_the_client_drains_it(
    client: TestClient, repository: InMemoryTicketRepository
) -> None:
    """ADR 0001's determinism claim, asserted rather than trusted.

    Two things at once: the endpoint really does schedule `run_pipeline` as a background
    task, and `TestClient` really does drain it before returning. Every other
    POST-then-GET test in the suite depends on the second half.
    """
    ticket_id = _post(client).json()["id"]

    stored = repository.get(ticket_id)
    assert stored is not None
    assert stored.status is TicketStatus.REPLIED  # terminal already, with no polling


def test_an_unknown_user_is_accepted_and_not_rejected_at_the_edge(client: TestClient) -> None:
    """API.md, deliberately: whether a user exists is a fact only a tool can establish,
    and establishing it is a pipeline step that belongs in the trace. Rejecting here with
    a `400` would put that decision outside the audit record."""
    response = _post(client, user_id="u_005")

    assert response.status_code == 202
    body = client.get(f"/tickets/{response.json()['id']}").json()
    assert body["status"] == "handed_off"
    assert body["handoff_reason"] == HandoffReason.USER_NOT_FOUND.value


@pytest.mark.parametrize(
    ("description", "payload"),
    [
        ("no user_id", {k: v for k, v in _BILLING.items() if k != "user_id"}),
        ("no subject", {k: v for k, v in _BILLING.items() if k != "subject"}),
        ("no body", {k: v for k, v in _BILLING.items() if k != "body"}),
        ("empty user_id", _BILLING | {"user_id": ""}),
        ("empty subject", _BILLING | {"subject": ""}),
        ("empty body", _BILLING | {"body": ""}),
        ("subject too long", _BILLING | {"subject": "x" * 201}),
        ("body too long", _BILLING | {"body": "x" * 5001}),
        ("subject is not a string", _BILLING | {"subject": 42}),
        ("an unknown field", _BILLING | {"priority": "urgent"}),
    ],
)
def test_a_malformed_ticket_is_rejected(
    client: TestClient, description: str, payload: dict[str, Any]
) -> None:
    """FastAPI's `422`, per API.md. The unknown-field case is the one worth stating: the
    request model forbids extras like `Ticket` does, so a client misspelling `body`
    is told, rather than having its ticket silently emptied."""
    assert client.post("/tickets", json=payload).status_code == 422, description


@pytest.mark.parametrize("field", ["user_id", "subject", "body"])
def test_the_request_schema_repeats_the_domain_limits(field: str) -> None:
    """The wire contract and the domain model state the same bounds, and this is what
    stops them drifting apart. Two enforcement points that disagree would be worse than
    one -- the same argument `Ticket`'s validator and the table `CHECK` already make."""
    assert _bounds(CreateTicketRequest, field) == _bounds(Ticket, field)


# --------------------------------------------------------------------------------------
# GET /tickets/{id}
# --------------------------------------------------------------------------------------


def test_an_unknown_ticket_is_a_404(client: TestClient) -> None:
    response = client.get(f"/tickets/{new_ticket_id()}")

    assert response.status_code == 404
    assert "detail" in response.json()


def test_the_response_carries_exactly_the_documented_fields(client: TestClient) -> None:
    ticket_id = _post(client).json()["id"]

    assert set(client.get(f"/tickets/{ticket_id}").json()) == _DOCUMENTED_FIELDS


def test_a_processing_ticket_has_no_reply_no_reason_and_an_empty_trace(
    client: TestClient, repository: InMemoryTicketRepository
) -> None:
    """Seeded directly rather than posted, because the pipeline finishes before the
    response returns and `processing` would otherwise be unobservable. API.md: the trace
    of a processing ticket is empty, not partial -- steps land with the terminal state in
    one transaction."""
    ticket = _seed(repository)

    body = client.get(f"/tickets/{ticket.id}").json()

    assert body["status"] == "processing"
    assert body["reply"] is None
    assert body["handoff_reason"] is None
    assert body["trace"] == []


def test_a_replied_ticket_carries_its_reply_and_no_reason(client: TestClient) -> None:
    ticket_id = _post(client).json()["id"]

    body = client.get(f"/tickets/{ticket_id}").json()

    assert body["status"] == "replied"
    assert body["reply"]
    assert body["handoff_reason"] is None


def test_a_handed_off_ticket_carries_its_reason_and_no_reply(client: TestClient) -> None:
    """ADR 0005: a handoff sends nothing at all -- not an empty string, not a holding
    message."""
    ticket_id = _post(client, user_id="u_005").json()["id"]

    body = client.get(f"/tickets/{ticket_id}").json()

    assert body["status"] == "handed_off"
    assert body["reply"] is None
    assert body["handoff_reason"]


def test_updated_at_moves_when_the_ticket_reaches_a_terminal_state(
    client: TestClient, repository: InMemoryTicketRepository
) -> None:
    """Why the field is served at all: it says when the run ended. A ticket that has not
    been touched since it arrived reports the two timestamps equal."""
    seeded = client.get(f"/tickets/{_seed(repository).id}").json()
    assert seeded["created_at"] == seeded["updated_at"]

    ran = client.get(f"/tickets/{_post(client).json()['id']}").json()
    assert ran["updated_at"] > ran["created_at"]


def test_the_timestamps_are_utc_with_a_z(client: TestClient) -> None:
    body = client.get(f"/tickets/{_post(client).json()['id']}").json()

    assert body["created_at"].endswith("Z")
    assert body["updated_at"].endswith("Z")


# --------------------------------------------------------------------------------------
# The trace, over the wire
# --------------------------------------------------------------------------------------


def test_the_served_trace_is_the_whole_story_in_order(client: TestClient) -> None:
    body = client.get(f"/tickets/{_post(client).json()['id']}").json()

    assert [step["type"] for step in body["trace"]] == [
        "intent_classified",
        "llm_decision",
        "tool_call",
        "tool_result",
        "llm_decision",
        "tool_call",
        "tool_result",
        "llm_decision",
        "grounding_check",
        "final_decision",
    ]
    assert [step["seq"] for step in body["trace"]] == list(range(1, 11))


def test_a_violation_is_served_under_class(
    client: TestClient, repository: InMemoryTicketRepository
) -> None:
    """`Violation.literal_class` is serialised as `class`, the key TRACEABILITY.md
    documents. The field is renamed in Python only because `class` is a keyword, and an
    alias that stopped at the storage layer would make the documented trace JSON wrong
    at the one place anybody reads it."""
    ticket = _seed(repository)
    repository.finalise(
        ticket.id,
        TicketStatus.HANDED_OFF,
        None,
        HandoffReason.UNGROUNDED_REPLY,
        [
            GroundingCheck(
                seq=1,
                ts=_NOW,
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
                seq=2,
                ts=_NOW,
                outcome=TicketStatus.HANDED_OFF,
                reason=HandoffReason.UNGROUNDED_REPLY,
                detail="reply withheld -- unsourced literals: 99.00 (number)",
            ),
        ],
    )

    violation = client.get(f"/tickets/{ticket.id}").json()["trace"][0]["violations"][0]

    assert violation["class"] == "number"
    assert "literal_class" not in violation


def test_a_served_step_omits_the_fields_it_does_not_have(client: TestClient) -> None:
    """The trace is read by a human under time pressure (TRACEABILITY.md), so a step
    carries the keys that apply to it and not a column of nulls. The top-level `reply` and
    `handoff_reason` are the opposite case -- API.md's field contract promises they are
    always present, and `null` there is the answer."""
    trace = client.get(f"/tickets/{_post(client).json()['id']}").json()["trace"]

    reply_decision = next(s for s in trace if s["type"] == "llm_decision" and s["seq"] == 8)
    assert "tool" not in reply_decision  # only a tool_call decision names a tool

    final = trace[-1]
    assert final["outcome"] == "replied"
    assert "reason" not in final and "detail" not in final


# --------------------------------------------------------------------------------------
# Operational endpoints
# --------------------------------------------------------------------------------------


def test_health_is_ok_when_the_database_answers(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_reports_unavailable_when_the_database_does_not() -> None:
    """A health check that does not touch the database is what makes a container
    HEALTHCHECK lie: the process is up and every request still fails."""
    app = create_app(repository=_UnreachableDatabase(), llm=FakeLLM(), clock=FrozenClock())
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["status"] != "ok"


def test_the_interactive_docs_are_served(client: TestClient) -> None:
    # README points a reader at /docs as the way to try the service.
    assert client.get("/docs").status_code == 200
