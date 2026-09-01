"""The application factory, and the one place the service is wired together.

`create_app()` takes its three collaborators as optional arguments and falls back to the
production ones. That is what lets a test assemble the whole HTTP surface over an
in-memory repository and a frozen clock without patching a module-level global, and it is
the same injection argument the orchestrator makes one layer down
([ADR 0008](../../../docs/adr/0008-injected-clock-with-advancing-test-double.md)).

**Who owns the repository.** The database connection has to outlive a request -- a
connection per call would make `:memory:` lose its contents and would serialise nothing --
so something must hold it for the process lifetime and close it at the end. That is the
lifespan, and only when it built the repository itself: an injected one belongs to
whoever passed it in, and closing someone else's connection at shutdown is how a test
suite starts failing in its teardown.

Nothing opens a database at import time, which is what makes the module-level `app` below
safe: `uvicorn support_assistant.api.app:app` imports this file, and the connection is
made when the server starts, not when the module is read.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from support_assistant.api import ops, routes
from support_assistant.clock import Clock, SystemClock
from support_assistant.llm.fake import FakeLLM
from support_assistant.llm.protocol import LLMClient
from support_assistant.observability.logging import configure_logging
from support_assistant.observability.metrics import REGISTRY, MetricRegistry
from support_assistant.storage.protocol import TicketRepository
from support_assistant.storage.sqlite import SqliteTicketRepository, database_path

TITLE = "Support Ticket Auto-Reply Service"
DESCRIPTION = """\
Replies to EV-charging support tickets, or hands them to a human -- and records why.

`POST /tickets` accepts a ticket and returns immediately; the pipeline runs in the
background. `GET /tickets/{id}` returns the outcome together with the full trace of how it
was reached.

The service is deliberately unauthenticated: anyone holding a ticket id can read that
ticket's trace. See API.md.
"""


def create_app(
    *,
    repository: TicketRepository | None = None,
    llm: LLMClient | None = None,
    clock: Clock | None = None,
    metrics: MetricRegistry | None = None,
) -> FastAPI:
    """The application, with its collaborators injected or defaulted.

    The defaults are the production ones: `FakeLLM` (deterministic, offline -- ADR 0006),
    a `SystemClock`, and the process-wide metric `REGISTRY`. The repository is built in the
    lifespan rather than here, so importing this module never touches the filesystem.
    """
    the_clock = clock if clock is not None else SystemClock()
    the_llm = llm if llm is not None else FakeLLM()
    the_metrics = metrics if metrics is not None else REGISTRY

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging()
        owned = SqliteTicketRepository(database_path(), the_clock) if repository is None else None
        app.state.repository = repository if owned is None else owned
        app.state.llm = the_llm
        app.state.clock = the_clock
        app.state.metrics = the_metrics
        try:
            yield
        finally:
            if owned is not None:
                # Only what this lifespan opened. An injected repository is the caller's,
                # and closing it here would shut a connection its owner is still using.
                owned.close()

    app = FastAPI(title=TITLE, description=DESCRIPTION, version="0.1.0", lifespan=lifespan)
    app.include_router(routes.router)
    app.include_router(ops.router)
    return app


app = create_app()
"""The ASGI application `uvicorn support_assistant.api.app:app` serves (README.md,
PACKAGING.md). Production wiring, chosen by the defaults above."""
