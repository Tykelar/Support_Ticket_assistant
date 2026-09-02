"""What a request handler is given: the repository, the LLM client, the clock, the metrics.

They live on `app.state`, put there by the lifespan, and are read back through `Depends` so
a handler receives them **typed as their protocols** rather than reaching into untyped
state. That is the reason this module exists: `api/` should not be able to tell a
`SqliteTicketRepository` from an `InMemoryTicketRepository`.

Nothing is constructed here -- wiring is `create_app`'s job, once per application.
"""

from typing import Annotated

from fastapi import Depends, Request

from support_assistant.clock import Clock
from support_assistant.llm.protocol import LLMClient
from support_assistant.observability.metrics import MetricRegistry
from support_assistant.storage.protocol import TicketRepository


def get_repository(request: Request) -> TicketRepository:
    return request.app.state.repository


def get_llm(request: Request) -> LLMClient:
    return request.app.state.llm


def get_clock(request: Request) -> Clock:
    return request.app.state.clock


def get_metrics(request: Request) -> MetricRegistry:
    return request.app.state.metrics


Repository = Annotated[TicketRepository, Depends(get_repository)]
LLM = Annotated[LLMClient, Depends(get_llm)]
Time = Annotated[Clock, Depends(get_clock)]
"""Named `Time` rather than `Clock` so the alias does not shadow the protocol it wraps."""
Metrics = Annotated[MetricRegistry, Depends(get_metrics)]
"""The one registry the process shares: `POST /tickets` hands it to the background run,
`GET /metrics` renders it."""
