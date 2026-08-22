from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from bridge.adapters import (
    UnsupportedEventError,
    normalize_google_event,
    normalize_timetree_event,
)
from bridge.canonical import canonicalize_recurrence
from bridge.google_client import GoogleClientError, google_event_body
from bridge.models import (
    EventKind,
    NormalizedEvent,
    Recurrence,
    Source,
    TimeTreeLabelCatalog,
)
from bridge.recurrence import RecurrenceContractError, validate_recurrence_lines
from bridge.timetree_client import (
    TimeTreeMCPClient,
    TimeTreeWriteGateError,
    timetree_event_body,
    timetree_update_body,
)

TEST_LABEL_CATALOG = TimeTreeLabelCatalog.from_mapping({3: "大河予定", 10: "共通予定"})


class TextBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


def result(payload: dict) -> SimpleNamespace:
    return SimpleNamespace(
        content=[TextBlock(json.dumps(payload))],
        structured_content=None,
        is_error=False,
    )


class FakeClient:
    def __init__(self, responses: list[SimpleNamespace]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name: str, arguments: dict) -> SimpleNamespace:
        self.calls.append((name, arguments))
        return self.responses.pop(0)


def timed_series(*, lines: tuple[str, ...] | None = None) -> NormalizedEvent:
    return NormalizedEvent(
        source=Source.TIMETREE,
        source_calendar_id="tt-calendar",
        source_event_id="tt-series",
        kind=EventKind.SERIES,
        parent_source_event_id=None,
        original_start=None,
        title="P6 timed series",
        all_day=False,
        start=datetime(2030, 2, 5, 10, 0, tzinfo=ZoneInfo("Asia/Tokyo")),
        end=datetime(2030, 2, 5, 10, 30, tzinfo=ZoneInfo("Asia/Tokyo")),
        start_timezone="Asia/Tokyo",
        end_timezone="Asia/Tokyo",
        description=None,
        location=None,
        recurrence=Recurrence(
            lines
            or (
                "RRULE:FREQ=WEEKLY;INTERVAL=2;COUNT=4;BYDAY=TU",
                "EXDATE:20300219T010000Z",
            )
        ),
    )


def all_day_series() -> NormalizedEvent:
    return NormalizedEvent(
        source=Source.TIMETREE,
        source_calendar_id="tt-calendar",
        source_event_id="tt-all-day-series",
        kind=EventKind.SERIES,
        parent_source_event_id=None,
        original_start=None,
        title="P6 all-day series",
        all_day=True,
        start=date(2030, 2, 5),
        end=date(2030, 2, 6),
        start_timezone=None,
        end_timezone=None,
        description=None,
        location=None,
        recurrence=Recurrence(
            (
                "RRULE:FREQ=WEEKLY;COUNT=3;BYDAY=TU",
                "EXDATE;VALUE=DATE:20300212",
            )
        ),
    )


