"""The enumerations the whole system agrees on (ARCHITECTURE.md section 4).

They sit below everything else because a `Ticket` carries its trace and a trace step names
an `Intent` -- if the enums lived with the ticket, `domain` and `tracing.models` would
import each other
([ADR 0011](../../docs/adr/0011-shared-vocabulary-below-the-components.md)).

Import them from `support_assistant.domain`, which re-exports them. This module exists to
fix the layering, not to be a second front door.
"""

from enum import StrEnum


class Intent(StrEnum):
    """The kind of question a ticket is asking.

    `unknown` is a handoff trigger, not a category to serve: guessing between two
    templates is the confident-and-wrong behaviour the system exists to avoid.
    """

    BILLING_QUESTION = "billing_question"
    CHARGING_SESSION_PROBLEM = "charging_session_problem"
    UNKNOWN = "unknown"


class TicketStatus(StrEnum):
    """`processing` -> (`replied` | `handed_off`). Both terminal states are final."""

    PROCESSING = "processing"
    REPLIED = "replied"
    HANDED_OFF = "handed_off"


class ReplyTemplate(StrEnum):
    """The reply the pipeline renders, chosen by the LLM from the data it gathered.

    Closed because two components key on it: the LLM picks one, and grounding layer 2
    looks up that template's `TEMPLATE_SAFE_LITERALS`. The prose lives in
    `llm/templates.py`.
    """

    BILLING_ALL_PAID = "billing_all_paid"
    BILLING_FAILED = "billing_failed"
    BILLING_PENDING = "billing_pending"
    SESSION_COMPLETED = "session_completed"
    SESSION_INTERRUPTED = "session_interrupted"


class SessionStatus(StrEnum):
    """How a charging session ended. Closed rather than free text, so status words enter
    the FactSet as facts and grounding layer 2 can check them (ADR 0004)."""

    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"


class InvoiceStatus(StrEnum):
    """Where an invoice stands. Closed for the same reason as `SessionStatus`."""

    PAID = "paid"
    PENDING = "pending"
    FAILED = "failed"


class HandoffReason(StrEnum):
    """Why a ticket was handed to a human (ARCHITECTURE.md section 4).

    Every handoff carries exactly one, and each is produced at exactly one place in the
    orchestrator -- which is what makes handoff-rate-by-reason a real signal rather than a
    rough grouping (OBSERVABILITY.md).
    """

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


class LiteralClass(StrEnum):
    """How a factual literal in a reply was extracted by grounding layer 2.

    The checker tags each extracted literal with one, and `Violation` carries it into the
    trace as the `class` key. Semantic grounding would add a fourth, `contradiction`
    ([ROADMAP](../../docs/ROADMAP.md#semantic-grounding)) -- `verify` treats a class it has
    no rule for as ungrounded.
    """

    NUMBER = "number"
    IDENTIFIER = "identifier"
    STATUS = "status"
