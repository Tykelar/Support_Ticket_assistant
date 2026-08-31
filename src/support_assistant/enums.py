"""The enumerations the whole system agrees on (ARCHITECTURE.md section 4).

These sit below everything else on purpose. A `Ticket` carries its trace, and a trace
step names an `Intent` and a `TicketStatus` -- so if the enums lived with the ticket,
`domain` and `tracing.models` would import each other. They are the one layer both can
depend on.

Import them from `support_assistant.domain`, which re-exports them as part of the domain
vocabulary; this module exists to fix the layering, not to be a second front door.
"""

from enum import StrEnum


class Intent(StrEnum):
    """The kind of question a ticket is asking, as classified by the pipeline.

    Exactly the brief's categories. `unknown` is a handoff trigger, not a category to
    serve: guessing between two templates is the confident-and-wrong behaviour the
    system exists to avoid.
    """

    BILLING_QUESTION = "billing_question"
    CHARGING_SESSION_PROBLEM = "charging_session_problem"
    UNKNOWN = "unknown"


class TicketStatus(StrEnum):
    """`processing` -> (`replied` | `handed_off`). Both terminal states are final."""

    PROCESSING = "processing"
    REPLIED = "replied"
    HANDED_OFF = "handed_off"


class SessionStatus(StrEnum):
    """How a charging session ended.

    A closed vocabulary rather than free text, so status words enter the FactSet as facts
    and grounding layer 2 can check them (ADR 0004).
    """

    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"


class InvoiceStatus(StrEnum):
    """Where an invoice stands. Closed for the same reason as `SessionStatus`."""

    PAID = "paid"
    PENDING = "pending"
    FAILED = "failed"