class RecurrenceContractTests(unittest.TestCase):
    def test_parameterized_exdate_is_recognized_by_normalizer(self) -> None:
        raw = {
            "id": "google-all-day-series",
            "status": "confirmed",
            "eventType": "default",
            "summary": "All-day series",
            "start": {"date": "2030-02-05"},
            "end": {"date": "2030-02-06"},
            "recurrence": [
                "RRULE:FREQ=WEEKLY;COUNT=3;BYDAY=TU",
                "EXDATE;VALUE=DATE:20300212",
            ],
        }
        event = normalize_google_event(
            raw,
            source_calendar_id="google-calendar",
            default_timezone="Asia/Tokyo",
        )
        self.assertIs(event.kind, EventKind.SERIES)
        self.assertIn("EXDATE;VALUE=DATE:20300212", event.recurrence.lines)

    def test_weekly_interval_count_until_contract(self) -> None:
        counted = validate_recurrence_lines(
            ("RRULE:FREQ=WEEKLY;INTERVAL=2;COUNT=4;BYDAY=TU,TH",),
            all_day=False,
            timezone="Asia/Tokyo",
        )
        until_timed = validate_recurrence_lines(
            ("RRULE:FREQ=WEEKLY;UNTIL=20301231T145959Z;BYDAY=TU",),
            all_day=False,
            timezone="Asia/Tokyo",
        )
        until_all_day = validate_recurrence_lines(
            ("RRULE:FREQ=WEEKLY;UNTIL=20301231;BYDAY=TU",),
            all_day=True,
            timezone=None,
        )
        self.assertEqual(len(counted), 1)
        self.assertEqual(len(until_timed), 1)
        self.assertEqual(len(until_all_day), 1)

    def test_nonweekly_and_unconfirmed_rrule_keys_fail_safe(self) -> None:
        for line in (
            "RRULE:FREQ=DAILY;COUNT=3",
            "RRULE:FREQ=WEEKLY;BYMONTH=2",
            "RRULE:FREQ=WEEKLY;INTERVAL=0",
        ):
            with (
                self.subTest(line=line),
                self.assertRaises(RecurrenceContractError),
            ):
                validate_recurrence_lines(
                    (line,),
                    all_day=False,
                    timezone="Asia/Tokyo",
                )

    def test_yearly_contract_is_exact_all_day_only(self) -> None:
        self.assertEqual(
            validate_recurrence_lines(
                ("RRULE:FREQ=YEARLY",),
                all_day=True,
                timezone=None,
            ),
            ("RRULE:FREQ=YEARLY",),
        )

        unsupported_all_day = (
            "RRULE:FREQ=YEARLY;INTERVAL=2",
            "RRULE:FREQ=YEARLY;COUNT=2",
            "RRULE:FREQ=YEARLY;UNTIL=20301231",
            "RRULE:FREQ=YEARLY;BYDAY=TU",
            "RRULE:FREQ=YEARLY;BYMONTH=2",
            "RRULE:FREQ=YEARLY;BYMONTHDAY=2",
            "RRULE:FREQ=YEARLY;INTERVAL=1",
        )
        for line in unsupported_all_day:
            with (
                self.subTest(line=line),
                self.assertRaises(RecurrenceContractError),
            ):
                validate_recurrence_lines(
                    (line,),
                    all_day=True,
                    timezone=None,
                )

        with self.assertRaises(RecurrenceContractError):
            validate_recurrence_lines(
                ("RRULE:FREQ=YEARLY",),
                all_day=False,
                timezone="Asia/Tokyo",
            )
        with self.assertRaises(RecurrenceContractError):
            validate_recurrence_lines(
                (
                    "RRULE:FREQ=YEARLY",
                    "EXDATE;VALUE=DATE:20300212",
                ),
                all_day=True,
                timezone=None,
            )

    def test_google_and_timetree_yearly_normalization_has_same_canonical_rule(
        self,
    ) -> None:
        google = normalize_google_event(
            {
                "id": "google-yearly-series",
                "status": "confirmed",
                "eventType": "default",
                "summary": "Yearly series",
                "start": {"date": "2030-01-15"},
                "end": {"date": "2030-01-16"},
                "recurrence": ["RRULE:FREQ=YEARLY"],
            },
            source_calendar_id="google-calendar",
            default_timezone="Asia/Tokyo",
        )
        timetree = normalize_timetree_event(
            {
                "uuid": "timetree-yearly-series",
                "calendar_id": 123,
                "title": "Yearly series",
                "all_day": True,
                "start_at": 1894665600000,
                "start_timezone": "UTC",
                "end_at": 1894665600000,
                "end_timezone": "UTC",
                "category": 1,
                "type": 0,
                "label_id": 10,
                "recurrences": ["RRULE:FREQ=YEARLY"],
            },
            default_timezone="Asia/Tokyo",
            label_catalog=TEST_LABEL_CATALOG,
        )
        self.assertEqual(google.recurrence.lines, timetree.recurrence.lines)
        self.assertEqual(
            canonicalize_recurrence(google.recurrence.lines),
            ("RRULE:FREQ=YEARLY",),
        )

    def test_exdate_forms_are_context_safe(self) -> None:
        validate_recurrence_lines(
            (
                "RRULE:FREQ=WEEKLY;COUNT=3;BYDAY=TU",
                "EXDATE:20300212T010000Z",
            ),
            all_day=False,
            timezone="Asia/Tokyo",
        )
        validate_recurrence_lines(
            (
                "RRULE:FREQ=WEEKLY;COUNT=3;BYDAY=TU",
                "EXDATE;TZID=Asia/Tokyo:20300212T100000",
            ),
            all_day=False,
            timezone="Asia/Tokyo",
        )
        validate_recurrence_lines(
            (
                "RRULE:FREQ=WEEKLY;COUNT=3;BYDAY=TU",
                "EXDATE;VALUE=DATE:20300212",
            ),
            all_day=True,
            timezone=None,
        )
        with self.assertRaises(RecurrenceContractError):
            validate_recurrence_lines(
                (
                    "RRULE:FREQ=WEEKLY;COUNT=3;BYDAY=TU",
                    "EXDATE;TZID=America/Los_Angeles:20300212T100000",
                ),
                all_day=False,
                timezone="Asia/Tokyo",
            )
        with self.assertRaises(RecurrenceContractError):
            validate_recurrence_lines(
                (
                    "RRULE:FREQ=WEEKLY;COUNT=3;BYDAY=TU",
                    "EXDATE:20300212",
                ),
                all_day=True,
                timezone=None,
            )

    def test_rdate_and_exrule_remain_unsupported(self) -> None:
        for line in (
            "RDATE:20300212T010000Z",
            "EXRULE:FREQ=WEEKLY;COUNT=2",
        ):
            with (
                self.subTest(line=line),
                self.assertRaises(RecurrenceContractError),
            ):
                validate_recurrence_lines(
                    (
                        "RRULE:FREQ=WEEKLY;COUNT=3;BYDAY=TU",
                        line,
                    ),
                    all_day=False,
                    timezone="Asia/Tokyo",
                )

    def test_normalizer_rejects_nonweekly_rrule(self) -> None:
        raw = {
            "id": "google-daily-series",
            "status": "confirmed",
            "eventType": "default",
            "summary": "Daily series",
            "start": {
                "dateTime": "2030-02-05T10:00:00+09:00",
                "timeZone": "Asia/Tokyo",
            },
            "end": {
                "dateTime": "2030-02-05T10:30:00+09:00",
                "timeZone": "Asia/Tokyo",
            },
            "recurrence": ["RRULE:FREQ=DAILY;COUNT=3"],
        }
        with self.assertRaises(UnsupportedEventError) as caught:
            normalize_google_event(
                raw,
                source_calendar_id="google-calendar",
                default_timezone="Asia/Tokyo",
            )
        self.assertEqual(caught.exception.code, "UNSUPPORTED_RECURRENCE_FEATURE")


