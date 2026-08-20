from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from bridge.db import ensure_database
from bridge.lock import (
    RunLockHeldError,
    acquire_lock,
    inspect_lock,
    read_lock,
    release_lock,
    run_lock,
)
from bridge.repository import (
    NEEDS_MANUAL_RECOVERY,
    InvalidRepositoryValue,
    OperationTransitionError,
    StateRepository,
)


class RepositoryAndLockTests(unittest.TestCase):
    def test_event_link_and_sync_state_crud(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            with ensure_database(db_path) as connection:
                repo = StateRepository(connection)

                link_id = repo.create_event_link(
                    timetree_event_id="tt-1",
                    google_event_id="g-1",
                    status="synced",
                    last_synced_hash="hash-1",
                )
                link = repo.get_event_link(link_id)
                self.assertEqual(link["timetree_event_id"], "tt-1")
                self.assertEqual(
                    repo.get_event_link_by_google_id("g-1")["id"],
                    link_id,
                )

                updated = repo.update_event_link(
                    link_id,
                    status="conflict",
                    last_synced_hash="hash-2",
                )
                self.assertEqual(updated["status"], "conflict")
                self.assertEqual(updated["last_synced_hash"], "hash-2")

                repo.set_sync_state("google_sync_token", "token-1")
                self.assertEqual(
                    repo.get_sync_state("google_sync_token"),
                    "token-1",
                )
                repo.set_sync_state("google_sync_token", "token-2")
                self.assertEqual(
                    repo.get_sync_state("google_sync_token"),
                    "token-2",
                )
                self.assertTrue(repo.delete_sync_state("google_sync_token"))
                self.assertIsNone(repo.get_sync_state("google_sync_token"))

                self.assertTrue(repo.delete_event_link(link_id))
                self.assertIsNone(repo.get_event_link(link_id))

                with self.assertRaises(InvalidRepositoryValue):
                    repo.create_event_link(
                        timetree_event_id="tt-x",
                        google_event_id="g-x",
                        status="pending",
                    )

    def test_conflict_crud(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            with ensure_database(db_path) as connection:
                repo = StateRepository(connection)
                link_id = repo.create_event_link(
                    timetree_event_id="tt-1",
                    google_event_id="g-1",
                    status="conflict",
                )
                repo.create_conflict(
                    conflict_id="conflict-1",
                    event_link_id=link_id,
                    conflict_type="both_updated",
                    timetree_snapshot_json='{"title":"tt"}',
                    google_snapshot_json='{"title":"g"}',
                    status="open",
                )

                conflict = repo.get_conflict("conflict-1")
                self.assertEqual(conflict["status"], "open")

                resolved = repo.resolve_conflict(
                    "conflict-1",
                    status="resolved",
                    resolution="manual_keep_timetree",
                )
                self.assertEqual(resolved["status"], "resolved")
                self.assertEqual(
                    resolved["resolution"],
                    "manual_keep_timetree",
                )
                self.assertIsNotNone(resolved["resolved_at"])

                self.assertTrue(repo.delete_conflict("conflict-1"))
                self.assertIsNone(repo.get_conflict("conflict-1"))

    def test_operation_transition_and_manual_recovery_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            with ensure_database(db_path) as connection:
                repo = StateRepository(connection)
                repo.create_operation(
                    operation_id="op-create",
                    direction="timetree_to_google",
                    action="create",
                    source_event_id="tt-1",
                    source_hash="source-hash",
                    payload_hash="payload-hash",
                )

                operation = repo.get_operation("op-create")
                self.assertEqual(operation["state"], "prepared")
                self.assertEqual(operation["attempts"], 0)

                operation = repo.increment_operation_attempts("op-create")
                self.assertEqual(operation["attempts"], 1)

                operation = repo.transition_operation(
                    "op-create",
                    "remote_applied",
                    target_event_id="g-1",
                )
                self.assertEqual(operation["state"], "remote_applied")
                self.assertEqual(operation["target_event_id"], "g-1")

                operation = repo.transition_operation(
                    "op-create",
                    "remote_applied",
                    target_event_id="g-1",
                )
                self.assertEqual(operation["state"], "remote_applied")

                operation = repo.transition_operation(
                    "op-create",
                    "mapping_saved",
                )
                self.assertEqual(operation["state"], "mapping_saved")

                operation = repo.transition_operation("op-create", "done")
                self.assertEqual(operation["state"], "done")

                with self.assertRaises(OperationTransitionError):
                    repo.transition_operation("op-create", "prepared")

                repo.create_operation(
                    operation_id="op-manual",
                    direction="google_to_timetree",
                    action="create",
                )
                manual = repo.mark_manual_recovery("op-manual")
                self.assertEqual(manual["state"], "failed")
                self.assertEqual(
                    manual["last_error"],
                    NEEDS_MANUAL_RECOVERY,
                )

                with self.assertRaises(InvalidRepositoryValue):
                    repo.transition_operation(
                        "op-manual",
                        "needs_manual_recovery",
                    )

                self.assertTrue(repo.delete_operation("op-create"))
                self.assertIsNone(repo.get_operation("op-create"))

    def test_remote_applied_can_finish_without_mapping_saved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            with ensure_database(db_path) as connection:
                repo = StateRepository(connection)
                repo.create_operation(
                    operation_id="op-update",
                    direction="google_to_timetree",
                    action="update",
                )
                repo.transition_operation("op-update", "remote_applied")
                done = repo.transition_operation("op-update", "done")
                self.assertEqual(done["state"], "done")

    def test_run_lock_acquire_release_and_live_owner_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "state" / "bridge.lock"

            owner = acquire_lock(
                lock_path,
                process_checker=lambda pid: pid == os.getpid(),
            )
            self.assertTrue(lock_path.exists())
            self.assertEqual(read_lock(lock_path)["pid"], os.getpid())

            with self.assertRaises(RunLockHeldError):
                acquire_lock(
                    lock_path,
                    process_checker=lambda pid: pid == os.getpid(),
                )

            release_lock(
                lock_path,
                expected_pid=owner["pid"],
                expected_started_at=owner["started_at"],
            )
            self.assertFalse(lock_path.exists())

    def test_run_lock_recovers_dead_pid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "bridge.lock"
            lock_path.write_text(
                json.dumps(
                    {
                        "pid": 424242,
                        "started_at": "2026-01-01T00:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )

            before = inspect_lock(
                lock_path,
                process_checker=lambda pid: False,
            )
            self.assertTrue(before["stale"])

            owner = acquire_lock(
                lock_path,
                process_checker=lambda pid: False,
            )
            self.assertEqual(owner["pid"], os.getpid())
            self.assertEqual(read_lock(lock_path)["pid"], os.getpid())

            release_lock(
                lock_path,
                expected_pid=owner["pid"],
                expected_started_at=owner["started_at"],
            )

    def test_run_lock_context_manager_cleans_up_on_exception(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "bridge.lock"

            with self.assertRaisesRegex(RuntimeError, "boom"), run_lock(
                lock_path,
                process_checker=lambda pid: pid == os.getpid(),
            ):
                self.assertTrue(lock_path.exists())
                raise RuntimeError("boom")

            self.assertFalse(lock_path.exists())


if __name__ == "__main__":
    unittest.main()
