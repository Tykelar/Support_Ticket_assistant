"""The typed failures the three tools raise.

One exception per failure mode. The distinction between the first two is what lets the
orchestrator pick the right `HandoffReason` (ADR 0005): `UserNotFound` -> `USER_NOT_FOUND`,
`NoDataAvailable` -> `DATA_NOT_FOUND`, `ToolExecutionError` (and any other exception) ->
`TOOL_ERROR`. Tools raise; they never name a reason themselves -- the mapping is the
orchestrator's, in a later phase.

These are **not** `tracing.models.ToolError`. That model records *any* exception that
reached the tool call site, `NoDataAvailable` and `UserNotFound` included; these three
*are* exceptions. Naming it after one of them would be worse than the near-collision, so
neither gets renamed -- TRACEABILITY.md says this once, where `ToolError` is defined.
"""

from pydantic import ValidationError


class UserNotFound(Exception):
    """No fixture record for that `user_id`. Raised by all three tools -- the collection
    tools check the user before their own data, so the reason is right regardless of which
    tool the loop reaches first."""


class NoDataAvailable(Exception):
    """The user exists, but has no rows of the requested kind.

    Collection tools only, and never returned as `[]`: zero rows is ambiguous -- genuinely
    none, not yet synced, or a broken join upstream -- and a confident "you have no
    invoices" built on the last two would be a wrong statement about a customer's billing
    (ADR 0009).
    """


class ToolExecutionError(Exception):
    """The fixture is malformed, unreadable, or fails validation.

    Also raised by the registry for an unregistered tool name and for arguments that fail
    the tool's schema. The message carries a locator, not the offending value
    (`invoice inv_601 for u_006 failed validation: amount`): it reaches the trace, which is
    served over the API. The full `ValidationError` is chained as `__cause__` for the
    structured log, whose audience is a developer rather than a support agent.
    """


def failed_fields(exc: ValidationError) -> str:
    """The dotted paths of the fields that failed, comma-joined -- the tail of a
    `ToolExecutionError` message (`... failed validation: amount`). Never the offending
    value: that stays in the chained `ValidationError`, bound for the log, not the trace."""
    return ", ".join(
        ".".join(str(part) for part in error["loc"]) for error in exc.errors()
    )
