"""Operational endpoints. Not part of the brief's contract; they make the service operable.

`GET /health` **touches the database**. A check that only proves the process is running is
worse than none: the container reports healthy, the load balancer keeps sending traffic,
and every request fails on a database that is gone. So it asks the repository a real
question and reports `503` when it cannot answer, which is what makes the `HEALTHCHECK` in
[PACKAGING.md](../../../deploy/PACKAGING.md) mean something.

`GET /metrics` renders the in-process `MetricRegistry` as Prometheus text
([OBSERVABILITY.md](../observability/OBSERVABILITY.md)). It always names its families and
only the sample lines wait for the first run, so a scrape is never an empty body a
dashboard could read as "nothing is wrong".
"""

from fastapi import APIRouter, Response, status
from fastapi.responses import PlainTextResponse

from support_assistant.api.dependencies import Metrics, Repository
from support_assistant.api.schemas import Health

router = APIRouter(tags=["ops"])

_PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
"""The exposition-format content type a Prometheus scraper expects."""

_PROBE_ID = "t_00000000000000000000000000000000"
"""The id the health check asks about. Whether it exists is not asserted: the question is
whether the store answers at all."""


@router.get(
    "/health",
    response_model=Health,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": Health}},
    summary="Liveness, including the database behind it",
)
def health(repository: Repository, response: Response) -> Health:
    """The status code is set on the injected `Response` rather than by returning a
    `JSONResponse`, so the body still goes out through `response_model=Health`. A
    hand-built response would bypass that model and leave the two states unchecked."""
    try:
        repository.get(_PROBE_ID)
    except Exception:
        # Deliberately broad: every way storage can fail means the same thing to whoever
        # reads this. Narrowing to sqlite3 errors would also tie this endpoint to which
        # repository is wired in.
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return Health(status="unavailable")
    return Health(status="ok")


@router.get(
    "/metrics",
    response_class=PlainTextResponse,
    summary="The in-process counters and histograms, as Prometheus text",
)
def metrics(registry: Metrics) -> PlainTextResponse:
    """The registry `create_app` built and the pipeline writes to. Unauthenticated, like
    everything else -- it exposes ticket volumes and the handoff breakdown to anyone who
    asks (API.md)."""
    return PlainTextResponse(registry.render(), media_type=_PROMETHEUS_CONTENT_TYPE)
