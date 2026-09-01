"""`GroundingChecker` -- grounding layer 2: post-hoc verification of a rendered reply.

Reserved by TESTS.md ("the checker"). The load-bearing case is
`test_ungrounded_literal_is_caught`: a deliberately doctored template that injects an
amount no tool returned must be flagged, with the offending literal recorded. That is the
evidence the brief's "one unforgivable bug" cannot ship (GUARDRAILS.md section 3, ADR
0004). The full `handed_off` / `UNGROUNDED_REPLY` outcome is asserted end to end in the
pipeline phase; here the checker is exercised directly.
"""

from decimal import Decimal

import pytest

from support_assistant.domain import Observation, ToolCall
from support_assistant.enums import InvoiceStatus, ReplyTemplate, SessionStatus
from support_assistant.guardrails.factset import FactSet, SessionFact
from support_assistant.guardrails.grounding import GroundingChecker
from support_assistant.llm import templates
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
    # A real Template -- the production dataclass, the real field selection, the real
    # safe-literal set -- with one amount doctored into its prose. Rendered through the
    # same code path render() uses, so the reply under test is a genuinely rendered one.
    facts = _facts("u_002", "get_invoices")  # amounts 42.10, 38.90, 31.20 -- never 99.00
    honest = templates.spec_for(ReplyTemplate.BILLING_FAILED)
    doctored = templates.Template(
        name=honest.name,
        body="Hi {name}, invoice {invoice_id} for 99.00 {currency} has a failed payment.",
        context=honest.context,
        TEMPLATE_SAFE_LITERALS=honest.TEMPLATE_SAFE_LITERALS,
    )

    reply = doctored.render(facts)
    violations = GroundingChecker.verify(reply, facts, doctored)

    assert "99.00" in reply  # the doctored amount really did reach the reply
    assert len(violations) == 1
    (bad,) = violations
    assert bad.literal == "99.00"
    assert bad.literal_class == "number"
    assert "FactSet" in bad.reason


def test_the_honest_version_of_that_template_is_clean() -> None:
    # The control: same facts, same template, undoctored -- so the test above is failing
    # on the injected amount and not on something incidental in the prose.
    facts = _facts("u_002", "get_invoices")
    honest = templates.spec_for(ReplyTemplate.BILLING_FAILED)
    assert GroundingChecker.verify(honest.render(facts), facts, honest) == []


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


def test_a_violation_records_the_literal_as_it_was_written() -> None:
    # Violation.literal is "the offending text exactly as it appeared" -- the trace is the
    # audit record, so it must show what the reply said, not a normalised form of it.
    facts = _facts("u_001", "get_invoices")  # every invoice paid -- "failed" is not a fact
    (bad,) = GroundingChecker.verify("Your payment Failed.", facts, _NO_SAFE_LITERALS)
    assert bad.literal == "Failed"
    assert bad.literal_class == "status"


def test_a_grounded_status_matches_whatever_its_case() -> None:
    # Case-insensitive matching, not case-sensitive rejection: a capitalised status word
    # that *is* in the FactSet is grounded.
    facts = _facts("u_001", "get_invoices")
    assert GroundingChecker.verify("Your invoice is Paid.", facts, _NO_SAFE_LITERALS) == []


# --------------------------------------------------------------------------------------
# extract() -- what the pipeline records as literals_checked
# --------------------------------------------------------------------------------------


def test_extract_returns_one_entry_per_literal_occurrence() -> None:
    facts = _facts("u_002", "get_invoices")
    reply = "Invoice inv_204 for 42.10 EUR is paid; invoice inv_203 for 38.90 EUR is paid."
    kinds = [lit.kind for lit in GroundingChecker.extract(reply, facts)]

    assert kinds.count("identifier") == 2      # inv_204, inv_203
    assert kinds.count("number") == 2          # 42.10, 38.90
    assert kinds.count("status") == 2          # paid, paid
    assert len(GroundingChecker.extract("Nothing factual here.", facts)) == 0


def test_verify_checks_exactly_the_extracted_literals() -> None:
    facts = _facts("u_002", "get_invoices")
    reply = "Invoice inv_204 for 42.10 EUR has a failed payment."
    # every literal is grounded, so a clean reply yields no violations but did check some
    assert GroundingChecker.verify(reply, facts, _NO_SAFE_LITERALS) == []
    assert len(GroundingChecker.extract(reply, facts)) >= 3


