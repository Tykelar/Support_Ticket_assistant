"""The three tools, and the fixture reading and validation behind them.

Each tool *is* a thin loader: filter the flat table to the requested user, validate the
rows it fetched, sort newest-first, raise if there are none. TOOLS.md is the contract;
the constraints that would bite a skim are:

- **Filter by `user_id` first, validate second.** Every row carries `user_id` as its
  filter key. `get_user` validates its row whole (`User` declares `user_id`); `_collection`
  drops the key per row, since the collection models are `extra="forbid"` and do not.
  Filtering first is what stops `u_006`'s malformed invoice breaking every other user's
  billing path.
- **Zero rows is `NoDataAvailable`, never `[]`** -- collection tools only (ADR 0009).
- **Read and parse on every call, no cache** -- ADR 0003 keeps fixtures as JSON so a
  reviewer can edit a file and re-run without a restart.
- **Sort in the loader**, not trusted from the file: a reviewer editing a row should not
  be able to silently break the newest-first contract.
- A validation failure's message is a **locator, not the value** -- it lands in the trace,
  which is served over the API. The full `ValidationError` is chained for the log.
"""

import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from support_assistant.domain import ChargingSession, FixtureRecord, Invoice, User
from support_assistant.tools.errors import (
    NoDataAvailable,
    ToolExecutionError,
    UserNotFound,
    failed_fields,
)

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"
"""Resolved relative to the package, not injected. TESTS.md is explicit that the suite
reads the same files as the running service, so there is no second directory to point at;
making it an argument is a one-line change if that ever stops being true."""

_FILTER_KEY = "user_id"
"""Every fixture row carries this as its filter key. `User` also declares it as a real
field, so its row validates whole; the collection models forbid it, so `_collection` drops
it per row before validating."""


# --------------------------------------------------------------------------------------
# The three tools
# --------------------------------------------------------------------------------------


def get_user(user_id: str) -> User:
    """The one `User` for `user_id`, or `UserNotFound`. Cannot be empty."""
    rows = _rows_for("users.json", user_id)
    if not rows:
        raise UserNotFound(f"no user {user_id}")
    return _validate(User, rows[0], locator=f"user {user_id}")


def get_charging_sessions(user_id: str) -> list[ChargingSession]:
    """Recent charging sessions for `user_id`, newest first."""
    return _collection(
        ChargingSession, "sessions.json", user_id,
        noun="charging session", id_field="session_id", sort_key=lambda s: s.started_at,
    )


def get_invoices(user_id: str) -> list[Invoice]:
    """Recent invoices for `user_id`, newest first."""
    return _collection(
        Invoice, "invoices.json", user_id,
        noun="invoice", id_field="invoice_id", sort_key=lambda i: i.issued_at,
    )


def _collection[R: FixtureRecord](
    model: type[R],
    filename: str,
    user_id: str,
    *,
    noun: str,
    id_field: str,
    sort_key: Callable[[R], datetime],
) -> list[R]:
    """The shared body of the two collection tools: check the user, filter to their rows,
    fail if there are none (ADR 0009), validate each, return them newest first."""
    get_user(user_id)  # an unknown user is UserNotFound, not NoDataAvailable
    rows = _rows_for(filename, user_id)
    if not rows:
        raise NoDataAvailable(f"user {user_id} has no {noun}s")
    records = [
        _validate(
            model,
            {k: v for k, v in row.items() if k != _FILTER_KEY},
            locator=f"{noun} {row.get(id_field, '?')} for {user_id}",
        )
        for row in rows
    ]
    return sorted(records, key=sort_key, reverse=True)


# --------------------------------------------------------------------------------------
# Reading and validation -- private, and deliberately not their own module
# --------------------------------------------------------------------------------------


def _read_rows(filename: str) -> list[dict]:
    """Parse one fixture file into a list of rows. Read fresh every call -- no cache."""
    path = FIXTURES_DIR / filename
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ToolExecutionError(f"could not read fixture {filename}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ToolExecutionError(f"fixture {filename} is not valid JSON") from exc
    if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
        raise ToolExecutionError(f"fixture {filename} is not a JSON array of objects")
    return data


def _rows_for(filename: str, user_id: str) -> list[dict]:
    """Every row for one user. Filter only -- `_collection` drops the filter key per row
    before validating; `get_user` keeps it, since `User` declares it as a real field."""
    return [row for row in _read_rows(filename) if row.get(_FILTER_KEY) == user_id]


def _validate[R: FixtureRecord](model: type[R], payload: dict, *, locator: str) -> R:
    """Validate one already-filtered row, or raise `ToolExecutionError` whose message is
    `locator` plus the failed field names -- never the offending value, which stays in the
    chained `ValidationError` bound for the log."""
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise ToolExecutionError(
            f"{locator} failed validation: {failed_fields(exc)}"
        ) from exc
