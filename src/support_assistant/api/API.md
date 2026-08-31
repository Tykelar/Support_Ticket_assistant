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
{ "id": "t_9f3a2b1c", "status": "processing" }
```

`202`, not `201`: the resource exists, but the work it represents has not finished. The
pipeline runs as a `BackgroundTask` after the response is sent.

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
  "id": "t_9f3a2b1c",
  "user_id": "u_002",
  "status": "replied",
  "reply": "Hi Ben, your invoice inv_204 for EUR 42.10 has a status of failed...",
  "handoff_reason": null,
  "created_at": "2026-08-31T10:14:02Z",
  "trace": [
    { "seq": 1, "ts": "2026-08-31T10:14:02.101Z", "type": "intent_classified",
      "intent": "billing_question", "matched_keywords": ["payment", "invoice"] },
    { "seq": 2, "ts": "2026-08-31T10:14:02.104Z", "type": "llm_decision",
      "iteration": 1, "decision": "tool_call", "tool": "get_user" },
    { "seq": 3, "ts": "2026-08-31T10:14:02.106Z", "type": "tool_call",
      "tool": "get_user", "args": {"user_id": "u_002"} },
    { "seq": 4, "ts": "2026-08-31T10:14:02.109Z", "type": "tool_result",
      "tool": "get_user", "ok": true, "summary": {"found": true, "plan": "basic"} },
    { "seq": 5, "ts": "2026-08-31T10:14:02.111Z", "type": "llm_decision",
      "iteration": 2, "decision": "tool_call", "tool": "get_invoices" },
    { "seq": 6, "ts": "2026-08-31T10:14:02.112Z", "type": "tool_call",
      "tool": "get_invoices", "args": {"user_id": "u_002"} },
    { "seq": 7, "ts": "2026-08-31T10:14:02.117Z", "type": "tool_result",
      "tool": "get_invoices", "ok": true,
      "summary": {"count": 3, "statuses": {"paid": 2, "failed": 1},
                  "referenced": ["inv_204"]} },
    { "seq": 8, "ts": "2026-08-31T10:14:02.119Z", "type": "llm_decision",
      "iteration": 3, "decision": "reply" },
    { "seq": 9, "ts": "2026-08-31T10:14:02.121Z", "type": "grounding_check",
      "passed": true, "literals_checked": 3 },
    { "seq": 10, "ts": "2026-08-31T10:14:02.122Z", "type": "final_decision",
      "outcome": "replied" }
  ]
}
```

Every step carries `seq` and `ts`; `ts` comes from an injected clock, so the trace is
timestamped in production and reproducible in tests
([ADR 0008](../../../docs/adr/0008-injected-clock-with-advancing-test-double.md)).

**Field contract**

| Field | `processing` | `replied` | `handed_off` |
|---|---|---|---|
| `reply` | `null` | the reply text | **always `null`** (ADR 0005) |
| `handoff_reason` | `null` | `null` | the enum member |
| `trace` | partial, grows | complete | complete, ends in `final_decision` |

`reply` and `handoff_reason` are mutually exclusive. Exactly one is non-null in a terminal
state; neither is in `processing`.

**Errors**

| Code | When |
|---|---|
| `404` | no ticket with that id |

---

## Polling

There is no webhook and no streaming. A client polls `GET` until `status != "processing"`.

Acceptable here because the fake pipeline completes in milliseconds, so in practice the
first `GET` after the `POST` already returns a terminal state. Under a real LLM this
becomes a real wait, and the honest answer is a callback or SSE — recorded in the README
as a "with more time" item rather than pretended away.

**In tests this is a non-issue.** Starlette's `TestClient` drains background tasks before
returning the `POST` response, so a test can `POST` then immediately `GET` a terminal
status with no sleep and no race. That determinism is a large part of why ADR 0001 chose
in-process background tasks over a queue.

---

## Operational endpoints

Not part of the brief's contract; they exist to make the service operable.

| Endpoint | Returns |
|---|---|
| `GET /health` | `200 {"status": "ok"}` once the database is reachable. Backs the container `HEALTHCHECK` ([PACKAGING.md](../../../deploy/PACKAGING.md)) |
| `GET /metrics` | the in-process counters and histograms from [OBSERVABILITY.md](../observability/OBSERVABILITY.md) |

Both are unauthenticated, which is fine here and would not be in production — `/metrics`
in particular exposes ticket volumes and handoff reasons.

---

## Structure

```
api/
  __init__.py
  app.py        FastAPI app factory, dependency wiring, lifespan (DB init)
  routes.py     the ticket endpoints
  ops.py        /health and /metrics
  schemas.py    Pydantic request/response models
  API.md        this file
```

`app.py` builds the app via a factory so tests can inject an `InMemoryTicketRepository`
and a stub `LLMClient` without touching module-level globals.

---

## Deliberately absent

- **Authentication.** Out of scope for the exercise. A real deployment needs it before
  anything else on this list.
- **Rate limiting.** Each ticket triggers model and tool work; an unauthenticated `POST`
  is a trivial amplification vector.
- **Pagination on the trace.** Traces are bounded by `MAX_ITERATIONS`, so they cannot
  grow unboundedly.
- **A cancel or retry endpoint.** Terminal states are final; a re-run would be a new
  ticket.
