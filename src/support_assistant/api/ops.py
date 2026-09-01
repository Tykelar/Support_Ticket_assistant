"""Operational endpoints. Not part of the brief's contract; they make the service
operable.

`GET /health` **touches the database**. A health check that only proves the process is
running is worse than none: the container reports healthy, the load balancer keeps sending
traffic, and every request fails on a database that is gone. So the check asks the
repository a real question and reports `503` when it cannot answer -- which is what makes
the `HEALTHCHECK` in [PACKAGING.md](../../../deploy/PACKAGING.md) mean something.

`GET /metrics` renders the in-process `MetricRegistry` as Prometheus text. The families
are defined in [OBSERVABILITY.md](../observability/OBSERVABILITY.md) and populated by
`record_run` at the end of every pipeline run. The endpoint is always present and always
names its metrics; only the sample lines wait for the first run, so a scrape is never an
empty body a dashboard could read as "nothing is wrong".
"""

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse, PlainTextResponse

from support_assistant.api.dependencies import Metrics, Repository
from support_assistant.api.schemas import Health

router = APIRouter(tags=["ops"])

_PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
"""The exposition-format content type a Prometheus scraper expects."""

_PROBE_ID = "t_00000000000000000000000000000000"
"""The id the health check asks about. Whether it exists is irrelevant and is not
asserted: the question is whether the store answers at all."""


@router.get(
    "/health",
    response_model=Health,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": Health}},
    summary="Liveness, including the database behind it",
)
def health(repository: Repository) -> JSONResponse:
    try:
        repository.get(_PROBE_ID)
    except Exception:
        # Deliberately broad. Every way storage can fail -- a missing file, a locked
        # database, a corrupt page -- means the same thing to whoever reads this: do not
        # send traffic here. Narrowing it to sqlite3 errors would also tie an operational
        # endpoint to which repository implementation is wired in.
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=Health(status="unavailable").model_dump(),
        )
    return JSONResponse(status_code=status.HTTP_200_OK, content=Health(status="ok").model_dump())


@router.get(
    "/metrics",
    response_class=PlainTextResponse,
    summary="The in-process counters and histograms, as Prometheus text",
)
def metrics(registry: Metrics) -> PlainTextResponse:
    """The registry `create_app` built and the pipeline writes to, rendered on demand.
    Unauthenticated, like everything else -- it exposes ticket volumes and the handoff
    breakdown to anyone who asks (API.md's security note)."""
    return PlainTextResponse(registry.render(), media_type=_PROMETHEUS_CONTENT_TYPE)
