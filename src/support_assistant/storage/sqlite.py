"""`SqliteTicketRepository` -- the file-backed implementation the running service uses.

The schema is STORAGE.md's, created at startup with `CREATE TABLE IF NOT EXISTS`. No
migration tool: the schema is small and the database is disposable
([ADR 0003](../../../docs/adr/0003-sqlite-behind-a-repository-protocol.md)).

Three things here are load-bearing rather than incidental:

- **The table `CHECK`s are the backstop for ADR 0005's invariant.** A handed-off ticket
  has `reply IS NULL`, always. `Ticket`'s own validator enforces the same rule, and the
  duplication is deliberate -- the application stops the bad write being attempted, the
  database stops it landing if a future code path forgets.
- **`finalise` is one transaction.** `UPDATE tickets` and the trace rows go in together,
  so a ticket is never observable as `replied` with a half-written trace (STORAGE.md).
- **One connection, `check_same_thread=False`, every operation behind a lock.** The
  pipeline runs as a background task off the event loop, so the repository is reached
  from a worker thread; and a connection per call would make `:memory:` -- which
  STORAGE.md allows -- lose its contents between calls. The lock is what makes
  PIPELINE.md's "the repository serialises writes" true rather than aspirational.

Trace steps are stored as JSON with `seq`, `ts` and `type` promoted to columns, because
those three are on every step and are the fields worth ordering and filtering by, while
the rest of the shape depends on the type. `TypeAdapter(TraceStep)` does both directions,
so a persisted trace reconstructs into the same typed steps rather than untyped dicts.
"""

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from support_assistant.clock import Clock
from support_assistant.domain import Ticket
from support_assistant.enums import HandoffReason, TicketStatus
from support_assistant.storage.protocol import TicketAlreadyExists, TicketNotFound
from support_assistant.tracing.models import TraceStep

DEFAULT_DATABASE_PATH = "data/tickets.db"
"""Where the service keeps its database. `data/` is gitignored and is a named volume in
`docker compose`, so traces survive a restart -- otherwise "persisted" would be a claim
the demo contradicts (STORAGE.md)."""

MEMORY = ":memory:"
"""Valid for throwaway runs. Contents live as long as the connection, which is why this
class holds one open rather than opening one per call."""

_PROMOTED = ("seq", "ts", "type")
"""The step fields that are columns rather than payload."""

_STEP = TypeAdapter(TraceStep)
"""One adapter for the whole discriminated union, in both directions."""

SCHEMA = """
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
"""


def database_path() -> str:
    """`DATABASE_PATH` from the environment, or the default.

    A function rather than a constant read at import, so a test or a container can set the
    variable without import order deciding the answer -- the same shape as
    `guardrails.limits.max_iterations()`.
    """
    return os.environ.get("DATABASE_PATH", DEFAULT_DATABASE_PATH)


