from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

EVENT_LINK_STATUSES = frozenset(
    {"synced", "conflict", "deleted", "error", "unsupported"}
)

OPERATION_STATES = frozenset(
    {"prepared", "remote_applied", "mapping_saved", "done", "failed"}
)

NEEDS_MANUAL_RECOVERY = "NEEDS_MANUAL_RECOVERY"

_ALLOWED_OPERATION_TRANSITIONS = {
    "prepared": frozenset({"remote_applied", "failed"}),
    "remote_applied": frozenset({"mapping_saved", "done", "failed"}),
    "mapping_saved": frozenset({"done", "failed"}),
    "done": frozenset(),
    "failed": frozenset(),
}


class RepositoryError(RuntimeError):
    """Base error for persistent bridge state operations."""


class InvalidRepositoryValue(RepositoryError):
    """Raised when a canonical repository value is invalid."""


class OperationTransitionError(RepositoryError):
    """Raised when a sync operation attempts an unsafe state transition."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class StateRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def create_event_link(
        self,
        *,
        timetree_event_id: str | None,
        google_event_id: str | None,
        timetree_parent_event_id: str | None = None,
        google_parent_event_id: str | None = None,
        event_kind: str = "single",
        last_synced_hash: str | None = None,
        status: str = "synced",
        last_synced_at: str | None = None,
        deleted_at: str | None = None,
    ) -> int:
        if status not in EVENT_LINK_STATUSES:
            raise InvalidRepositoryValue(f"Invalid event_links.status: {status}")

        now = _utc_now()
        with self.connection:
            cursor = self.connection.execute(
                """
                INSERT INTO event_links (
                    timetree_event_id,
                    google_event_id,
                    timetree_parent_event_id,
                    google_parent_event_id,
                    event_kind,
                    last_synced_hash,
                    status,
                    last_synced_at,
                    deleted_at,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timetree_event_id,
                    google_event_id,
                    timetree_parent_event_id,
                    google_parent_event_id,
                    event_kind,
                    last_synced_hash,
                    status,
                    last_synced_at,
                    deleted_at,
                    now,
                    now,
                ),
            )
        return int(cursor.lastrowid)

    def get_event_link(self, link_id: int) -> dict[str, Any] | None:
        return _row_dict(
            self.connection.execute(
                "SELECT * FROM event_links WHERE id = ?",
                (link_id,),
            ).fetchone()
        )

    def get_event_link_by_timetree_id(
        self,
        timetree_event_id: str,
    ) -> dict[str, Any] | None:
        return _row_dict(
            self.connection.execute(
                "SELECT * FROM event_links WHERE timetree_event_id = ?",
                (timetree_event_id,),
            ).fetchone()
        )

    def get_event_link_by_google_id(
        self,
        google_event_id: str,
    ) -> dict[str, Any] | None:
        return _row_dict(
            self.connection.execute(
                "SELECT * FROM event_links WHERE google_event_id = ?",
                (google_event_id,),
            ).fetchone()
        )

    def update_event_link(
        self,
        link_id: int,
        *,
        status: str | None = None,
        last_synced_hash: str | None = None,
        last_synced_at: str | None = None,
        deleted_at: str | None = None,
    ) -> dict[str, Any]:
        if status is not None and status not in EVENT_LINK_STATUSES:
            raise InvalidRepositoryValue(f"Invalid event_links.status: {status}")

        fields = {
            "status": status,
            "last_synced_hash": last_synced_hash,
            "last_synced_at": last_synced_at,
            "deleted_at": deleted_at,
        }
        updates = {key: value for key, value in fields.items() if value is not None}
        if not updates:
            existing = self.get_event_link(link_id)
            if existing is None:
                raise RepositoryError(f"event_link not found: {link_id}")
            return existing

        updates["updated_at"] = _utc_now()
        assignments = ", ".join(f"{key} = ?" for key in updates)
        values = [*updates.values(), link_id]

        with self.connection:
            cursor = self.connection.execute(
                f"UPDATE event_links SET {assignments} WHERE id = ?",
                values,
            )
        if cursor.rowcount != 1:
            raise RepositoryError(f"event_link not found: {link_id}")

        updated = self.get_event_link(link_id)
        assert updated is not None
        return updated

    def delete_event_link(self, link_id: int) -> bool:
        with self.connection:
            cursor = self.connection.execute(
                "DELETE FROM event_links WHERE id = ?",
                (link_id,),
            )
        return cursor.rowcount == 1

    def set_sync_state(self, key: str, value: str | None) -> None:
        now = _utc_now()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO sync_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, value, now),
            )

    def get_sync_state(self, key: str) -> str | None:
        row = self.connection.execute(
            "SELECT value FROM sync_state WHERE key = ?",
            (key,),
        ).fetchone()
        return None if row is None else row["value"]

    def delete_sync_state(self, key: str) -> bool:
        with self.connection:
            cursor = self.connection.execute(
                "DELETE FROM sync_state WHERE key = ?",
                (key,),
            )
        return cursor.rowcount == 1

    def create_operation(
        self,
        *,
        operation_id: str,
        direction: str,
        action: str,
        source_event_id: str | None = None,
        target_event_id: str | None = None,
        source_hash: str | None = None,
        payload_hash: str | None = None,
    ) -> None:
        now = _utc_now()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO sync_operations (
                    operation_id,
                    direction,
                    action,
                    source_event_id,
                    target_event_id,
                    source_hash,
                    payload_hash,
                    state,
                    attempts,
                    last_error,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 'prepared', 0, NULL, ?, ?)
                """,
                (
                    operation_id,
                    direction,
                    action,
                    source_event_id,
                    target_event_id,
                    source_hash,
                    payload_hash,
                    now,
                    now,
                ),
            )

    def get_operation(self, operation_id: str) -> dict[str, Any] | None:
        return _row_dict(
            self.connection.execute(
                "SELECT * FROM sync_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        )

    def transition_operation(
        self,
        operation_id: str,
        to_state: str,
        *,
        target_event_id: str | None = None,
        last_error: str | None = None,
    ) -> dict[str, Any]:
        if to_state not in OPERATION_STATES:
            raise InvalidRepositoryValue(f"Invalid sync_operations.state: {to_state}")

        current = self.get_operation(operation_id)
        if current is None:
            raise RepositoryError(f"sync_operation not found: {operation_id}")

        from_state = current["state"]
        if from_state not in OPERATION_STATES:
            raise OperationTransitionError(
                f"Stored sync operation has invalid state: {from_state}"
            )

        if to_state != from_state and to_state not in _ALLOWED_OPERATION_TRANSITIONS[from_state]:
            raise OperationTransitionError(
                f"Unsafe sync operation transition: {from_state} -> {to_state}"
            )

        updates: dict[str, Any] = {
            "state": to_state,
            "updated_at": _utc_now(),
        }
        if target_event_id is not None:
            updates["target_event_id"] = target_event_id
        if last_error is not None or to_state != "failed":
            updates["last_error"] = last_error

        assignments = ", ".join(f"{key} = ?" for key in updates)
        values = [*updates.values(), operation_id]
        with self.connection:
            self.connection.execute(
                f"UPDATE sync_operations SET {assignments} WHERE operation_id = ?",
                values,
            )

        updated = self.get_operation(operation_id)
        assert updated is not None
        return updated

    def increment_operation_attempts(
        self,
        operation_id: str,
        *,
        last_error: str | None = None,
    ) -> dict[str, Any]:
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE sync_operations
                SET attempts = attempts + 1,
                    last_error = ?,
                    updated_at = ?
                WHERE operation_id = ?
                """,
                (last_error, _utc_now(), operation_id),
            )
        if cursor.rowcount != 1:
            raise RepositoryError(f"sync_operation not found: {operation_id}")

        updated = self.get_operation(operation_id)
        assert updated is not None
        return updated

    def mark_manual_recovery(self, operation_id: str) -> dict[str, Any]:
        return self.transition_operation(
            operation_id,
            "failed",
            last_error=NEEDS_MANUAL_RECOVERY,
        )

    def delete_operation(self, operation_id: str) -> bool:
        with self.connection:
            cursor = self.connection.execute(
                "DELETE FROM sync_operations WHERE operation_id = ?",
                (operation_id,),
            )
        return cursor.rowcount == 1

    def create_conflict(
        self,
        *,
        conflict_id: str,
        event_link_id: int | None,
        conflict_type: str,
        timetree_snapshot_json: str | None,
        google_snapshot_json: str | None,
        status: str,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO conflicts (
                    conflict_id,
                    event_link_id,
                    conflict_type,
                    timetree_snapshot_json,
                    google_snapshot_json,
                    status,
                    resolution,
                    created_at,
                    resolved_at
                )
                VALUES (?, ?, ?, ?, ?, ?, NULL, ?, NULL)
                """,
                (
                    conflict_id,
                    event_link_id,
                    conflict_type,
                    timetree_snapshot_json,
                    google_snapshot_json,
                    status,
                    _utc_now(),
                ),
            )

    def get_conflict(self, conflict_id: str) -> dict[str, Any] | None:
        return _row_dict(
            self.connection.execute(
                "SELECT * FROM conflicts WHERE conflict_id = ?",
                (conflict_id,),
            ).fetchone()
        )

    def resolve_conflict(
        self,
        conflict_id: str,
        *,
        status: str,
        resolution: str,
    ) -> dict[str, Any]:
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE conflicts
                SET status = ?,
                    resolution = ?,
                    resolved_at = ?
                WHERE conflict_id = ?
                """,
                (status, resolution, _utc_now(), conflict_id),
            )
        if cursor.rowcount != 1:
            raise RepositoryError(f"conflict not found: {conflict_id}")

        updated = self.get_conflict(conflict_id)
        assert updated is not None
        return updated

    def delete_conflict(self, conflict_id: str) -> bool:
        with self.connection:
            cursor = self.connection.execute(
                "DELETE FROM conflicts WHERE conflict_id = ?",
                (conflict_id,),
            )
        return cursor.rowcount == 1
