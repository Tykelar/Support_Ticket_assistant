# Tools

The pipeline's only route to data. Exactly three, as the brief specifies, backed by local
JSON fixtures.

Tools read fixture data and return it, or fail in a typed way. They decide nothing — they
raise, and the orchestrator converts that into an outcome
([ADR 0005](../../../docs/adr/0005-fail-closed-to-human-handoff.md)).

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

A session's `status` is `completed`, `interrupted` or `failed`; an invoice's is `paid`,
`pending` or `failed`. Those enumerations matter beyond validation: because status words
are enumerated rather than free text, they enter the `FactSet` as facts and grounding
layer 1 covers them
([ADR 0004](../../../docs/adr/0004-two-layer-grounding-enforcement.md)).

---

## Error semantics

| Exception | Raised when | Becomes |
|---|---|---|
| `UserNotFound` | no fixture record for that `user_id` | `USER_NOT_FOUND` |
| `NoDataAvailable` | the user exists, but has no rows of the requested kind | `DATA_NOT_FOUND` |
| `ToolExecutionError` | the fixture is malformed, unreadable, or fails validation | `TOOL_ERROR` |

An empty list is **not** a valid success: the two collection tools raise `NoDataAvailable`
rather than returning `[]`. The opposite looks obvious — "you have no invoices" *is* a
truthful, grounded reply — but zero rows is ambiguous: genuinely none, not yet synced, or a
broken join upstream, and the tool cannot tell which. The last two would produce a
confident wrong statement about a customer's billing
([ADR 0009](../../../docs/adr/0009-absent-data-is-a-handoff.md)).

`get_user` cannot be empty: a user exists, or raises `UserNotFound`. Both collection tools
check the user first, so the reason is right whichever tool the loop reaches.

**A failure message carries a locator, not the payload**, because it reaches the trace,
which is served over the API:

```
invoice inv_601 for u_006 failed validation: amount
```

The record id gets a reader to the row; the offending value is one lookup away for anyone
entitled to it. The full `ValidationError` goes to the log, where the audience is a
developer ([TRACEABILITY.md](../tracing/TRACEABILITY.md)).

---

## The registry

The loop never imports a tool directly. It dispatches through a registry:

```python
registry.run(name: str, args: dict[str, Any]) -> ToolResult
```

Two reasons this indirection earns its place:

1. **Containment.** A model can only reach registered tools. An unrecognised name is a
   `ToolExecutionError`, not an `AttributeError` or a call to something that was never
   meant to be a tool.
2. **Argument validation.** Each entry declares a Pydantic schema; bad arguments fail
   before the tool body runs.

**Tracing is not one of them.** The orchestrator records `tool_call` and `tool_result`, and
the registry never sees a `TraceRecorder` — which is what lets `tools/` import nothing from
`tracing/` and keeps a tool testable without assembling a trace
([ADR 0010](../../../docs/adr/0010-the-orchestrator-records-tool-steps.md)).

### `ToolResult`

```python
class ToolResult:
    tool: str                                        # which tool produced this
    records: list[User | ChargingSession | Invoice]  # always a list; get_user yields one
```

The tools return domain types; the *registry* wraps them, which keeps a tool function
readable on its own. `records` is always a list so that summarisation and `FactSet`
projection each have one code path.

`ToolResult` is defined in `domain.py`, not here: three components consume it, and
ARCHITECTURE.md forbids `llm/` and `guardrails/` from importing `tools/`.

### Adding a fourth tool

More than one file, and two of the steps fail silently or only at runtime:

| Step | If forgotten |
|---|---|
| the loader in `loaders.py` + an entry in `registry.py` | dispatch fails loudly |
| a member of `ToolResult.records` in `domain.py` | pydantic rejects the result |
| a summariser in `tracing/summarise.py` | **runtime `ValueError`, every call hands off** |
| a projection branch in `guardrails/factset.py` | **silent — the records never become facts** |
| a keyword rule in `FakeLLM`, a line in `OllamaLLM`'s catalogue, a fixture | the tool is never called |

`test_registry.py::test_every_registered_tool_is_summarised_and_projected` runs each
registered tool through the whole downstream path, so the two dangerous ones fail the suite
rather than production.

---

## Fixtures

`src/support_assistant/fixtures/` — `users.json`, `sessions.json`, `invoices.json`.

Five user records and one deliberate absence, chosen so that **every pipeline path has a
user that exercises it**. The fixtures are test infrastructure as much as sample data.

| User | Shape | Exercises |
|---|---|---|
| `u_001` Ana | all invoices paid, sessions completed | billing happy path, nothing wrong |
| `u_002` Ben | one **failed** invoice among paid ones | billing happy path with a real problem to explain |
| `u_003` Chloe | one **interrupted** session | charging-session happy path |
| `u_004` Dmitri | exists; **zero** sessions, **zero** invoices | `DATA_NOT_FOUND` handoff |
| `u_005` | **absent from the fixtures entirely** | `USER_NOT_FOUND` handoff |
| `u_006` Eva | invoice record with a **malformed amount** | `TOOL_ERROR` handoff |

### Filter first, validate second

`invoices.json` holds every user's invoices, and `u_006` carries a deliberately malformed
amount. A loader that validated the whole file eagerly would let that one bad record break
`get_invoices` for **every** user — the fixture built to exercise one failure path taking
out the happy paths as collateral.

So a tool filters to the requested `user_id`, then validates only the rows it fetched,
which is also how a real database adapter behaves. A regression test pins it: `u_006` must
raise while `u_002` still returns its invoices from the same file.

### How the loaders read

**Sorted in the loader, not trusted from the file.** The fixtures are written newest-first
and a test asserts it, but a reviewer editing a file should not be able to silently break a
documented contract.

**Read and parsed on every call, never cached**, so a reviewer can change an amount and
re-run without a restart — the property JSON fixtures were chosen for
([ADR 0003](../../../docs/adr/0003-sqlite-behind-a-repository-protocol.md)). The files are
around a kilobyte.

**The fixtures directory is a module constant**, since the suite reads the same files as
the running service and there is no second directory to point at.

### Two shape choices

**Each file is a flat array and every row carries `user_id`**, the filter key.
`ChargingSession` and `Invoice` do not declare it, so the loader drops it before
validating; `User` does, so its row validates whole. A flat table is how a real data source
behaves, and it makes "filter first, validate second" the obvious implementation rather
than a discipline.

**Amounts, `kwh` and `cost` are JSON strings, not numbers** — `"42.10"`, not `42.10`. As a
JSON float, `42.10` is `42.1` by the time a template interpolates it, and a reply stating
`42.1` when the fixture says `42.10` is a literal no tool returned. Strings parse to
`Decimal` exactly, which is the type grounding compares against.

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

The three tools share `loaders.py` because each one *is* a thin loader. Splitting sixty
lines across two files would add an import hop and a second place to look.

---

## Why exactly three

The brief fixes the number. Worth noting what it costs: there is no tool for tariffs,
station status or support history, so tickets needing those facts cannot be answered. That
is not a gap to work around — it resolves to `UNSUPPORTED_INTENT` or `DATA_NOT_FOUND`,
which is the correct behaviour. The honest answer to a question the system lacks data for
is a handoff.
