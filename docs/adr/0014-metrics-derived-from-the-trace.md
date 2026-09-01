# ADR 0014 — Metrics derived from the trace, not incremented inline

**Status:** Accepted · 2026-09-01

## Context

Phase 9 adds the counters and histograms
[OBSERVABILITY.md](../../src/support_assistant/observability/OBSERVABILITY.md) specifies:
`tickets_total{status}`, `handoffs_total{reason}`, `grounding_violations_total{literal_class}`,
`tool_calls_total{tool,outcome}`, `iterations_per_ticket`, `pipeline_duration_seconds{outcome}`.

The obvious way to populate them is to increment as the run happens — a
`metrics.tool_calls_total.inc(...)` next to every `trace.tool_result(...)` in
`pipeline/orchestrator.py`, a `handoffs_total.inc(...)` in each of the six places a
`HandoffReason` is produced, a duration timer opened at the top of `_decide` and read at
the bottom. Roughly ten new call sites, all inside the one module the brief is actually
evaluating ("the system around the model — the loop, the guardrails, the failure
handling").

Two things make that unattractive here specifically:

- [ADR 0013](0013-one-write-outside-the-catch-all.md) went to some length to make `_decide`
  **unable to write** — it is not handed the repository, so a terminal state cannot be
  persisted from inside the catch-all. Inline `metrics.inc(...)` calls put a mutating side
  effect back into that function, and a metric written from inside the catch-all is a
  metric a retry double-counts, which is the exact shape of the bug ADR 0013 removed.
- Every one of those call sites is a place the number can disagree with the trace — an
  `inc` that runs when the matching `trace.*` call was skipped, or the reverse. The trace
  is already the audited, ordered, typed record of what the run did. A second record of
  the same events, kept in step by hand, is a liability.

Separately: whether to depend on `prometheus_client`.

## Decision

**The registry is folded from the finished trace, once, at the orchestrator's single
write site.**

`observability.metrics.record_run(registry, *, status, reason, steps)` walks the same
`list[TraceStep]` that `repository.finalise` persists and `GET /tickets/{id}` serves, and
updates every family from it. `run_pipeline` calls it immediately **after**
`repository.finalise`, below the catch-all, beside the `final_decision` write it mirrors.
`_decide` gains nothing: it still cannot write and now cannot count either.

`pipeline_duration_seconds` is `steps[-1].ts - steps[0].ts` — the span of the trace,
measured from timestamps the injected `Clock` already stamped
([ADR 0008](0008-injected-clock-with-advancing-test-double.md)), not a separate timer. So
under `FrozenClock` the duration is a known tick times a known number of steps, and it is
covered by a test like anything else rather than shipping as an untestable wall-clock
read.

**The registry is in-process and hand-rolled — no `prometheus_client`.** `Counter` and
`Histogram` are a few lines each; `render()` emits the text exposition format directly.
`MetricRegistry` is **injected** — `run_pipeline` takes one, `create_app` owns one on
`app.state`, `GET /metrics` reads that same object — with a module-level `REGISTRY` as the
production default, exactly the pattern of `tools=registry.run` and
`max_iterations=MAX_ITERATIONS` one layer down.

**Structured logs are emitted the same way — from the recorded step, not from a second
call site.** `TraceRecorder` gains one optional `on_step` callback, invoked once per step
in `_record`; the orchestrator wires `observability.logging.log_step` as it. The recorder
does not import `observability/` — the hook is injected — so `tracing/` stays below it in
the dependency graph. Unlike the metrics, the logs are emitted **live**, step by step, so
a process that dies mid-run still leaves per-step evidence even though the trace is written
atomically only at the end.

## Consequences

- **One call site for metrics, and it reads as part of the write:** decide, record,
  persist, count. No instrumentation threaded through the loop.
- **A run and its numbers cannot drift.** They are computed from the artifact the run is
  judged by. A metric that looked wrong would mean the trace is wrong, which is a bigger
  bug and one other tests already guard.
- **`_decide` keeps the property ADR 0013 gave it** — no repository, no writes, and now no
  metric mutations. The catch-all still wraps only things a handoff can describe.
- **A run whose `finalise` raised is not counted.** `record_run` is after the persist, so
  the counters never claim an outcome storage does not hold. The cost is a blind spot:
  a stranded ticket contributes to nothing on `/metrics` — which is precisely the gap
  `tickets_processing_age_seconds` (the stranded-ticket gauge, deferred to the reaper —
  [ROADMAP](../ROADMAP.md#durable-work-and-a-reaper)) exists to cover. The two together
  are complete; neither alone is.
- **Metrics for a run are lost if the process dies before `record_run`.** So is the
  persisted terminal state, so this adds no new failure mode — and the per-step log is the
  live record that survives it.
- **`handoffs_total{reason}` stays trustworthy.** Each `HandoffReason` is still produced at
  exactly one place in the orchestrator; `record_run` reads the `final_decision` step's
  reason, so the count is the enum, not a rough grouping.
- **Swapping in `prometheus_client` is contained** — `MetricRegistry` is the seam, the
  same way `TicketRepository` is the seam for Postgres. `record_run` and the endpoint do
  not change.
- `record_run` is total by construction (closed enums, a closed set of step types), so it
  needs no `try` around it at the call site — a bare call, like the `finalise` above it.

## Alternatives considered

**Increment inline, as the run happens.** The conventional shape. Rejected here because it
scatters ~10 mutations through `pipeline/orchestrator.py`, each a point where the metric
and the trace can disagree, and because it reintroduces a side effect into `_decide` that
[ADR 0013](0013-one-write-outside-the-catch-all.md) deliberately removed — including
inside the catch-all, where a retry would double-count. The one thing it buys —
sub-step timing granularity — is not on OBSERVABILITY.md's list.

**Depend on `prometheus_client`.** Battle-tested exposition, histograms, exemplars.
Rejected: it runs a metrics stack inside a take-home to serve six families, and the
registry seam makes adopting it later a contained change rather than a rewrite. The point
of this phase is to show what is worth measuring and have it counted, not to operate
Prometheus.

**A dedicated metrics recorder passed alongside the `TraceRecorder`.** Symmetric with the
trace: two recorders, each notified of each event. Rejected for the same reason as inline
increments — it is a second running record of the same events to keep in step — and it is
strictly worse than deriving from the trace the first recorder already produced.

**Emit the step logs by deriving from `trace.steps` after the run**, like the metrics.
Rejected for the logs specifically: OBSERVABILITY.md's reason for the log to exist is that
it survives a process death that loses the not-yet-assembled trace. A log written only at
the end would not. Hence the live `on_step` hook for logs, and the after-the-fact
`record_run` for metrics — the difference is deliberate.
