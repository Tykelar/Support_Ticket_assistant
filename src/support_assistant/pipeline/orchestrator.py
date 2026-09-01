"""The orchestrator: one ticket, from `processing` to a terminal state.

This is the component the brief is actually evaluating -- "the system around the model:
the loop, the guardrails, the failure handling". The control flow is PIPELINE.md's, and
the ordering of the guardrails is the contract.

**It is the only component permitted to decide a terminal outcome**
([ADR 0005](../../../docs/adr/0005-fail-closed-to-human-handoff.md)). Tools raise, the LLM
proposes, guardrails report -- this module decides, and `finish_replied` / `finish_handoff`
are the only two functions that write one. Each records the `final_decision` step and
persists it with the trace in a single call, so a terminal ticket without a final decision
is structurally impossible rather than merely unlikely.

Everything it needs is injected: the repository, the LLM client, the tool registry, the
clock, the template resolver, and the cap. No module-level singletons and no ambient time
([ADR 0008](../../../docs/adr/0008-injected-clock-with-advancing-test-double.md)), so a
test assembles a pipeline from an in-memory repository, a frozen clock, and a client that
misbehaves on purpose.

This is also the one module that imports `llm/`, `tools/` and `guardrails/` together --
they know nothing about each other and meet here (ARCHITECTURE.md section 3).
"""

from collections.abc import Callable
from typing import Any, Protocol

from support_assistant.clock import Clock
from support_assistant.domain import (
    Handoff,
    HandoffReason,
    Intent,
    Observation,
    Reply,
    ReplyTemplate,
    TicketStatus,
    ToolCall,
    ToolResult,
)
from support_assistant.guardrails.factset import FactSet
from support_assistant.guardrails.grounding import GroundingChecker
from support_assistant.llm.protocol import LLMClient
from support_assistant.llm.templates import Template, spec_for
from support_assistant.pipeline.config import MAX_ITERATIONS
from support_assistant.storage.protocol import TicketRepository
from support_assistant.tools import registry
from support_assistant.tools.errors import NoDataAvailable, UserNotFound
from support_assistant.tracing.models import Violation
from support_assistant.tracing.recorder import TraceRecorder, as_tool_error
from support_assistant.tracing.summarise import summarise


class ToolRunner(Protocol):
    """The slice of `tools.registry` the loop uses. Injected so a test can supply a
    registry that misbehaves; the default is the real one."""

    def run(self, name: str, args: dict[str, Any]) -> ToolResult: ...


def run_pipeline(
    ticket_id: str,
    *,
    repository: TicketRepository,
    llm: LLMClient,
    clock: Clock,
    tools: ToolRunner = registry,  # type: ignore[assignment]  # a module satisfies it
    resolve_template: Callable[[ReplyTemplate], Template] = spec_for,
    max_iterations: int = MAX_ITERATIONS,
) -> None:
    """Run one ticket to a terminal state. Returns nothing: the outcome is the persisted
    ticket, not a value a caller could forget to check.

    Raises `ValueError` if the id is unknown. That is not a handoff -- the API creates the
    ticket before scheduling the run, so an unknown id is a bug to surface, and there is
    no ticket to carry a reason anyway.
    """
    ticket = repository.get(ticket_id)
    if ticket is None:
        raise ValueError(f"no ticket {ticket_id!r} to run")

    trace = TraceRecorder(clock)
    in_flight: str | None = None
    """Which tool is executing, for the detail on a typed tool failure. The trace already
    names it; the `final_decision` detail says which incident, not just which category."""

    try:
        # 1. Classify -------------------------------------------------------------------
        classification = llm.classify_intent(ticket)
        trace.intent_classified(classification.intent, list(classification.matched_keywords))
        if classification.intent is Intent.UNKNOWN:
            return finish_handoff(
                repository,
                trace,
                ticket_id,
                HandoffReason.UNSUPPORTED_INTENT,
                _unsupported_detail(classification.matched_keywords),
            )

        # 2. Loop -- bounded, agentic ---------------------------------------------------
        history: list[Observation] = []
        draft: Reply | None = None
        for iteration in range(1, max_iterations + 1):
            step = llm.decide_next_step(ticket, history)
            # tracing/ cannot import llm/, so the orchestrator adapts its own step. Only
            # a ToolCall carries a `tool`, which is exactly what LLMDecision's validator
            # requires (domain.py).
            trace.llm_decision(iteration, step.decision, tool=getattr(step, "tool", None))

            match step:
                case ToolCall():
                    in_flight = step.tool
                    trace.tool_call(step.tool, step.args)
                    try:
                        result = tools.run(step.tool, step.args)
                    except Exception as exc:  # record, then re-raise (ADR 0010)
                        # Without this the ok:false step -- the first thing an agent reads
                        # when a ticket went wrong -- would be written by nothing, because
                        # a raising tool jumps straight past the line below.
                        trace.tool_result(step.tool, error=as_tool_error(exc))
                        raise
                    trace.tool_result(step.tool, summarise(result))
                    # Only successful observations accumulate, so the model never sees an
                    # error and never gets to work around one. Fail closed (PIPELINE.md).
                    history.append(Observation(step=step, result=result))
                    in_flight = None
                case Reply():
                    draft = step
                    break
                case Handoff():
                    return finish_handoff(
                        repository, trace, ticket_id, step.reason, _gave_up_detail(step.reason)
                    )
        else:
            # Reached only when the loop finished without `break` -- the cap is structural,
            # not an `if` someone can forget to write. There is no path from here to a
            # reply (GUARDRAILS.md section 1).
            return finish_handoff(
                repository,
                trace,
                ticket_id,
                HandoffReason.ITERATION_CAP_EXCEEDED,
                f"the loop reached MAX_ITERATIONS ({max_iterations}) without terminating",
            )

        # 3. Ground and verify ----------------------------------------------------------
        assert draft is not None  # the loop breaks only on a Reply
        facts = FactSet.from_observations(history)
        template = resolve_template(draft.template)  # one spec both renders and is checked
        reply = template.render(facts)
        violations = GroundingChecker.verify(reply, facts, template)
        trace.grounding_check(len(GroundingChecker.extract(reply, facts)), violations)
        if violations:
            return finish_handoff(
                repository,
                trace,
                ticket_id,
                HandoffReason.UNGROUNDED_REPLY,
                _violation_detail(violations),
            )

        return finish_replied(repository, trace, ticket_id, reply)

    except UserNotFound as exc:
        return finish_handoff(
            repository, trace, ticket_id, HandoffReason.USER_NOT_FOUND, _tool_detail(in_flight, exc)
        )
    except NoDataAvailable as exc:
        return finish_handoff(
            repository, trace, ticket_id, HandoffReason.DATA_NOT_FOUND, _tool_detail(in_flight, exc)
        )
    except Exception as exc:  # deliberate catch-all -- ADR 0005
        # "Any step fails" is one of the brief's three handoff triggers, and an
        # unanticipated exception is exactly the case that cannot be enumerated. Without
        # this a bug leaves a ticket in `processing` forever -- a state with no owner and
        # no alarm. Last clause, so the typed failures above keep their specific reasons.
        return finish_handoff(
            repository, trace, ticket_id, HandoffReason.TOOL_ERROR, repr(exc)
        )


