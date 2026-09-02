"""Grounding layer 2 -- post-hoc verification of a rendered reply.

Runs on the finished text, unconditionally, whichever client produced it. Layer 1's
guarantee is a property of the *fake* and disappears the moment a real model writes prose
([ADR 0004](../../../docs/adr/0004-two-layer-grounding-enforcement.md)).

`verify` extracts numeric literals, fixture identifiers and status words from the reply
and checks each against the `FactSet` plus the template's declared static numbers. Anything
unaccounted for is a `Violation`, which the orchestrator turns into a
`handed_off` / `UNGROUNDED_REPLY`.

It returns the literals it checked alongside the violations, so the trace's
`literals_checked` comes from the pass that did the checking rather than a second reading.

What it does **not** catch (GUARDRAILS.md): invented open-vocabulary entities, and
semantic faithfulness. Extraction is numbers, ids and status words only.
"""

import re
from collections.abc import Collection, Iterable
from decimal import Decimal, InvalidOperation
from typing import NamedTuple, Protocol

from support_assistant.enums import InvoiceStatus, LiteralClass, SessionStatus
from support_assistant.guardrails.factset import FactSet
from support_assistant.tracing.models import Violation

_UNSOURCED = "not present in FactSet or TEMPLATE_SAFE_LITERALS"

_IDENTIFIER = re.compile(r"\b(?:inv|sess|u)_\w+\b")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


def _alternation(words: Iterable[str]) -> str:
    """Longest first, so `interrupted` is never half-matched by a shorter member."""
    return "|".join(re.escape(word) for word in sorted(set(words), key=len, reverse=True))


_STATUS = re.compile(
    r"\b(?:" + _alternation(s.value for s in (*InvoiceStatus, *SessionStatus)) + r")\b",
    re.IGNORECASE,
)
"""Built from the enums rather than retyped: a member added there must not become a word
no reply is ever checked for. Case-insensitive so a capitalised status word is still
found; `verify` folds case, and records the literal with the reply's spelling."""


class Literal(NamedTuple):
    """One factual token found in a reply, and how it was extracted."""

    text: str
    """Exactly as it appeared in the reply. `Violation` carries this into the trace, which
    has to show what was actually written."""

    kind: LiteralClass


class GroundingResult(NamedTuple):
    """What one `verify` pass found: what it looked at, and what was wrong with it."""

    literals: list[Literal]
    """Every literal the pass extracted and checked. The orchestrator records
    `len(literals)` as the trace's `literals_checked`."""

    violations: list[Violation]
    """One per unsourced literal. Empty means grounded."""


class Template(Protocol):
    """The slice of an `llm.templates.Template` the checker needs. Passed in by the
    orchestrator so `guardrails/` never imports `llm/` (ARCHITECTURE.md section 3)."""

    TEMPLATE_SAFE_LITERALS: Collection[str]


def _as_decimal(text: str) -> Decimal | None:
    try:
        return Decimal(text.replace(",", "."))
    except InvalidOperation:
        return None


def _mask(text: str, spans: Collection[str]) -> str:
    """Blank out `spans`, longest first so a nested one cannot leave a fragment behind."""
    if not spans:
        return text
    return re.compile(_alternation(spans)).sub(" ", text)


class GroundingChecker:
    """Stateless. Both methods are static; construct nothing."""

    @staticmethod
    def extract(reply: str, facts: FactSet) -> list[Literal]:
        """Every factual literal in `reply`, in no particular order.

        Two things are masked before the numeric and status scans: identifiers, so the
        digits inside `inv_204` are not re-read as an amount, and the `FactSet`'s entity
        strings, so a station called `A1 Norte` is sourced text rather than an ungrounded
        `1`. Only strings the facts actually hold are masked, so nothing unsourced escapes.
        """
        identifiers = [
            Literal(match, LiteralClass.IDENTIFIER) for match in _IDENTIFIER.findall(reply)
        ]
        masked = _mask(_IDENTIFIER.sub(" ", reply), facts.allowed_entities())
        numbers = [Literal(match, LiteralClass.NUMBER) for match in _NUMBER.findall(masked)]
        statuses = [Literal(match, LiteralClass.STATUS) for match in _STATUS.findall(masked)]
        return identifiers + numbers + statuses

    @staticmethod
    def verify(reply: str, facts: FactSet, template: Template) -> GroundingResult:
        """Check every extracted literal against the facts and the template's safe list.

        Returns the literals it checked and one `Violation` per unsourced one; no
        violations means grounded. Scanned once, so the count and the verdict describe the
        same reading.
        """
        safe_numbers = {
            value
            for text in template.TEMPLATE_SAFE_LITERALS
            if (value := _as_decimal(text)) is not None
        }
        # Numbers only. Letting the same list satisfy an identifier or a status word
        # would let a template switch off half the check (LLM.md).
        allowed_numbers = facts.allowed_numbers() | safe_numbers
        allowed_identifiers = facts.allowed_identifiers()
        allowed_statuses = facts.allowed_statuses()

        literals = GroundingChecker.extract(reply, facts)
        violations: list[Violation] = []
        for literal in literals:
            match literal.kind:
                case LiteralClass.NUMBER:
                    value = _as_decimal(literal.text)
                    grounded = value is not None and value in allowed_numbers
                case LiteralClass.IDENTIFIER:
                    grounded = literal.text in allowed_identifiers
                case LiteralClass.STATUS:
                    # The vocabulary is lower-case; the reply's casing is not the
                    # checker's business, only its sourcing.
                    grounded = literal.text.lower() in allowed_statuses
                case _:
                    # A class added to the enum without a rule here. Ungrounded, so a new
                    # kind of literal is withheld rather than left unverified.
                    grounded = False

            if not grounded:
                violations.append(
                    Violation(literal=literal.text, literal_class=literal.kind, reason=_UNSOURCED)
                )
        return GroundingResult(literals, violations)
