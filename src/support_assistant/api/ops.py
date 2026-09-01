"""Operational endpoints. Not part of the brief's contract; they make the service
operable.

`GET /health` **touches the database**. A health check that only proves the process is
running is worse than none: the container reports healthy, the load balancer keeps sending
traffic, and every request fails on a database that is gone. So the check asks the
repository a real question and reports `503` when it cannot answer -- which is what makes
the `HEALTHCHECK` in [PACKAGING.md](../../../deploy/PACKAGING.md) mean something.

`GET /metrics` lands with `observability/`; the counters it serves are defined in
[OBSERVABILITY.md](../observability/OBSERVABILITY.md) and nothing produces them yet. An
endpoint returning an empty metric set would be worse than an absent one -- a dashboard
would read zeros as "nothing is wrong".
"""

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse

from support_assistant.api.dependencies import Repository
from support_assistant.api.schemas import Health

router = APIRouter(tags=["ops"])

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
