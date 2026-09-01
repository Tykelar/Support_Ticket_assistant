"""The brief's second required test: missing data hands the ticket to a human, over HTTP.

Two cases, because the brief distinguishes "the user doesn't exist" from "the requested
data doesn't exist" and the two must not collapse into one reason -- a support agent
triaging a queue is reading exactly that distinction, and
`handoffs_total{reason}` is only informative while it holds
([OBSERVABILITY.md](../src/support_assistant/observability/OBSERVABILITY.md)).

| Case | User | Expected |
|---|---|---|
| absent from the fixtures | `u_005` | `USER_NOT_FOUND` |
| exists, has no invoices | `u_004` | `DATA_NOT_FOUND` |

Both assert the reply is **`null`** -- not an empty string, not a holding message
([ADR 0005](../docs/adr/0005-fail-closed-to-human-handoff.md)) -- and that the
`final_decision` carries the reason *and* the detail: the category explains what kind of
thing went wrong, the detail explains which incident it was (GUARDRAILS.md).
"""

from collections.abc import Callable
from typing import Any

import pytest

_SUBJECT = "My payment failed"
_BODY = "I got an email saying my last invoice couldn't be charged. What happened?"

Submit = Callable[[str, str, str], dict[str, Any]]

_CASES = [
    pytest.param("u_005", "USER_NOT_FOUND", id="absent-user"),
    pytest.param("u_004", "DATA_NOT_FOUND", id="user-with-no-invoices"),
]
_USERS = [case.values[0] for case in _CASES]


def _final(ticket: dict[str, Any]) -> dict[str, Any]:
    (final,) = [step for step in ticket["trace"] if step["type"] == "final_decision"]
    return final


@pytest.mark.parametrize(("user_id", "reason"), _CASES)
def test_missing_data_hands_the_ticket_off_with_its_own_reason(
    submit: Submit, user_id: str, reason: str
) -> None:
    ticket = submit(user_id, _SUBJECT, _BODY)

    assert ticket["status"] == "handed_off"
    assert ticket["handoff_reason"] == reason


@pytest.mark.parametrize("user_id", _USERS)
def test_a_handed_off_ticket_sends_the_customer_nothing(submit: Submit, user_id: str) -> None:
    """`null`, not `""`. An empty reply is indistinguishable from one that failed to
    render, and a customer must never receive a half-answer the system could not stand
    behind."""
    ticket = submit(user_id, _SUBJECT, _BODY)

    assert ticket["reply"] is None


@pytest.mark.parametrize(("user_id", "reason"), _CASES)
def test_the_final_decision_carries_the_reason_and_the_incident(
    submit: Submit, user_id: str, reason: str
) -> None:
    final = _final(submit(user_id, _SUBJECT, _BODY))

    assert final["outcome"] == "handed_off"
    assert final["reason"] == reason
    assert final["detail"]
    assert user_id in final["detail"]  # which user, not merely which category


def test_the_trace_shows_where_the_run_stopped(submit: Submit) -> None:
    """`get_user` succeeds for `u_004` and `get_invoices` is the call that finds nothing,
    so the trace has to show one `ok` result and one failure -- otherwise the agent cannot
    tell "we could not identify you" from "we have no billing data for you"."""
    ticket = submit("u_004", _SUBJECT, _BODY)

    results = [step for step in ticket["trace"] if step["type"] == "tool_result"]
    assert [(r["tool"], r["ok"]) for r in results] == [("get_user", True), ("get_invoices", False)]
    assert results[-1]["error"]["type"] == "NoDataAvailable"


def test_an_absent_user_stops_at_the_very_first_tool(submit: Submit) -> None:
    ticket = submit("u_005", _SUBJECT, _BODY)

    results = [step for step in ticket["trace"] if step["type"] == "tool_result"]
    assert [(r["tool"], r["ok"]) for r in results] == [("get_user", False)]
    assert results[-1]["error"]["type"] == "UserNotFound"


def test_no_reply_was_ever_drafted_so_nothing_was_grounded(submit: Submit) -> None:
    """A handoff on missing data never reaches the renderer, so there is no
    `grounding_check` step. If one appeared, a reply existed and was thrown away
    somewhere the trace does not record."""
    ticket = submit("u_005", _SUBJECT, _BODY)

    assert [step for step in ticket["trace"] if step["type"] == "grounding_check"] == []


def test_the_two_cases_do_not_collapse_into_one_reason(submit: Submit) -> None:
    absent = submit("u_005", _SUBJECT, _BODY)
    empty = submit("u_004", _SUBJECT, _BODY)

    assert absent["handoff_reason"] != empty["handoff_reason"]
