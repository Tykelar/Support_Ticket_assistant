"""Trace step models, the TraceRecorder, seq/ts ordering, and the summarisation rules.

Started as the step-model shapes TRACEABILITY.md specifies; phase 4 grew it with the
recorder that produces those steps and the per-tool result summariser. It grows rather
than being replaced.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import TypeAdapter, ValidationError

from support_assistant.clock import DEFAULT_START, DEFAULT_TICK, FrozenClock
from support_assistant.domain import (
    ChargingSession,
    Intent,
    Invoice,
    InvoiceStatus,
    SessionStatus,
    TicketStatus,
    ToolResult,
    User,
)
from support_assistant.guardrails.grounding import Violation
from support_assistant.guardrails.handoff import HandoffReason
from support_assistant.tools.errors import NoDataAvailable
from support_assistant.tracing.models import (
    FinalDecision,
    GroundingCheck,
    IntentClassified,
    LLMDecision,
    ToolCallStep,
    ToolError,
    ToolResultStep,
    TraceStep,
)
from support_assistant.tracing.recorder import TraceRecorder, as_tool_error
from support_assistant.tracing.summarise import summarise

TS = datetime(2026, 8, 31, 10, 14, 2, 101000, tzinfo=UTC)
STEPS = TypeAdapter(list[TraceStep])


# --------------------------------------------------------------------------------------
# Each step type, against the JSON in TRACEABILITY.md and API.md.
# --------------------------------------------------------------------------------------


def test_intent_classified_records_the_evidence_not_just_the_verdict() -> None:
    step = IntentClassified(
        seq=1, ts=TS, intent=Intent.BILLING_QUESTION, matched_keywords=["payment", "invoice"]
    )
    assert step.type == "intent_classified"
    assert step.matched_keywords == ["payment", "invoice"]


def test_llm_decision_records_what_the_model_chose() -> None:
    step = LLMDecision(seq=2, ts=TS, iteration=1, decision="tool_call", tool="get_user")
    assert step.type == "llm_decision"
    assert step.iteration == 1


def test_llm_decision_needs_no_tool_when_the_model_replies() -> None:
    # Recorded separately from tool_call on purpose: one says what the model chose, the
    # other what the system did, and the case worth seeing is where they diverge.
    step = LLMDecision(seq=8, ts=TS, iteration=3, decision="reply")
    assert step.tool is None


def test_llm_decision_rejects_a_decision_outside_the_union() -> None:
    with pytest.raises(ValidationError):
        LLMDecision(seq=2, ts=TS, iteration=1, decision="retry")


def test_tool_call_records_its_arguments() -> None:
    step = ToolCallStep(seq=3, ts=TS, tool="get_invoices", args={"user_id": "u_002"})
    assert step.args == {"user_id": "u_002"}


def test_tool_result_carries_a_summary_on_success() -> None:
    step = ToolResultStep(
        seq=7,
        ts=TS,
        tool="get_invoices",
        ok=True,
        summary={"count": 3, "statuses": {"paid": 2, "failed": 1}, "referenced": ["inv_204"]},
    )
    assert step.error is None
    assert step.summary["count"] == 3


def test_tool_result_carries_a_typed_error_on_failure() -> None:
    step = ToolResultStep(
        seq=4,
        ts=TS,
        tool="get_invoices",
        ok=False,
        error=ToolError(type="NoDataAvailable", message="user u_004 has no invoices"),
    )
    assert step.summary is None
    assert step.error.type == "NoDataAvailable"


def test_grounding_check_records_the_offending_literals() -> None:
    step = GroundingCheck(
        seq=9,
        ts=TS,
        passed=False,
        literals_checked=4,
        violations=[
            Violation(
                literal="99.00",
                literal_class="number",
                reason="not present in FactSet or TEMPLATE_SAFE_LITERALS",
            )
        ],
    )
    assert step.violations[0].literal == "99.00"


def test_grounding_check_passes_with_no_violations() -> None:
    step = GroundingCheck(seq=9, ts=TS, passed=True, literals_checked=3)
    assert step.violations == []


def test_final_decision_carries_reason_and_detail_on_a_handoff() -> None:
    # A reason without detail explains the category but not the incident (GUARDRAILS.md).
    step = FinalDecision(
        seq=5,
        ts=TS,
        outcome=TicketStatus.HANDED_OFF,
        reason=HandoffReason.DATA_NOT_FOUND,
        detail="get_invoices found no records for u_004",
    )
    assert step.reason is HandoffReason.DATA_NOT_FOUND
    assert step.detail


def test_final_decision_needs_no_reason_when_replied() -> None:
    step = FinalDecision(seq=10, ts=TS, outcome=TicketStatus.REPLIED)
    assert step.reason is None


@pytest.mark.parametrize(
    "kwargs, why",
    [
        ({"outcome": TicketStatus.PROCESSING}, "processing is not a decision"),
        (
            {"outcome": TicketStatus.HANDED_OFF},
            "a handoff always carries exactly one reason",
        ),
        (
            {"outcome": TicketStatus.REPLIED, "reason": HandoffReason.TOOL_ERROR},
            "a reply has no handoff reason",
        ),
    ],
)
def test_final_decision_rejects_incoherent_outcomes(kwargs: dict, why: str) -> None:
    with pytest.raises(ValidationError):
        FinalDecision(seq=5, ts=TS, **kwargs)


# --------------------------------------------------------------------------------------
# The union -- a persisted trace is JSON and has to come back as the right classes.
# --------------------------------------------------------------------------------------


def test_a_trace_discriminates_into_its_concrete_step_types() -> None:
    # STORAGE.md persists each step's payload as JSON keyed by `type`; reading a ticket
    # back has to reconstruct the right class or the API response is untyped soup.
    raw = [
        {
            "seq": 1,
            "ts": "2026-08-31T10:14:02.101Z",
            "type": "intent_classified",
            "intent": "billing_question",
            "matched_keywords": ["payment"],
        },
        {
            "seq": 2,
            "ts": "2026-08-31T10:14:02.104Z",
            "type": "llm_decision",
            "iteration": 1,
            "decision": "tool_call",
            "tool": "get_user",
        },
        {
            "seq": 3,
            "ts": "2026-08-31T10:14:02.106Z",
            "type": "tool_call",
            "tool": "get_user",
            "args": {"user_id": "u_002"},
        },
        {
            "seq": 4,
            "ts": "2026-08-31T10:14:02.109Z",
            "type": "tool_result",
            "tool": "get_user",
            "ok": True,
            "summary": {"found": True, "plan": "basic"},
        },
        {
            "seq": 5,
            "ts": "2026-08-31T10:14:02.121Z",
            "type": "grounding_check",
            "passed": True,
            "literals_checked": 3,
        },
        {
            "seq": 6,
            "ts": "2026-08-31T10:14:02.122Z",
            "type": "final_decision",
            "outcome": "replied",
        },
    ]
    steps = STEPS.validate_python(raw)
    assert [type(s) for s in steps] == [
        IntentClassified,
        LLMDecision,
        ToolCallStep,
        ToolResultStep,
        GroundingCheck,
        FinalDecision,
    ]


def test_an_unknown_step_type_is_rejected() -> None:
    # The step vocabulary is closed. A type nothing writes is either a typo or a step
    # someone added without documenting it.
    with pytest.raises(ValidationError):
        STEPS.validate_python([{"seq": 1, "ts": TS, "type": "llm_thinking"}])


@pytest.mark.parametrize(
    "step_cls, extra",
    [
        (IntentClassified, {"intent": Intent.UNKNOWN, "matched_keywords": []}),
        (LLMDecision, {"iteration": 1, "decision": "handoff"}),
        (ToolCallStep, {"tool": "get_user", "args": {}}),
        (ToolResultStep, {"tool": "get_user", "ok": True}),
        (GroundingCheck, {"passed": True, "literals_checked": 0}),
        (FinalDecision, {"outcome": TicketStatus.REPLIED}),
    ],
)
def test_every_step_carries_seq_and_ts(step_cls: type, extra: dict) -> None:
    # Both are on every step because ordering and timing are properties of the trace as a
    # whole, not of any one kind of step.
    with pytest.raises(ValidationError):
        step_cls(ts=TS, **extra)
    with pytest.raises(ValidationError):
        step_cls(seq=1, **extra)


# --------------------------------------------------------------------------------------
# Violation -- serialised into the trace, so its JSON key matters.
# --------------------------------------------------------------------------------------


def test_violation_serialises_its_class_key_as_class() -> None:
    # `class` is a Python keyword, so the field is named literal_class; the trace JSON in
    # TRACEABILITY.md says "class" and that is the contract a reader sees.
    violation = Violation(literal="99.00", literal_class="number", reason="unsourced")
    assert violation.model_dump(by_alias=True)["class"] == "number"


def test_violation_parses_from_the_traces_own_json() -> None:
    violation = Violation.model_validate(
        {"literal": "99.00", "class": "number", "reason": "unsourced"}
    )
    assert violation.literal_class == "number"


# --------------------------------------------------------------------------------------
# TraceRecorder -- assigns seq, stamps ts from the injected clock (ADR 0008).
# --------------------------------------------------------------------------------------


def _invoices_result() -> ToolResult:
    """The three invoices the fixture gives u_002: one failed among two paid, newest
    first -- the rows the summary example in TRACEABILITY.md / API.md is drawn from."""
    return ToolResult(
        tool="get_invoices",
        records=[
            Invoice(
                invoice_id="inv_204", amount=Decimal("42.10"), currency="EUR",
                status=InvoiceStatus.FAILED, issued_at=datetime(2026, 8, 15, 9, tzinfo=UTC),
            ),
            Invoice(
                invoice_id="inv_203", amount=Decimal("38.90"), currency="EUR",
                status=InvoiceStatus.PAID, issued_at=datetime(2026, 7, 15, 9, tzinfo=UTC),
            ),
            Invoice(
                invoice_id="inv_202", amount=Decimal("31.20"), currency="EUR",
                status=InvoiceStatus.PAID, issued_at=datetime(2026, 6, 15, 9, tzinfo=UTC),
            ),
        ],
    )


def test_recorder_assigns_seq_1_based_and_monotonic_across_step_types() -> None:
    rec = TraceRecorder("t_abc", FrozenClock())
    rec.intent_classified(Intent.BILLING_QUESTION, ["invoice"])
    rec.llm_decision(1, "tool_call", tool="get_user")
    rec.tool_call("get_user", {"user_id": "u_002"})
    rec.tool_result("get_user", summary={"found": True, "plan": "basic"})
    rec.grounding_check(3, [])
    rec.final_decision(TicketStatus.REPLIED)

    assert [s.seq for s in rec.steps] == [1, 2, 3, 4, 5, 6]
    assert [type(s) for s in rec.steps] == [
        IntentClassified,
        LLMDecision,
        ToolCallStep,
        ToolResultStep,
        GroundingCheck,
        FinalDecision,
    ]


def test_recorder_stamps_ts_from_the_injected_clock_only() -> None:
    # ADR 0008: nothing reads the wall clock. A known tick times a known number of steps
    # is arithmetic -- which is what makes pipeline_duration_seconds assertable.
    rec = TraceRecorder("t_abc", FrozenClock())
    rec.intent_classified(Intent.UNKNOWN, [])
    rec.final_decision(
        TicketStatus.HANDED_OFF,
        reason=HandoffReason.UNSUPPORTED_INTENT,
        detail="no keywords matched",
    )
    assert [s.ts for s in rec.steps] == [DEFAULT_START, DEFAULT_START + DEFAULT_TICK]


def test_recorder_ts_strictly_increases_with_seq() -> None:
    rec = TraceRecorder("t_abc", FrozenClock())
    for i in range(1, 6):
        rec.llm_decision(i, "tool_call", tool="get_user")
    steps = rec.steps
    assert [s.seq for s in steps] == [1, 2, 3, 4, 5]
    assert all(b.ts > a.ts for a, b in zip(steps, steps[1:], strict=False))


def test_tool_result_ok_is_derived_from_the_absence_of_an_error() -> None:
    rec = TraceRecorder("t_abc", FrozenClock())
    rec.tool_result("get_invoices", summary={"count": 0, "statuses": {}, "referenced": []})
    rec.tool_result(
        "get_invoices", error=ToolError(type="NoDataAvailable", message="none")
    )
    ok_step, err_step = rec.steps
    assert ok_step.ok is True and ok_step.error is None
    assert err_step.ok is False and err_step.summary is None


def test_grounding_check_passed_follows_from_whether_violations_is_empty() -> None:
    rec = TraceRecorder("t_abc", FrozenClock())
    rec.grounding_check(3, [])
    rec.grounding_check(
        4, [Violation(literal="99.00", literal_class="number", reason="unsourced")]
    )
    clean, dirty = rec.steps
    assert clean.passed is True
    assert dirty.passed is False
    assert dirty.violations[0].literal == "99.00"


def test_recorder_rejects_an_incoherent_final_decision() -> None:
    # The recorder builds a FinalDecision, whose model validator enforces the
    # outcome/reason invariant -- a handoff without a reason cannot be recorded.
    rec = TraceRecorder("t_abc", FrozenClock())
    with pytest.raises(ValidationError):
        rec.final_decision(TicketStatus.HANDED_OFF)


def test_recorded_steps_round_trip_through_the_trace_union() -> None:
    # STORAGE.md persists the trace as JSON; reading a ticket back has to reconstruct the
    # concrete step classes, not an untyped dict.
    rec = TraceRecorder("t_abc", FrozenClock())
    rec.intent_classified(Intent.BILLING_QUESTION, ["invoice"])
    rec.llm_decision(1, "reply")
    rec.tool_call("get_invoices", {"user_id": "u_002"})
    rec.tool_result("get_invoices", summary=summarise(_invoices_result()))
    rec.grounding_check(3, [])
    rec.final_decision(TicketStatus.REPLIED)

    dumped = [s.model_dump(mode="json") for s in rec.steps]
    assert STEPS.validate_python(dumped) == rec.steps


# --------------------------------------------------------------------------------------
# summarise -- per-tool tool-result summary: counts, status distribution, identifiers.
# --------------------------------------------------------------------------------------


def test_summarise_get_user_reports_found_and_plan() -> None:
    result = ToolResult(
        tool="get_user",
        records=[User(user_id="u_002", name="Ben Carter", language="en", plan="basic")],
    )
    assert summarise(result) == {"found": True, "plan": "basic"}


def test_summarise_get_invoices_counts_and_distributes_over_status() -> None:
    summary = summarise(_invoices_result())
    assert summary == {
        "count": 3,
        "statuses": {"paid": 2, "failed": 1},
        "referenced": ["inv_204", "inv_203", "inv_202"],
    }
    # Ordered by enum declaration, not row order (the rows lead with the failed one), so
    # the distribution is stable to serialise and to assert on.
    assert list(summary["statuses"]) == ["paid", "failed"]


def test_summarise_narrows_referenced_to_the_supplied_identifiers() -> None:
    # In the loop there is no reply yet, so referenced lists every returned id; once a
    # reply is rendered the pipeline passes the ids it actually used to narrow it.
    summary = summarise(_invoices_result(), referenced={"inv_204"})
    assert summary["referenced"] == ["inv_204"]
    assert summary["count"] == 3


def test_summarise_get_charging_sessions_distributes_over_session_status() -> None:
    result = ToolResult(
        tool="get_charging_sessions",
        records=[
            ChargingSession(
                session_id="sess_3002", station="Lyon Part-Dieu", kwh=Decimal("6.20"),
                cost=Decimal("2.48"), status=SessionStatus.INTERRUPTED,
                started_at=datetime(2026, 8, 22, 14, 5, tzinfo=UTC),
            ),
            ChargingSession(
                session_id="sess_3001", station="Lyon Confluence", kwh=Decimal("28.40"),
                cost=Decimal("11.36"), status=SessionStatus.COMPLETED,
                started_at=datetime(2026, 8, 9, 11, 20, tzinfo=UTC),
            ),
        ],
    )
    assert summarise(result) == {
        "count": 2,
        "statuses": {"completed": 1, "interrupted": 1},
        "referenced": ["sess_3002", "sess_3001"],
    }


def test_summarise_rejects_a_tool_it_has_no_rule_for() -> None:
    # "per tool" (TRACEABILITY.md): a fourth tool added without a summariser fails loudly
    # rather than silently producing an empty summary.
    with pytest.raises(ValueError, match="no summariser"):
        summarise(ToolResult(tool="get_refunds", records=[]))


# --------------------------------------------------------------------------------------
# as_tool_error -- maps a caught exception to the ToolError the failed step carries.
# --------------------------------------------------------------------------------------


def test_as_tool_error_records_the_exception_class_and_message() -> None:
    err = as_tool_error(NoDataAvailable("user u_004 has no invoices"))
    assert err == ToolError(type="NoDataAvailable", message="user u_004 has no invoices")


def test_as_tool_error_maps_an_unanticipated_exception_too() -> None:
    # ADR 0010: ToolError records *any* exception that reached the call site, not only the
    # three typed tool failures -- the catch in the loop is deliberately broad.
    err = as_tool_error(RuntimeError("connection reset"))
    assert err.type == "RuntimeError"
    assert err.message == "connection reset"