# --------------------------------------------------------------------------------------
# The two exits
#
# Every path above converges on one of these, and nothing else writes a terminal state.
# That is what makes requirement 5 -- a persisted trace explaining every outcome -- hold
# by construction rather than by discipline.
#
# The outcome metric each of these will emit lands with `observability/` (phase 9); the
# trace step and the persisted state are here now.
# --------------------------------------------------------------------------------------


def finish_replied(
    repository: TicketRepository, trace: TraceRecorder, ticket_id: str, reply: str
) -> None:
    """The reply was rendered from the facts and passed grounding. Send it."""
    trace.final_decision(TicketStatus.REPLIED)
    repository.finalise(ticket_id, TicketStatus.REPLIED, reply, None, trace.steps)


def finish_handoff(
    repository: TicketRepository,
    trace: TraceRecorder,
    ticket_id: str,
    reason: HandoffReason,
    detail: str,
) -> None:
    """A human takes the ticket. No reply -- not an empty string, not a holding message
    (ADR 0005).

    `detail` is required rather than optional: a reason explains the category, and the
    detail explains the incident -- which user id was missing, which tool raised, which
    literal was ungrounded (GUARDRAILS.md).
    """
    trace.final_decision(TicketStatus.HANDED_OFF, reason=reason, detail=detail)
    repository.finalise(ticket_id, TicketStatus.HANDED_OFF, None, reason, trace.steps)


# --------------------------------------------------------------------------------------
# Handoff details -- one per reason, so the trace says what happened and not just what
# kind of thing happened
# --------------------------------------------------------------------------------------


def _unsupported_detail(matched_keywords: tuple[str, ...]) -> str:
    if not matched_keywords:
        return "intent classified unknown: nothing in the ticket matched a supported category"
    return (
        "intent classified unknown: an ambiguous match on "
        f"{', '.join(matched_keywords)}"
    )


def _gave_up_detail(reason: HandoffReason) -> str:
    return f"the model ended the run itself, giving {reason.value}"


def _tool_detail(in_flight: str | None, exc: Exception) -> str:
    """The failing tool and its message. The tool name comes from the loop rather than
    from the exception, because the exceptions carry a user id and a noun, not a tool."""
    return f"{in_flight}: {exc}" if in_flight else str(exc)


def _violation_detail(violations: list[Violation]) -> str:
    """The offending literals themselves. The `grounding_check` step holds the full
    evidence; this is the one line a reader sees first."""
    offenders = ", ".join(f"{v.literal} ({v.literal_class.value})" for v in violations)
    return f"reply withheld -- unsourced literals: {offenders}"
