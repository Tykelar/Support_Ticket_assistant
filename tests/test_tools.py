"""The three tools, read straight against the shared fixtures.

TOOLS.md is the contract. The properties that matter here are the ones a skim would miss:
zero rows is a typed failure and never `[]` (ADR 0009), the missing-user and missing-data
cases stay distinct (`u_005` vs `u_004`), one malformed row fails only its own user, and a
failure message carries a locator rather than the offending value. `test_fixtures.py`
already pinned these at the data level; this pins them at the loader level.
"""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from support_assistant.domain import ChargingSession, User
from support_assistant.tools import loaders
from support_assistant.tools.errors import NoDataAvailable, ToolExecutionError, UserNotFound


def _write_fixtures(
    directory: Path,
    *,
    users: list[dict] | None = None,
    sessions: list[dict] | None = None,
    invoices: list[dict] | None = None,
) -> None:
    """Lay down a full set of fixture files in `directory` -- the loaders read all three
    names, and the collection tools check the user before touching their own file."""
    (directory / "users.json").write_text(json.dumps(users or []), encoding="utf-8")
    (directory / "sessions.json").write_text(json.dumps(sessions or []), encoding="utf-8")
    (directory / "invoices.json").write_text(json.dumps(invoices or []), encoding="utf-8")


_A_USER = {"user_id": "u_x", "name": "X Person", "language": "en", "plan": "basic"}


def _invoice(invoice_id: str, issued_at: str, amount: str = "1.00") -> dict:
    return {
        "user_id": "u_x",
        "invoice_id": invoice_id,
        "amount": amount,
        "currency": "EUR",
        "status": "paid",
        "issued_at": issued_at,
    }


# --------------------------------------------------------------------------------------
# get_user -- one record, or UserNotFound. It cannot be empty.
# --------------------------------------------------------------------------------------


def test_get_user_returns_the_matching_record() -> None:
    user = loaders.get_user("u_002")
    assert isinstance(user, User)
    assert user.name == "Ben Carter"
    assert user.language == "en"


def test_get_user_works_for_the_user_whose_invoices_are_broken() -> None:
    # u_006's malformed row is in invoices.json; the user record itself is well formed,
    # so the damage stays scoped to the billing path.
    assert loaders.get_user("u_006").name == "Eva Nowak"


def test_get_user_raises_user_not_found_for_the_deliberately_absent_user() -> None:
    # u_005 is not a record, it is the absence of one -- the USER_NOT_FOUND path.
    with pytest.raises(UserNotFound):
        loaders.get_user("u_005")


def test_get_user_raises_user_not_found_for_an_unknown_id() -> None:
    with pytest.raises(UserNotFound):
        loaders.get_user("u_999")


def test_get_user_with_a_malformed_row_locates_it_without_the_bad_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # get_user validates the row whole -- User genuinely declares user_id. A bad field is
    # a ToolExecutionError whose message names the user and the field, never the offending
    # value: the message reaches the trace, which is served over the API.
    monkeypatch.setattr(loaders, "FIXTURES_DIR", tmp_path)
    _write_fixtures(tmp_path, users=[{**_A_USER, "plan": {"tier": "gold"}}])
    with pytest.raises(ToolExecutionError) as caught:
        loaders.get_user("u_x")
    message = str(caught.value)
    assert "user u_x" in message
    assert "plan" in message
    assert "gold" not in message


# --------------------------------------------------------------------------------------
# The collection tools -- newest first, exact decimals, never an empty list.
# --------------------------------------------------------------------------------------


def test_get_charging_sessions_returns_newest_first() -> None:
    sessions = loaders.get_charging_sessions("u_003")
    assert [s.session_id for s in sessions] == ["sess_3002", "sess_3001"]
    assert sessions[0].status.value == "interrupted", "the newest session is the problem one"
    assert all(isinstance(s, ChargingSession) for s in sessions)


def test_get_charging_sessions_parses_kwh_and_cost_as_decimal() -> None:
    session = loaders.get_charging_sessions("u_001")[0]
    assert session.kwh == Decimal("31.50")
    assert isinstance(session.cost, Decimal)


def test_get_invoices_returns_newest_first_with_exact_decimals() -> None:
    invoices = loaders.get_invoices("u_002")
    assert [i.invoice_id for i in invoices] == ["inv_204", "inv_203", "inv_202"]
    failed = invoices[0]
    assert failed.amount == Decimal("42.10")
    assert str(failed.amount) == "42.10", "the trailing zero is part of the fact (ADR 0004)"
    assert isinstance(failed.amount, Decimal)


