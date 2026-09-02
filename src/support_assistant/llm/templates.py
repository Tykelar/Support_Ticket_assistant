"""The five reply bodies, `spec_for()`, and each template's `TEMPLATE_SAFE_LITERALS`.

A `Reply` from the model names a `ReplyTemplate` only. The orchestrator projects a
`FactSet`, resolves the spec and renders it, so every value in the finished text comes from
that `FactSet` -- grounding layer 1
([ADR 0004](../../../docs/adr/0004-two-layer-grounding-enforcement.md)).

`TEMPLATE_SAFE_LITERALS` is the per-template allowlist for numbers in a body's static prose
("within 3 business days"), which are never in a `FactSet`. Adding a number to prose means
adding it here -- a visible, reviewable act (GUARDRAILS.md).

`FactSet` is imported under `TYPE_CHECKING` only: `llm/` and `guardrails/` know nothing
about each other at runtime (ARCHITECTURE.md section 3).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from support_assistant.enums import InvoiceStatus, ReplyTemplate

if TYPE_CHECKING:
    from support_assistant.guardrails.factset import FactSet, InvoiceFact

from support_assistant.domain import format_amount


@dataclass(frozen=True)
class Template:
    """One reply template: its prose, how it pulls its fields from a `FactSet`, and the
    static literals its prose may contain. The orchestrator hands it to
    `GroundingChecker.verify`."""

    name: ReplyTemplate
    body: str
    context: Callable[[FactSet], dict[str, str]]
    TEMPLATE_SAFE_LITERALS: frozenset[str]

    def render(self, facts: FactSet) -> str:
        """This template's prose, every field filled from `facts`.

        A method on the spec rather than a lookup taking a `ReplyTemplate`, so the
        doctored-template test renders a deliberately wrong `Template` through the same
        code path a real one takes (TESTS.md).

        Raises `ValueError` if `facts` cannot fill the template. `FakeLLM` cannot ask for
        that, since it picks the template from these records; a real model names any of the
        five, and naming the missing fact beats a bare `StopIteration` in the trace.
        """
        return self.body.format(**self.context(facts))


def spec_for(template: ReplyTemplate) -> Template:
    """The `Template` for a `ReplyTemplate`. The same spec has to both render and be
    checked, so there is deliberately no `render(template, facts)` shortcut beside this:
    the orchestrator has to hold the spec either way (ADR 0004).
    """
    return _TEMPLATES[template]


# --------------------------------------------------------------------------------------
# Field selection -- which record each template speaks about.
#
# Each selector raises `ValueError` naming the fact it wanted rather than letting a bare
# `StopIteration` or `IndexError` out. Both fail closed to a TOOL_ERROR handoff, but only
# one tells the support agent reading the trace what was missing.
# --------------------------------------------------------------------------------------


def _name(facts: FactSet) -> str:
    """The name is a fact and must be sourced like any other, so a missing one is a
    refusal rather than an empty greeting."""
    if not facts.user_name:
        raise ValueError("a reply template needs the user's name, but the FactSet has none")
    return facts.user_name


def _first(invoices: tuple[InvoiceFact, ...], status: InvoiceStatus) -> InvoiceFact:
    for invoice in invoices:
        if invoice.status is status:
            return invoice
    raise ValueError(
        f"a reply template needs an invoice with status {status.value}, "
        f"but the FactSet has none"
    )


def _name_only(facts: FactSet) -> dict[str, str]:
    return {"name": _name(facts)}


def _invoice_with(facts: FactSet, status: InvoiceStatus) -> dict[str, str]:
    invoice = _first(facts.invoices, status)
    return {
        "name": _name(facts),
        "invoice_id": invoice.invoice_id,
        "amount": format_amount(invoice.amount),
        "currency": invoice.currency,
    }


def _failed_invoice(facts: FactSet) -> dict[str, str]:
    return _invoice_with(facts, InvoiceStatus.FAILED)


def _pending_invoice(facts: FactSet) -> dict[str, str]:
    return _invoice_with(facts, InvoiceStatus.PENDING)


def _latest_session(facts: FactSet) -> dict[str, str]:
    if not facts.sessions:
        raise ValueError("a reply template needs a charging session, but the FactSet has none")
    session = facts.sessions[0]  # loaders return newest-first
    return {
        "name": _name(facts),
        "station": session.station,
        "status": session.status.value,
        "kwh": format_amount(session.kwh),
        "cost": format_amount(session.cost),
    }


# --------------------------------------------------------------------------------------
# The bodies. Every interpolated value comes from the FactSet; the only static number in
# the whole surface is the "3" in billing_pending, declared below.
# --------------------------------------------------------------------------------------

_BILLING_ALL_PAID = """\
Hi {name},

Thanks for getting in touch. I've checked your account and every invoice on it is paid, \
with nothing outstanding.

If something still looks wrong, reply to this message and we'll take another look.

Best regards,
Support
"""

_BILLING_FAILED = """\
Hi {name},

Thanks for getting in touch. Invoice {invoice_id} for {amount} {currency} has a failed \
payment, so that amount is still outstanding.

You can retry it from the billing section of the app. If it keeps failing, reply here \
and we'll help sort it out.

Best regards,
Support
"""

_BILLING_PENDING = """\
Hi {name},

Thanks for getting in touch. Invoice {invoice_id} for {amount} {currency} is still \
pending and has not been collected yet.

Pending invoices are normally settled automatically within 3 business days. If it is \
still pending after that, reply here and we'll take a look.

Best regards,
Support
"""

_SESSION_COMPLETED = """\
Hi {name},

Thanks for getting in touch. Your most recent charging session at {station} completed \
normally: it delivered {kwh} kWh, charged at {cost}.

If your app shows something different, reply here with the details and we'll investigate.

Best regards,
Support
"""

_SESSION_INTERRUPTED = """\
Hi {name},

Thanks for getting in touch. Your most recent charging session at {station} ended early \
with a status of {status}, after delivering {kwh} kWh.

You were charged {cost} for the energy actually delivered. If that does not look right, \
reply here and we'll review the session and correct any overcharge.

Best regards,
Support
"""


_TEMPLATES: dict[ReplyTemplate, Template] = {
    ReplyTemplate.BILLING_ALL_PAID: Template(
        ReplyTemplate.BILLING_ALL_PAID, _BILLING_ALL_PAID, _name_only, frozenset()
    ),
    ReplyTemplate.BILLING_FAILED: Template(
        ReplyTemplate.BILLING_FAILED, _BILLING_FAILED, _failed_invoice, frozenset()
    ),
    ReplyTemplate.BILLING_PENDING: Template(
        ReplyTemplate.BILLING_PENDING, _BILLING_PENDING, _pending_invoice, frozenset({"3"})
    ),
    ReplyTemplate.SESSION_COMPLETED: Template(
        ReplyTemplate.SESSION_COMPLETED, _SESSION_COMPLETED, _latest_session, frozenset()
    ),
    ReplyTemplate.SESSION_INTERRUPTED: Template(
        ReplyTemplate.SESSION_INTERRUPTED, _SESSION_INTERRUPTED, _latest_session, frozenset()
    ),
}
