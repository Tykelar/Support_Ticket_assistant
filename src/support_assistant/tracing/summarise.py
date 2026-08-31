"""Tool-result summarisation -- one rule per tool.

`tool_result` records a *summary*, never the payload: counts, the distribution over
enumerated status fields, and the identifiers the result returned. Enough to explain the
reply, never more -- the trace is served over the API and retained for audit, so copying
every field of every record into it multiplies exposure for no gain (TRACEABILITY.md,
ARCHITECTURE.md section 5).

`referenced` lists every identifier the result returned. Narrowing it to just the ids the
rendered reply cites is a deferred refinement: it only becomes meaningful once the
pipeline renders a reply (phase 7), and doing it means either a second pass or building
the `tool_result` step at persist time rather than in the loop -- a call the orchestrator
will own when it exists. Until then the full list is a safe superset: a reader can still
trace any statement in the reply to a record.

`statuses` is ordered by enum declaration rather than row order, so the summary is stable
to serialise and to assert on.
"""

from collections.abc import Callable
from enum import StrEnum
from typing import Any

from support_assistant.domain import ToolResult
from support_assistant.enums import InvoiceStatus, SessionStatus


def summarise(result: ToolResult) -> dict[str, Any]:
    """Summarise one tool call's result.

    Raises `ValueError` for a tool with no summariser -- a fourth tool must add one here
    rather than silently getting an empty summary.
    """
    try:
        rule = _SUMMARISERS[result.tool]
    except KeyError:
        raise ValueError(f"no summariser for tool {result.tool!r}") from None
    return rule(result)


def _summarise_user(result: ToolResult) -> dict[str, Any]:
    user = result.records[0]
    return {"found": True, "plan": user.plan}


def _summarise_invoices(result: ToolResult) -> dict[str, Any]:
    return _summarise_collection(result, InvoiceStatus, id_of=lambda inv: inv.invoice_id)


def _summarise_sessions(result: ToolResult) -> dict[str, Any]:
    return _summarise_collection(result, SessionStatus, id_of=lambda s: s.session_id)


def _summarise_collection(
    result: ToolResult,
    status_enum: type[StrEnum],
    *,
    id_of: Callable[[Any], str],
) -> dict[str, Any]:
    counts: dict[Any, int] = {}
    for record in result.records:
        counts[record.status] = counts.get(record.status, 0) + 1
    statuses = {member.value: counts[member] for member in status_enum if member in counts}

    return {
        "count": len(result.records),
        "statuses": statuses,
        "referenced": [id_of(record) for record in result.records],
    }


_SUMMARISERS = {
    "get_user": _summarise_user,
    "get_invoices": _summarise_invoices,
    "get_charging_sessions": _summarise_sessions,
}
