from __future__ import annotations

import json
import time
from dataclasses import replace
from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from bridge.config import load_config, load_secrets
from bridge.google_client import GoogleCalendarClient, google_event_body
from bridge.models import ChangeType, EventKind, NormalizedEvent, Source


def _contains_change(result, event_id: str, change_type: ChangeType) -> bool:
    return any(
        change.source_event_id == event_id and change.change_type is change_type
        for change in result.changes
    )


def _wait_for_change(
    client: GoogleCalendarClient,
    sync_token: str,
    event_id: str,
    change_type: ChangeType,
) -> str:
    current_token = sync_token
    for _ in range(5):
        result = client.list_changes(sync_token=current_token)
        current_token = result.next_sync_token
        if _contains_change(result, event_id, change_type):
            return current_token
        time.sleep(1)
    raise RuntimeError(f"Google change not observed: {event_id} {change_type.value}")


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

    metadata = client.get_calendar_metadata()
    baseline = client.list_changes()
    active_before = [
        change
        for change in baseline.changes
        if change.change_type is ChangeType.UPSERT
    ]
    if active_before:
        raise RuntimeError(
            "Dedicated Google calendar contains active events; P4 live probe aborted"
        )

    probe_uuid = uuid4().hex
    zone = ZoneInfo(config.default_timezone)
    start = datetime.now(zone).replace(second=0, microsecond=0) + timedelta(days=30)
    end = start + timedelta(minutes=30)
    event = NormalizedEvent(
        source=Source.TIMETREE,
        source_calendar_id="p4-live-probe",
        source_event_id=probe_uuid,
        kind=EventKind.SINGLE,
        parent_source_event_id=None,
        original_start=None,
        title="[P4 TEST] Google Client Probe",
        all_day=False,
        start=start,
        end=end,
        start_timezone=config.default_timezone,
        end_timezone=config.default_timezone,
        description="P4 live probe",
        location=None,
    )
    private_properties = {
        "sync_source": "timetree-chatgpt-bridge",
        "timetree_id": probe_uuid,
        "bridge_version": config.version,
    }

    created_id: str | None = None
    deleted = False
    token = baseline.next_sync_token
    checks = {
        "calendar_metadata": bool(metadata.get("id")),
        "initial_active_events": 0,
        "create_incremental": False,
        "update_incremental": False,
        "patch_preserved_unsynced_field": False,
        "delete_incremental": False,
        "cleanup_active_events": False,
    }

    try:
        create_body = google_event_body(
            event,
            private_properties=private_properties,
        )
        create_body["transparency"] = "transparent"
        created = client.insert_event(create_body)
        created_id = str(created["id"])
        token = _wait_for_change(client, token, created_id, ChangeType.UPSERT)
        checks["create_incremental"] = True

        updated_event = replace(event, title="[P4 TEST] Google Client Probe Updated")
        patch_body = google_event_body(
            updated_event,
            private_properties=private_properties,
        )
        client.patch_event(created_id, patch_body)
        current = client.get_event(created_id)
        if current.get("transparency") != "transparent":
            raise RuntimeError("events.patch did not preserve unsynced transparency")
        checks["patch_preserved_unsynced_field"] = True

        token = _wait_for_change(client, token, created_id, ChangeType.UPSERT)
        checks["update_incremental"] = True

        client.delete_event(created_id)
        deleted = True
        _wait_for_change(client, token, created_id, ChangeType.DELETE)
        checks["delete_incremental"] = True

    finally:
        if created_id is not None and not deleted:
            client.delete_event(created_id)

    final = client.list_changes()
    active_after = [
        change
        for change in final.changes
        if change.change_type is ChangeType.UPSERT
    ]
    checks["cleanup_active_events"] = not active_after

    print(json.dumps(checks, ensure_ascii=False, indent=2, sort_keys=True))
    required_true = (
        "calendar_metadata",
        "create_incremental",
        "update_incremental",
        "patch_preserved_unsynced_field",
        "delete_incremental",
        "cleanup_active_events",
    )
    if checks["initial_active_events"] != 0:
        return 1
    if any(checks[name] is not True for name in required_true):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
