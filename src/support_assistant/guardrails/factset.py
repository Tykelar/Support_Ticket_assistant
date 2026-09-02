"""Grounding layer 1 -- tool results projected into a typed `FactSet`.

Templates interpolate **only** from a `FactSet`, so under `FakeLLM` there is no code path
from a reply to a value no tool returned
([ADR 0004](../../../docs/adr/0004-two-layer-grounding-enforcement.md)).

The projection is deliberately lossy. It keeps names, identifiers, amounts, currencies,
status words and counts; it **drops** dates, plan tier and language. A reply must never
state those, and not carrying them is the layer-1 guarantee.

`guardrails/` may import `domain` but not `llm/` or `tools/` (ARCHITECTURE.md section 3).
"""

from collections.abc import Iterable
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from support_assistant.domain import ChargingSession, Invoice, Observation, User
from support_assistant.enums import InvoiceStatus, SessionStatus


class InvoiceFact(BaseModel):
    """One invoice, reduced to the fields a reply may cite."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    invoice_id: str
    amount: Decimal
    currency: str
    status: InvoiceStatus


class SessionFact(BaseModel):
    """One charging session, reduced to the fields a reply may cite."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str
    station: str
    kwh: Decimal
    cost: Decimal
    status: SessionStatus


class FactSet(BaseModel):
    """The complete set of facts a reply may be built from (CONTEXT.md's *fact set*).

    Frozen: projected once from the history, then read-only for the rest of the run.

    The four `allowed_*` helpers below are what `GroundingChecker.verify` compares
    against -- one per literal class, because numbers have to compare as `Decimal`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    user_name: str | None = None
    user_id: str | None = None
    invoices: tuple[InvoiceFact, ...] = ()
    sessions: tuple[SessionFact, ...] = ()

    @classmethod
    def from_observations(cls, history: Iterable[Observation]) -> "FactSet":
        """Project every `ToolResult` in the history into facts, keeping the loaders'
        newest-first order so a template can cite `sessions[0]` as the most recent."""
        user_name: str | None = None
        user_id: str | None = None
        invoices: list[InvoiceFact] = []
        sessions: list[SessionFact] = []

        for observation in history:
            for record in observation.result.records:
                if isinstance(record, User):
                    user_name = record.name
                    user_id = record.user_id
                elif isinstance(record, Invoice):
                    invoices.append(
                        InvoiceFact(
                            invoice_id=record.invoice_id,
                            amount=record.amount,
                            currency=record.currency,
                            status=record.status,
                        )
                    )
                elif isinstance(record, ChargingSession):
                    sessions.append(
                        SessionFact(
                            session_id=record.session_id,
                            station=record.station,
                            kwh=record.kwh,
                            cost=record.cost,
                            status=record.status,
                        )
                    )

        return cls(
            user_name=user_name,
            user_id=user_id,
            invoices=tuple(invoices),
            sessions=tuple(sessions),
        )

    def allowed_numbers(self) -> set[Decimal]:
        """Numeric facts as `Decimal`, so `42.10`, `42,10` and `42.1` compare equal.
        Includes the row counts ("all 3 of your invoices")."""
        numbers: set[Decimal] = set()
        for invoice in self.invoices:
            numbers.add(invoice.amount)
        for session in self.sessions:
            numbers.add(session.kwh)
            numbers.add(session.cost)
        if self.invoices:
            numbers.add(Decimal(len(self.invoices)))
        if self.sessions:
            numbers.add(Decimal(len(self.sessions)))
        return numbers

    def allowed_identifiers(self) -> set[str]:
        """Fixture identifiers a reply may cite: the user id and every invoice/session id."""
        identifiers = {invoice.invoice_id for invoice in self.invoices}
        identifiers |= {session.session_id for session in self.sessions}
        if self.user_id:
            identifiers.add(self.user_id)
        return identifiers

    def allowed_entities(self) -> set[str]:
        """The open-vocabulary strings that are nonetheless facts: station names and the
        user's name.

        Layer 2 cannot verify these (ADR 0004), but it can recognise the ones it already
        sourced and stop re-reading them as something else: a station called `A1 Norte`
        holds a digit that is not an amount. Whole spans, as the tool returned them.
        """
        entities = {session.station for session in self.sessions}
        if self.user_name:
            entities.add(self.user_name)
        return entities

    def allowed_statuses(self) -> set[str]:
        """The status words actually present -- checked as an exact, closed vocabulary."""
        statuses = {invoice.status.value for invoice in self.invoices}
        statuses |= {session.status.value for session in self.sessions}
        return statuses
