# API

The HTTP surface. FastAPI. Two endpoints carry the brief's contract, plus two operational
ones.

**Responsibility:** validate input, schedule work, serve ticket state. Nothing else. This
package contains no pipeline logic, no tool access, and no handoff decisions — it calls
the orchestrator and reads the repository. If business logic ever appears here, it is in
the wrong place.

**Why it is shaped this way:** [ADR 0001](../../../docs/adr/0001-asynchronous-in-process-processing.md).

---

## `POST /tickets`

Accepts a ticket, schedules the pipeline, returns immediately.

**Request**

```json
{
  "user_id": "u_002",
  "subject": "My payment failed",
  "body": "I got an email saying my last invoice couldn't be charged. What happened?"
}
```

| Field | Type | Rules |
|---|---|---|
| `user_id` | string | required, non-empty. **Not** validated against the fixtures here — an unknown user is a pipeline handoff (`USER_NOT_FOUND`), not a `400` |
| `subject` | string | required, non-empty, max 200 |
| `body` | string | required, non-empty, max 5000 |

**Response — `202 Accepted`**

```json
{ "id": "t_4f0c9a7b21e84d3fa6c5b8e0d1927354", "status": "processing" }
```

`202`, not `201`: the resource exists, but the work it represents has not finished. The
pipeline runs as a `BackgroundTask` after the response is sent.

**The id is 128 bits of randomness**, hex-encoded behind a `t_` prefix. Not a sequence and
not a UUIDv4 timestamp — because the id is the only thing protecting a ticket's trace
(see below), so it must not be guessable or enumerable.

**Errors**

| Code | When |
|---|---|
| `422` | schema violation (missing field, wrong type, length) — FastAPI's default |

Note what is *not* here. A nonexistent `user_id` returns `202`, and the ticket
subsequently reaches `handed_off` with `USER_NOT_FOUND`. This is deliberate: whether a
user exists is a fact only a tool can establish, and establishing it is a pipeline step
that belongs in the trace. Rejecting at the edge would put that decision outside the
audit record.

---

## `GET /tickets/{id}`

Returns everything known about a ticket. This single call is the whole support-agent
interface — requirement 5 says an agent must be able to answer "why did the AI say this?"
from it alone.

**Response — `200 OK`**

```json
{
  "id": "t_b3573fe4cfe8131617109e95f7d1a365",
  "user_id": "u_002",
  "status": "replied",
  "reply": "Hi Ben Carter,\n\nThanks for getting in touch. Invoice inv_204 for 42.10 EUR has a failed payment, so that amount is still outstanding.\n\n...",
  "handoff_reason": null,
  "created_at": "2026-09-01T15:03:50.833413Z",
  "updated_at": "2026-09-01T15:03:50.837967Z",
  "trace": [
    { "seq": 1, "ts": "2026-09-01T15:03:50.835968Z", "type": "intent_classified",
      "intent": "billing_question",
      "matched_keywords": ["charged", "invoice", "payment"] },
    { "seq": 2, "ts": "2026-09-01T15:03:50.835968Z", "type": "llm_decision",
      "iteration": 1, "decision": "tool_call", "tool": "get_user" },
    { "seq": 3, "ts": "2026-09-01T15:03:50.835968Z", "type": "tool_call",
      "tool": "get_user", "args": {"user_id": "u_002"} },
    { "seq": 4, "ts": "2026-09-01T15:03:50.836950Z", "type": "tool_result",
      "tool": "get_user", "ok": true, "summary": {"found": true, "plan": "basic"} },
    { "seq": 5, "ts": "2026-09-01T15:03:50.836950Z", "type": "llm_decision",
      "iteration": 2, "decision": "tool_call", "tool": "get_invoices" },
    { "seq": 6, "ts": "2026-09-01T15:03:50.836950Z", "type": "tool_call",
      "tool": "get_invoices", "args": {"user_id": "u_002"} },
    { "seq": 7, "ts": "2026-09-01T15:03:50.836950Z", "type": "tool_result",
      "tool": "get_invoices", "ok": true,
      "summary": {"count": 3, "statuses": {"paid": 2, "failed": 1},
                  "referenced": ["inv_204", "inv_203", "inv_202"]} },
    { "seq": 8, "ts": "2026-09-01T15:03:50.836950Z", "type": "llm_decision",
      "iteration": 3, "decision": "reply" },
    { "seq": 9, "ts": "2026-09-01T15:03:50.837967Z", "type": "grounding_check",
      "passed": true, "literals_checked": 3, "violations": [] },
    { "seq": 10, "ts": "2026-09-01T15:03:50.837967Z", "type": "final_decision",
      "outcome": "replied" }
  ]
}
```

**That is real output**, copied from a `POST` of the ticket above against a running
service; only the reply body is elided, at the `...`. An example nobody can reproduce is a
defect in this repo, so the shapes below are what the code emits, down to the key order.

Every step carries `seq` and `ts`; `ts` comes from an injected clock, so the trace is
timestamped in production and reproducible in tests
([ADR 0008](../../../docs/adr/0008-injected-clock-with-advancing-test-double.md)).

**A step carries only the keys that apply to it.** Seq 8 has no `tool` because a `reply`
decision calls none; seq 10 has no `reason` because a reply is not a handoff. The
top-level fields are the opposite and always present — see the contract below.

**Field contract**

| Field | `processing` | `replied` | `handed_off` |
|---|---|---|---|
| `reply` | `null` | the reply text | **always `null`** (ADR 0005) |
| `handoff_reason` | `null` | `null` | the enum member |
| `trace` | `[]` — see below | complete | complete, ends in `final_decision` |
| `updated_at` | equal to `created_at` | when the run finished | when the run finished |