def test_a_known_user_with_no_rows_raises_no_data_available_not_an_empty_list() -> None:
    # ADR 0009: zero rows is ambiguous -- genuinely none, not synced, or a broken join --
    # so the tool hands off rather than asserting the customer has no invoices.
    with pytest.raises(NoDataAvailable):
        loaders.get_invoices("u_004")
    with pytest.raises(NoDataAvailable):
        loaders.get_charging_sessions("u_004")


def test_the_collection_tools_check_the_user_before_the_data() -> None:
    # Both also raise UserNotFound for an unknown user, so the reason is right regardless
    # of which tool the loop reaches first.
    with pytest.raises(UserNotFound):
        loaders.get_invoices("u_005")
    with pytest.raises(UserNotFound):
        loaders.get_charging_sessions("u_005")


def test_missing_user_and_missing_data_do_not_collapse_into_one_reason() -> None:
    # The brief draws this distinction; u_004 and u_005 are the fixtures that keep it.
    assert not issubclass(NoDataAvailable, UserNotFound)
    assert not issubclass(UserNotFound, NoDataAvailable)
    with pytest.raises(NoDataAvailable):
        loaders.get_invoices("u_004")
    with pytest.raises(UserNotFound):
        loaders.get_invoices("u_005")


# --------------------------------------------------------------------------------------
# The regression TOOLS.md demands -- one bad row must not poison the file.
# --------------------------------------------------------------------------------------


def test_a_malformed_row_fails_only_its_own_user() -> None:
    with pytest.raises(ToolExecutionError):
        loaders.get_invoices("u_006")
    # Same file, read again: the happy path is untouched.
    assert [i.invoice_id for i in loaders.get_invoices("u_002")] == [
        "inv_204",
        "inv_203",
        "inv_202",
    ]


def test_the_failure_message_is_a_locator_not_the_offending_value() -> None:
    # The message reaches the trace, which is served over the API. The record id gets a
    # reader to the row; the bad value goes to the structured log instead.
    with pytest.raises(ToolExecutionError) as caught:
        loaders.get_invoices("u_006")
    message = str(caught.value)
    assert "inv_601" in message
    assert "u_006" in message
    assert "amount" in message
    assert "forty-two euros" not in message


# --------------------------------------------------------------------------------------
# How the loaders read -- sorted in the loader, parsed on every call.
# --------------------------------------------------------------------------------------


def test_the_loader_sorts_rather_than_trusting_the_file_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(loaders, "FIXTURES_DIR", tmp_path)
    _write_fixtures(
        tmp_path,
        users=[_A_USER],
        invoices=[
            _invoice("inv_old", "2026-01-01T09:00:00Z"),
            _invoice("inv_new", "2026-06-01T09:00:00Z"),
        ],
    )
    # Written oldest-first; the loader must reorder rather than relay the file.
    assert [i.invoice_id for i in loaders.get_invoices("u_x")] == ["inv_new", "inv_old"]


def test_the_loader_re_reads_the_file_on_every_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ADR 0003 kept fixtures as JSON so a reviewer can edit a file and re-run. A cache
    # would quietly require a restart and weaken the one property the format was chosen for.
    monkeypatch.setattr(loaders, "FIXTURES_DIR", tmp_path)
    _write_fixtures(tmp_path, users=[_A_USER], invoices=[_invoice("inv_1", "2026-01-01T09:00:00Z")])
    assert len(loaders.get_invoices("u_x")) == 1

    _write_fixtures(
        tmp_path,
        users=[_A_USER],
        invoices=[
            _invoice("inv_1", "2026-01-01T09:00:00Z"),
            _invoice("inv_2", "2026-02-01T09:00:00Z"),
        ],
    )
    assert len(loaders.get_invoices("u_x")) == 2


def test_an_unreadable_fixture_is_a_tool_execution_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(loaders, "FIXTURES_DIR", tmp_path)
    _write_fixtures(tmp_path, users=[_A_USER])
    (tmp_path / "invoices.json").write_text("{ not json", encoding="utf-8")
    with pytest.raises(ToolExecutionError):
        loaders.get_invoices("u_x")


def test_a_fixture_that_is_an_array_of_non_objects_is_a_tool_execution_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The `isinstance(data, list)` check passes but the rows are not objects. The loader
    # must still raise ToolExecutionError with a locator, not let an AttributeError escape
    # from `row.get` -- a malformed fixture is a typed failure (TOOLS.md).
    monkeypatch.setattr(loaders, "FIXTURES_DIR", tmp_path)
    _write_fixtures(tmp_path, users=[_A_USER])
    (tmp_path / "invoices.json").write_text(json.dumps(["u_x", 1, 2]), encoding="utf-8")
    with pytest.raises(ToolExecutionError):
        loaders.get_invoices("u_x")