class GoogleSeriesWriteTests(unittest.TestCase):
    def test_series_create_body_requires_gate_and_emits_validated_rules(self) -> None:
        series = timed_series()
        with self.assertRaises(GoogleClientError):
            google_event_body(series)

        body = google_event_body(series, allow_recurrence_write=True)
        self.assertEqual(len(body["recurrence"]), 2)
        self.assertTrue(any(line.startswith("RRULE:") for line in body["recurrence"]))
        self.assertTrue(any(line.startswith("EXDATE:") for line in body["recurrence"]))

    def test_recurrence_removal_is_explicit(self) -> None:
        target = replace(
            timed_series(),
            kind=EventKind.SINGLE,
            recurrence=Recurrence(),
        )
        body = google_event_body(
            target,
            allow_recurrence_write=True,
            clear_recurrence=True,
        )
        self.assertEqual(body["recurrence"], [])
        with self.assertRaises(GoogleClientError):
            google_event_body(target, clear_recurrence=True)

    def test_exception_write_stays_closed_even_when_p6_gate_is_open(self) -> None:
        single = replace(
            timed_series(),
            kind=EventKind.SINGLE,
            recurrence=Recurrence(),
        )
        exception = replace(
            single,
            kind=EventKind.EXCEPTION,
            parent_source_event_id="google-master",
            original_start=single.start,
        )
        with self.assertRaises(GoogleClientError):
            google_event_body(exception, allow_recurrence_write=True)

    def test_manual_unsupported_recurrence_cannot_bypass_normalizer(self) -> None:
        unsupported = timed_series(lines=("RDATE:20300212T010000Z",))
        with self.assertRaises(GoogleClientError):
            google_event_body(unsupported, allow_recurrence_write=True)

    def test_all_day_series_body_uses_date_exdate(self) -> None:
        body = google_event_body(
            all_day_series(),
            allow_recurrence_write=True,
        )
        self.assertEqual(body["start"], {"date": "2030-02-05"})
        self.assertEqual(body["end"], {"date": "2030-02-06"})
        self.assertIn("EXDATE;VALUE=DATE:20300212", body["recurrence"])

    def test_all_day_yearly_series_body_uses_exact_rule(self) -> None:
        yearly = replace(
            all_day_series(),
            recurrence=Recurrence(("RRULE:FREQ=YEARLY",)),
        )
        google_body = google_event_body(
            yearly,
            allow_recurrence_write=True,
        )
        timetree_body = timetree_event_body(
            yearly,
            calendar_id="123",
            default_timezone="Asia/Tokyo",
            allow_recurrence_write=True,
            label_catalog=TEST_LABEL_CATALOG,
        )
        self.assertEqual(google_body["recurrence"], ["RRULE:FREQ=YEARLY"])
        self.assertEqual(timetree_body["recurrences"], ["RRULE:FREQ=YEARLY"])


