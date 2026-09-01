"""Grounding layer 2 -- post-hoc verification of a rendered reply.

Runs on the finished text, unconditionally, whichever client produced it. Layer 1's
structural guarantee is a property of the *fake*; it disappears the moment a real model
writes prose, so the sourcing of every literal is checked again here
([ADR 0004](../../../docs/adr/0004-two-layer-grounding-enforcement.md)).

`verify` extracts the numeric literals, fixture identifiers and closed-vocabulary status
words from the reply and checks each against the `FactSet` (plus the template's own
declared static literals). Anything unaccounted for is a `Violation`; the orchestrator
turns a non-empty list into a `handed_off` / `UNGROUNDED_REPLY` and records the offending
literals as evidence (GUARDRAILS.md section 3).

What this does **not** catch is stated in GUARDRAILS.md: open-vocabulary entities (station
names) and semantic faithfulness. Extraction is numbers, ids and status words only.

Lives in its own module rather than in `grounding.py` because it imports `FactSet`
(-> `domain` -> `tracing.models` -> `guardrails.grounding`); keeping `GroundingChecker`
out of `grounding.py` is what stops that becoming an import cycle. `Violation` stays in
`grounding.py`, which imports nothing from the package.
"""

import re
from collections.abc import Collection
from decimal import Decimal, InvalidOperation
from typing import NamedTuple, Protocol

from support_assistant.guardrails.factset import FactSet
from support_assistant.guardrails.grounding import Violation

_UNSOURCED = "not present in FactSet or TEMPLATE_SAFE_LITERALS"

_IDENTIFIER = re.compile(r"\b(?:inv|sess|u)_\w+\b")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_STATUS = re.compile(r"\b(?:paid|pending|failed|completed|interrupted)\b", re.IGNORECASE)
"""Case-insensitive so a capitalised status word is still *found*; the comparison in
`verify` folds case, and the literal is recorded with the spelling the reply used."""


class Literal(NamedTuple):
    """One factual token found in a reply, and how it was extracted."""

    text: str
    """Exactly as it appeared in the reply, spelling and case included -- `Violation`
    carries this straight into the trace, which has to show what was actually written."""

    kind: str
    """`number`, `identifier`, or `status`."""


class Template(Protocol):
    """The slice of an `llm.templates.Template` the checker needs: the static literals a
    template's own prose is allowed to contain. Passed in by the orchestrator so
    `guardrails/` never imports `llm/` (ARCHITECTURE.md section 3)."""

    TEMPLATE_SAFE_LITERALS: Collection[str]


def _as_decimal(text: str) -> Decimal | None:
    try:
        return Decimal(text.replace(",", "."))
    except InvalidOperation:
        return None


class GroundingChecker:
    """Stateless. Both methods are static; construct nothing."""

    @staticmethod
    def extract(reply: str) -> list[Literal]:
        """Every factual literal in `reply`, in no particular order. Identifiers are pulled
        first and masked out before the numeric scan, so the digits inside `inv_204` are
        never re-read as an amount. `len(extract(reply))` is the `literals_checked` the
        trace records."""
        identifiers = [Literal(match, "identifier") for match in _IDENTIFIER.findall(reply)]
        masked = _IDENTIFIER.sub(" ", reply)
        numbers = [Literal(match, "number") for match in _NUMBER.findall(masked)]
        statuses = [Literal(match, "status") for match in _STATUS.findall(masked)]
        return identifiers + numbers + statuses

    @staticmethod
    def verify(reply: str, facts: FactSet, template: Template) -> list[Violation]:
        """Check every extracted literal against the facts and the template's safe list.
        Returns one `Violation` per unsourced literal; an empty list means grounded."""
        safe = set(template.TEMPLATE_SAFE_LITERALS)
        safe_numbers = {value for text in safe if (value := _as_decimal(text)) is not None}

        allowed_numbers = facts.allowed_numbers() | safe_numbers
        allowed_identifiers = facts.allowed_identifiers() | safe
        # Folded, because the status comparison below folds the literal's case too.
        allowed_statuses = facts.allowed_statuses() | {text.lower() for text in safe}

        violations: list[Violation] = []
        for literal in GroundingChecker.extract(reply):
            if literal.kind == "number":
                value = _as_decimal(literal.text)
                grounded = value is not None and value in allowed_numbers
            elif literal.kind == "identifier":
                grounded = literal.text in allowed_identifiers
            else:
                # The status vocabulary is lower-case; the reply's casing is not the
                # checker's business, only its sourcing.
                grounded = literal.text.lower() in allowed_statuses

            if not grounded:
                violations.append(
                    Violation(literal=literal.text, literal_class=literal.kind, reason=_UNSOURCED)
                )
        return violations
