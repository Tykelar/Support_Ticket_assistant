# Traceability

> A support agent must be able to answer "why did the AI say this?" from
> `GET /tickets/{id}` alone.

That sentence is the whole specification for this package. Not a log file, not a
dashboard, not a debugger — one API call.

---

## The trace

An ordered list of typed steps, persisted with the ticket and returned by `GET`. Every
step has `seq` (1-based, monotonic), `ts`, and `type`; the rest of the fields depend on
the type.

`ts` comes from an injected `Clock` rather than `datetime.now()`
([ADR 0008](../../../docs/adr/0008-injected-clock-with-advancing-test-double.md)). In
production that is real UTC time; in tests it is a frozen clock advancing a fixed 10ms
tick, so traces stay byte-for-byte reproducible while durations remain real enough to
assert on. Timestamps must increase with `seq`, and a contract test enforces it — which is
the check a constant clock would silently make impossible.

| Step | Emitted | Carries |
|---|---|---|
| `intent_classified` | once, before the loop | `intent`, `matched_keywords` |
| `llm_decision` | once per iteration | `iteration`, `decision` (`tool_call` / `reply` / `handoff`), `tool` if any |
| `tool_call` | per tool invocation | `tool`, `args` |
| `tool_result` | per tool invocation | `tool`, `ok`, `summary`, or `error` |
| `grounding_check` | once, if a reply was drafted | `passed`, `literals_checked`, `violations` |
| `final_decision` | exactly once, always last | `outcome`, `reason`, `detail` |

**`final_decision` is always present in a terminal state.** It is written by
`finish_replied` and `finish_handoff`, the only two functions that can write one — so a
terminal ticket without a final decision step is structurally impossible, not merely
unlikely ([PIPELINE.md](../pipeline/PIPELINE.md)).

`llm_decision` is recorded separately from `tool_call` on purpose. One says what the model
*chose*; the other says what the system *did*. Collapsing them would hide the case where
those diverge — a rejected tool name, a validation failure before execution — which is
precisely the case worth seeing.

---

## Why results are summarised

`tool_result` records a **summary**, not the payload:

```json
{ "seq": 7, "ts": "2026-08-31T10:14:02.113Z",
  "type": "tool_result", "tool": "get_invoices", "ok": true,
  "summary": { "count": 3, "statuses": { "paid": 2, "failed": 1 },
               "referenced": ["inv_204", "inv_203", "inv_202"] } }
```

Three reasons, in order of importance:

1. **The brief asks for it** — "each tool call with its arguments and (summarized)
   result".
2. **Readability.** The trace exists to be read by a human under time pressure. A full
   invoice dump per call buries the reasoning in data the agent can look up elsewhere.
3. **Data minimisation.** The trace is retained for audit and is served over the API.
   Copying every field of every record into it multiplies the exposure of customer data
   for no gain.

### The summarisation rule

Enough to explain the reply, never more:

- **counts** — how many rows came back;
- **distribution over enumerated fields** — the statuses and their frequencies;
- **the identifiers the result returned** — so a reader can go from a sentence in the
  reply to the exact source record.

What is deliberately excluded: full field-by-field records.

`summarise(result)` in [summarise.py](summarise.py) produces this, one rule per tool. The
`referenced` list holds every identifier the call returned. Narrowing it to just the ones
the rendered reply cites is a deferred refinement — it only means anything once the
pipeline renders a reply, and it needs either a second pass or building the `tool_result`
step at persist time; the orchestrator will own that call when it exists. Until then the
full list is a safe superset. The status distribution is emitted in enum-declaration
order rather than row order, so a persisted summary is byte-stable run to run.

**The tension worth naming:** summarisation is lossy, and a lossy audit record can fail to
explain an outcome. The rule above is chosen so the *decision-relevant* facts always
survive — the `referenced` list is what guarantees a reader can trace any statement in the
reply back to a specific record. Per-record field detail is the only thing dropped.

---

## Failure detail

Failure steps carry more than success steps, because a failure is what someone will
actually be reading:

```json
{ "seq": 4, "type": "tool_result", "tool": "get_invoices", "ok": false,
  "error": { "type": "NoDataAvailable", "message": "user u_004 has no invoices" } }

{ "seq": 5, "type": "final_decision", "outcome": "handed_off",
  "reason": "DATA_NOT_FOUND",
  "detail": "get_invoices found no records for u_004" }
```

