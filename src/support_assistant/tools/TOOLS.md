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

Three reasons this indirection earns its place:

1. **Containment.** A model — fake or real — can only reach registered tools. An
   unrecognised name is a `ToolExecutionError`, not an `AttributeError` or, worse, a call
   to something that was never meant to be a tool.
2. **Uniform tracing.** Every call passes one chokepoint, so `tool_call` and
   `tool_result` steps are recorded in one place rather than at three call sites.
3. **Argument validation.** Each entry declares a Pydantic schema; bad arguments fail
   before the tool body runs.

Adding a fourth tool is: write the function, register it with a schema, add its keyword
rule to `FakeLLM`, add a fixture, add a test. That is the shape of the change the live
review session is most likely to ask for.

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
  loaders.py     fixture reading + validation into typed models
  errors.py      UserNotFound, NoDataAvailable, ToolExecutionError
  TOOLS.md       this file
../fixtures/     users.json, sessions.json, invoices.json
```

Fixtures live outside the `tools/` package because they are data, not code, and are read
by path rather than imported.

---

## Why exactly three

The brief fixes the number. Worth noting what it costs: there is no tool for tariffs,
station status, or support history, so tickets needing those facts cannot be answered.
That is not a gap to work around — it resolves to `UNSUPPORTED_INTENT` or
`DATA_NOT_FOUND`, which is the correct behaviour. The system's honest answer to a question
it lacks data for is a handoff.
