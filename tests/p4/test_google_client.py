from __future__ import annotations

import unittest
from copy import deepcopy
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from bridge.adapters import UnsupportedEventError
from bridge.google_client import (
    FORBIDDEN_INCREMENTAL_PARAMETERS,
    GOOGLE_SYNC_QUERY,
    FullResyncRequired,
    GoogleCalendarClient,
    GoogleClientError,
    GoogleProtocolError,
    GoogleQueryContractError,
    google_event_body,
    google_event_change,
    validate_google_sync_query,
)
from bridge.models import ChangeType, EventKind, NormalizedEvent, Recurrence, Source


class FakeRequest:
    def __init__(self, value=None, error: BaseException | None = None):
        self.value = value
        self.error = error

    def execute(self):
        if self.error is not None:
            raise self.error
        return deepcopy(self.value)


class FakeHttpError(RuntimeError):
    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.resp = SimpleNamespace(status=status)


class FakeEventsResource:
    def __init__(self, list_results=None):
        self.list_results = list(list_results or [])
        self.list_calls: list[dict] = []
        self.get_calls: list[dict] = []
        self.insert_calls: list[dict] = []
        self.patch_calls: list[dict] = []
        self.delete_calls: list[dict] = []
        self.update_calls: list[dict] = []
        self.stored: dict[str, dict] = {}
        self.next_id = 1

    def list(self, **kwargs):
        self.list_calls.append(deepcopy(kwargs))
        if not self.list_results:
            raise AssertionError("unexpected events.list call")
        value = self.list_results.pop(0)
        if isinstance(value, BaseException):
            return FakeRequest(error=value)
        return FakeRequest(value)

    def get(self, **kwargs):
        self.get_calls.append(deepcopy(kwargs))
        return FakeRequest(self.stored[kwargs["eventId"]])

    def insert(self, **kwargs):
        self.insert_calls.append(deepcopy(kwargs))
        event_id = f"fake-{self.next_id}"
        self.next_id += 1
        stored = deepcopy(kwargs["body"])
        stored["id"] = event_id
        stored.setdefault("status", "confirmed")
        stored.setdefault("eventType", "default")
        self.stored[event_id] = stored
        return FakeRequest(stored)

    def patch(self, **kwargs):
        self.patch_calls.append(deepcopy(kwargs))
        event_id = kwargs["eventId"]
        self.stored[event_id].update(deepcopy(kwargs["body"]))
        return FakeRequest(self.stored[event_id])

    def update(self, **kwargs):
        self.update_calls.append(deepcopy(kwargs))
        raise AssertionError("events.update must not be used by the bridge")

    def delete(self, **kwargs):
        self.delete_calls.append(deepcopy(kwargs))
        self.stored.pop(kwargs["eventId"], None)
        return FakeRequest(None)


class FakeCalendarsResource:
    def __init__(self, metadata=None):
        self.metadata = metadata or {
            "id": "calendar-id",
            "summary": "TimeTree Bridge",
            "timeZone": "Asia/Tokyo",
        }
        self.get_calls: list[dict] = []

    def get(self, **kwargs):
        self.get_calls.append(deepcopy(kwargs))
        return FakeRequest(self.metadata)


class FakeService:
    def __init__(self, *, list_results=None, metadata=None):
        self.events_resource = FakeEventsResource(list_results)
        self.calendars_resource = FakeCalendarsResource(metadata)

    def events(self):
        return self.events_resource

    def calendars(self):
        return self.calendars_resource


def google_single(event_id: str = "g-single") -> dict:
    return {
        "id": event_id,
        "status": "confirmed",
        "eventType": "default",
        "summary": "Fixture single",
        "description": "Description",
        "location": "Location",
        "start": {
            "dateTime": "2030-02-01T10:00:00+09:00",
            "timeZone": "Asia/Tokyo",
        },
        "end": {
            "dateTime": "2030-02-01T11:00:00+09:00",
            "timeZone": "Asia/Tokyo",
        },
    }


def google_series(event_id: str = "g-series") -> dict:
    raw = google_single(event_id)
    raw["recurrence"] = ["RRULE:FREQ=WEEKLY;COUNT=3;BYDAY=TU"]
    return raw


def normalized_single(*, all_day: bool = False) -> NormalizedEvent:
    if all_day:
        return NormalizedEvent(
            source=Source.TIMETREE,
            source_calendar_id="tt-calendar",
            source_event_id="tt-1",
            kind=EventKind.SINGLE,
            parent_source_event_id=None,
            original_start=None,
            title="Fixture",
            all_day=True,
            start=date(2030, 2, 1),
            end=date(2030, 2, 2),
            start_timezone=None,
            end_timezone=None,
            description=None,
            location=None,
        )

    return NormalizedEvent(
        source=Source.TIMETREE,
        source_calendar_id="tt-calendar",
        source_event_id="tt-1",
        kind=EventKind.SINGLE,
        parent_source_event_id=None,
        original_start=None,
        title="Fixture",
        all_day=False,
        start=datetime(2030, 2, 1, 10, 0, tzinfo=ZoneInfo("Asia/Tokyo")),
        end=datetime(2030, 2, 1, 11, 0, tzinfo=ZoneInfo("Asia/Tokyo")),
        start_timezone="Asia/Tokyo",
        end_timezone="Asia/Tokyo",
        description="Description",
        location="Location",
    )