Grounding violations record the specific offending literals — the evidence for why a
reply was withheld:

```json
{ "seq": 9, "type": "grounding_check", "passed": false, "literals_checked": 4,
  "violations": [ { "literal": "99.00", "class": "number",
                    "reason": "not present in FactSet or TEMPLATE_SAFE_LITERALS" } ] }
```

Stack traces go to the structured log, not the trace. The trace answers *why the AI said
this*; a traceback answers *why the code broke*. Different audiences, different places.

**`ToolError` is not `ToolExecutionError`.** The first is the model above — a failure as
recorded in a `tool_result` step. The second is one of the three exceptions a tool raises
([TOOLS.md](../tools/TOOLS.md)). The similarity is unfortunate but the names are right:
`ToolError` records *any* exception that reached the call site, `NoDataAvailable` and
`UserNotFound` included, so naming it after one of the three would be actively
misleading. Which component writes the step is settled in
[ADR 0010](../../../docs/adr/0010-the-orchestrator-records-tool-steps.md).

---

## Recording

The recorder is constructed with a `Clock` and stamps every step it appends:

```python
class TraceRecorder:
    def __init__(self, clock: Clock) -> None
    def intent_classified(self, intent, matched_keywords) -> None
    def llm_decision(self, iteration, decision, tool=None) -> None
    def tool_call(self, tool, args) -> None
    def tool_result(self, tool, summary=None, error=None) -> None
    def grounding_check(self, literals_checked, violations) -> None
    def final_decision(self, outcome, reason=None, detail=None) -> None
```

`llm_decision` takes the decision (`tool_call` / `reply` / `handoff`) and, for a tool
call, the tool name — not the LLM's step object. The `ToolCall` / `Reply` / `Handoff`
types arrive with `llm/` in a later phase and nothing in `tracing/` may depend on `llm/`
([ARCHITECTURE.md](../../../ARCHITECTURE.md) §3); the orchestrator unpacks its own `step`
at the call site. Three coherence rules are enforced by the models, not left to the
caller: `tool_result`'s `ok` is derived from whether an `error` is present,
`grounding_check`'s `passed` from whether `violations` is empty, and `llm_decision` names
a `tool` exactly when the decision is `tool_call`.

Steps accumulate in memory during the run and are persisted with the terminal state in one
transaction, so a ticket is never observed with a terminal status but a truncated trace
([STORAGE.md](../storage/STORAGE.md)).

**The trade-off:** a process that dies mid-run loses the partial trace along with the
ticket, which stays in `processing` (ADR 0001's known limitation). Incremental persistence
would preserve the partial trace at the cost of a write per step and the atomicity above.
Given that a stranded ticket needs a reaper either way
([roadmap](../../../docs/ROADMAP.md#durable-work-and-a-reaper)), the atomic write is the better trade here — and the structured log retains a per-step record for exactly this case
([OBSERVABILITY.md](../observability/OBSERVABILITY.md)).

`grounding_check` takes the count as well as the violations because `passed` follows from
whether the list is empty but `literals_checked` does not, and it is what distinguishes a
check that passed from one that had nothing to look at: `passed: true, literals_checked: 0`
is a reply the checker never really inspected, and a bare `passed: true` would hide it.
The orchestrator supplies the count as `len(GroundingChecker.extract(reply))` — the same
extraction `verify` runs — so it is exactly the number of literals that were checked.

The recorder is injected, not global, so tests assert on the recorded steps directly.

---

## Reading a trace

The intended workflow when an agent asks "why did the AI say this?":

1. `final_decision` — what happened and why, in one step.
2. `intent_classified` — was the ticket even understood correctly? `matched_keywords`
   shows the evidence.
3. `tool_call` / `tool_result` pairs — what data the reply was built from.
4. `grounding_check` — for a withheld reply, exactly which literal was unsourced.

`GET /tickets/{id}` returns all of it. No log access, no database query, no second call.

---

## Structure

```
tracing/
  __init__.py
  models.py        TraceStep types, discriminated by `type`
  recorder.py      TraceRecorder
  summarise.py     tool-result summarisation rules, per tool
  TRACEABILITY.md  this file
```
