# ADR 0015 — `OllamaLLM` uses JSON mode, not the tool-calling API

**Status:** Accepted · 2026-09-02

## Context

Phase 11 wires the optional real client
([ADR 0006](0006-fake-first-llm-behind-a-client-protocol.md)): `OllamaLLM`, the same two
`LLMClient` methods against a local Ollama server over HTTP. `classify_intent` returns a
`Classification`; `decide_next_step` returns a `ToolCall | Reply | Handoff` — the
discriminated `Step` union in [`domain.py`](../../src/support_assistant/domain.py),
discriminated on `decision`.

Ollama's `POST /api/chat` offers two ways to get structured output back:

- **The tool-calling API.** Pass a `tools` array of JSON-Schema function definitions; the
  model replies with `message.tool_calls: [{function: {name, arguments}}]`, or with plain
  `message.content` when it chooses not to call one.
- **JSON mode.** Pass `"format": "json"` (or a JSON schema); the model is constrained to
  emit a single JSON object as `message.content`, which the caller parses.

Two constraints from this codebase bear on the choice:

- `llm/` may not import `tools/` (ARCHITECTURE.md §3; `tests/test_layering.py` enforces it,
  with `llm/fake.py`'s one `registry.registered()` read the only sanctioned exception). So
  `OllamaLLM` cannot build a `tools` array from the registry's schemas — the tool
  catalogue it shows the model is hand-written prose either way.
- The return type is already a Pydantic discriminated union. `Reply` and `Handoff` are
  first-class members of it — "the model decided to reply" and "the model decided to give
  up" are decisions, not tool invocations.

## Decision

**`OllamaLLM` calls `/api/chat` in JSON mode** — `{"model", "messages", "format": "json",
"stream": false}` — and validates `message.content` straight onto the domain types:
`TypeAdapter(Step).validate_python(...)` for `decide_next_step`, `Intent(...)` inside a
`Classification` for `classify_intent`.

The system prompt carries the tool catalogue (`get_user`, `get_invoices`,
`get_charging_sessions`, each taking `user_id`), the five `ReplyTemplate` names, the
`HandoffReason` names, and the exact JSON shape for each `decision` variant. The model
fills in the union; the orchestrator still projects the `FactSet`, renders the template,
and runs the post-hoc grounding check (grounding layer 2 —
[ADR 0004](0004-two-layer-grounding-enforcement.md)).

**Failure is closed and uniform.** Any failure to obtain a well-formed answer — an `httpx`
transport or timeout error, a non-2xx response, `message.content` that is not JSON, or a
JSON object that does not validate against `Step` / `Intent` — raises
`OllamaProtocolError`. The orchestrator's catch-all
([`pipeline/orchestrator.py`](../../src/support_assistant/pipeline/orchestrator.py), ADR
0005) turns that into a `TOOL_ERROR` handoff. A well-formed answer naming an unregistered
tool fails the same way, one layer down, through `registry.run`. A well-formed
`{"intent": "unknown"}` is **not** an error — the model classified the ticket, and the
outcome is an `UNSUPPORTED_INTENT` handoff.

## Consequences

- **The swap stays a swap, not a redesign.** `decide_next_step`'s body is one HTTP call
  and one `TypeAdapter` validation; the union it returns is the one the orchestrator
  already consumes from `FakeLLM`.
- **Model-agnostic.** JSON mode works with any instruction-following model Ollama can run.
  The tool-calling API is supported by a subset of models and its absence is a silent
  quality drop rather than a clean error.
- **One failure path.** Transport, HTTP, parse, and schema failures all land on
  `OllamaProtocolError` → `TOOL_ERROR`, so the pipeline's behaviour under a broken or
  absent model server is exactly its behaviour under any other tool failure — bounded and
  handed off, never stuck in `processing`.
- **The tool catalogue is hand-maintained.** Because `llm/` cannot read the registry, the
  prose list of tools in `ollama.py` is a second place a fourth tool must be added. The
  registry stays the enforcement point, so the cost of it drifting is a `TOOL_ERROR`
  handoff, not a wrong reply — but it is a documented gap
  ([LLM.md](../../src/support_assistant/llm/LLM.md),
  [ROADMAP](../ROADMAP.md#hardening-the-real-llm-client)).
- **Prompt quality is unaddressed, deliberately.** The brief does not grade it, and the
  hardening that would (a golden-file eval set, schema-exhaustive validation, a circuit
  breaker, token accounting) is roadmap, not phase 11.

## Alternatives considered

**The tool-calling API.** The conventional shape for an agentic loop, and a native fit for
`ToolCall`. Rejected here because: `Reply` and `Handoff` are not tools, so the model's
"I'm done" and "I give up" arrive as free-text `message.content` that has to be parsed
anyway — reintroducing exactly the ad-hoc parsing JSON mode centralises; the `tools` array
would still be hand-written, since `llm/` cannot import the registry, so the safety
argument for it is weaker than it looks; and it narrows the set of usable models for no
gain the union does not already give us.

**A JSON schema passed as `format` (structured outputs), not bare `"json"`.** Stronger
server-side constraint — the model is decoded against the schema. Attractive, and a
natural first hardening step, but it needs `Step`'s JSON schema threaded through to the
request and per-variant schemas for the discriminator, which is more than "merely wired".
Left to the ROADMAP's response-schema-validation item; the `TypeAdapter` validation on the
way back is the phase-11 floor.

**Raise a distinct exception type per failure (transport vs parse vs schema).** Rejected:
the orchestrator maps all of them to `TOOL_ERROR` regardless, so the distinction would
exist only to be collapsed. `OllamaProtocolError` with a message that says which failure
it was is enough for a human reading the trace detail.
