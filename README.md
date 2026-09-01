# Support Ticket Auto-Reply Service

Automatically replies to customer support tickets for an EV-charging app. A ticket arrives
over HTTP; an AI pipeline decides what data it needs, gathers it through three tools,
drafts a reply grounded strictly in what those tools returned, and either sends it or
hands the ticket to a human — recording everything it did along the way.

Built for the [take-home challenge](docs/brief.md).

> **Status: the service runs.** `POST` a ticket and `GET` it back with its reply and
> full trace — under `uvicorn`, from the command below. Still to land, per
> [Build order](#build-order): `/metrics` and structured logs (phase 9), and the Docker
> packaging (phase 10), so `docker compose up` does not work yet.

---

## Run it

```bash
pip install -e ".[dev]"
uvicorn support_assistant.api.app:app --reload
```

Service on `http://localhost:8000`, interactive docs at `/docs`. No configuration needed:
the database path defaults, and the LLM is the deterministic `FakeLLM` unless you opt in
to a real one.

Docker is phase 10 — once it lands, `docker compose up` is the one-liner.

## Test it

```bash
pytest
```

No configuration is needed for either — every setting has a working default and the
optional real LLM is opt-in. Details in [deploy/PACKAGING.md](deploy/PACKAGING.md).

## Try it

```bash
curl -s -X POST localhost:8000/tickets -H 'content-type: application/json' \
  -d '{"user_id":"u_002","subject":"My payment failed",
       "body":"I got an email saying my invoice could not be charged. What happened?"}'
# -> {"id":"t_...","status":"processing"}

curl -s localhost:8000/tickets/t_... | jq
# -> status, reply, and the full trace of how the reply was produced
```

`u_005` (a user who does not exist) demonstrates the handoff path. The full fixture map —
which user exercises which path — is in [TOOLS.md](src/support_assistant/tools/TOOLS.md).

---

## Documentation

Start with **[ARCHITECTURE.md](ARCHITECTURE.md)** — the pipeline end to end, and a table
mapping every requirement in the brief to where it is satisfied.

Each component's documentation lives with its code:

| Part | Doc |
|---|---|
| API | [src/support_assistant/api/API.md](src/support_assistant/api/API.md) |
| Pipeline (the loop) | [src/support_assistant/pipeline/PIPELINE.md](src/support_assistant/pipeline/PIPELINE.md) |
| The LLM | [src/support_assistant/llm/LLM.md](src/support_assistant/llm/LLM.md) |
| Tools | [src/support_assistant/tools/TOOLS.md](src/support_assistant/tools/TOOLS.md) |
| Guardrails | [src/support_assistant/guardrails/GUARDRAILS.md](src/support_assistant/guardrails/GUARDRAILS.md) |
| Traceability | [src/support_assistant/tracing/TRACEABILITY.md](src/support_assistant/tracing/TRACEABILITY.md) |
| Storage | [src/support_assistant/storage/STORAGE.md](src/support_assistant/storage/STORAGE.md) |
| Observability | [src/support_assistant/observability/OBSERVABILITY.md](src/support_assistant/observability/OBSERVABILITY.md) |
| Tests | [tests/TESTS.md](tests/TESTS.md) |
| Packaging | [deploy/PACKAGING.md](deploy/PACKAGING.md) |
| Deferred work, and how | [docs/ROADMAP.md](docs/ROADMAP.md) |

Component docs say **what** and **how**; the nine ADRs in [docs/adr/](docs/adr/) say
**why**, once, and are linked from wherever the decision shows up. Vocabulary is in
[CONTEXT.md](CONTEXT.md), and everything deliberately left out — with how it would be
built — is in [docs/ROADMAP.md](docs/ROADMAP.md).

---

## Design decisions

### Sync vs async — async, in-process

`POST /tickets` returns `202` immediately and the pipeline runs as a FastAPI
`BackgroundTask`.

The brief defines three statuses, and `processing` is only ever observable if the API can
return before the pipeline finishes. Building this synchronously would leave one of the
three required statuses permanently unreachable. It also puts model latency on the request
path, which is the wrong coupling the moment a real LLM is wired in.

A queue and a worker would be the production-correct answer and would fix the durability
gap below, but it adds a broker and a second process for no gain the brief is asking
about. Full reasoning: [ADR 0001](docs/adr/0001-asynchronous-in-process-processing.md).

### The pipeline is a real agentic loop, not a plan

The LLM client is asked `decide_next_step(ticket, history)` on each iteration and sees
prior tool results before choosing the next action.

This is the decision the rest of the design turns on. The alternative — classify, return a
list of tools, run them once — is simpler, but the number of iterations is then known
before the loop starts, and the iteration cap the brief requires becomes decorative: it
can never fire, and no honest test can make it fire. With a real loop the cap guards
something that can actually run away, and there is a test that proves it.
[ADR 0002](docs/adr/0002-true-agentic-tool-calling-loop.md).

### Grounding is enforced twice

> Inventing data the tools didn't return is the one unforgivable bug in this domain.

**Layer 1, structural:** replies render only from a typed `FactSet` projected from
recorded tool results. There is no code path to a value no tool returned.

**Layer 2, post-hoc:** a `GroundingChecker` re-reads the finished reply and verifies every
numeric literal, identifier, and status word against the `FactSet`. A violation fails
closed to a handoff.

Layer 1 alone is airtight — but it is a property of the *fake* LLM, and it disappears
silently the moment a real model writes prose. Layer 2 is the one that survives the swap,
because it inspects output rather than trusting the thing that produced it.
[ADR 0004](docs/adr/0004-two-layer-grounding-enforcement.md).

### Everything fails closed

One closed `HandoffReason` enum, six members, each produced at exactly one place in the
code. Components raise typed failures; only the orchestrator converts one into a terminal
outcome. A catch-all turns any unanticipated exception into `TOOL_ERROR` — an unhandled
crash must never leave a ticket stuck in `processing`, and must never produce a reply.

This deliberately biases toward handing off tickets a human would have answered. That is
the right trade when the alternative is being confidently wrong about someone's money.
[ADR 0005](docs/adr/0005-fail-closed-to-human-handoff.md).

### Storage is split

Tickets and traces go to SQLite behind a `TicketRepository` protocol, so the audit record
survives a restart. Tool fixtures stay as JSON files, so a reviewer can open one, change
an amount, and re-run. [ADR 0003](docs/adr/0003-sqlite-behind-a-repository-protocol.md).

### It is unauthenticated, and that is a vulnerability

Worth stating plainly rather than burying. There is no authentication anywhere: anyone who
can reach the service can post tickets, and anyone holding a ticket id can read that
ticket's full trace — customer name, invoice ids, amounts, payment statuses, charging
stations. `GET /metrics` exposes ticket volumes and handoff reasons to anyone.

The only mitigation is that ticket ids are 128 bits of randomness, so they can't be
enumerated. That makes an id a bearer token in all but name, and a poor one: it never
expires and leaks through logs and shared URLs.

This is a scoping decision, not an oversight. Auth is a large surface that exercises
nothing the brief is evaluating, and a half-built version would be worse than none. It is
the first thing required before this touches real customers —
[how I'd do it](docs/ROADMAP.md#authentication-and-rate-limiting).

### Docs live with the code

Each component is a package holding its own documentation. A document three directories
away from what it describes goes stale without anything visibly breaking.
[ADR 0007](docs/adr/0007-component-packages-with-colocated-docs.md).

---

## What I would measure in production

The failure this system actually has is **being confidently wrong**, and a wrong reply
looks exactly like a right one from the outside: `200 OK`, `replied`, fast, no errors. So
uptime and latency are close to useless as quality signals here.

**Handoff rate broken down by reason** is the single most informative number. The rate
alone says little; the shape says a lot. `UNSUPPORTED_INTENT` climbing is a product signal
about which fourth tool to build. `TOOL_ERROR` climbing is a defect. `DATA_NOT_FOUND`
climbing is usually upstream. And a rate falling toward zero is not a win — it is a reason
to check whether the guardrails have been weakened.

**Grounding violations should be exactly zero.** Any non-zero value means the renderer
produced a literal no tool returned. The guardrail caught it before a customer saw it, and
something is still badly wrong.

**Iterations per ticket** is a leading indicator: the distribution drifting toward the cap
predicts cap handoffs before they happen. **The age of the oldest `processing` ticket**
catches work lost to a process restart, which nothing else would notice.

Honestly, none of those measure whether the replies are any **good**. That needs feedback
the API does not yet capture: reopen rate after an auto-reply (the closest thing to ground
truth), agent override rate, and sampled human review of replied tickets — the only way to
catch confidently-wrong replies that customers never bother to challenge. Wiring that
feedback loop is the first thing worth building after the core system.

Detail: [OBSERVABILITY.md](src/support_assistant/observability/OBSERVABILITY.md).

---

## What I would change with more time

The short version; **[docs/ROADMAP.md](docs/ROADMAP.md) is the long one** — fourteen
entries, each with why it was deferred and concretely how it would be built.

**First, capture the feedback loop.** Every metric this system emits measures whether it
is *behaving* — handoff rates, iteration counts, grounding violations. None measures
whether the replies are any *good*, and a confidently wrong reply nobody challenges is
invisible to all of them. Reopen rate, agent override rate, and sampled human review are
the ground truth everything else would be tuned against, and none of them exist today.
[How](docs/ROADMAP.md#the-feedback-loop).

**Then make the work durable.** In-process background tasks die with the process, leaving
a ticket in `processing` forever with nobody paged. A reaper is an hour's work and turns
silent loss into a handoff; a real queue behind the `TicketRepository` seam is the proper
fix. [How](docs/ROADMAP.md#durable-work-and-a-reaper).

**Then close the two grounding gaps**, both of which only bite once a real model writes
the prose: the checker verifies every literal is *sourced*, not that the sentence around
it is *true* ([how](docs/ROADMAP.md#semantic-grounding)), and its extraction covers
numbers, ids, and closed status vocabularies but not open-vocabulary entities like an
invented station name ([how](docs/ROADMAP.md#entity-coverage-in-grounding-layer-2)).

**And before any real customer touches it, authentication** — see the security note
above. [How](docs/ROADMAP.md#authentication-and-rate-limiting).

Also deferred, each with a plan in the roadmap: replies in the user's language, retry on
transient tool failure, idempotent submission, push instead of polling, trace retention,
bounded concurrency, Postgres, cross-ticket queries, and hardening the real LLM client.

---

## Build order

Documentation and ADRs first, then tests, then implementation, per phase. The history is
meant to be read: one commit per meaningful step, messages that say why.

| Phase | | Phase | |
|---|---|---|---|
| 0 | Scaffolding | 6 | Guardrails |
| 1 | Docs + ADRs | 7 | Pipeline |
| 2 | Domain models + fixtures | 8 | API |
| 3 | Tools | 9 | Observability |
| 4 | Storage + tracing | 10 | Packaging + README |
| 5 | LLM | 11 | Ollama client (bonus) |

---

## AI assistants

Per the brief's disclosure requirement.

**Claude (Claude Code)** was used throughout, in two distinct modes:

- **As an interviewer, before any code.** The design decisions above were reached by
  being pushed on them — sync vs async, whether the iteration cap guards anything real,
  whether structural grounding survives a real model. The twelve ADRs are the record of
  those arguments, including the alternatives that were rejected and why.
- **As a drafting tool** for documentation and implementation, reviewed and edited by
  hand.

Every design decision here is one I made and can defend, and the reasoning is written down
in the ADRs rather than living only in the code. The known limitations sections exist
because I would rather state a gap than have it found for me.
