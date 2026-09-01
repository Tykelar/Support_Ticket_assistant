"""The orchestrator: one ticket, from `processing` to a terminal state.

This is the component the brief is actually evaluating -- "the system around the model:
the loop, the guardrails, the failure handling". The control flow is PIPELINE.md's, and
the ordering of the guardrails is the contract.

**It is the only component permitted to decide a terminal outcome**
([ADR 0005](../../../docs/adr/0005-fail-closed-to-human-handoff.md)). Tools raise, the LLM
proposes, guardrails report -- this module decides.

The run is split in two, and the split is the point
([ADR 0013](../../../docs/adr/0013-one-write-outside-the-catch-all.md)). `_decide` runs the
ticket and returns an `Outcome`; it is never handed the repository, so it *cannot* write a
terminal state. `run_pipeline` writes that outcome, once, below the catch-all that guards
`_decide`. A terminal ticket without exactly one matching `final_decision` is therefore
structurally impossible rather than merely unlikely.

Everything it needs is injected: the repository, the LLM client, the tool runner, the
clock, the template resolver, and the cap. No module-level singletons and no ambient time
([ADR 0008](../../../docs/adr/0008-injected-clock-with-advancing-test-double.md)), so a
test assembles a pipeline from an in-memory repository, a frozen clock, and a client that
misbehaves on purpose.

This is also the one module that imports `llm/`, `tools/` and `guardrails/` together --
they know nothing about each other and meet here (ARCHITECTURE.md section 3).
"""

from collections.abc import Callable
from typing import Any, NamedTuple

from support_assistant.clock import Clock
from support_assistant.domain import (
    Handoff,
    HandoffReason,
    Intent,
    Observation,
    Reply,
    ReplyTemplate,
    Ticket,
    TicketStatus,
    ToolCall,
    ToolResult,
)
from support_assistant.guardrails.factset import FactSet
from support_assistant.guardrails.grounding import GroundingChecker
from support_assistant.guardrails.limits import MAX_ITERATIONS
from support_assistant.llm.protocol import LLMClient
from support_assistant.llm.templates import Template, spec_for
from support_assistant.storage.protocol import TicketRepository
from support_assistant.tools import registry
from support_assistant.tools.errors import NoDataAvailable, UserNotFound
from support_assistant.tracing.models import Violation
from support_assistant.tracing.recorder import TraceRecorder, as_tool_error
from support_assistant.tracing.summarise import summarise

ToolRunner = Callable[[str, dict[str, Any]], ToolResult]
"""How the loop reaches a tool. `registry.run` is the default; a test injects one that
misbehaves. A plain callable rather than a protocol, like `resolve_template` beside it --
there is one method, so there is nothing an interface would add."""


# --------------------------------------------------------------------------------------
# The two outcomes
#
# `_decide` returns one of these; `run_pipeline` writes it. Nothing else constructs one,
# so the status/reply/reason pairing cannot be assembled wrong -- and `FinalDecision` and
# `Ticket` each validate it again on the way out.
# --------------------------------------------------------------------------------------


class Outcome(NamedTuple):
    """What the run concluded, in the shape `finalise` takes."""

    status: TicketStatus
    reply: str | None
    reason: HandoffReason | None
    detail: str | None


def replied(reply: str) -> Outcome:
    """The reply was rendered from the facts and passed grounding. Send it."""
    return Outcome(TicketStatus.REPLIED, reply, None, None)


def handed_off(reason: HandoffReason, detail: str) -> Outcome:
    """A human takes the ticket. No reply -- not an empty string, not a holding message
    (ADR 0005).

    `detail` is required rather than optional: a reason explains the category, and the
    detail explains the incident -- which user id was missing, which tool raised, which
    literal was ungrounded (GUARDRAILS.md).
    """
    return Outcome(TicketStatus.HANDED_OFF, None, reason, detail)


def run_pipeline(
    ticket_id: str,
    *,
    repository: TicketRepository,
    llm: LLMClient,
    clock: Clock,
    tools: ToolRunner = registry.run,
    resolve_template: Callable[[ReplyTemplate], Template] = spec_for,
    max_iterations: int = MAX_ITERATIONS,
) -> None:
    """Run one ticket to a terminal state. Returns nothing: the outcome is the persisted
    ticket, not a value a caller could forget to check.

    Raises `ValueError` if the id is unknown. That is not a handoff -- the API creates the
    ticket before scheduling the run, so an unknown id is a bug to surface, and there is
    no ticket to carry a reason anyway.

    A failing `repository.finalise` propagates too. It is the one failure the pipeline
    cannot fail closed over, because failing closed is itself a write -- catching it in
    order to write again only appends a second `final_decision` describing an outcome that
    was never persisted (ADR 0013). A stranded ticket is the reaper's job
    ([roadmap](../../../docs/ROADMAP.md#durable-work-and-a-reaper)).
    """
    ticket = repository.get(ticket_id)
    if ticket is None:
        raise ValueError(f"no ticket {ticket_id!r} to run")

    trace = TraceRecorder(clock)
    outcome = _decide(
        ticket,
        trace,
        llm=llm,
        tools=tools,
        resolve_template=resolve_template,
        max_iterations=max_iterations,
    )

    # The single write. No branch here: `Outcome` already carries the pairing, so there is
    # no arm of a condition that could record one thing and persist another.
    trace.final_decision(outcome.status, reason=outcome.reason, detail=outcome.detail)
    repository.finalise(ticket_id, outcome.status, outcome.reply, outcome.reason, trace.steps)