class TimeTreeSeriesWriteTests(unittest.IsolatedAsyncioTestCase):
    def test_series_create_and_rule_update_emit_recurrences(self) -> None:
        series = timed_series()
        create_body = timetree_event_body(
            series,
            calendar_id="123",
            default_timezone="Asia/Tokyo",
            allow_recurrence_write=True,
            label_catalog=TEST_LABEL_CATALOG,
        )
        update_body = timetree_update_body(
            series,
            fields={"recurrence"},
            calendar_id="123",
            default_timezone="Asia/Tokyo",
            allow_recurrence_write=True,
        )
        self.assertEqual(create_body["recurrences"], update_body["recurrences"])
        self.assertEqual(update_body["calendar_id"], 123)

    def test_recurrence_removal_sends_empty_array_and_requires_gate(self) -> None:
        target = replace(
            timed_series(),
            kind=EventKind.SINGLE,
            recurrence=Recurrence(),
        )
        body = timetree_update_body(
            target,
            fields={"recurrence"},
            calendar_id="123",
            default_timezone="Asia/Tokyo",
            allow_recurrence_write=True,
        )
        self.assertEqual(body, {"calendar_id": 123, "recurrences": []})
        with self.assertRaises(TimeTreeWriteGateError):
            timetree_update_body(
                target,
                fields={"recurrence"},
                calendar_id="123",
                default_timezone="Asia/Tokyo",
            )

    async def test_series_delete_opens_but_exception_delete_stays_closed(self) -> None:
        fake = FakeClient(
            [result({"success": True, "deleted_event_uuid": "tt-series"})]
        )
        client = TimeTreeMCPClient(
            fake,
            calendar_id="123",
            default_timezone="Asia/Tokyo",
        )
        deleted = await client.delete_event(
            "tt-series",
            event_kind=EventKind.SERIES,
            allow_recurrence_write=True,
        )
        self.assertEqual(deleted.event_uuid, "tt-series")
        self.assertEqual(len(fake.calls), 1)

        with self.assertRaises(TimeTreeWriteGateError):
            await client.delete_event(
                "tt-exception",
                event_kind=EventKind.EXCEPTION,
                allow_recurrence_write=True,
            )
        self.assertEqual(len(fake.calls), 1)

    def test_all_day_series_uses_inclusive_timetree_end_and_date_exdate(self) -> None:
        body = timetree_event_body(
            all_day_series(),
            calendar_id="123",
            default_timezone="Asia/Tokyo",
            allow_recurrence_write=True,
            label_catalog=TEST_LABEL_CATALOG,
        )
        start = datetime.fromtimestamp(
            body["start_at"] / 1000,
            tz=ZoneInfo("UTC"),
        )
        end = datetime.fromtimestamp(
            body["end_at"] / 1000,
            tz=ZoneInfo("UTC"),
        )
        self.assertEqual(start, datetime(2030, 2, 5, tzinfo=ZoneInfo("UTC")))
        self.assertEqual(end, datetime(2030, 2, 5, tzinfo=ZoneInfo("UTC")))
        self.assertEqual(body["start_timezone"], "UTC")
        self.assertEqual(body["end_timezone"], "UTC")
        self.assertIn("EXDATE;VALUE=DATE:20300212", body["recurrences"])


if __name__ == "__main__":
    unittest.main()
