# API

The HTTP surface. FastAPI. Two endpoints carry the brief's contract, plus two operational
ones.

Validate input, schedule work, serve ticket state — nothing else. No pipeline logic, no
tool access, no handoff decisions; this package calls the orchestrator and reads the
repository, and `test_layering.py` enforces it.

Why it is shaped this way:
[ADR 0001](../../../docs/adr/0001-asynchronous-in-process-processing.md).

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

**The id is 128 bits of randomness**, hex-encoded behind a `t_` prefix — not a sequence and
not a timestamped UUID, because the id is the only thing protecting a ticket's trace (see
Security below).

**Errors**

| Code | When |
|---|---|
| `422` | schema violation (missing field, wrong type, length) — FastAPI's default |

Note what is *not* here. A nonexistent `user_id` returns `202` and the ticket reaches
`handed_off` with `USER_NOT_FOUND`: whether a user exists is a fact only a tool can
establish, and that belongs in the trace. Rejecting at the edge would put the decision
outside the audit record.

---

## `GET /tickets/{id}`

Returns everything known about a ticket. This single call is the whole support-agent
interface: an agent must be able to answer "why did the AI say this?" from it alone.

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

**That is real output**, copied from a `POST` of the ticket above; only the reply body is
elided, at the `...`. `test_docs.py` drives that documented request through the real
pipeline and compares this block against what comes back, so an example that drifted from
the code fails the suite.

Every step carries `seq` and `ts`, from an injected clock
([ADR 0008](../../../docs/adr/0008-injected-clock-with-advancing-test-double.md)).

**A step carries only the keys that apply to it.** Seq 8 has no `tool` because a `reply`
decision calls none; seq 10 has no `reason` because a reply is not a handoff. The top-level
fields are the opposite and always present.

**Field contract**

| Field | `processing` | `replied` | `handed_off` |
|---|---|---|---|
| `reply` | `null` | the reply text | **always `null`** (ADR 0005) |
| `handoff_reason` | `null` | `null` | the enum member |
| `trace` | `[]` — see below | complete | complete, ends in `final_decision` |
| `updated_at` | equal to `created_at` | when the run finished | when the run finished |

`subject` and `body` are not echoed back: whoever reads a ticket already has the customer's
words, and this response exists to say what the service did with them. `updated_at` minus
`created_at` is how long the pipeline took — the per-ticket figure; the aggregate is
`pipeline_duration_seconds{outcome}` on `GET /metrics`.

`reply` and `handoff_reason` are mutually exclusive: exactly one is non-null in a terminal
state, neither is in `processing`.

**The trace of a `processing` ticket is empty, not partial.** Steps are written with the
terminal state in one transaction, so a ticket is never observed as `replied` with a
half-written trace ([STORAGE.md](../storage/STORAGE.md)). The cost is that a `GET` mid-run
shows the ticket exists and nothing else — milliseconds wide under `FakeLLM`, real under a
real model, and the trade [TRACEABILITY.md](../tracing/TRACEABILITY.md) weighs and
declines.

**Errors**

| Code | When |
|---|---|
| `404` | no ticket with that id — `{"detail": "no ticket with that id"}` |

The `404` body does not echo the id that was asked for: a ticket id is the only thing
protecting a trace, so the API does not copy one into a body that a proxy, a log or a
screenshot might outlive.

---

## Polling

There is no webhook and no streaming. A client polls `GET` until `status != "processing"`.

Acceptable because the fake pipeline completes in milliseconds, so the first `GET` after
the `POST` already returns a terminal state. Under a real LLM this becomes a real wait, and
the honest answer is a callback or SSE
([roadmap](../../../docs/ROADMAP.md#push-instead-of-polling)).

**In tests this is a non-issue.** Starlette's `TestClient` drains background tasks before
returning the `POST` response, so a test can `POST` then immediately `GET` a terminal status
with no sleep and no race — a large part of why ADR 0001 chose in-process background tasks
over a queue.

---

## Operational endpoints

Not part of the brief's contract; they exist to make the service operable.

| Endpoint | Returns |
|---|---|
| `GET /health` | `200 {"status": "ok"}` — or `503 {"status": "unavailable"}`. Backs the container `HEALTHCHECK` ([PACKAGING.md](../../../deploy/PACKAGING.md)) |
| `GET /metrics` | `200 text/plain` — the in-process counters and histograms from [OBSERVABILITY.md](../observability/OBSERVABILITY.md), in Prometheus exposition format |

**`/health` asks the repository a real question** rather than returning a constant. A check
that only proves the process is running is worse than none: the container reports healthy,
traffic keeps arriving, and every request fails against a database that is gone. Every
storage failure is reported the same way, because they all mean the same thing to whoever
is reading it.

**`/metrics` renders the `MetricRegistry`** `create_app` built and every run writes to
([ADR 0014](../../../docs/adr/0014-metrics-derived-from-the-trace.md)). It always names its
families; only the sample lines wait for the first run, so a scrape is never an empty body a
dashboard could misread as "nothing is wrong". The background task and this endpoint share
one object, both from `app.state`.

Both are unauthenticated. So is everything else — see below.

---

## The demo page

`GET /ui` serves a static page that submits tickets and renders their traces
([DEMO.md](../demo/DEMO.md)). It is a `StaticFiles` mount, not a router: files go out, and
nothing comes back in through it.

**It adds no data access.** The page drives `POST /tickets` and `GET /tickets/{id}` — the
same two endpoints a `curl` would — so everything it can read was already being served to
anyone who asked. In particular there is still no listing endpoint: the page remembers the
ids it was given, because a `GET /tickets` would be unauthenticated bulk access to customer
data ([roadmap](../../../docs/ROADMAP.md#cross-ticket-queries)).

---

## Structure

```
api/
  __init__.py
  app.py            FastAPI app factory, lifespan (DB open/close, logging), the `/ui` mount, the module-level `app`
  routes.py         the ticket endpoints
  ops.py            /health and /metrics
  schemas.py        Pydantic request/response models
  dependencies.py   app.state, read back as the protocols a handler is typed against
  API.md            this file
```

`app.py` builds the app via a factory so tests can inject an `InMemoryTicketRepository` and
a stub `LLMClient` without touching globals. It also exposes `app = create_app()` for
`uvicorn support_assistant.api.app:app`; nothing opens a database at import time.

**The lifespan closes the repository only when it built one.** The connection outlives a
request by design, so something must own it for the process — but an injected repository
belongs to whoever passed it in, and closing a caller's connection at shutdown breaks its
owner. Both halves are pinned by `test_api.py`.

`dependencies.py` exists so a handler's signature says `TicketRepository`, not
`request.app.state.repository`.

---

## Security: what is not here, stated once

**The API is entirely unauthenticated, and that is a real vulnerability, not an oversight.**
Anyone who can reach the service can post tickets and read any ticket whose id they hold.

What an attacker with a ticket id gets: the customer's name, their invoice ids, amounts and
payment statuses, their charging stations — the whole trace, which exists precisely to be
complete. `GET /metrics` separately exposes ticket volumes and the handoff breakdown to
anyone who asks; in production it belongs on an internal listener.

The only control is that ticket ids are 128 bits of randomness, so they cannot be enumerated
or guessed. That makes them a bearer token in everything but name — a weak design, because
bearer tokens leak through logs, referrers and shared URLs, and these never expire.

This is scope, deliberately: authentication exercises nothing the brief is evaluating, and
building it badly would be worse than not building it. It is the first thing that must exist
before this serves real customers
([ROADMAP](../../../docs/ROADMAP.md#authentication-and-rate-limiting)).

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
