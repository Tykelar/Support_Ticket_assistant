"""The orchestrator -- the component the brief is actually evaluating.

Reserved by TESTS.md: "orchestration with stubbed collaborators; every handoff reason
reachable". The control flow under test is PIPELINE.md's, and the property that matters
most is the one the enum parametrisation pins: **every `HandoffReason` member is produced
by some path**. A reason no code can reach is dead code pretending to be a guardrail.

Stubs, not mocks. Each is a small class implementing the real `LLMClient` protocol, so a
protocol change breaks them at the call site instead of leaving tests passing against an
interface that no longer exists. The happy path runs the real `FakeLLM` against the
shared fixtures, because the point of a deterministic fake is that it can be used in
anger.
"""

from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from support_assistant.clock import FrozenClock
from support_assistant.domain import (
    Classification,
    Handoff,
    HandoffReason,
    Intent,
    Observation,
    Reply,
    ReplyTemplate,
    Step,
    Ticket,
    TicketStatus,
    ToolCall,
    ToolResult,
    new_ticket_id,
)
from support_assistant.guardrails.limits import MAX_ITERATIONS
from support_assistant.llm import templates
from support_assistant.llm.fake import FakeLLM
from support_assistant.llm.protocol import LLMClient
from support_assistant.observability.metrics import REGISTRY, MetricRegistry
from support_assistant.pipeline.orchestrator import ToolRunner, run_pipeline
from support_assistant.storage.memory import InMemoryTicketRepository
from support_assistant.tools import registry
from support_assistant.tracing.models import (
    FinalDecision,
    GroundingCheck,
    IntentClassified,
    LLMDecision,
    ToolCallStep,
    ToolResultStep,
)

_NOW = datetime(2026, 8, 31, 10, 14, tzinfo=UTC)

_BILLING = ("My payment failed", "Why was my last invoice not paid?")
_CHARGING = ("Charging session stopped", "the session at the station was interrupted")
_VAGUE = ("Hello", "I have a general question about the app")


# --------------------------------------------------------------------------------------
# Stubs -- each implements the real protocol
# --------------------------------------------------------------------------------------


class _Stub:
    """Classifies every ticket as billing; each subclass differs only in what it decides.

    The classification is shared because none of these tests is about classifying -- they
    are about what the loop does with the step that comes back. `decide_next_step` stays
    abstract so a subclass cannot silently inherit someone else's behaviour.
    """

    def classify_intent(self, ticket: Ticket) -> Classification:
        return Classification(intent=Intent.BILLING_QUESTION, matched_keywords=("invoice",))

    def decide_next_step(self, ticket: Ticket, history: list[Observation]) -> Step:
        raise NotImplementedError


class _Runaway(_Stub):
    """Never terminates. The iteration cap is the only thing that stops it."""

    def decide_next_step(self, ticket: Ticket, history: list[Observation]) -> Step:
        return ToolCall(tool="get_user", args={"user_id": ticket.user_id})


class _GivesUp(_Stub):
    """A model that decides to stop. `FakeLLM` never does this; a real one can, and the
    pipeline has to treat it as an outcome rather than an error (ADR 0006)."""

    def __init__(self, reason: HandoffReason) -> None:
        self._reason = reason

    def decide_next_step(self, ticket: Ticket, history: list[Observation]) -> Step:
        return Handoff(reason=self._reason)


class _CallsAnUnregisteredTool(_Stub):
    """The model-chose-versus-system-did divergence ADR 0010 exists to make visible."""

    def decide_next_step(self, ticket: Ticket, history: list[Observation]) -> Step:
        return ToolCall(tool="get_refunds", args={"user_id": ticket.user_id})


class _RepliesImmediately(_Stub):
    """Replies without gathering anything, so the FactSet cannot fill the template."""

    def decide_next_step(self, ticket: Ticket, history: list[Observation]) -> Step:
        return Reply(template=ReplyTemplate.BILLING_ALL_PAID)


# --------------------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------------------


