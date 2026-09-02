# Observability

Structured logs and counters — the concrete backing for the README's "what I would measure
in production" section.

**The question is not "is the service up?"** That is easy and nearly useless here. The
failure this system actually has is **being confidently wrong**, and a wrong reply looks
exactly like a right one from the outside: `200 OK`, `status: replied`, low latency, no
errors. The signals below are chosen to detect quality failures, not availability ones.

---

## Metrics

### The one that matters most

```
tickets_total{status}                    counter
handoffs_total{reason}                   counter
```

**Handoff rate broken down by reason** is the single most informative signal this system
emits, and it is only possible because `HandoffReason` is a closed enum produced at exactly
one place per reason ([GUARDRAILS.md](../guardrails/GUARDRAILS.md)). The rate alone says
little; the *shape* of the breakdown says a lot:

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

Expected value: **zero**. Anything else means the renderer produced a literal no tool
returned — the unforgivable bug, caught by layer 2 before it reached a customer
([ADR 0004](../../../docs/adr/0004-two-layer-grounding-enforcement.md)).

The guardrail worked and something is still wrong, so this is a defect alert rather than a
quality metric. Under `FakeLLM` it should be structurally impossible; if it fires, layer 1
has a hole. Under `OllamaLLM` it is the primary quality signal for the model.

### Loop behaviour

```
iterations_per_ticket                    histogram
tool_calls_total{tool, outcome}          counter
pipeline_duration_seconds{outcome}       histogram
```

`iterations_per_ticket` is a leading indicator. `FakeLLM` sits at 3; the distribution
drifting upward means the model is taking longer routes, and mass approaching
`MAX_ITERATIONS` predicts cap handoffs before they happen. It buckets from **zero**, so a
ticket handed off before the loop is distinguishable from one that took a single iteration.

`tool_calls_total{tool, outcome}` separates a broken tool from a model that stopped calling
it.

`pipeline_duration_seconds` is derived from trace timestamps rather than measured
separately, so the frozen clock makes the expected duration arithmetic rather than a
wall-clock race (ADR 0008).

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
([ADR 0014](../../../docs/adr/0014-metrics-derived-from-the-trace.md)); this one asks about
runs that have *not* finished, answerable only by a live read against the repository
(`MIN(created_at) WHERE status = 'processing'`). That means a new method on the
`TicketRepository` protocol, so it lands with the reaper that consumes the same query
([ROADMAP](../../../docs/ROADMAP.md#durable-work-and-a-reaper)). Until then the blind spot
is explicit: a run whose `finalise` failed is counted by *nothing* here.

---

## What I would measure that is not a counter

The metrics above measure whether the *system* is behaving. They cannot measure whether
the replies are any **good**. In production the signals that answer that are:

- **Reopen rate** — a customer replying again after an auto-reply. The closest thing to
  ground truth on reply quality, and the number worth optimising.
- **Agent override rate** — how often a human edits or retracts a sent reply.
- **Sampled human review** of replied tickets — the only way to catch confidently-wrong
  replies that customers never bother to challenge.
- **Time-to-resolution**, auto-replied versus handed-off. Whether the automation helps
  rather than adding a hop.

The first three need feedback the API does not capture. Wiring them is the first thing
worth building after the core system
([how](../../../docs/ROADMAP.md#the-feedback-loop)).

---

## Structured logs

One JSON object per line, one line per pipeline step, always carrying `ticket_id`.

```json
{"ts":"2026-08-31T10:14:02.113Z","level":"info","event":"tool_call",
 "ticket_id":"t_4f0c9a7b21e84d3fa6c5b8e0d1927354","tool":"get_invoices","args":{"user_id":"u_004"}}
{"ts":"2026-08-31T10:14:02.119Z","level":"warning","event":"handoff",
 "ticket_id":"t_4f0c9a7b21e84d3fa6c5b8e0d1927354","reason":"DATA_NOT_FOUND","detail":"get_invoices: user u_004 has no invoices"}
```

A line's fields after `ticket_id` are the trace step's own, so they differ by event.
`test_a_tool_call_log_carries_exactly_the_keys_that_step_has` pins the set.

**Logs and traces are not the same thing and do not serve the same reader.**

| | Trace | Log |
|---|---|---|
| Reader | support agent | engineer |
| Question | "why did the AI say this?" | "what is the system doing?" |
| Access | `GET /tickets/{id}` | log aggregator |
| Retention | with the ticket | short |
| Stack traces | never | yes |

The overlap is deliberate: the trace is written atomically at the end of a run, so a
process that dies mid-run leaves the log as the only evidence of what happened
([TRACEABILITY.md](../tracing/TRACEABILITY.md)).

Ticket `subject` and `body` are **not** logged. They are customer text of unknown
sensitivity, already stored with the ticket, and copying them into an aggregator widens
their exposure for no diagnostic gain.

---

## Wiring

**Every metric is derived from the finished trace, once**
([ADR 0014](../../../docs/adr/0014-metrics-derived-from-the-trace.md)). `record_run` walks
the same `list[TraceStep]` that `finalise` persists and `GET /tickets/{id}` serves.
`run_pipeline` calls it exactly once, **after** `repository.finalise` and below the
catch-all, so a run whose terminal write failed is not counted and `_decide` keeps the
"cannot write" property [ADR 0013](../../../docs/adr/0013-one-write-outside-the-catch-all.md)
gave it.

**The registry is injected, not reached for.** `run_pipeline` requires a `MetricRegistry`,
`create_app` owns one on `app.state`, and `GET /metrics` reads that same object — the
pattern `api/` already uses for `TicketRepository`. The module-level `REGISTRY` is the
production default in `create_app` and a default nowhere below it: a registry is
per-application state, so a fallback to the singleton would write where the endpoint never
reads, quietly, since the served numbers would be low rather than absent.

Each metric family locks its own counters, because `record_run` runs in the background
threadpool and two runs can finish at once.

**Structured logs come from the recorded step, live.** `TraceRecorder` takes an optional
`on_step` callback — injected, never imported, so `tracing/` stays below `observability/` —
and the orchestrator wires `log_step` into it under a `ticket_scope(ticket_id)`. Unlike the
metrics, the log is emitted step by step, which is what lets it survive a process death
that loses the not-yet-assembled trace. `configure_logging()` runs in the app lifespan;
level is `LOG_LEVEL` ([PACKAGING.md](../../../deploy/PACKAGING.md)), then `info`.

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
text. No client dependency and no push gateway — the point is to show what is worth
measuring and to have it counted, not to run a metrics stack inside a take-home.
`MetricRegistry` is the seam: swapping it for `prometheus_client` leaves `record_run` and
the endpoint untouched.
