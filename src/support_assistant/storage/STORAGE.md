# Storage

Tickets and traces, in SQLite, behind a protocol.

**Why:** [ADR 0003](../../../docs/adr/0003-sqlite-behind-a-repository-protocol.md) — and
note the split it describes. Tool fixture data is *not* here; it stays as JSON in
`fixtures/`, read by the tools ([TOOLS.md](../tools/TOOLS.md)). This package holds only
what the system writes.

---

## The protocol

```python
class TicketRepository(Protocol):
    def create(self, ticket: Ticket) -> None: ...
    def get(self, ticket_id: str) -> Ticket | None: ...
    def finalise(
        self,
        ticket_id: str,
        status: TicketStatus,
        reply: str | None,
        handoff_reason: HandoffReason | None,
        trace: list[TraceStep],
    ) -> None: ...
```

Three methods, because the pipeline only ever does three things: record a new ticket, read
one back, and write a terminal state.

**`finalise` is one call, not four setters.** Status, reply, reason, and trace are written
in a single transaction. That is what makes it impossible to observe a ticket that is
`replied` but whose trace is still being written, or `handed_off` with a stale reply from
a previous attempt. The atomicity is the reason the signature looks the way it does.

There is no `update_status`, and no partial write. A run reaches a terminal state or it
does not.

---

## Implementations

| Implementation | Used by | Notes |
|---|---|---|
| `SqliteTicketRepository` | the running service | file-backed, `PRAGMA journal_mode=WAL` |
| `InMemoryTicketRepository` | tests | a dict, same protocol, no I/O |

Both are exercised by **one shared contract test suite**, parametrised over the two
implementations. This is the part that matters: a test double that has quietly drifted
from the real thing is worse than no double, because it makes the suite confidently
wrong. The contract suite is what keeps the in-memory implementation honest.

---

## Schema

Created at startup with `CREATE TABLE IF NOT EXISTS`. No migration tool — the schema is
small and the database is disposable.

```sql
CREATE TABLE IF NOT EXISTS tickets (
    id             TEXT PRIMARY KEY,
    user_id        TEXT NOT NULL,
    subject        TEXT NOT NULL,
    body           TEXT NOT NULL,
    status         TEXT NOT NULL
                   CHECK (status IN ('processing', 'replied', 'handed_off')),
    reply          TEXT,
    handoff_reason TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,

    -- The reply/handoff invariant, enforced by the database rather than by convention.
    CHECK (
        (status = 'processing'  AND reply IS NULL     AND handoff_reason IS NULL)
     OR (status = 'replied'     AND reply IS NOT NULL AND handoff_reason IS NULL)
     OR (status = 'handed_off'  AND reply IS NULL     AND handoff_reason IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS trace_steps (
    ticket_id TEXT    NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    seq       INTEGER NOT NULL,
    ts        TEXT    NOT NULL,          -- ISO-8601 UTC, from the injected Clock
    type      TEXT    NOT NULL,
    payload   TEXT    NOT NULL,          -- JSON, shape depends on `type`
    PRIMARY KEY (ticket_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);
```

### Why the table `CHECK` is worth having

ADR 0005 says a handed-off ticket has `reply == None`, always. That is the kind of
invariant that holds until the one code path that forgets it. Encoding it as a constraint
means a violation is an immediate error at the write, not a wrong reply discovered by a
customer. The application enforces it too; the database is the backstop.

`idx_tickets_status` supports finding tickets stranded in `processing` — the reaper query
from ADR 0001's known limitation, and the source of the stranded-ticket gauge in
[OBSERVABILITY.md](../observability/OBSERVABILITY.md).

### Trace steps as JSON

Step types have different fields ([TRACEABILITY.md](../tracing/TRACEABILITY.md)), so a
typed column per field would be a wide sparse table. `(ticket_id, seq)` gives ordering and
identity; `ts` and `type` are promoted to columns because they are present on every step
and are the two fields worth filtering or sorting by; `payload` holds the rest.

The cost is that steps are not queryable by their inner fields in SQL. Acceptable — the
access pattern is "fetch every step for one ticket, in order", which is exactly what the
primary key serves. Aggregate questions ("how often does grounding fail?") are answered by
metrics, not by querying traces.

---

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_PATH` | `data/tickets.db` | SQLite file. `:memory:` is valid for throwaway runs |

`data/` is gitignored. In `docker compose` it is a named volume, so traces survive
`docker compose restart` — otherwise "persisted" would be a claim the demo contradicts.

---

## Structure

```
storage/
  __init__.py
  protocol.py     TicketRepository
  sqlite.py       SqliteTicketRepository, schema DDL, connection handling
  memory.py       InMemoryTicketRepository
  STORAGE.md      this file
```

---

## What this is not built for

- **Concurrent writers across processes.** WAL makes SQLite safe for the single-process
  service here; the protocol is the seam where Postgres would attach.
  [Roadmap](../../../docs/ROADMAP.md#postgres-and-multiple-writers).
- **Retention or archival.** Traces accumulate forever.
  [Roadmap](../../../docs/ROADMAP.md#trace-retention).
- **Querying across tickets.** No search, filtering, or listing endpoint. The brief's
  access pattern is by id. [Roadmap](../../../docs/ROADMAP.md#cross-ticket-queries).
