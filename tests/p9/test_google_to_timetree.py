from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from bridge.adapters import normalize_google_event, normalize_timetree_event
from bridge.canonical import event_hash
from bridge.db import ensure_database
from bridge.google_client import google_event_change
from bridge.lock import RunLockHeldError, default_lock_path, run_lock
from bridge.models import GOOGLE_BRIDGE_SYNC_SOURCE, ChangeType, TimeTreeLabelCatalog
from bridge.repository import NEEDS_MANUAL_RECOVERY, StateRepository
from bridge.sync import GoogleToTimeTreeError, GoogleToTimeTreeRunner
from bridge.timetree_client import (
    TimeTreeCalendar,
    TimeTreeWriteResult,
    timetree_update_body,
)

LABELS = TimeTreeLabelCatalog.from_mapping(
    {10: "大河予定", 20: "共通予定", 99: "対象外"}
)


def timed_raw(
    *,
    event_id: str = "tt-1",
    title: str = "Original title",
    label_id: int = 10,
    description: str | None = None,
    location: str | None = None,
    recurrences: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": event_id,
        "uuid": event_id,
        "calendar_id": 101,
        "title": title,
        "all_day": False,
        "start_at": 1893492000000,
        "start_timezone": "Asia/Tokyo",
        "end_at": 1893499200000,
        "end_timezone": "Asia/Tokyo",
        "category": 1,
        "type": 0,
        "label_id": label_id,
        "location": location,
        "note": description,
        "url": None,
        "attendees": [],
        "alerts": [],
        "recurrences": list(recurrences or []),
        "parent_id": None,
        "recurring_uuid": None,
        "deactivated_at": None,
    }


def normalized_timetree(raw: dict[str, Any]):
    return normalize_timetree_event(
        raw,
        default_timezone="Asia/Tokyo",
        label_catalog=LABELS,
    )


def google_raw(
    *,
    event_id: str = "g-1",
    title: str = "Original title",
    label: str | None = "大河予定",
    managed_timetree_id: str | None = None,
    metadata: bool = True,
    recurrence: list[str] | None = None,
) -> dict[str, Any]:
    private: dict[str, str] = {}
    if metadata:
        if managed_timetree_id is not None:
            private["sync_source"] = GOOGLE_BRIDGE_SYNC_SOURCE
            private["timetree_id"] = managed_timetree_id
        if label is not None:
            private["timetree_label_name"] = label
    raw: dict[str, Any] = {
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
    }
    if metadata:
        raw["extendedProperties"] = {"private": private}
    if recurrence is not None:
        raw["recurrence"] = list(recurrence)
    return raw


def event_change(raw: dict[str, Any]):
    return google_event_change(
        raw,
        source_calendar_id="google-calendar",
        default_timezone="Asia/Tokyo",
        allow_missing_managed_label=raw.get("_allow_missing_managed_label", False),
    )