# --------------------------------------------------------------------------------------
# Sourced entity text is not re-scanned
# --------------------------------------------------------------------------------------


def _session_facts(station: str) -> FactSet:
    return FactSet(
        user_name="Ana Ribeiro",
        user_id="u_001",
        sessions=(
            SessionFact(
                session_id="sess_1001",
                station=station,
                kwh=Decimal("4.00"),
                cost=Decimal("1.60"),
                status=SessionStatus.COMPLETED,
            ),
        ),
    )


@pytest.mark.parametrize("station", ["A1 Norte", "Porto Norte 2", "Lyon 7 Confluence"])
def test_a_station_name_containing_a_digit_is_not_read_as_an_amount(station: str) -> None:
    # The station came from a tool result, so every character of it is sourced. Re-reading
    # its digits as an ungrounded amount would withhold an honest reply -- fail closed on
    # a fact, which is the one direction "fail closed" is not supposed to cover.
    facts = _session_facts(station)
    reply = f"Your session at {station} delivered 4.00 kWh, charged at 1.60."
    assert GroundingChecker.verify(reply, facts, _NO_SAFE_LITERALS) == []


@pytest.mark.parametrize("station", ["Completed Street", "Failed Bridge Park"])
def test_a_station_name_colliding_with_the_status_vocabulary_is_not_a_status(
    station: str,
) -> None:
    facts = _session_facts(station)
    reply = f"Your session at {station} is completed."
    assert GroundingChecker.verify(reply, facts, _NO_SAFE_LITERALS) == []


def test_an_entity_the_factset_does_not_hold_is_still_scanned() -> None:
    # The guard against over-masking: masking applies to sourced strings only, so an
    # invented station's digits still fail closed.
    facts = _session_facts("Porto Norte 2")
    (bad,) = GroundingChecker.verify(
        "Your session at Berlin Mitte 9 completed.", facts, _NO_SAFE_LITERALS
    )
    assert bad.literal == "9"
    assert bad.literal_class == "number"


def test_masking_an_entity_does_not_hide_an_identifier_inside_it() -> None:
    # Identifiers are pulled before any masking, so an unsourced id in the same sentence
    # as a sourced station is still caught.
    facts = _session_facts("Porto Norte 2")
    (bad,) = GroundingChecker.verify(
        "Your session at Porto Norte 2 is on invoice inv_999.", facts, _NO_SAFE_LITERALS
    )
    assert bad.literal == "inv_999"


# --------------------------------------------------------------------------------------
# TEMPLATE_SAFE_LITERALS is a numbers allowlist, and only that
# --------------------------------------------------------------------------------------


def test_a_safe_literal_does_not_whitelist_a_status_word() -> None:
    # LLM.md: "Any *number* a template states in its own static prose". A template must
    # not be able to switch off status grounding by naming a word in its safe list.
    facts = _facts("u_001", "get_invoices")  # every invoice paid -- "failed" is not a fact
    (bad,) = GroundingChecker.verify("Your payment failed.", facts, _Template("failed"))
    assert bad.literal == "failed"
    assert bad.literal_class == "status"


def test_a_safe_literal_does_not_whitelist_an_identifier() -> None:
    facts = _facts("u_001", "get_invoices")
    (bad,) = GroundingChecker.verify("See invoice inv_999.", facts, _Template("inv_999"))
    assert bad.literal == "inv_999"
    assert bad.literal_class == "identifier"


# --------------------------------------------------------------------------------------
# The status vocabulary is the enums', not a second copy of them
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [*InvoiceStatus, *SessionStatus], ids=lambda s: s.value)
def test_every_enumerated_status_word_is_extractable(status: str) -> None:
    # enums.py: the vocabularies are closed "so that status words enter the FactSet as
    # facts and grounding layer 2 can check them". A member the extractor cannot see is a
    # word no reply is ever checked for -- silently unchecked, not loudly broken.
    facts = FactSet(user_name="Ana Ribeiro", user_id="u_001")
    extracted = GroundingChecker.extract(f"The state is {status.value}.", facts)
    assert [lit.text for lit in extracted if lit.kind == "status"] == [status.value]
