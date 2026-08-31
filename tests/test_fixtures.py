"""The fixture data itself, checked as data.

The fixtures are test infrastructure as much as sample data (TOOLS.md): every pipeline
path has a user that exercises it, and one user is deliberately broken. These tests read
the raw JSON rather than going through the loaders -- the loaders arrive in phase 3, and
several of the properties below are the constraints those loaders must respect.
"""

import json
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent.parent / "src" / "support_assistant" / "fixtures"

KNOWN_USERS = {"u_001", "u_002", "u_003", "u_004", "u_006"}
ABSENT_USER = "u_005"


def _load(name: str) -> list[dict]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def users() -> list[dict]:
    return _load("users.json")


@pytest.fixture(scope="module")
def sessions() -> list[dict]:
    return _load("sessions.json")


@pytest.fixture(scope="module")
def invoices() -> list[dict]:
    return _load("invoices.json")


def _for(rows: list[dict], user_id: str) -> list[dict]:
    """Filter to one user -- the same 'filter first' the loaders must do."""
    return [row for row in rows if row["user_id"] == user_id]


# --------------------------------------------------------------------------------------
# Shape
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["users.json", "sessions.json", "invoices.json"])
def test_each_fixture_file_is_a_json_array(name: str) -> None:
    rows = _load(name)
    assert isinstance(rows, list)
    assert rows, f"{name} is empty"
    assert all(isinstance(row, dict) for row in rows)


def test_every_row_carries_the_user_id_the_loader_filters_on(
    sessions: list[dict], invoices: list[dict]
) -> None:
    # Flat tables keyed by user_id, so a tool can filter before validating. That ordering
    # is what stops u_006's bad row breaking everyone else.
    for row in sessions + invoices:
        assert "user_id" in row


def test_no_row_references_an_unknown_user(
    sessions: list[dict], invoices: list[dict]
) -> None:
    for row in sessions + invoices:
        assert row["user_id"] in KNOWN_USERS


def test_identifiers_are_unique(sessions: list[dict], invoices: list[dict]) -> None:
    # Grounding traces a sentence back to one source record via its id (TRACEABILITY.md);
    # a duplicated id would make that lookup ambiguous.
    assert len({s["session_id"] for s in sessions}) == len(sessions)
    assert len({i["invoice_id"] for i in invoices}) == len(invoices)


def test_rows_are_ordered_newest_first_per_user(
    sessions: list[dict], invoices: list[dict]
) -> None:
    # TOOLS.md says both collection tools return recent rows newest first. Keeping the
    # file in that order means the fixture reads the way the tool returns it.
    for user_id in KNOWN_USERS:
        starts = [datetime.fromisoformat(r["started_at"]) for r in _for(sessions, user_id)]
        assert starts == sorted(starts, reverse=True)
        issued = [datetime.fromisoformat(r["issued_at"]) for r in _for(invoices, user_id)]
        assert issued == sorted(issued, reverse=True)


# --------------------------------------------------------------------------------------
# The six users, and the path each one exercises
# --------------------------------------------------------------------------------------


def test_the_fixture_users_are_exactly_the_documented_five_records(
    users: list[dict],
) -> None:
    assert {u["user_id"] for u in users} == KNOWN_USERS


def test_u_005_is_absent_from_every_file(
    users: list[dict], sessions: list[dict], invoices: list[dict]
) -> None:
    # u_005 is not a record, it is the deliberate absence of one -- the USER_NOT_FOUND
    # path. A well-meaning edit that "completes" the fixtures would delete that path.
    for row in users + sessions + invoices:
        assert row["user_id"] != ABSENT_USER


def test_users_carry_varied_languages(users: list[dict]) -> None:
    # Replies are English only, but `language` is read and traced (ADR 0006). Varied
    # values keep the scope cut visible rather than accidentally untested.
    assert len({u["language"] for u in users}) > 1


