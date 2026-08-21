from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from bridge.cli import build_parser, main, run_bootstrap_dry_run, run_doctor
from bridge.db import ensure_database
from bridge.models import TimeTreeLabelCatalog
from bridge.p8_gate import run_read_only_bootstrap_gate
from bridge.repository import StateRepository
from bridge.timetree_client import TimeTreeWriteResult
from tests.p8.test_bootstrap import (
    FakeGoogle,
    FakeTimeTree,
    google_raw,
    tt_event,
)


def write_config(root: Path) -> Path:
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "bridge.toml"
    path.write_text(
        '''[bridge]
version = "p8b-test"
default_timezone = "Asia/Tokyo"

[timetree]
calendar_id = "101"
incremental_interval_seconds = 300
overlap_seconds = 30

[google]
calendar_id = "google-calendar"
incremental_interval_seconds = 60

[reconcile]
interval_seconds = 3600

[verify]
interval_seconds = 86400

[exporter]
calendar_code = "fixture-calendar"

[state]
database = "state/test.db"

[logging]
path = "logs/test.jsonl"
''',
        encoding="utf-8",
    )
    return path


class LabelMissingTimeTree(FakeTimeTree):
    async def get_calendar_labels(self) -> TimeTreeLabelCatalog:
        self.calls.append("get_calendar_labels")
        return TimeTreeLabelCatalog.from_mapping({10: "not-a-sync-label"})


class WriteTrapTimeTree(FakeTimeTree):
    async def create_event(self, *args, **kwargs) -> TimeTreeWriteResult:
        raise AssertionError("dry-run called TimeTree create_event")

    async def update_event(self, *args, **kwargs) -> TimeTreeWriteResult:
        raise AssertionError("dry-run called TimeTree update_event")

    async def delete_event(self, *args, **kwargs) -> TimeTreeWriteResult:
        raise AssertionError("dry-run called TimeTree delete_event")


