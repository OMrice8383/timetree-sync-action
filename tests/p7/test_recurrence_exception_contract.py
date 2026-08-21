from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date, datetime
from zoneinfo import ZoneInfo

from bridge.adapters import (
    UnsupportedEventError,
    normalize_google_event,
    normalize_timetree_event,
)
from bridge.google_client import (
    GoogleClientError,
    google_event_body,
    google_event_change,
)
from bridge.models import (
    ChangeType,
    EventKind,
    NormalizedEvent,
    Source,
    TimeTreeLabelCatalog,
)
from bridge.timetree_client import (
    TimeTreeMCPClient,
    TimeTreeWriteGateError,
    timetree_event_body,
    timetree_update_body,
)

TEST_LABEL_CATALOG = TimeTreeLabelCatalog.from_mapping({3: "大河予定"})


def _timed_single(*, source: Source = Source.GOOGLE) -> NormalizedEvent:
    return NormalizedEvent(
        source=source,
        source_calendar_id="p7-calendar",
        source_event_id="p7-event",
        kind=EventKind.SINGLE,
        parent_source_event_id=None,
        original_start=None,
        title="P7 fixture event",
        all_day=False,
        start=datetime(2030, 2, 12, 10, 0, tzinfo=ZoneInfo("Asia/Tokyo")),
        end=datetime(2030, 2, 12, 10, 30, tzinfo=ZoneInfo("Asia/Tokyo")),
        start_timezone="Asia/Tokyo",
        end_timezone="Asia/Tokyo",
        description=None,
        location=None,
    )


def _google_exception(*, moved: bool = True) -> dict:
    return {
        "id": "google-p7-instance",
        "status": "confirmed",
        "eventType": "default",
        "summary": "P7 exception fixture",
        "start": {
            "dateTime": (
                "2030-02-12T10:00:00+09:00" if moved else "2030-02-05T10:00:00+09:00"
            ),
            "timeZone": "Asia/Tokyo",
        },
        "end": {
            "dateTime": (
                "2030-02-12T10:30:00+09:00" if moved else "2030-02-05T10:30:00+09:00"
            ),
            "timeZone": "Asia/Tokyo",
        },
        "recurringEventId": "google-p7-master",
        "originalStartTime": {
            "dateTime": "2030-02-05T10:00:00+09:00",
            "timeZone": "Asia/Tokyo",
        },
    }


def _timetree_event_with_relation(field: str) -> dict:
    raw = {
        "uuid": "timetree-p7-child",
        "calendar_id": 123,
        "title": "P7 TimeTree fixture",
        "all_day": False,
        "start_at": 1894060800000,
        "end_at": 1894062600000,
        "start_timezone": "UTC",
        "end_timezone": "UTC",
        "category": 1,
        "type": 0,
        "label_id": 3,
        "recurrences": [],
        "deactivated_at": None,
        "parent_id": None,
        "recurring_uuid": None,
    }
    raw[field] = 987654 if field == "parent_id" else "timetree-p7-master"
    return raw


class _NoCallClient:
    async def call_tool(self, name: str, arguments: dict) -> None:
        raise AssertionError(f"unexpected TimeTree write call: {name}")


