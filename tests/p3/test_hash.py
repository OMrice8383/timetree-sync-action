from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from bridge.adapters import normalize_google_event, normalize_timetree_event
from bridge.canonical import (
    canonical_event_dict,
    canonicalize_recurrence,
    event_hash,
)
from bridge.models import EventKind, Recurrence

FIXTURES = Path(__file__).parents[1] / "fixtures"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def base_event():
    return normalize_google_event(
        fixture("google_single.json"),
        source_calendar_id="fixture-calendar",
        default_timezone="Asia/Tokyo",
    )


class RecurrenceCanonicalizationTests(unittest.TestCase):
    def test_same_rrule_meaning_has_same_canonical_form(self) -> None:
        a = (
            "RRULE:FREQ=WEEKLY;BYDAY=WE,MO;INTERVAL=1",
        )
        b = (
            "RRULE:BYDAY=MO,WE;FREQ=WEEKLY",
        )
        self.assertEqual(canonicalize_recurrence(a), canonicalize_recurrence(b))


    def test_rrule_numeric_defaults_and_list_order_are_semantic(self) -> None:
        a = (
            "RRULE:FREQ=weekly;INTERVAL=01;BYMONTH=10,02,2",
        )
        b = (
            "RRULE:BYMONTH=2,10;FREQ=WEEKLY",
        )
        self.assertEqual(canonicalize_recurrence(a), canonicalize_recurrence(b))

    def test_exdate_parameter_and_value_order_is_stable(self) -> None:
        a = (
            "EXDATE;VALUE=DATE;TZID=Asia/Tokyo:20300212,20300205",
        )
        b = (
            "EXDATE;TZID=Asia/Tokyo;VALUE=DATE:20300205,20300212",
        )
        self.assertEqual(canonicalize_recurrence(a), canonicalize_recurrence(b))


    def test_timed_exdate_utc_and_tzid_forms_are_same_instant(self) -> None:
        utc_form = (
            "EXDATE:20260918T010000Z",
        )
        tzid_form = (
            "EXDATE;TZID=Asia/Tokyo:20260918T100000",
        )
        self.assertEqual(
            canonicalize_recurrence(utc_form),
            canonicalize_recurrence(tzid_form),
        )


class EventHashTests(unittest.TestCase):
    def test_same_instant_different_datetime_offset_has_same_hash(self) -> None:
        event = base_event()
        start = event.start.astimezone(ZoneInfo("UTC"))
        end = event.end.astimezone(ZoneInfo("UTC"))
        shifted = replace(event, start=start, end=end)
        self.assertEqual(event_hash(event), event_hash(shifted))

    def test_same_instant_but_effective_timezone_change_changes_hash(self) -> None:
        event = base_event()
        changed = replace(
            event,
            start_timezone="America/Los_Angeles",
            end_timezone="America/Los_Angeles",
        )
        self.assertNotEqual(event_hash(event), event_hash(changed))


    def test_start_and_end_timezone_changes_each_change_hash(self) -> None:
        event = base_event()
        start_changed = replace(
            event,
            start_timezone="America/Los_Angeles",
        )
        end_changed = replace(
            event,
            end_timezone="America/Los_Angeles",
        )
        self.assertNotEqual(event_hash(event), event_hash(start_changed))
        self.assertNotEqual(event_hash(event), event_hash(end_changed))

    def test_all_day_source_timezone_does_not_enter_event_hash(self) -> None:
        raw = fixture("timetree_all_day.json")
        utc_event = normalize_timetree_event(raw, default_timezone="Asia/Tokyo")
        raw["start_timezone"] = "Asia/Tokyo"
        raw["end_timezone"] = "Asia/Tokyo"
        tokyo_event = normalize_timetree_event(raw, default_timezone="Asia/Tokyo")
        self.assertEqual(utc_event.start, tokyo_event.start)
        self.assertEqual(utc_event.end, tokyo_event.end)
        self.assertEqual(event_hash(utc_event), event_hash(tokyo_event))

    def test_title_and_time_changes_change_hash(self) -> None:
        event = base_event()
        self.assertNotEqual(
            event_hash(event),
            event_hash(replace(event, title="Changed")),
        )
        self.assertNotEqual(
            event_hash(event),
            event_hash(
                replace(
                    event,
                    start=event.start + timedelta(minutes=5),
                    end=event.end + timedelta(minutes=5),
                )
            ),
        )

    def test_updated_at_is_not_part_of_event_hash(self) -> None:
        event = base_event()
        updated = replace(
            event,
            updated_at=datetime(2030, 2, 1, 12, 0, tzinfo=ZoneInfo("UTC")),
        )
        self.assertEqual(event_hash(event), event_hash(updated))

    def test_recurrence_semantics_affect_hash_not_textual_order(self) -> None:
        event = replace(
            base_event(),
            kind=EventKind.SERIES,
            recurrence=Recurrence(
                ("RRULE:FREQ=WEEKLY;BYDAY=WE,MO;INTERVAL=1",)
            ),
        )
        reordered = replace(
            event,
            recurrence=Recurrence(("RRULE:BYDAY=MO,WE;FREQ=WEEKLY",)),
        )
        changed = replace(
            event,
            recurrence=Recurrence(("RRULE:FREQ=DAILY",)),
        )
        self.assertEqual(event_hash(event), event_hash(reordered))
        self.assertNotEqual(event_hash(event), event_hash(changed))


    def test_kind_and_exception_identity_change_hash(self) -> None:
        event = base_event()
        original_start = event.start
        exception = replace(
            event,
            kind=EventKind.EXCEPTION,
            parent_source_event_id="series-a",
            original_start=original_start,
        )
        changed_parent = replace(exception, parent_source_event_id="series-b")
        changed_original = replace(
            exception,
            original_start=original_start + timedelta(days=7),
        )
        self.assertNotEqual(event_hash(event), event_hash(exception))
        self.assertNotEqual(event_hash(exception), event_hash(changed_parent))
        self.assertNotEqual(event_hash(exception), event_hash(changed_original))

    def test_newline_normalization_is_stable(self) -> None:
        event = replace(base_event(), description="a\r\nb\rc")
        canonical = canonical_event_dict(event)
        self.assertEqual(canonical["description"], "a\nb\nc")


if __name__ == "__main__":
    unittest.main()
