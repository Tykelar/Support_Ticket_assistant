# ADR 0003 — SQLite behind a repository protocol

**Status:** Accepted · 2026-08-31

## Context

Two different kinds of data need a home, and they have opposite requirements.

**Tickets and traces** are written by the system. The brief says to *persist* a trace and
requires that a support agent can answer "why did the AI say this?" from
`GET /tickets/{id}` alone. That is an audit record; losing it on restart undermines the
point of having it.

**Tool fixture data** (users, charging sessions, invoices) is read-only test data that a
reviewer will want to open, read, and edit to try a scenario. The brief offers JSON files
or SQLite and asks for 3–5 users with varied data.

## Decision

Split them.

- Tickets and traces go in **SQLite**, reached through a `TicketRepository` protocol.
  An `InMemoryTicketRepository` implements the same protocol and is the test double.
- Tool fixtures stay **JSON files** in `src/support_assistant/fixtures/`, loaded by the
  tool adapters.

## Consequences

- Traces survive a restart, so the audit story holds up.
- Defining the storage boundary as a protocol keeps the pipeline ignorant of SQLite. The
  pipeline tests run against the in-memory implementation and stay fast; one contract
  test suite runs against both implementations to keep them honest.
- That same boundary is where a real queue (ADR 0001) or Postgres would later attach.
- A reviewer can open `invoices.json`, change an amount, and re-run — no SQL, no
  migration, no seed script.
- Two storage mechanisms instead of one, which needs the explanation above to not look
  like indecision.
- Schema changes are handled by `CREATE TABLE IF NOT EXISTS` at startup. No migration
  tool; the schema is small and the database is disposable.

## Alternatives considered

**In-memory only.** Fastest to build, zero setup. Rejected: "persist" then holds only
within a single process lifetime, and the audit trail dies with the container.

**SQLite for everything, fixtures included.** Marginally more production-like. Rejected
because it makes the fixtures materially harder for a reviewer to read and modify, which
is the main thing fixtures are for here.
