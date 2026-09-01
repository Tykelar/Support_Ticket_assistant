# Roadmap

Everything deliberately left out, and how it would be done.

Deferred work is described at three depths in this repository. A component doc states its
gap in one line, where a reader of that code will trip over it. The README summarises the
important ones in a paragraph, because the brief asks for that. **This file is the deep
layer** — each entry says what it is, why it was deferred, and concretely how it would be
built.

Nothing here is a discovered gap. Each was a decision, and the ones with real trade-offs
behind them have an ADR in [adr/](adr/).

Roughly in the order I would do them.

---

## The feedback loop

**What.** Reopen rate, agent override rate, and sampled human review of replied tickets.

**Why deferred.** Needs data the API does not currently capture, and none of it exercises
the pipeline the brief is evaluating.

**Why it is first.** Every metric the system emits today measures whether it is *behaving*
— handoff rates, iteration counts, grounding violations. None measures whether the replies
are any **good**. A confidently wrong reply that a customer never bothers to challenge is
invisible to all of them. Without this, there is no ground truth to tune anything against.

**How.**
1. `POST /tickets/{id}/outcome` accepting `{reopened | overridden | accepted}` plus an
   optional corrected reply, written to a new `ticket_outcomes` table.
2. The support tool calls it when an agent edits or retracts a sent reply; the ticket
   ingestion path calls it when a customer replies again within a window.
3. Counters `replies_reopened_total` and `replies_overridden_total`, sliced by intent and
   by template — per-template override rate is what tells you *which* template is wrong.
4. A sampling job that flags 1% of replied tickets for human rating, since the first two
   only catch failures a customer or agent noticed.

**Effort.** ~half a day for the endpoint and counters; the sampling workflow is a product
question as much as an engineering one.

**Unblocks.** Any tuning of classification, templates, or the handoff thresholds. Also
makes an accuracy SLO expressible.

---

## Durable work and a reaper

**What.** Survive a process restart without stranding tickets.

**Why deferred.** [ADR 0001](adr/0001-asynchronous-in-process-processing.md) — a queue and
a worker are the production-correct answer, but they add a broker and a second process to
`docker compose` for no gain against what the brief asks about.

**The gap it closes.** In-process background work dies with the process. A ticket sits in
`processing` forever: no reply, no handoff, no error, nobody paged.

**How.**
1. Short term, and cheap: a reaper. A periodic task queries
   `SELECT id FROM tickets WHERE status='processing' AND updated_at < now() - interval`
   (the `idx_tickets_status` index in
   [STORAGE.md](../src/support_assistant/storage/STORAGE.md) already exists for this) and
   fails each closed with a new `HandoffReason.ABANDONED`. Turns silent loss into a
   handoff, which is the correct failing-closed behaviour.
2. Properly: replace the `BackgroundTask` with an enqueue. The `TicketRepository`
   boundary is the seam — the API writes the ticket and pushes an id; a worker process
   consumes it and calls the same `run_pipeline`. Redis + RQ, or Postgres
   `SELECT ... FOR UPDATE SKIP LOCKED` to avoid a new dependency.
3. Make the pipeline idempotent on re-delivery so at-least-once is safe: `finalise` on an
   already-terminal ticket is a no-op rather than an overwrite.

**Effort.** Reaper, ~1h. The queue, most of a day with compose changes.

**Unblocks.** Horizontal scaling; retry on transient failure below.

---

## Semantic grounding

**What.** Verify that a reply is *true*, not merely that its literals are *sourced*.

**Why deferred.** [ADR 0004](adr/0004-two-layer-grounding-enforcement.md). Layer 1 covers
it today, because status words are enumerated `FactSet` values rather than free text — so
under `FakeLLM` a sentence cannot contradict its facts.

**The gap.** `GroundingChecker` would not catch *"your invoice of EUR 42.10 was paid"* when
42.10 is real and the status is `failed`. Every literal is sourced; the sentence is a lie.
The moment a real model writes the prose, layer 1 stops covering this.

**How.**
1. Extract `(subject, predicate, object)` claims from the reply — for a bounded template
   vocabulary this is a parse, not NLP.
2. Check each against the `FactSet` as an entailment: does a fact assert this, contradict
   it, or neither? Contradiction and "neither" both fail closed to
   `UNGROUNDED_REPLY`, extending the existing `Violation` type with a `class` of
   `contradiction`.
3. For a real LLM, the cheap version is a second model call as a judge: "does the
   following reply follow from these facts?". Slower and itself fallible, so it belongs
   behind the deterministic check, not instead of it.

**Effort.** ~a day for the template-scoped version; the general case is open-ended.

**Prerequisite.** Only worth building alongside a real LLM. Under the fake it would be
dead code.

---

## Entity coverage in grounding layer 2

