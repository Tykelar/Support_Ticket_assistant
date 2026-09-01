# Pipeline

The orchestrator. This is the component the brief is actually evaluating — "the system
around the model — the loop, the guardrails, the failure handling".

**Responsibility:** run one ticket from `processing` to a terminal state. It is the
**only** component permitted to decide a terminal outcome
([ADR 0005](../../../docs/adr/0005-fail-closed-to-human-handoff.md)). Tools raise, the
LLM proposes, guardrails report — the orchestrator decides.

**Why it is shaped this way:**
[ADR 0002](../../../docs/adr/0002-true-agentic-tool-calling-loop.md).

---

## The run

```python
def run_pipeline(ticket_id: str) -> None:
    ticket = repo.get(ticket_id)
    trace = TraceRecorder(clock)

    outcome = _decide(ticket, trace, ...)          # no repository: it cannot write

    trace.final_decision(outcome.status, reason=outcome.reason, detail=outcome.detail)
    repo.finalise(ticket_id, outcome.status, outcome.reply, outcome.reason, trace.steps)


def _decide(ticket, trace, ...) -> Outcome:
    try:
        # 1. Classify
        intent = llm.classify_intent(ticket)
        trace.intent_classified(intent, matched_keywords)
        if intent is Intent.UNKNOWN:
            return handed_off(HandoffReason.UNSUPPORTED_INTENT, detail=...)

        # 2. Loop  -- bounded, agentic
        history: list[Observation] = []
        for iteration in range(1, MAX_ITERATIONS + 1):
            step = llm.decide_next_step(ticket, history)
            # tracing/ can't import llm/, so the orchestrator adapts its own step:
            trace.llm_decision(iteration, step.decision,
                               tool=step.tool if isinstance(step, ToolCall) else None)

            match step:
                case ToolCall():
                    trace.tool_call(step.tool, step.args)
                    try:
                        result = tools(step.tool, step.args)
                    except Exception as exc:                      # record, then re-raise
                        trace.tool_result(step.tool, error=as_tool_error(exc))
                        raise
                    trace.tool_result(step.tool, summarise(result))
                    history.append(Observation(step, result))
                case Reply():
                    return _ground(step, history, trace, resolve_template)
                case Handoff():
                    return handed_off(step.reason, detail=...)
        else:
            return handed_off(HandoffReason.ITERATION_CAP_EXCEEDED, detail=...)

    except UserNotFound as exc:
        return handed_off(HandoffReason.USER_NOT_FOUND, detail=f"{in_flight}: {exc}")
    except NoDataAvailable as exc:
        return handed_off(HandoffReason.DATA_NOT_FOUND, detail=f"{in_flight}: {exc}")
    except Exception as exc:                      # deliberate catch-all
        return handed_off(HandoffReason.TOOL_ERROR, detail=repr(exc))


# 3. Ground and verify
def _ground(draft, history, trace, resolve_template) -> Outcome:
    facts = FactSet.from_observations(history)
    template = resolve_template(draft.template)   # one spec both renders and is checked
    reply = template.render(facts)
    checked = GroundingChecker.verify(reply, facts, template)
    trace.grounding_check(len(checked.literals), checked.violations)
    if checked.violations:
        return handed_off(HandoffReason.UNGROUNDED_REPLY, detail=checked.violations)
    return replied(reply)
```

Illustrative, not the final source — but the control flow and the ordering of the
guardrails are the contract. `orchestrator.py` follows it, with three details the sketch
elides: `in_flight` is the tool name the loop last dispatched, so a typed failure's detail
names the incident and not just the category ([GUARDRAILS.md](../guardrails/GUARDRAILS.md)
asks every handoff for both); `verify` returns the literals it checked alongside the
violations, so `literals_checked` is the count from the pass that did the checking rather
than a second reading of the same reply; and `handed_off` takes `detail` as a **required**
argument rather than an optional one, since a default of `None` is how "every handoff
carries its detail" erodes one call site at a time.

---

## Six things about this loop that are deliberate

**1. `for ... else` is the cap.** The `else` branch runs only when the loop completes
without `break`, i.e. the model never terminated within `MAX_ITERATIONS`. The cap is
structural, not an `if` someone can forget to write. There is no code path where
exhausting the loop produces a reply.

**2. The catch-all is load-bearing, not lazy.** "Any step fails" is one of the brief's
three handoff triggers, and an unanticipated exception is exactly the case you cannot
enumerate. Without it, a bug leaves a ticket stuck in `processing` forever — a state with
no owner and no alarm. It is the last clause, after the typed handlers, so specific
failures still get specific reasons. The exception is recorded in the trace and the
structured log; only the stack trace is withheld from the API.

**3. Deciding and writing are separate, and only one of them touches storage.**
`_decide` returns an `Outcome`; it is never handed the repository, so it *cannot* write a
terminal state. `run_pipeline` records the `final_decision` and persists it with the trace
in one `finalise` call, once, below the catch-all — and with no branch at the write, since
`Outcome` already carries the status/reply/reason pairing. A terminal ticket therefore
carries exactly one `final_decision` by construction rather than by six call sites each
remembering to write one ([ADR 0013](../../../docs/adr/0013-one-write-outside-the-catch-all.md)).
The outcome metric this will also emit lands with
[`observability/`](../observability/OBSERVABILITY.md) in a later phase; the trace step and
the persisted state are here now.

