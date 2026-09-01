# ADR 0013 — One write, outside the catch-all

**Status:** Accepted · 2026-09-01

## Context

[ADR 0005](0005-fail-closed-to-human-handoff.md) put a catch-all around the whole run, so
that any unanticipated exception becomes a `TOOL_ERROR` handoff rather than a ticket
stranded in `processing`. The first implementation took "the whole run" literally: the
`try` block also contained the calls that *wrote* the terminal state.

```python
try:
    ...
    return finish_replied(repository, trace, ticket_id, reply)   # inside the try
except Exception as exc:
    return finish_handoff(repository, trace, ticket_id, HandoffReason.TOOL_ERROR, repr(exc))
```

So the catch-all guarded the writes it existed to produce. A failing
`repository.finalise` was caught and answered with another `finalise` — and since
`finish_*` records the `final_decision` step before persisting, the retry appended a
second one. Run against a repository whose first write fails:

```
finalise call 1: status=replied      reason=None
finalise call 2: status=handed_off   reason=TOOL_ERROR
PERSISTED: handed_off TOOL_ERROR      final_decision count persisted: 2
```

The persisted trace carried `final_decision: replied` followed by
`final_decision: handed_off`, on a ticket that is `handed_off` — the first describing an
outcome that was never persisted. That contradicts `TraceRecorder.final_decision`
("exactly once, always last"), [TRACEABILITY.md](../../src/support_assistant/tracing/TRACEABILITY.md),
and the `FinalDecision` docstring, in the one artifact requirement 5 exists to produce.

The convergence rule was doing less work than it appeared to. "Every exit converges on
two functions" was true of six call sites scattered across a `try` and three `except`
clauses, and held by reading rather than by construction.

## Decision

**Deciding and writing are separate functions, and only one of them touches storage.**

- `_decide(ticket, trace, ...) -> Outcome` runs the ticket: classify, loop, ground. It
  holds the catch-all. **It is not given the repository**, so it cannot write a terminal
  state — the property is structural, not a convention.
- `Outcome` is a `NamedTuple` in the shape `finalise` takes, built only by `replied()`
  and `handed_off()`. Those two constructors are what remains of `finish_replied` /
  `finish_handoff`: they describe an outcome instead of performing one.
- `run_pipeline` calls `_decide`, then records the `final_decision` and persists it, once,
  below the catch-all. There is no branch at the write: `Outcome` already carries the
  status/reply/reason pairing, so no arm of a condition can record one thing and persist
  another.

**A failing write propagates.** It is the one failure the pipeline cannot fail closed
over, because failing closed is itself a write. Catching it to write again is what
produced the corrupt trace above; catching it to write *nothing* would be a silent
swallow. It joins the unknown-ticket-id `ValueError` as a bug to surface, and the ticket
it leaves in `processing` is the reaper's job
([roadmap](../ROADMAP.md#durable-work-and-a-reaper)) — a stranded ticket needs one either
way, which [TRACEABILITY.md](../../src/support_assistant/tracing/TRACEABILITY.md) already
accepts as the trade behind the single atomic write.

## Consequences

- **A terminal ticket carries exactly one `final_decision`, by construction.** Not because
  six call sites each remember to write one, but because there is one write.
- `run_pipeline` is six statements, and reads as the shape of the run: fetch, decide,
  record, persist.
- ADR 0005 is unchanged in substance. The catch-all still wraps everything that can fail
  in a way a handoff can describe; it simply no longer wraps the act of describing it.
- Each `HandoffReason` is still produced at exactly one place in the code, which is what
  keeps handoff-rate-by-reason trustworthy
  ([OBSERVABILITY.md](../../src/support_assistant/observability/OBSERVABILITY.md)). The
  reasons did not move; only the write did.
- The `Reply` branch returns `_ground(...)` directly, which removed the `draft: Reply |
  None` variable and the `assert draft is not None` that guarded it — an assertion
  `python -O` strips, standing in for a nullable that no longer needs to exist.
- `PIPELINE.md`'s "every exit converges on two functions" becomes "every exit produces an
  `Outcome`, and one line writes it".

## Alternatives considered

**Keep the writes inside, and reset the recorder before retrying.** Preserves the current
shape and fixes the double step. Rejected: it buys a mutating method on `TraceRecorder` —
which [ROADMAP.md](../ROADMAP.md#narrowing-a-traces-referenced-ids) deliberately defers
elsewhere — to make a retry work that cannot succeed anyway when storage is the thing
that failed.

**Catch the write failure and swallow it.** The ticket stays in `processing` with no
exception raised. Rejected outright: it is the stuck-in-`processing` outcome ADR 0005
exists to prevent, with the alarm removed as well.

**A `Replied | HandedOff` union matched at the write site.** Slightly more precise types —
no optional fields on `Outcome` at all. Rejected because it reintroduces a `match` at the
write, and the pairing is already validated twice downstream: by `FinalDecision`'s
validator and by `Ticket`'s
([ADR 0011](0011-shared-vocabulary-below-the-components.md)). A branchless write is worth
more here than the fourth enforcement point.
