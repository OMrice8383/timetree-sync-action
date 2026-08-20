"""P6 live recurrence-series probe for Google Calendar."""

from __future__ import annotations

import json
import sys
import time
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from googleapiclient.errors import HttpError

from bridge.adapters import normalize_google_event
from bridge.canonical import canonicalize_recurrence
from bridge.config import load_config, load_secrets
from bridge.google_client import (
    GoogleCalendarClient,
    GoogleClientError,
    google_event_body,
)
from bridge.models import ChangeType, EventKind, NormalizedEvent, Recurrence, Source


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
        source=Source.TIMETREE,
        source_calendar_id="p6-live-probe",
        source_event_id=f"p6-google-timed-{offset_days}",
        kind=EventKind.SERIES,
        parent_source_event_id=None,
        original_start=None,
        title="[P6 TEST] Google Timed Series",
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
        source=Source.TIMETREE,
        source_calendar_id="p6-live-probe",
        source_event_id="p6-google-all-day",
        kind=EventKind.SERIES,
        parent_source_event_id=None,
        original_start=None,
        title="[P6 TEST] Google All Day Series",
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


def _same_recurrence(
    actual: tuple[str, ...],
    expected: tuple[str, ...],
) -> bool:
    return canonicalize_recurrence(actual) == canonicalize_recurrence(expected)


def _wait_for_change(
    client: GoogleCalendarClient,
    sync_token: str,
    event_id: str,
    change_type: ChangeType,
) -> str:
    token = sync_token
    for _ in range(6):
        result = client.list_changes(sync_token=token)
        token = result.next_sync_token
        if any(
            change.source_event_id == event_id
            and change.change_type is change_type
            for change in result.changes
        ):
            return token
        time.sleep(1)
    raise RuntimeError(
        f"Google recurrence change not observed: {event_id} {change_type.value}"
    )


def _normalize_current(
    client: GoogleCalendarClient,
    event_id: str,
    *,
    default_timezone: str,
) -> NormalizedEvent:
    return normalize_google_event(
        client.get_event(event_id),
        source_calendar_id=client.calendar_id,
        default_timezone=default_timezone,
    )


def main() -> int:
    config = load_config()
    secrets = load_secrets(required=False)
    if not secrets.google_service_account_file:
        raise RuntimeError(
            "GOOGLE_SERVICE_ACCOUNT_FILE or GOOGLE_SERVICE_ACCOUNT_JSON is required"
        )

    client = GoogleCalendarClient.from_service_account_file(
        secrets.google_service_account_file,
        calendar_id=config.google_calendar_id,
        default_timezone=config.default_timezone,
    )

    token = client.list_changes().next_sync_token
    pending: set[str] = set()
    all_created: set[str] = set()
    checks = {
        "timed_series_create_incremental": False,
        "timed_series_read_roundtrip": False,
        "timed_series_rule_update": False,
        "recurrence_removal": False,
        "removed_series_delete_incremental": False,
        "active_series_delete_incremental": False,
        "all_day_series_roundtrip": False,
        "all_day_series_delete_incremental": False,
        "cleanup": False,
    }

    try:
        timed = _timed_series(
            default_timezone=config.default_timezone,
            offset_days=21,
        )
        private_properties = {
            "sync_source": "timetree-chatgpt-bridge",
            "timetree_id": timed.source_event_id,
            "bridge_version": config.version,
        }
        body = google_event_body(
            timed,
            private_properties=private_properties,
            allow_recurrence_write=True,
        )
        created = client.insert_event(body)
        timed_id = str(created["id"])
        pending.add(timed_id)
        all_created.add(timed_id)
        token = _wait_for_change(
            client,
            token,
            timed_id,
            ChangeType.UPSERT,
        )
        checks["timed_series_create_incremental"] = True

        normalized = _normalize_current(
            client,
            timed_id,
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
        patch = google_event_body(
            updated_series,
            private_properties=private_properties,
            allow_recurrence_write=True,
        )
        client.patch_event(timed_id, patch)
        token = _wait_for_change(
            client,
            token,
            timed_id,
            ChangeType.UPSERT,
        )
        normalized = _normalize_current(
            client,
            timed_id,
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
        patch = google_event_body(
            single,
            private_properties=private_properties,
            allow_recurrence_write=True,
            clear_recurrence=True,
        )
        client.patch_event(timed_id, patch)
        token = _wait_for_change(
            client,
            token,
            timed_id,
            ChangeType.UPSERT,
        )
        normalized = _normalize_current(
            client,
            timed_id,
            default_timezone=config.default_timezone,
        )
        checks["recurrence_removal"] = (
            normalized.kind is EventKind.SINGLE
            and not normalized.recurrence
        )

        client.delete_event(timed_id)
        token = _wait_for_change(
            client,
            token,
            timed_id,
            ChangeType.DELETE,
        )
        pending.discard(timed_id)
        checks["removed_series_delete_incremental"] = True

        active_series = _timed_series(
            default_timezone=config.default_timezone,
            offset_days=35,
        )
        active_private = {
            "sync_source": "timetree-chatgpt-bridge",
            "timetree_id": active_series.source_event_id,
            "bridge_version": config.version,
        }
        created = client.insert_event(
            google_event_body(
                active_series,
                private_properties=active_private,
                allow_recurrence_write=True,
            )
        )
        active_id = str(created["id"])
        pending.add(active_id)
        all_created.add(active_id)
        token = _wait_for_change(
            client,
            token,
            active_id,
            ChangeType.UPSERT,
        )
        client.delete_event(active_id)
        token = _wait_for_change(
            client,
            token,
            active_id,
            ChangeType.DELETE,
        )
        pending.discard(active_id)
        checks["active_series_delete_incremental"] = True

        all_day = _all_day_series(default_timezone=config.default_timezone)
        all_day_private = {
            "sync_source": "timetree-chatgpt-bridge",
            "timetree_id": all_day.source_event_id,
            "bridge_version": config.version,
        }
        created = client.insert_event(
            google_event_body(
                all_day,
                private_properties=all_day_private,
                allow_recurrence_write=True,
            )
        )
        all_day_id = str(created["id"])
        pending.add(all_day_id)
        all_created.add(all_day_id)
        token = _wait_for_change(
            client,
            token,
            all_day_id,
            ChangeType.UPSERT,
        )
        normalized = _normalize_current(
            client,
            all_day_id,
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
        client.delete_event(all_day_id)
        _wait_for_change(
            client,
            token,
            all_day_id,
            ChangeType.DELETE,
        )
        pending.discard(all_day_id)
        checks["all_day_series_delete_incremental"] = True

    finally:
        cleanup_attempt_ok = True
        for event_id in tuple(pending):
            try:
                client.delete_event(event_id)
                pending.discard(event_id)
            except (GoogleClientError, HttpError):
                cleanup_attempt_ok = False

        final = client.list_changes()
        active_probe_ids = {
            change.source_event_id
            for change in final.changes
            if change.change_type is ChangeType.UPSERT
            and change.source_event_id in all_created
        }
        checks["cleanup"] = (
            cleanup_attempt_ok
            and not pending
            and not active_probe_ids
        )

    print(json.dumps(checks, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