A failing `finalise` propagates rather than being retried. It is the one failure that
cannot be failed closed over, because failing closed is itself a write — catching it to
write again only appends a second `final_decision` for an outcome that was never
persisted.

**4. Grounding runs after rendering, unconditionally.** Not "if the LLM is real". The
check is on the output, so it costs the same either way and it cannot be forgotten during
the swap that would make it matter. The `Template` is resolved once and used for both
calls: rendering and verifying against *different* templates would check a reply's
literals against the wrong safe list, which is the one way this step could pass something
it should have caught. `guardrails/` never imports `llm/`, so the orchestrator is what
carries the spec across ([ARCHITECTURE.md](../../../ARCHITECTURE.md) §3).

**5. The tool call is wrapped, and the catch is broad.** A raising tool would otherwise
jump straight past the line that records its result, leaving the `ok: false` step — the
first thing a support agent reads when a ticket went wrong — written by nothing. The catch
is `Exception` rather than a tool-error base class on purpose: an unanticipated bug is
exactly the case where a reader needs to know which tool was in flight, and narrowing it
would make the most confusing failures the ones the trace stays silent about. The `raise`
is unchanged, so the typed handlers below still pick the right reason. The registry never
sees the recorder ([ADR 0010](../../../docs/adr/0010-the-orchestrator-records-tool-steps.md)).

**6. History accumulates only successful observations.** A failed tool call terminates the
run, so the model never sees an error and never gets to retry. Retry-on-failure would be a
reasonable feature, and it is deliberately absent: it multiplies iterations against the
cap and gives the model room to work around a failure rather than surface it. Fail closed.
If it were added it belongs in the registry, not the loop —
[roadmap](../../../docs/ROADMAP.md#retry-on-transient-tool-failure).

---

## State machine

```
              POST /tickets
                    |
                    v
             +-------------+
             | processing  |
             +------+------+
                    |
        +-----------+------------+
        |                        |
   reply verified          any of six
   and grounded          handoff reasons
        |                        |
        v                        v
   +---------+            +-------------+
   | replied |            | handed_off  |
   +---------+            +-------------+
   reply: str             reply: None
   reason: None           reason: HandoffReason
```

Both terminal states are final. Nothing re-opens a ticket; a re-run would be a new one.

---

## Handoff reason selection

| Reason | Decided at |
|---|---|
| `UNSUPPORTED_INTENT` | step 1, before the loop is entered |
| `USER_NOT_FOUND` | `UserNotFound` from any tool |
| `DATA_NOT_FOUND` | `NoDataAvailable` — only from the two collection tools ([ADR 0009](../../../docs/adr/0009-absent-data-is-a-handoff.md)) |
| `TOOL_ERROR` | `ToolExecutionError`, or any unhandled exception |
| `ITERATION_CAP_EXCEEDED` | the `for ... else` branch |
| `UNGROUNDED_REPLY` | grounding violations after rendering |

Each is produced at exactly one place in the code. That one-to-one mapping is what makes
handoff-rate-by-reason a trustworthy operational signal
([OBSERVABILITY.md](../observability/OBSERVABILITY.md)).

Each also carries a detail naming the incident: the matched keywords behind an ambiguous
classification, the failing tool and its message, the cap and its value, the offending
literals and their classes. `HandoffReason` is what a dashboard counts; the detail is what
a support agent reads.

---

## Structure

```
pipeline/
  __init__.py
  orchestrator.py   run_pipeline, _decide, _ground, and the two Outcome constructors
  PIPELINE.md       this file
```

Dependencies are injected — repository, LLM client, tool registry, `Clock`, the template
resolver, and the iteration cap
([ADR 0008](../../../docs/adr/0008-injected-clock-with-advancing-test-double.md)). No
module-level singletons and no ambient time, so a test can assemble a pipeline with an
in-memory repository, a frozen clock, and a stub client that misbehaves on purpose.

The `TraceRecorder` is **built from the injected clock** rather than passed in: it belongs
to one run, and constructing it inside is what guarantees a run cannot inherit another's
steps. The clock is the injected dependency; the recorder is a consequence of it.

`resolve_template` defaults to `llm.templates.spec_for` and exists as a seam for one test.
`test_grounding.py` has to render a deliberately doctored template through the *production*
path — real `Template`, real field selection, real `Template.render` — to prove an
ungrounded reply is withheld. Injecting the resolver is how it does that without reaching
into `templates.py`'s private registry, which would test a path no reply ever takes.

---

## Concurrency

Each run touches one ticket and shares no mutable state with another. Tools are read-only.
The repository serialises writes. Two tickets processing at once cannot interfere.

There is no limit on concurrent background tasks — a burst of `POST`s becomes a burst of
concurrent pipelines. Acceptable at this scale; the fix is a bounded worker pool or the
queue from ADR 0001.
