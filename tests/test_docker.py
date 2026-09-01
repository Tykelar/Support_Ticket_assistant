"""The shipping image, exercised: it builds, it starts, and the container answers.

Everything else in the suite injects collaborators and never touches Docker. This test is
the one that proves `docker compose up` -- the primary path in
[PACKAGING.md](../deploy/PACKAGING.md) -- actually works: the multi-stage build succeeds,
the non-root container comes up, its `HEALTHCHECK` target returns `200`, and a real ticket
goes in and comes back `replied` through the same HTTP surface a reviewer would use.

It is deliberately kept out of the default `pytest` run (the `docker` marker, deselected by
`addopts` in `pyproject.toml`): a two-minute image build does not belong on every
invocation of a 1.5-second suite. Run it on demand with `pytest -m docker`, or set
`SKIP_DOCKER_TESTS` to skip it even then. What it does *not* cover is anything about the
image beyond "it builds and the service responds" (TESTS.md).
"""

import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(shutil.which("docker") is None, reason="Docker is not installed"),
    pytest.mark.skipif(
        bool(os.environ.get("SKIP_DOCKER_TESTS")), reason="SKIP_DOCKER_TESTS is set"
    ),
]

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = REPO_ROOT / "deploy" / "docker-compose.yml"
PROJECT = "support-ticket-assistant-test"
"""A dedicated compose project so this test's stack never collides with a stack a
developer is running by hand from the same repo."""

BASE_URL = "http://localhost:8000"
BUILD_TIMEOUT = 300  # seconds -- a cold multi-stage build pulling python:3.12-slim
HEALTHY_TIMEOUT = 90  # seconds -- from `up` returning to the first 200 from /health


def _compose(*args: str, timeout: int) -> subprocess.CompletedProcess[str]:
    """`docker compose` for this test's project, run from the repo root so the compose
    file's `context: ..` resolves there."""
    return subprocess.run(
        ["docker", "compose", "-p", PROJECT, "-f", str(COMPOSE_FILE), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )


@pytest.fixture
def running_stack() -> Iterator[None]:
    """Build the image and bring the container up; tear it down and drop the named volume
    afterwards, whatever happened."""
    try:
        _compose("up", "-d", "--build", timeout=BUILD_TIMEOUT)
        yield
    finally:
        _compose("down", "-v", "--remove-orphans", timeout=60)


def _wait_until_healthy() -> None:
    deadline = time.monotonic() + HEALTHY_TIMEOUT
    last: str = "no response yet"
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{BASE_URL}/health", timeout=5)
            if response.status_code == 200 and response.json() == {"status": "ok"}:
                return
            last = f"{response.status_code} {response.text}"
        except httpx.HTTPError as exc:  # not up yet, or mid-restart
            last = repr(exc)
        time.sleep(2)
    raise AssertionError(
        f"container never reported healthy within {HEALTHY_TIMEOUT}s; last: {last}"
    )


def test_the_image_builds_and_the_container_answers(running_stack: None) -> None:
    _wait_until_healthy()

    accepted = httpx.post(
        f"{BASE_URL}/tickets",
        json={
            "user_id": "u_002",
            "subject": "My payment failed",
            "body": "I got an email saying my invoice could not be charged. What happened?",
        },
        timeout=10,
    )
    assert accepted.status_code == 202, accepted.text
    ticket_id = accepted.json()["id"]

    # The pipeline runs as a background task; poll the terminal status rather than sleep.
    deadline = time.monotonic() + 30
    served: dict[str, object] = {}
    while time.monotonic() < deadline:
        served = httpx.get(f"{BASE_URL}/tickets/{ticket_id}", timeout=10).json()
        if served["status"] != "processing":
            break
        time.sleep(1)
    assert served["status"] == "replied", served

    metrics = httpx.get(f"{BASE_URL}/metrics", timeout=10)
    assert metrics.status_code == 200
    assert "tickets_total" in metrics.text
