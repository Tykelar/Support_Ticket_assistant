# ADR 0001 — Asynchronous in-process ticket processing

**Status:** Accepted · 2026-08-31

## Context

`POST /tickets` has to return a ticket id. The brief leaves synchronous versus
asynchronous processing to us and asks for a justification. It also defines three
statuses — `processing | replied | handed_off`.

That status list is the tell. `processing` is only ever observable if the API can return
before the pipeline finishes. Building this synchronously would leave one of the three
required statuses permanently unreachable — a requirement satisfied on paper only.

The realistic production shape matters too. The pipeline makes several sequential model
and tool calls. Under a real LLM that is seconds of latency, and holding an HTTP request
open for it couples the client's timeout budget to the model's tail latency.

## Decision

`POST /tickets` persists the ticket with status `processing`, schedules the pipeline as a
FastAPI `BackgroundTask`, and returns `202 Accepted` with the id immediately. The client
polls `GET /tickets/{id}` for the terminal state.

The work runs **in-process**. No queue, no broker, no second container.

## Consequences

- All three statuses are real states the system actually passes through.
- Client latency is decoupled from model latency; swapping in a slow real LLM
  (ADR 0006) changes nothing about the API's response time.
- Tests stay deterministic. Starlette's `TestClient` drains background tasks before it
  returns the response, so a test can `POST` and then immediately `GET` a terminal
  status without polling, sleeping, or racing.
- **In-flight work is lost if the process dies.** A ticket can be stranded in
  `processing` forever. Accepted for this exercise; the production fix is a durable
  queue plus a reaper that fails stranded tickets closed to `handed_off`. Noted in the
  README's "what I'd change with more time".
- Concurrency is bounded by the event loop, not by a worker pool. Fine at this scale.

## Alternatives considered

**Fully synchronous.** Simplest to build and reason about, and needs no polling. Rejected
because `processing` never occurs and because it puts model latency on the request path.

**Queue plus separate worker (Redis/RQ, Celery).** The genuinely production-correct
answer, and it fixes the durability gap above. Rejected on time-box grounds: it adds a
broker and a second process to `docker compose`, and spends a large share of a 6–8h
budget on plumbing the brief did not ask for. The `TicketRepository` boundary
(ADR 0003) is where a queue would later attach.
