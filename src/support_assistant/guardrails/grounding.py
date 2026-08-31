"""`Violation` -- the evidence a failed grounding check records.

This module holds only `Violation` and imports nothing from the package: `tracing.models`
imports it for the `grounding_check` trace step, and `domain` imports `tracing.models`, so
anything here that reached back into `domain` would close an import cycle.

`GroundingChecker` and the literal-extraction rules live in `checker.py` for exactly that
reason -- they need `FactSet`, which is downstream of `domain` (GUARDRAILS.md section 3).
"""

from pydantic import BaseModel, ConfigDict, Field


class Violation(BaseModel):
    """One literal in a reply that no tool result accounts for.

    Recorded in the trace so a reader can see *which* claim was unsourced, not merely
    that the reply was withheld. Serialised with `class` as the key, matching the trace
    JSON in TRACEABILITY.md; the field is renamed here only because `class` is a Python
    keyword.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    literal: str
    """The offending text exactly as it appeared in the reply."""

    literal_class: str = Field(alias="class")
    """How the literal was extracted: `number`, `identifier`, or `status`."""

    reason: str
    """Why it failed -- typically absent from both the FactSet and the template's
    declared safe literals."""
