"""DDL for agent-habitat's audit tables — ADR-002 Option 1.

These three tables live in `data/state/agent_habitat.db`. LangGraph's
`SqliteSaver` (wired in Phase 2 when the orchestrator lands) will create its
own tables in the same file; we do not touch them and they do not touch ours.

Schema mirrors ADR-002 exactly. CHECK constraints encode the status / level
enums and are duplicated in `models.py` as Python enums — keep both in sync.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DDL_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS workflows (
      id              TEXT PRIMARY KEY,
      workflow_type   TEXT NOT NULL,
      status          TEXT NOT NULL CHECK (
                        status IN ('running','paused','completed','failed','cancelled')),
      started_at      TEXT NOT NULL,
      finished_at     TEXT,
      cost_total_usd  REAL NOT NULL DEFAULT 0.0,
      metadata        TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS workflow_steps (
      id              INTEGER PRIMARY KEY AUTOINCREMENT,
      workflow_id     TEXT NOT NULL REFERENCES workflows(id),
      step_index      INTEGER NOT NULL,
      agent_name      TEXT NOT NULL,
      status          TEXT NOT NULL CHECK (
                        status IN ('running','completed','failed','skipped')),
      started_at      TEXT NOT NULL,
      finished_at     TEXT,
      input_ref       TEXT,
      output_ref      TEXT,
      cost_usd        REAL NOT NULL DEFAULT 0.0,
      error_message   TEXT,
      UNIQUE (workflow_id, step_index)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
      id              INTEGER PRIMARY KEY AUTOINCREMENT,
      workflow_id     TEXT NOT NULL REFERENCES workflows(id),
      step_id         INTEGER REFERENCES workflow_steps(id),
      timestamp       TEXT NOT NULL,
      level           TEXT NOT NULL CHECK (
                        level IN ('info','warn','error','checkpoint','approval')),
      message         TEXT NOT NULL,
      structured_data TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_workflow_steps_wf ON workflow_steps(workflow_id, step_index)",
    "CREATE INDEX IF NOT EXISTS idx_events_wf_time   ON events(workflow_id, timestamp)",
)


DEFAULT_DB_PATH = Path("data/state/agent_habitat.db")


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open a SQLite connection with our standard pragmas.

    - `foreign_keys=ON` is per-connection in SQLite and OFF by default; turn
      it on so the FK references in our DDL are actually enforced.
    - `journal_mode=WAL` lets concurrent readers proceed alongside a single
      writer and reduces "database is locked" errors. WAL is database-wide
      (not per-connection) and persists across closes, so the pragma is a
      no-op when the database is already in WAL mode. WAL adds two sidecar
      files (``<db>.db-wal``, ``<db>.db-shm``); both are covered by the
      ``data/state/*.db*`` glob in ``.gitignore``. For ``:memory:`` databases
      the pragma is silently ignored — SQLite reports back "memory" rather
      than "wal" and that's the expected outcome under tests.
    - `row_factory=sqlite3.Row` lets persistence.py read columns by name.
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Create agent-habitat's audit tables if they do not exist.

    Idempotent: every statement is `CREATE ... IF NOT EXISTS`, so calling on
    an already-initialised DB is a no-op. Does NOT create LangGraph's tables
    (those are owned by `SqliteSaver` and instantiated by the orchestrator).
    """
    with conn:
        for stmt in DDL_STATEMENTS:
            conn.execute(stmt)
