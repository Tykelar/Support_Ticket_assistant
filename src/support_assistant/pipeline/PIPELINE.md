# Pipeline

The orchestrator: run one ticket from `processing` to a terminal state.

It is the **only** component permitted to decide a terminal outcome
([ADR 0005](../../../docs/adr/0005-fail-closed-to-human-handoff.md)). Tools raise, the LLM
proposes, guardrails report — the orchestrator decides. Why it is a real loop rather than a
fixed plan: [ADR 0002](../../../docs/adr/0002-true-agentic-tool-calling-loop.md).

---

## The run

[`orchestrator.py`](orchestrator.py) is short enough to read directly; this is the shape it
follows.

`run_pipeline` loads the ticket, builds a `TraceRecorder`, and calls `_decide` — which is
handed no repository, so it cannot write. It then records the `final_decision`, persists it
with the trace in one `finalise`, and folds the finished trace into the metrics.

`_decide` runs inside one `try`:

1. **Classify.** `llm.classify_intent`, recorded with its matched keywords. `UNKNOWN` hands
   off before the loop is entered.
2. **Loop**, at most `MAX_ITERATIONS` times. Each turn asks `llm.decide_next_step` for a
   `ToolCall`, `Reply` or `Handoff` and records what was chosen. A `ToolCall` is dispatched
   through the registry, summarised, recorded, and appended to the history; a `Reply` goes
   to grounding; a `Handoff` ends the run. Falling out of the loop is
   `ITERATION_CAP_EXCEEDED`.
3. **Ground.** `_ground` projects a `FactSet` from the history, renders the named template
   from it, and verifies the finished text against the same template. Violations mean
   `UNGROUNDED_REPLY`; a clean check means the reply is sent.

Three `except` clauses close it: `UserNotFound` and `NoDataAvailable` map to their own
reasons, and a final catch-all maps everything else to `TOOL_ERROR`.

---

## Five things about this loop that are deliberate

**1. `for ... else` is the cap.** The `else` runs only when the loop never terminated
within `MAX_ITERATIONS`. There is no code path from an exhausted loop to a reply.

**2. The catch-all is load-bearing.** "Any step fails" is one of the brief's three handoff
triggers, and an unanticipated exception is the case you cannot enumerate. Without it a bug
leaves a ticket in `processing` forever — a state with no owner and no alarm. It is the
last clause, so typed failures keep their specific reasons.

**3. Deciding and writing are separate.** `_decide` is never handed the repository, so it
cannot write a terminal state. `run_pipeline` records the `final_decision` and persists it
with the trace in one `finalise` call, below the catch-all and with no branch at the write.
A terminal ticket carries exactly one `final_decision` by construction
([ADR 0013](../../../docs/adr/0013-one-write-outside-the-catch-all.md)).

A failing `finalise` propagates rather than being retried: failing closed is itself a
write, so catching it would only append a second `final_decision` for an outcome nothing
persisted.

**4. Grounding runs after rendering, unconditionally** — not "if the LLM is real". The
check is on the output, so it costs the same either way and cannot be forgotten during the
swap that would make it matter. The same `Template` renders and is verified: two different
ones would check the reply's literals against the wrong safe list.

**5. The tool call is wrapped, and the catch is broad.** A raising tool would otherwise
jump past the line that records its result, leaving the `ok: false` step — the first thing
an agent reads when a ticket went wrong — written by nothing. Summarising is inside the
same `try` for the same reason. History accumulates only successful observations, so the
model never sees an error and never gets to work around one; retry-on-failure is
deliberately absent
([roadmap](../../../docs/ROADMAP.md#retry-on-transient-tool-failure)).

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
| `DATA_NOT_FOUND` | `NoDataAvailable` — collection tools only ([ADR 0009](../../../docs/adr/0009-absent-data-is-a-handoff.md)) |
| `TOOL_ERROR` | `ToolExecutionError`, or any unhandled exception |
| `ITERATION_CAP_EXCEEDED` | the `for ... else` branch |
| `UNGROUNDED_REPLY` | grounding violations after rendering |

Each is produced at exactly one place in the code, which is what makes
handoff-rate-by-reason a trustworthy signal
([OBSERVABILITY.md](../observability/OBSERVABILITY.md)).

Each also carries a `detail` naming the incident — the failing tool and its message, the
cap and its value, the offending literals. The reason is what a dashboard counts; the
detail is what a support agent reads. `handed_off` takes it as a required argument, since
a default of `None` is how "every handoff carries its detail" erodes one call site at a
time.

---

## Structure

```
pipeline/
  __init__.py
  orchestrator.py   run_pipeline, _decide, _ground, and the two Outcome constructors
  PIPELINE.md       this file
```

Everything is injected — repository, LLM client, `Clock`, metric registry, tool runner,
template resolver, iteration cap
([ADR 0008](../../../docs/adr/0008-injected-clock-with-advancing-test-double.md)).

**Four are required, three carry defaults, and the split is not arbitrary.** `tools`,
`resolve_template` and `max_iterations` default to process-wide constants every caller
wants; choosing them at the HTTP layer would put pipeline configuration in `api/`.
`repository`, `llm`, `clock` and `metrics` are per-application state that `create_app`
owns. `metrics` is the one worth naming: a default of the module-level `REGISTRY` would be
the wrong object in exactly the case injection exists for, and every number would read as
low rather than as missing.

The `TraceRecorder` is built from the injected clock rather than passed in, so a run cannot
inherit another's steps. `resolve_template` is the seam `test_grounding.py` uses to render
a deliberately doctored template through the production path.

---

## Concurrency

Each run touches one ticket and shares no mutable state with another. Tools are read-only,
the repository serialises writes, and the metric families lock their own counters.

There is no limit on concurrent background tasks — a burst of `POST`s becomes a burst of
concurrent pipelines. Acceptable at this scale; the fix is a bounded worker pool or the
queue from ADR 0001.
