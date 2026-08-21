from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bridge.config import load_config, load_secrets
from bridge.models import (
    DEFAULT_TIMETREE_LABEL_NAME,
    EventKind,
    NormalizedEvent,
    Recurrence,
    Source,
)
from bridge.timetree_client import TimeTreeClientError, TimeTreeMCPClient

OBSERVED_EVENT_FIELDS = (
    "uuid",
    "parent_id",
    "recurring_uuid",
    "start_at",
    "end_at",
    "start_timezone",
    "end_timezone",
    "all_day",
    "recurrences",
    "updated_at",
    "deactivated_at",
    "category",
    "type",
)


@dataclass(frozen=True)
class ProbeSeries:
    label: str
    title: str
    master_uuid: str
    baseline_recurrences: tuple[str, ...]
    baseline_updated_at: int | None
    created_master_label_id: int | None
    resolved_label_name: str | None
    label_match: bool
    updated_after_watermark: int
    baseline_master: Mapping[str, Any]


def _default_mcp_entrypoint() -> Path:
    return PROJECT_ROOT.parent / "TimeTree-MCP" / "dist" / "index.js"


def _now_ms() -> int:
    return int(datetime.now(tz=UTC).timestamp() * 1000)


def _timed_series(
    *,
    default_timezone: str,
    label: str,
    title: str,
    offset_days: int,
) -> NormalizedEvent:
    zone = ZoneInfo(default_timezone)
    start = (datetime.now(tz=zone) + timedelta(days=offset_days)).replace(
        hour=10,
        minute=0,
        second=0,
        microsecond=0,
    )
    return NormalizedEvent(
        source=Source.TIMETREE,
        source_calendar_id="p7-live-probe",
        source_event_id=f"p7-{label}-source",
        kind=EventKind.SERIES,
        parent_source_event_id=None,
        original_start=None,
        title=title,
        all_day=False,
        start=start,
        end=start + timedelta(minutes=30),
        start_timezone=default_timezone,
        end_timezone=default_timezone,
        description="P7 disposable recurrence exception read probe",
        location=None,
        label=DEFAULT_TIMETREE_LABEL_NAME,
        recurrence=Recurrence(("RRULE:FREQ=WEEKLY;COUNT=4",)),
    )


def _find_by_uuid(
    events: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    event_uuid: str,
) -> dict[str, Any] | None:
    return next((event for event in events if event.get("uuid") == event_uuid), None)


def _safe_watermark(master: Mapping[str, Any]) -> int:
    updated_at = master.get("updated_at")
    if isinstance(updated_at, int) and not isinstance(updated_at, bool):
        # Keep a small overlap so a boundary-rounded server timestamp cannot hide
        # the first UI mutation. This is a read watermark, not an identity.
        return max(0, updated_at - 30_000)
    return max(0, _now_ms() - 60_000)


async def _create_probe_series(
    client: TimeTreeMCPClient,
    event: NormalizedEvent,
    *,
    label: str,
) -> ProbeSeries:
    created = await client.create_event(event, allow_recurrence_write=True)
    events = await client.get_events()
    master = _find_by_uuid(events, created.event_uuid)
    if master is None:
        raise RuntimeError(f"created {label} master was not returned by get_events")

    recurrences = master.get("recurrences")
    if not isinstance(recurrences, list) or not all(
        isinstance(line, str) for line in recurrences
    ):
        raise RuntimeError(f"created {label} master has an invalid recurrence payload")
    updated_at = master.get("updated_at")
    if isinstance(updated_at, bool) or not isinstance(updated_at, (int, type(None))):
        raise TypeError(f"created {label} master has an invalid updated_at payload")

    label_catalog = await client.get_calendar_labels()
    label_id = master.get("label_id")
    created_master_label_id = (
        label_id if isinstance(label_id, int) and not isinstance(label_id, bool) else None
    )
    try:
        resolved_label_name = label_catalog.label_name_for_id(label_id)
    except (TypeError, ValueError):
        resolved_label_name = None

    return ProbeSeries(
        label=label,
        title=event.title,
        master_uuid=created.event_uuid,
        baseline_recurrences=tuple(recurrences),
        baseline_updated_at=updated_at,
        created_master_label_id=created_master_label_id,
        resolved_label_name=resolved_label_name,
        label_match=resolved_label_name == DEFAULT_TIMETREE_LABEL_NAME,
        updated_after_watermark=_safe_watermark(master),
        baseline_master=dict(master),
    )


def _master_relation_values(master: Mapping[str, Any]) -> set[str]:
    # These values are only used to select structural candidates. parent_id is
    # not promoted to the canonical parent UUID by this probe.
    values: set[str] = set()
    for field in ("id", "event_id", "uuid"):
        value = master.get(field)
        if value is not None:
            values.add(str(value))
    return values


