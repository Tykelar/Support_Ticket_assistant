"""The brief's first required test: a billing question, end to end, over HTTP.

`POST` a ticket for `u_002` -- who has one failed invoice among paid ones -- and `GET` a
grounded reply back. The pipeline half of this is already covered by `test_pipeline.py`;
what this file adds is the HTTP surface, the background-task scheduling and the real
SQLite round trip (TESTS.md).

**The assertion that carries the file** is not `status == "replied"` but
`test_every_literal_in_the_reply_comes_from_the_fixture_data`: the grounding property
asserted directly on the output. It extracts the reply's literals with its own regexes and
checks them against the raw `fixtures/*.json` -- deliberately *not* against the `FactSet`
the pipeline built, so it is not the checker marking its own homework. A `GroundingChecker`
that had been quietly disabled would still pass every other test in this file.
"""

import re
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest

_USER = "u_002"
_SUBJECT = "My payment failed"
_BODY = "I got an email saying my last invoice couldn't be charged. What happened?"

_IDENTIFIER = re.compile(r"\b(?:inv|sess|u)_\w+\b")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_WORD = re.compile(r"[a-z]+")
"""Extraction only, and restated here rather than imported from
`guardrails/grounding.py` on purpose: a test that shares the checker's idea of what a
literal is cannot fail when the checker's idea is the thing that is wrong."""

_STATUS_VOCABULARY = {"paid", "pending", "failed", "completed", "interrupted"}
"""Every status word a reply could contain, listed independently of `enums.py` for the
same reason."""

Rows = Callable[[str, str], list[dict[str, Any]]]


@pytest.fixture
def ticket(submit: Callable[[str, str, str], dict[str, Any]]) -> dict[str, Any]:
    return submit(_USER, _SUBJECT, _BODY)


def _steps(ticket: dict[str, Any], step_type: str) -> list[dict[str, Any]]:
    return [step for step in ticket["trace"] if step["type"] == step_type]


# --------------------------------------------------------------------------------------
# The outcome
# --------------------------------------------------------------------------------------


def test_a_billing_question_gets_a_reply_and_no_handoff(ticket: dict[str, Any]) -> None:
    assert ticket["status"] == "replied"
    assert ticket["handoff_reason"] is None
    assert ticket["reply"]


def test_the_reply_names_the_real_invoice_and_the_real_amount(ticket: dict[str, Any]) -> None:
    """The failed invoice from the fixture, not a plausible-looking one."""
    assert "inv_204" in ticket["reply"]
    assert "42.10" in ticket["reply"]


def test_every_literal_in_the_reply_comes_from_the_fixture_data(
    ticket: dict[str, Any], fixture_rows: Rows
) -> None:
    """The unforgivable bug, asserted on the output rather than trusted because a
    guardrail ran.

    Every identifier, number and status word in the reply has to be traceable to a record
    the tools could actually have returned for this user.
    """
    reply = ticket["reply"]
    invoices = fixture_rows("invoices.json", _USER)

    identifiers = set(_IDENTIFIER.findall(reply))
    assert identifiers <= {invoice["invoice_id"] for invoice in invoices} | {_USER}

    without_ids = _IDENTIFIER.sub(" ", reply)  # so the digits in inv_204 are not read as money
    numbers = {Decimal(text.replace(",", ".")) for text in _NUMBER.findall(without_ids)}
    assert numbers <= {Decimal(invoice["amount"]) for invoice in invoices}

    spoken = set(_WORD.findall(reply.lower())) & _STATUS_VOCABULARY
    assert spoken <= {invoice["status"] for invoice in invoices}


def test_the_reply_greets_the_customer_by_their_real_name(
    ticket: dict[str, Any], fixture_rows: Rows
) -> None:
    """The name is a fact like any other, sourced from `get_user` (LLM.md)."""
    (user,) = fixture_rows("users.json", _USER)

    assert user["name"] in ticket["reply"]


# --------------------------------------------------------------------------------------
# The trace -- requirement 5, answered from the one GET
# --------------------------------------------------------------------------------------


def test_the_trace_records_the_classification_with_its_evidence(ticket: dict[str, Any]) -> None:
    (classified,) = _steps(ticket, "intent_classified")

    assert classified["intent"] == "billing_question"
    assert classified["matched_keywords"]  # ADR 0012: the evidence, not just the verdict


def test_the_trace_holds_both_tool_calls_with_their_results(ticket: dict[str, Any]) -> None:
    calls = _steps(ticket, "tool_call")
    results = _steps(ticket, "tool_result")

    assert [call["tool"] for call in calls] == ["get_user", "get_invoices"]
    assert [result["tool"] for result in results] == ["get_user", "get_invoices"]
    assert all(call["args"] == {"user_id": _USER} for call in calls)
    assert all(result["ok"] for result in results)


def test_the_tool_result_summary_can_be_traced_back_to_the_reply(
    ticket: dict[str, Any], fixture_rows: Rows
) -> None:
    """The `referenced` list is what lets a reader go from a sentence in the reply to the
    exact source record (TRACEABILITY.md)."""
    invoices = next(r for r in _steps(ticket, "tool_result") if r["tool"] == "get_invoices")

    assert invoices["summary"]["count"] == len(fixture_rows("invoices.json", _USER))
    assert "inv_204" in invoices["summary"]["referenced"]


def test_the_grounding_check_ran_and_passed(ticket: dict[str, Any]) -> None:
    """It runs unconditionally, whichever client wrote the reply (ADR 0004)."""
    (check,) = _steps(ticket, "grounding_check")

    assert check["passed"] is True
    assert check["literals_checked"] > 0


def test_the_trace_ends_in_exactly_one_final_decision(ticket: dict[str, Any]) -> None:
    (final,) = _steps(ticket, "final_decision")

    assert final is ticket["trace"][-1]
    assert final["outcome"] == "replied"
