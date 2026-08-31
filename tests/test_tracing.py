"""Trace step models -- the shapes TRACEABILITY.md specifies.

The TraceRecorder, seq/ts ordering and the summarisation rules arrive with phase 4; this
file covers the models those will produce, and grows rather than being replaced.
"""

from datetime import UTC, datetime

import pytest
from pydantic import TypeAdapter, ValidationError

from support_assistant.domain import Intent, TicketStatus
from support_assistant.guardrails.grounding import Violation
from support_assistant.guardrails.handoff import HandoffReason
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
