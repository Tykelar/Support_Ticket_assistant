"""`GroundingChecker` -- grounding layer 2: post-hoc verification of a rendered reply.

Reserved by TESTS.md ("the checker"). The load-bearing case is
`test_ungrounded_literal_is_caught`: a deliberately doctored template that injects an
amount no tool returned must be flagged, with the offending literal recorded. That is the
evidence the brief's "one unforgivable bug" cannot ship (GUARDRAILS.md section 3, ADR
0004). The full `handed_off` / `UNGROUNDED_REPLY` outcome is asserted end to end in the
pipeline phase; here the checker is exercised directly.
"""

import pytest

from support_assistant.domain import Observation, ToolCall
from support_assistant.guardrails.checker import GroundingChecker
from support_assistant.guardrails.factset import FactSet
from support_assistant.tools import registry


def _facts(user_id: str, data_tool: str) -> FactSet:
    history = [
        Observation(
            step=ToolCall(tool=tool, args={"user_id": user_id}),
            result=registry.run(tool, {"user_id": user_id}),
        )
        for tool in ("get_user", data_tool)
    ]
    return FactSet.from_observations(history)


class _Template:
    """A stand-in for an `llm.templates.Template` -- the checker only needs the
    safe-literal set (it is passed the template, never `llm/`)."""

    def __init__(self, *safe: str) -> None:
        self.TEMPLATE_SAFE_LITERALS = frozenset(safe)


_NO_SAFE_LITERALS = _Template()


# --------------------------------------------------------------------------------------
# The one that guards the unforgivable bug
# --------------------------------------------------------------------------------------


def test_ungrounded_literal_is_caught() -> None:
    facts = _facts("u_002", "get_invoices")  # amounts 42.10, 38.90, 31.20
    doctored_reply = (
        "Hi Ben Carter, invoice inv_204 for 99.00 EUR has a failed payment."
    )

    violations = GroundingChecker.verify(doctored_reply, facts, _NO_SAFE_LITERALS)

    assert len(violations) == 1
    (bad,) = violations
    assert bad.literal == "99.00"
    assert bad.literal_class == "number"
    assert "FactSet" in bad.reason


# --------------------------------------------------------------------------------------
# Numeric normalisation
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("written", ["42.10", "42,10", "42.1"])
def test_an_amount_is_grounded_however_it_is_written(written: str) -> None:
    facts = _facts("u_002", "get_invoices")
    reply = f"Your invoice inv_204 came to {written} EUR."
    assert GroundingChecker.verify(reply, facts, _NO_SAFE_LITERALS) == []


# --------------------------------------------------------------------------------------
# TEMPLATE_SAFE_LITERALS
# --------------------------------------------------------------------------------------


def test_a_static_number_passes_only_when_the_template_declares_it() -> None:
    facts = _facts("u_003", "get_invoices")  # 2 invoices, so "3" is not a count either
    reply = "Pending invoices are collected within 3 business days."

    assert GroundingChecker.verify(reply, facts, _Template("3")) == []

    (bad,) = GroundingChecker.verify(reply, facts, _NO_SAFE_LITERALS)
    assert bad.literal == "3"
    assert bad.literal_class == "number"


# --------------------------------------------------------------------------------------
# Identifiers and status words
# --------------------------------------------------------------------------------------


def test_an_unknown_identifier_is_rejected() -> None:
    facts = _facts("u_002", "get_invoices")
    (bad,) = GroundingChecker.verify(
        "Please see invoice inv_999 for details.", facts, _NO_SAFE_LITERALS
    )
    assert bad.literal == "inv_999"
    assert bad.literal_class == "identifier"


def test_identifier_digits_are_not_re_read_as_ungrounded_numbers() -> None:
    facts = _facts("u_002", "get_invoices")
    # "204" is inside inv_204 and is not an amount; masking the id before the number
    # scan is what keeps this clean.
    reply = "Invoice inv_204 for 42.10 EUR."
    assert GroundingChecker.verify(reply, facts, _NO_SAFE_LITERALS) == []


def test_a_status_word_must_be_one_the_factset_actually_holds() -> None:
    facts = _facts("u_003", "get_invoices")  # statuses: pending, paid -- never failed
    (bad,) = GroundingChecker.verify(
        "Your payment failed.", facts, _NO_SAFE_LITERALS
    )
    assert bad.literal == "failed"
    assert bad.literal_class == "status"

    assert GroundingChecker.verify("Your invoice is pending.", facts, _NO_SAFE_LITERALS) == []


# --------------------------------------------------------------------------------------
# extract() -- what the pipeline records as literals_checked
# --------------------------------------------------------------------------------------


def test_extract_returns_one_entry_per_literal_occurrence() -> None:
    reply = "Invoice inv_204 for 42.10 EUR is paid; invoice inv_203 for 38.90 EUR is paid."
    kinds = [lit.kind for lit in GroundingChecker.extract(reply)]

    assert kinds.count("identifier") == 2      # inv_204, inv_203
    assert kinds.count("number") == 2          # 42.10, 38.90
    assert kinds.count("status") == 2          # paid, paid
    assert len(GroundingChecker.extract("Nothing factual here.")) == 0


def test_verify_checks_exactly_the_extracted_literals() -> None:
    facts = _facts("u_002", "get_invoices")
    reply = "Invoice inv_204 for 42.10 EUR has a failed payment."
    # every literal is grounded, so a clean reply yields no violations but did check some
    assert GroundingChecker.verify(reply, facts, _NO_SAFE_LITERALS) == []
    assert len(GroundingChecker.extract(reply)) >= 3
