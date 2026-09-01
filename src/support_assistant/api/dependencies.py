"""What a request handler is given: the repository, the LLM client, and the clock.

They live on `app.state`, put there by the lifespan in `app.py`, and are read back through
`Depends` so a handler receives them **typed as their protocols** rather than reaching into
untyped application state itself. That is the whole reason this module exists: `api/`
should not be able to tell a `SqliteTicketRepository` from an `InMemoryTicketRepository`,
and a signature that says `TicketRepository` is what keeps it honest.

Nothing is constructed here. Wiring is `create_app`'s job, once per application, so a test
injects its own three and the production defaults live in exactly one place.
"""

from typing import Annotated

from fastapi import Depends, Request

from support_assistant.clock import Clock
from support_assistant.llm.protocol import LLMClient
from support_assistant.storage.protocol import TicketRepository


def get_repository(request: Request) -> TicketRepository:
    return request.app.state.repository


def get_llm(request: Request) -> LLMClient:
    return request.app.state.llm


def get_clock(request: Request) -> Clock:
    return request.app.state.clock


Repository = Annotated[TicketRepository, Depends(get_repository)]
LLM = Annotated[LLMClient, Depends(get_llm)]
Time = Annotated[Clock, Depends(get_clock)]
"""Named `Time` rather than `Clock` so the alias does not shadow the protocol it wraps."""
