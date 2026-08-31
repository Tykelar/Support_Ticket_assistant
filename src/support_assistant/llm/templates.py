"""The five reply bodies, `render()`, and each template's `TEMPLATE_SAFE_LITERALS`.

A `Reply` from the model names a `ReplyTemplate` only (LLM.md). The orchestrator projects
a `FactSet` from the history and calls `render(template, facts)`; every value in the
finished text comes from that `FactSet` -- grounding layer 1
([ADR 0004](../../../docs/adr/0004-two-layer-grounding-enforcement.md)).

`TEMPLATE_SAFE_LITERALS` is the small, per-template allowlist for numbers that live in a
body's *static prose* ("within 3 business days") and so will never be in a `FactSet`.
Adding a number to prose means adding it here -- a visible, reviewable act (GUARDRAILS.md).

The `FactSet` type is imported under `TYPE_CHECKING` only: `render` needs its shape for
type-checking but `llm/` and `guardrails/` know nothing about each other at runtime
(ARCHITECTURE.md section 3). The orchestrator, which imports both, wires them together.
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
    static literals its prose is allowed to contain. `spec_for` returns one of these; the
    orchestrator hands it to `GroundingChecker.verify`."""

    name: ReplyTemplate
    body: str
    context: Callable[[FactSet], dict[str, str]]
    TEMPLATE_SAFE_LITERALS: frozenset[str]


def render(template: ReplyTemplate, facts: FactSet) -> str:
    """The reply text for `template`, every field filled from `facts`."""
    spec = _TEMPLATES[template]
    return spec.body.format(**spec.context(facts))


def spec_for(template: ReplyTemplate) -> Template:
    """The `Template` for a `ReplyTemplate`, for the grounding check's `template` argument."""
    return _TEMPLATES[template]


# --------------------------------------------------------------------------------------
# Field selection -- which record each template speaks about
# --------------------------------------------------------------------------------------


def _first(invoices: tuple[InvoiceFact, ...], status: InvoiceStatus) -> InvoiceFact:
    return next(invoice for invoice in invoices if invoice.status is status)


def _name_only(facts: FactSet) -> dict[str, str]:
    return {"name": facts.user_name or ""}


def _failed_invoice(facts: FactSet) -> dict[str, str]:
    invoice = _first(facts.invoices, InvoiceStatus.FAILED)
    return {
        "name": facts.user_name or "",
        "invoice_id": invoice.invoice_id,
        "amount": format_amount(invoice.amount),
        "currency": invoice.currency,
    }


def _pending_invoice(facts: FactSet) -> dict[str, str]:
    invoice = _first(facts.invoices, InvoiceStatus.PENDING)
    return {
        "name": facts.user_name or "",
        "invoice_id": invoice.invoice_id,
        "amount": format_amount(invoice.amount),
        "currency": invoice.currency,
    }


def _latest_session(facts: FactSet) -> dict[str, str]:
    session = facts.sessions[0]  # loaders return newest-first
    return {
        "name": facts.user_name or "",
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
