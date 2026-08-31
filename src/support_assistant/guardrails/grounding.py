"""Grounding layer 2 -- post-hoc verification of a rendered reply.

This module currently holds only `Violation`, the evidence a failed check records. The
`GroundingChecker` and the literal extraction rules arrive with the guardrails phase;
the model lands here first because the `grounding_check` trace step carries it
(TRACEABILITY.md).
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
