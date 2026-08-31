"""Tool-result summarisation -- one rule per tool.

`tool_result` records a *summary*, never the payload: counts, the distribution over
enumerated status fields, and the identifiers the reply used. Enough to explain the
reply, never more -- the trace is served over the API and retained for audit, so copying
every field of every record into it multiplies exposure for no gain (TRACEABILITY.md,
ARCHITECTURE.md section 5).

`referenced` is the loose end: in the agentic loop `summarise` runs before any reply
exists, so it cannot yet know which identifiers a reply will cite. Called with
`referenced=None` it lists every identifier the result returned; once a reply is
rendered the orchestrator calls it again with the ids that reply actually used, and the
list narrows to those. `statuses` is ordered by enum declaration rather than row order,
so the summary is stable to serialise and to assert on.
"""

from collections.abc import Iterable
from typing import Any

from support_assistant.domain import ToolResult
from support_assistant.enums import InvoiceStatus, SessionStatus


def summarise(
    result: ToolResult, referenced: Iterable[str] | None = None
) -> dict[str, Any]:
    """Summarise one tool call's result. `referenced`, when given, is the set of
    identifiers to keep in the summary's `referenced` list; omit it to keep them all.

    Raises `ValueError` for a tool with no summariser -- a fourth tool must add one here
    rather than silently getting an empty summary.
    """
    try:
        rule = _SUMMARISERS[result.tool]
    except KeyError:
        raise ValueError(f"no summariser for tool {result.tool!r}") from None
    return rule(result, referenced)


def _summarise_user(result: ToolResult, referenced: Iterable[str] | None) -> dict[str, Any]:
    user = result.records[0]
    return {"found": True, "plan": user.plan}


def _summarise_invoices(
    result: ToolResult, referenced: Iterable[str] | None
) -> dict[str, Any]:
    return _collection(
        result, InvoiceStatus, id_of=lambda inv: inv.invoice_id, referenced=referenced
    )


def _summarise_sessions(
    result: ToolResult, referenced: Iterable[str] | None
) -> dict[str, Any]:
    return _collection(
        result, SessionStatus, id_of=lambda sess: sess.session_id, referenced=referenced
    )


def _collection(
    result: ToolResult,
    status_enum: type,
    *,
    id_of: Any,
    referenced: Iterable[str] | None,
) -> dict[str, Any]:
    counts: dict[Any, int] = {}
    for record in result.records:
        counts[record.status] = counts.get(record.status, 0) + 1
    statuses = {member.value: counts[member] for member in status_enum if member in counts}

    ids = [id_of(record) for record in result.records]
    if referenced is not None:
        keep = set(referenced)
        ids = [identifier for identifier in ids if identifier in keep]

    return {"count": len(result.records), "statuses": statuses, "referenced": ids}


_SUMMARISERS = {
    "get_user": _summarise_user,
    "get_invoices": _summarise_invoices,
    "get_charging_sessions": _summarise_sessions,
}
