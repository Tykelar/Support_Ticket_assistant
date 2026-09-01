# Architecture

The end-to-end design of the support ticket auto-reply service. This is the map; each
component's own document is the detail, and the ADRs in [`docs/adr/`](docs/adr/) are the
reasoning.

> **Reading order.** This file → the component doc for whatever you care about →
> the ADR it links for *why*. Vocabulary is in [`CONTEXT.md`](CONTEXT.md).

---

## 1. What the system does

A customer support ticket arrives over HTTP. An AI pipeline decides what customer data it
needs, gathers it through three tools, drafts a reply grounded strictly in what those
tools returned, and either sends it or hands the ticket to a human. Everything it did is
recorded so a support agent can answer "why did the AI say this?" afterwards.

The governing principle is **fail closed**: a delayed reply costs far less than a wrong
one, so every uncertainty resolves to a human handoff (ADR 0005).

---

## 2. The pipeline

```
   POST /tickets {user_id, subject, body}
        |
        +--> persist ticket, status = processing
        +--> 202 Accepted {id, status: "processing"}         [ADR 0001]
             |
             '--> BackgroundTask: run_pipeline(ticket_id)
                   |
    +--------------v-------------------------------------------------------+
    | 1. CLASSIFY INTENT                                                    |
    |    llm.classify_intent(ticket) -> billing_question       [ADR 0012]   |
    |                                 | charging_session_problem            |
    |                                 | unknown -----------------------+    |
    +---------------------------------------------------------------- | ---+
    | 2. AGENTIC TOOL LOOP        while iterations < MAX_ITERATIONS    |    |
    |                                                       [ADR 0002] |    |
    |    step = llm.decide_next_step(ticket, history)                  |    |
    |      |- ToolCall --> registry.run(name, args)                    |    |
    |      |                |- ok    --> history += Observation --> loop    |
    |      |                '- error ------------------------------+  |    |
    |      |- Reply    --> leave loop with draft ---------+        |  |    |
    |      '- Handoff  ------------------------------------+-------+  |    |
    |                                                     |        |  |    |
    |    loop exhausted -----------------------------------+-------+  |    |
    +-----------------------------------------------------|--------|--|----+
    | 3. GROUND AND VERIFY                                 v        |  |    |
    |    facts = FactSet.from_observations(history)                 |  |    |
    |    template = spec_for(draft.template)              [ADR 0004]|  |    |
    |    reply = template.render(facts)                             |  |    |
    |    checked = GroundingChecker.verify(reply, facts, template)  |  |    |
    |      |- none --> [OK] status = replied, reply persisted       |  |    |
    |      '- any  -------------------------------------------------+  |    |
    +-----------------------------------------------------------------v----+
                                            [STOP] status = handed_off,
                                                   reply = None,
                                                   + HandoffReason  [ADR 0005]

    Every arrow above appends a typed step to the trace.               [see 5]
```

`GET /tickets/{id}` returns `{id, status, reply, handoff_reason, trace}` — everything a
support agent needs, from one call.

---

## 3. Components

Each is a package under `src/support_assistant/` holding its own code and documentation
(ADR 0007).

| Component | Responsibility | Doc |
|---|---|---|
| `api/` | HTTP surface. Validates input, schedules work, serves ticket state. Contains no pipeline logic. | [API.md](src/support_assistant/api/API.md) |
| `pipeline/` | The orchestrator. Runs the loop and is the **only** component that decides a terminal outcome. | [PIPELINE.md](src/support_assistant/pipeline/PIPELINE.md) |
| `llm/` | The `LLMClient` protocol, the deterministic `FakeLLM`, and the optional `OllamaLLM`. | [LLM.md](src/support_assistant/llm/LLM.md) |
| `tools/` | The three tools, the registry the loop dispatches through, and the JSON fixtures. | [TOOLS.md](src/support_assistant/tools/TOOLS.md) |
| `guardrails/` | Iteration cap, handoff rules, and the two-layer grounding enforcement. | [GUARDRAILS.md](src/support_assistant/guardrails/GUARDRAILS.md) |
| `tracing/` | The trace model, step types, and result summarisation. | [TRACEABILITY.md](src/support_assistant/tracing/TRACEABILITY.md) |
| `storage/` | `TicketRepository` protocol, SQLite implementation, in-memory test double. | [STORAGE.md](src/support_assistant/storage/STORAGE.md) |
| `observability/` | Structured logs and the counters that answer "is this working in production?". | [OBSERVABILITY.md](src/support_assistant/observability/OBSERVABILITY.md) |
| `tests/` | Test strategy and the three cases the brief names. | [TESTS.md](tests/TESTS.md) |
| `deploy/` | Dockerfile, compose, and the two commands to run it. | [PACKAGING.md](deploy/PACKAGING.md) |

Three modules sit at the package root rather than in a component, because several
components need them and putting them in any one would force the sideways imports the
dependency direction below rules out:

| Module | Holds |
|---|---|
| `domain.py` | `Ticket`, `User`, `ChargingSession`, `Invoice`, `ToolResult`, `Classification`, and the `ToolCall \| Reply \| Handoff` decision union + `Observation` — the vocabulary of [CONTEXT.md](CONTEXT.md) as types. `ToolResult` and the decision union are here rather than in `tools/` or `llm/` because `llm/` and `guardrails/` both consume them (`FactSet.from_observations`), and neither may import the other |
| `enums.py` | `Intent`, `TicketStatus`, `SessionStatus`, `InvoiceStatus`, `ReplyTemplate`, `HandoffReason`, `LiteralClass`. Separate from `domain.py` only because a ticket carries its trace and a trace step names an intent; the enums are the layer both can depend on. `HandoffReason` and `LiteralClass` are here rather than in `guardrails/`, which is what keeps `domain.py` from importing a component package ([ADR 0011](docs/adr/0011-shared-vocabulary-below-the-components.md)). The domain ones are re-exported by `domain.py` |
| `clock.py` | `Clock`, `SystemClock`, `FrozenClock` (ADR 0008) |


