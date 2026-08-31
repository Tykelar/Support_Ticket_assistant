# Tools

The pipeline's only route to data. Exactly three, as the brief specifies, backed by local
JSON fixtures.

**Responsibility:** read fixture data and return it, or fail in a typed way. Tools do not
decide anything — they never trigger a handoff themselves, they raise, and the
orchestrator converts that into an outcome ([ADR 0005](../../../docs/adr/0005-fail-closed-to-human-handoff.md)).

---

## The three tools

```python
def get_user(user_id: str) -> User
def get_charging_sessions(user_id: str) -> list[ChargingSession]
def get_invoices(user_id: str) -> list[Invoice]
```

| Tool | Returns | Fields |
|---|---|---|
| `get_user` | one `User` | `user_id`, `name`, `language`, `plan` |
| `get_charging_sessions` | recent sessions, newest first | `session_id`, `station`, `kwh`, `cost`, `status`, `started_at` |
| `get_invoices` | recent invoices, newest first | `invoice_id`, `amount`, `currency`, `status`, `issued_at` |

`status` on a session is `completed`, `interrupted`, or `failed`.
`status` on an invoice is `paid`, `pending`, or `failed`.

These enumerations matter beyond validation: because status words are enumerated values
rather than free text, they enter the `FactSet` as facts and the structural grounding
layer covers them ([ADR 0004](../../../docs/adr/0004-two-layer-grounding-enforcement.md)).

---

## Error semantics

Typed exceptions, one per failure mode. The distinction between the first two is what
lets the orchestrator pick the right `HandoffReason`.

| Exception | Raised when | Becomes |
|---|---|---|
| `UserNotFound` | no fixture record for that `user_id` | `USER_NOT_FOUND` |
| `NoDataAvailable` | the user exists, but has no rows of the requested kind | `DATA_NOT_FOUND` |
| `ToolExecutionError` | the fixture is malformed, unreadable, or fails validation | `TOOL_ERROR` |

An empty list is **not** a valid success. `get_charging_sessions` and `get_invoices`
raise `NoDataAvailable` rather than returning `[]`.

The reasoning is worth stating, because the opposite looks obvious: "you have no invoices"
*is* a truthful, grounded reply, and handing it off pages a human to write one sentence.
The problem is that zero rows is ambiguous — genuinely none, not yet synced, or a broken
join upstream — and the tool cannot tell which. The last two would produce a confident
wrong statement about a customer's billing. Full argument, and the condition under which
this should flip, in [ADR 0009](../../../docs/adr/0009-absent-data-is-a-handoff.md).

### What a failure message may say

A tool's message reaches the trace, which is served over the API and retained for audit.
So it carries a **locator, not the payload**:

```
invoice inv_601 for u_006 failed validation: amount
```

The record id gets a reader to the row; the offending value is one lookup away for anyone
entitled to it. The full `ValidationError`, value included, goes to the structured log,
where the audience is a developer rather than a support agent — the same split that sends
stack traces to the log and not the trace
([TRACEABILITY.md](../tracing/TRACEABILITY.md)).

**This applies only to the two collection-returning tools.** `get_user` returns a single
record and cannot be empty: a user exists, or raises `UserNotFound`. Both collection tools
also raise `UserNotFound` for an unknown user, so the reason is correct regardless of
which tool the loop reaches first — the missing-user and missing-data cases stay distinct,
as the brief distinguishes them.

---

## The registry

The loop never imports a tool directly. It dispatches through a registry:

```python
registry.run(name: str, args: dict) -> ToolResult
```

### `ToolResult`

What a tool returned for one call, as the rest of the system sees it:

```python
class ToolResult:
    tool: str                                        # which tool produced this
    records: list[User | ChargingSession | Invoice]  # always a list; get_user yields one
```

`records` is always a list — `get_user` yields one element. The uniformity is the point:
result summarisation ([TRACEABILITY.md](../tracing/TRACEABILITY.md)) and `FactSet`
projection ([GUARDRAILS.md](../guardrails/GUARDRAILS.md)) both count and walk records, and
one shape means one code path for each. Per-tool rules still dispatch on `tool`.

**The tools themselves return domain types** — `get_user(user_id) -> User`, as above. The
*registry* wraps the return value into a `ToolResult`. That split is why the two
signatures differ, and it keeps a tool function readable on its own.

**`ToolResult` is defined in `domain.py`, not here.** Three components consume it —
`Observation` in `llm/`, `FactSet` in `guardrails/`, and summarisation in `tracing/` — and
[ARCHITECTURE.md](../../../ARCHITECTURE.md) forbids `llm` and `guardrails` from importing
`tools`. It is also already domain vocabulary: **Tool result** is defined in
[CONTEXT.md](../../../CONTEXT.md).

Two reasons this indirection earns its place:

1. **Containment.** A model — fake or real — can only reach registered tools. An
   unrecognised name is a `ToolExecutionError`, not an `AttributeError` or, worse, a call
   to something that was never meant to be a tool.
2. **Argument validation.** Each entry declares a Pydantic schema; bad arguments fail
   before the tool body runs.

**Tracing is not one of them.** An earlier draft of this document argued the registry was
the natural chokepoint for recording `tool_call` and `tool_result` steps. It is not: the
orchestrator records both, and the registry never sees a `TraceRecorder`. Keeping tracing
out of here is what lets `tools/` import nothing from `tracing/` and keeps a tool testable
without assembling a trace — reasoning in
[ADR 0010](../../../docs/adr/0010-the-orchestrator-records-tool-steps.md).

