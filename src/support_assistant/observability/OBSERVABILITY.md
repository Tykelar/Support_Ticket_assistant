# Observability

Structured logs and counters. This package is the concrete backing for the README's
"what I would measure in production to know this is working" section — the measurements
exist in code, not only in prose.

---

## The question this has to answer

Not "is the service up?" — that is easy and nearly useless here. The failure this system
actually has is **being confidently wrong**, and a wrong reply looks exactly like a right
one from the outside: `200 OK`, `status: replied`, low latency, no errors.

So the signals below are chosen to detect quality failures, not availability failures.

---

## Metrics

### The one that matters most

```
tickets_total{status}                    counter
handoffs_total{reason}                   counter
```

**Handoff rate broken down by reason** is the single most informative signal this system
emits, and it is only possible because `HandoffReason` is a closed enum produced at
exactly one place per reason ([GUARDRAILS.md](../guardrails/GUARDRAILS.md)).

The rate alone says little — the *shape* of the breakdown says a lot:

| What moves | Likely meaning |
|---|---|
| `UNSUPPORTED_INTENT` climbing | customers are asking about things the three tools cannot answer. A product signal: which fourth tool to build |
| `DATA_NOT_FOUND` climbing | an upstream data problem, or tickets arriving before data lands |
| `USER_NOT_FOUND` climbing | an identity or integration bug, almost never a genuine unknown customer |
| `TOOL_ERROR` climbing | a real defect. Page someone |
| `ITERATION_CAP_EXCEEDED` non-zero | the model is looping. Under a real LLM, the first thing to watch after a prompt or model change |
| `UNGROUNDED_REPLY` non-zero | **the alarm.** See below |
| Overall rate falling toward zero | not a win. Suspect the guardrails have been weakened |

### Grounding

```
grounding_violations_total{literal_class}    counter
```

Expected value: **zero**. Any non-zero value means the renderer produced a literal that no
tool returned — the unforgivable bug, caught by layer 2 before it reached a customer
([ADR 0004](../../../docs/adr/0004-two-layer-grounding-enforcement.md)).

The guardrail worked, and something is still wrong. This is a defect alert, not a quality
metric. Under `FakeLLM` it should be structurally impossible; if it fires, layer 1 has a
hole. Under `OllamaLLM` it is the primary quality signal for the model.

### Loop behaviour

```
iterations_per_ticket                    histogram
tool_calls_total{tool, outcome}          counter
pipeline_duration_seconds{outcome}       histogram
```

`iterations_per_ticket` is a leading indicator. `FakeLLM` sits at 3; the distribution
drifting upward means the model is taking longer routes, and the mass approaching
`MAX_ITERATIONS` predicts cap handoffs before they happen.

`tool_calls_total{tool, outcome}` separates a broken tool from a model that stopped
calling it.

`pipeline_duration_seconds` is derived from trace step timestamps rather than measured
separately, so it is covered by tests: the frozen clock advances a known tick, which makes
the expected duration arithmetic rather than a wall-clock race
([ADR 0008](../../../docs/adr/0008-injected-clock-with-advancing-test-double.md)).

### The stranded-ticket gauge — deferred

```
tickets_processing_age_seconds           gauge (max)      NOT WIRED
```

Directly instruments ADR 0001's known limitation. In-process background work is lost if
the process dies, leaving a ticket in `processing` with no owner and no error. Nothing
else on this list would notice — the run simply never finishes.

Alert when the oldest `processing` ticket exceeds a few multiples of p99 pipeline
duration. This gauge is also the trigger for the reaper that a production version would
add.

