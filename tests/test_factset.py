"""`FactSet` -- grounding layer 1: tool results projected into typed facts.

Reserved by TESTS.md ("projection from observations, `allowed_literals()`"). Every case
builds its history the way the orchestrator will -- an `Observation` pairing a `ToolCall`
with `registry.run`'s result against the *shared* fixtures -- so the projected facts
cannot drift from the data the running service reads (TESTS.md strategy).
"""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from support_assistant.domain import Observation, ToolCall
from support_assistant.enums import InvoiceStatus, SessionStatus
from support_assistant.guardrails.factset import FactSet, InvoiceFact, SessionFact
from support_assistant.tools import registry


def _observe(tool: str, user_id: str) -> Observation:
    step = ToolCall(tool=tool, args={"user_id": user_id})
    return Observation(step=step, result=registry.run(tool, step.args))


def _history(user_id: str, data_tool: str) -> list[Observation]:
    return [_observe("get_user", user_id), _observe(data_tool, user_id)]


# --------------------------------------------------------------------------------------
# Projection
# --------------------------------------------------------------------------------------


def test_empty_history_projects_an_empty_factset() -> None:
    facts = FactSet.from_observations([])
    assert facts.user_name is None
    assert facts.user_id is None
    assert facts.invoices == ()
    assert facts.sessions == ()
    assert facts.allowed_literals() == set()
    assert facts.allowed_numbers() == set()


def test_billing_history_projects_user_and_invoices_in_loader_order() -> None:
    facts = FactSet.from_observations(_history("u_002", "get_invoices"))

    assert facts.user_name == "Ben Carter"
    assert facts.user_id == "u_002"
    # get_invoices returns newest-first: inv_204 (08-15), inv_203 (07-15), inv_202 (06-15)
    assert [inv.invoice_id for inv in facts.invoices] == ["inv_204", "inv_203", "inv_202"]

    failed = facts.invoices[0]
    assert isinstance(failed, InvoiceFact)
    assert failed.status is InvoiceStatus.FAILED
    assert failed.amount == Decimal("42.10")
    assert failed.currency == "EUR"


def test_charging_history_projects_sessions_newest_first_with_stations() -> None:
    facts = FactSet.from_observations(_history("u_003", "get_charging_sessions"))

    assert facts.user_name == "Chloe Martin"
    # sess_3002 (08-22, interrupted) is newer than sess_3001 (08-09, completed)
    assert [s.session_id for s in facts.sessions] == ["sess_3002", "sess_3001"]
    assert isinstance(facts.sessions[0], SessionFact)
    assert facts.sessions[0].status is SessionStatus.INTERRUPTED
    assert facts.sessions[0].station == "Lyon Part-Dieu"
    assert facts.invoices == ()


def test_the_projection_drops_dates_and_plan() -> None:
    # A reply may never state an issue date or a plan tier; layer 1 makes that
    # unreachable by simply not carrying them.
    facts = FactSet.from_observations(_history("u_002", "get_invoices"))
    dropped = {"2026", "08", "15", "basic", "premium", "en"}
    assert dropped.isdisjoint(facts.allowed_literals())


# --------------------------------------------------------------------------------------
# allowed_literals() and the per-class helpers
# --------------------------------------------------------------------------------------


def test_allowed_literals_covers_names_ids_amounts_currencies_statuses_and_count() -> None:
    facts = FactSet.from_observations(_history("u_002", "get_invoices"))
    literals = facts.allowed_literals()

    assert {"Ben Carter", "Ben", "Carter"} <= literals          # name and its tokens
    assert {"u_002", "inv_204", "inv_203", "inv_202"} <= literals
    assert {"42.10", "38.90", "31.20"} <= literals              # amounts, two decimals
    assert "EUR" in literals
    assert {"failed", "paid"} <= literals
    assert "3" in literals                                      # the invoice count


def test_allowed_numbers_compares_amounts_as_decimals_not_strings() -> None:
    facts = FactSet.from_observations(_history("u_002", "get_invoices"))
    numbers = facts.allowed_numbers()

    assert Decimal("42.10") in numbers
    assert Decimal("42.1") in numbers        # 42.10 and 42.1 are the same fact
    assert Decimal("42,10".replace(",", ".")) in numbers
    assert Decimal(3) in numbers             # the count, as a number
    assert Decimal("99.00") not in numbers


def test_identifier_and_status_helpers_are_the_typed_subsets() -> None:
    facts = FactSet.from_observations(_history("u_002", "get_invoices"))
    assert facts.allowed_identifiers() == {"u_002", "inv_204", "inv_203", "inv_202"}
    assert facts.allowed_statuses() == {"failed", "paid"}


def test_allowed_entities_holds_the_open_vocabulary_strings_that_are_facts() -> None:
    # The strings layer 2 cannot verify but layer 1 sourced: station names and the user's
    # name. The checker masks these out of a reply before scanning it, so a real station
    # whose name contains a digit does not read as an ungrounded amount.
    facts = FactSet.from_observations(_history("u_003", "get_charging_sessions"))
    assert facts.allowed_entities() == {"Chloe Martin", "Lyon Part-Dieu", "Lyon Confluence"}


def test_allowed_entities_excludes_ids_amounts_and_status_words() -> None:
    # Masking is a weakening -- it must cover only what no other class already checks.
    facts = FactSet.from_observations(_history("u_002", "get_invoices"))
    entities = facts.allowed_entities()
    assert entities == {"Ben Carter"}
    assert entities.isdisjoint({"inv_204", "42.10", "failed", "EUR", "u_002"})


def test_allowed_entities_is_empty_without_facts() -> None:
    assert FactSet.from_observations([]).allowed_entities() == set()


def test_charging_literals_include_session_ids_stations_and_statuses() -> None:
    facts = FactSet.from_observations(_history("u_003", "get_charging_sessions"))
    literals = facts.allowed_literals()

    assert {"sess_3002", "sess_3001"} <= literals
    assert {"Lyon Part-Dieu", "Lyon Confluence"} <= literals
    assert {"interrupted", "completed"} <= literals
    assert "2" in literals                                      # the session count
    assert facts.allowed_statuses() == {"interrupted", "completed"}


# --------------------------------------------------------------------------------------
# Shape
# --------------------------------------------------------------------------------------


def test_factset_is_frozen() -> None:
    facts = FactSet.from_observations(_history("u_002", "get_invoices"))
    with pytest.raises(ValidationError):
        facts.user_name = "Someone Else"


def test_factset_forbids_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        FactSet(user_name="Ben Carter", surprise=True)
