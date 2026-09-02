"""The shared vocabulary: enums, the fixture record models, and the Ticket invariant.

These are contracts several components depend on. ARCHITECTURE.md section 4 fixes the
enum members; STORAGE.md fixes the status/reply/handoff_reason invariant as a SQL CHECK
and this module holds the application-side half of it.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import TypeAdapter, ValidationError

from support_assistant.domain import (
    ChargingSession,
    Classification,
    Handoff,
    HandoffReason,
    Intent,
    Invoice,
    InvoiceStatus,
    Observation,
    Reply,
    ReplyTemplate,
    SessionStatus,
    Step,
    Ticket,
    TicketStatus,
    ToolCall,
    ToolResult,
    User,
    format_amount,
    new_ticket_id,
)
from support_assistant.enums import LiteralClass

_STEP = TypeAdapter(Step)

# --------------------------------------------------------------------------------------
# Enums -- the members are a cross-component contract, so they are pinned by value.
# --------------------------------------------------------------------------------------


def test_intent_members_match_the_briefs_categories() -> None:
    assert {i.value for i in Intent} == {
        "billing_question",
        "charging_session_problem",
        "unknown",
    }


def test_ticket_status_members_match_the_state_machine() -> None:
    assert {s.value for s in TicketStatus} == {"processing", "replied", "handed_off"}


def test_handoff_reason_members_match_the_closed_set() -> None:
    # ARCHITECTURE.md section 4. A member added without being wired is caught here first,
    # and by test_pipeline.py's reachability test once the orchestrator exists.
    assert {r.value for r in HandoffReason} == {
        "USER_NOT_FOUND",
        "DATA_NOT_FOUND",
        "UNSUPPORTED_INTENT",
        "TOOL_ERROR",
        "ITERATION_CAP_EXCEEDED",
        "UNGROUNDED_REPLY",
    }


def test_literal_class_members_match_the_traces_class_key() -> None:
    # TRACEABILITY.md's grounding_check JSON carries "class": "number". A closed enum for
    # the same reason SessionStatus is one -- the ROADMAP adds a fourth, `contradiction`,
    # with semantic grounding.
    assert {c.value for c in LiteralClass} == {"number", "identifier", "status"}


def test_handoff_reason_is_re_exported_by_domain() -> None:
    # It is one of ARCHITECTURE.md section 4's cross-system contracts, so it lives in
    # enums.py beside the other five and is re-exported here (ADR 0011).
    from support_assistant import domain, enums

    assert domain.HandoffReason is enums.HandoffReason
    assert "HandoffReason" in domain.__all__


def test_session_status_members_match_tools_md() -> None:
    assert {s.value for s in SessionStatus} == {"completed", "interrupted", "failed"}


def test_invoice_status_members_match_tools_md() -> None:
    assert {s.value for s in InvoiceStatus} == {"paid", "pending", "failed"}


@pytest.mark.parametrize(
    "enum_cls",
    [
        Intent,
        TicketStatus,
        SessionStatus,
        InvoiceStatus,
        HandoffReason,
        ReplyTemplate,
        LiteralClass,
    ],
)
def test_enums_serialise_as_their_string_value(enum_cls: type) -> None:
    # The API and the trace payloads are JSON; an enum that serialises as
    # "Intent.UNKNOWN" would leak Python into the contract.
    for member in enum_cls:
        assert str(member) == member.value


# --------------------------------------------------------------------------------------
# Ticket ids -- the only thing protecting a trace (API.md).
# --------------------------------------------------------------------------------------


def test_ticket_id_is_128_bits_of_hex_behind_a_prefix() -> None:
    assert new_ticket_id() != new_ticket_id()
    for _ in range(100):
        ticket_id = new_ticket_id()
        assert ticket_id.startswith("t_")
        hex_part = ticket_id.removeprefix("t_")
        assert len(hex_part) == 32, "128 bits is 32 hex characters"
        int(hex_part, 16)  # raises if it is not hex


def test_ticket_ids_do_not_collide() -> None:
    # Not a serious test of the entropy -- a guard against a sequence or a fixed prefix
    # sneaking in, which is what would make ids enumerable.
    assert len({new_ticket_id() for _ in range(1000)}) == 1000


# --------------------------------------------------------------------------------------
# How an amount is written -- shared by the FactSet helpers and the templates, so
# the text a reply prints and the text the facts list are the same string.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, written",
    [
        (Decimal("42.10"), "42.10"),
        (Decimal("42.1"), "42.10"),  # trailing zero restored
        (Decimal("6.2"), "6.20"),
        (Decimal("31"), "31.00"),
    ],
)
def test_format_amount_always_writes_two_decimal_places(value: Decimal, written: str) -> None:
    assert format_amount(value) == written


# --------------------------------------------------------------------------------------
# Fixture record models
# --------------------------------------------------------------------------------------


def test_user_accepts_a_well_formed_record() -> None:
    user = User(user_id="u_002", name="Ben Carter", language="en", plan="basic")
    assert user.name == "Ben Carter"


def test_invoice_keeps_the_amount_exact() -> None:
    # Grounding compares numeric literals as Decimals (ADR 0004). If "42.10" arrived as a
    # binary float it would render as 42.1 and the reply would state a number the fixture
    # does not contain.
    invoice = Invoice(
        invoice_id="inv_204",
        amount="42.10",
        currency="EUR",
        status=InvoiceStatus.FAILED,
        issued_at=datetime(2026, 8, 15, tzinfo=UTC),
    )
    assert invoice.amount == Decimal("42.10")
    assert str(invoice.amount) == "42.10", "trailing zero is part of the fact"


def test_invoice_rejects_a_non_numeric_amount() -> None:
    # This is exactly u_006's malformed row; the tool must fail rather than coerce.
    with pytest.raises(ValidationError):
        Invoice(
            invoice_id="inv_601",
            amount="not-a-number",
            currency="EUR",
            status=InvoiceStatus.PAID,
            issued_at=datetime(2026, 8, 1, tzinfo=UTC),
        )


def test_invoice_rejects_a_status_outside_the_vocabulary() -> None:
    # The status vocabulary is closed so that grounding layer 2 can check status words
    # against it (GUARDRAILS.md).
    with pytest.raises(ValidationError):
        Invoice(
            invoice_id="inv_204",
            amount="42.10",
            currency="EUR",
            status="overdue",
            issued_at=datetime(2026, 8, 15, tzinfo=UTC),
        )


def test_charging_session_accepts_a_well_formed_record() -> None:
    session = ChargingSession(
        session_id="sess_3001",
        station="Lyon Part-Dieu",
        kwh="6.2",
        cost="2.48",
        status=SessionStatus.INTERRUPTED,
        started_at=datetime(2026, 8, 22, 14, 5, tzinfo=UTC),
    )
    assert session.kwh == Decimal("6.2")
    assert session.status is SessionStatus.INTERRUPTED


def test_charging_session_rejects_a_status_outside_the_vocabulary() -> None:
    with pytest.raises(ValidationError):
        ChargingSession(
            session_id="sess_3001",
            station="Lyon Part-Dieu",
            kwh="6.2",
            cost="2.48",
            status="aborted",
            started_at=datetime(2026, 8, 22, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    "model_cls, row",
    [
        (
            User,
            {
                "user_id": "u_002",
                "name": "Ben",
                "language": "en",
                "plan": "basic",
                "user_id_typo": "u_002",
            },
        ),
        (
            Invoice,
            {
                "invoice_id": "inv_204",
                "amount": "42.10",
                "currency": "EUR",
                "status": "failed",
                "issued_at": "2026-08-15T00:00:00Z",
                "user_id": "u_002",
            },
        ),
    ],
)
def test_record_models_reject_unexpected_fields(model_cls: type, row: dict) -> None:
    # TOOLS.md fixes each model's field list. A stray key is a malformed fixture, which
    # is a TOOL_ERROR handoff -- not something to silently ignore. Note the Invoice case:
    # `user_id` is the fixture's filter key and must be stripped before validation.
    with pytest.raises(ValidationError):
        model_cls(**row)


# --------------------------------------------------------------------------------------
# The Ticket invariant -- the application half of STORAGE.md's table CHECK.
# --------------------------------------------------------------------------------------


def _ticket(**overrides: object) -> Ticket:
    base: dict[str, object] = {
        "id": new_ticket_id(),
        "user_id": "u_002",
        "subject": "My payment failed",
        "body": "I got an email saying my last invoice could not be charged.",
        "created_at": datetime(2026, 8, 31, 10, 14, tzinfo=UTC),
        "updated_at": datetime(2026, 8, 31, 10, 14, tzinfo=UTC),
    }
    return Ticket(**(base | overrides))


def test_a_new_ticket_is_processing_with_neither_field_set() -> None:
    ticket = _ticket()
    assert ticket.status is TicketStatus.PROCESSING
    assert ticket.reply is None
    assert ticket.handoff_reason is None
    assert ticket.trace == []


def test_a_replied_ticket_carries_a_reply_and_no_reason() -> None:
    ticket = _ticket(status=TicketStatus.REPLIED, reply="Hi Ben, invoice inv_204 failed.")
    assert ticket.reply is not None
    assert ticket.handoff_reason is None


def test_a_handed_off_ticket_carries_a_reason_and_no_reply() -> None:
    ticket = _ticket(
        status=TicketStatus.HANDED_OFF, handoff_reason=HandoffReason.USER_NOT_FOUND
    )
    assert ticket.reply is None
    assert ticket.handoff_reason is HandoffReason.USER_NOT_FOUND


@pytest.mark.parametrize(
    "overrides, why",
    [
        ({"status": TicketStatus.REPLIED}, "replied without a reply"),
        (
            {
                "status": TicketStatus.REPLIED,
                "reply": "ok",
                "handoff_reason": HandoffReason.TOOL_ERROR,
            },
            "replied with a handoff reason as well",
        ),
        ({"status": TicketStatus.HANDED_OFF}, "handed off without a reason"),
        (
            {
                "status": TicketStatus.HANDED_OFF,
                "handoff_reason": HandoffReason.TOOL_ERROR,
                "reply": "We are looking into it.",
            },
            "handed off with a holding message",
        ),
        (
            {
                "status": TicketStatus.HANDED_OFF,
                "handoff_reason": HandoffReason.TOOL_ERROR,
                "reply": "",
            },
            "handed off with an empty-string reply",
        ),
        ({"status": TicketStatus.PROCESSING, "reply": "too early"}, "processing with a reply"),
        (
            {"status": TicketStatus.PROCESSING, "handoff_reason": HandoffReason.TOOL_ERROR},
            "processing with a reason",
        ),
    ],
)
def test_ticket_rejects_states_the_schema_check_would_reject(
    overrides: dict, why: str
) -> None:
    # Mirrors the three-way CHECK in STORAGE.md. The database is the backstop; this is
    # the layer that stops the bad write being attempted at all.
    with pytest.raises(ValidationError):
        _ticket(**overrides)


def test_handed_off_reply_is_none_not_empty_string() -> None:
    # Called out separately because "" is the tempting shortcut and ADR 0005 forbids it:
    # an empty reply is indistinguishable from a reply that failed to render.
    ticket = _ticket(
        status=TicketStatus.HANDED_OFF, handoff_reason=HandoffReason.DATA_NOT_FOUND
    )
    assert ticket.reply is None
    assert ticket.reply != ""


# --------------------------------------------------------------------------------------
# Input limits -- API.md's request rules, enforced on the domain model too.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["subject", "body"])
def test_ticket_rejects_an_empty_text_field(field: str) -> None:
    with pytest.raises(ValidationError):
        _ticket(**{field: ""})


def test_ticket_rejects_an_oversized_subject() -> None:
    with pytest.raises(ValidationError):
        _ticket(subject="x" * 201)


def test_ticket_rejects_an_oversized_body() -> None:
    with pytest.raises(ValidationError):
        _ticket(body="x" * 5001)


def test_ticket_accepts_the_limits_exactly() -> None:
    ticket = _ticket(subject="x" * 200, body="y" * 5000)
    assert len(ticket.subject) == 200
    assert len(ticket.body) == 5000


# --------------------------------------------------------------------------------------
# What the model decides -- ToolCall | Reply | Handoff, and the Observation that pairs a
# tool call with its result. LLM.md and PIPELINE.md; the union return type of
# LLMClient.decide_next_step.
# --------------------------------------------------------------------------------------


def test_reply_template_members_match_llm_md() -> None:
    assert {t.value for t in ReplyTemplate} == {
        "billing_all_paid",
        "billing_failed",
        "billing_pending",
        "session_completed",
        "session_interrupted",
    }


def test_only_a_tool_call_carries_a_tool_name() -> None:
    # This is the exact contract PIPELINE.md relies on:
    #   tool = step.tool if isinstance(step, ToolCall) else None
    # and LLMDecision's validator (tool named iff decision == "tool_call").
    steps = [
        ToolCall(tool="get_user", args={"user_id": "u_002"}),
        Reply(template=ReplyTemplate.BILLING_ALL_PAID),
        Handoff(reason=HandoffReason.UNSUPPORTED_INTENT),
    ]

    narrowed = [(s.decision, s.tool if isinstance(s, ToolCall) else None) for s in steps]

    assert narrowed == [("tool_call", "get_user"), ("reply", None), ("handoff", None)]
    assert not any(hasattr(s, "tool") for s in steps if not isinstance(s, ToolCall))


def test_the_step_union_discriminates_on_decision() -> None:
    raw = [
        {"decision": "tool_call", "tool": "get_user", "args": {"user_id": "u_002"}},
        {"decision": "reply", "template": "billing_failed"},
        {"decision": "handoff", "reason": "TOOL_ERROR"},
    ]
    steps = [_STEP.validate_python(entry) for entry in raw]
    assert [type(s) for s in steps] == [ToolCall, Reply, Handoff]
    assert steps[1].template is ReplyTemplate.BILLING_FAILED
    assert steps[2].reason is HandoffReason.TOOL_ERROR


def test_reply_rejects_a_template_outside_the_vocabulary() -> None:
    with pytest.raises(ValidationError):
        Reply(template="billing_overdue")


def test_handoff_rejects_a_reason_outside_the_enum() -> None:
    with pytest.raises(ValidationError):
        Handoff(reason="ESCALATED")


def test_decision_types_are_frozen() -> None:
    tool_call = ToolCall(tool="get_user", args={"user_id": "u_002"})
    with pytest.raises(ValidationError):
        tool_call.tool = "get_invoices"


def test_observation_pairs_a_tool_call_with_its_result() -> None:
    obs = Observation(
        step=ToolCall(tool="get_user", args={"user_id": "u_002"}),
        result=ToolResult(
            tool="get_user",
            records=[User(user_id="u_002", name="Ben Carter", language="en", plan="basic")],
        ),
    )
    assert obs.step.tool == "get_user"
    assert obs.result.records[0].name == "Ben Carter"


def test_observation_rejects_a_terminal_step() -> None:
    # Reply and Handoff end the loop; only a ToolCall ever yields an observation
    # (PIPELINE.md appends one solely in the `case ToolCall()` branch).
    with pytest.raises(ValidationError):
        Observation(
            step=Reply(template=ReplyTemplate.BILLING_ALL_PAID),
            result=ToolResult(tool="get_user", records=[]),
        )


# --------------------------------------------------------------------------------------
# What the model classifies -- Classification, the return of LLMClient.classify_intent.
# ADR 0012: the evidence travels with the intent, because the trace step carries both and
# only the classifier knows the second half.
# --------------------------------------------------------------------------------------


def test_a_classification_carries_the_intent_and_its_evidence() -> None:
    classification = Classification(
        intent=Intent.BILLING_QUESTION, matched_keywords=("invoice", "payment")
    )
    assert classification.intent is Intent.BILLING_QUESTION
    assert classification.matched_keywords == ("invoice", "payment")


def test_a_classification_may_carry_no_evidence() -> None:
    # A real provider has no "matched keywords" to give; the field defaults to empty so
    # the protocol does not oblige an implementation to invent evidence (ADR 0012).
    assert Classification(intent=Intent.UNKNOWN).matched_keywords == ()


def test_a_classification_is_frozen() -> None:
    classification = Classification(intent=Intent.UNKNOWN)
    with pytest.raises(ValidationError):
        classification.intent = Intent.BILLING_QUESTION


def test_a_classification_rejects_an_intent_outside_the_enum() -> None:
    with pytest.raises(ValidationError):
        Classification(intent="refund_request")
