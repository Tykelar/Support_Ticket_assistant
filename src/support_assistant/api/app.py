"""The application factory, and the one place the service is wired together.

`create_app()` takes its collaborators as optional arguments and falls back to the
production ones, so a test can assemble the whole HTTP surface over an in-memory repository
and a frozen clock without patching a global (ADR 0008).

**Who owns the repository.** The connection has to outlive a request, so the lifespan holds
it for the process lifetime -- and closes it only when it built it. An injected repository
belongs to whoever passed it in, and closing someone else's connection at shutdown is how a
suite starts failing in teardown.

Nothing opens a database at import time, which is what makes the module-level `app` below
safe.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from support_assistant.api import ops, routes
from support_assistant.clock import Clock, SystemClock
from support_assistant.demo import STATIC_DIR as DEMO_STATIC_DIR
from support_assistant.llm.protocol import LLMClient
from support_assistant.llm.provider import build_llm
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

    The defaults are the production ones: `build_llm()`'s choice of client, a
    `SystemClock`, and the process-wide `REGISTRY`. The repository is built in the lifespan
    rather than here, so importing this module never touches the filesystem.
    """
    the_clock = clock if clock is not None else SystemClock()
    the_llm = llm if llm is not None else build_llm()
    the_metrics = metrics if metrics is not None else REGISTRY

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging()
        # `owned` is what this lifespan opened, and so the only thing it may close.
        if repository is not None:
            owned = None
            app.state.repository = repository
        else:
            owned = SqliteTicketRepository(database_path(), the_clock)
            app.state.repository = owned
        app.state.llm = the_llm
        app.state.clock = the_clock
        app.state.metrics = the_metrics
        try:
            yield
        finally:
            if owned is not None:
                owned.close()

    app = FastAPI(title=TITLE, description=DESCRIPTION, version="0.1.0", lifespan=lifespan)
    app.include_router(routes.router)
    app.include_router(ops.router)

    # The demo page (DEMO.md). Static files only: it drives the same two endpoints a curl
    # would, so it reads nothing this app was not already serving to anyone who asked.
    app.mount("/ui", StaticFiles(directory=DEMO_STATIC_DIR, html=True), name="ui")
    return app


app = create_app()
"""The ASGI application `uvicorn support_assistant.api.app:app` serves (README.md,
PACKAGING.md). Production wiring, chosen by the defaults above."""
