"""The typed failures the three tools raise.

One exception per failure mode, which is what lets the orchestrator pick the right
`HandoffReason` (ADR 0005): `UserNotFound` -> `USER_NOT_FOUND`, `NoDataAvailable` ->
`DATA_NOT_FOUND`, `ToolExecutionError` and anything else -> `TOOL_ERROR`. Tools raise;
they never name a reason themselves.

Not to be confused with `tracing.models.ToolError`, which is the *record* of any exception
that reached the call site.
"""

from pydantic import ValidationError


class UserNotFound(Exception):
    """No fixture record for that `user_id`. Raised by all three tools: the collection
    tools check the user first, so the reason is right whichever tool the loop reaches."""


class NoDataAvailable(Exception):
    """The user exists, but has no rows of the requested kind.

    Collection tools only, and never returned as `[]`: zero rows is ambiguous -- genuinely
    none, not yet synced, or a broken join -- and "you have no invoices" built on the last
    two would be a wrong statement about a customer's billing (ADR 0009).
    """


class ToolExecutionError(Exception):
    """The fixture is malformed, unreadable, or fails validation. Also raised by the
    registry for an unregistered name or bad arguments.

    The message carries a locator, never the offending value
    (`invoice inv_601 for u_006 failed validation: amount`), because it reaches the trace,
    which is served over the API. The `ValidationError` is chained for the log.
    """


def failed_fields(exc: ValidationError) -> str:
    """The dotted paths of the fields that failed, comma-joined. Never the offending
    value: that stays in the chained `ValidationError`, bound for the log."""
    return ", ".join(
        ".".join(str(part) for part in error["loc"]) for error in exc.errors()
    )
