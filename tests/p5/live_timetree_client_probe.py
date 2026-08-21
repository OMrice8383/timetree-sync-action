from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bridge.adapters import normalize_timetree_event
from bridge.config import load_config, load_secrets
from bridge.models import EventKind, NormalizedEvent, Source
from bridge.timetree_client import TimeTreeClientError, TimeTreeMCPClient


def _default_mcp_entrypoint() -> Path:
    return PROJECT_ROOT.parent / "TimeTree-MCP" / "dist" / "index.js"


def _build_probe_event(*, default_timezone: str) -> NormalizedEvent:
    zone = ZoneInfo(default_timezone)
    start = (datetime.now(tz=zone) + timedelta(days=2)).replace(
        second=0,
        microsecond=0,
    )
    end = start + timedelta(minutes=30)
    return NormalizedEvent(
        source=Source.GOOGLE,
        source_calendar_id="p5-live-probe",
        source_event_id="p5-live-source",
        kind=EventKind.SINGLE,
        parent_source_event_id=None,
        original_start=None,
        title="BRIDGE-P5-LIVE",
        all_day=False,
        start=start,
        end=end,
        start_timezone=default_timezone,
        end_timezone=default_timezone,
        description=None,
        location=None,
    )


async def run_probe(args: argparse.Namespace) -> dict[str, bool]:
    config = load_config(args.config)
    secrets = load_secrets(required=False)
    if not secrets.timetree_email or not secrets.timetree_password:
        raise RuntimeError(
            "TIMETREE_EMAIL and TIMETREE_PASSWORD must be set in the local environment"
        )

    probe_started_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
    created_uuid: str | None = None
    cleanup_ok = False

    result = {
        "connection": False,
        "target_calendar_found": False,
        "full_read": False,
        "create_uuid": False,
        "create_read_same_uuid": False,
        "incremental_read": False,
        "update_same_uuid": False,
        "update_read": False,
        "delete_same_uuid": False,
        "delete_absent_from_full_read": False,
        "cleanup": False,
    }

    async with TimeTreeMCPClient.connect(
        mcp_entrypoint=args.mcp_entrypoint,
        calendar_id=config.timetree_calendar_id,
        default_timezone=config.default_timezone,
        env={
            "TIMETREE_EMAIL": secrets.timetree_email,
            "TIMETREE_PASSWORD": secrets.timetree_password,
        },
    ) as client:
        result["connection"] = True

        calendars = await client.list_calendars()
        result["target_calendar_found"] = any(
            calendar.calendar_id == config.timetree_calendar_id
            for calendar in calendars
        )
        if not result["target_calendar_found"]:
            raise RuntimeError("configured TimeTree calendar was not returned by list_calendars")

        label_catalog = await client.get_calendar_labels()

        baseline = await client.get_events()
        result["full_read"] = isinstance(baseline, tuple)

        probe_event = _build_probe_event(default_timezone=config.default_timezone)
        try:
            created = await client.create_event(probe_event)
            created_uuid = created.event_uuid
            result["create_uuid"] = bool(created_uuid)

            after_create = await client.get_events()
            created_raw = next(
                (event for event in after_create if event["uuid"] == created_uuid),
                None,
            )
            if created_raw is None:
                raise RuntimeError("created TimeTree UUID was not returned by get_events")
            created_normalized = normalize_timetree_event(
                created_raw,
                default_timezone=config.default_timezone,
                label_catalog=label_catalog,
            )
            result["create_read_same_uuid"] = (
                created_normalized.source_event_id == created_uuid
            )

            incremental = await client.get_updated_events(probe_started_ms - 30_000)
            result["incremental_read"] = any(
                event["uuid"] == created_uuid for event in incremental
            )

            updated_event = replace(probe_event, title="BRIDGE-P5-LIVE-UPDATED")
            updated = await client.update_event(
                created_uuid,
                updated_event,
                fields={"title"},
            )
            result["update_same_uuid"] = updated.event_uuid == created_uuid

            after_update = await client.get_events()
            updated_raw = next(
                (event for event in after_update if event["uuid"] == created_uuid),
                None,
            )
            if updated_raw is None:
                raise RuntimeError("updated TimeTree UUID was not returned by get_events")
            updated_normalized = normalize_timetree_event(
                updated_raw,
                default_timezone=config.default_timezone,
                label_catalog=label_catalog,
            )
            result["update_read"] = (
                updated_normalized.source_event_id == created_uuid
                and updated_normalized.title == "BRIDGE-P5-LIVE-UPDATED"
            )

            deleted = await client.delete_event(
                created_uuid,
                event_kind=EventKind.SINGLE,
            )
            result["delete_same_uuid"] = deleted.event_uuid == created_uuid

            after_delete = await client.get_events()
            result["delete_absent_from_full_read"] = not any(
                event["uuid"] == deleted.event_uuid for event in after_delete
            )
            cleanup_ok = result["delete_absent_from_full_read"]
            if cleanup_ok:
                created_uuid = None
        finally:
            if created_uuid is not None:
                try:
                    await client.delete_event(
                        created_uuid,
                        event_kind=EventKind.SINGLE,
                    )
                    cleanup_ok = True
                except TimeTreeClientError:
                    cleanup_ok = False
            result["cleanup"] = cleanup_ok

    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/bridge.toml")
    parser.add_argument(
        "--mcp-entrypoint",
        type=Path,
        default=_default_mcp_entrypoint(),
    )
    args = parser.parse_args()

    result = asyncio.run(run_probe(args))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if all(result.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