def test_u_001_is_the_nothing_wrong_path(
    invoices: list[dict], sessions: list[dict]
) -> None:
    assert {i["status"] for i in _for(invoices, "u_001")} == {"paid"}
    assert {s["status"] for s in _for(sessions, "u_001")} == {"completed"}


def test_u_002_matches_the_worked_example_in_the_docs(invoices: list[dict]) -> None:
    # API.md's sample trace states count 3 and {paid: 2, failed: 1}, and the sample reply
    # names inv_204 at 42.10. Pinning it here means the doc and the data cannot drift.
    rows = _for(invoices, "u_002")
    assert len(rows) == 3
    assert Counter(r["status"] for r in rows) == {"paid": 2, "failed": 1}
    failed = next(r for r in rows if r["status"] == "failed")
    assert failed["invoice_id"] == "inv_204"
    assert Decimal(failed["amount"]) == Decimal("42.10")
    assert failed["currency"] == "EUR"


def test_u_003_is_the_interrupted_session_path(sessions: list[dict]) -> None:
    rows = _for(sessions, "u_003")
    assert rows[0]["status"] == "interrupted", "the newest session is the problem one"


def test_u_004_exists_but_has_no_data(
    users: list[dict], sessions: list[dict], invoices: list[dict]
) -> None:
    # The DATA_NOT_FOUND path: the distinction between "no such user" and "no such data"
    # is one the brief draws, so it needs a user of its own.
    assert any(u["user_id"] == "u_004" for u in users)
    assert _for(sessions, "u_004") == []
    assert _for(invoices, "u_004") == []


def test_u_006_has_a_malformed_invoice_amount(invoices: list[dict]) -> None:
    rows = _for(invoices, "u_006")
    assert rows, "u_006 must have an invoice for the TOOL_ERROR path to exist"
    with pytest.raises(InvalidOperation):
        Decimal(rows[0]["amount"])


def test_u_006_is_broken_only_in_its_invoices(sessions: list[dict]) -> None:
    # Scoping the damage keeps the fixture honest: u_006 fails the billing path for a
    # specific reason, not because the record is generally unusable.
    rows = _for(sessions, "u_006")
    assert rows
    for row in rows:
        Decimal(row["kwh"])
        Decimal(row["cost"])


def test_one_bad_row_does_not_poison_the_file(invoices: list[dict]) -> None:
    """The regression TOOLS.md demands: u_006 fails while u_002 still parses.

    invoices.json holds every user's invoices. Eager whole-file validation would let the
    row built to exercise one failure path take out the happy paths as collateral, so the
    loader must filter by user_id before validating -- and this pins the data shape that
    makes the distinction possible.
    """
    with pytest.raises(InvalidOperation):
        [Decimal(r["amount"]) for r in _for(invoices, "u_006")]
    assert [Decimal(r["amount"]) for r in _for(invoices, "u_002")] == [
        Decimal("42.10"),
        Decimal("38.90"),
        Decimal("31.20"),
    ]


# --------------------------------------------------------------------------------------
# Coverage of the paths, stated as a property rather than per user
# --------------------------------------------------------------------------------------


def test_every_status_word_appears_in_the_data(
    sessions: list[dict], invoices: list[dict]
) -> None:
    # Every reply template in LLM.md is selected by a status, so a status with no fixture
    # is a template no test can reach -- billing_pending being the easy one to forget.
    assert {i["status"] for i in invoices} >= {"paid", "pending", "failed"}
    assert {s["status"] for s in sessions} >= {"completed", "interrupted"}


def test_amounts_are_strings_so_decimals_stay_exact(invoices: list[dict]) -> None:
    # A JSON float 42.10 becomes 42.1 through binary, and the reply would then state a
    # number the fixture does not contain. Grounding compares Decimals (ADR 0004).
    for row in invoices:
        assert isinstance(row["amount"], str)
    assert str(Decimal(next(r["amount"] for r in invoices if r["invoice_id"] == "inv_204"))) == (
        "42.10"
    )