class SqliteTicketRepository:
    """Tickets and traces in SQLite, behind `TicketRepository`."""

    def __init__(self, path: str | Path, clock: Clock) -> None:
        """Both arguments are explicit: the path so a caller states which database it
        means (`database_path()` supplies the configured one), the clock because nothing
        in this system reads the wall clock (ADR 0008)."""
        self._clock = clock
        self._lock = threading.Lock()
        self._connection = self._connect(path)
        with self._connection:
            self._connection.executescript(SCHEMA)

    @staticmethod
    def _connect(path: str | Path) -> sqlite3.Connection:
        if str(path) != MEMORY:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(path), check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @property
    def connection(self) -> sqlite3.Connection:
        """The open connection. Exposed so a test can reach the constraints directly --
        the three-way `CHECK` is unreachable through the protocol, because `Ticket`'s
        validator rejects the same state before a write is attempted."""
        return self._connection

    def close(self) -> None:
        """Release the connection. The service holds one for its lifetime; tests close
        theirs so a temporary file can be removed on Windows."""
        self._connection.close()

    # ----------------------------------------------------------------------------------
    # The protocol
    # ----------------------------------------------------------------------------------

    def create(self, ticket: Ticket) -> None:
        with self._lock, self._connection:
            try:
                self._connection.execute(
                    "INSERT INTO tickets (id, user_id, subject, body, status, reply,"
                    " handoff_reason, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        ticket.id,
                        ticket.user_id,
                        ticket.subject,
                        ticket.body,
                        ticket.status.value,
                        ticket.reply,
                        None if ticket.handoff_reason is None else ticket.handoff_reason.value,
                        ticket.created_at.isoformat(),
                        ticket.updated_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                # The id is 128 bits of randomness, so this is a re-create bug rather than
                # a collision -- and overwriting would lose a trace.
                raise TicketAlreadyExists(ticket.id) from exc
        # `ticket.trace` is deliberately not written: the field is read-populated and
        # write-ignored, and a new ticket has no trace (STORAGE.md).

    def get(self, ticket_id: str) -> Ticket | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
            ).fetchone()
            if row is None:
                return None
            steps = self._connection.execute(
                "SELECT seq, ts, type, payload FROM trace_steps WHERE ticket_id = ? ORDER BY seq",
                (ticket_id,),
            ).fetchall()

        return Ticket(
            id=row["id"],
            user_id=row["user_id"],
            subject=row["subject"],
            body=row["body"],
            status=TicketStatus(row["status"]),
            reply=row["reply"],
            handoff_reason=(
                None if row["handoff_reason"] is None else HandoffReason(row["handoff_reason"])
            ),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            trace=[_step_from_row(step) for step in steps],
        )

    def finalise(
        self,
        ticket_id: str,
        status: TicketStatus,
        reply: str | None,
        handoff_reason: HandoffReason | None,
        trace: list[TraceStep],
    ) -> None:
        with self._lock, self._connection:
            updated = self._connection.execute(
                "UPDATE tickets SET status = ?, reply = ?, handoff_reason = ?, updated_at = ?"
                " WHERE id = ?",
                (
                    status.value,
                    reply,
                    None if handoff_reason is None else handoff_reason.value,
                    self._clock.now().isoformat(),
                    ticket_id,
                ),
            )
            if updated.rowcount == 0:
                raise TicketNotFound(ticket_id)
            # Replaced, not appended: a run writes its trace once, and a trace that
            # doubled would say a step happened twice when it happened once.
            self._connection.execute("DELETE FROM trace_steps WHERE ticket_id = ?", (ticket_id,))
            self._connection.executemany(
                "INSERT INTO trace_steps (ticket_id, seq, ts, type, payload)"
                " VALUES (?, ?, ?, ?, ?)",
                [_step_to_row(ticket_id, step) for step in trace],
            )


# --------------------------------------------------------------------------------------
# Trace step (de)serialisation
# --------------------------------------------------------------------------------------


def _step_to_row(ticket_id: str, step: TraceStep) -> tuple[str, int, str, str, str]:
    """One step as its row.

    `by_alias` so `Violation.literal_class` is stored under the `class` key
    TRACEABILITY.md documents. Key order is left exactly as the producer built it and is
    **not** sorted: `summarise.py` emits a tool result's status distribution in
    enum-declaration order on purpose, and sorting here would quietly replace that with
    alphabetical the moment a trace was persisted. Byte-stability comes from the
    producers being deterministic, which is a property this system has anyway.
    """
    data: dict[str, Any] = _STEP.dump_python(step, mode="json", by_alias=True)
    payload = {key: value for key, value in data.items() if key not in _PROMOTED}
    return ticket_id, data["seq"], data["ts"], data["type"], json.dumps(payload)


def _step_from_row(row: sqlite3.Row) -> TraceStep:
    """The inverse: promoted columns back into the payload, then through the union so the
    step reconstructs as its own class rather than a dict."""
    data = json.loads(row["payload"])
    data.update(seq=row["seq"], ts=row["ts"], type=row["type"])
    return _STEP.validate_python(data)
