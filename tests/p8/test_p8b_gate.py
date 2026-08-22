from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bridge.adapters import (
    EventEligibilityError,
    classify_timetree_event,
    normalize_timetree_event,
)
from bridge.bootstrap import deterministic_google_event_id
from bridge.canonical import event_hash
from bridge.cli import (
    build_parser,
    main,
    run_bootstrap_dry_run,
    run_bootstrap_live,
    run_doctor,
)
from bridge.db import ensure_database
from bridge.lock import RunLockHeldError, default_lock_path, run_lock
from bridge.models import SYNC_TIMETREE_LABEL_NAMES, TimeTreeLabelCatalog
from bridge.p8_gate import run_read_only_bootstrap_gate
from bridge.repository import StateRepository
from bridge.timetree_client import TimeTreeWriteResult
from tests.p8.test_bootstrap import (
    FakeGoogle,
    FakeTimeTree,
    google_raw,
    read_fixture,
    test_uuid,
    tt_event,
    tt_normalized,
)


def write_config(root: Path) -> Path:
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "bridge.toml"
    path.write_text(
        """[bridge]
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
""",
        encoding="utf-8",
    )
    return path


class LabelMissingTimeTree(FakeTimeTree):
    async def get_calendar_labels(self) -> TimeTreeLabelCatalog:
        self.calls.append("get_calendar_labels")
        return TimeTreeLabelCatalog.from_mapping({10: "not-a-sync-label"})


RUNTIME_SYNC_NAMES = tuple(sorted(SYNC_TIMETREE_LABEL_NAMES))
RUNTIME_LABELS = TimeTreeLabelCatalog.from_mapping(
    {
        101: RUNTIME_SYNC_NAMES[0],
        202: RUNTIME_SYNC_NAMES[1],
        303: None,
    }
)


