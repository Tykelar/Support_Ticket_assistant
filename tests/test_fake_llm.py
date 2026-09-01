"""The deterministic `FakeLLM` -- keyword intent rules and the step state machine.

Reserved by TESTS.md: "keyword rules per intent, tie resolves to `unknown`, step state
machine ordering". Every case here is offline and clock-free -- the same ticket always
produces the same decision (ADR 0006).

Template and step decisions are checked against observations built from the *shared*
fixtures via `registry.run`, so the test data cannot drift from the demo data
(TESTS.md strategy).
"""

from datetime import UTC, datetime

import pytest

from support_assistant.clock import FrozenClock
from support_assistant.domain import (
    Handoff,
    Intent,
    Observation,
    Reply,
    ReplyTemplate,
    Ticket,
    ToolCall,
    new_ticket_id,
)
from support_assistant.llm.fake import FakeLLM
from support_assistant.tools import registry
from support_assistant.tracing.recorder import TraceRecorder

_NOW = datetime(2026, 8, 31, 10, 14, tzinfo=UTC)


def _ticket(subject: str, body: str, *, user_id: str = "u_002") -> Ticket:
    return Ticket(
        id=new_ticket_id(),
        user_id=user_id,
        subject=subject,
        body=body,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _observe(step: ToolCall) -> Observation:
    """Run a proposed tool call against the real fixtures and pair it with its result."""
    return Observation(step=step, result=registry.run(step.tool, step.args))


def _history(user_id: str, data_tool: str) -> list[Observation]:
    """The history after `get_user` and the intent's data tool have both run for a user."""
    return [
        _observe(ToolCall(tool="get_user", args={"user_id": user_id})),
        _observe(ToolCall(tool=data_tool, args={"user_id": user_id})),
    ]


def _drive(fake: FakeLLM, ticket: Ticket) -> list[object]:
    """Run the loop the way the orchestrator will: decide, observe, decide again."""
    history: list[Observation] = []
    decisions: list[object] = []
    for _ in range(10):  # generous; the fake is expected to terminate in three
        step = fake.decide_next_step(ticket, history)
        decisions.append(step)
        if not isinstance(step, ToolCall):
            break
        history.append(_observe(step))
    return decisions


# --------------------------------------------------------------------------------------
# Intent classification -- case-insensitive keyword matching over subject + body.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "subject, body",
    [
        ("My payment failed", "Why was my invoice not paid?"),
        ("Refund request", "I want a refund for an incorrect charge"),
        ("Question about my bill", "the cost of the last one seems wrong"),
    ],
)
def test_billing_keywords_classify_as_billing_question(subject: str, body: str) -> None:
    assert FakeLLM().classify_intent(_ticket(subject, body)).intent is Intent.BILLING_QUESTION


@pytest.mark.parametrize(
    "subject, body",
    [
        ("Charging session stopped", "The session at the station was interrupted"),
        ("Charger broken", "the connector on the plug will not lock"),
        ("No power delivered", "my charging session recorded 0 kwh"),
    ],
)
def test_charging_keywords_classify_as_charging_problem(subject: str, body: str) -> None:
    fake = FakeLLM()
    assert fake.classify_intent(_ticket(subject, body)).intent is Intent.CHARGING_SESSION_PROBLEM


# --------------------------------------------------------------------------------------
# The evidence -- what the `intent_classified` trace step shows a support agent (ADR 0012)
# --------------------------------------------------------------------------------------


def test_a_classification_carries_the_keywords_it_matched() -> None:
    # The whole point of ADR 0012: an agent reading the trace sees *why* a ticket was
    # categorised as it was, and only the classifier knows that.
    classification = FakeLLM().classify_intent(_ticket("My payment failed", "invoice unpaid"))
    assert classification.intent is Intent.BILLING_QUESTION
    assert classification.matched_keywords == ("invoice", "payment")


def test_the_evidence_is_the_winning_categorys_keywords_only() -> None:
    # A ticket can hit both vocabularies. Showing the loser's hits as evidence for the
    # winner would make the trace argue for a classification it did not make.
    classification = FakeLLM().classify_intent(
        _ticket("invoice and payment query", "one charging note")
    )
    assert classification.intent is Intent.BILLING_QUESTION
    assert "charging" not in classification.matched_keywords


