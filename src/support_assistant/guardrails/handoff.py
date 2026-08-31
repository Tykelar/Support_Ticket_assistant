"""Handoff reasons.

The closed set of explanations for why a ticket went to a human. Every handoff carries
exactly one, and each is produced at exactly one place in the orchestrator -- which is
what makes handoff-rate-by-reason a trustworthy signal rather than a rough grouping
(GUARDRAILS.md, OBSERVABILITY.md).

The typed failures the tools raise, and the mapping from those to these reasons, arrive
with the tools and the orchestrator. This module holds only the vocabulary.
"""

from enum import StrEnum


class HandoffReason(StrEnum):
    """Why a ticket was handed to a human. See ARCHITECTURE.md section 4."""

    USER_NOT_FOUND = "USER_NOT_FOUND"
    """`get_user` found no record for the ticket's user_id."""

    DATA_NOT_FOUND = "DATA_NOT_FOUND"
    """The user exists, but the data the intent needs is absent (ADR 0009)."""

    UNSUPPORTED_INTENT = "UNSUPPORTED_INTENT"
    """Intent classified `unknown`, including a keyword tie."""

    TOOL_ERROR = "TOOL_ERROR"
    """A tool raised, or any unanticipated exception in the run."""

    ITERATION_CAP_EXCEEDED = "ITERATION_CAP_EXCEEDED"
    """The loop hit MAX_ITERATIONS without terminating."""

    UNGROUNDED_REPLY = "UNGROUNDED_REPLY"
    """The grounding checker found a literal no tool result accounts for."""
