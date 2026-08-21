from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bridge.adapters import normalize_timetree_event
from bridge.bootstrap import (
    BootstrapError,
    BootstrapRunner,
    deterministic_google_event_id,
)
from bridge.canonical import event_hash
from bridge.db import ensure_database
from bridge.google_client import google_event_change
from bridge.models import GOOGLE_BRIDGE_SYNC_SOURCE, TimeTreeLabelCatalog
from bridge.repository import StateRepository
from bridge.timetree_client import TimeTreeCalendar

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "fixtures"
LABELS = TimeTreeLabelCatalog.from_mapping(
    {10: "大河予定", 20: "共通予定", 99: "対象外"}
)


def read_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_uuid(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def tt_event(*, label_id: int = 10, event_id: str = "tt-1") -> dict[str, Any]:
    raw = read_fixture("timetree_single.json")
    source_id = event_id if len(event_id) == 32 else test_uuid(event_id)
    raw["uuid"] = source_id
    raw["id"] = source_id
    raw["label_id"] = label_id
    raw["title"] = f"Fixture {event_id}"
    return raw


def tt_normalized(raw: dict[str, Any]):
    return normalize_timetree_event(
        raw,
        default_timezone="Asia/Tokyo",
        label_catalog=LABELS,
    )


def google_raw(
    *,
    event_id: str = "g-1",
    timetree_id: str | None = None,
    label: str | None = None,
    title: str = "Fixture tt-1",
) -> dict[str, Any]:
    private: dict[str, str] = {}
    if timetree_id is not None:
        private["sync_source"] = GOOGLE_BRIDGE_SYNC_SOURCE
        private["timetree_id"] = timetree_id
    if label is not None:
        private["timetree_label_name"] = label
    return {
        "id": event_id,
        "status": "confirmed",
        "eventType": "default",
        "summary": title,
        "description": "",
        "location": "",
        "start": {
            "dateTime": "2030-01-01T19:00:00+09:00",
            "timeZone": "Asia/Tokyo",
        },
        "end": {
            "dateTime": "2030-01-01T21:00:00+09:00",
            "timeZone": "Asia/Tokyo",
        },
        "extendedProperties": {"private": private},
    }


class FakeTimeTree:
    calendar_id = "101"

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = copy.deepcopy(events)
        self.calls: list[str] = []

    async def list_calendars(self) -> tuple[TimeTreeCalendar, ...]:
        self.calls.append("list_calendars")
        return (
            TimeTreeCalendar(
                calendar_id=self.calendar_id,
                name="Fixture Calendar",
                alias_code=None,
                users=(),
            ),
        )

    async def get_calendar_labels(self) -> TimeTreeLabelCatalog:
        self.calls.append("get_calendar_labels")
        return LABELS

    async def get_events(self) -> tuple[dict[str, Any], ...]:
        self.calls.append("get_events")
        return tuple(copy.deepcopy(self.events))


class FakeHttpError(RuntimeError):
    def __init__(self, status: int) -> None:
        self.resp = type("FakeResponse", (), {"status": status})()
        super().__init__(f"HTTP {status}")


class FakeGoogle:
    calendar_id = "google-calendar"

    def __init__(
        self,
        events: list[dict[str, Any]] | None = None,
        *,
        tombstones: list[dict[str, Any]] | None = None,
        response_mutator: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        final_mutator: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        conflict_on_insert: dict[str, Any] | None = None,
        get_error: BaseException | None = None,
        access_role: str = "writer",
    ) -> None:
        self.events = {
            raw["id"]: copy.deepcopy(raw) for raw in (events or [])
        }
        self.tombstones = copy.deepcopy(tombstones or [])
        self.response_mutator = response_mutator
        self.final_mutator = final_mutator
        self.conflict_on_insert = copy.deepcopy(conflict_on_insert)
        self.get_error = get_error
        self.access_role = access_role
        self.list_calls = 0
        self.insert_calls: list[dict[str, Any]] = []
        self.get_calls: list[str] = []
        self.find_calls: list[tuple[str, str]] = []
        self.next_id = 1

    def get_calendar_metadata(self) -> dict[str, str]:
        return {
            "id": self.calendar_id,
            "timeZone": "Asia/Tokyo",
            "accessRole": self.access_role,
        }

    def list_changes(self):
        self.list_calls += 1
        events = [copy.deepcopy(raw) for raw in self.events.values()]
        if self.list_calls > 1 and self.final_mutator is not None:
            events = [self.final_mutator(raw) for raw in events]
        raw_changes = [*events, *copy.deepcopy(self.tombstones)]
        changes = tuple(
            google_event_change(
                raw,
                source_calendar_id=self.calendar_id,
                default_timezone="Asia/Tokyo",
            )
            for raw in raw_changes
        )
        return type(
            "FakeSyncResult",
            (),
            {
                "changes": changes,
                "next_sync_token": f"token-{self.list_calls}",
                "access_role": self.access_role,
            },
        )()

    def insert_event(self, body: dict[str, Any]) -> dict[str, Any]:
        self.insert_calls.append(copy.deepcopy(body))
        event_id = str(body.get("id") or f"g-{self.next_id}")
        self.next_id += 1
        if self.conflict_on_insert is not None:
            conflict = copy.deepcopy(self.conflict_on_insert)
            self.events[conflict["id"]] = conflict
            self.conflict_on_insert = None
            raise FakeHttpError(409)
        stored = copy.deepcopy(body)
        stored.update({"id": event_id, "status": "confirmed", "eventType": "default"})
        self.events[event_id] = stored
        response = copy.deepcopy(stored)
        if self.response_mutator is not None:
            response = self.response_mutator(response)
        return response

    def get_event(self, event_id: str) -> dict[str, Any]:
        self.get_calls.append(event_id)
        if self.get_error is not None:
            raise self.get_error
        if event_id not in self.events:
            raise FakeHttpError(404)
        return copy.deepcopy(self.events[event_id])

    def find_events_by_private_property(
        self,
        property_name: str,
        value: str,
    ) -> tuple[dict[str, Any], ...]:
        self.find_calls.append((property_name, value))
        return tuple(
            copy.deepcopy(raw)
            for raw in self.events.values()
            if raw.get("extendedProperties", {})
            .get("private", {})
            .get(property_name)
            == value
        )


class BootstrapTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "state.db"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def run_bootstrap(
        self,
        events: list[dict[str, Any]],
        google: FakeGoogle | None = None,
        **kwargs: Any,
    ):
        timetree = FakeTimeTree(events)
        google = google or FakeGoogle()
        with ensure_database(self.db_path) as connection:
            result = await BootstrapRunner(
                timetree_client=timetree,
                google_client=google,
                repository=StateRepository(connection),
                default_timezone="Asia/Tokyo",
                bridge_version="p8-test",
                **kwargs,
            ).run()
            state = {
                key: StateRepository(connection).get_sync_state(key)
                for key in (
                    "google_sync_token",
                    "timetree_updated_after_ms",
                    "bridge_bootstrapped_at",
                )
            }
            links = connection.execute("SELECT * FROM event_links").fetchall()
            operations = connection.execute("SELECT * FROM sync_operations").fetchall()
        return result, google, state, links, operations

    async def assert_aborts_without_write(
        self,
        events: list[dict[str, Any]],
        *,
        code: str,
        google: FakeGoogle | None = None,
        expected_writes: int = 0,
    ) -> None:
        google = google or FakeGoogle()
        timetree = FakeTimeTree(events)
        with ensure_database(self.db_path) as connection:
            with self.assertRaises(BootstrapError) as caught:
                await BootstrapRunner(
                    timetree_client=timetree,
                    google_client=google,
                    repository=StateRepository(connection),
                    default_timezone="Asia/Tokyo",
                    bridge_version="p8-test",
                ).run()
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(len(google.insert_calls), expected_writes)
            self.assertIsNone(
                StateRepository(connection).get_sync_state("bridge_bootstrapped_at")
            )

    async def test_single_create_mapping_hash_and_state_commit(self) -> None:
        source = tt_event()
        result, google, state, links, operations = await self.run_bootstrap([source])

        self.assertEqual(result.status, "bootstrapped")
        self.assertEqual(result.created_event_count, 1)
        self.assertEqual(len(google.insert_calls), 1)
        self.assertEqual(len(links), 1)
        self.assertEqual(dict(links[0])["status"], "synced")
        self.assertEqual(
            dict(links[0])["last_synced_hash"],
            event_hash(tt_normalized(source)),
        )
        self.assertEqual(dict(operations[0])["state"], "done")
        self.assertEqual(state["google_sync_token"], "token-2")
        self.assertIsNotNone(state["bridge_bootstrapped_at"])

    async def test_supported_weekly_series_creates(self) -> None:
        source = tt_event()
        source["recurrences"] = ["RRULE:FREQ=WEEKLY;COUNT=3;BYDAY=TU"]
        result, google, _, _, _ = await self.run_bootstrap([source])
        self.assertEqual(result.created_event_count, 1)
        self.assertEqual(
            google.insert_calls[0]["recurrence"],
            ["RRULE:BYDAY=TU;COUNT=3;FREQ=WEEKLY"],
        )

    async def test_known_ignored_events_are_skipped_without_remote_write(self) -> None:
        memo = tt_event(label_id=10, event_id="memo")
        memo["category"] = 2
        birthday = tt_event(label_id=10, event_id="birthday")
        birthday["type"] = 1
        out_of_scope = tt_event(label_id=99, event_id="out-of-scope")
        result, google, _, links, _ = await self.run_bootstrap(
            [memo, birthday, out_of_scope]
        )
        self.assertEqual(result.eligible_event_count, 0)
        self.assertEqual(google.insert_calls, [])
        self.assertEqual(links, [])

    async def test_unknown_classification_aborts_before_write(self) -> None:
        source = tt_event()
        source["type"] = 999
        await self.assert_aborts_without_write(
            [source],
            code="TIMETREE_CATEGORY_1_TYPE_999",
        )

    async def test_parent_id_and_recurring_uuid_are_safe_stops(self) -> None:
        for field, value in (("parent_id", "master"), ("recurring_uuid", "master")):
            source = tt_event(event_id=f"child-{field}")
            source[field] = value
            with self.subTest(field=field):
                await self.assert_aborts_without_write(
                    [source],
                    code="UNSUPPORTED_RECURRENCE_EXCEPTION",
                )

    async def test_master_exdate_is_safe_stop_before_p6_normalization(self) -> None:
        source = tt_event()
        source["recurrences"] = [
            "RRULE:FREQ=WEEKLY;COUNT=3;BYDAY=TU",
            "EXDATE:20300101T010000Z",
        ]
        await self.assert_aborts_without_write(
            [source],
            code="UNSUPPORTED_RECURRENCE_EXCEPTION",
        )

    async def test_unsupported_recurrence_aborts_before_write(self) -> None:
        source = tt_event()
        source["recurrences"] = ["RDATE:20300101T010000Z"]
        await self.assert_aborts_without_write(
            [source],
            code="UNSUPPORTED_RECURRENCE_FEATURE",
        )

    async def test_unmanaged_google_live_event_aborts(self) -> None:
        google = FakeGoogle([google_raw(event_id="unmanaged")])
        await self.assert_aborts_without_write(
            [tt_event()],
            code="UNMANAGED_GOOGLE_EVENT",
            google=google,
        )

    async def test_delete_tombstone_does_not_block_empty_preflight(self) -> None:
        google = FakeGoogle(
            tombstones=[{"id": "deleted", "status": "cancelled"}]
        )
        result, google, _, _, _ = await self.run_bootstrap([tt_event()], google)
        self.assertEqual(result.created_event_count, 1)
        self.assertEqual(len(google.insert_calls), 1)

    async def test_remote_applied_operation_recovers_without_duplicate_create(self) -> None:
        source = tt_event()
        normalized = tt_normalized(source)
        deterministic_id = deterministic_google_event_id(source["uuid"])
        existing = google_raw(
            event_id=deterministic_id,
            timetree_id=source["uuid"],
            label=normalized.label,
        )
        google = FakeGoogle([existing])
        with ensure_database(self.db_path) as connection:
            repo = StateRepository(connection)
            repo.create_operation(
                operation_id=(
                    "bootstrap:timetree_to_google:create:" + source["uuid"]
                ),
                direction="timetree_to_google",
                action="create",
                source_event_id=source["uuid"],
                source_hash=event_hash(normalized),
            )
            repo.transition_operation(
                "bootstrap:timetree_to_google:create:" + source["uuid"],
                "remote_applied",
                target_event_id=deterministic_id,
            )
        result, google, state, links, operations = await self.run_bootstrap(
            [source], google
        )
        self.assertEqual(result.recovered_event_count, 1)
        self.assertEqual(google.insert_calls, [])
        self.assertEqual(dict(links[0])["google_event_id"], deterministic_id)
        self.assertEqual(dict(operations[0])["state"], "done")
        self.assertIsNotNone(state["bridge_bootstrapped_at"])

    async def test_prepared_operation_with_zero_recovery_matches_creates_once(self) -> None:
        source = tt_event()
        with ensure_database(self.db_path) as connection:
            StateRepository(connection).create_operation(
                operation_id=(
                    "bootstrap:timetree_to_google:create:" + source["uuid"]
                ),
                direction="timetree_to_google",
                action="create",
                source_event_id=source["uuid"],
                source_hash=event_hash(tt_normalized(source)),
            )
        result, google, _, _, operations = await self.run_bootstrap([source])
        self.assertEqual(result.created_event_count, 1)
        self.assertEqual(result.recovered_event_count, 0)
        self.assertEqual(len(google.insert_calls), 1)
        self.assertEqual(dict(operations[0])["state"], "done")

    async def test_same_timetree_uuid_has_same_google_event_id(self) -> None:
        first = tt_event(event_id="first")
        first_id = deterministic_google_event_id(first["uuid"])
        self.assertEqual(first_id, deterministic_google_event_id(first["uuid"]))

    async def test_different_timetree_uuid_has_different_google_event_id(self) -> None:
        first = tt_event(event_id="first")
        second = tt_event(event_id="second")
        first_id = deterministic_google_event_id(first["uuid"])
        self.assertNotEqual(first_id, deterministic_google_event_id(second["uuid"]))

    async def test_generated_google_event_id_meets_contract(self) -> None:
        first = tt_event(event_id="first")
        first_id = deterministic_google_event_id(first["uuid"])
        self.assertGreaterEqual(len(first_id), 5)
        self.assertLessEqual(len(first_id), 1024)
        self.assertRegex(first_id, r"^[a-v0-9]+$")

    async def test_invalid_timetree_uuid_fails_safe_before_write(self) -> None:
        source = tt_event()
        source["uuid"] = "not-a-confirmed-uuid"
        source["id"] = source["uuid"]
        await self.assert_aborts_without_write(
            [source],
            code="UNSAFE_TIMETREE_UUID",
        )

    async def test_prepared_operation_with_deterministic_event_recovers(self) -> None:
        source = tt_event()
        normalized = tt_normalized(source)
        deterministic_id = deterministic_google_event_id(source["uuid"])
        google = FakeGoogle(
            [
                google_raw(
                    event_id=deterministic_id,
                    timetree_id=source["uuid"],
                    label=normalized.label,
                )
            ]
        )
        operation_id = "bootstrap:timetree_to_google:create:" + source["uuid"]
        with ensure_database(self.db_path) as connection:
            StateRepository(connection).create_operation(
                operation_id=operation_id,
                direction="timetree_to_google",
                action="create",
                source_event_id=source["uuid"],
                source_hash=event_hash(normalized),
            )
        result, google, _, links, _ = await self.run_bootstrap([source], google)
        self.assertEqual(result.created_event_count, 0)
        self.assertEqual(result.recovered_event_count, 1)
        self.assertEqual(google.insert_calls, [])
        self.assertEqual(dict(links[0])["google_event_id"], deterministic_id)
        self.assertIn(deterministic_id, google.get_calls)

    async def test_prepared_operation_with_get_404_creates_once(self) -> None:
        source = tt_event()
        operation_id = "bootstrap:timetree_to_google:create:" + source["uuid"]
        with ensure_database(self.db_path) as connection:
            StateRepository(connection).create_operation(
                operation_id=operation_id,
                direction="timetree_to_google",
                action="create",
                source_event_id=source["uuid"],
                source_hash=event_hash(tt_normalized(source)),
            )
        result, google, _, _, _ = await self.run_bootstrap([source])
        self.assertEqual(result.created_event_count, 1)
        self.assertEqual(len(google.insert_calls), 1)
        self.assertEqual(
            google.insert_calls[0]["id"],
            deterministic_google_event_id(source["uuid"]),
        )

    async def test_prepared_operation_with_ambiguous_get_does_not_create(self) -> None:
        source = tt_event()
        operation_id = "bootstrap:timetree_to_google:create:" + source["uuid"]
        with ensure_database(self.db_path) as connection:
            StateRepository(connection).create_operation(
                operation_id=operation_id,
                direction="timetree_to_google",
                action="create",
                source_event_id=source["uuid"],
                source_hash=event_hash(tt_normalized(source)),
            )
        await self.assert_aborts_without_write(
            [source],
            code="RECOVERY_LOOKUP_FAILED",
            google=FakeGoogle(get_error=OSError("network timeout")),
        )

    async def test_insert_conflict_with_matching_event_recovers(self) -> None:
        source = tt_event()
        normalized = tt_normalized(source)
        deterministic_id = deterministic_google_event_id(source["uuid"])
        google = FakeGoogle(
            conflict_on_insert=google_raw(
                event_id=deterministic_id,
                timetree_id=source["uuid"],
                label=normalized.label,
            )
        )
        result, google, _, links, _ = await self.run_bootstrap([source], google)
        self.assertEqual(result.created_event_count, 0)
        self.assertEqual(result.recovered_event_count, 1)
        self.assertEqual(len(google.insert_calls), 1)
        self.assertEqual(dict(links[0])["google_event_id"], deterministic_id)

    async def test_insert_conflict_with_foreign_event_fails_safe(self) -> None:
        source = tt_event()
        foreign = tt_event(event_id="foreign")
        deterministic_id = deterministic_google_event_id(source["uuid"])
        google = FakeGoogle(
            conflict_on_insert=google_raw(
                event_id=deterministic_id,
                timetree_id=foreign["uuid"],
                label=tt_normalized(foreign).label,
            )
        )
        await self.assert_aborts_without_write(
            [source],
            code="GOOGLE_METADATA_MISMATCH",
            google=google,
            expected_writes=1,
        )

    async def test_recovery_duplicate_metadata_fails_safe(self) -> None:
        source = tt_event()
        normalized = tt_normalized(source)
        google = FakeGoogle(
            [
                google_raw(
                    event_id="duplicate-a",
                    timetree_id=source["uuid"],
                    label=normalized.label,
                ),
                google_raw(
                    event_id="duplicate-b",
                    timetree_id=source["uuid"],
                    label=normalized.label,
                ),
            ]
        )
        await self.assert_aborts_without_write(
            [source],
            code="DUPLICATE_GOOGLE_MANAGED_EVENT",
            google=google,
        )

    async def test_response_hash_mismatch_does_not_commit(self) -> None:
        google = FakeGoogle(
            response_mutator=lambda raw: {**raw, "summary": "wrong"}
        )
        await self.assert_aborts_without_write(
            [tt_event()],
            code="GOOGLE_TARGET_HASH_MISMATCH",
            google=google,
            expected_writes=1,
        )

    async def test_consistency_mismatch_does_not_commit_states(self) -> None:
        def mutate(raw: dict[str, Any]) -> dict[str, Any]:
            changed = copy.deepcopy(raw)
            changed["extendedProperties"]["private"]["timetree_label_name"] = "共通予定"
            return changed

        google = FakeGoogle(final_mutator=mutate)
        await self.assert_aborts_without_write(
            [tt_event(label_id=10)],
            code="BOOTSTRAP_CONSISTENCY_MISMATCH",
            google=google,
            expected_writes=1,
        )

    async def test_bootstrap_watermark_is_start_time_not_completion_time(self) -> None:
        source = tt_event()
        result, _, state, _, _ = await self.run_bootstrap(
            [source],
            clock_ms=lambda: 123456,
            now_utc=lambda: datetime(2040, 1, 1, tzinfo=UTC),
        )
        self.assertEqual(result.bootstrap_started_ms, 123456)
        self.assertEqual(state["timetree_updated_after_ms"], "123456")
        self.assertNotEqual(state["timetree_updated_after_ms"], "1262304000000")

    async def test_already_bootstrapped_is_idempotent_without_remote_calls(self) -> None:
        source = tt_event()
        timetree = FakeTimeTree([source])
        google = FakeGoogle()
        with ensure_database(self.db_path) as connection:
            repo = StateRepository(connection)
            repo.set_sync_state("bridge_bootstrapped_at", "2040-01-01T00:00:00+00:00")
            repo.set_sync_state("google_sync_token", "existing-token")
            result = await BootstrapRunner(
                timetree_client=timetree,
                google_client=google,
                repository=repo,
                default_timezone="Asia/Tokyo",
                bridge_version="p8-test",
            ).run()
        self.assertEqual(result.status, "already_bootstrapped")
        self.assertEqual(timetree.calls, [])
        self.assertEqual(google.list_calls, 0)
        self.assertEqual(google.insert_calls, [])

    async def test_both_labels_round_trip_to_metadata(self) -> None:
        for label_id, label_name in ((10, "大河予定"), (20, "共通予定")):
            with self.subTest(label=label_name):
                self.db_path = Path(self.tmp.name) / f"state-{label_id}.db"
                source = tt_event(label_id=label_id, event_id=f"tt-{label_id}")
                result, google, _, _, _ = await self.run_bootstrap([source])
                self.assertEqual(result.created_event_count, 1)
                private = google.insert_calls[0]["extendedProperties"]["private"]
                self.assertEqual(private["timetree_id"], source["uuid"])
                self.assertEqual(private["timetree_label_name"], label_name)

    async def test_missing_or_inconsistent_google_metadata_fails_consistency(self) -> None:
        cases = (
            lambda raw: {
                **raw,
                "extendedProperties": {"private": {
                    "sync_source": GOOGLE_BRIDGE_SYNC_SOURCE,
                    "timetree_label_name": "大河予定",
                }},
            },
            lambda raw: {
                **raw,
                "extendedProperties": {"private": {
                    "sync_source": GOOGLE_BRIDGE_SYNC_SOURCE,
                    "timetree_id": "wrong-id",
                    "timetree_label_name": "大河予定",
                }},
            },
        )
        for index, mutate in enumerate(cases):
            with self.subTest(mutate=mutate):
                self.db_path = Path(self.tmp.name) / f"state-metadata-{index}.db"
                await self.assert_aborts_without_write(
                    [tt_event()],
                    code=(
                        "BOOTSTRAP_CONSISTENCY_MISMATCH"
                    ),
                    expected_writes=1,
                    google=FakeGoogle(final_mutator=mutate),
                )


if __name__ == "__main__":
    unittest.main()