class QueryContractTests(unittest.TestCase):
    def test_fixed_query_contract(self):
        self.assertEqual(
            GOOGLE_SYNC_QUERY,
            {
                "singleEvents": False,
                "showDeleted": True,
                "eventTypes": "default",
                "maxResults": 2500,
            },
        )
        validate_google_sync_query(GOOGLE_SYNC_QUERY)

    def test_incremental_forbidden_parameters_are_rejected(self):
        for name in FORBIDDEN_INCREMENTAL_PARAMETERS:
            params = dict(GOOGLE_SYNC_QUERY)
            params["syncToken"] = "token"
            params[name] = "forbidden"
            with (
                self.subTest(name=name),
                self.assertRaises(GoogleQueryContractError),
            ):
                validate_google_sync_query(params)

    def test_semantic_query_drift_is_rejected(self):
        bad_single = dict(GOOGLE_SYNC_QUERY, singleEvents=True)
        bad_deleted = dict(GOOGLE_SYNC_QUERY, showDeleted=False)
        bad_type = dict(GOOGLE_SYNC_QUERY, eventTypes="birthday")
        for params in (bad_single, bad_deleted, bad_type):
            with self.assertRaises(GoogleQueryContractError):
                validate_google_sync_query(params)


class ChangeParsingTests(unittest.TestCase):
    def test_regular_event_becomes_upsert(self):
        change = google_event_change(
            google_single(),
            source_calendar_id="calendar-id",
            default_timezone="Asia/Tokyo",
        )
        self.assertIs(change.change_type, ChangeType.UPSERT)
        self.assertEqual(change.source_event_id, "g-single")
        self.assertEqual(change.event.kind, EventKind.SINGLE)

    def test_recurring_master_is_preserved_as_series(self):
        change = google_event_change(
            google_series(),
            source_calendar_id="calendar-id",
            default_timezone="Asia/Tokyo",
        )
        self.assertIs(change.change_type, ChangeType.UPSERT)
        self.assertEqual(change.event.kind, EventKind.SERIES)

    def test_id_only_cancelled_event_becomes_delete(self):
        change = google_event_change(
            {"id": "deleted-id", "status": "cancelled"},
            source_calendar_id="calendar-id",
            default_timezone="Asia/Tokyo",
        )
        self.assertIs(change.change_type, ChangeType.DELETE)
        self.assertEqual(change.source_event_id, "deleted-id")
        self.assertIsNone(change.event)

    def test_cancelled_recurring_exception_keeps_identity(self):
        change = google_event_change(
            {
                "id": "instance-id",
                "status": "cancelled",
                "recurringEventId": "master-id",
                "originalStartTime": {"date": "2030-02-05"},
            },
            source_calendar_id="calendar-id",
            default_timezone="Asia/Tokyo",
        )
        self.assertIs(
            change.change_type,
            ChangeType.RECURRENCE_EXCEPTION_DELETE,
        )
        self.assertEqual(change.parent_source_event_id, "master-id")
        self.assertEqual(change.original_start, date(2030, 2, 5))

    def test_partial_cancelled_exception_fails_safe(self):
        with self.assertRaises(UnsupportedEventError) as captured:
            google_event_change(
                {
                    "id": "instance-id",
                    "status": "cancelled",
                    "recurringEventId": "master-id",
                },
                source_calendar_id="calendar-id",
                default_timezone="Asia/Tokyo",
            )
        self.assertEqual(
            captured.exception.code,
            "UNSUPPORTED_GOOGLE_CANCELLED_EXCEPTION",
        )


