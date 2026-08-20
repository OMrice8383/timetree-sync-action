from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

CORE_TABLES = (
    "event_links",
    "sync_state",
    "sync_operations",
    "conflicts",
)

SYNC_STATE_KEYS = (
    "google_sync_token",
    "timetree_updated_after_ms",
    "last_google_sync_at",
    "last_timetree_sync_at",
    "last_mcp_reconcile_at",
    "last_exporter_verify_at",
    "bridge_bootstrapped_at",
)

SCHEMA_VERSION = 1

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS event_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    timetree_event_id TEXT UNIQUE,
    google_event_id TEXT UNIQUE,

    timetree_parent_event_id TEXT,
    google_parent_event_id TEXT,

    event_kind TEXT NOT NULL DEFAULT 'single',

    last_synced_hash TEXT,

    status TEXT NOT NULL,

    last_synced_at TEXT,
    deleted_at TEXT,

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_state (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_operations (
    operation_id TEXT PRIMARY KEY,

    direction TEXT NOT NULL,
    action TEXT NOT NULL,

    source_event_id TEXT,
    target_event_id TEXT,

    source_hash TEXT,
    payload_hash TEXT,

    state TEXT NOT NULL,

    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conflicts (
    conflict_id TEXT PRIMARY KEY,

    event_link_id INTEGER,
    conflict_type TEXT NOT NULL,

    timetree_snapshot_json TEXT,
    google_snapshot_json TEXT,

    status TEXT NOT NULL,
    resolution TEXT,

    created_at TEXT NOT NULL,
    resolved_at TEXT
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def migrate(connection: sqlite3.Connection) -> None:
    with connection:
        connection.executescript(_SCHEMA_SQL)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


@contextmanager
def ensure_database(path: str | Path) -> Iterator[sqlite3.Connection]:
    connection = connect(path)
    try:
        migrate(connection)
        yield connection
    finally:
        connection.close()


def core_tables(connection: sqlite3.Connection) -> tuple[str, ...]:
    rows = connection.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name IN (?, ?, ?, ?)
        ORDER BY name
        """,
        CORE_TABLES,
    ).fetchall()
    return tuple(row["name"] for row in rows)


def schema_version(connection: sqlite3.Connection) -> int:
    return int(connection.execute("PRAGMA user_version").fetchone()[0])


def table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in CORE_TABLES:
        row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        counts[table] = int(row[0])
    return counts


def sync_state_presence(connection: sqlite3.Connection) -> dict[str, bool]:
    placeholders = ",".join("?" for _ in SYNC_STATE_KEYS)
    rows = connection.execute(
        f"SELECT key FROM sync_state WHERE key IN ({placeholders})",
        SYNC_STATE_KEYS,
    ).fetchall()
    present = {row["key"] for row in rows}
    return {key: key in present for key in SYNC_STATE_KEYS}