def test_the_evidence_is_distinct_and_ordered() -> None:
    # Deterministic byte for byte, like everything else in the fake (ADR 0006): the same
    # ticket must produce the same trace, so the evidence cannot come out of a set.
    ticket = _ticket("invoice invoice bill", "payment and invoice")
    first = FakeLLM().classify_intent(ticket)
    assert first.matched_keywords == ("bill", "invoice", "payment")
    assert FakeLLM().classify_intent(ticket).matched_keywords == first.matched_keywords


def test_an_unknown_classification_carries_no_evidence() -> None:
    # Nothing matched, or a tie -- either way there is no evidence *for* the outcome, and
    # listing both sides' hits would read as evidence for a category that lost.
    assert FakeLLM().classify_intent(_ticket("Hello", "a general question")).matched_keywords == ()
    tie = _ticket("Charge or session?", "An invoice query and a kwh query")
    assert FakeLLM().classify_intent(tie).matched_keywords == ()


def test_a_ticket_with_no_keywords_is_unknown() -> None:
    ticket = _ticket("Hello", "I have a general question about the app")
    assert FakeLLM().classify_intent(ticket).intent is Intent.UNKNOWN


def test_a_keyword_tie_resolves_to_unknown() -> None:
    # Equal distinct hits on both sides is genuine ambiguity; guessing between two
    # templates is the confident-and-wrong behaviour the system exists to avoid (LLM.md).
    ticket = _ticket("Charge or session?", "An invoice query and a kwh query")
    assert FakeLLM().classify_intent(ticket).intent is Intent.UNKNOWN


def test_classification_is_case_insensitive() -> None:
    ticket = _ticket("INVOICE PROBLEM", "MY PAYMENT DID NOT GO THROUGH")
    assert FakeLLM().classify_intent(ticket).intent is Intent.BILLING_QUESTION


def test_score_counts_distinct_keywords_not_total_occurrences() -> None:
    # "invoice" x3 is one distinct billing hit; "session" + "station" is two charging
    # hits -- so distinct scoring picks charging, total scoring would pick billing.
    ticket = _ticket("invoice invoice invoice", "a session at a station")
    assert FakeLLM().classify_intent(ticket).intent is Intent.CHARGING_SESSION_PROBLEM


def test_the_higher_distinct_count_wins() -> None:
    ticket = _ticket("invoice and payment", "one charging note")
    assert FakeLLM().classify_intent(ticket).intent is Intent.BILLING_QUESTION


def test_both_subject_and_body_are_searched() -> None:
    fake = FakeLLM()
    assert (
        fake.classify_intent(_ticket("Help please", "my invoice and payment are wrong")).intent
        is Intent.BILLING_QUESTION
    )
    assert (
        fake.classify_intent(_ticket("charging session issue", "please advise")).intent
        is Intent.CHARGING_SESSION_PROBLEM
    )


# --------------------------------------------------------------------------------------
# Step state machine -- get_user first, then the intent's data tool, then a reply.
# --------------------------------------------------------------------------------------


def test_the_first_step_is_always_get_user() -> None:
    step = FakeLLM().decide_next_step(_ticket("invoice", "payment"), [])
    assert isinstance(step, ToolCall)
    assert step.tool == "get_user"
    assert step.args == {"user_id": "u_002"}


def test_after_get_user_a_billing_ticket_asks_for_invoices() -> None:
    fake = FakeLLM()
    ticket = _ticket("invoice", "payment", user_id="u_002")
    history = [_observe(fake.decide_next_step(ticket, []))]
    step = fake.decide_next_step(ticket, history)
    assert isinstance(step, ToolCall)
    assert step.tool == "get_invoices"
    assert step.args == {"user_id": "u_002"}


def test_after_get_user_a_charging_ticket_asks_for_sessions() -> None:
    fake = FakeLLM()
    ticket = _ticket("charging session", "session at the station", user_id="u_002")
    history = [_observe(fake.decide_next_step(ticket, []))]
    step = fake.decide_next_step(ticket, history)
    assert isinstance(step, ToolCall)
    assert step.tool == "get_charging_sessions"


@pytest.mark.parametrize(
    "user_id, expected",
    [
        ("u_001", ReplyTemplate.BILLING_ALL_PAID),  # inv_102, inv_101 both paid
        ("u_002", ReplyTemplate.BILLING_FAILED),  # inv_204 failed among two paid
        ("u_003", ReplyTemplate.BILLING_PENDING),  # inv_302 pending, none failed
    ],
)
def test_billing_reply_template_follows_the_invoice_statuses(
    user_id: str, expected: ReplyTemplate
) -> None:
    step = FakeLLM().decide_next_step(
        _ticket("invoice", "payment", user_id=user_id),
        _history(user_id, "get_invoices"),
    )
    assert isinstance(step, Reply)
    assert step.template is expected


