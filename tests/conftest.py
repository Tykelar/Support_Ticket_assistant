"""Fixtures shared by the two end-to-end files TESTS.md requires.

They run against a **real `SqliteTicketRepository`** in `tmp_path` rather than the
in-memory double: the point of an end-to-end test is the production wiring, and the
serialisation path through SQLite is exactly where the last session's `sort_keys` defect
lived -- a defect every in-memory assertion passed straight over.

Time is still injected. One `FrozenClock` is shared by the app and the repository, so
timestamps are deterministic and monotonic across a whole request
([ADR 0008](../docs/adr/0008-injected-clock-with-advancing-test-double.md)).
"""

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from support_assistant.api.app import create_app
from support_assistant.clock import FrozenClock
from support_assistant.llm.fake import FakeLLM
from support_assistant.storage.sqlite import SqliteTicketRepository

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "src" / "support_assistant" / "fixtures"
"""The tool fixtures, read as raw JSON. An end-to-end assertion about what the reply is
allowed to say has to come from the data itself, not from the objects the pipeline built
out of it."""


@pytest.fixture
def e2e_client(tmp_path: Path) -> Iterator[TestClient]:
    """The service, wired as it is in production, over a throwaway database file."""
    clock = FrozenClock()
    repository = SqliteTicketRepository(tmp_path / "tickets.db", clock)
    try:
        app = create_app(repository=repository, llm=FakeLLM(), clock=clock)
        with TestClient(app) as client:
            yield client
    finally:
        # Windows will not remove tmp_path while the connection is open.
        repository.close()


@pytest.fixture
def submit(e2e_client: TestClient) -> Callable[[str, str, str], dict[str, Any]]:
    """`POST` a ticket, then `GET` it back -- the whole client contract in one call.

    No sleeping and no polling between the two: `TestClient` drains background tasks
    before returning the `POST` response (ADR 0001), which `test_api.py` asserts directly.
    """

    def _submit(user_id: str, subject: str, body: str) -> dict[str, Any]:
        accepted = e2e_client.post(
            "/tickets", json={"user_id": user_id, "subject": subject, "body": body}
        )
        assert accepted.status_code == 202, accepted.text
        assert accepted.json()["status"] == "processing"

        served = e2e_client.get(f"/tickets/{accepted.json()['id']}")
        assert served.status_code == 200, served.text
        return served.json()

    return _submit


@pytest.fixture
def fixture_rows() -> Callable[[str, str], list[dict[str, Any]]]:
    """One user's rows from a fixture file, as raw JSON.

    An end-to-end test asserts what the reply is allowed to say against *this*, not
    against the `FactSet` the pipeline assembled -- otherwise a broken grounding check
    and a broken assertion would agree with each other.
    """

    def _rows(name: str, user_id: str) -> list[dict[str, Any]]:
        rows = json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))
        return [row for row in rows if row["user_id"] == user_id]

    return _rows
