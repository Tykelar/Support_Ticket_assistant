"""The `LLMClient` protocol -- the seam every implementation sits behind.

Two methods. `classify_intent` runs once before the loop; `decide_next_step` runs once
per iteration and sees the results of the previous tool calls, which is the whole
difference between an agentic loop and a fixed plan (ADR 0002, ADR 0006).

The decision types (`ToolCall`, `Reply`, `Handoff`) and `Observation` are in
`support_assistant.domain`, not here: `guardrails/` consumes `Observation` and may not
import `llm/` (ARCHITECTURE.md section 3).
"""

from typing import Protocol

from support_assistant.domain import Intent, Observation, Step, Ticket


class LLMClient(Protocol):
    """What the orchestrator depends on. `FakeLLM` is the default implementation; a real
    provider (`OllamaLLM`, optional) sits behind the identical interface."""

    def classify_intent(self, ticket: Ticket) -> Intent:
        """The kind of question the ticket is asking. `Intent.UNKNOWN` is a handoff
        trigger the pipeline acts on before entering the loop."""
        ...

    def decide_next_step(self, ticket: Ticket, history: list[Observation]) -> Step:
        """The next action given what has been gathered so far: a `ToolCall` to gather
        more, a `Reply` to finish, or a `Handoff` to give up. `Reply` and `Handoff` are
        terminal."""
        ...
