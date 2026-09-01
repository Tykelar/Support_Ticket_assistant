"""The third test the brief requires: a runaway loop must hit the cap.

```bash
pytest tests/test_iteration_cap.py -v
```

Self-contained on purpose -- TESTS.md names this file so it can be run in isolation, and
a reviewer opening it should see the whole argument without following imports into a
shared harness.

The argument is that the cap is a **real** guardrail here rather than a formality. Under a
plan-then-execute design the iteration count is known before the loop starts and a cap can
never fire. Because the model sees prior results before choosing its next action
([ADR 0002](../docs/adr/0002-true-agentic-tool-calling-loop.md)), it can genuinely loop
forever -- so the stub below does exactly that, and nothing but `MAX_ITERATIONS` stops it.

The assertion is on the **exact count**, not just the outcome. An off-by-one that ran six
times would still produce the right status and would pass a weaker test.
"""

from datetime import UTC, datetime

from support_assistant.clock import FrozenClock
from support_assistant.domain import (
    Classification,
    HandoffReason,
    Intent,
    Observation,
    Step,
    Ticket,
    TicketStatus,
    ToolCall,
    new_ticket_id,
)
from support_assistant.guardrails.limits import MAX_ITERATIONS
from support_assistant.pipeline.orchestrator import run_pipeline
from support_assistant.storage.memory import InMemoryTicketRepository
from support_assistant.tracing.models import LLMDecision

_NOW = datetime(2026, 8, 31, 10, 14, tzinfo=UTC)


class RunawayLLM:
    """Always asks for another tool call. Never replies, never hands off.

    A legal `LLMClient`: every step it returns is one the pipeline would happily execute,
    and each call succeeds. Nothing about any individual iteration is wrong -- which is
    the point. The failure is only visible in the aggregate, and only the cap sees it.
    """

    def classify_intent(self, ticket: Ticket) -> Classification:
        return Classification(intent=Intent.BILLING_QUESTION, matched_keywords=("invoice",))

    def decide_next_step(self, ticket: Ticket, history: list[Observation]) -> Step:
        return ToolCall(tool="get_user", args={"user_id": ticket.user_id})


def test_runaway_loop_hits_cap() -> None:
    repository = InMemoryTicketRepository(FrozenClock())
    ticket = Ticket(
        id=new_ticket_id(),
        user_id="u_002",
        subject="My payment failed",
        body="Why was my last invoice not paid?",
        created_at=_NOW,
        updated_at=_NOW,
    )
    repository.create(ticket)

    # No timeout, no polling: the loop is bounded structurally, so if this test hangs the
    # guardrail is gone and the hang is the failure.
    run_pipeline(
        ticket.id,
        repository=repository,
        llm=RunawayLLM(),
        clock=FrozenClock(),
    )

    stored = repository.get(ticket.id)
    assert stored is not None
    assert stored.status is TicketStatus.HANDED_OFF
    assert stored.handoff_reason is HandoffReason.ITERATION_CAP_EXCEEDED
    assert stored.reply is None

    decisions = [step for step in stored.trace if isinstance(step, LLMDecision)]
    assert len(decisions) == MAX_ITERATIONS
    assert [step.iteration for step in decisions] == list(range(1, MAX_ITERATIONS + 1))
