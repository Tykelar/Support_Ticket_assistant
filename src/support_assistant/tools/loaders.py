"""The three tools, and the fixture reading and validation behind them.

Each tool *is* a thin loader: filter the flat table to the requested user, validate the
rows it fetched, sort newest-first, raise if there are none. TOOLS.md is the contract;
the constraints that would bite a skim are:

- **Filter by `user_id` first, validate second.** Every row carries `user_id` as its
  filter key; the models are `extra="forbid"` and have no such field, so it is stripped
  before validating. This is what stops `u_006`'s malformed invoice breaking every other
  user's billing path.
- **Zero rows is `NoDataAvailable`, never `[]`** -- collection tools only (ADR 0009).
- **Read and parse on every call, no cache** -- ADR 0003 keeps fixtures as JSON so a
  reviewer can edit a file and re-run without a restart.
- **Sort in the loader**, not trusted from the file: a reviewer editing a row should not
  be able to silently break the newest-first contract.
- A validation failure's message is a **locator, not the value** -- it lands in the trace,
  which is served over the API. The full `ValidationError` is chained for the log.
"""

import json
from pathlib import Path

from pydantic import ValidationError

from support_assistant.domain import ChargingSession, FixtureRecord, Invoice, User
from support_assistant.tools.errors import NoDataAvailable, ToolExecutionError, UserNotFound

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"
"""Resolved relative to the package, not injected. TESTS.md is explicit that the suite
reads the same files as the running service, so there is no second directory to point at;
making it an argument is a one-line change if that ever stops being true."""

_FILTER_KEY = "user_id"
"""Every fixture row carries this as its filter key. `User` also declares it as a real
field; `ChargingSession` and `Invoice` do not, so it is dropped before validating those."""


# --------------------------------------------------------------------------------------
# The three tools
# --------------------------------------------------------------------------------------


def get_user(user_id: str) -> User:
    """The one `User` for `user_id`, or `UserNotFound`. Cannot be empty."""
    rows = _rows_for("users.json", user_id)
    if not rows:
        raise UserNotFound(f"no user {user_id}")
    return _validate(User, rows[0], user_id, noun="user", id_field=_FILTER_KEY)


def get_charging_sessions(user_id: str) -> list[ChargingSession]:
    """Recent charging sessions for `user_id`, newest first."""
    get_user(user_id)  # an unknown user is UserNotFound, not NoDataAvailable
    rows = _rows_for("sessions.json", user_id)
    if not rows:
        raise NoDataAvailable(f"user {user_id} has no charging sessions")
    sessions = [
        _validate(
            ChargingSession, row, user_id,
            noun="charging session", id_field="session_id", drop_filter_key=True,
        )
        for row in rows
    ]
    return sorted(sessions, key=lambda s: s.started_at, reverse=True)


def get_invoices(user_id: str) -> list[Invoice]:
    """Recent invoices for `user_id`, newest first."""
    get_user(user_id)  # an unknown user is UserNotFound, not NoDataAvailable
    rows = _rows_for("invoices.json", user_id)
    if not rows:
        raise NoDataAvailable(f"user {user_id} has no invoices")
    invoices = [
        _validate(
            Invoice, row, user_id,
            noun="invoice", id_field="invoice_id", drop_filter_key=True,
        )
        for row in rows
    ]
    return sorted(invoices, key=lambda i: i.issued_at, reverse=True)


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
    if not isinstance(data, list):
        raise ToolExecutionError(f"fixture {filename} is not a JSON array")
    return data


def _rows_for(filename: str, user_id: str) -> list[dict]:
    """Every row for one user. Filter only -- the filter key is dropped per-model in
    `_validate`, since `User` keeps it and the collection models forbid it."""
    return [row for row in _read_rows(filename) if row.get(_FILTER_KEY) == user_id]


def _validate[R: FixtureRecord](
    model: type[R],
    row: dict,
    user_id: str,
    *,
    noun: str,
    id_field: str,
    drop_filter_key: bool = False,
) -> R:
    """Validate one already-filtered row, or raise `ToolExecutionError` whose message
    locates the row without quoting the offending value."""
    payload = {k: v for k, v in row.items() if k != _FILTER_KEY} if drop_filter_key else row
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        fields = ", ".join(
            ".".join(str(part) for part in error["loc"]) for error in exc.errors()
        )
        if id_field == _FILTER_KEY:
            locator = f"{noun} {user_id}"
        else:
            locator = f"{noun} {row.get(id_field, '?')} for {user_id}"
        raise ToolExecutionError(f"{locator} failed validation: {fields}") from exc