def _decide(
    ticket: Ticket,
    trace: TraceRecorder,
    *,
    llm: LLMClient,
    tools: ToolRunner,
    resolve_template: Callable[[ReplyTemplate], Template],
    max_iterations: int,
) -> Outcome:
    """Classify, loop, ground -- and say what should happen, without making it happen.

    No repository parameter, deliberately. Everything below runs inside a catch-all, and a
    write inside that catch-all is a write the catch-all would retry (ADR 0013). Not being
    given the repository is what makes that impossible rather than merely avoided.
    """
    in_flight: str | None = None
    """Which tool is executing, for the detail on a typed tool failure. The trace already
    names it; the `final_decision` detail says which incident, not just which category."""

    try:
        # 1. Classify -------------------------------------------------------------------
        classification = llm.classify_intent(ticket)
        trace.intent_classified(classification.intent, list(classification.matched_keywords))
        if classification.intent is Intent.UNKNOWN:
            return handed_off(
                HandoffReason.UNSUPPORTED_INTENT,
                _unsupported_detail(classification.matched_keywords),
            )

        # 2. Loop -- bounded, agentic ---------------------------------------------------
        history: list[Observation] = []
        for iteration in range(1, max_iterations + 1):
            step = llm.decide_next_step(ticket, history)
            # tracing/ cannot import llm/, so the orchestrator adapts its own step. Only a
            # ToolCall carries a `tool`, which is exactly what LLMDecision's validator
            # requires (domain.py).
            tool = step.tool if isinstance(step, ToolCall) else None
            trace.llm_decision(iteration, step.decision, tool=tool)

            match step:
                case ToolCall():
                    in_flight = step.tool
                    trace.tool_call(step.tool, step.args)
                    try:
                        result = tools(step.tool, step.args)
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
                    # 3. Ground and verify ------------------------------------------
                    return _ground(step, history, trace, resolve_template)
                case Handoff():
                    return handed_off(step.reason, _gave_up_detail(step.reason))
        else:
            # Reached only when the loop ran to completion without returning -- the cap is
            # structural, not an `if` someone can forget to write. There is no path from
            # here to a reply (GUARDRAILS.md section 1).
            return handed_off(
                HandoffReason.ITERATION_CAP_EXCEEDED,
                f"the loop reached MAX_ITERATIONS ({max_iterations}) without terminating",
            )

    except UserNotFound as exc:
        return handed_off(HandoffReason.USER_NOT_FOUND, _tool_detail(in_flight, exc))
    except NoDataAvailable as exc:
        return handed_off(HandoffReason.DATA_NOT_FOUND, _tool_detail(in_flight, exc))
    except Exception as exc:  # deliberate catch-all -- ADR 0005
        # "Any step fails" is one of the brief's three handoff triggers, and an
        # unanticipated exception is exactly the case that cannot be enumerated. Without
        # this a bug leaves a ticket in `processing` forever -- a state with no owner and
        # no alarm. Last clause, so the typed failures above keep their specific reasons.
        return handed_off(HandoffReason.TOOL_ERROR, repr(exc))


def _ground(
    draft: Reply,
    history: list[Observation],
    trace: TraceRecorder,
    resolve_template: Callable[[ReplyTemplate], Template],
) -> Outcome:
    """Render the model's chosen template from the facts, then check the finished text.

    One `Template` both renders and is checked: verifying against a different spec would
    check a reply's literals against the wrong safe list, which is the one way this step
    could pass something it should have caught (PIPELINE.md).
    """
    facts = FactSet.from_observations(history)
    template = resolve_template(draft.template)
    reply = template.render(facts)

    checked = GroundingChecker.verify(reply, facts, template)
    trace.grounding_check(len(checked.literals), checked.violations)
    if checked.violations:
        return handed_off(HandoffReason.UNGROUNDED_REPLY, _violation_detail(checked.violations))

    return replied(reply)


# --------------------------------------------------------------------------------------
# Handoff details -- one per reason, so the trace says what happened and not just what
# kind of thing happened
# --------------------------------------------------------------------------------------


def _unsupported_detail(matched_keywords: tuple[str, ...]) -> str:
    if not matched_keywords:
        return "intent classified unknown: nothing in the ticket matched a supported category"
    return f"intent classified unknown: an ambiguous match on {', '.join(matched_keywords)}"


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