def _related_events(
    events: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    series: ProbeSeries,
) -> list[dict[str, Any]]:
    master = series.baseline_master
    relation_values = _master_relation_values(master)
    related: list[dict[str, Any]] = []
    seen: set[str] = set()
    for event in events:
        event_uuid = event.get("uuid")
        if not isinstance(event_uuid, str) or event_uuid in seen:
            continue
        recurring_uuid = event.get("recurring_uuid")
        parent_id = event.get("parent_id")
        is_related = (
            event_uuid == series.master_uuid
            or recurring_uuid == series.master_uuid
            or (parent_id is not None and str(parent_id) in relation_values)
        )
        if is_related:
            related.append(event)
            seen.add(event_uuid)
    return related


def _children(
    events: list[dict[str, Any]], series: ProbeSeries
) -> list[dict[str, Any]]:
    return [event for event in events if event.get("uuid") != series.master_uuid]


def _sanitize_event(event: Mapping[str, Any]) -> dict[str, Any]:
    return {
        field: event.get(field) for field in OBSERVED_EVENT_FIELDS if field in event
    }


def _added_recurrence_lines(
    current_master: Mapping[str, Any] | None,
    series: ProbeSeries,
) -> list[str]:
    if current_master is None:
        return []
    current = current_master.get("recurrences")
    if not isinstance(current, list):
        return []
    return [line for line in current if line not in series.baseline_recurrences]


def _edit_observation(
    full_read: tuple[dict[str, Any], ...],
    updated_delta: tuple[dict[str, Any], ...],
    series: ProbeSeries,
) -> dict[str, Any]:
    related = _related_events(full_read, series)
    delta_related = _related_events(updated_delta, series)
    current_master = _find_by_uuid(full_read, series.master_uuid)
    children = _children(related, series)
    delta_children = _children(delta_related, series)
    added_lines = _added_recurrence_lines(current_master, series)
    has_added_exdate = any(line.upper().startswith("EXDATE") for line in added_lines)

    return {
        "edit_exception_detected": bool(children),
        "edit_child_uuid_distinct": any(
            event.get("uuid") != series.master_uuid for event in children
        ),
        "edit_recurring_uuid_matches_master": any(
            event.get("recurring_uuid") == series.master_uuid for event in children
        ),
        "edit_parent_id_present": any(
            event.get("parent_id") is not None for event in children
        ),
        "edit_master_recurrence_changed": (
            current_master is not None
            and current_master.get("recurrences") != list(series.baseline_recurrences)
        ),
        "edit_master_added_recurrence_lines": added_lines,
        "edit_updated_delta_contains_master": any(
            event.get("uuid") == series.master_uuid for event in updated_delta
        ),
        "edit_updated_delta_contains_child": bool(delta_children),
        "edit_master_present_after": current_master is not None,
        "original_start_status": "UNRESOLVED",
        "original_start_candidate_only": bool(children and has_added_exdate),
        "relevant_events": [_sanitize_event(event) for event in related],
        "updated_delta_events": [_sanitize_event(event) for event in delta_related],
    }


def _delete_observation(
    full_read: tuple[dict[str, Any], ...],
    updated_delta: tuple[dict[str, Any], ...],
    series: ProbeSeries,
) -> dict[str, Any]:
    related = _related_events(full_read, series)
    delta_related = _related_events(updated_delta, series)
    current_master = _find_by_uuid(full_read, series.master_uuid)
    children = _children(related, series)
    delta_children = _children(delta_related, series)
    master_changed = current_master is not None and current_master.get(
        "recurrences"
    ) != list(series.baseline_recurrences)

    return {
        "delete_exception_detected": bool(
            master_changed
            or children
            or delta_children
            or any(event.get("deactivated_at") is not None for event in children)
        ),
        "delete_master_recurrence_changed": master_changed,
        "delete_master_added_recurrence_lines": _added_recurrence_lines(
            current_master,
            series,
        ),
        "delete_child_present": bool(children),
        "delete_child_deactivated_at_present": any(
            event.get("deactivated_at") is not None for event in children
        ),
        "delete_updated_delta_contains_master": any(
            event.get("uuid") == series.master_uuid for event in updated_delta
        ),
        "delete_updated_delta_contains_child": bool(delta_children),
        "delete_master_present_after": current_master is not None,
        "relevant_events": [_sanitize_event(event) for event in related],
        "updated_delta_events": [_sanitize_event(event) for event in delta_related],
    }


def _series_metadata(series: ProbeSeries) -> dict[str, Any]:
    return {
        "master_uuid": series.master_uuid,
        "baseline_recurrences": list(series.baseline_recurrences),
        "baseline_updated_at": series.baseline_updated_at,
        "created_master_label_id": series.created_master_label_id,
        "resolved_label_name": series.resolved_label_name,
        "expected_label_name": DEFAULT_TIMETREE_LABEL_NAME,
        "label_match": series.label_match,
        "updated_after_watermark": series.updated_after_watermark,
    }


def _redact_error(message: str) -> str:
    redacted = message
    for key in (
        "TIMETREE_EMAIL",
        "TIMETREE_PASSWORD",
        "GOOGLE_SERVICE_ACCOUNT_JSON",
        "GOOGLE_SERVICE_ACCOUNT_FILE",
    ):
        value = os.getenv(key)
        if value:
            redacted = redacted.replace(value, "<redacted>")
    return redacted


