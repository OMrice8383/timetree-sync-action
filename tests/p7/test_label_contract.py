from __future__ import annotations

import json
import unittest
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from bridge.adapters import (
    UnsupportedEventError,
    classify_timetree_event,
    normalize_google_event,
    normalize_timetree_event,
)
from bridge.canonical import event_hash
from bridge.google_client import google_event_body
from bridge.models import (
    DEFAULT_TIMETREE_LABEL_NAME,
    EventClassification,
    EventKind,
    NormalizedEvent,
    Source,
    TimeTreeLabelCatalog,
)
from bridge.timetree_client import (
    TimeTreeMCPClient,
    TimeTreeProtocolError,
    timetree_event_body,
    timetree_update_body,
)

LABEL_CATALOG = TimeTreeLabelCatalog.from_mapping(
    {
        37: "大河予定",
        42: "共通予定",
        99: "夢香プライベート予定",
    }
)


class TextBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


def result(payload: dict) -> SimpleNamespace:
    return SimpleNamespace(
        content=[TextBlock(json.dumps(payload, ensure_ascii=False))],
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


def timetree_raw(*, label_id: int | None = 37) -> dict:
    raw = {
        "uuid": "label-contract-event",
        "calendar_id": 123,
        "title": "Label contract event",
        "all_day": False,
        "start_at": 1894060800000,
        "end_at": 1894062600000,
        "start_timezone": "UTC",
        "end_timezone": "UTC",
        "category": 1,
        "type": 0,
        "recurrences": [],
        "parent_id": None,
        "recurring_uuid": None,
    }
    if label_id is not None:
        raw["label_id"] = label_id
    return raw


def google_raw(*, private: dict | None = None, extra: dict | None = None) -> dict:
    raw = {
        "id": "google-label-contract-event",
        "status": "confirmed",
        "eventType": "default",
        "summary": "Google label contract event",
        "description": "Description is not a label source",
        "colorId": "11",
        "start": {
            "dateTime": "2030-02-12T10:00:00+09:00",
            "timeZone": "Asia/Tokyo",
        },
        "end": {
            "dateTime": "2030-02-12T10:30:00+09:00",
            "timeZone": "Asia/Tokyo",
        },
    }
    if private is not None:
        raw["extendedProperties"] = {"private": private}
    if extra:
        raw.update(extra)
    return raw


def normalized_event(*, label: str = DEFAULT_TIMETREE_LABEL_NAME) -> NormalizedEvent:
    return NormalizedEvent(
        source=Source.GOOGLE,
        source_calendar_id="google-label-contract",
        source_event_id="google-label-contract-event",
        kind=EventKind.SINGLE,
        parent_source_event_id=None,
        original_start=None,
        title="Label contract event",
        all_day=False,
        start=datetime(2030, 2, 12, 10, 0, tzinfo=ZoneInfo("Asia/Tokyo")),
        end=datetime(2030, 2, 12, 10, 30, tzinfo=ZoneInfo("Asia/Tokyo")),
        start_timezone="Asia/Tokyo",
        end_timezone="Asia/Tokyo",
        description=None,
        location=None,
        label=label,
    )


class LabelResolutionTests(unittest.TestCase):
    def test_target_labels_sync_and_other_real_label_is_ignored(self) -> None:
        first = classify_timetree_event(
            timetree_raw(label_id=37), label_catalog=LABEL_CATALOG
        )
        self.assertIs(first.classification, EventClassification.SYNC)
        self.assertEqual(
            classify_timetree_event(
                timetree_raw(label_id=42), label_catalog=LABEL_CATALOG
            ).classification,
            EventClassification.SYNC,
        )
        ignored = classify_timetree_event(
            timetree_raw(label_id=99), label_catalog=LABEL_CATALOG
        )
        self.assertEqual(ignored.classification, EventClassification.IGNORE_KNOWN)
        self.assertEqual(ignored.code, "LABEL_OUT_OF_SCOPE")

    def test_missing_unknown_and_unresolvable_labels_fail_safe(self) -> None:
        cases = (
            (timetree_raw(label_id=None), LABEL_CATALOG, "TIMETREE_LABEL_MISSING"),
            (timetree_raw(label_id=404), LABEL_CATALOG, "TIMETREE_LABEL_UNKNOWN_ID"),
            (
                timetree_raw(label_id=37),
                TimeTreeLabelCatalog.from_mapping({37: None, 42: "共通予定"}),
                "TIMETREE_LABEL_NAME_MISSING",
            ),
        )
        for raw, catalog, code in cases:
            with self.subTest(code=code):
                eligibility = classify_timetree_event(raw, label_catalog=catalog)
                self.assertIs(eligibility.classification, EventClassification.UNSUPPORTED)
                self.assertEqual(eligibility.code, code)

    def test_duplicate_names_and_missing_target_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            TimeTreeLabelCatalog.from_mapping({37: "大河予定", 42: "大河予定"})
        with self.assertRaises(ValueError):
            TimeTreeLabelCatalog.from_mapping({37: "大河予定"}).require_sync_labels()

    def test_normalized_label_is_semantic_and_changes_hash(self) -> None:
        first = normalize_timetree_event(
            timetree_raw(label_id=37),
            default_timezone="Asia/Tokyo",
            label_catalog=LABEL_CATALOG,
        )
        second = normalize_timetree_event(
            timetree_raw(label_id=42),
            default_timezone="Asia/Tokyo",
            label_catalog=LABEL_CATALOG,
        )
        self.assertEqual(first.label, "大河予定")
        self.assertEqual(second.label, "共通予定")
        self.assertNotEqual(event_hash(first), event_hash(second))

    def test_timetree_create_and_explicit_update_resolve_runtime_ids(self) -> None:
        create_body = timetree_event_body(
            normalized_event(label="共通予定"),
            calendar_id="123",
            default_timezone="Asia/Tokyo",
            label_catalog=LABEL_CATALOG,
        )
        self.assertEqual(create_body["label_id"], 42)

        title_body = timetree_update_body(
            normalized_event(label="共通予定"),
            fields={"title"},
            calendar_id="123",
            default_timezone="Asia/Tokyo",
        )
        self.assertNotIn("label_id", title_body)

        label_body = timetree_update_body(
            normalized_event(label="共通予定"),
            fields={"label"},
            calendar_id="123",
            default_timezone="Asia/Tokyo",
            label_catalog=LABEL_CATALOG,
        )
        self.assertEqual(label_body["label_id"], 42)


class LabelClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_label_catalog_is_used_for_create_and_label_update(self) -> None:
        labels = {
            "calendar_id": "123",
            "labels": [
                {"id": 37, "name": "大河予定"},
                {"id": 42, "name": "共通予定"},
                {"id": 99, "name": "夢香プライベート予定"},
            ],
        }
        fake = FakeClient(
            [
                result(labels),
                result({"success": True, "event": {"uuid": "created"}}),
                result(labels),
                result({"success": True, "event": {"uuid": "created"}}),
            ]
        )
        client = TimeTreeMCPClient(fake, calendar_id="123", default_timezone="Asia/Tokyo")
        await client.create_event(normalized_event(label="大河予定"))
        await client.update_event(
            "created",
            normalized_event(label="共通予定"),
            fields={"label"},
        )
        self.assertEqual(fake.calls[0][0], "get_calendar_labels")
        self.assertEqual(fake.calls[1][1]["label_id"], 37)
        self.assertEqual(fake.calls[2][0], "get_calendar_labels")
        self.assertEqual(fake.calls[3][1]["label_id"], 42)

    async def test_update_without_label_does_not_read_catalog_or_send_label_id(self) -> None:
        fake = FakeClient(
            [result({"success": True, "event": {"uuid": "updated"}})]
        )
        client = TimeTreeMCPClient(fake, calendar_id="123", default_timezone="Asia/Tokyo")
        await client.update_event(
            "updated",
            normalized_event(label="共通予定"),
            fields={"title"},
        )
        self.assertEqual(fake.calls, [("update_event", {
            "calendar_id": 123,
            "title": "Label contract event",
            "event_uuid": "updated",
        })])

    async def test_catalog_missing_target_or_duplicate_name_fails_safe(self) -> None:
        for labels in (
            [{"id": 37, "name": "大河予定"}],
            [
                {"id": 37, "name": "大河予定"},
                {"id": 42, "name": "大河予定"},
                {"id": 43, "name": "共通予定"},
            ],
        ):
            with self.subTest(labels=labels):
                fake = FakeClient(
                    [result({"calendar_id": "123", "labels": labels})]
                )
                client = TimeTreeMCPClient(
                    fake,
                    calendar_id="123",
                    default_timezone="Asia/Tokyo",
                )
                with self.assertRaises(TimeTreeProtocolError):
                    await client.get_calendar_labels()


class GoogleLabelMetadataTests(unittest.TestCase):
    def test_google_managed_metadata_round_trips_and_body_preserves_name(self) -> None:
        managed = normalize_google_event(
            google_raw(
                private={
                    "sync_source": "timetree-chatgpt-bridge",
                    "timetree_label_name": "共通予定",
                }
            ),
            source_calendar_id="google-calendar",
            default_timezone="Asia/Tokyo",
        )
        self.assertEqual(managed.label, "共通予定")
        body = google_event_body(managed)
        self.assertEqual(
            body["extendedProperties"]["private"]["timetree_label_name"],
            "共通予定",
        )

    def test_unmanaged_google_without_metadata_defaults_without_inference(self) -> None:
        event = normalize_google_event(
            google_raw(),
            source_calendar_id="google-calendar",
            default_timezone="Asia/Tokyo",
        )
        self.assertEqual(event.label, DEFAULT_TIMETREE_LABEL_NAME)

    def test_managed_metadata_loss_and_unknown_label_fail_safe(self) -> None:
        for private in (
            {"sync_source": "timetree-chatgpt-bridge"},
            {
                "sync_source": "timetree-chatgpt-bridge",
                "timetree_label_name": "夢香プライベート予定",
            },
        ):
            with self.subTest(private=private):
                with self.assertRaises(UnsupportedEventError) as caught:
                    normalize_google_event(
                        google_raw(private=private),
                        source_calendar_id="google-calendar",
                        default_timezone="Asia/Tokyo",
                    )
                self.assertEqual(
                    caught.exception.code,
                    "UNSUPPORTED_GOOGLE_LABEL_METADATA",
                )


if __name__ == "__main__":
    unittest.main()