Adding a fourth tool is: write the function, register it with a schema, add its keyword
rule to `FakeLLM`, add a fixture, add a test — and, if it returns a record type the other
three don't, add that type to the `ToolResult.records` union in `domain.py`. That is the
shape of the change the live review session is most likely to ask for.

---

## Fixtures

`src/support_assistant/fixtures/` — `users.json`, `sessions.json`, `invoices.json`.

Six users, chosen so that **every pipeline path has a user that exercises it**. The
fixtures are test infrastructure as much as sample data.

| User | Shape | Exercises |
|---|---|---|
| `u_001` Ana | all invoices paid, sessions completed | billing happy path, nothing wrong |
| `u_002` Ben | one **failed** invoice among paid ones | billing happy path with a real problem to explain |
| `u_003` Chloe | one **interrupted** session | charging-session happy path |
| `u_004` Dmitri | exists; **zero** sessions, **zero** invoices | `DATA_NOT_FOUND` handoff |
| `u_005` | **absent from the fixtures entirely** | `USER_NOT_FOUND` handoff |
| `u_006` Eva | invoice record with a **malformed amount** | `TOOL_ERROR` handoff |

`u_005` is not a record — it is the deliberate absence of one, referenced only by tests.

### Loading is lazy and per-user, and that is not incidental

`invoices.json` holds every user's invoices, and `u_006` carries a deliberately malformed
amount. If the loader validated the whole file eagerly, that one bad record would break
`get_invoices` for **every** user — the fixture built to exercise one failure path would
take out the happy paths as collateral.

So: **filter to the requested `user_id` first, validate second.** A tool validates the
rows it fetched, never the whole table — which is also how a real database adapter
behaves, so the constraint costs nothing in realism.

A regression test pins this: `u_006` must raise `ToolExecutionError` while `u_002` still
returns its invoices from the same file.

### How the loaders read

**Sorted in the loader, not trusted from the file.** Both collection tools return rows
newest first, and the loader sorts to make that true rather than relying on the fixtures
being written in order. They are, and a test asserts it — but a reviewer editing a file to
try something should not be able to silently break a documented contract.

**Read and parsed on every call, never cached.**
[ADR 0003](../../../docs/adr/0003-sqlite-behind-a-repository-protocol.md) kept fixtures as
JSON so a reviewer can open a file, change an amount, and re-run. A cache would make that
require a restart, quietly weakening the one property the format was chosen for. The files
are around a kilobyte.

**The fixtures directory is a module constant**, resolved relative to the package, not an
injected dependency. Nothing varies it: [TESTS.md](../../../tests/TESTS.md) is explicit
that the suite reads the same files as the running service, so there is no second
directory to point at. Making it an argument is a one-line change if that ever stops being
true.

### Two shape choices the loaders inherit

**Each file is a flat array and every row carries `user_id`.** That is the filter key.
`ChargingSession` and `Invoice` do not declare it, so the loader drops it before
validating those two; `User` *does* declare it — it is a genuine field there — so its row
validates whole. A map keyed by user would work too; a flat table is how the real data
source behaves, and it makes "filter first, validate second" the obvious implementation
rather than a discipline.

**Amounts, `kwh` and `cost` are JSON strings, not numbers** — `"42.10"`, not `42.10`.
Parsed as a JSON float, `42.10` is `42.1` by the time a template interpolates it, and a
reply stating `42.1` when the fixture says `42.10` is a literal no tool returned. Strings
parse to `Decimal` exactly, which is also the type grounding layer 2 compares against
([ADR 0004](../../../docs/adr/0004-two-layer-grounding-enforcement.md)).

Users carry varied `language` values (`pt`, `en`, `fr`) even though replies are English
only. The field is read and traced; not routing on it is a documented scope cut
([ADR 0006](../../../docs/adr/0006-fake-first-llm-behind-a-client-protocol.md)).

JSON rather than SQLite so a reviewer can open a file, change an amount, and re-run —
reasoning in [ADR 0003](../../../docs/adr/0003-sqlite-behind-a-repository-protocol.md).

---

## Structure

```
tools/
  __init__.py
  registry.py    name -> (callable, arg schema); the dispatch chokepoint
  loaders.py     the three tools, the shared `_collection` body, and the fixture reading
  errors.py      UserNotFound, NoDataAvailable, ToolExecutionError, failed_fields()
  TOOLS.md       this file
../fixtures/     users.json, sessions.json, invoices.json
```

`get_user`, `get_charging_sessions` and `get_invoices` live in `loaders.py` rather than a
module of their own, because each one *is* a thin loader: filter to the user, validate,
sort, raise if empty. Splitting sixty lines across two files to honour a naming instinct
would add an import hop and a second place to look when the review asks for a fourth
tool.

Fixtures live outside the `tools/` package because they are data, not code, and are read
by path rather than imported.

---

## Why exactly three

The brief fixes the number. Worth noting what it costs: there is no tool for tariffs,
station status, or support history, so tickets needing those facts cannot be answered.
That is not a gap to work around — it resolves to `UNSUPPORTED_INTENT` or
`DATA_NOT_FOUND`, which is the correct behaviour. The system's honest answer to a question
it lacks data for is a handoff.