**Why it is not built yet.** Every other metric here is folded from a *finished* run
([ADR 0014](../../../docs/adr/0014-metrics-derived-from-the-trace.md)); this one is the
opposite — it is a question about runs that have *not* finished, answerable only by a live
read against the repository (`MIN(created_at) WHERE status = 'processing'`). That means a
new method on the `TicketRepository` protocol, which is one of the system's fixed
contracts. It lands with the reaper that consumes the same query
([ROADMAP](../../../docs/ROADMAP.md#durable-work-and-a-reaper)) — the two are one change.
Until then the blind spot is explicit: a run whose `finalise` failed is counted by
*nothing* on this page, because `record_run` runs after the persist.

---

## What I would measure that is not a counter

Honest note: the metrics above measure whether the *system* is behaving. They cannot
measure whether the replies are any **good**. In production the signals that answer that
are:

- **Reopen rate** — a customer replying again after an auto-reply. The closest thing to
  ground truth on reply quality, and the number worth optimising.
- **Agent override rate** — how often a human edits or retracts a sent reply.
- **Sampled human review** of replied tickets, rated for correctness. The only way to
  catch confidently-wrong replies that customers never bother to challenge.
- **Time-to-resolution**, auto-replied versus handed-off. Whether the automation is
  actually helping rather than adding a hop.

The first three need feedback the API does not currently capture. Wiring them is the
first thing worth building after the core system — [how](../../../docs/ROADMAP.md#the-feedback-loop).

---

## Structured logs

One JSON object per line, one line per pipeline step, always carrying `ticket_id`.

```json
{"ts":"2026-08-31T10:14:02.113Z","level":"info","event":"tool_call",
 "ticket_id":"t_4f0c9a7b21e84d3fa6c5b8e0d1927354","tool":"get_invoices","iteration":2}
{"ts":"2026-08-31T10:14:02.119Z","level":"warning","event":"handoff",
 "ticket_id":"t_4f0c9a7b21e84d3fa6c5b8e0d1927354","reason":"DATA_NOT_FOUND","detail":"no invoices for u_004"}
```

**Logs and traces are not the same thing and do not serve the same reader.**

| | Trace | Log |
|---|---|---|
| Reader | support agent | engineer |
| Question | "why did the AI say this?" | "what is the system doing?" |
| Access | `GET /tickets/{id}` | log aggregator |
| Retention | with the ticket | short |
| Stack traces | never | yes |

The overlap is deliberate. The log keeps a per-step record even when the trace is lost —
the trace is written atomically at the end of a run ([TRACEABILITY.md](../tracing/TRACEABILITY.md)),
so a process that dies mid-run leaves the log as the only evidence of what happened.

Ticket `subject` and `body` are **not** logged. They are customer text of unknown
sensitivity, they are already stored with the ticket, and copying them into a log
aggregator widens their exposure for no diagnostic gain.

---

## Wiring

**Every metric is derived from the finished trace, once**
([ADR 0014](../../../docs/adr/0014-metrics-derived-from-the-trace.md)).
`metrics.record_run(registry, status=, reason=, steps=)` walks the same `list[TraceStep]`
that `repository.finalise` persists and `GET /tickets/{id}` serves, and updates every
family from it. `run_pipeline` calls it exactly once, **after** `repository.finalise` and
below the catch-all — so a run whose terminal write failed is not counted, and `_decide`
keeps the "cannot write" property [ADR 0013](../../../docs/adr/0013-one-write-outside-the-catch-all.md)
gave it. `pipeline_duration_seconds` is `steps[-1].ts - steps[0].ts`, so under a
`FrozenClock` the duration is arithmetic and is tested like anything else.

**The registry is injected, not reached for.** `run_pipeline` takes a `MetricRegistry`,
`create_app` owns one on `app.state`, `GET /metrics` reads that same object — the pattern
`api/` already uses for `TicketRepository`. The module-level `metrics.REGISTRY` is the
production default, exactly as `registry.run` and `MAX_ITERATIONS` are defaults one layer
down. Tests inject their own, so one run never colours another's `/metrics`.

**Structured logs come from the recorded step, live.** `TraceRecorder` takes an optional
`on_step` callback — injected, never imported, so `tracing/` stays below `observability/`
— and the orchestrator wires `logging.log_step` as it, under a `ticket_scope(ticket_id)`
so every line carries the id. Unlike the metrics, the log is emitted step by step during
the run, which is what lets it survive a process death that loses the not-yet-assembled
trace. `configure_logging()` runs in the app lifespan; level is `LOG_LEVEL`
([PACKAGING.md](../../../deploy/PACKAGING.md)), then `info`.

---

## Structure

```
observability/
  __init__.py
  logging.py         JSON formatter, ticket_id context binding, the log_step hook
  metrics.py         Counter / Histogram, the in-process MetricRegistry, record_run
  OBSERVABILITY.md   this file
```

Metrics are held in an in-process registry and exposed on `GET /metrics` as Prometheus
text. No Prometheus client dependency and no push gateway — the point is to show what is
worth measuring and to have it counted, not to run a metrics stack inside a take-home.
`MetricRegistry` is the seam; swapping it for `prometheus_client` is a contained change
that leaves `record_run` and the endpoint untouched.