**What.** Catch invented open-vocabulary strings — a station name, a plan name — not just
numbers, identifiers, and status words.

**Why deferred.** Under `FakeLLM` it cannot happen: layer 1 makes an unsourced string
unreachable. Extraction of arbitrary entities from free text is also the least reliable
part of the checker.

**How.** Invert the check for strings. Rather than extracting entities from the reply and
looking them up, take the closed set of entity-shaped values across *all* fixtures
(station names, plan names) and assert that any occurring in the reply is in *this
ticket's* `FactSet`. That catches the realistic failure — a model recalling a real station
from training or from another user's data — without needing a general entity extractor.
Genuinely novel invented nouns still slip through; note that limit rather than claim
otherwise.

**Effort.** ~2h.

**Prerequisite.** Same as above — matters only with a real LLM.

---

## Replies in the user's language

**What.** Reply in the profile's `language` rather than always English.

**Why deferred.** [ADR 0006](adr/0006-fake-first-llm-behind-a-client-protocol.md).
Templating in three languages triples the template surface — and the
`TEMPLATE_SAFE_LITERALS` surface with it — while exercising no part of the system under
evaluation. The field is read and traced today, so the pipeline demonstrably has it.

**How.** Key the template registry on `(intent, template_name, language)` and select using
the `language` fact from `get_user`. Each localised template carries its own
`TEMPLATE_SAFE_LITERALS`, since "3 business days" is a different literal set in each
language. Fall back to English on a missing translation rather than handing off — an
English reply is a degradation, not a correctness failure. Number and date formatting also
becomes locale-dependent, which the grounding checker's `Decimal` normalisation already
handles for `42,10` versus `42.10`.

**Effort.** ~2h per language, mostly copy.

**Note.** This is a template change, not an architecture change. That is why it was safe
to defer.

---

## Retry on transient tool failure

**What.** Retry a failed tool call instead of ending the run.

**Why deferred.** Deliberate, not forgotten
([PIPELINE.md](../src/support_assistant/pipeline/PIPELINE.md) point 5). Retrying
multiplies iterations against the cap, and letting the model see an error gives it room to
work around a failure rather than surface it.

**How.** Retry in the **registry**, not in the loop — bounded (2 attempts), with backoff,
and only for a new `TransientToolError` that a tool raises for a timeout or connection
failure. `ToolExecutionError` and `NoDataAvailable` stay terminal. This keeps retries
invisible to the model and to the iteration cap, which is the property that makes them
safe. Record each attempt in the trace so a retried call is visible rather than hidden.

**Effort.** ~2h.

**Prerequisite.** Only meaningful once tools call something that can fail transiently.
Against local JSON fixtures there is no such failure mode.

---

## Idempotent ticket submission

**What.** A retried `POST /tickets` returns the original ticket instead of creating a
second one.

**Why deferred.** The brief does not ask for it, and it exercises nothing being evaluated.
Recorded because duplicate auto-replies to one customer complaint is a visible,
embarrassing failure, and its absence should read as a decision rather than an oversight.

**How.** Accept an optional `Idempotency-Key` header. Add a unique index on it in
`tickets`. On a repeat, return `200` with the existing ticket rather than `202` with a new
one — a different status code so a client can tell the difference. Scope keys per
`user_id` so two users cannot collide. Without the header, behaviour is unchanged.

**Effort.** ~1h.

---

## Authentication and rate limiting

**What.** Anything at all. There is currently none.

**Why deferred.** Auth is a large surface exercising nothing the brief evaluates, and a
half-built version is worse than none.

**The gap, precisely.** Anyone who can reach the service can post tickets. Anyone holding
a ticket id can read that ticket's whole trace: customer name, invoice ids, amounts,
payment statuses, charging stations. `GET /metrics` gives ticket volumes and the handoff
breakdown to anyone. The only control is that ticket ids are 128 bits of randomness, which
makes an id a bearer token in all but name — one that never expires and leaks through
logs, referrers, and shared URLs.

**How.**
1. Two audiences, two mechanisms. The ticket-ingestion caller is a service: a signed
   service token, or mTLS. Support agents reading traces are people: an OIDC session with
   a role check.
2. Authorise reads on *ownership*, not on possession of the id — an agent may read a
   ticket because of their role, a customer only their own. That removes the bearer-token
   property entirely.
3. `/metrics` and `/health` move to an internal listener, not the public one.
4. Rate limit `POST /tickets` per authenticated caller — each ticket triggers model and
   tool work, so it is an amplification vector.
5. Audit-log every trace read. The trace is customer data; who looked at it matters.

**Effort.** 1–2 days done properly, which is exactly why it is not in a take-home.

**Do this before anything else on this page** if the service ever faces real customers.

---

## Narrowing a trace's referenced ids