`subject` and `body` are deliberately not echoed back: whoever reads a ticket already has
the customer's words, and this response exists to say what the service did with them.
`updated_at` is here because it is the only record of *when* — and `updated_at` minus
`created_at` is how long the pipeline took, which is the one latency figure available
before `observability/` lands.

`reply` and `handoff_reason` are mutually exclusive. Exactly one is non-null in a terminal
state; neither is in `processing`.

**The trace of a `processing` ticket is empty, not partial.** Steps accumulate in memory
during the run and are written with the terminal state in one transaction, so a ticket is
never observed as `replied` with a half-written trace
([STORAGE.md](../storage/STORAGE.md)). The cost is that a `GET` mid-run shows the ticket
exists and nothing else. Under `FakeLLM` that window is milliseconds wide; under a real
model it is real, and incremental persistence — a write per step, in exchange for that
atomicity — is the trade
[TRACEABILITY.md](../tracing/TRACEABILITY.md) weighs and declines.

**Errors**

| Code | When |
|---|---|
| `404` | no ticket with that id — `{"detail": "no ticket with that id"}` |

The `404` body does not echo the id that was asked for. A ticket id is the only thing
protecting a trace (see Security below), so the API does not copy one into a response body
that a proxy, a log or a screenshot might outlive.

---

## Polling

There is no webhook and no streaming. A client polls `GET` until `status != "processing"`.

Acceptable here because the fake pipeline completes in milliseconds, so in practice the
first `GET` after the `POST` already returns a terminal state. Under a real LLM this
becomes a real wait, and the honest answer is a callback or SSE
([roadmap](../../../docs/ROADMAP.md#push-instead-of-polling)) rather than pretended away.

**In tests this is a non-issue.** Starlette's `TestClient` drains background tasks before
returning the `POST` response, so a test can `POST` then immediately `GET` a terminal
status with no sleep and no race. That determinism is a large part of why ADR 0001 chose
in-process background tasks over a queue.

---

## Operational endpoints

Not part of the brief's contract; they exist to make the service operable.

| Endpoint | Returns |
|---|---|
| `GET /health` | `200 {"status": "ok"}` — or `503 {"status": "unavailable"}`. Backs the container `HEALTHCHECK` ([PACKAGING.md](../../../deploy/PACKAGING.md)) |
| `GET /metrics` &nbsp;·&nbsp; *awaiting `observability/`* | the in-process counters and histograms from [OBSERVABILITY.md](../observability/OBSERVABILITY.md) |

**`/health` asks the repository a real question** rather than returning a constant. A check
that only proves the process is running is worse than none: the container reports healthy,
traffic keeps arriving, and every request fails against a database that is gone. Any
failure from storage — missing file, locked database, corrupt page — is reported the same
way, because they all mean the same thing to whoever is reading it.

`/metrics` is not built. Its counters are produced by `observability/`, which does not
exist yet, and an endpoint serving an empty metric set would be worse than an absent one:
a dashboard reads zeros as "nothing is wrong".

Both are unauthenticated. So is everything else — see below.

---

## Structure

```
api/
  __init__.py
  app.py            FastAPI app factory, lifespan (DB open/close), the module-level `app`
  routes.py         the ticket endpoints
  ops.py            /health
  schemas.py        Pydantic request/response models
  dependencies.py   app.state, read back as the protocols a handler is typed against
  API.md            this file
```

`app.py` builds the app via a factory so tests can inject an `InMemoryTicketRepository`
and a stub `LLMClient` without touching module-level globals. It also exposes
`app = create_app()` for `uvicorn support_assistant.api.app:app`; nothing opens a database
at import time, so importing that module is free.

**The lifespan closes the repository only when it built one.** The connection outlives a
request by design (STORAGE.md), so something has to own it for the process; but an
injected repository belongs to whoever passed it in, and closing a caller's connection at
shutdown breaks its owner. Both halves are pinned by tests in `test_api.py`.

`dependencies.py` exists so a handler's signature says `TicketRepository`, not
`request.app.state.repository`. `api/` cannot tell a SQLite repository from an in-memory
one, and being typed against the protocol is what keeps that true.

---

## Security: what is not here, stated once

**The API is entirely unauthenticated, and that is a real vulnerability, not an oversight.**
Anyone who can reach the service can post tickets and read any ticket whose id they hold.

Concretely, what an attacker with a ticket id gets: the customer's name, their invoice
ids, amounts and payment statuses, their charging stations — the whole trace, which exists
precisely to be complete. `GET /metrics`, once `observability/` lands, will separately expose
ticket volumes and the handoff breakdown to anyone who asks.

The only control is that ticket ids are 128 bits of randomness, so they cannot be
enumerated or guessed. That makes the ids a bearer token in everything but name — which is
a weak design, because bearer tokens leak through logs, referrers, and shared URLs, and
these ones never expire.

This is scope, deliberately: authentication is a large surface that exercises nothing the
brief is evaluating, and building it badly would be worse than not building it. It is the
first thing that must exist before this serves real customers.
[ROADMAP: authentication and rate limiting](../../../docs/ROADMAP.md#authentication-and-rate-limiting).

## Also deliberately absent

- **Rate limiting** — each ticket triggers model and tool work, so an unauthenticated
  `POST` is an amplification vector.
  [ROADMAP](../../../docs/ROADMAP.md#authentication-and-rate-limiting).
- **Idempotency** — a retried `POST` creates a second ticket and a second reply.
  [ROADMAP](../../../docs/ROADMAP.md#idempotent-ticket-submission).
- **Pagination on the trace** — traces are bounded by `MAX_ITERATIONS`, so they cannot
  grow unboundedly. Not needed.
- **A cancel or retry endpoint** — terminal states are final; a re-run would be a new
  ticket. Not needed.
