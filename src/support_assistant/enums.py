"""The enumerations the whole system agrees on (ARCHITECTURE.md section 4).

These sit below everything else on purpose. A `Ticket` carries its trace, and a trace
step names an `Intent` and a `TicketStatus` -- so if the enums lived with the ticket,
`domain` and `tracing.models` would import each other. They are the one layer both can
depend on, and the reason `HandoffReason` and `LiteralClass` live here rather than beside
the code that raises them
([ADR 0011](../../docs/adr/0011-shared-vocabulary-below-the-components.md)).

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


class ReplyTemplate(StrEnum):
    """The reply the pipeline renders, chosen by the LLM from the data it gathered.

    A closed vocabulary, beside `Intent`, because two components key on it: the LLM picks
    one (`Reply.template`), and grounding layer 2 looks up that template's
    `TEMPLATE_SAFE_LITERALS` when it verifies the rendered text (LLM.md, GUARDRAILS.md).
    The prose and the per-template safe-literal lists live in `llm/templates.py`.
    """

    BILLING_ALL_PAID = "billing_all_paid"
    BILLING_FAILED = "billing_failed"
    BILLING_PENDING = "billing_pending"
    SESSION_COMPLETED = "session_completed"
    SESSION_INTERRUPTED = "session_interrupted"


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


class HandoffReason(StrEnum):
    """Why a ticket was handed to a human. See ARCHITECTURE.md section 4.

    The closed set of explanations, and one of the contracts fixed across the system.
    Every handoff carries exactly one, and each is produced at exactly one place in the
    orchestrator -- which is what makes handoff-rate-by-reason a trustworthy signal rather
    than a rough grouping (GUARDRAILS.md, OBSERVABILITY.md).
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

    Closed for the same reason as `SessionStatus`, and named by two components: the
    checker tags each extracted literal with one, and `Violation` carries it into the
    trace as the `class` key TRACEABILITY.md documents. Semantic grounding would add a
    fourth, `contradiction` ([ROADMAP](../../docs/ROADMAP.md#semantic-grounding)).
    """

    NUMBER = "number"
    IDENTIFIER = "identifier"
    STATUS = "status"