def _ticket(user_id: str, subject: str, body: str) -> Ticket:
    return Ticket(
        id=new_ticket_id(),
        user_id=user_id,
        subject=subject,
        body=body,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _run(
    user_id: str = "u_002",
    text: tuple[str, str] = _BILLING,
    *,
    llm: LLMClient | None = None,
    resolve_template: Callable[[ReplyTemplate], templates.Template] = templates.spec_for,
    max_iterations: int = MAX_ITERATIONS,
    metrics: MetricRegistry | None = None,
    tools: ToolRunner = registry.run,
) -> Ticket:
    """Create a ticket, run the pipeline over it, and read back what was persisted.

    Reading back rather than asserting on the recorder is deliberate: a terminal state
    the repository never saw would pass every in-memory assertion and still strand the
    ticket.

    A fresh `MetricRegistry` per run unless one is passed. Not a detail: these runs used
    to take `run_pipeline`'s old module-level default and count into the process-wide
    `REGISTRY`, so a whole file's tests silently coloured the object `GET /metrics`
    serves -- the exact leak `metrics.py` claims tests avoid by injecting their own.
    """
    repository = InMemoryTicketRepository(FrozenClock())
    ticket = _ticket(user_id, *text)
    repository.create(ticket)

    run_pipeline(
        ticket.id,
        repository=repository,
        llm=llm if llm is not None else FakeLLM(),
        clock=FrozenClock(),
        resolve_template=resolve_template,
        max_iterations=max_iterations,
        metrics=metrics if metrics is not None else MetricRegistry(),
        tools=tools,
    )

    stored = repository.get(ticket.id)
    assert stored is not None
    return stored


def _types(ticket: Ticket) -> list[str]:
    return [step.type for step in ticket.trace]


def _final(ticket: Ticket) -> FinalDecision:
    last = ticket.trace[-1]
    assert isinstance(last, FinalDecision)
    return last


# --------------------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------------------


def test_a_billing_ticket_reaches_replied_with_a_grounded_reply() -> None:
    ticket = _run("u_002", _BILLING)

    assert ticket.status is TicketStatus.REPLIED
    assert ticket.handoff_reason is None
    assert ticket.reply is not None
    assert "inv_204" in ticket.reply  # the real invoice from the fixture
    assert "42.10" in ticket.reply  # the real amount


def test_a_charging_ticket_reaches_replied() -> None:
    ticket = _run("u_003", _CHARGING)

    assert ticket.status is TicketStatus.REPLIED
    assert ticket.reply is not None
    assert "Lyon Part-Dieu" in ticket.reply  # the most recent session's station


def test_the_happy_path_trace_is_the_whole_story_in_order() -> None:
    # Requirement 5: an agent answers "why did the AI say this?" from this list alone.
    ticket = _run("u_002", _BILLING)

    assert _types(ticket) == [
        "intent_classified",
        "llm_decision",
        "tool_call",
        "tool_result",
        "llm_decision",
        "tool_call",
        "tool_result",
        "llm_decision",
        "grounding_check",
        "final_decision",
    ]


def test_the_trace_records_the_evidence_for_the_classification() -> None:
    # ADR 0012: the keywords come from the classifier, not from a copy of its rules.
    ticket = _run("u_002", _BILLING)

    first = ticket.trace[0]
    assert isinstance(first, IntentClassified)
    assert first.intent is Intent.BILLING_QUESTION
    assert "invoice" in first.matched_keywords


def test_the_trace_records_what_the_model_chose_and_what_the_system_did() -> None:
    ticket = _run("u_002", _BILLING)

    decisions = [s for s in ticket.trace if isinstance(s, LLMDecision)]
    assert [(d.iteration, d.decision, d.tool) for d in decisions] == [
        (1, "tool_call", "get_user"),
        (2, "tool_call", "get_invoices"),
        (3, "reply", None),
    ]


def test_a_tool_result_is_summarised_not_dumped() -> None:
    ticket = _run("u_002", _BILLING)

    results = [s for s in ticket.trace if isinstance(s, ToolResultStep)]
    assert all(step.ok for step in results)
    assert results[1].summary == {
        "count": 3,
        "statuses": {"paid": 2, "failed": 1},
        "referenced": ["inv_204", "inv_203", "inv_202"],
    }


def test_the_grounding_check_ran_and_actually_inspected_something() -> None:
    # `passed: true, literals_checked: 0` is a check that never looked at anything
    # (TRACEABILITY.md), so the count matters as much as the verdict.
    ticket = _run("u_002", _BILLING)

    check = next(s for s in ticket.trace if isinstance(s, GroundingCheck))
    assert check.passed
    assert check.literals_checked > 0
    assert check.violations == []


def test_a_replied_ticket_ends_with_a_final_decision_carrying_no_reason() -> None:
    ticket = _run("u_002", _BILLING)

    final = _final(ticket)
    assert final.outcome is TicketStatus.REPLIED
    assert final.reason is None


def test_trace_timestamps_increase_with_seq() -> None:
    ticket = _run("u_002", _BILLING)

    assert [s.seq for s in ticket.trace] == list(range(1, len(ticket.trace) + 1))
    pairs = zip(ticket.trace, ticket.trace[1:], strict=False)
    assert all(earlier.ts < later.ts for earlier, later in pairs)


# --------------------------------------------------------------------------------------
# Every handoff reason is reachable
# --------------------------------------------------------------------------------------


def _unsupported_intent() -> Ticket:
    return _run("u_002", _VAGUE)


def _user_not_found() -> Ticket:
    return _run("u_005", _BILLING)  # absent from every fixture file


def _data_not_found() -> Ticket:
    return _run("u_004", _BILLING)  # a real user with no invoices and no sessions


def _tool_error() -> Ticket:
    return _run("u_006", _BILLING)  # inv_601's amount is "forty-two euros"


def _iteration_cap_exceeded() -> Ticket:
    return _run("u_002", _BILLING, llm=_Runaway())


def _ungrounded_reply() -> Ticket:
    honest = templates.spec_for(ReplyTemplate.BILLING_FAILED)
    doctored = templates.Template(
        name=honest.name,
        body="Hi {name}, invoice {invoice_id} for 99.00 {currency} has a failed payment.",
        context=honest.context,
        TEMPLATE_SAFE_LITERALS=honest.TEMPLATE_SAFE_LITERALS,
    )
    return _run("u_002", _BILLING, resolve_template=lambda _: doctored)


_REACHES = {
    HandoffReason.UNSUPPORTED_INTENT: _unsupported_intent,
    HandoffReason.USER_NOT_FOUND: _user_not_found,
    HandoffReason.DATA_NOT_FOUND: _data_not_found,
    HandoffReason.TOOL_ERROR: _tool_error,
    HandoffReason.ITERATION_CAP_EXCEEDED: _iteration_cap_exceeded,
    HandoffReason.UNGROUNDED_REPLY: _ungrounded_reply,
}


@pytest.mark.parametrize("reason", list(HandoffReason))
def test_every_handoff_reason_is_reachable(reason: HandoffReason) -> None:
    # Parametrised over the enum rather than over the dict, so a member added without
    # being wired to a code path fails here instead of shipping as a guardrail that
    # cannot fire (TESTS.md).
    ticket = _REACHES[reason]()

    assert ticket.status is TicketStatus.HANDED_OFF
    assert ticket.handoff_reason is reason


@pytest.mark.parametrize("reason", list(HandoffReason))
def test_every_handoff_sends_nothing_at_all(reason: HandoffReason) -> None:
    # ADR 0005: reply is None. Not an empty string, not a polite holding message.
    assert _REACHES[reason]().reply is None


@pytest.mark.parametrize("reason", list(HandoffReason))
def test_every_handoff_records_its_reason_and_a_supporting_detail(
    reason: HandoffReason,
) -> None:
    # A reason explains the category; the detail explains the incident -- which user id
    # was missing, which tool raised, which literal was ungrounded (GUARDRAILS.md).
    final = _final(_REACHES[reason]())

    assert final.outcome is TicketStatus.HANDED_OFF
    assert final.reason is reason
    assert final.detail


@pytest.mark.parametrize("reason", list(HandoffReason))
def test_every_handoff_ends_with_exactly_one_final_decision(reason: HandoffReason) -> None:
    trace = _REACHES[reason]().trace

    assert [step.type for step in trace].count("final_decision") == 1
    assert trace[-1].type == "final_decision"


# --------------------------------------------------------------------------------------
# The reasons, one at a time -- what each one's trace has to show
# --------------------------------------------------------------------------------------


def test_an_unknown_intent_hands_off_before_the_loop_is_entered() -> None:
    ticket = _run("u_002", _VAGUE)

    assert _types(ticket) == ["intent_classified", "final_decision"]
    assert ticket.handoff_reason is HandoffReason.UNSUPPORTED_INTENT


def test_a_failed_tool_call_still_records_its_result_step() -> None:
    # ADR 0010: a raising tool would otherwise jump past the line that records it,
    # leaving the ok:false step -- the first thing an agent reads when a ticket went
    # wrong -- written by nothing.
    ticket = _run("u_005", _BILLING)

    result = next(s for s in ticket.trace if isinstance(s, ToolResultStep))
    assert result.ok is False
    assert result.tool == "get_user"
    assert result.error is not None
    assert result.error.type == "UserNotFound"
    assert result.summary is None


def test_a_missing_user_and_missing_data_do_not_collapse_into_one_reason() -> None:
    # The brief draws the distinction; ADR 0009 keeps it.
    assert _run("u_005", _BILLING).handoff_reason is HandoffReason.USER_NOT_FOUND
    assert _run("u_004", _BILLING).handoff_reason is HandoffReason.DATA_NOT_FOUND


def test_missing_data_is_detected_after_the_user_was_found() -> None:
    ticket = _run("u_004", _BILLING)

    results = [s for s in ticket.trace if isinstance(s, ToolResultStep)]
    assert [(s.tool, s.ok) for s in results] == [
        ("get_user", True),
        ("get_invoices", False),
    ]


def test_an_unregistered_tool_name_is_contained_and_recorded() -> None:
    ticket = _run("u_002", _BILLING, llm=_CallsAnUnregisteredTool())

    assert ticket.handoff_reason is HandoffReason.TOOL_ERROR
    call = next(s for s in ticket.trace if isinstance(s, ToolCallStep))
    result = next(s for s in ticket.trace if isinstance(s, ToolResultStep))
    assert call.tool == "get_refunds"  # what the model chose
    assert result.ok is False  # what the system did
    assert result.error is not None
    assert result.error.type == "ToolExecutionError"


def test_a_template_the_facts_cannot_fill_is_a_tool_error_with_a_readable_detail() -> None:
    # render() raises ValueError naming the fact it wanted. The catch-all turns that into
    # TOOL_ERROR, and the detail is what makes it readable rather than a bare traceback
    # class name (LLM.md).
    ticket = _run("u_002", _BILLING, llm=_RepliesImmediately())

    final = _final(ticket)
    assert final.reason is HandoffReason.TOOL_ERROR
    assert "name" in (final.detail or "")


def test_a_model_that_gives_up_is_an_outcome_not_an_error() -> None:
    ticket = _run("u_002", _BILLING, llm=_GivesUp(HandoffReason.UNSUPPORTED_INTENT))

    assert ticket.status is TicketStatus.HANDED_OFF
    assert ticket.handoff_reason is HandoffReason.UNSUPPORTED_INTENT
    decision = next(s for s in ticket.trace if isinstance(s, LLMDecision))
    assert decision.decision == "handoff"
    assert decision.tool is None


def test_an_ungrounded_reply_is_withheld_with_the_literal_recorded() -> None:
    ticket = _ungrounded_reply()

    assert ticket.reply is None
    check = next(s for s in ticket.trace if isinstance(s, GroundingCheck))
    assert check.passed is False
    assert [v.literal for v in check.violations] == ["99.00"]
    assert "99.00" in (_final(ticket).detail or "")


# --------------------------------------------------------------------------------------
# The cap, and what the loop never does
# --------------------------------------------------------------------------------------


def test_a_capped_run_never_reaches_the_grounding_check() -> None:
    # There is no path where exhausting the loop produces a reply, so there is nothing
    # to ground (PIPELINE.md's `for ... else`).
    ticket = _run("u_002", _BILLING, llm=_Runaway())

    assert "grounding_check" not in _types(ticket)
    assert ticket.reply is None


def test_a_lower_cap_is_honoured() -> None:
    # The cap is configurable because the right value depends on the model
    # (guardrails/limits.py); the orchestrator must actually read it.
    ticket = _run("u_002", _BILLING, llm=_Runaway(), max_iterations=2)

    assert len([s for s in ticket.trace if isinstance(s, LLMDecision)]) == 2
    assert ticket.handoff_reason is HandoffReason.ITERATION_CAP_EXCEEDED


def test_a_failed_tool_call_ends_the_run_rather_than_being_retried() -> None:
    # History accumulates only successful observations, so the model never sees an error
    # and never gets to work around it. Retry-on-failure is deliberately absent.
    ticket = _run("u_004", _BILLING)

    assert len([s for s in ticket.trace if isinstance(s, ToolCallStep)]) == 2
    assert ticket.status is TicketStatus.HANDED_OFF


# --------------------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------------------


def test_the_run_never_leaves_a_ticket_in_processing() -> None:
    for produce in _REACHES.values():
        assert produce().status is not TicketStatus.PROCESSING
    assert _run("u_002", _BILLING).status is not TicketStatus.PROCESSING


def test_a_run_counts_only_into_the_registry_it_was_handed() -> None:
    # `repository`, `llm` and `clock` are per-app state and have no defaults; `metrics` is
    # per-app state too -- `create_app` builds the one `GET /metrics` serves. It used to
    # default to the module-level `REGISTRY`, which is the wrong object in exactly the
    # case the parameter exists for.
    before = REGISTRY.render()
    registry = MetricRegistry()

    _run("u_002", _BILLING, metrics=registry)

    assert 'tickets_total{status="replied"} 1' in registry.render()
    assert REGISTRY.render() == before  # nothing leaked into the process-wide one


def test_run_pipeline_has_no_default_registry_to_leak_into() -> None:
    """The guard for the test above, and for the callers that do not exist yet.

    With a default, a caller that omits `metrics` counts into the module singleton while
    the endpoint serves an object nobody wrote to -- silently, since every number is
    merely low rather than absent. The roadmap's reaper and queue worker are the next two
    callers of `run_pipeline`; the parameter being required is what stops either of them
    being written that way.
    """
    repository = InMemoryTicketRepository(FrozenClock())
    ticket = _ticket("u_002", *_BILLING)
    repository.create(ticket)

    with pytest.raises(TypeError):
        run_pipeline(  # type: ignore[call-arg]
            ticket.id, repository=repository, llm=FakeLLM(), clock=FrozenClock()
        )


def test_running_an_unknown_ticket_id_raises_rather_than_writing_anything() -> None:
    # The API creates the ticket before scheduling the run, so this cannot happen in the
    # real flow -- and if it does, it is a bug to surface, not a handoff to invent for a
    # ticket that does not exist to carry it.
    repository = InMemoryTicketRepository(FrozenClock())

    with pytest.raises(ValueError):
        run_pipeline(
            new_ticket_id(),
            repository=repository,
            llm=FakeLLM(),
            clock=FrozenClock(),
            metrics=MetricRegistry(),
        )


class _WriteFails:
    """A repository whose `finalise` always raises, recording what it was handed.

    Storage failing is the one thing the orchestrator cannot fail closed over, because
    failing closed *is* a write (ADR 0013).
    """

    def __init__(self) -> None:
        self._inner = InMemoryTicketRepository(FrozenClock())
        self.attempts: list[list[str]] = []

    def create(self, ticket: Ticket) -> None:
        self._inner.create(ticket)

    def get(self, ticket_id: str) -> Ticket | None:
        return self._inner.get(ticket_id)

    def finalise(self, ticket_id, status, reply, handoff_reason, trace) -> None:
        self.attempts.append([step.type for step in trace])
        raise RuntimeError("database is locked")


def test_a_failing_write_is_not_retried_into_a_second_final_decision() -> None:
    # The catch-all exists to turn a failed run into a terminal write. It must not also
    # guard the write itself: retrying one appends a second `final_decision`, and the
    # trace then carries two -- the first claiming an outcome that was never persisted.
    repository = _WriteFails()
    ticket = _ticket("u_002", *_BILLING)
    repository.create(ticket)

    with pytest.raises(RuntimeError):
        run_pipeline(
            ticket.id,
            repository=repository,
            llm=FakeLLM(),
            clock=FrozenClock(),
            metrics=MetricRegistry(),
        )

    (attempt,) = repository.attempts  # tried once, not caught and tried again
    assert attempt.count("final_decision") == 1


def test_a_tool_call_is_always_followed_by_a_tool_result() -> None:
    """The `tool_result` step is the first thing a reader looks at when a ticket went
    wrong, so the loop records one even when the call failed. Summarising the result is
    part of that call: if it raises after the tool returned, an unpaired `tool_call` is
    the only trace left of what happened.
    """

    def _unsummarisable(tool: str, args: dict[str, object]) -> ToolResult:
        # A tool that dispatches and returns, but that `tracing/` has no rule for.
        return ToolResult(tool="get_refunds", records=[])

    ticket = _run(llm=_CallsAnUnregisteredTool(), tools=_unsummarisable)

    assert _types(ticket) == [
        "intent_classified",
        "llm_decision",
        "tool_call",
        "tool_result",
        "final_decision",
    ]
    result = next(s for s in ticket.trace if isinstance(s, ToolResultStep))
    assert not result.ok
    assert result.error is not None and "summariser" in result.error.message
    assert _final(ticket).reason is HandoffReason.TOOL_ERROR
