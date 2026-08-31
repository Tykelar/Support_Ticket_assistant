"""`llm.templates` -- the five reply bodies, `render()`, and `TEMPLATE_SAFE_LITERALS`.

Two things are checked: that each template renders real facts into prose, and -- the one
that matters -- that a rendered reply never trips grounding layer 2. Layer 1 (rendering
only from the `FactSet`) and layer 2 (re-reading the finished text) are built to agree;
this is the test that proves they do (GUARDRAILS.md section 3).
"""

import pytest

from support_assistant.domain import Observation, ToolCall
from support_assistant.enums import ReplyTemplate
from support_assistant.guardrails.checker import GroundingChecker
from support_assistant.guardrails.factset import FactSet
from support_assistant.llm import templates
from support_assistant.tools import registry

# Which fixture user drives each template to be the chosen one (see FakeLLM._template_for).
_USER_FOR = {
    ReplyTemplate.BILLING_ALL_PAID: ("u_001", "get_invoices"),
    ReplyTemplate.BILLING_FAILED: ("u_002", "get_invoices"),
    ReplyTemplate.BILLING_PENDING: ("u_003", "get_invoices"),
    ReplyTemplate.SESSION_COMPLETED: ("u_001", "get_charging_sessions"),
    ReplyTemplate.SESSION_INTERRUPTED: ("u_003", "get_charging_sessions"),
}


def _facts(user_id: str, data_tool: str) -> FactSet:
    history = [
        Observation(
            step=ToolCall(tool=tool, args={"user_id": user_id}),
            result=registry.run(tool, {"user_id": user_id}),
        )
        for tool in ("get_user", data_tool)
    ]
    return FactSet.from_observations(history)


@pytest.mark.parametrize("template", list(ReplyTemplate))
def test_every_template_renders_without_leftover_placeholders(template: ReplyTemplate) -> None:
    user_id, data_tool = _USER_FOR[template]
    reply = templates.render(template, _facts(user_id, data_tool))

    assert reply.strip()
    assert "{" not in reply and "}" not in reply


def test_billing_failed_names_the_real_invoice_and_amount() -> None:
    reply = templates.render(ReplyTemplate.BILLING_FAILED, _facts("u_002", "get_invoices"))
    assert "inv_204" in reply
    assert "42.10" in reply
    assert "Ben" in reply


@pytest.mark.parametrize("template", list(ReplyTemplate))
def test_a_rendered_reply_never_trips_grounding(template: ReplyTemplate) -> None:
    user_id, data_tool = _USER_FOR[template]
    facts = _facts(user_id, data_tool)

    reply = templates.render(template, facts)
    violations = GroundingChecker.verify(reply, facts, templates.spec_for(template))

    assert violations == [], f"{template} rendered an ungrounded literal: {violations}"


@pytest.mark.parametrize("template", list(ReplyTemplate))
def test_spec_for_exposes_a_frozen_safe_literal_set(template: ReplyTemplate) -> None:
    spec = templates.spec_for(template)
    assert isinstance(spec.TEMPLATE_SAFE_LITERALS, frozenset)


def test_only_billing_pending_declares_a_static_literal() -> None:
    # "within 3 business days" is the single static number in the whole template surface.
    safe = {t: templates.spec_for(t).TEMPLATE_SAFE_LITERALS for t in ReplyTemplate}
    assert safe.pop(ReplyTemplate.BILLING_PENDING) == frozenset({"3"})
    assert all(literals == frozenset() for literals in safe.values())
