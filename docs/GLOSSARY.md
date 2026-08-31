# Glossary

The vocabulary this codebase uses. These are the names in the code, not just prose — if a
term appears here, there is a type, enum member, or module with that name.

## Domain

| Term | Meaning |
|---|---|
| **Ticket** | A customer support request: `{user_id, subject, body}` plus system-assigned `id`, `status`, `reply`, `handoff_reason`, and `trace`. The unit of work. |
| **User** | A customer of the EV-charging app. Has a `name`, `language`, and `plan`. Lives in the fixtures; the service never creates one. |
| **Charging session** | One use of a charging station: `station`, `kWh`, `cost`, `status`. |
| **Invoice** | A billing record: `amount`, `status` ∈ `paid` \| `pending` \| `failed`. |
| **Status** | The ticket's lifecycle state: `processing` → (`replied` \| `handed_off`). Terminal states are final; nothing re-opens a ticket. |

## Pipeline

| Term | Meaning |
|---|---|
| **Pipeline** | The whole automated run for one ticket: classify, loop, render, verify, resolve. |
| **Orchestrator** | The code that runs the pipeline. The only component allowed to decide a terminal outcome (ADR 0005). |
| **Intent** | The classified category of a ticket: `billing_question`, `charging_session_problem`, or `unknown`. `unknown` is not a category to be handled — it is a handoff trigger. |
| **Step** | One decision returned by the LLM client: a `ToolCall`, a `Reply`, or a `Handoff`. `Reply` and `Handoff` are terminal. |
| **Iteration** | One pass of the loop: ask for a step, execute it, record it. Counted against the iteration cap. |
| **Iteration cap** | `MAX_ITERATIONS` (default 5). The hard upper bound on loop passes. Exhausting it is a handoff, never a reply (ADR 0002). |
| **Observation** | A tool call and its result, appended to the history the LLM client sees on the next iteration. The loop's memory. |
| **History** | The ordered list of observations so far. What makes the loop agentic rather than a fixed plan. |

## Tools

| Term | Meaning |
|---|---|
| **Tool** | One of exactly three data-gathering functions: `get_user`, `get_charging_sessions`, `get_invoices`. The pipeline's only route to data. |
| **Tool registry** | The lookup from a tool name to its callable, plus its argument schema. The loop dispatches through it and cannot reach anything not registered. |
| **Tool result** | What a tool returned. Recorded in the trace in **summarised** form — counts and key fields, not the full payload (`TRACEABILITY.md`). |
| **Fixture** | The local JSON data the tools read. Read-only; six users chosen so every pipeline path has a user that exercises it (`TOOLS.md`). |

## Guardrails

| Term | Meaning |
|---|---|
| **Fact** | A single value that provably came from a tool result. |
| **FactSet** | The typed collection of facts projected from recorded tool results. The **only** source a reply may draw from (ADR 0004). |
| **Grounding** | The property that every factual literal in a reply traces back to a tool result. |
| **Grounding checker** | The post-hoc verifier that re-reads a finished reply and confirms grounding, independently of which LLM wrote it. |
| **`TEMPLATE_SAFE_LITERALS`** | The small, explicit, per-template allowlist of literals that appear in a template's own static prose (e.g. "3 business days") and so are not expected in the `FactSet`. |
| **Handoff** | The terminal outcome where no reply is sent and a human takes over. Always carries a reason. |
| **Handoff reason** | A closed enum: `USER_NOT_FOUND`, `DATA_NOT_FOUND`, `UNSUPPORTED_INTENT`, `TOOL_ERROR`, `ITERATION_CAP_EXCEEDED`, `UNGROUNDED_REPLY`. |
| **Fail closed** | The governing principle: when uncertain, hand off rather than reply. A wrong reply costs more than a delayed one (ADR 0005). |

## Traceability

| Term | Meaning |
|---|---|
| **Trace** | The ordered, persisted record of everything the pipeline did for one ticket. Returned by `GET /tickets/{id}`. |
| **Trace step** | One typed entry in a trace: `intent_classified`, `llm_decision`, `tool_call`, `tool_result`, `grounding_check`, `final_decision`. |
| **Summarised result** | A tool result reduced to counts and key fields for the trace — enough to explain the reply, not a full data dump. |

## A note on two words that get confused

- **Step** is what the *model* decides. **Trace step** is what the *system* records. One
  model step produces several trace steps (the decision, the call, the result).
- **Handoff** is the outcome. **Handoff reason** is the enum member explaining it. A
  handoff without a reason is a bug, not a state.