class FakeGoogle:
    calendar_id = "google-calendar"

    def __init__(
        self,
        raw_events: list[dict[str, Any]] | None = None,
        *,
        changes: list[Any] | None = None,
        next_token: str = "token-next",
    ) -> None:
        self.raw_events = {raw["id"]: copy.deepcopy(raw) for raw in (raw_events or [])}
        self.changes = changes
        self.next_token = next_token
        self.list_tokens: list[str | None] = []
        self.get_calls: list[str] = []
        self.patch_calls: list[tuple[str, dict[str, Any]]] = []
        self.patch_error: BaseException | None = None
        self.patch_wrong_metadata = False
        self.before_patch: Any | None = None

    def list_changes(self, *, sync_token: str):
        self.list_tokens.append(sync_token)
        if self.changes is not None:
            changes = tuple(self.changes)
        else:
            changes = tuple(
                event_change({**raw, "_allow_missing_managed_label": True})
                for raw in self.raw_events.values()
            )
        return SimpleNamespace(changes=changes, next_sync_token=self.next_token)

    def get_event(self, event_id: str) -> dict[str, Any]:
        self.get_calls.append(event_id)
        return copy.deepcopy(self.raw_events[event_id])

    def patch_event(self, event_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self.patch_calls.append((event_id, copy.deepcopy(body)))
        if self.before_patch is not None:
            self.before_patch()
        if self.patch_error is not None:
            raise self.patch_error
        raw = self.raw_events[event_id]
        private = copy.deepcopy(body["extendedProperties"]["private"])
        if self.patch_wrong_metadata:
            private["timetree_id"] = "wrong-target"
        raw["extendedProperties"] = {"private": private}
        return copy.deepcopy(raw)


class RaisingGoogle(FakeGoogle):
    def list_changes(self, *, sync_token: str):
        self.list_tokens.append(sync_token)
        event_change(
            google_raw(
                event_id="unknown-label",
                label="未知ラベル",
            )
        )
        return SimpleNamespace(changes=(), next_sync_token="unused")


class FakeTimeTree:
    calendar_id = "101"

    def __init__(self, events: list[dict[str, Any]] | None = None) -> None:
        self.events = [copy.deepcopy(raw) for raw in (events or [])]
        self.updated_events: list[dict[str, Any]] = []
        self.calls: list[tuple[str, Any]] = []
        self.create_calls: list[Any] = []
        self.update_calls: list[dict[str, Any]] = []
        self.delete_calls: list[str] = []
        self.before_create: Any | None = None
        self.create_error: BaseException | None = None
        self.before_update: Any | None = None
        self.update_return_raw_override: Any | None = None
        self.next_uuid = "tt-created-1"

    async def list_calendars(self) -> tuple[TimeTreeCalendar, ...]:
        self.calls.append(("list_calendars", None))
        return (
            TimeTreeCalendar(
                calendar_id=self.calendar_id,
                name="Fake calendar",
                alias_code=None,
                users=(),
            ),
        )

    async def get_calendar_labels(self) -> TimeTreeLabelCatalog:
        self.calls.append(("get_calendar_labels", None))
        return LABELS

    async def get_events(self) -> tuple[dict[str, Any], ...]:
        self.calls.append(("get_events", None))
        return tuple(copy.deepcopy(self.events))

    async def get_updated_events(self, updated_after_ms: int):
        self.calls.append(("get_updated_events", updated_after_ms))
        return tuple(copy.deepcopy(self.updated_events))

    def _raw_from_event(self, event: Any, event_id: str) -> dict[str, Any]:
        raw = timed_raw(
            event_id=event_id,
            title=event.title,
            label_id=LABELS.label_id_for_name(event.label),
            description=event.description,
            location=event.location,
        )
        raw["start_at"] = int(event.start.timestamp() * 1000)
        raw["end_at"] = int(event.end.timestamp() * 1000)
        raw["start_timezone"] = event.start_timezone
        raw["end_timezone"] = event.end_timezone
        raw["recurrences"] = list(event.recurrence.lines)
        return raw

    async def create_event(self, event: Any, *, allow_recurrence_write: bool):
        self.create_calls.append(event)
        if self.before_create is not None:
            self.before_create()
        if self.create_error is not None:
            raise self.create_error
        raw = self._raw_from_event(event, self.next_uuid)
        self.events.append(copy.deepcopy(raw))
        return TimeTreeWriteResult(event_uuid=self.next_uuid, raw_event=raw)

    async def update_event(
        self,
        event_uuid: str,
        event: Any,
        *,
        fields: set[str],
        allow_recurrence_write: bool,
    ):
        if self.before_update is not None:
            self.before_update()
        current = next(raw for raw in self.events if raw["uuid"] == event_uuid)
        body = timetree_update_body(
            event,
            fields=fields,
            calendar_id=self.calendar_id,
            default_timezone="Asia/Tokyo",
            allow_recurrence_write=allow_recurrence_write,
            label_catalog=LABELS if "label" in fields else None,
        )
        self.update_calls.append(
            {
                "event_uuid": event_uuid,
                "fields": set(fields),
                "body": body,
            }
        )
        raw = self._raw_from_event(event, event_uuid)
        self.events[self.events.index(current)] = copy.deepcopy(raw)
        returned_raw = raw
        if self.update_return_raw_override is not None:
            returned_raw = self.update_return_raw_override(copy.deepcopy(raw))
        return TimeTreeWriteResult(event_uuid=event_uuid, raw_event=returned_raw)

    async def delete_event(self, event_uuid: str, **kwargs: Any):
        self.delete_calls.append(event_uuid)
        raise AssertionError("P9 must not execute TimeTree delete")


class P9Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "state.db"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _seed_token(
        self, repository: StateRepository, token: str = "token-old"
    ) -> None:
        repository.set_sync_state("google_sync_token", token)
        repository.set_sync_state("bridge_bootstrapped_at", "2030-01-01T00:00:00+00:00")

    def _seed_mapping(
        self,
        repository: StateRepository,
        raw: dict[str, Any],
        google_id: str,
    ) -> None:
        normalized = normalized_timetree(raw)
        repository.create_event_link(
            timetree_event_id=raw["uuid"],
            google_event_id=google_id,
            event_kind=normalized.kind.value,
            last_synced_hash=event_hash(normalized),
            status="synced",
            last_synced_at="2030-01-01T00:00:00+00:00",
        )

    async def _run(
        self,
        google: Any,
        timetree: FakeTimeTree,
    ) -> Any:
        with ensure_database(self.db_path) as connection:
            repository = StateRepository(connection)
            self._seed_token(repository)
            result = await GoogleToTimeTreeRunner(
                google_client=google,
                timetree_client=timetree,
                repository=repository,
                default_timezone="Asia/Tokyo",
                overlap_seconds=30,
            ).run()
            return result, repository

    async def test_mapped_google_upsert_updates_timetree(self) -> None:
        current = timed_raw(event_id="tt-mapped", title="Old title")
        google = FakeGoogle(
            [
                google_raw(
                    event_id="g-mapped",
                    title="New title",
                    managed_timetree_id="tt-mapped",
                )
            ]
        )
        timetree = FakeTimeTree([current])
        with ensure_database(self.db_path) as connection:
            repo = StateRepository(connection)
            self._seed_token(repo)
            self._seed_mapping(repo, current, "g-mapped")
            result = await GoogleToTimeTreeRunner(
                google_client=google,
                timetree_client=timetree,
                repository=repo,
                default_timezone="Asia/Tokyo",
            ).run()
            self.assertEqual(result.updated_event_count, 1)
            self.assertEqual(timetree.update_calls[0]["fields"], {"title"})
            self.assertEqual(repo.get_sync_state("google_sync_token"), "token-next")

    async def test_unchanged_label_omits_label_id_write(self) -> None:
        current = timed_raw(event_id="tt-label", title="Old title", label_id=10)
        google = FakeGoogle(
            [
                google_raw(
                    event_id="g-label",
                    title="New title",
                    label="大河予定",
                    managed_timetree_id="tt-label",
                )
            ]
        )
        timetree = FakeTimeTree([current])
        with ensure_database(self.db_path) as connection:
            repo = StateRepository(connection)
            self._seed_token(repo)
            self._seed_mapping(repo, current, "g-label")
            await GoogleToTimeTreeRunner(
                google_client=google,
                timetree_client=timetree,
                repository=repo,
                default_timezone="Asia/Tokyo",
            ).run()
        self.assertNotIn("label_id", timetree.update_calls[0]["body"])

    async def test_new_google_series_creates_series_mapping(self) -> None:
        raw = google_raw(
            event_id="g-new-series",
            title="New series",
            metadata=False,
            recurrence=["RRULE:FREQ=WEEKLY;COUNT=3;BYDAY=TU"],
        )
        google = FakeGoogle([raw])
        timetree = FakeTimeTree()
        result, _ = await self._run(google, timetree)
        with ensure_database(self.db_path) as connection:
            link = StateRepository(connection).get_event_link_by_google_id(
                "g-new-series"
            )
        self.assertEqual(result.created_event_count, 1)
        self.assertEqual(timetree.create_calls[0].kind.value, "series")
        self.assertEqual(link["event_kind"], "series")
        self.assertEqual(
            timetree.events[0]["recurrences"],
            ["RRULE:BYDAY=TU;COUNT=3;FREQ=WEEKLY"],
        )

    async def test_mapped_single_to_series_updates_recurrence_and_link_kind(
        self,
    ) -> None:
        current = timed_raw(event_id="tt-single-series")
        raw = google_raw(
            event_id="g-single-series",
            managed_timetree_id="tt-single-series",
            recurrence=["RRULE:FREQ=WEEKLY;COUNT=3;BYDAY=TU"],
        )
        google = FakeGoogle([raw])
        timetree = FakeTimeTree([current])
        with ensure_database(self.db_path) as connection:
            repo = StateRepository(connection)
            self._seed_token(repo)
            self._seed_mapping(repo, current, "g-single-series")
            result = await GoogleToTimeTreeRunner(
                google_client=google,
                timetree_client=timetree,
                repository=repo,
                default_timezone="Asia/Tokyo",
            ).run()
            link = repo.get_event_link_by_google_id("g-single-series")
        self.assertEqual(result.updated_event_count, 1)
        self.assertIn("recurrence", timetree.update_calls[0]["fields"])
        self.assertEqual(link["event_kind"], "series")
        self.assertEqual(
            timetree.update_calls[0]["body"]["recurrences"],
            ["RRULE:BYDAY=TU;COUNT=3;FREQ=WEEKLY"],
        )

    async def test_mapped_series_to_single_removes_recurrence_and_link_kind(
        self,
    ) -> None:
        recurrence = ["RRULE:FREQ=WEEKLY;COUNT=3;BYDAY=TU"]
        current = timed_raw(event_id="tt-series-single", recurrences=recurrence)
        raw = google_raw(
            event_id="g-series-single",
            managed_timetree_id="tt-series-single",
        )
        google = FakeGoogle([raw])
        timetree = FakeTimeTree([current])
        with ensure_database(self.db_path) as connection:
            repo = StateRepository(connection)
            self._seed_token(repo)
            self._seed_mapping(repo, current, "g-series-single")
            result = await GoogleToTimeTreeRunner(
                google_client=google,
                timetree_client=timetree,
                repository=repo,
                default_timezone="Asia/Tokyo",
            ).run()
            link = repo.get_event_link_by_google_id("g-series-single")
        self.assertEqual(result.updated_event_count, 1)
        self.assertEqual(timetree.update_calls[0]["body"]["recurrences"], [])
        self.assertEqual(link["event_kind"], "single")

    async def test_mapped_series_rrule_change_remains_supported(self) -> None:
        old_recurrence = ["RRULE:FREQ=WEEKLY;COUNT=3;BYDAY=TU"]
        new_recurrence = ["RRULE:FREQ=WEEKLY;COUNT=4;BYDAY=TU"]
        current = timed_raw(event_id="tt-series-update", recurrences=old_recurrence)
        raw = google_raw(
            event_id="g-series-update",
            managed_timetree_id="tt-series-update",
            recurrence=new_recurrence,
        )
        google = FakeGoogle([raw])
        timetree = FakeTimeTree([current])
        with ensure_database(self.db_path) as connection:
            repo = StateRepository(connection)
            self._seed_token(repo)
            self._seed_mapping(repo, current, "g-series-update")
            result = await GoogleToTimeTreeRunner(
                google_client=google,
                timetree_client=timetree,
                repository=repo,
                default_timezone="Asia/Tokyo",
            ).run()
            link = repo.get_event_link_by_google_id("g-series-update")
        self.assertEqual(result.updated_event_count, 1)
        self.assertEqual(link["event_kind"], "series")
        self.assertEqual(
            timetree.update_calls[0]["body"]["recurrences"],
            ["RRULE:BYDAY=TU;COUNT=4;FREQ=WEEKLY"],
        )

    async def test_exception_transition_remains_blocked(self) -> None:
        current = timed_raw(event_id="tt-exception-transition")
        raw = google_raw(
            event_id="g-exception-transition",
            managed_timetree_id="tt-exception-transition",
        )
        raw["recurringEventId"] = "g-series"
        raw["originalStartTime"] = {
            "dateTime": "2030-01-01T19:00:00+09:00",
            "timeZone": "Asia/Tokyo",
        }
        google = FakeGoogle(changes=[event_change(raw)])
        timetree = FakeTimeTree([current])
        with ensure_database(self.db_path) as connection:
            repo = StateRepository(connection)
            self._seed_token(repo)
            self._seed_mapping(repo, current, "g-exception-transition")
            with self.assertRaises(GoogleToTimeTreeError) as caught:
                await GoogleToTimeTreeRunner(
                    google_client=google,
                    timetree_client=timetree,
                    repository=repo,
                    default_timezone="Asia/Tokyo",
                ).run()
            self.assertEqual(repo.get_sync_state("google_sync_token"), "token-old")
        self.assertEqual(caught.exception.code, "UNSUPPORTED_RECURRENCE_EXCEPTION")
        self.assertEqual(timetree.update_calls, [])

    async def test_new_google_event_defaults_to_taiga_label(self) -> None:
        raw = google_raw(event_id="g-new", title="New event", metadata=False)
        google = FakeGoogle([raw])
        timetree = FakeTimeTree()
        result, _ = await self._run(google, timetree)
        self.assertEqual(result.created_event_count, 1)
        self.assertEqual(timetree.create_calls[0].label, "大河予定")
        created_raw = timetree.events[0]
        self.assertEqual(created_raw["label_id"], 10)

    async def test_explicit_common_label_uses_runtime_label_id(self) -> None:
        raw = google_raw(event_id="g-common", title="Common", label="共通予定")
        google = FakeGoogle([raw])
        timetree = FakeTimeTree()
        await self._run(google, timetree)
        self.assertEqual(timetree.events[0]["label_id"], 20)

    async def test_mapped_common_label_without_metadata_safe_stops(self) -> None:
        current = timed_raw(event_id="tt-missing-label")
        raw = google_raw(
            event_id="g-missing-label",
            managed_timetree_id="tt-missing-label",
            label=None,
        )
        raw["_allow_missing_managed_label"] = True
        google = FakeGoogle(changes=[event_change(raw)])
        timetree = FakeTimeTree([current])
        with ensure_database(self.db_path) as connection:
            repo = StateRepository(connection)
            self._seed_token(repo)
            self._seed_mapping(repo, current, "g-missing-label")
            with self.assertRaises(GoogleToTimeTreeError) as caught:
                await GoogleToTimeTreeRunner(
                    google_client=google,
                    timetree_client=timetree,
                    repository=repo,
                    default_timezone="Asia/Tokyo",
                ).run()
            self.assertEqual(repo.get_sync_state("google_sync_token"), "token-old")
        self.assertEqual(caught.exception.code, "UNSUPPORTED_GOOGLE_LABEL_METADATA")
        self.assertEqual(timetree.create_calls, [])
        self.assertEqual(timetree.update_calls, [])

    async def test_mapped_event_with_mismatched_timetree_id_safe_stops(self) -> None:
        current = timed_raw(event_id="tt-id-check")
        raw = google_raw(
            event_id="g-id-check",
            managed_timetree_id="tt-other",
        )
        google = FakeGoogle([raw])
        timetree = FakeTimeTree([current])
        with ensure_database(self.db_path) as connection:
            repo = StateRepository(connection)
            self._seed_token(repo)
            self._seed_mapping(repo, current, "g-id-check")
            with self.assertRaises(GoogleToTimeTreeError) as caught:
                await GoogleToTimeTreeRunner(
                    google_client=google,
                    timetree_client=timetree,
                    repository=repo,
                    default_timezone="Asia/Tokyo",
                ).run()
            self.assertEqual(repo.get_sync_state("google_sync_token"), "token-old")
        self.assertEqual(caught.exception.code, "GOOGLE_METADATA_MAPPING_MISMATCH")
        self.assertEqual(timetree.create_calls, [])
        self.assertEqual(timetree.update_calls, [])

    async def test_mapped_event_missing_sync_source_safe_stops(self) -> None:
        current = timed_raw(event_id="tt-missing-source")
        raw = google_raw(
            event_id="g-missing-source",
            managed_timetree_id="tt-missing-source",
        )
        del raw["extendedProperties"]["private"]["sync_source"]
        google = FakeGoogle([raw])
        timetree = FakeTimeTree([current])
        with ensure_database(self.db_path) as connection:
            repo = StateRepository(connection)
            self._seed_token(repo)
            self._seed_mapping(repo, current, "g-missing-source")
            with self.assertRaises(GoogleToTimeTreeError) as caught:
                await GoogleToTimeTreeRunner(
                    google_client=google,
                    timetree_client=timetree,
                    repository=repo,
                    default_timezone="Asia/Tokyo",
                ).run()
            self.assertEqual(repo.get_sync_state("google_sync_token"), "token-old")
        self.assertEqual(caught.exception.code, "GOOGLE_METADATA_MAPPING_MISMATCH")
        self.assertEqual(timetree.create_calls, [])
        self.assertEqual(timetree.update_calls, [])

    async def test_mapped_event_foreign_sync_source_safe_stops(self) -> None:
        current = timed_raw(event_id="tt-foreign-source")
        raw = google_raw(
            event_id="g-foreign-source",
            managed_timetree_id="tt-foreign-source",
        )
        raw["extendedProperties"]["private"]["sync_source"] = "foreign-bridge"
        google = FakeGoogle([raw])
        timetree = FakeTimeTree([current])
        with ensure_database(self.db_path) as connection:
            repo = StateRepository(connection)
            self._seed_token(repo)
            self._seed_mapping(repo, current, "g-foreign-source")
            with self.assertRaises(GoogleToTimeTreeError) as caught:
                await GoogleToTimeTreeRunner(
                    google_client=google,
                    timetree_client=timetree,
                    repository=repo,
                    default_timezone="Asia/Tokyo",
                ).run()
            self.assertEqual(repo.get_sync_state("google_sync_token"), "token-old")
        self.assertEqual(caught.exception.code, "GOOGLE_METADATA_MAPPING_MISMATCH")
        self.assertEqual(timetree.create_calls, [])
        self.assertEqual(timetree.update_calls, [])

    async def test_mapped_event_missing_timetree_id_safe_stops(self) -> None:
        current = timed_raw(event_id="tt-missing-id")
        raw = google_raw(
            event_id="g-missing-id",
            managed_timetree_id="tt-missing-id",
        )
        del raw["extendedProperties"]["private"]["timetree_id"]
        google = FakeGoogle([raw])
        timetree = FakeTimeTree([current])
        with ensure_database(self.db_path) as connection:
            repo = StateRepository(connection)
            self._seed_token(repo)
            self._seed_mapping(repo, current, "g-missing-id")
            with self.assertRaises(GoogleToTimeTreeError) as caught:
                await GoogleToTimeTreeRunner(
                    google_client=google,
                    timetree_client=timetree,
                    repository=repo,
                    default_timezone="Asia/Tokyo",
                ).run()
            self.assertEqual(repo.get_sync_state("google_sync_token"), "token-old")
        self.assertEqual(caught.exception.code, "GOOGLE_METADATA_MAPPING_MISMATCH")
        self.assertEqual(timetree.create_calls, [])
        self.assertEqual(timetree.update_calls, [])

    async def test_unmapped_bridge_identity_metadata_never_creates(self) -> None:
        raw = google_raw(
            event_id="g-unmapped-bridge",
            managed_timetree_id="tt-orphan",
        )
        google = FakeGoogle([raw])
        timetree = FakeTimeTree()
        with ensure_database(self.db_path) as connection:
            repo = StateRepository(connection)
            self._seed_token(repo)
            with self.assertRaises(GoogleToTimeTreeError) as caught:
                await GoogleToTimeTreeRunner(
                    google_client=google,
                    timetree_client=timetree,
                    repository=repo,
                    default_timezone="Asia/Tokyo",
                ).run()
            self.assertEqual(repo.get_sync_state("google_sync_token"), "token-old")
        self.assertEqual(caught.exception.code, "GOOGLE_METADATA_MAPPING_REQUIRED")
        self.assertEqual(timetree.create_calls, [])

    async def test_unchanged_mapped_google_event_skips_conflict_guard(self) -> None:
        current = timed_raw(event_id="tt-unchanged")
        raw = google_raw(event_id="g-unchanged", managed_timetree_id="tt-unchanged")
        google_event = normalize_google_event(
            raw,
            source_calendar_id="google-calendar",
            default_timezone="Asia/Tokyo",
        )
        google = FakeGoogle([raw])
        timetree = FakeTimeTree([current])
        with ensure_database(self.db_path) as connection:
            repo = StateRepository(connection)
            self._seed_token(repo)
            repo.create_event_link(
                timetree_event_id="tt-unchanged",
                google_event_id="g-unchanged",
                last_synced_hash=event_hash(google_event),
                status="synced",
                last_synced_at="2030-01-01T00:00:00+00:00",
            )
            result = await GoogleToTimeTreeRunner(
                google_client=google,
                timetree_client=timetree,
                repository=repo,
                default_timezone="Asia/Tokyo",
            ).run()
        self.assertEqual(result.skipped_event_count, 1)
        self.assertEqual(timetree.update_calls, [])
        self.assertNotIn("get_updated_events", [name for name, _ in timetree.calls])
        self.assertNotIn("get_events", [name for name, _ in timetree.calls])

    async def test_unchanged_google_event_does_not_conflict_with_changed_timetree(
        self,
    ) -> None:
        changed = timed_raw(event_id="tt-unchanged-tt", title="TT changed")
        raw = google_raw(
            event_id="g-unchanged-tt",
            managed_timetree_id="tt-unchanged-tt",
        )
        google_event = normalize_google_event(
            raw,
            source_calendar_id="google-calendar",
            default_timezone="Asia/Tokyo",
        )
        google = FakeGoogle([raw])
        timetree = FakeTimeTree([changed])
        timetree.updated_events = [changed]
        with ensure_database(self.db_path) as connection:
            repo = StateRepository(connection)
            self._seed_token(repo)
            repo.create_event_link(
                timetree_event_id="tt-unchanged-tt",
                google_event_id="g-unchanged-tt",
                last_synced_hash=event_hash(google_event),
                status="synced",
                last_synced_at="2030-01-01T00:00:00+00:00",
            )
            result = await GoogleToTimeTreeRunner(
                google_client=google,
                timetree_client=timetree,
                repository=repo,
                default_timezone="Asia/Tokyo",
            ).run()
            self.assertIsNone(repo.get_conflict("p9:conflict:g-unchanged-tt"))
        self.assertEqual(result.skipped_event_count, 1)
        self.assertEqual(timetree.update_calls, [])
        self.assertEqual(timetree.events[0]["title"], changed["title"])

    async def test_metadata_only_google_change_skips_timetree(self) -> None:
        current = timed_raw(event_id="tt-metadata-only")
        raw = google_raw(
            event_id="g-metadata-only",
            managed_timetree_id="tt-metadata-only",
        )
        raw["extendedProperties"]["private"]["bridge_version"] = "new-version"
        google_event = normalize_google_event(
            raw,
            source_calendar_id="google-calendar",
            default_timezone="Asia/Tokyo",
        )
        google = FakeGoogle([raw])
        timetree = FakeTimeTree([current])
        with ensure_database(self.db_path) as connection:
            repo = StateRepository(connection)
            self._seed_token(repo)
            repo.create_event_link(
                timetree_event_id="tt-metadata-only",
                google_event_id="g-metadata-only",
                last_synced_hash=event_hash(google_event),
                status="synced",
                last_synced_at="2030-01-01T00:00:00+00:00",
            )
            result = await GoogleToTimeTreeRunner(
                google_client=google,
                timetree_client=timetree,
                repository=repo,
                default_timezone="Asia/Tokyo",
            ).run()
        self.assertEqual(result.skipped_event_count, 1)
        self.assertEqual(timetree.update_calls, [])

    async def test_unknown_label_stops_before_timetree_write(self) -> None:
        google = RaisingGoogle()
        timetree = FakeTimeTree()
        with ensure_database(self.db_path) as connection:
            repo = StateRepository(connection)
            self._seed_token(repo)
            with self.assertRaises(GoogleToTimeTreeError) as caught:
                await GoogleToTimeTreeRunner(
                    google_client=google,
                    timetree_client=timetree,
                    repository=repo,
                    default_timezone="Asia/Tokyo",
                ).run()
            self.assertEqual(caught.exception.code, "UNSUPPORTED_GOOGLE_LABEL_METADATA")
            self.assertEqual(repo.get_sync_state("google_sync_token"), "token-old")
        self.assertEqual(timetree.create_calls, [])
        self.assertEqual(timetree.update_calls, [])

    async def test_create_operation_is_durable_before_remote_write(self) -> None:
        raw = google_raw(event_id="g-durable", title="Durable", metadata=False)
        google = FakeGoogle([raw])
        timetree = FakeTimeTree()
        operation_states: list[str] = []

        def observe_operation() -> None:
            with ensure_database(self.db_path) as connection:
                op = StateRepository(connection).get_operation(
                    "p9:google_to_timetree:create:g-durable"
                )
                assert op is not None
                operation_states.append(op["state"])

        timetree.before_create = observe_operation
        await self._run(google, timetree)
        self.assertEqual(operation_states, ["prepared"])

    async def test_successful_create_saves_uuid_mapping_and_done_operation(
        self,
    ) -> None:
        raw = google_raw(event_id="g-create", title="Create", metadata=False)
        google = FakeGoogle([raw])
        timetree = FakeTimeTree()
        await self._run(google, timetree)
        with ensure_database(self.db_path) as connection:
            repo = StateRepository(connection)
            link = repo.get_event_link_by_google_id("g-create")
            op = repo.get_operation("p9:google_to_timetree:create:g-create")
        self.assertEqual(link["timetree_event_id"], "tt-created-1")
        self.assertEqual(op["state"], "done")
        self.assertEqual(len(google.patch_calls), 1)
        patched_private = google.patch_calls[0][1]["extendedProperties"]["private"]
        self.assertEqual(
            patched_private,
            {
                "sync_source": GOOGLE_BRIDGE_SYNC_SOURCE,
                "timetree_id": "tt-created-1",
                "timetree_label_name": LABELS.label_name_for_id(10),
                "bridge_version": "p9-test",
            },
        )

    async def test_create_is_done_only_after_google_metadata_patch(self) -> None:
        raw = google_raw(event_id="g-patch-order", metadata=False)
        google = FakeGoogle([raw])
        timetree = FakeTimeTree()
        observed: list[str] = []

        def observe_patch() -> None:
            with ensure_database(self.db_path) as connection:
                operation = StateRepository(connection).get_operation(
                    "p9:google_to_timetree:create:g-patch-order"
                )
                assert operation is not None
                observed.append(operation["state"])

        google.before_patch = observe_patch
        await self._run(google, timetree)
        self.assertEqual(observed, ["mapping_saved"])

    async def test_metadata_patch_failure_retains_mapping_saved(self) -> None:
        raw = google_raw(event_id="g-patch-failure", metadata=False)
        google = FakeGoogle([raw])
        google.patch_error = OSError("Google patch transport failure")
        timetree = FakeTimeTree()
        with ensure_database(self.db_path) as connection:
            repo = StateRepository(connection)
            self._seed_token(repo)
            with self.assertRaises(GoogleToTimeTreeError) as caught:
                await GoogleToTimeTreeRunner(
                    google_client=google,
                    timetree_client=timetree,
                    repository=repo,
                    default_timezone="Asia/Tokyo",
                ).run()
            operation = repo.get_operation(
                "p9:google_to_timetree:create:g-patch-failure"
            )
            link = repo.get_event_link_by_google_id("g-patch-failure")
            self.assertEqual(repo.get_sync_state("google_sync_token"), "token-old")
        self.assertEqual(caught.exception.code, "GOOGLE_METADATA_PATCH_FAILED")
        self.assertEqual(caught.exception.confirmed_remote_writes, 1)
        self.assertTrue(caught.exception.remote_write_outcome_unknown)
        self.assertEqual(operation["state"], "mapping_saved")
        self.assertEqual(link["timetree_event_id"], "tt-created-1")
        self.assertEqual(len(timetree.create_calls), 1)

        google.patch_error = None
        retry_timetree = FakeTimeTree([timetree.events[0]])
        with ensure_database(self.db_path) as connection:
            repo = StateRepository(connection)
            result = await GoogleToTimeTreeRunner(
                google_client=google,
                timetree_client=retry_timetree,
                repository=repo,
                default_timezone="Asia/Tokyo",
            ).run()
            operation = repo.get_operation(
                "p9:google_to_timetree:create:g-patch-failure"
            )
        self.assertTrue(result.token_committed)
        self.assertEqual(operation["state"], "done")
        self.assertEqual(retry_timetree.create_calls, [])

    async def test_mismatched_google_metadata_safe_stops_create_completion(
        self,
    ) -> None:
        raw = google_raw(event_id="g-wrong-patch", metadata=False)
        google = FakeGoogle([raw])
        google.patch_wrong_metadata = True
        timetree = FakeTimeTree()
        with ensure_database(self.db_path) as connection:
            repo = StateRepository(connection)
            self._seed_token(repo)
            with self.assertRaises(GoogleToTimeTreeError) as caught:
                await GoogleToTimeTreeRunner(
                    google_client=google,
                    timetree_client=timetree,
                    repository=repo,
                    default_timezone="Asia/Tokyo",
                ).run()
            operation = repo.get_operation("p9:google_to_timetree:create:g-wrong-patch")
        self.assertEqual(caught.exception.code, "GOOGLE_METADATA_MISMATCH")
        self.assertEqual(operation["state"], "mapping_saved")
        self.assertEqual(len(timetree.create_calls), 1)

    async def test_ambiguous_create_does_not_blindly_duplicate(self) -> None:
        raw = google_raw(event_id="g-ambiguous", title="Ambiguous", metadata=False)
        google = FakeGoogle([raw])
        first_timetree = FakeTimeTree()
        first_timetree.create_error = OSError("ambiguous remote outcome")
        with self.assertRaises(GoogleToTimeTreeError):
            await self._run(google, first_timetree)

        normalized = normalize_google_event(
            raw,
            source_calendar_id="google-calendar",
            default_timezone="Asia/Tokyo",
        )
        candidates = [
            timed_raw(
                event_id="tt-a",
                title=normalized.title,
            ),
            timed_raw(
                event_id="tt-b",
                title=normalized.title,
            ),
        ]
        second_timetree = FakeTimeTree(candidates)
        with ensure_database(self.db_path) as connection:
            repo = StateRepository(connection)
            with self.assertRaises(GoogleToTimeTreeError) as caught:
                await GoogleToTimeTreeRunner(
                    google_client=google,
                    timetree_client=second_timetree,
                    repository=repo,
                    default_timezone="Asia/Tokyo",
                ).run()
        self.assertEqual(caught.exception.code, NEEDS_MANUAL_RECOVERY)
        self.assertEqual(second_timetree.create_calls, [])

    async def test_prepared_create_with_one_identical_event_is_manual_recovery(
        self,
    ) -> None:
        raw = google_raw(event_id="g-recover", title="Recover", metadata=False)
        google = FakeGoogle([raw])
        first_timetree = FakeTimeTree()
        first_timetree.create_error = OSError("outcome unknown")
        with self.assertRaises(GoogleToTimeTreeError):
            await self._run(google, first_timetree)

        normalized = normalize_google_event(
            raw,
            source_calendar_id="google-calendar",
            default_timezone="Asia/Tokyo",
        )
        recovered_raw = timed_raw(event_id="tt-recovered", title=normalized.title)
        second_timetree = FakeTimeTree([recovered_raw])
        with ensure_database(self.db_path) as connection:
            repo = StateRepository(connection)
            with self.assertRaises(GoogleToTimeTreeError) as caught:
                await GoogleToTimeTreeRunner(
                    google_client=google,
                    timetree_client=second_timetree,
                    repository=repo,
                    default_timezone="Asia/Tokyo",
                ).run()
            link = repo.get_event_link_by_google_id("g-recover")
            operation = repo.get_operation("p9:google_to_timetree:create:g-recover")
        self.assertEqual(caught.exception.code, NEEDS_MANUAL_RECOVERY)
        self.assertIsNone(link)
        self.assertEqual(operation["state"], "failed")
        self.assertEqual(second_timetree.create_calls, [])

    async def test_non_unique_recovery_is_manual_recovery(self) -> None:
        raw = google_raw(event_id="g-nonunique", title="Nonunique", metadata=False)
        google = FakeGoogle([raw])
        first_timetree = FakeTimeTree()
        first_timetree.create_error = OSError("outcome unknown")
        with self.assertRaises(GoogleToTimeTreeError):
            await self._run(google, first_timetree)
        normalized = normalize_google_event(
            raw,
            source_calendar_id="google-calendar",
            default_timezone="Asia/Tokyo",
        )
        second_timetree = FakeTimeTree(
            [
                timed_raw(event_id="tt-one", title=normalized.title),
                timed_raw(event_id="tt-two", title=normalized.title),
            ]
        )
        with ensure_database(self.db_path) as connection:
            repo = StateRepository(connection)
            with self.assertRaises(GoogleToTimeTreeError) as caught:
                await GoogleToTimeTreeRunner(
                    google_client=google,
                    timetree_client=second_timetree,
                    repository=repo,
                    default_timezone="Asia/Tokyo",
                ).run()
            operation = repo.get_operation("p9:google_to_timetree:create:g-nonunique")
        self.assertEqual(caught.exception.code, NEEDS_MANUAL_RECOVERY)
        self.assertEqual(operation["last_error"], NEEDS_MANUAL_RECOVERY)
        self.assertEqual(second_timetree.create_calls, [])

    async def test_remote_applied_exact_uuid_recovers_without_create(self) -> None:
        raw = google_raw(event_id="g-exact-recovery", title="Exact", metadata=False)
        google = FakeGoogle([raw])
        first_timetree = FakeTimeTree()
        first_timetree.create_error = OSError("outcome unknown")
        with self.assertRaises(GoogleToTimeTreeError):
            await self._run(google, first_timetree)

        target = timed_raw(event_id="tt-exact", title="Exact")
        with ensure_database(self.db_path) as connection:
            repo = StateRepository(connection)
            repo.transition_operation(
                "p9:google_to_timetree:create:g-exact-recovery",
                "remote_applied",
                target_event_id="tt-exact",
            )
            recovery_timetree = FakeTimeTree([target])
            result = await GoogleToTimeTreeRunner(
                google_client=google,
                timetree_client=recovery_timetree,
                repository=repo,
                default_timezone="Asia/Tokyo",
            ).run()
            link = repo.get_event_link_by_google_id("g-exact-recovery")
            operation = repo.get_operation(
                "p9:google_to_timetree:create:g-exact-recovery"
            )
        self.assertTrue(result.token_committed)
        self.assertEqual(link["timetree_event_id"], "tt-exact")
        self.assertEqual(operation["state"], "done")
        self.assertEqual(recovery_timetree.create_calls, [])

    async def test_mapping_saved_exact_mapping_finishes_without_create(self) -> None:
        raw = google_raw(event_id="g-mapping-recovery", title="Mapping", metadata=False)
        google = FakeGoogle([raw])
        first_timetree = FakeTimeTree()
        first_timetree.create_error = OSError("outcome unknown")
        with self.assertRaises(GoogleToTimeTreeError):
            await self._run(google, first_timetree)

        target = timed_raw(event_id="tt-mapping", title="Mapping")
        source_hash = event_hash(
            normalize_google_event(
                raw,
                source_calendar_id="google-calendar",
                default_timezone="Asia/Tokyo",
            )
        )
        with ensure_database(self.db_path) as connection:
            repo = StateRepository(connection)
            repo.transition_operation(
                "p9:google_to_timetree:create:g-mapping-recovery",
                "remote_applied",
                target_event_id="tt-mapping",
            )
            repo.create_event_link(
                timetree_event_id="tt-mapping",
                google_event_id="g-mapping-recovery",
                event_kind="single",
                last_synced_hash=source_hash,
                status="synced",
            )
            repo.transition_operation(
                "p9:google_to_timetree:create:g-mapping-recovery",
                "mapping_saved",
            )
            recovery_timetree = FakeTimeTree([target])
            result = await GoogleToTimeTreeRunner(
                google_client=google,
                timetree_client=recovery_timetree,
                repository=repo,
                default_timezone="Asia/Tokyo",
            ).run()
            operation = repo.get_operation(
                "p9:google_to_timetree:create:g-mapping-recovery"
            )
        self.assertTrue(result.token_committed)
        self.assertEqual(operation["state"], "done")
        self.assertEqual(recovery_timetree.create_calls, [])

    async def test_next_sync_token_commits_only_after_safe_processing(self) -> None:
        raw = google_raw(event_id="g-token", title="Token", metadata=False)
        google = FakeGoogle([raw], next_token="token-committed")
        timetree = FakeTimeTree()
        result, _ = await self._run(google, timetree)
        self.assertTrue(result.token_committed)
        self.assertEqual(google.list_tokens, ["token-old"])
        with ensure_database(self.db_path) as connection:
            self.assertEqual(
                StateRepository(connection).get_sync_state("google_sync_token"),
                "token-committed",
            )

    async def test_processing_failure_does_not_advance_sync_token(self) -> None:
        current = timed_raw(event_id="tt-failure", title="Old")
        google = FakeGoogle(
            [
                google_raw(
                    event_id="g-failure",
                    title="New",
                    managed_timetree_id="tt-failure",
                )
            ],
            next_token="must-not-commit",
        )
        timetree = FakeTimeTree([current])
        timetree.update_event = _failing_update  # type: ignore[method-assign]
        with ensure_database(self.db_path) as connection:
            repo = StateRepository(connection)
            self._seed_token(repo)
            self._seed_mapping(repo, current, "g-failure")
            with self.assertRaises(GoogleToTimeTreeError):
                await GoogleToTimeTreeRunner(
                    google_client=google,
                    timetree_client=timetree,
                    repository=repo,
                    default_timezone="Asia/Tokyo",
                ).run()
            self.assertEqual(repo.get_sync_state("google_sync_token"), "token-old")

    async def test_update_readback_failure_reports_confirmed_remote_write(self) -> None:
        current = timed_raw(event_id="tt-readback", title="Old")
        google = FakeGoogle(
            [
                google_raw(
                    event_id="g-readback",
                    title="New",
                    managed_timetree_id="tt-readback",
                )
            ]
        )
        timetree = FakeTimeTree([current])

        def mismatched_readback(raw: dict[str, Any]) -> dict[str, Any]:
            raw["title"] = "different after write"
            return raw

        timetree.update_return_raw_override = mismatched_readback
        with ensure_database(self.db_path) as connection:
            repo = StateRepository(connection)
            self._seed_token(repo)
            self._seed_mapping(repo, current, "g-readback")
            with self.assertRaises(GoogleToTimeTreeError) as caught:
                await GoogleToTimeTreeRunner(
                    google_client=google,
                    timetree_client=timetree,
                    repository=repo,
                    default_timezone="Asia/Tokyo",
                ).run()
        self.assertEqual(caught.exception.code, "TIMETREE_TARGET_HASH_MISMATCH")
        self.assertEqual(caught.exception.confirmed_remote_writes, 1)
        self.assertFalse(caught.exception.remote_write_outcome_unknown)
        self.assertEqual(len(timetree.update_calls), 1)

    async def test_remote_transport_exception_is_ambiguous_not_zero_certain(
        self,
    ) -> None:
        raw = google_raw(event_id="g-ambiguous-observe", metadata=False)
        google = FakeGoogle([raw])
        timetree = FakeTimeTree()
        timetree.create_error = OSError("remote outcome unknown")
        with self.assertRaises(GoogleToTimeTreeError) as caught:
            await self._run(google, timetree)
        self.assertEqual(caught.exception.confirmed_remote_writes, 0)
        self.assertTrue(caught.exception.remote_write_outcome_unknown)

    async def test_p9_requires_bootstrap_state_even_with_google_token(self) -> None:
        google = FakeGoogle(changes=[])
        timetree = FakeTimeTree()
        with ensure_database(self.db_path) as connection:
            repo = StateRepository(connection)
            repo.set_sync_state("google_sync_token", "token-only")
            with self.assertRaises(GoogleToTimeTreeError) as caught:
                await GoogleToTimeTreeRunner(
                    google_client=google,
                    timetree_client=timetree,
                    repository=repo,
                    default_timezone="Asia/Tokyo",
                ).run()
        self.assertEqual(caught.exception.code, "P9_BOOTSTRAP_REQUIRED")
        self.assertEqual(google.list_tokens, [])
        self.assertEqual(timetree.create_calls, [])
        self.assertEqual(timetree.update_calls, [])

    def test_cli_reports_confirmed_write_after_readback_failure(self) -> None:
        from bridge.cli import run_sync
        from bridge.config import load_config
        from tests.p2.test_foundation import write_config

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)
            config = load_config(config_path)
            current = timed_raw(event_id="tt-cli-observe", title="Old")
            raw = google_raw(
                event_id="g-cli-observe",
                title="New",
                managed_timetree_id="tt-cli-observe",
            )
            google = FakeGoogle([raw])
            timetree = FakeTimeTree([current])
            timetree.update_return_raw_override = lambda value: {
                **value,
                "title": "wrong readback",
            }
            with ensure_database(config.database_path) as connection:
                repo = StateRepository(connection)
                self._seed_token(repo)
                self._seed_mapping(repo, current, "g-cli-observe")
            result = run_sync(
                config_path,
                google_client=google,
                timetree_client=timetree,
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["remote_writes"], 1)
        self.assertFalse(result["remote_write_outcome_unknown"])

    async def test_delete_and_recurrence_exception_delete_are_distinct(self) -> None:
        normal_delete = event_change({"id": "g-delete", "status": "cancelled"})
        exception_delete = event_change(
            {
                "id": "g-exception-delete",
                "status": "cancelled",
                "recurringEventId": "g-series",
                "originalStartTime": {
                    "dateTime": "2030-01-01T19:00:00+09:00",
                    "timeZone": "Asia/Tokyo",
                },
            }
        )
        self.assertEqual(normal_delete.change_type, ChangeType.DELETE)
        self.assertEqual(
            exception_delete.change_type,
            ChangeType.RECURRENCE_EXCEPTION_DELETE,
        )
        google = FakeGoogle(changes=[normal_delete, exception_delete])
        timetree = FakeTimeTree()
        result, _ = await self._run(google, timetree)
        self.assertEqual(result.deferred_delete_count, 2)
        self.assertFalse(result.token_committed)
        self.assertEqual(timetree.delete_calls, [])
        with ensure_database(self.db_path) as connection:
            operations = StateRepository(connection).list_operations(
                direction="google_to_timetree"
            )
        self.assertEqual(
            {operation["action"] for operation in operations},
            {"delete", "recurrence_exception_delete"},
        )

    async def test_recurrence_exception_delete_never_calls_series_delete(self) -> None:
        change = event_change(
            {
                "id": "g-exception-delete-only",
                "status": "cancelled",
                "recurringEventId": "g-series",
                "originalStartTime": {
                    "date": "2030-01-01",
                },
            }
        )
        google = FakeGoogle(changes=[change])
        timetree = FakeTimeTree()
        result, _ = await self._run(google, timetree)
        self.assertEqual(result.deferred_delete_count, 1)
        self.assertEqual(timetree.delete_calls, [])

    async def test_normal_delete_prepares_one_idempotent_operation(self) -> None:
        change = event_change({"id": "g-delete-journal", "status": "cancelled"})
        google = FakeGoogle(changes=[change])
        timetree = FakeTimeTree()
        with ensure_database(self.db_path) as connection:
            repo = StateRepository(connection)
            self._seed_token(repo)
            first = await GoogleToTimeTreeRunner(
                google_client=google,
                timetree_client=timetree,
                repository=repo,
                default_timezone="Asia/Tokyo",
            ).run()
            second = await GoogleToTimeTreeRunner(
                google_client=google,
                timetree_client=timetree,
                repository=repo,
                default_timezone="Asia/Tokyo",
            ).run()
            operations = repo.list_operations(
                direction="google_to_timetree",
                action="delete",
            )
            self.assertEqual(repo.get_sync_state("google_sync_token"), "token-old")
        self.assertEqual(first.deferred_delete_count, 1)
        self.assertEqual(second.deferred_delete_count, 1)
        self.assertEqual(len(operations), 1)
        self.assertEqual(operations[0]["state"], "prepared")
        self.assertEqual(timetree.delete_calls, [])

    async def test_mismatched_deferred_delete_journal_safe_stops(self) -> None:
        change = event_change({"id": "g-delete-mismatch", "status": "cancelled"})
        google = FakeGoogle(changes=[change])
        with ensure_database(self.db_path) as connection:
            repo = StateRepository(connection)
            self._seed_token(repo)
            await GoogleToTimeTreeRunner(
                google_client=google,
                timetree_client=FakeTimeTree(),
                repository=repo,
                default_timezone="Asia/Tokyo",
            ).run()
            raw = timed_raw(event_id="tt-delete-mismatch")
            self._seed_mapping(repo, raw, "g-delete-mismatch")
            with self.assertRaises(GoogleToTimeTreeError) as caught:
                await GoogleToTimeTreeRunner(
                    google_client=google,
                    timetree_client=FakeTimeTree([raw]),
                    repository=repo,
                    default_timezone="Asia/Tokyo",
                ).run()
            operation = repo.get_operation(
                "p9:google_to_timetree:delete:g-delete-mismatch"
            )
        self.assertEqual(caught.exception.code, NEEDS_MANUAL_RECOVERY)
        self.assertEqual(operation["state"], "failed")

    def _exception_delete_change(
        self,
        *,
        source_id: str = "g-exception-journal",
        parent_id: str = "g-series",
        original_start: str = "2030-01-01T19:00:00+09:00",
    ) -> Any:
        return event_change(
            {
                "id": source_id,
                "status": "cancelled",
                "recurringEventId": parent_id,
                "originalStartTime": {
                    "dateTime": original_start,
                    "timeZone": "Asia/Tokyo",
                },
            }
        )

    async def test_identical_exception_delete_is_one_prepared_operation(self) -> None:
        change = self._exception_delete_change()
        google = FakeGoogle(changes=[change])
        timetree = FakeTimeTree()
        with ensure_database(self.db_path) as connection:
            repo = StateRepository(connection)
            self._seed_token(repo)
            await GoogleToTimeTreeRunner(
                google_client=google,
                timetree_client=timetree,
                repository=repo,
                default_timezone="Asia/Tokyo",
            ).run()
            await GoogleToTimeTreeRunner(
                google_client=google,
                timetree_client=timetree,
                repository=repo,
                default_timezone="Asia/Tokyo",
            ).run()
            operations = repo.list_operations(
                direction="google_to_timetree",
                action="recurrence_exception_delete",
            )
        self.assertEqual(len(operations), 1)
        self.assertEqual(
            operations[0]["operation_id"],
            "p9:google_to_timetree:recurrence_exception_delete:g-exception-journal",
        )
        self.assertEqual(operations[0]["state"], "prepared")
        self.assertEqual(timetree.delete_calls, [])

    async def test_exception_delete_parent_mismatch_is_manual_recovery(self) -> None:
        first = self._exception_delete_change(parent_id="g-series-a")
        second = self._exception_delete_change(parent_id="g-series-b")
        google = FakeGoogle(changes=[first])
        timetree = FakeTimeTree()
        with ensure_database(self.db_path) as connection:
            repo = StateRepository(connection)
            self._seed_token(repo)
            await GoogleToTimeTreeRunner(
                google_client=google,
                timetree_client=timetree,
                repository=repo,
                default_timezone="Asia/Tokyo",
            ).run()
            google.changes = [second]
            with self.assertRaises(GoogleToTimeTreeError) as caught:
                await GoogleToTimeTreeRunner(
                    google_client=google,
                    timetree_client=timetree,
                    repository=repo,
                    default_timezone="Asia/Tokyo",
                ).run()
            operation = repo.get_operation(
                "p9:google_to_timetree:recurrence_exception_delete:g-exception-journal"
            )
        self.assertEqual(caught.exception.code, NEEDS_MANUAL_RECOVERY)
        self.assertEqual(operation["state"], "failed")
        self.assertEqual(timetree.delete_calls, [])

    async def test_exception_delete_original_start_mismatch_is_manual_recovery(
        self,
    ) -> None:
        first = self._exception_delete_change(
            original_start="2030-01-01T19:00:00+09:00"
        )
        second = self._exception_delete_change(
            original_start="2030-01-08T19:00:00+09:00"
        )
        google = FakeGoogle(changes=[first])
        timetree = FakeTimeTree()
        with ensure_database(self.db_path) as connection:
            repo = StateRepository(connection)
            self._seed_token(repo)
            await GoogleToTimeTreeRunner(
                google_client=google,
                timetree_client=timetree,
                repository=repo,
                default_timezone="Asia/Tokyo",
            ).run()
            google.changes = [second]
            with self.assertRaises(GoogleToTimeTreeError) as caught:
                await GoogleToTimeTreeRunner(
                    google_client=google,
                    timetree_client=timetree,
                    repository=repo,
                    default_timezone="Asia/Tokyo",
                ).run()
            operation = repo.get_operation(
                "p9:google_to_timetree:recurrence_exception_delete:g-exception-journal"
            )
        self.assertEqual(caught.exception.code, NEEDS_MANUAL_RECOVERY)
        self.assertEqual(operation["state"], "failed")
        self.assertEqual(timetree.delete_calls, [])

    async def test_exception_delete_operation_is_distinct_from_normal_delete(
        self,
    ) -> None:
        exception_change = self._exception_delete_change(source_id="g-distinct")
        normal_change = event_change({"id": "g-distinct", "status": "cancelled"})
        google = FakeGoogle(changes=[exception_change, normal_change])
        timetree = FakeTimeTree()
        with ensure_database(self.db_path) as connection:
            repo = StateRepository(connection)
            self._seed_token(repo)
            result = await GoogleToTimeTreeRunner(
                google_client=google,
                timetree_client=timetree,
                repository=repo,
                default_timezone="Asia/Tokyo",
            ).run()
            operations = repo.list_operations(
                direction="google_to_timetree",
            )
        self.assertEqual(result.deferred_delete_count, 2)
        self.assertEqual(
            {operation["action"] for operation in operations},
            {"delete", "recurrence_exception_delete"},
        )
        self.assertEqual(timetree.delete_calls, [])

    async def test_mixed_upsert_and_deferred_delete_does_not_commit_token(self) -> None:
        current = timed_raw(event_id="tt-mixed", title="Old")
        google = FakeGoogle(
            changes=[
                event_change(
                    google_raw(
                        event_id="g-mixed",
                        title="New",
                        managed_timetree_id="tt-mixed",
                    )
                ),
                event_change({"id": "g-mixed-delete", "status": "cancelled"}),
            ]
        )
        timetree = FakeTimeTree([current])
        with ensure_database(self.db_path) as connection:
            repo = StateRepository(connection)
            self._seed_token(repo)
            self._seed_mapping(repo, current, "g-mixed")
            result = await GoogleToTimeTreeRunner(
                google_client=google,
                timetree_client=timetree,
                repository=repo,
                default_timezone="Asia/Tokyo",
            ).run()
            operation = repo.get_operation(
                "p9:google_to_timetree:delete:g-mixed-delete"
            )
            token = repo.get_sync_state("google_sync_token")
        self.assertEqual(result.updated_event_count, 1)
        self.assertFalse(result.token_committed)
        self.assertEqual(token, "token-old")
        self.assertEqual(operation["state"], "prepared")
        self.assertEqual(timetree.delete_calls, [])

    async def test_conflict_guard_stops_update_before_write(self) -> None:
        current = timed_raw(event_id="tt-conflict", title="Old")
        changed = timed_raw(event_id="tt-conflict", title="Changed in TimeTree")
        google = FakeGoogle(
            [
                google_raw(
                    event_id="g-conflict",
                    title="Changed in Google",
                    managed_timetree_id="tt-conflict",
                )
            ]
        )
        timetree = FakeTimeTree([changed])
        timetree.updated_events = [changed]
        with ensure_database(self.db_path) as connection:
            repo = StateRepository(connection)
            self._seed_token(repo)
            self._seed_mapping(repo, current, "g-conflict")
            with self.assertRaises(GoogleToTimeTreeError) as caught:
                await GoogleToTimeTreeRunner(
                    google_client=google,
                    timetree_client=timetree,
                    repository=repo,
                    default_timezone="Asia/Tokyo",
                ).run()
            link = repo.get_event_link_by_google_id("g-conflict")
        self.assertEqual(caught.exception.code, "CONFLICT")
        self.assertEqual(timetree.update_calls, [])
        self.assertEqual(link["status"], "conflict")

    async def test_conflict_guard_ignores_unrelated_out_of_scope_events(self) -> None:
        current = timed_raw(event_id="tt-scoped", title="Old")
        unrelated = timed_raw(
            event_id="tt-birthday",
            title="Birthday",
            label_id=99,
        )
        google = FakeGoogle(
            [
                google_raw(
                    event_id="g-scoped",
                    title="New",
                    managed_timetree_id="tt-scoped",
                )
            ]
        )
        timetree = FakeTimeTree([current, unrelated])
        with ensure_database(self.db_path) as connection:
            repo = StateRepository(connection)
            self._seed_token(repo)
            self._seed_mapping(repo, current, "g-scoped")
            result = await GoogleToTimeTreeRunner(
                google_client=google,
                timetree_client=timetree,
                repository=repo,
                default_timezone="Asia/Tokyo",
            ).run()
        self.assertEqual(result.updated_event_count, 1)
        self.assertEqual(
            [call["event_uuid"] for call in timetree.update_calls],
            ["tt-scoped"],
        )
        self.assertEqual(timetree.events[1]["uuid"], "tt-birthday")

    async def test_conflict_guard_detects_current_change_when_updated_batch_is_empty(
        self,
    ) -> None:
        baseline = timed_raw(event_id="tt-current-race", title="Baseline")
        current = timed_raw(event_id="tt-current-race", title="Current TimeTree")
        google = FakeGoogle(
            [
                google_raw(
                    event_id="g-current-race",
                    title="Google update",
                    managed_timetree_id="tt-current-race",
                )
            ]
        )
        timetree = FakeTimeTree([current])
        timetree.updated_events = []
        with ensure_database(self.db_path) as connection:
            repo = StateRepository(connection)
            self._seed_token(repo)
            self._seed_mapping(repo, baseline, "g-current-race")
            with self.assertRaises(GoogleToTimeTreeError) as caught:
                await GoogleToTimeTreeRunner(
                    google_client=google,
                    timetree_client=timetree,
                    repository=repo,
                    default_timezone="Asia/Tokyo",
                ).run()
        self.assertEqual(caught.exception.code, "CONFLICT")
        self.assertEqual(timetree.update_calls, [])

    async def test_conflict_guard_ignores_reverted_historical_change(self) -> None:
        baseline = timed_raw(event_id="tt-reverted", title="Baseline")
        historical = timed_raw(event_id="tt-reverted", title="Historical change")
        google = FakeGoogle(
            [
                google_raw(
                    event_id="g-reverted",
                    title="Google update",
                    managed_timetree_id="tt-reverted",
                )
            ]
        )
        timetree = FakeTimeTree([baseline])
        timetree.updated_events = [historical]
        with ensure_database(self.db_path) as connection:
            repo = StateRepository(connection)
            self._seed_token(repo)
            self._seed_mapping(repo, baseline, "g-reverted")
            result = await GoogleToTimeTreeRunner(
                google_client=google,
                timetree_client=timetree,
                repository=repo,
                default_timezone="Asia/Tokyo",
            ).run()
        self.assertEqual(result.updated_event_count, 1)
        self.assertEqual(timetree.update_calls[0]["event_uuid"], "tt-reverted")

    async def test_conflict_guard_allows_normal_update_at_baseline_hash(self) -> None:
        baseline = timed_raw(event_id="tt-baseline", title="Baseline")
        google = FakeGoogle(
            [
                google_raw(
                    event_id="g-baseline",
                    title="Google update",
                    managed_timetree_id="tt-baseline",
                )
            ]
        )
        timetree = FakeTimeTree([baseline])
        timetree.updated_events = []
        with ensure_database(self.db_path) as connection:
            repo = StateRepository(connection)
            self._seed_token(repo)
            self._seed_mapping(repo, baseline, "g-baseline")
            result = await GoogleToTimeTreeRunner(
                google_client=google,
                timetree_client=timetree,
                repository=repo,
                default_timezone="Asia/Tokyo",
            ).run()
        self.assertEqual(result.updated_event_count, 1)
        self.assertEqual(timetree.update_calls[0]["event_uuid"], "tt-baseline")

    async def test_p8_bootstrapped_state_remains_intact(self) -> None:
        google = FakeGoogle(changes=[], next_token="token-after-empty")
        timetree = FakeTimeTree()
        with ensure_database(self.db_path) as connection:
            repo = StateRepository(connection)
            self._seed_token(repo, "p8-token")
            repo.set_sync_state("bridge_bootstrapped_at", "2030-01-01T00:00:00+00:00")
            for index in range(90):
                repo.create_event_link(
                    timetree_event_id=f"tt-p8-{index}",
                    google_event_id=f"g-p8-{index}",
                    last_synced_hash=f"hash-{index}",
                    status="synced",
                )
                repo.create_operation(
                    operation_id=f"p8:operation:{index}",
                    direction="timetree_to_google",
                    action="create",
                    source_event_id=f"tt-p8-{index}",
                )
                repo.transition_operation(
                    f"p8:operation:{index}",
                    "remote_applied",
                    target_event_id=f"g-p8-{index}",
                )
                repo.transition_operation(f"p8:operation:{index}", "mapping_saved")
                repo.transition_operation(f"p8:operation:{index}", "done")
            result = await GoogleToTimeTreeRunner(
                google_client=google,
                timetree_client=timetree,
                repository=repo,
                default_timezone="Asia/Tokyo",
            ).run()
            counts = {
                "links": connection.execute(
                    "SELECT COUNT(*) FROM event_links"
                ).fetchone()[0],
                "operations": connection.execute(
                    "SELECT COUNT(*) FROM sync_operations"
                ).fetchone()[0],
            }
            bootstrap_time = repo.get_sync_state("bridge_bootstrapped_at")
        self.assertTrue(result.token_committed)
        self.assertEqual(counts, {"links": 90, "operations": 90})
        self.assertEqual(bootstrap_time, "2030-01-01T00:00:00+00:00")


async def _failing_update(*args: Any, **kwargs: Any):
    raise OSError("fake TimeTree update failure")


class RunLockIntegrationTests(unittest.TestCase):
    def test_p9_execution_path_reuses_existing_run_lock(self) -> None:
        from bridge.cli import run_sync
        from tests.p2.test_foundation import write_config

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = write_config(root)
            lock_path = default_lock_path(root)
            with run_lock(lock_path), self.assertRaises(RunLockHeldError):
                run_sync(
                    config,
                    google_client=FakeGoogle(),
                    timetree_client=FakeTimeTree(),
                )


if __name__ == "__main__":
    unittest.main()
