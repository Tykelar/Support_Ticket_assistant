"""`FakeLLM` -- the default `LLMClient`. Deterministic: no clock, no randomness, no
network, so the same ticket always produces the same trace (ADR 0006).

Intent is case-insensitive keyword matching over `subject + body`, scored by distinct
keywords hit; a tie, including nothing matched, is `Intent.UNKNOWN`. Keyword matching is
brittle by design and is not defended as good classification (LLM.md) -- it is a
deterministic stand-in so the loop and the guardrails can be evaluated on their own.

The step decision is a state machine over the history: fetch the user, then the intent's
data tool, then reply with the template the gathered statuses imply. At most three
iterations, and never `Handoff` -- that exists for a real model, not for the fake.
"""

import re

from support_assistant.domain import (
    Classification,
    Intent,
    InvoiceStatus,
    Observation,
    Reply,
    ReplyTemplate,
    SessionStatus,
    Step,
    Ticket,
    ToolCall,
    ToolResult,
)
from support_assistant.tools import registry

_BILLING_KEYWORDS = (
    "invoice", "bill", "billing", "payment", "paid", "charge", "charged",
    "refund", "receipt", "cost", "price", "euro",
)
_CHARGING_KEYWORDS = (
    "charging", "charger", "station", "session", "kwh", "plug", "connector",
    "stopped", "interrupted",
)


def _pattern(keywords: tuple[str, ...]) -> re.Pattern[str]:
    """Whole-word alternation, so "charge" does not fire inside "charging"."""
    return re.compile(r"\b(?:" + "|".join(re.escape(k) for k in keywords) + r")\b")


_BILLING = _pattern(_BILLING_KEYWORDS)
_CHARGING = _pattern(_CHARGING_KEYWORDS)

_USER_TOOL = "get_user"
_DATA_TOOL = {
    Intent.BILLING_QUESTION: "get_invoices",
    Intent.CHARGING_SESSION_PROBLEM: "get_charging_sessions",
}


class FakeLLM:
    """The deterministic default. Stateless -- construct with no arguments."""

    def classify_intent(self, ticket: Ticket) -> Classification:
        """The intent, and the keywords that produced it.

        The evidence is the winning category's hits only: listing the loser's would make
        the `intent_classified` step argue for a classification the fake did not make, and
        an `unknown` outcome has no evidence for it at all (ADR 0012).

        Sorted and de-duplicated, so the same ticket produces the same trace byte for byte,
        and so the score counts *distinct* keywords.
        """
        text = f"{ticket.subject}\n{ticket.body}".lower()
        billing = sorted(set(_BILLING.findall(text)))
        charging = sorted(set(_CHARGING.findall(text)))
        if len(billing) > len(charging):
            return Classification(
                intent=Intent.BILLING_QUESTION, matched_keywords=tuple(billing)
            )
        if len(charging) > len(billing):
            return Classification(
                intent=Intent.CHARGING_SESSION_PROBLEM, matched_keywords=tuple(charging)
            )
        # A tie is genuine ambiguity -- fail closed (LLM.md).
        return Classification(intent=Intent.UNKNOWN)

    def decide_next_step(self, ticket: Ticket, history: list[Observation]) -> Step:
        intent = self.classify_intent(ticket).intent
        if intent is Intent.UNKNOWN:
            raise ValueError(
                "decide_next_step called for an unknown intent; the pipeline hands "
                "those off before entering the loop"
            )

        called = {obs.step.tool for obs in history}
        if _USER_TOOL not in called:
            return self._tool_call(_USER_TOOL, ticket.user_id)

        data_tool = _DATA_TOOL[intent]
        if data_tool not in called:
            return self._tool_call(data_tool, ticket.user_id)

        return Reply(template=self._template_for(intent, history))

    @staticmethod
    def _tool_call(tool: str, user_id: str) -> ToolCall:
        """Build a call, checking the name against the registry so the registry stays the
        single source of truth for tool names."""
        if tool not in registry.registered():
            raise RuntimeError(f"FakeLLM would call unregistered tool {tool!r}")
        return ToolCall(tool=tool, args={"user_id": user_id})

    def _template_for(self, intent: Intent, history: list[Observation]) -> ReplyTemplate:
        records = self._result_for(history, _DATA_TOOL[intent]).records
        if intent is Intent.BILLING_QUESTION:
            statuses = {invoice.status for invoice in records}
            if InvoiceStatus.FAILED in statuses:
                return ReplyTemplate.BILLING_FAILED
            if InvoiceStatus.PENDING in statuses:
                return ReplyTemplate.BILLING_PENDING
            return ReplyTemplate.BILLING_ALL_PAID

        latest = records[0].status  # the loader returns sessions newest-first
        if latest is SessionStatus.COMPLETED:
            return ReplyTemplate.SESSION_COMPLETED
        return ReplyTemplate.SESSION_INTERRUPTED  # interrupted or failed

    @staticmethod
    def _result_for(history: list[Observation], tool: str) -> ToolResult:
        for obs in history:
            if obs.step.tool == tool:
                return obs.result
        raise RuntimeError(f"no {tool} observation in history")  # unreachable via the machine
