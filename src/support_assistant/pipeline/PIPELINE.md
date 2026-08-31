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
    trace = TraceRecorder(ticket_id, clock)

    try:
        # 1. Classify
        intent = llm.classify_intent(ticket)
        trace.intent_classified(intent, matched_keywords)
        if intent is Intent.UNKNOWN:
            return finish_handoff(HandoffReason.UNSUPPORTED_INTENT)

        # 2. Loop  -- bounded, agentic
        history: list[Observation] = []
        for iteration in range(1, MAX_ITERATIONS + 1):
            step = llm.decide_next_step(ticket, history)
            trace.llm_decision(iteration, step)

            match step:
                case ToolCall():
                    trace.tool_call(step.tool, step.args)
                    result = registry.run(step.tool, step.args)   # may raise
                    trace.tool_result(step.tool, summarise(result))
                    history.append(Observation(step, result))
                case Reply():
                    draft = step
                    break
                case Handoff():
                    return finish_handoff(step.reason)
        else:
            return finish_handoff(HandoffReason.ITERATION_CAP_EXCEEDED)

        # 3. Ground and verify
        facts = FactSet.from_observations(history)
        reply = render(draft.template, facts)
        violations = GroundingChecker.verify(reply, facts, draft.template)
        trace.grounding_check(violations)
        if violations:
            return finish_handoff(HandoffReason.UNGROUNDED_REPLY, detail=violations)

        return finish_replied(reply)

    except UserNotFound:
        return finish_handoff(HandoffReason.USER_NOT_FOUND)
    except NoDataAvailable:
        return finish_handoff(HandoffReason.DATA_NOT_FOUND)
    except Exception as exc:                      # deliberate catch-all
        return finish_handoff(HandoffReason.TOOL_ERROR, detail=repr(exc))
```

Illustrative, not the final source — but the control flow and the ordering of the
guardrails are the contract.

---

## Five things about this loop that are deliberate

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

**3. Every exit converges on two functions.** `finish_replied` and `finish_handoff` are
the only writers of a terminal state. Each writes the `final_decision` trace step,
persists atomically, and emits the outcome metric. There is no way to reach a terminal
state without being recorded — which is what makes requirement 5 hold by construction
rather than by discipline.

**4. Grounding runs after rendering, unconditionally.** Not "if the LLM is real". The
check is on the output, so it costs the same either way and it cannot be forgotten during
the swap that would make it matter.

**5. History accumulates only successful observations.** A failed tool call terminates the
run, so the model never sees an error and never gets to retry. Retry-on-failure would be a
reasonable feature, and it is deliberately absent: it multiplies iterations against the
cap and gives the model room to work around a failure rather than surface it. Fail closed.

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
| `DATA_NOT_FOUND` | `NoDataAvailable` from any tool |
| `TOOL_ERROR` | `ToolExecutionError`, or any unhandled exception |
| `ITERATION_CAP_EXCEEDED` | the `for ... else` branch |
| `UNGROUNDED_REPLY` | grounding violations after rendering |

Each is produced at exactly one place in the code. That one-to-one mapping is what makes
handoff-rate-by-reason a trustworthy operational signal
([OBSERVABILITY.md](../observability/OBSERVABILITY.md)).

---

## Structure

```
pipeline/
  __init__.py
  orchestrator.py   run_pipeline, finish_replied, finish_handoff
  config.py         MAX_ITERATIONS and other env-backed settings
  PIPELINE.md       this file
```

Dependencies are injected — repository, LLM client, tool registry, trace recorder, and
`Clock` ([ADR 0008](../../../docs/adr/0008-injected-clock-with-advancing-test-double.md)).
No module-level singletons and no ambient time, so a test can assemble a pipeline with an
in-memory repository, a frozen clock, and a stub client that misbehaves on purpose.

---

## Concurrency

Each run touches one ticket and shares no mutable state with another. Tools are read-only.
The repository serialises writes. Two tickets processing at once cannot interfere.

There is no limit on concurrent background tasks — a burst of `POST`s becomes a burst of
concurrent pipelines. Acceptable at this scale; the fix is a bounded worker pool or the
queue from ADR 0001.