class ExceptionContractTests(unittest.TestCase):
    def test_normalized_exception_requires_parent_and_original_start(self) -> None:
        single = _timed_single()
        valid = replace(
            single,
            kind=EventKind.EXCEPTION,
            parent_source_event_id="master-id",
            original_start=single.start,
        )
        self.assertIs(valid.kind, EventKind.EXCEPTION)

        for field, value in (
            ("parent_source_event_id", None),
            ("original_start", None),
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                replace(valid, **{field: value})

    def test_timetree_parent_id_or_recurring_uuid_fails_safe_independently(
        self,
    ) -> None:
        for relation_field in ("parent_id", "recurring_uuid"):
            with self.subTest(relation_field=relation_field):
                with self.assertRaises(UnsupportedEventError) as caught:
                    normalize_timetree_event(
                        _timetree_event_with_relation(relation_field),
                        default_timezone="Asia/Tokyo",
                        label_catalog=TEST_LABEL_CATALOG,
                    )
                self.assertEqual(
                    caught.exception.code,
                    "UNSUPPORTED_RECURRENCE_EXCEPTION",
                )

    def test_google_normal_exception_preserves_parent_and_original_start(self) -> None:
        event = normalize_google_event(
            _google_exception(moved=False),
            source_calendar_id="google-calendar",
            default_timezone="Asia/Tokyo",
        )
        self.assertIs(event.kind, EventKind.EXCEPTION)
        self.assertEqual(event.parent_source_event_id, "google-p7-master")
        self.assertEqual(
            event.original_start,
            datetime(2030, 2, 5, 10, 0, tzinfo=ZoneInfo("Asia/Tokyo")),
        )

    def test_google_moved_occurrence_keeps_actual_and_original_start_separate(
        self,
    ) -> None:
        event = normalize_google_event(
            _google_exception(moved=True),
            source_calendar_id="google-calendar",
            default_timezone="Asia/Tokyo",
        )
        self.assertEqual(
            event.start,
            datetime(2030, 2, 12, 10, 0, tzinfo=ZoneInfo("Asia/Tokyo")),
        )
        self.assertEqual(
            event.original_start,
            datetime(2030, 2, 5, 10, 0, tzinfo=ZoneInfo("Asia/Tokyo")),
        )
        self.assertNotEqual(event.start, event.original_start)

    def test_google_exception_without_original_start_fails_safe(self) -> None:
        raw = _google_exception()
        raw.pop("originalStartTime")
        with self.assertRaises(UnsupportedEventError) as caught:
            normalize_google_event(
                raw,
                source_calendar_id="google-calendar",
                default_timezone="Asia/Tokyo",
            )
        self.assertEqual(
            caught.exception.code,
            "UNSUPPORTED_GOOGLE_RECURRENCE_EXCEPTION",
        )

    def test_google_cancelled_exception_requires_and_preserves_identity(self) -> None:
        change = google_event_change(
            {
                "id": "google-p7-cancelled-instance",
                "status": "cancelled",
                "recurringEventId": "google-p7-master",
                "originalStartTime": {"date": "2030-02-05"},
            },
            source_calendar_id="google-calendar",
            default_timezone="Asia/Tokyo",
        )
        self.assertIs(change.change_type, ChangeType.RECURRENCE_EXCEPTION_DELETE)
        self.assertEqual(change.source_event_id, "google-p7-cancelled-instance")
        self.assertEqual(change.parent_source_event_id, "google-p7-master")
        self.assertEqual(change.original_start, date(2030, 2, 5))

        with self.assertRaises(UnsupportedEventError):
            google_event_change(
                {
                    "id": "google-p7-cancelled-instance",
                    "status": "cancelled",
                    "recurringEventId": "google-p7-master",
                },
                source_calendar_id="google-calendar",
                default_timezone="Asia/Tokyo",
            )


class ExceptionWriteGateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        single = _timed_single(source=Source.TIMETREE)
        self.exception = replace(
            single,
            kind=EventKind.EXCEPTION,
            parent_source_event_id="timetree-p7-master",
            original_start=single.start,
        )

    def test_google_exception_write_stays_closed_with_p6_gate(self) -> None:
        with self.assertRaises(GoogleClientError):
            google_event_body(self.exception, allow_recurrence_write=True)

    async def test_timetree_exception_create_update_delete_stay_closed_with_p6_gate(
        self,
    ) -> None:
        with self.assertRaises(TimeTreeWriteGateError):
            timetree_event_body(
                self.exception,
                calendar_id="123",
                default_timezone="Asia/Tokyo",
                allow_recurrence_write=True,
            )

        with self.assertRaises(TimeTreeWriteGateError):
            timetree_update_body(
                self.exception,
                fields={"start"},
                calendar_id="123",
                default_timezone="Asia/Tokyo",
                allow_recurrence_write=True,
            )

        client = TimeTreeMCPClient(
            _NoCallClient(),
            calendar_id="123",
            default_timezone="Asia/Tokyo",
        )
        with self.assertRaises(TimeTreeWriteGateError):
            await client.create_event(
                self.exception,
                allow_recurrence_write=True,
            )
        with self.assertRaises(TimeTreeWriteGateError):
            await client.update_event(
                "timetree-p7-child",
                self.exception,
                fields={"label"},
                allow_recurrence_write=True,
            )
        with self.assertRaises(TimeTreeWriteGateError):
            await client.delete_event(
                "timetree-p7-child",
                event_kind=EventKind.EXCEPTION,
                allow_recurrence_write=True,
            )


if __name__ == "__main__":
    unittest.main()
