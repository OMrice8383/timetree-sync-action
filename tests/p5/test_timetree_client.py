from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from bridge.adapters import normalize_timetree_event
from bridge.models import EventKind, NormalizedEvent, Recurrence, Source
from bridge.timetree_client import (
    TimeTreeMCPClient,
    TimeTreeProtocolError,
    TimeTreeToolError,
    TimeTreeTransportError,
    TimeTreeWriteGateError,
    timetree_event_body,
    timetree_update_body,
)


class TextBlock:
    type = "text"
    def __init__(self, text: str):
        self.text = text


def result(payload: dict, *, is_error: bool = False):
    return SimpleNamespace(
        content=[TextBlock(json.dumps(payload))],
        structured_content=None,
        is_error=is_error,
    )


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return self.responses.pop(0)


def single_event(*, title="Fixture", all_day=False):
    if all_day:
        start = date(2030, 1, 2)
        end = date(2030, 1, 4)
        start_timezone = None
        end_timezone = None
    else:
        start = datetime(2030, 1, 2, 10, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
        end = datetime(2030, 1, 2, 11, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
        start_timezone = "Asia/Tokyo"
        end_timezone = "Asia/Tokyo"
    return NormalizedEvent(
        source=Source.GOOGLE,
        source_calendar_id="google-test",
        source_event_id="google-event",
        kind=EventKind.SINGLE,
        parent_source_event_id=None,
        original_start=None,
        title=title,
        all_day=all_day,
        start=start,
        end=end,
        start_timezone=start_timezone,
        end_timezone=end_timezone,
        description="desc",
        location="loc",
    )


def mcp_read_event(*, uuid="tt-1", updated="2030-01-02T01:00:00.000Z"):
    return {
        "uuid": uuid,
        "calendar_id": 123,
        "title": "Fixture",
        "start_at": "2030-01-02T01:00:00.000Z",
        "start_timezone": "Asia/Tokyo",
        "end_at": "2030-01-02T02:00:00.000Z",
        "end_timezone": "Asia/Tokyo",
        "all_day": False,
        "category": 1,
        "type": 0,
        "recurrences": [],
        "updated_at": updated,
        "created_at": "2030-01-01T00:00:00.000Z",
        "deactivated_at": None,
        "parent_id": None,
        "recurring_uuid": None,
        "note": None,
        "location": None,
    }


class TimeTreeClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_calendars_and_one_client_is_reused(self):
        fake = FakeClient(
            [
                result({"calendars": [{"id": "123", "name": "Shared", "alias_code": "abc", "users": []}], "total": 1}),
                result({"calendar_id": "123", "events": [], "total": 0, "total_fetched": 0}),
            ]
        )
        client = TimeTreeMCPClient(fake, calendar_id="123", default_timezone="Asia/Tokyo")
        calendars = await client.list_calendars()
        events = await client.get_events()
        self.assertEqual(calendars[0].calendar_id, "123")
        self.assertEqual(events, ())
        self.assertEqual([name for name, _ in fake.calls], ["list_calendars", "get_events"])

    async def test_sanitized_p1_fixture_flows_into_p3_normalizer(self):
        fixture_path = Path(__file__).resolve().parents[1] / "fixtures" / "timetree_single.json"
        raw = json.loads(fixture_path.read_text(encoding="utf-8"))
        raw.pop("fixture_meta", None)
        calendar_id = str(raw["calendar_id"])
        fake = FakeClient([result({"calendar_id": calendar_id, "events": [raw]})])
        client = TimeTreeMCPClient(fake, calendar_id=calendar_id, default_timezone="Asia/Tokyo")
        (event,) = await client.get_events()
        normalized = normalize_timetree_event(event, default_timezone="Asia/Tokyo")
        self.assertEqual(normalized.source_event_id, raw["uuid"])
        self.assertEqual(normalized.source_calendar_id, calendar_id)

    async def test_read_converts_iso_to_unix_ms_and_preserves_type_zero(self):
        raw = mcp_read_event()
        fake = FakeClient([result({"calendar_id": "123", "events": [raw]})])
        client = TimeTreeMCPClient(fake, calendar_id="123", default_timezone="Asia/Tokyo")
        (event,) = await client.get_events()
        self.assertEqual(event["calendar_id"], 123)
        self.assertEqual(event["type"], 0)
        self.assertIsInstance(event["start_at"], int)
        self.assertIsInstance(event["end_at"], int)
        self.assertIsInstance(event["updated_at"], int)
        self.assertIsNone(event["deactivated_at"])

    async def test_incremental_dedupes_inclusive_boundary_by_uuid(self):
        older = mcp_read_event(updated="2030-01-02T01:00:00.000Z")
        newer = mcp_read_event(updated="2030-01-02T01:00:01.000Z")
        newer["title"] = "newer"
        fake = FakeClient([result({"calendar_id": "123", "events": [older, newer]})])
        client = TimeTreeMCPClient(fake, calendar_id="123", default_timezone="Asia/Tokyo")
        events = await client.get_updated_events(1_893_456_000_000)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["title"], "newer")
        self.assertEqual(fake.calls[0][1]["updated_after"], 1_893_456_000_000)

    async def test_p1_type_regression_fails_safe(self):
        raw = mcp_read_event()
        raw["type"] = None
        fake = FakeClient([result({"calendar_id": "123", "events": [raw]})])
        client = TimeTreeMCPClient(fake, calendar_id="123", default_timezone="Asia/Tokyo")
        with self.assertRaises(TimeTreeProtocolError):
            await client.get_events()

    async def test_transport_exception_is_mapped(self):
        class BrokenClient:
            async def call_tool(self, name, arguments):
                raise OSError("stdio closed")

        client = TimeTreeMCPClient(
            BrokenClient(),
            calendar_id="123",
            default_timezone="Asia/Tokyo",
        )
        with self.assertRaises(TimeTreeTransportError):
            await client.get_events()

    async def test_malformed_json_fails_safe(self):
        malformed = SimpleNamespace(
            content=[TextBlock("not-json")],
            structured_content=None,
            is_error=False,
        )
        fake = FakeClient([malformed])
        client = TimeTreeMCPClient(
            fake,
            calendar_id="123",
            default_timezone="Asia/Tokyo",
        )
        with self.assertRaises(TimeTreeProtocolError):
            await client.get_events()

    async def test_tool_error_is_mapped(self):
        fake = FakeClient([result({"error": "Invalid calendar", "message": "not found"}, is_error=True)])
        client = TimeTreeMCPClient(fake, calendar_id="123", default_timezone="Asia/Tokyo")
        with self.assertRaises(TimeTreeToolError) as ctx:
            await client.get_events()
        self.assertEqual(ctx.exception.tool, "get_events")
        self.assertEqual(ctx.exception.error, "Invalid calendar")

    async def test_create_update_delete_use_same_uuid(self):
        fake = FakeClient(
            [
                result({"success": True, "event": {"uuid": "tt-created"}}),
                result({"success": True, "event": {"uuid": "tt-created"}}),
                result({"success": True, "deleted_event_uuid": "tt-created"}),
            ]
        )
        client = TimeTreeMCPClient(fake, calendar_id="123", default_timezone="Asia/Tokyo")
        event = single_event()
        created = await client.create_event(event)
        updated = await client.update_event(created.event_uuid, single_event(title="Updated"), fields={"title"})
        deleted = await client.delete_event(updated.event_uuid, event_kind=EventKind.SINGLE)
        self.assertEqual(created.event_uuid, "tt-created")
        self.assertEqual(updated.event_uuid, "tt-created")
        self.assertEqual(deleted.event_uuid, "tt-created")
        self.assertEqual(fake.calls[0][1]["calendar_id"], 123)
        self.assertEqual(fake.calls[1][1], {"calendar_id": 123, "title": "Updated", "event_uuid": "tt-created"})
        self.assertEqual(fake.calls[2][1], {"calendar_id": 123, "event_uuid": "tt-created"})

    async def test_series_write_is_gated_until_p6(self):
        event = single_event()
        series = replace(
            event,
            kind=EventKind.SERIES,
            recurrence=Recurrence(("RRULE:FREQ=DAILY",)),
        )
        with self.assertRaises(TimeTreeWriteGateError):
            timetree_event_body(series, calendar_id="123", default_timezone="Asia/Tokyo")

    async def test_exception_delete_remains_gated_even_when_series_gate_is_open(self):
        fake = FakeClient([])
        client = TimeTreeMCPClient(
            fake,
            calendar_id="123",
            default_timezone="Asia/Tokyo",
        )
        with self.assertRaises(TimeTreeWriteGateError):
            await client.delete_event(
                "exception-uuid",
                event_kind=EventKind.EXCEPTION,
                allow_recurrence_write=True,
            )
        self.assertEqual(fake.calls, [])

    def test_all_day_write_uses_inclusive_timetree_end(self):
        body = timetree_event_body(single_event(all_day=True), calendar_id="123", default_timezone="Asia/Tokyo")
        start = datetime.fromtimestamp(body["start_at"] / 1000, tz=ZoneInfo("Asia/Tokyo"))
        inclusive_end = datetime.fromtimestamp(body["end_at"] / 1000, tz=ZoneInfo("Asia/Tokyo"))
        self.assertEqual(start.date(), date(2030, 1, 2))
        self.assertEqual(inclusive_end.date(), date(2030, 1, 3))
        self.assertEqual(body["start_timezone"], "Asia/Tokyo")
        self.assertEqual(body["end_timezone"], "Asia/Tokyo")

    def test_update_only_emits_requested_semantic_fields(self):
        body = timetree_update_body(single_event(title="Changed"), fields={"title"}, calendar_id="123", default_timezone="Asia/Tokyo")
        self.assertEqual(body, {"calendar_id": 123, "title": "Changed"})

    async def test_connect_uses_stdio_transport_once_and_only_explicit_credentials(self):
        entered = {"client": 0, "transport": 0}
        captured = {}

        class FakeParams:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        @asynccontextmanager
        async def fake_transport(params):
            entered["transport"] += 1
            yield (object(), object())

        class ConnectedClient(FakeClient):
            def __init__(self, transport):
                super().__init__([])
                self.transport = transport
                self._transport_cm = None
            async def __aenter__(self):
                entered["client"] += 1
                self._transport_cm = self.transport
                await self._transport_cm.__aenter__()
                return self
            async def __aexit__(self, *args):
                assert self._transport_cm is not None
                await self._transport_cm.__aexit__(*args)
                return False

        with tempfile.TemporaryDirectory() as tmp:
            entrypoint = Path(tmp) / "dist" / "index.js"
            entrypoint.parent.mkdir()
            entrypoint.write_text("// fake", encoding="utf-8")
            with patch(
                "bridge.timetree_client._load_mcp_dependencies",
                return_value=(ConnectedClient, FakeParams, fake_transport),
            ):
                async with TimeTreeMCPClient.connect(
                    mcp_entrypoint=entrypoint,
                    calendar_id="123",
                    default_timezone="Asia/Tokyo",
                    env={"TIMETREE_EMAIL": "user@example.invalid", "TIMETREE_PASSWORD": "secret"},
                ) as client:
                    self.assertEqual(client.calendar_id, "123")

        self.assertEqual(entered, {"client": 1, "transport": 1})
        self.assertEqual(captured["command"], "node")
        self.assertEqual(set(captured["env"]), {"TIMETREE_EMAIL", "TIMETREE_PASSWORD"})


if __name__ == "__main__":
    unittest.main()