def test_billing_failed_takes_precedence_over_pending_and_paid() -> None:
    # u_002 has a failed invoice and two paid ones; "failed" wins the template choice.
    step = FakeLLM().decide_next_step(
        _ticket("invoice", "payment", user_id="u_002"),
        _history("u_002", "get_invoices"),
    )
    assert isinstance(step, Reply)
    assert step.template is ReplyTemplate.BILLING_FAILED


def test_charging_reply_is_completed_when_the_latest_session_completed() -> None:
    step = FakeLLM().decide_next_step(
        _ticket("charging session", "session at the station", user_id="u_001"),
        _history("u_001", "get_charging_sessions"),
    )
    assert isinstance(step, Reply)
    assert step.template is ReplyTemplate.SESSION_COMPLETED


def test_charging_reply_reads_the_most_recent_session_not_an_older_one() -> None:
    # u_003: newest session (sess_3002, 08-22) is interrupted, the older one
    # (sess_3001, 08-09) completed -- the template must follow the newest.
    step = FakeLLM().decide_next_step(
        _ticket("charging session", "session at the station", user_id="u_003"),
        _history("u_003", "get_charging_sessions"),
    )
    assert isinstance(step, Reply)
    assert step.template is ReplyTemplate.SESSION_INTERRUPTED


# --------------------------------------------------------------------------------------
# Termination and the handoff contract.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "user_id, phrasing, data_tool, template",
    [
        ("u_002", ("invoice", "payment"), "get_invoices", ReplyTemplate.BILLING_FAILED),
        (
            "u_001",
            ("charging session", "session at the station"),
            "get_charging_sessions",
            ReplyTemplate.SESSION_COMPLETED,
        ),
    ],
)
def test_the_fake_terminates_in_three_steps_and_never_hands_off(
    user_id: str, phrasing: tuple[str, str], data_tool: str, template: ReplyTemplate
) -> None:
    decisions = _drive(FakeLLM(), _ticket(*phrasing, user_id=user_id))
    assert [type(d) for d in decisions] == [ToolCall, ToolCall, Reply]
    assert [d.tool for d in decisions[:2]] == ["get_user", data_tool]
    assert decisions[-1].template is template
    assert not any(isinstance(d, Handoff) for d in decisions)


def test_decide_next_step_refuses_an_unknown_intent() -> None:
    # The pipeline hands `unknown` off before entering the loop, so being asked to decide
    # for one is a contract violation, not a case to guess at.
    with pytest.raises(ValueError):
        FakeLLM().decide_next_step(
            _ticket("Hello", "I have a general question about the app"), []
        )


# --------------------------------------------------------------------------------------
# Coupling: the registry is the single source of truth for tool names.
# --------------------------------------------------------------------------------------


def test_every_tool_the_fake_calls_is_a_registered_tool() -> None:
    fake = FakeLLM()
    seen: set[str] = set()
    for user_id, phrasing in [
        ("u_002", ("invoice", "payment")),
        ("u_002", ("charging session", "session at the station")),
    ]:
        ticket = _ticket(*phrasing, user_id=user_id)
        history: list[Observation] = []
        for _ in range(5):
            step = fake.decide_next_step(ticket, history)
            if not isinstance(step, ToolCall):
                break
            seen.add(step.tool)
            history.append(_observe(step))
    assert seen == {"get_user", "get_invoices", "get_charging_sessions"}
    assert seen <= set(registry.registered())


# --------------------------------------------------------------------------------------
# The decision object feeds the trace recorder's llm_decision call unchanged.
# --------------------------------------------------------------------------------------


def test_a_decision_feeds_trace_llm_decision_via_its_own_attributes() -> None:
    # PIPELINE.md: trace.llm_decision(i, step.decision, tool=getattr(step, "tool", None)).
    fake = FakeLLM()
    ticket = _ticket("invoice", "payment", user_id="u_002")
    tool_step = fake.decide_next_step(ticket, [])
    reply_step = fake.decide_next_step(ticket, _history("u_002", "get_invoices"))

    rec = TraceRecorder(FrozenClock())
    for i, step in enumerate((tool_step, reply_step), start=1):
        rec.llm_decision(i, step.decision, tool=getattr(step, "tool", None))

    tool_rec, reply_rec = rec.steps
    assert (tool_rec.decision, tool_rec.tool) == ("tool_call", "get_user")
    assert (reply_rec.decision, reply_rec.tool) == ("reply", None)
