from __future__ import annotations

import json
import unittest
import warnings
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from bridge.adapters import (
    EventEligibilityError,
    TimezoneFallbackWarning,
    UnsupportedEventError,
    classify_google_event,
    normalize_google_event,
)
from bridge.adapters import (
    classify_timetree_event as _classify_timetree_event,
)
from bridge.adapters import (
    normalize_timetree_event as _normalize_timetree_event,
)
from bridge.models import (
    ChangeType,
    EventChange,
    EventClassification,
    EventKind,
    Source,
    TimeTreeLabelCatalog,
)

FIXTURES = Path(__file__).parents[1] / "fixtures"
TEST_LABEL_CATALOG = TimeTreeLabelCatalog.from_mapping(
    {
        1: "夢香プライベート予定",
        3: "大河予定",
        7: "夢香仕事予定",
        10: "共通予定",
    }
)


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def classify_timetree_event(raw: dict):
    return _classify_timetree_event(raw, label_catalog=TEST_LABEL_CATALOG)


def normalize_timetree_event(raw: dict, *, default_timezone: str):
    return _normalize_timetree_event(
        raw,
        default_timezone=default_timezone,
        label_catalog=TEST_LABEL_CATALOG,
    )


class ClassificationTests(unittest.TestCase):
    def test_timetree_three_state_classification(self) -> None:
        self.assertEqual(
            classify_timetree_event(fixture("timetree_single.json")).classification,
            EventClassification.SYNC,
        )
        self.assertEqual(
            classify_timetree_event(fixture("timetree_memo.json")).classification,
            EventClassification.IGNORE_KNOWN,
        )
        self.assertEqual(
            classify_timetree_event(fixture("timetree_birthday.json")).classification,
            EventClassification.IGNORE_KNOWN,
        )
        birthday = fixture("timetree_birthday.json")
        birthday["label_id"] = 3
        self.assertEqual(
            classify_timetree_event(birthday).classification,
            EventClassification.IGNORE_KNOWN,
        )
        self.assertEqual(
            classify_timetree_event(
                fixture("timetree_unsupported_type.json")
            ).classification,
            EventClassification.UNSUPPORTED,
        )
        unsupported = fixture("timetree_unsupported_type.json")
        unsupported["label_id"] = 3
        self.assertEqual(
            classify_timetree_event(unsupported).classification,
            EventClassification.UNSUPPORTED,
        )

    def test_timetree_classification_precedes_label_scope(self) -> None:
        memo = fixture("timetree_memo.json")
        memo.pop("label_id", None)
        self.assertEqual(
            classify_timetree_event(memo).code,
            "TIMETREE_MEMO",
        )

        unknown = fixture("timetree_unsupported_type.json")
        unknown.pop("label_id", None)
        self.assertEqual(
            classify_timetree_event(unknown).code,
            "TIMETREE_CATEGORY_1_TYPE_999",
        )

        normal = fixture("timetree_single.json")
        normal.pop("label_id", None)
        self.assertEqual(
            classify_timetree_event(normal).code,
            "TIMETREE_LABEL_MISSING",
        )

    def test_google_empty_title_and_special_event_are_unsupported(self) -> None:
        self.assertEqual(
            classify_google_event(fixture("google_empty_title.json")).classification,
            EventClassification.UNSUPPORTED,
        )
        special = fixture("google_single.json")
        special["eventType"] = "focusTime"
        self.assertEqual(
            classify_google_event(special).classification,
            EventClassification.UNSUPPORTED,
        )


