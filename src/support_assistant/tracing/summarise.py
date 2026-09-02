"""Tool-result summarisation -- one rule per tool.

`tool_result` records a *summary*, never the payload: counts, the status distribution, and
the identifiers returned. Enough to explain the reply, never more -- the trace is served
over the API, so copying every field of every record multiplies exposure for no gain
(TRACEABILITY.md).

`referenced` lists every identifier the result returned, which is a safe superset of the
ones the reply cites
([roadmap](../../../docs/ROADMAP.md#narrowing-a-traces-referenced-ids)).

`statuses` is ordered by enum declaration rather than row order, so the summary is stable
to serialise and to assert on.

Every registered tool needs a rule here;
`test_registry.py::test_every_registered_tool_is_summarised_and_projected` is what makes a
missing one a test failure rather than a runtime handoff.
"""

from collections.abc import Callable
from enum import StrEnum
from typing import Any

from support_assistant.domain import ChargingSession, Invoice, ToolResult, User
from support_assistant.enums import InvoiceStatus, SessionStatus


def summarise(result: ToolResult) -> dict[str, Any]:
    """Summarise one tool call's result.

    Raises `ValueError` for a tool with no rule, rather than returning an empty summary.
    """
    try:
        rule = _SUMMARISERS[result.tool]
    except KeyError:
        raise ValueError(f"no summariser for tool {result.tool!r}") from None
    return rule(result)


def _summarise_user(result: ToolResult) -> dict[str, Any]:
    user = result.records[0]
    if not isinstance(user, User):
        raise ValueError(f"get_user returned a {type(user).__name__}, not a User")
    return {"found": True, "plan": user.plan}


def _summarise_invoices(result: ToolResult) -> dict[str, Any]:
    return _summarise_collection(result, InvoiceStatus, id_of=lambda inv: inv.invoice_id)


def _summarise_sessions(result: ToolResult) -> dict[str, Any]:
    return _summarise_collection(result, SessionStatus, id_of=lambda s: s.session_id)


def _summarise_collection(
    result: ToolResult,
    status_enum: type[StrEnum],
    *,
    id_of: Callable[[ChargingSession | Invoice], str],
) -> dict[str, Any]:
    records = [r for r in result.records if isinstance(r, ChargingSession | Invoice)]
    counts: dict[StrEnum, int] = {}
    for record in records:
        counts[record.status] = counts.get(record.status, 0) + 1
    statuses = {member.value: counts[member] for member in status_enum if member in counts}

    return {
        "count": len(records),
        "statuses": statuses,
        "referenced": [id_of(record) for record in records],
    }


_SUMMARISERS = {
    "get_user": _summarise_user,
    "get_invoices": _summarise_invoices,
    "get_charging_sessions": _summarise_sessions,
}
