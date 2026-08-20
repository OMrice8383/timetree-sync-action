"""P6 live recurrence-series probe for TimeTree-MCP."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bridge.adapters import normalize_timetree_event
from bridge.canonical import canonicalize_recurrence
from bridge.config import load_config, load_secrets
from bridge.models import EventKind, NormalizedEvent, Recurrence, Source
from bridge.timetree_client import TimeTreeClientError, TimeTreeMCPClient


def _default_mcp_entrypoint() -> Path:
    return PROJECT_ROOT.parent / "TimeTree-MCP" / "dist" / "index.js"


def _compact_utc(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _timed_series(*, default_timezone: str, offset_days: int) -> NormalizedEvent:
    zone = ZoneInfo(default_timezone)
    start = (datetime.now(zone) + timedelta(days=offset_days)).replace(
        hour=10,
        minute=0,
        second=0,
        microsecond=0,
    )
    end = start + timedelta(minutes=30)
    excluded = start + timedelta(weeks=1)
    return NormalizedEvent(
        source=Source.GOOGLE,
        source_calendar_id="p6-live-probe",
        source_event_id=f"p6-live-timed-{offset_days}",
        kind=EventKind.SERIES,
        parent_source_event_id=None,
        original_start=None,
        title="[P6 TEST] TimeTree Timed Series",
        all_day=False,
        start=start,
        end=end,
        start_timezone=default_timezone,
        end_timezone=default_timezone,
        description="P6 recurrence series live probe",
        location=None,
        recurrence=Recurrence(
            (
                "RRULE:FREQ=WEEKLY;COUNT=3",
                f"EXDATE:{_compact_utc(excluded)}",
            )
        ),
    )


def _all_day_series(*, default_timezone: str) -> NormalizedEvent:
    zone = ZoneInfo(default_timezone)
    start = datetime.now(zone).date() + timedelta(days=50)
    end = start + timedelta(days=1)
    excluded = start + timedelta(weeks=1)
    return NormalizedEvent(
        source=Source.GOOGLE,
        source_calendar_id="p6-live-probe",
        source_event_id="p6-live-all-day",
        kind=EventKind.SERIES,
        parent_source_event_id=None,
        original_start=None,
        title="[P6 TEST] TimeTree All Day Series",
        all_day=True,
        start=start,
        end=end,
        start_timezone=None,
        end_timezone=None,
        description="P6 all-day recurrence live probe",
        location=None,
        recurrence=Recurrence(
            (
                "RRULE:FREQ=WEEKLY;COUNT=3",
                f"EXDATE;VALUE=DATE:{excluded.strftime('%Y%m%d')}",
            )
        ),
    )


async def _raw_by_uuid(
    client: TimeTreeMCPClient,
    event_uuid: str,
) -> dict | None:
    events = await client.get_events()
    return next((event for event in events if event["uuid"] == event_uuid), None)


def _same_recurrence(
    actual: tuple[str, ...],
    expected: tuple[str, ...],
) -> bool:
    return canonicalize_recurrence(actual) == canonicalize_recurrence(expected)


async def run_probe(args: argparse.Namespace) -> dict[str, bool]:
    config = load_config(args.config)
    secrets = load_secrets(required=False)
    if not secrets.timetree_email or not secrets.timetree_password:
        raise RuntimeError(
            "TIMETREE_EMAIL and TIMETREE_PASSWORD must be set in the local environment"
        )

    pending: dict[str, EventKind] = {}
    all_created: set[str] = set()
    checks = {
        "connection": False,
        "timed_series_create": False,
        "timed_series_read_roundtrip": False,
        "timed_series_rule_update": False,
        "recurrence_removal": False,
        "removed_series_delete": False,
        "active_series_delete": False,
        "all_day_series_roundtrip": False,
        "all_day_series_delete": False,
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
        checks["connection"] = True

        try:
            timed = _timed_series(
                default_timezone=config.default_timezone,
                offset_days=21,
            )
            created = await client.create_event(
                timed,
                allow_recurrence_write=True,
            )
            timed_uuid = created.event_uuid
            pending[timed_uuid] = EventKind.SERIES
            all_created.add(timed_uuid)
            checks["timed_series_create"] = bool(timed_uuid)

            raw = await _raw_by_uuid(client, timed_uuid)
            if raw is None:
                raise RuntimeError("created TimeTree series was not returned by get_events")
            normalized = normalize_timetree_event(
                raw,
                default_timezone=config.default_timezone,
            )
            checks["timed_series_read_roundtrip"] = (
                normalized.kind is EventKind.SERIES
                and _same_recurrence(
                    normalized.recurrence.lines,
                    timed.recurrence.lines,
                )
            )

            until = _compact_utc(timed.start + timedelta(weeks=8))
            updated_series = replace(
                timed,
                recurrence=Recurrence(
                    (f"RRULE:FREQ=WEEKLY;INTERVAL=2;UNTIL={until}",)
                ),
            )
            await client.update_event(
                timed_uuid,
                updated_series,
                fields={"recurrence"},
                allow_recurrence_write=True,
            )
            raw = await _raw_by_uuid(client, timed_uuid)
            if raw is None:
                raise RuntimeError("updated TimeTree series disappeared")
            normalized = normalize_timetree_event(
                raw,
                default_timezone=config.default_timezone,
            )
            checks["timed_series_rule_update"] = (
                normalized.kind is EventKind.SERIES
                and _same_recurrence(
                    normalized.recurrence.lines,
                    updated_series.recurrence.lines,
                )
            )

            single = replace(
                updated_series,
                kind=EventKind.SINGLE,
                recurrence=Recurrence(),
            )
            await client.update_event(
                timed_uuid,
                single,
                fields={"recurrence"},
                allow_recurrence_write=True,
            )
            pending[timed_uuid] = EventKind.SINGLE
            raw = await _raw_by_uuid(client, timed_uuid)
            if raw is None:
                raise RuntimeError("TimeTree event disappeared after recurrence removal")
            normalized = normalize_timetree_event(
                raw,
                default_timezone=config.default_timezone,
            )
            checks["recurrence_removal"] = (
                normalized.kind is EventKind.SINGLE
                and not normalized.recurrence
            )

            deleted = await client.delete_event(
                timed_uuid,
                event_kind=EventKind.SINGLE,
            )
            checks["removed_series_delete"] = (
                deleted.event_uuid == timed_uuid
                and await _raw_by_uuid(client, timed_uuid) is None
            )
            if checks["removed_series_delete"]:
                pending.pop(timed_uuid, None)

            active_series = _timed_series(
                default_timezone=config.default_timezone,
                offset_days=35,
            )
            active_created = await client.create_event(
                active_series,
                allow_recurrence_write=True,
            )
            active_uuid = active_created.event_uuid
            pending[active_uuid] = EventKind.SERIES
            all_created.add(active_uuid)
            active_deleted = await client.delete_event(
                active_uuid,
                event_kind=EventKind.SERIES,
                allow_recurrence_write=True,
            )
            checks["active_series_delete"] = (
                active_deleted.event_uuid == active_uuid
                and await _raw_by_uuid(client, active_uuid) is None
            )
            if checks["active_series_delete"]:
                pending.pop(active_uuid, None)

            all_day = _all_day_series(default_timezone=config.default_timezone)
            all_day_created = await client.create_event(
                all_day,
                allow_recurrence_write=True,
            )
            all_day_uuid = all_day_created.event_uuid
            pending[all_day_uuid] = EventKind.SERIES
            all_created.add(all_day_uuid)
            raw = await _raw_by_uuid(client, all_day_uuid)
            if raw is None:
                raise RuntimeError("created TimeTree all-day series was not returned")
            normalized = normalize_timetree_event(
                raw,
                default_timezone=config.default_timezone,
            )
            checks["all_day_series_roundtrip"] = (
                normalized.kind is EventKind.SERIES
                and normalized.all_day
                and isinstance(normalized.start, date)
                and normalized.start == all_day.start
                and normalized.end == all_day.end
                and _same_recurrence(
                    normalized.recurrence.lines,
                    all_day.recurrence.lines,
                )
            )
            all_day_deleted = await client.delete_event(
                all_day_uuid,
                event_kind=EventKind.SERIES,
                allow_recurrence_write=True,
            )
            checks["all_day_series_delete"] = (
                all_day_deleted.event_uuid == all_day_uuid
                and await _raw_by_uuid(client, all_day_uuid) is None
            )
            if checks["all_day_series_delete"]:
                pending.pop(all_day_uuid, None)

        finally:
            cleanup_attempt_ok = True
            for event_uuid, kind in tuple(pending.items()):
                try:
                    await client.delete_event(
                        event_uuid,
                        event_kind=kind,
                        allow_recurrence_write=True,
                    )
                    pending.pop(event_uuid, None)
                except TimeTreeClientError:
                    cleanup_attempt_ok = False

            remaining = await client.get_events()
            remaining_ids = {event["uuid"] for event in remaining}
            checks["cleanup"] = (
                cleanup_attempt_ok
                and not pending
                and all_created.isdisjoint(remaining_ids)
            )

    return checks


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