### Dependency direction

```
    api  -->  pipeline  -->  llm
                  |     -->  tools      --> fixtures (JSON)
                  |     -->  guardrails
                  |     -->  tracing
                  '-->  storage         --> SQLite

    everything  -->  observability
    everything  -->  domain / enums / clock
```

Dependencies point one way: inward from `api`, downward from `pipeline`. Nothing imports
`api`, and `llm` / `tools` / `guardrails` know nothing about each other — they meet only
in the orchestrator. That is what keeps each one testable in isolation.

Two edges are worth naming because they are the ones a reader would otherwise trip over.
`domain` imports `tracing.models`, because a `Ticket` carries its trace — so that one
module stays below `domain` and holds `Violation`. And `guardrails` imports
`tracing.models` for that same `Violation`. Both follow from
[ADR 0011](docs/adr/0011-shared-vocabulary-below-the-components.md);
`tests/test_layering.py` parses the imports and fails on anything else.

---

## 4. Contracts fixed across the system

These are single definitions that several components depend on. Changing one is a
cross-cutting change.

**Ticket status** — `processing` → (`replied` or `handed_off`). Terminal states are final.

**Intent** — `billing_question`, `charging_session_problem`, `unknown`. Exactly the
brief's categories; `unknown` is a handoff trigger, not a category to serve.

**`HandoffReason`** — a closed enum in `enums.py`. Every handoff carries exactly one:

| Reason | Raised when |
|---|---|
| `USER_NOT_FOUND` | `get_user` finds no such user |
| `DATA_NOT_FOUND` | the user exists but the data the intent needs is absent (ADR 0009) |
| `UNSUPPORTED_INTENT` | intent classified `unknown` |
| `TOOL_ERROR` | a tool raised, or any unanticipated exception in the run |
| `ITERATION_CAP_EXCEEDED` | the loop hit `MAX_ITERATIONS` without terminating |
| `UNGROUNDED_REPLY` | the grounding checker found an unsourced literal |

**`MAX_ITERATIONS`** — 5, environment-configurable.

**Time** — never read ambiently. Every timestamp comes from an injected `Clock`; nothing
calls `datetime.now()` directly (ADR 0008).

**Reply on handoff** — always `None`. Never an empty string, never a holding message.

---

## 5. Traceability

Every ticket carries an ordered list of typed trace steps, persisted alongside it. Every
step carries `seq` and `ts` — the timestamp comes from an injected clock so traces are
auditable in production and reproducible in tests (ADR 0008).

| Step | Records |
|---|---|
| `intent_classified` | the intent, and the signal that produced it |
| `llm_decision` | which step the model chose this iteration |
| `tool_call` | tool name and arguments |
| `tool_result` | a **summarised** result — counts and key fields, not the raw payload |
| `grounding_check` | pass, or the specific literals that failed |
| `final_decision` | `replied` or `handed_off`, plus the reason |

Summarisation is deliberate: the trace has to explain the reply, not duplicate the
database. Details in [TRACEABILITY.md](src/support_assistant/tracing/TRACEABILITY.md).

---

## 6. Where each requirement lives

| Brief requirement | Satisfied by | Reasoning |
|---|---|---|
| 1. `POST` / `GET` endpoints | `api/` | ADR 0001 (sync vs async) |
| 2. Exactly three tools, fixture-backed | `tools/` | — |
| 2. Replies contain only tool-sourced facts | `guardrails/` — two layers | **ADR 0004** |
| 3. LLM interface + deterministic fake | `llm/` | ADR 0006 |
| 4. Hard iteration cap | `guardrails/`, enforced in `pipeline/` | ADR 0002 |
| 4. Handoff on missing data / bad intent / failure | `pipeline/`, one typed enum | ADR 0005 |
| 4. Handoff records why | `HandoffReason` + `final_decision` trace step | ADR 0005 |
| 5. Persisted trace, readable from `GET` alone | `tracing/` + `storage/` | ADR 0003 |
| 6. Tests: happy path, missing data, iteration cap | `tests/` | — |
| 7. Two-command run, real commit history | `deploy/` | — |

---

## 7. Known limitations

Stated here rather than discovered in review. Each is a decision, not a discovery;
[docs/ROADMAP.md](docs/ROADMAP.md) says how each would be addressed.

- **In-flight work is lost on restart** — a ticket can be stranded in `processing`
  forever (ADR 0001). [Roadmap](docs/ROADMAP.md#durable-work-and-a-reaper).
- **Grounding verifies sourcing, not truth** — a sentence built from real numbers can
  still be false (ADR 0004). [Roadmap](docs/ROADMAP.md#semantic-grounding).
- **Keyword classification is brittle** — deliberately, as a deterministic stand-in
  rather than a claim about classification quality (ADR 0006).
- **Replies are English only**, though profiles carry a `language` (ADR 0006).
  [Roadmap](docs/ROADMAP.md#replies-in-the-users-language).
- **No authentication or rate limiting.** Anyone holding a ticket id can read that
  customer's data. A real vulnerability, deliberately scoped out.
  [Roadmap](docs/ROADMAP.md#authentication-and-rate-limiting).
