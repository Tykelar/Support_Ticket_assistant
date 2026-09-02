"""The two endpoints the brief's contract is made of.

`POST` validates, creates and schedules; `GET` reads. No branch here decides anything about
a ticket -- no tool is called, no reply judged, no handoff chosen. Those belong to the
orchestrator, where they are recorded in the trace (ADR 0005), and `test_layering.py`
enforces it.

The `202`-and-a-background-task shape is
[ADR 0001](../../../docs/adr/0001-asynchronous-in-process-processing.md): `processing` is
only ever observable because the response leaves before the pipeline finishes.
"""

from fastapi import APIRouter, BackgroundTasks, HTTPException, status

from support_assistant.api.dependencies import LLM, Metrics, Repository, Time
from support_assistant.api.schemas import CreateTicketRequest, TicketAccepted, TicketView
from support_assistant.domain import Ticket, new_ticket_id
from support_assistant.pipeline.orchestrator import run_pipeline

router = APIRouter(tags=["tickets"])

_NO_SUCH_TICKET = "no ticket with that id"
"""Deliberately not an echo of what was asked for: a ticket id is the only thing protecting
a trace, so it is never reflected into a response, a log line or a proxy's records."""


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

    Persisted **before** the task is scheduled, so a client that polls the instant it gets
    a `202` finds a ticket rather than a `404`.

    `tools`, `resolve_template` and `max_iterations` are deliberately not passed: choosing
    them here would put pipeline configuration in the HTTP layer. `metrics` must be, since
    it is the registry `GET /metrics` reads.
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
    """The whole support-agent interface, in one call. The repository reads the trace with
    the ticket, so "why did the AI say this?" never needs a second request."""
    ticket = repository.get(ticket_id)
    if ticket is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=_NO_SUCH_TICKET)
    return TicketView.model_validate(ticket)