class TimeTreeNormalizationTests(unittest.TestCase):
    def test_timed_event_uses_uuid_and_effective_timezones(self) -> None:
        event = normalize_timetree_event(
            fixture("timetree_single.json"),
            default_timezone="Asia/Tokyo",
        )
        self.assertEqual(event.source, Source.TIMETREE)
        self.assertEqual(event.source_event_id, "fixture-timetree-single")
        self.assertEqual(event.source_calendar_id, "123456789")
        self.assertEqual(event.kind, EventKind.SINGLE)
        self.assertFalse(event.all_day)
        self.assertIsInstance(event.start, datetime)
        self.assertIsNotNone(event.start.tzinfo)
        self.assertEqual(event.start_timezone, "Asia/Tokyo")
        self.assertEqual(event.end_timezone, "Asia/Tokyo")

    def test_missing_timezone_falls_back_only_on_missing_side(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            event = normalize_timetree_event(
                fixture("timetree_timezone_missing_start.json"),
                default_timezone="America/Los_Angeles",
            )
        self.assertEqual(event.start_timezone, "America/Los_Angeles")
        self.assertEqual(event.end_timezone, "Asia/Tokyo")
        self.assertEqual(len(caught), 1)
        self.assertTrue(issubclass(caught[0].category, TimezoneFallbackWarning))

    def test_all_day_converts_inclusive_timetree_end_to_exclusive(self) -> None:
        event = normalize_timetree_event(
            fixture("timetree_all_day.json"),
            default_timezone="Asia/Tokyo",
        )
        self.assertTrue(event.all_day)
        self.assertEqual(event.start, date(2030, 1, 1))
        self.assertEqual(event.end, date(2030, 1, 2))
        self.assertIsNone(event.start_timezone)
        self.assertIsNone(event.end_timezone)

    def test_all_day_extracts_local_calendar_date_before_date_conversion(self) -> None:
        raw = fixture("timetree_all_day.json")
        raw["start_timezone"] = "America/Los_Angeles"
        raw["end_timezone"] = "America/Los_Angeles"
        event = normalize_timetree_event(raw, default_timezone="Asia/Tokyo")
        self.assertEqual(event.start, date(2029, 12, 31))
        self.assertEqual(event.end, date(2030, 1, 1))

    def test_both_missing_timezones_use_default_independently(self) -> None:
        raw = fixture("timetree_timezone_missing_start.json")
        raw["end_timezone"] = None
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            event = normalize_timetree_event(
                raw,
                default_timezone="America/Los_Angeles",
            )
        self.assertEqual(event.start_timezone, "America/Los_Angeles")
        self.assertEqual(event.end_timezone, "America/Los_Angeles")
        self.assertEqual(len(caught), 2)

    def test_multi_day_all_day_keeps_exclusive_internal_end(self) -> None:
        raw = fixture("timetree_all_day.json")
        raw["end_at"] = raw["start_at"] + 2 * 86_400_000
        event = normalize_timetree_event(raw, default_timezone="Asia/Tokyo")
        self.assertEqual(event.start, date(2030, 1, 1))
        self.assertEqual(event.end, date(2030, 1, 4))

    def test_timetree_updated_at_is_unix_ms_metadata(self) -> None:
        raw = fixture("timetree_single.json")
        raw["updated_at"] = 1_893_492_000_000
        event = normalize_timetree_event(raw, default_timezone="Asia/Tokyo")
        self.assertIsNotNone(event.updated_at)
        self.assertIsNotNone(event.updated_at.tzinfo)

    def test_timetree_all_day_requires_boolean(self) -> None:
        raw = fixture("timetree_all_day.json")
        raw["all_day"] = "false"
        with self.assertRaises(ValueError):
            normalize_timetree_event(raw, default_timezone="Asia/Tokyo")

    def test_ignore_known_event_is_not_normalized(self) -> None:
        with self.assertRaises(EventEligibilityError) as caught:
            normalize_timetree_event(
                fixture("timetree_memo.json"),
                default_timezone="Asia/Tokyo",
            )
        self.assertEqual(
            caught.exception.eligibility.classification,
            EventClassification.IGNORE_KNOWN,
        )


class GoogleNormalizationTests(unittest.TestCase):
    def test_google_timed_and_all_day(self) -> None:
        timed = normalize_google_event(
            fixture("google_single.json"),
            source_calendar_id="fixture-calendar",
            default_timezone="Asia/Tokyo",
        )
        all_day = normalize_google_event(
            fixture("google_all_day.json"),
            source_calendar_id="fixture-calendar",
            default_timezone="Asia/Tokyo",
        )
        self.assertEqual(timed.source, Source.GOOGLE)
        self.assertEqual(timed.start_timezone, "Asia/Tokyo")
        self.assertEqual(timed.end_timezone, "Asia/Tokyo")
        self.assertEqual(all_day.start, date(2030, 2, 2))
        self.assertEqual(all_day.end, date(2030, 2, 3))
        self.assertIsNone(all_day.start_timezone)

    def test_google_offset_only_accepts_matching_default_timezone(self) -> None:
        event = normalize_google_event(
            fixture("google_offset_only.json"),
            source_calendar_id="fixture-calendar",
            default_timezone="Asia/Tokyo",
        )
        self.assertEqual(event.start_timezone, "Asia/Tokyo")
        self.assertEqual(event.start.tzinfo, ZoneInfo("Asia/Tokyo"))

    def test_google_offset_only_rejects_mismatching_default_timezone(self) -> None:
        with self.assertRaises(UnsupportedEventError) as caught:
            normalize_google_event(
                fixture("google_offset_only.json"),
                source_calendar_id="fixture-calendar",
                default_timezone="America/Los_Angeles",
            )
        self.assertEqual(caught.exception.code, "UNSUPPORTED_GOOGLE_TIMEZONE")

    def test_google_offset_only_handles_dst_boundary(self) -> None:
        raw = fixture("google_offset_only.json")
        raw["start"]["dateTime"] = "2030-03-10T01:30:00-08:00"
        raw["end"]["dateTime"] = "2030-03-10T03:30:00-07:00"
        event = normalize_google_event(
            raw,
            source_calendar_id="fixture-calendar",
            default_timezone="America/Los_Angeles",
        )
        self.assertEqual(event.start_timezone, "America/Los_Angeles")
        self.assertEqual(event.end_timezone, "America/Los_Angeles")
        self.assertNotEqual(event.start.utcoffset(), event.end.utcoffset())

    def test_google_updated_is_rfc3339_metadata(self) -> None:
        raw = fixture("google_single.json")
        raw["updated"] = "2030-02-01T03:00:00Z"
        event = normalize_google_event(
            raw,
            source_calendar_id="fixture-calendar",
            default_timezone="Asia/Tokyo",
        )
        self.assertIsNotNone(event.updated_at)
        self.assertIsNotNone(event.updated_at.tzinfo)

    def test_google_unknown_fields_do_not_change_normalized_meaning(self) -> None:
        raw = fixture("google_single.json")
        original = normalize_google_event(
            raw,
            source_calendar_id="fixture-calendar",
            default_timezone="Asia/Tokyo",
        )
        raw["hangoutLink"] = "https://example.invalid/ignored"
        with_extra = normalize_google_event(
            raw,
            source_calendar_id="fixture-calendar",
            default_timezone="Asia/Tokyo",
        )
        self.assertEqual(original, with_extra)

    def test_google_empty_title_is_not_invented(self) -> None:
        with self.assertRaises(EventEligibilityError) as caught:
            normalize_google_event(
                fixture("google_empty_title.json"),
                source_calendar_id="fixture-calendar",
                default_timezone="Asia/Tokyo",
            )
        self.assertEqual(
            caught.exception.eligibility.code,
            "UNSUPPORTED_GOOGLE_EMPTY_TITLE",
        )

    def test_series_requires_matching_effective_timezones(self) -> None:
        raw = fixture("google_recurrence.json")
        raw["end"]["timeZone"] = "America/Los_Angeles"
        with self.assertRaises(UnsupportedEventError) as caught:
            normalize_google_event(
                raw,
                source_calendar_id="fixture-calendar",
                default_timezone="Asia/Tokyo",
            )
        self.assertEqual(caught.exception.code, "UNSUPPORTED_RECURRENCE_TIMEZONE")

    def test_exdate_is_enabled_by_confirmed_p1_contract(self) -> None:
        raw = fixture("google_recurrence.json")
        raw["recurrence"] = [
            "RRULE:FREQ=WEEKLY;COUNT=3;BYDAY=TU",
            "EXDATE:20300212T000000Z",
        ]
        event = normalize_google_event(
            raw,
            source_calendar_id="fixture-calendar",
            default_timezone="Asia/Tokyo",
        )
        self.assertEqual(len(event.recurrence.lines), 2)

    def test_rdate_and_exrule_are_not_enabled(self) -> None:
        for line in ("RDATE:20300212T000000Z", "EXRULE:FREQ=MONTHLY"):
            raw = fixture("google_recurrence.json")
            raw["recurrence"] = [line]
            with (
                self.subTest(line=line),
                self.assertRaises(UnsupportedEventError) as caught,
            ):
                normalize_google_event(
                    raw,
                    source_calendar_id="fixture-calendar",
                    default_timezone="Asia/Tokyo",
                )
            self.assertEqual(caught.exception.code, "UNSUPPORTED_RECURRENCE_FEATURE")


class EventChangeTests(unittest.TestCase):
    def test_partial_delete_needs_only_source_id(self) -> None:
        change = EventChange(ChangeType.DELETE, "google-event-id")
        self.assertEqual(change.source_event_id, "google-event-id")
        self.assertIsNone(change.event)

    def test_recurrence_exception_delete_requires_parent_and_original_start(
        self,
    ) -> None:
        original = datetime(2030, 2, 5, 9, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
        change = EventChange(
            ChangeType.RECURRENCE_EXCEPTION_DELETE,
            "google-instance-id",
            parent_source_event_id="google-series-id",
            original_start=original,
        )
        self.assertEqual(change.parent_source_event_id, "google-series-id")
        with self.assertRaises(ValueError):
            EventChange(
                ChangeType.RECURRENCE_EXCEPTION_DELETE,
                "google-instance-id",
            )


if __name__ == "__main__":
    unittest.main()
