"""The two endpoints the brief's contract is made of.

Read them as a pair of one-liners, because that is what they are: `POST` validates,
creates and schedules; `GET` reads. There is no branch here that decides anything about a
ticket -- no tool is called, no reply is judged, no handoff is chosen. Every one of those
decisions belongs to the orchestrator, where it is recorded in the trace
([ADR 0005](../../../docs/adr/0005-fail-closed-to-human-handoff.md), API.md). A rule
enforced by `test_layering.py`, not by good intentions.

The `202`-and-a-background-task shape is
[ADR 0001](../../../docs/adr/0001-asynchronous-in-process-processing.md): `processing` is
one of the three statuses the brief requires, and it is only ever observable because the
response leaves before the pipeline finishes.
"""

from fastapi import APIRouter, BackgroundTasks, HTTPException, status

from support_assistant.api.dependencies import LLM, Metrics, Repository, Time
from support_assistant.api.schemas import CreateTicketRequest, TicketAccepted, TicketView
from support_assistant.domain import Ticket, new_ticket_id
from support_assistant.pipeline.orchestrator import run_pipeline

router = APIRouter(tags=["tickets"])

_NO_SUCH_TICKET = "no ticket with that id"
"""The `404` body, and deliberately not an echo of what was asked for. A ticket id is the
only thing protecting a trace (API.md), so the API does not reflect one back into a
response, a log line or a proxy's access record."""


@router.post(
    "/tickets",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=TicketAccepted,
    summary="Submit a ticket for automatic handling",
)
def create_ticket(
    payload: CreateTicketRequest,
    background: BackgroundTasks,
    repository: Repository,
    llm: LLM,
    clock: Time,
    metrics: Metrics,
) -> TicketAccepted:
    """Record the ticket, schedule the pipeline, and return the id immediately.

    The ticket is persisted **before** the task is scheduled, so the id in the response
    always resolves: a client that polls the instant it gets a `202` finds a ticket, not a
    `404`.

    `tools`, `resolve_template` and `max_iterations` are deliberately not passed --
    `run_pipeline` has production defaults for all three, and choosing them here would put
    pipeline configuration in the HTTP layer. `metrics` *is* passed: it is the one
    registry `GET /metrics` also reads, so the background run has to write to that same
    object rather than the module default.
    """
    now = clock.now()
    ticket = Ticket(
        id=new_ticket_id(),
        user_id=payload.user_id,
        subject=payload.subject,
        body=payload.body,
        created_at=now,
        updated_at=now,  # a ticket nothing has happened to yet was last touched when it arrived
    )
    repository.create(ticket)

    background.add_task(
        run_pipeline, ticket.id, repository=repository, llm=llm, clock=clock, metrics=metrics
    )

    return TicketAccepted(id=ticket.id, status=ticket.status)


@router.get(
    "/tickets/{ticket_id}",
    response_model=TicketView,
    responses={status.HTTP_404_NOT_FOUND: {"description": _NO_SUCH_TICKET}},
    summary="Everything known about a ticket, including why the AI said what it said",
)
def read_ticket(ticket_id: str, repository: Repository) -> TicketView:
    """The whole support-agent interface, in one call (requirement 5).

    The trace comes back with the ticket because the repository reads them together, so
    answering "why did the AI say this?" never needs a second request.
    """
    ticket = repository.get(ticket_id)
    if ticket is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=_NO_SUCH_TICKET)
    return TicketView.model_validate(ticket)