class PaginationTests(unittest.TestCase):
    def test_full_sync_paginates_and_returns_only_final_sync_token(self):
        service = FakeService(
            list_results=[
                {"items": [google_single("g-1")], "nextPageToken": "page-2"},
                {"items": [google_single("g-2")], "nextSyncToken": "sync-new"},
            ]
        )
        client = GoogleCalendarClient(
            service,
            calendar_id="calendar-id",
            default_timezone="Asia/Tokyo",
        )

        result = client.list_changes()

        self.assertEqual([item.source_event_id for item in result.changes], ["g-1", "g-2"])
        self.assertEqual(result.next_sync_token, "sync-new")
        first, second = service.events_resource.list_calls
        self.assertEqual(first["calendarId"], "calendar-id")
        self.assertNotIn("syncToken", first)
        self.assertNotIn("pageToken", first)
        self.assertEqual(second["pageToken"], "page-2")
        for key, value in GOOGLE_SYNC_QUERY.items():
            self.assertEqual(first[key], value)
            self.assertEqual(second[key], value)

    def test_incremental_pagination_keeps_sync_token_and_contract(self):
        service = FakeService(
            list_results=[
                {"items": [], "nextPageToken": "page-2"},
                {"items": [], "nextSyncToken": "sync-next"},
            ]
        )
        client = GoogleCalendarClient(
            service,
            calendar_id="calendar-id",
            default_timezone="Asia/Tokyo",
        )

        result = client.list_changes(sync_token="sync-old")

        self.assertEqual(result.next_sync_token, "sync-next")
        first, second = service.events_resource.list_calls
        self.assertEqual(first["syncToken"], "sync-old")
        self.assertEqual(second["syncToken"], "sync-old")
        self.assertNotIn("pageToken", first)
        self.assertEqual(second["pageToken"], "page-2")
        for key, value in GOOGLE_SYNC_QUERY.items():
            self.assertEqual(first[key], value)
            self.assertEqual(second[key], value)

    def test_410_on_incremental_is_full_resync_signal(self):
        service = FakeService(list_results=[FakeHttpError(410)])
        client = GoogleCalendarClient(
            service,
            calendar_id="calendar-id",
            default_timezone="Asia/Tokyo",
        )
        with self.assertRaises(FullResyncRequired) as captured:
            client.list_changes(sync_token="expired")
        self.assertEqual(captured.exception.code, "FULL_RESYNC_REQUIRED")

    def test_final_page_requires_next_sync_token(self):
        service = FakeService(list_results=[{"items": []}])
        client = GoogleCalendarClient(
            service,
            calendar_id="calendar-id",
            default_timezone="Asia/Tokyo",
        )
        with self.assertRaises(GoogleProtocolError):
            client.list_changes()


class WriteBoundaryTests(unittest.TestCase):
    def test_timed_body_keeps_start_and_end_effective_timezones(self):
        body = google_event_body(normalized_single())
        self.assertEqual(body["start"]["timeZone"], "Asia/Tokyo")
        self.assertEqual(body["end"]["timeZone"], "Asia/Tokyo")
        self.assertEqual(body["start"]["dateTime"], "2030-02-01T10:00:00+09:00")
        self.assertEqual(body["end"]["dateTime"], "2030-02-01T11:00:00+09:00")

    def test_all_day_body_uses_exclusive_dates_without_timezone(self):
        body = google_event_body(normalized_single(all_day=True))
        self.assertEqual(body["start"], {"date": "2030-02-01"})
        self.assertEqual(body["end"], {"date": "2030-02-02"})

    def test_recurrence_write_is_gated_until_p6(self):
        event = normalized_single()
        series = NormalizedEvent(
            source=event.source,
            source_calendar_id=event.source_calendar_id,
            source_event_id=event.source_event_id,
            kind=EventKind.SERIES,
            parent_source_event_id=None,
            original_start=None,
            title=event.title,
            all_day=event.all_day,
            start=event.start,
            end=event.end,
            start_timezone=event.start_timezone,
            end_timezone=event.end_timezone,
            description=event.description,
            location=event.location,
            recurrence=Recurrence(("RRULE:FREQ=DAILY;COUNT=3",)),
        )
        with self.assertRaises(GoogleClientError):
            google_event_body(series)

    def test_insert_get_patch_delete_and_metadata_use_expected_methods(self):
        service = FakeService()
        client = GoogleCalendarClient(
            service,
            calendar_id="calendar-id",
            default_timezone="Asia/Tokyo",
        )
        initial_body = google_event_body(normalized_single())
        initial_body["attendees"] = [{"email": "other@example.test"}]
        initial_body["reminders"] = {"useDefault": True}
        created = client.insert_event(initial_body)
        event_id = created["id"]

        patch_body = google_event_body(normalized_single())
        patch_body["summary"] = "Updated"
        patched = client.patch_event(event_id, patch_body)

        self.assertEqual(patched["summary"], "Updated")
        self.assertEqual(
            patched["attendees"],
            [{"email": "other@example.test"}],
        )
        self.assertEqual(patched["reminders"], {"useDefault": True})
        self.assertEqual(service.events_resource.update_calls, [])

        fetched = client.get_event(event_id)
        self.assertEqual(fetched["id"], event_id)
        metadata = client.get_calendar_metadata()
        self.assertEqual(metadata["timeZone"], "Asia/Tokyo")
        client.delete_event(event_id)
        self.assertNotIn(event_id, service.events_resource.stored)

    def test_private_metadata_body_contains_only_explicit_bridge_properties(self):
        body = google_event_body(
            normalized_single(),
            private_properties={
                "sync_source": "timetree-chatgpt-bridge",
                "timetree_id": "tt-uuid",
                "bridge_version": "0.1",
            },
        )
        self.assertEqual(
            body["extendedProperties"]["private"],
            {
                "sync_source": "timetree-chatgpt-bridge",
                "timetree_id": "tt-uuid",
                "bridge_version": "0.1",
                "timetree_label_name": "大河予定",
            },
        )


if __name__ == "__main__":
    unittest.main()