**What.** `tool_result.referenced` lists every identifier a tool call returned. Narrow it
to the ids the rendered reply actually cites.

**Why deferred.** The full list is a safe superset — a reader can still trace any
statement in the reply back to a source record, which is the property
[TRACEABILITY.md](../src/support_assistant/tracing/TRACEABILITY.md) needs it for. Doing
the narrowing means rewriting a step the `TraceRecorder` has already stamped, so it buys a
mutating method on the recorder, and the trade is worth making deliberately rather than in
passing. It is also only a partial win: `count` and `statuses` still describe every row.

**How.** After a passing grounding check the orchestrator already holds the identifiers,
as the `IDENTIFIER` literals from `GroundingChecker.extract`. Either add a narrowing pass
on the recorder before `finalise`, or build `tool_result` steps at persist time rather
than in the loop — the second is cleaner but changes when a step gets its timestamp, which
the ordering contract depends on.

**Effort.** ~1h, plus a decision about which of the two shapes to take.

---

## Trace retention

**What.** A policy. Traces currently accumulate forever.

**Why deferred.** No storage pressure at this scale, and the right retention period is a
legal and product question rather than an engineering one.

**How.** Decide the period from how long the audit record must be answerable — likely tied
to billing dispute windows. Then a scheduled delete of `trace_steps` older than the
window, keeping the ticket row and its outcome (which are small and are what aggregate
reporting needs). Consider redacting rather than deleting: retaining a trace's shape
without its customer data preserves debugging value at much lower exposure.

**Effort.** ~2h once the period is decided.

---

## Push instead of polling

**What.** Tell the client when a ticket reaches a terminal state.

**Why deferred.** The fake pipeline completes in milliseconds, so the first `GET` after a
`POST` already returns a terminal state. Polling is invisible at this speed.

**The gap.** Under a real LLM this becomes a real wait, and polling becomes wasteful and
slow.

**How.** A webhook is the right fit for a ticketing integration — a registered callback
URL, called on transition to `replied` or `handed_off`, with retries and a signed payload.
SSE on `GET /tickets/{id}/events` is nicer for a human-facing UI. Both, if there are both
consumers; the webhook first.

**Effort.** ~half a day with retry handling.

---

## Bounded concurrency

**What.** A limit on simultaneous pipeline runs.

**Why deferred.** Single process, in-memory tasks, and a fake LLM that returns instantly.
There is nothing to contend for yet.

**The gap.** A burst of `POST`s becomes an unbounded burst of concurrent pipelines. With a
real LLM that is a burst of concurrent model calls, which will hit a provider rate limit
or exhaust memory.

**How.** An `asyncio.Semaphore` around `run_pipeline` sized from the provider's
concurrency budget, with tickets queueing behind it — `processing` already means "not
finished", so queueing needs no new state. Once the durable queue above exists, worker
count replaces the semaphore and this disappears.

**Effort.** ~1h.

---

## Postgres and multiple writers

**What.** Replace SQLite when more than one process writes.

**Why deferred.** [ADR 0003](adr/0003-sqlite-behind-a-repository-protocol.md). WAL makes
SQLite safe for the single-process service, and a file is a much better fit for a
reviewer's clean clone than a database container.

**How.** Implement `TicketRepository` against Postgres — the protocol is the seam, and the
existing contract test suite already runs against every implementation, so the new one
inherits its coverage on day one. The schema translates directly; the `CHECK` constraint
enforcing the reply/handoff invariant survives unchanged.

**Effort.** ~half a day including compose changes.

**Prerequisite.** Only needed alongside the durable queue.

---

## Cross-ticket queries

**What.** List, search, filter tickets.

**Why deferred.** The brief's access pattern is by id, and a listing endpoint is
unauthenticated bulk access to customer data (see authentication above).

**How.** Behind auth, a `GET /tickets` with cursor pagination and filters on status,
handoff reason, and date. Aggregate questions — "how often does grounding fail?" — should
go to metrics rather than to trace queries; that separation is deliberate and worth
keeping.

**Effort.** ~3h.

---

## Hardening the real LLM client

**What.** Make `OllamaLLM` production-shaped rather than merely wired.

**Why deferred.** An explicit bonus, "never at the expense of the core requirements", and
prompt quality is what the brief says it is *not* grading.

**How.** Timeouts and a circuit breaker (a model server that hangs must fail closed, not
hold the pipeline open); response schema validation so a malformed tool call becomes
`TOOL_ERROR` rather than an exception; token accounting per ticket; and a golden-file
evaluation set of tickets with expected intents and tool sequences, so a prompt change
that regresses classification is caught before it ships. That evaluation set is the piece
that matters most, and it is also the piece that only makes sense once a real model is in
use.

**Effort.** ~a day.
