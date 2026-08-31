# ADR 0008 — An injected clock, with an advancing test double

**Status:** Accepted · 2026-08-31

## Context

The trace model originally carried `seq` but no timestamps. That was an omission rather
than a decision, and noticing it exposed a genuine conflict between two things the design
already committed to.

An audit record wants wall-clock time. "Why did the AI say this?" is often followed by
"and how long did it take?", or "what else was happening at 10:14?". Without timestamps
the trace can order events but cannot place or measure them, and
`pipeline_duration_seconds` has no source of truth to be derived from.

But [ADR 0006](0006-fake-first-llm-behind-a-client-protocol.md) makes determinism a
property of the whole system — `FakeLLM` has no clock and no randomness — and
`TESTS.md` leans on it: an end-to-end test asserts an exact trace. Reading
`datetime.now()` inside the recorder would make every trace unique and every exact-trace
assertion impossible.

## Decision

Time is a dependency, not an ambient fact.

```python
class Clock(Protocol):
    def now(self) -> datetime: ...
```

- `SystemClock` returns real UTC time. Used by the service.
- `FrozenClock` starts at a fixed instant and **advances a fixed tick (10ms) on every
  call**. Used by tests.
- Every trace step carries `ts`. The `TraceRecorder` takes a `Clock`; nothing in the
  system calls `datetime.now()` directly.

The advancing tick is the part worth arguing about. A frozen-constant clock would also be
deterministic and is simpler, but it makes every step in a test carry an identical
timestamp — which means duration is always zero, `pipeline_duration_seconds` ships with
no test coverage at all, and, more subtly, two steps recorded out of order become
invisible. Identical timestamps hide the bug that timestamps exist to reveal.

An advancing tick keeps full determinism — the same run produces the same timestamps
every time — while making durations exact, assertable, and wrong when the code is wrong.

## Consequences

- The trace answers timing questions, not just ordering ones.
- Tests stay byte-for-byte reproducible, so exact-trace assertions survive.
- `pipeline_duration_seconds` gets real test coverage: with a known tick and a known
  number of steps, the expected duration is arithmetic.
- Out-of-order step recording is detectable, because timestamps must increase with `seq`.
  A contract test asserts exactly that.
- `Clock` joins the repository, LLM client, and tool registry as an injected dependency.
  One more constructor argument, and one more thing a test must assemble — mitigated by a
  fixture that builds a default test pipeline.
- Threading a clock through afterwards would have touched every component that records
  anything, which is why this is settled before implementation rather than during it.
- `ts` is a column on `trace_steps`, so the schema carries it too
  ([STORAGE.md](../../src/support_assistant/storage/STORAGE.md)).

## Alternatives considered

**No timestamps; `seq` only.** Simplest, and preserves determinism for free. Rejected: an
audit record that cannot say when is a weak audit record, and it leaves the duration
metric unsourced.

**Timestamps on the ticket only** (`created_at` / `updated_at`), not per step. Cheap, and
enough for coarse latency. Rejected as a false economy: it tells you a run took 900ms but
not which tool ate it, and that second question is the one anybody actually asks.

**Real clock, tests assert loosely** (structure and ordering, not exact traces). No
injection needed. Rejected: it weakens every trace assertion in the suite to buy back
something injection provides for one constructor argument.