async def run_probe(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config(args.config)
    secrets = load_secrets(required=False)
    if not secrets.timetree_email or not secrets.timetree_password:
        raise RuntimeError(
            "TIMETREE_EMAIL and TIMETREE_PASSWORD must be set in the local environment"
        )

    created: dict[str, ProbeSeries] = {}
    result: dict[str, Any] = {
        "ok": False,
        "edit_series": None,
        "edit_observation": None,
        "delete_series": None,
        "delete_observation": None,
        "error": None,
        "cleanup": False,
        "cleanup_master_delete_errors": [],
        "cleanup_detached_candidates": [],
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
        try:
            edit_series = await _create_probe_series(
                client,
                _timed_series(
                    default_timezone=config.default_timezone,
                    label="edit",
                    title="[P7 TEST] TimeTree Exception Edit",
                    offset_days=3,
                ),
                label="edit",
            )
            created[edit_series.master_uuid] = edit_series

            delete_series = await _create_probe_series(
                client,
                _timed_series(
                    default_timezone=config.default_timezone,
                    label="delete",
                    title="[P7 TEST] TimeTree Exception Delete",
                    offset_days=10,
                ),
                label="delete",
            )
            created[delete_series.master_uuid] = delete_series

            print(
                "\n[P7 EDIT] Open TimeTree and choose a non-first occurrence "
                "of '[P7 TEST] TimeTree Exception Edit'."
            )
            print(
                "Change only that occurrence; prefer changing its time so the "
                "actual start differs from the original slot."
            )
            input(
                "Return to this terminal and press Enter to perform the read-only observation: "
            )

            edit_full = await client.get_events()
            edit_delta = await client.get_updated_events(
                edit_series.updated_after_watermark
            )
            result["edit_series"] = _series_metadata(edit_series)
            result["edit_observation"] = _edit_observation(
                edit_full,
                edit_delta,
                edit_series,
            )
            print(
                json.dumps(
                    {
                        "edit_series": result["edit_series"],
                        "edit_observation": result["edit_observation"],
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )

            print(
                "\n[P7 DELETE] Open TimeTree and choose a non-first occurrence "
                "of '[P7 TEST] TimeTree Exception Delete'."
            )
            print("Delete only that occurrence, not the whole series.")
            input(
                "Return to this terminal and press Enter to perform the read-only observation: "
            )

            delete_full = await client.get_events()
            delete_delta = await client.get_updated_events(
                delete_series.updated_after_watermark
            )
            result["delete_series"] = _series_metadata(delete_series)
            result["delete_observation"] = _delete_observation(
                delete_full,
                delete_delta,
                delete_series,
            )
            print(
                json.dumps(
                    {
                        "delete_series": result["delete_series"],
                        "delete_observation": result["delete_observation"],
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
            result["ok"] = True
        except (EOFError, OSError, RuntimeError, TimeTreeClientError) as exc:
            result["error"] = {
                "type": type(exc).__name__,
                "message": _redact_error(str(exc)),
            }
        finally:
            cleanup_errors: list[dict[str, str]] = []
            for event_uuid, series in tuple(created.items()):
                try:
                    deleted = await client.delete_event(
                        event_uuid,
                        event_kind=EventKind.SERIES,
                        allow_recurrence_write=True,
                    )
                    if deleted.event_uuid != event_uuid:
                        cleanup_errors.append(
                            {
                                "label": series.label,
                                "master_uuid": event_uuid,
                                "error": "delete returned a different UUID",
                            }
                        )
                except TimeTreeClientError as exc:
                    cleanup_errors.append(
                        {
                            "label": series.label,
                            "master_uuid": event_uuid,
                            "error": type(exc).__name__,
                        }
                    )

            result["cleanup_master_delete_errors"] = cleanup_errors
            try:
                remaining = await client.get_events()
                created_ids = set(created)
                remaining_master_ids = {
                    event.get("uuid")
                    for event in remaining
                    if event.get("uuid") in created_ids
                }
                detached = [
                    {
                        "uuid": event.get("uuid"),
                        "title_prefix": created[event.get("recurring_uuid")].title,
                    }
                    for event in remaining
                    if (
                        event.get("recurring_uuid") in created_ids
                        and event.get("uuid") not in created_ids
                        and event.get("recurring_uuid") in created
                    )
                ]
                result["cleanup_detached_candidates"] = detached
                result["cleanup"] = (
                    not cleanup_errors and not remaining_master_ids and not detached
                )
                if detached:
                    print(
                        "cleanup=false: a detached P7 test candidate remains; "
                        "do not call an unverified exception delete API."
                    )
                    print(json.dumps(detached, indent=2, sort_keys=True))
            except TimeTreeClientError as exc:
                result["cleanup_master_delete_errors"].append(
                    {"label": "verification", "error": type(exc).__name__}
                )
                result["cleanup"] = False

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

    try:
        result = asyncio.run(run_probe(args))
    except (EOFError, OSError, RuntimeError, TimeTreeClientError) as exc:
        result = {
            "ok": False,
            "error": {
                "type": type(exc).__name__,
                "message": _redact_error(str(exc)),
            },
            "cleanup": False,
        }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") and result.get("cleanup") else 1


if __name__ == "__main__":
    raise SystemExit(main())