class P8BGateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "state.db"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def gate(
        self,
        *,
        timetree=None,
        google=None,
    ):
        source = tt_event()
        timetree = timetree or FakeTimeTree([source])
        google = google or FakeGoogle()
        with ensure_database(self.db_path) as connection:
            result = await run_read_only_bootstrap_gate(
                google_client=google,
                timetree_client=timetree,
                repository=StateRepository(connection),
                default_timezone="Asia/Tokyo",
                bridge_version="p8b-test",
            )
            counts = {
                table: int(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                )
                for table in (
                    "event_links",
                    "sync_operations",
                    "conflicts",
                )
            }
            state = connection.execute("SELECT COUNT(*) FROM sync_state").fetchone()[0]
        return result, timetree, google, counts, state

    async def test_clean_snapshot_is_ready_and_never_writes(self) -> None:
        result, timetree, google, counts, state = await self.gate(
            timetree=WriteTrapTimeTree([tt_event()]),
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["remote_writes"], 0)
        self.assertTrue(result["gate"]["ready_for_live_bootstrap"])
        self.assertEqual(result["google"]["access_role"], "writer")
        self.assertEqual(result["timetree"]["eligible_count"], 1)
        self.assertEqual(google.insert_calls, [])
        self.assertEqual(counts, {"event_links": 0, "sync_operations": 0, "conflicts": 0})
        self.assertEqual(state, 0)
        self.assertEqual(timetree.calls.count("get_events"), 1)

    async def test_access_roles_owner_and_writer_pass(self) -> None:
        for access_role in ("owner", "writer"):
            with self.subTest(access_role=access_role):
                result, _, _, _, _ = await self.gate(
                    google=FakeGoogle(access_role=access_role),
                )
                self.assertTrue(result["ok"])
                self.assertEqual(result["google"]["access_role"], access_role)

    async def test_non_writer_access_roles_fail(self) -> None:
        for access_role in ("reader", "freeBusyReader", "writerWithoutPrivateAccess"):
            with self.subTest(access_role=access_role):
                result, _, google, _, _ = await self.gate(
                    google=FakeGoogle(access_role=access_role),
                )
                self.assertFalse(result["ok"])
                self.assertIn("GOOGLE_WRITER_PERMISSION_REQUIRED", result["gate"]["reasons"])
                self.assertEqual(google.insert_calls, [])

    async def test_unmanaged_google_event_fails(self) -> None:
        result, _, google, _, _ = await self.gate(
            google=FakeGoogle([google_raw(event_id="unmanaged")]),
        )
        self.assertFalse(result["ok"])
        self.assertIn("UNMANAGED_GOOGLE_EVENT", result["gate"]["reasons"])
        self.assertEqual(result["google"]["unmanaged_count"], 1)
        self.assertEqual(google.insert_calls, [])

    async def test_tombstone_only_google_snapshot_passes(self) -> None:
        result, _, _, _, _ = await self.gate(
            google=FakeGoogle(tombstones=[{"id": "deleted", "status": "cancelled"}]),
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["google"]["live_event_count"], 0)
        self.assertEqual(result["google"]["tombstone_count"], 1)

    async def test_missing_required_label_fails(self) -> None:
        result, _, _, _, _ = await self.gate(
            timetree=LabelMissingTimeTree([tt_event()]),
        )
        self.assertFalse(result["ok"])
        self.assertIn("TIMETREE_LABELS_UNRESOLVED", result["gate"]["reasons"])

    async def test_exception_evidence_fails_before_normalization(self) -> None:
        source = tt_event()
        source["parent_id"] = "master"
        result, _, google, _, _ = await self.gate(
            timetree=WriteTrapTimeTree([source]),
        )
        self.assertFalse(result["ok"])
        self.assertIn(
            "UNSUPPORTED_RECURRENCE_EXCEPTION",
            result["gate"]["reasons"],
        )
        self.assertEqual(result["timetree"]["exception_evidence_count"], 1)
        self.assertEqual(result["timetree"]["eligible_count"], 0)
        self.assertEqual(result["timetree"]["unsupported_count"], 1)
        self.assertEqual(
            result["timetree"]["unsupported_reasons"],
            {"UNSUPPORTED_RECURRENCE_EXCEPTION": 1},
        )
        self.assertEqual(google.insert_calls, [])

    async def test_unsupported_recurrence_fails(self) -> None:
        source = tt_event()
        source["recurrences"] = ["RDATE:20300101T010000Z"]
        result, _, _, _, _ = await self.gate(timetree=FakeTimeTree([source]))
        self.assertFalse(result["ok"])
        self.assertIn("UNSUPPORTED_RECURRENCE_FEATURE", result["gate"]["reasons"])

    async def test_existing_database_state_safe_stops(self) -> None:
        with ensure_database(self.db_path) as connection:
            StateRepository(connection).create_operation(
                operation_id="existing-operation",
                direction="timetree_to_google",
                action="create",
            )
        result, _, _, counts, _ = await self.gate()
        self.assertFalse(result["ok"])
        self.assertIn("DB_PENDING_OPERATIONS_PRESENT", result["gate"]["reasons"])
        self.assertEqual(counts["sync_operations"], 1)

    def test_bootstrap_parser_and_live_write_guard(self) -> None:
        args = build_parser().parse_args(["bootstrap", "--dry-run", "--json"])
        self.assertEqual(args.command, "bootstrap")
        self.assertTrue(args.dry_run)

        with tempfile.TemporaryDirectory() as tmp:
            config = write_config(Path(tmp))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["bootstrap", "--config", str(config), "--json"])
            result = json.loads(output.getvalue())
            self.assertEqual(code, 2)
            self.assertEqual(result["error"], "LIVE_BOOTSTRAP_WRITE_DISABLED_P8B")
            self.assertEqual(result["remote_writes"], 0)

    def test_cli_dry_run_with_fakes_does_not_persist_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = write_config(Path(tmp))
            result = run_bootstrap_dry_run(
                config,
                google_client=FakeGoogle(),
                timetree_client=WriteTrapTimeTree([tt_event()]),
            )
            self.assertTrue(result["ok"])
            self.assertEqual(result["remote_writes"], 0)
            db_path = Path(tmp) / "state" / "test.db"
            with ensure_database(db_path) as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM event_links").fetchone()[0],
                    0,
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM sync_operations").fetchone()[0],
                    0,
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM sync_state").fetchone()[0],
                    0,
                )

    def test_doctor_external_checks_with_read_only_fakes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = write_config(Path(tmp))
            doctor = run_doctor(
                config,
                external=True,
                google_client=FakeGoogle(access_role="owner"),
                timetree_client=FakeTimeTree([]),
            )
            self.assertTrue(doctor["ok"])
            self.assertTrue(doctor["external_services_checked"])
            self.assertTrue(doctor["external"]["google"]["writer_permission"])
            self.assertTrue(doctor["external"]["timetree"]["labels_resolved"])


if __name__ == "__main__":
    unittest.main()