class RuntimeLabelTimeTree(FakeTimeTree):
    async def get_calendar_labels(self) -> TimeTreeLabelCatalog:
        self.calls.append("get_calendar_labels")
        return RUNTIME_LABELS


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
        self.assertEqual(
            counts, {"event_links": 0, "sync_operations": 0, "conflicts": 0}
        )
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
                self.assertIn(
                    "GOOGLE_WRITER_PERMISSION_REQUIRED", result["gate"]["reasons"]
                )
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

    async def test_classification_happens_before_label_and_exception_scope(
        self,
    ) -> None:
        birthday = read_fixture("timetree_birthday.json")
        birthday.pop("label_id", None)
        birthday["parent_id"] = "master"
        birthday["recurrences"] = ["EXDATE:20300101T000000Z"]

        memo = read_fixture("timetree_memo.json")
        memo.pop("label_id", None)
        memo["recurring_uuid"] = "master"

        out_of_scope = tt_event(label_id=99)
        out_of_scope["recurrences"] = ["EXDATE:20300101T000000Z"]

        result, _, _, _, _ = await self.gate(
            timetree=WriteTrapTimeTree([birthday, memo, out_of_scope]),
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["timetree"]["eligible_count"], 0)
        self.assertEqual(result["timetree"]["ignored_count"], 3)
        self.assertEqual(
            result["timetree"]["ignored_reasons"],
            {
                "TIMETREE_BIRTHDAY": 1,
                "TIMETREE_MEMO": 1,
                "LABEL_OUT_OF_SCOPE": 1,
            },
        )
        self.assertEqual(result["timetree"]["unsupported_count"], 0)
        self.assertEqual(result["timetree"]["exception_evidence_count"], 0)

    async def test_unknown_type_is_not_replaced_by_label_failure(self) -> None:
        source = tt_event()
        source["category"] = 999
        source["type"] = 999
        source.pop("label_id", None)
        result, _, _, _, _ = await self.gate(timetree=FakeTimeTree([source]))
        self.assertFalse(result["ok"])
        self.assertIn(
            "TIMETREE_CATEGORY_999_TYPE_999",
            result["timetree"]["unsupported_reasons"],
        )
        self.assertNotIn("TIMETREE_LABEL_MISSING", result["gate"]["reasons"])

    async def test_normal_event_missing_label_is_unsupported(self) -> None:
        source = tt_event()
        source.pop("label_id", None)
        result, _, _, _, _ = await self.gate(timetree=FakeTimeTree([source]))
        self.assertFalse(result["ok"])
        self.assertIn(
            "TIMETREE_LABEL_MISSING",
            result["timetree"]["unsupported_reasons"],
        )

    async def test_existing_unnamed_out_of_scope_label_is_ignored(self) -> None:
        source = tt_event(label_id=303)
        result, _, _, _, _ = await self.gate(
            timetree=RuntimeLabelTimeTree([source]),
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["timetree"]["eligible_count"], 0)
        self.assertEqual(result["timetree"]["ignored_count"], 1)
        self.assertEqual(
            result["timetree"]["ignored_reasons"],
            {
                "TIMETREE_BIRTHDAY": 0,
                "TIMETREE_MEMO": 0,
                "LABEL_OUT_OF_SCOPE": 1,
            },
        )
        self.assertEqual(result["timetree"]["unsupported_count"], 0)
        self.assertEqual(result["timetree"]["exception_evidence_count"], 0)
        self.assertEqual(
            result["timetree"]["label_counts"][
                "unnamed_out_of_scope_label_event_count"
            ],
            1,
        )

        eligibility = classify_timetree_event(
            source,
            label_catalog=RUNTIME_LABELS,
        )
        self.assertEqual(eligibility.code, "LABEL_OUT_OF_SCOPE")
        with self.assertRaises(EventEligibilityError):
            normalize_timetree_event(
                source,
                default_timezone="Asia/Tokyo",
                label_catalog=RUNTIME_LABELS,
            )

    async def test_runtime_sync_label_ids_normalize_to_exact_names(self) -> None:
        for label_id, expected_name in zip((101, 202), RUNTIME_SYNC_NAMES):
            with self.subTest(label_id=label_id):
                source = tt_event(label_id=label_id)
                result, _, _, _, _ = await self.gate(
                    timetree=RuntimeLabelTimeTree([source]),
                )
                self.assertTrue(result["ok"])
                normalized = normalize_timetree_event(
                    source,
                    default_timezone="Asia/Tokyo",
                    label_catalog=RUNTIME_LABELS,
                )
                self.assertEqual(normalized.label, expected_name)

    def test_unknown_and_missing_label_ids_remain_unsupported(self) -> None:
        unknown = tt_event(label_id=404)
        missing = tt_event()
        missing.pop("label_id", None)
        for source, code in (
            (unknown, "TIMETREE_LABEL_UNKNOWN_ID"),
            (missing, "TIMETREE_LABEL_MISSING"),
        ):
            with self.subTest(code=code):
                eligibility = classify_timetree_event(
                    source,
                    label_catalog=RUNTIME_LABELS,
                )
                self.assertEqual(eligibility.code, code)

    def test_non_integer_label_id_never_aliases_existing_numeric_id(self) -> None:
        alias_catalog = TimeTreeLabelCatalog.from_mapping(
            {
                1: None,
                101: RUNTIME_SYNC_NAMES[0],
                202: RUNTIME_SYNC_NAMES[1],
            }
        )
        for invalid_label_id in (True, 1.0):
            with self.subTest(label_id=invalid_label_id):
                source = tt_event()
                source["label_id"] = invalid_label_id
                eligibility = classify_timetree_event(
                    source,
                    label_catalog=alias_catalog,
                )
                self.assertEqual(
                    eligibility.code,
                    "TIMETREE_LABEL_UNKNOWN_ID",
                )

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

    async def test_exception_evidence_blocks_only_in_scope_normal_events(self) -> None:
        for field, value in (
            ("parent_id", "master"),
            ("recurring_uuid", "master"),
            ("recurrences", ["EXDATE:20300101T000000Z"]),
        ):
            with self.subTest(field=field):
                source = tt_event()
                source[field] = value
                result, _, _, _, _ = await self.gate(
                    timetree=WriteTrapTimeTree([source]),
                )
                self.assertFalse(result["ok"])
                self.assertEqual(
                    result["timetree"]["exception_evidence_count"],
                    1,
                )
                self.assertIn(
                    "UNSUPPORTED_RECURRENCE_EXCEPTION",
                    result["gate"]["reasons"],
                )

    async def test_unsupported_recurrence_fails(self) -> None:
        source = tt_event()
        source["recurrences"] = ["RDATE:20300101T010000Z"]
        source["title"] = "PRIVATE_DIAGNOSTIC_TITLE"
        source["uuid"] = "private-diagnostic-uuid"
        result, _, google, counts, _ = await self.gate(timetree=FakeTimeTree([source]))
        self.assertFalse(result["ok"])
        self.assertIn("UNSUPPORTED_RECURRENCE_FEATURE", result["gate"]["reasons"])
        diagnostics = result["recurrence_diagnostics"]
        self.assertEqual(diagnostics["unsupported_count"], 1)
        self.assertEqual(len(diagnostics["shapes"]), 1)
        shape = diagnostics["shapes"][0]
        self.assertEqual(shape["count"], 1)
        self.assertEqual(shape["event_kind"], "series")
        self.assertFalse(shape["all_day"])
        self.assertTrue(shape["start_timezone_present"])
        self.assertTrue(shape["end_timezone_present"])
        self.assertEqual(shape["effective_timezone_relation"], "same")
        self.assertEqual(shape["property_names"], ["RDATE"])
        self.assertEqual(shape["recurrence_lines"], ["RDATE:20300101T010000Z"])
        self.assertEqual(shape["reason_code"], "UNSUPPORTED_RECURRENCE_FEATURE")
        self.assertTrue(shape["reason"])
        rendered = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("PRIVATE_DIAGNOSTIC_TITLE", rendered)
        self.assertNotIn("private-diagnostic-uuid", rendered)
        self.assertEqual(google.insert_calls, [])
        self.assertEqual(counts["event_links"], 0)
        self.assertEqual(counts["sync_operations"], 0)

    async def test_supported_recurrence_has_no_diagnostic(self) -> None:
        source = tt_event()
        source["recurrences"] = ["RRULE:FREQ=WEEKLY;COUNT=2"]
        result, _, google, counts, _ = await self.gate(
            timetree=WriteTrapTimeTree([source]),
        )
        self.assertTrue(result["ok"])
        self.assertEqual(
            result["recurrence_diagnostics"],
            {
                "unsupported_count": 0,
                "shapes": [],
            },
        )
        self.assertEqual(google.insert_calls, [])
        self.assertEqual(counts["event_links"], 0)
        self.assertEqual(counts["sync_operations"], 0)

    async def test_all_day_exact_yearly_is_supported_bootstrap_candidate(self) -> None:
        source = read_fixture("timetree_birthday.json")
        source.update(
            {
                "uuid": test_uuid("p8-yearly-candidate"),
                "id": test_uuid("p8-yearly-candidate"),
                "category": 1,
                "type": 0,
                "label_id": 10,
                "title": "Fixture yearly candidate",
            }
        )
        result, _, google, counts, _ = await self.gate(
            timetree=WriteTrapTimeTree([source]),
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["timetree"]["raw_event_count"], 1)
        self.assertEqual(result["timetree"]["eligible_count"], 1)
        self.assertEqual(result["timetree"]["unsupported_count"], 0)
        self.assertEqual(
            result["recurrence_diagnostics"],
            {
                "unsupported_count": 0,
                "shapes": [],
            },
        )
        self.assertEqual(google.insert_calls, [])
        self.assertEqual(counts["event_links"], 0)
        self.assertEqual(counts["sync_operations"], 0)

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

    def _seed_bootstrap_operation(
        self,
        source: dict[str, object],
        *,
        db_path: Path | None = None,
        operation_id: str | None = None,
        state: str = "prepared",
        target_event_id: str | None = None,
    ) -> str:
        source_id = str(source["uuid"])
        operation_id = operation_id or (
            "bootstrap:timetree_to_google:create:" + source_id
        )
        with ensure_database(db_path or self.db_path) as connection:
            repository = StateRepository(connection)
            repository.create_operation(
                operation_id=operation_id,
                direction="timetree_to_google",
                action="create",
                source_event_id=source_id,
                source_hash=event_hash(tt_normalized(source)),
            )
            if state == "failed":
                repository.mark_manual_recovery(operation_id)
            elif state in {"remote_applied", "mapping_saved", "done"}:
                repository.transition_operation(
                    operation_id,
                    "remote_applied",
                    target_event_id=target_event_id,
                )
                if state in {"mapping_saved", "done"}:
                    repository.transition_operation(operation_id, "mapping_saved")
                if state == "done":
                    repository.transition_operation(operation_id, "done")
        return operation_id

    def test_live_bootstrap_recovers_prepared_operation_without_duplicate_create(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = write_config(Path(tmp))
            source = tt_event()
            deterministic_id = deterministic_google_event_id(source["uuid"])
            google = FakeGoogle(
                [
                    google_raw(
                        event_id=deterministic_id,
                        timetree_id=source["uuid"],
                        label=tt_normalized(source).label,
                    )
                ]
            )
            self._seed_bootstrap_operation(
                source,
                db_path=Path(tmp) / "state" / "test.db",
            )

            result = run_bootstrap_live(
                config,
                google_client=google,
                timetree_client=FakeTimeTree([source]),
            )

            self.assertTrue(result["ok"])
            self.assertEqual(result["bootstrap_mode"], "recovery")
            self.assertEqual(result["bootstrap"]["created_event_count"], 0)
            self.assertEqual(result["bootstrap"]["recovered_event_count"], 1)
            self.assertEqual(google.insert_calls, [])

    def test_live_bootstrap_recovery_404_creates_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = write_config(Path(tmp))
            source = tt_event()
            self._seed_bootstrap_operation(
                source,
                db_path=Path(tmp) / "state" / "test.db",
            )
            google = FakeGoogle()

            result = run_bootstrap_live(
                config,
                google_client=google,
                timetree_client=FakeTimeTree([source]),
            )

            self.assertTrue(result["ok"])
            self.assertEqual(result["bootstrap_mode"], "recovery")
            self.assertEqual(result["bootstrap"]["created_event_count"], 1)
            self.assertEqual(result["bootstrap"]["recovered_event_count"], 0)
            self.assertEqual(len(google.insert_calls), 1)
            self.assertEqual(
                google.insert_calls[0]["id"],
                deterministic_google_event_id(source["uuid"]),
            )

    def test_live_bootstrap_already_bootstrapped_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = write_config(root)
            db_path = root / "state" / "test.db"
            with ensure_database(db_path) as connection:
                StateRepository(connection).set_sync_state(
                    "bridge_bootstrapped_at",
                    "2040-01-01T00:00:00+00:00",
                )
            google = FakeGoogle()
            timetree = FakeTimeTree([tt_event()])

            unavailable = SimpleNamespace(
                google_service_account_file=None,
                timetree_email=None,
                timetree_password=None,
            )
            with patch("bridge.cli.load_secrets", return_value=unavailable) as secrets:
                result = run_bootstrap_live(
                    config,
                    google_client=google,
                    timetree_client=timetree,
                )

            self.assertTrue(result["ok"])
            self.assertEqual(result["bootstrap_mode"], "already_bootstrapped")
            self.assertEqual(result["bootstrap"]["status"], "already_bootstrapped")
            self.assertEqual(result["remote_writes"], 0)
            self.assertEqual(google.insert_calls, [])
            self.assertEqual(google.list_calls, 0)
            self.assertEqual(timetree.calls, [])
            secrets.assert_not_called()

    def test_live_recovery_applied_states_require_target_event_id(self) -> None:
        for state in ("remote_applied", "mapping_saved", "done"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = write_config(root)
                db_path = root / "state" / "test.db"
                source = tt_event()
                normalized = tt_normalized(source)
                self._seed_bootstrap_operation(
                    source,
                    db_path=db_path,
                    state=state,
                    target_event_id=None,
                )
                if state in {"mapping_saved", "done"}:
                    with ensure_database(db_path) as connection:
                        StateRepository(connection).create_event_link(
                            timetree_event_id=source["uuid"],
                            google_event_id=deterministic_google_event_id(
                                source["uuid"]
                            ),
                            event_kind=normalized.kind.value,
                            last_synced_hash=event_hash(normalized),
                        )
                google = FakeGoogle()

                result = run_bootstrap_live(
                    config,
                    google_client=google,
                    timetree_client=FakeTimeTree([source]),
                )

                self.assertFalse(result["ok"])
                self.assertIn(
                    "DB_RECOVERY_TARGET_MAPPING_REQUIRED",
                    result["recovery"]["reasons"],
                )
                self.assertFalse(result["recovery"]["authorized"])
                self.assertEqual(google.insert_calls, [])

    def test_live_recovery_rejects_event_kind_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = write_config(root)
            db_path = root / "state" / "test.db"
            source = tt_event()
            normalized = tt_normalized(source)
            self._seed_bootstrap_operation(source, db_path=db_path)
            with ensure_database(db_path) as connection:
                StateRepository(connection).create_event_link(
                    timetree_event_id=source["uuid"],
                    google_event_id=deterministic_google_event_id(source["uuid"]),
                    event_kind="series",
                    last_synced_hash=event_hash(normalized),
                )
            google = FakeGoogle()

            result = run_bootstrap_live(
                config,
                google_client=google,
                timetree_client=FakeTimeTree([source]),
            )

            self.assertFalse(result["ok"])
            self.assertIn(
                "DB_RECOVERY_EVENT_LINK_MAPPING_MISMATCH",
                result["recovery"]["reasons"],
            )
            self.assertFalse(result["recovery"]["authorized"])
            self.assertEqual(google.insert_calls, [])

    def test_live_bootstrap_failed_operation_requires_manual_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = write_config(Path(tmp))
            source = tt_event()
            self._seed_bootstrap_operation(
                source,
                db_path=Path(tmp) / "state" / "test.db",
                state="failed",
            )
            google = FakeGoogle()

            result = run_bootstrap_live(
                config,
                google_client=google,
                timetree_client=FakeTimeTree([source]),
            )

            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "BOOTSTRAP_RECOVERY_MANUAL_REQUIRED")
            self.assertIn(
                "DB_FAILED_OPERATIONS_PRESENT",
                result["gate"]["reasons"],
            )
            self.assertIn(
                "DB_MANUAL_RECOVERY_REQUIRED",
                result["gate"]["reasons"],
            )
            self.assertIn("NEEDS_MANUAL_RECOVERY", result["recovery"]["reasons"])
            self.assertEqual(google.insert_calls, [])
            self.assertEqual(result["database"]["pending_operation_count"], 0)
            self.assertEqual(result["database"]["failed_operation_count"], 1)
            self.assertFalse(result["database"]["ready"])

    def test_live_bootstrap_foreign_operation_blocks_before_insert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = write_config(Path(tmp))
            source = tt_event()
            self._seed_bootstrap_operation(
                source,
                db_path=Path(tmp) / "state" / "test.db",
                operation_id="foreign-operation",
            )
            google = FakeGoogle()

            result = run_bootstrap_live(
                config,
                google_client=google,
                timetree_client=FakeTimeTree([source]),
            )

            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "LIVE_BOOTSTRAP_GATE_FAILED")
            self.assertIn(
                "DB_RECOVERY_FOREIGN_OPERATION",
                result["recovery"]["reasons"],
            )
            self.assertEqual(google.insert_calls, [])

    def test_live_bootstrap_orphan_operation_blocks_before_insert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = write_config(Path(tmp))
            orphan = tt_event(event_id="orphan")
            current = tt_event(event_id="current")
            self._seed_bootstrap_operation(
                orphan,
                db_path=Path(tmp) / "state" / "test.db",
            )
            google = FakeGoogle()

            result = run_bootstrap_live(
                config,
                google_client=google,
                timetree_client=FakeTimeTree([current]),
            )

            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "LIVE_BOOTSTRAP_GATE_FAILED")
            self.assertIn(
                "DB_RECOVERY_SOURCE_NOT_ELIGIBLE",
                result["recovery"]["reasons"],
            )
            self.assertEqual(google.insert_calls, [])

    def test_bootstrap_parser_and_live_dispatch(self) -> None:
        args = build_parser().parse_args(["bootstrap", "--dry-run", "--json"])
        self.assertEqual(args.command, "bootstrap")
        self.assertTrue(args.dry_run)

        with tempfile.TemporaryDirectory() as tmp:
            config = write_config(Path(tmp))
            output = io.StringIO()
            expected = {
                "ok": True,
                "command": "bootstrap",
                "dry_run": False,
                "remote_writes": 1,
            }
            with (
                patch("bridge.cli.run_bootstrap_live", return_value=expected) as live,
                contextlib.redirect_stdout(output),
            ):
                code = main(["bootstrap", "--config", str(config), "--json"])
            result = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertTrue(result["ok"])
            self.assertFalse(result["dry_run"])
            self.assertEqual(result["remote_writes"], 1)
            live.assert_called_once()

    def test_live_bootstrap_with_fakes_rechecks_gate_and_persists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = write_config(Path(tmp))
            google = FakeGoogle()
            timetree = FakeTimeTree([tt_event()])

            result = run_bootstrap_live(
                config,
                google_client=google,
                timetree_client=timetree,
            )

            self.assertTrue(result["ok"])
            self.assertFalse(result["dry_run"])
            self.assertEqual(result["remote_writes"], 1)
            self.assertTrue(result["gate"]["ready_for_live_bootstrap"])
            self.assertEqual(result["bootstrap"]["status"], "bootstrapped")
            self.assertEqual(result["bootstrap"]["created_event_count"], 1)
            self.assertEqual(len(google.insert_calls), 1)

            # The read-only gate runs first, then BootstrapRunner performs
            # its own authoritative preflight before any write.
            self.assertEqual(timetree.calls.count("get_events"), 2)

            db_path = Path(tmp) / "state" / "test.db"
            with ensure_database(db_path) as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM event_links").fetchone()[
                        0
                    ],
                    1,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM sync_operations"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM sync_state").fetchone()[0],
                    3,
                )

            self.assertFalse(default_lock_path(Path(tmp)).exists())

    def test_live_bootstrap_gate_failure_never_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = write_config(Path(tmp))
            google = FakeGoogle([google_raw(event_id="unmanaged")])

            result = run_bootstrap_live(
                config,
                google_client=google,
                timetree_client=FakeTimeTree([tt_event()]),
            )

            self.assertFalse(result["ok"])
            self.assertFalse(result["dry_run"])
            self.assertEqual(result["remote_writes"], 0)
            self.assertEqual(result["error"], "LIVE_BOOTSTRAP_GATE_FAILED")
            self.assertIn(
                "UNMANAGED_GOOGLE_EVENT",
                result["gate"]["reasons"],
            )
            self.assertEqual(google.insert_calls, [])

            db_path = Path(tmp) / "state" / "test.db"
            with ensure_database(db_path) as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM event_links").fetchone()[
                        0
                    ],
                    0,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM sync_operations"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM sync_state").fetchone()[0],
                    0,
                )

    def test_live_bootstrap_run_lock_blocks_second_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = write_config(root)
            lock_path = default_lock_path(root)

            with run_lock(lock_path), self.assertRaises(RunLockHeldError):
                run_bootstrap_live(
                    config,
                    google_client=FakeGoogle(),
                    timetree_client=FakeTimeTree([tt_event()]),
                )

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
                    connection.execute("SELECT COUNT(*) FROM event_links").fetchone()[
                        0
                    ],
                    0,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM sync_operations"
                    ).fetchone()[0],
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
                dry_run=True,
                external=True,
                google_client=FakeGoogle(access_role="owner"),
                timetree_client=FakeTimeTree([]),
            )
            self.assertTrue(doctor["ok"])
            self.assertTrue(doctor["external_services_checked"])
            self.assertTrue(doctor["external"]["google"]["writer_permission"])
            self.assertTrue(doctor["external"]["timetree"]["labels_resolved"])

    def test_missing_required_credentials_fail_doctor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = write_config(Path(tmp))
            with patch.dict(os.environ, {}, clear=True):
                doctor = run_doctor(config, dry_run=True)
            self.assertFalse(doctor["ok"])
            self.assertFalse(doctor["required_checks"]["timetree_credentials"])
            self.assertFalse(doctor["required_checks"]["google_credentials"])


if __name__ == "__main__":
    unittest.main()
