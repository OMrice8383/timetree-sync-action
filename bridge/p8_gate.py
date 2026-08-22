from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from .adapters import (
    UnsupportedEventError,
    classify_timetree_event,
    has_timetree_exception_evidence,
    normalize_timetree_event,
)
from .bootstrap import (
    BOOTSTRAP_OPERATION_PREFIX,
    BootstrapError,
    BootstrapRunner,
    _managed_timetree_id,
    _private_properties,
)
from .models import (
    GOOGLE_BRIDGE_SYNC_SOURCE,
    SYNC_TIMETREE_LABEL_NAMES,
    ChangeType,
    EventClassification,
    TimeTreeLabelCatalog,
)
from .recurrence import RecurrenceContractError, recurrence_property_name
from .repository import StateRepository

GOOGLE_WRITE_ACCESS_ROLES = frozenset({"owner", "writer"})
_SAFE_RECURRENCE_LINE = re.compile(r"[A-Za-z0-9_+.;=,:/\-]+")


def _append_reason(reasons: list[str], code: str) -> None:
    if code not in reasons:
        reasons.append(code)


def _error_code(exc: BaseException, fallback: str) -> str:
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return code
    return fallback


def database_preflight(repository: StateRepository) -> dict[str, Any]:
    connection = repository.connection
    event_link_count = int(
        connection.execute("SELECT COUNT(*) FROM event_links").fetchone()[0]
    )
    pending_operation_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM sync_operations WHERE state NOT IN ('done', 'failed')"
        ).fetchone()[0]
    )
    open_conflict_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM conflicts WHERE status = 'open'"
        ).fetchone()[0]
    )
    bootstrapped = repository.get_sync_state("bridge_bootstrapped_at") is not None
    return {
        "bootstrapped": bootstrapped,
        "event_link_count": event_link_count,
        "pending_operation_count": pending_operation_count,
        "open_conflict_count": open_conflict_count,
        "ready": not (
            bootstrapped
            or event_link_count
            or pending_operation_count
            or open_conflict_count
        ),
    }


def _raw_change(
    runner: BootstrapRunner,
    change: Any,
) -> Mapping[str, Any] | None:
    return runner._raw_google_change(change)


def _google_read_preflight(
    *,
    runner: BootstrapRunner,
    google_client: Any,
    repository: StateRepository,
) -> tuple[dict[str, Any], list[str]]:
    reasons: list[str] = []
    result: Any = None
    metadata: Mapping[str, Any] | None = None
    google = {
        "connected": False,
        "target_calendar_found": False,
        "full_sync_success": False,
        "access_role": None,
        "live_event_count": 0,
        "tombstone_count": 0,
        "managed_count": 0,
        "unmanaged_count": 0,
        "next_sync_token_observed": False,
    }

    try:
        metadata = runner._google_target_preflight()
        google["target_calendar_found"] = True
    except Exception as exc:  # noqa: BLE001 - external read boundary
        _append_reason(
            reasons,
            _error_code(exc, "GOOGLE_TARGET_PREFLIGHT_FAILED"),
        )

    try:
        result = google_client.list_changes()
        changes = getattr(result, "changes", None)
        token = getattr(result, "next_sync_token", None)
        if not isinstance(changes, Sequence) or isinstance(changes, (str, bytes)):
            raise BootstrapError(
                "UNSAFE_GOOGLE_SNAPSHOT",
                "Google full snapshot changes are not a sequence",
            )
        if not isinstance(token, str) or not token:
            raise BootstrapError(
                "UNSAFE_GOOGLE_SNAPSHOT",
                "Google full snapshot is missing nextSyncToken",
            )
        google["full_sync_success"] = True
        google["next_sync_token_observed"] = True
        google["connected"] = metadata is not None

        access_role = getattr(result, "access_role", None)
        if access_role is None and metadata is not None:
            access_role = metadata.get("accessRole")
        if isinstance(access_role, str) and access_role:
            google["access_role"] = access_role
        else:
            _append_reason(reasons, "GOOGLE_ACCESS_ROLE_MISSING")
        if access_role not in GOOGLE_WRITE_ACCESS_ROLES:
            _append_reason(reasons, "GOOGLE_WRITER_PERMISSION_REQUIRED")

        managed_ids: list[tuple[str, str]] = []
        for change in changes:
            if getattr(change, "change_type", None) is not ChangeType.UPSERT:
                google["tombstone_count"] += 1
                continue

            google["live_event_count"] += 1
            raw = _raw_change(runner, change)
            if raw is None:
                google["unmanaged_count"] += 1
                _append_reason(reasons, "UNSAFE_GOOGLE_SNAPSHOT")
                continue
            private = _private_properties(raw)
            if (
                private is None
                or private.get("sync_source") != GOOGLE_BRIDGE_SYNC_SOURCE
            ):
                google["unmanaged_count"] += 1
                continue

            google["managed_count"] += 1
            try:
                timetree_id = _managed_timetree_id(raw)
            except BootstrapError as exc:
                _append_reason(reasons, exc.code)
                continue
            target_id = raw.get("id")
            if isinstance(target_id, str) and target_id:
                managed_ids.append((timetree_id, target_id))

        if google["unmanaged_count"]:
            _append_reason(reasons, "UNMANAGED_GOOGLE_EVENT")

        try:
            runner._google_preflight_snapshot(changes, token)
        except Exception as exc:  # noqa: BLE001 - external read boundary
            _append_reason(
                reasons,
                _error_code(exc, "UNSAFE_GOOGLE_SNAPSHOT"),
            )

        for timetree_id, target_id in managed_ids:
            link = repository.get_event_link_by_timetree_id(timetree_id)
            operation = repository.get_operation(
                BOOTSTRAP_OPERATION_PREFIX + timetree_id
            )
            if link is None and operation is None:
                _append_reason(reasons, "GOOGLE_MANAGED_EVENT_UNTRACKED")
            if link is not None and link.get("google_event_id") != target_id:
                _append_reason(reasons, "GOOGLE_MANAGED_MAPPING_MISMATCH")
            if (
                operation is not None
                and operation.get("source_event_id") != timetree_id
            ):
                _append_reason(reasons, "GOOGLE_MANAGED_OPERATION_MISMATCH")
    except Exception as exc:  # noqa: BLE001 - external read boundary
        _append_reason(reasons, _error_code(exc, "UNSAFE_GOOGLE_SNAPSHOT"))

    if metadata is not None:
        metadata_id = metadata.get("id")
        configured_id = str(getattr(google_client, "calendar_id", ""))
        if str(metadata_id) != configured_id:
            google["target_calendar_found"] = False
            _append_reason(reasons, "GOOGLE_TARGET_CALENDAR_MISMATCH")

    return google, reasons


def _label_counts(
    raw_events: Sequence[Mapping[str, Any]],
    label_catalog: TimeTreeLabelCatalog,
) -> dict[str, int]:
    counts = {name: 0 for name in SYNC_TIMETREE_LABEL_NAMES}
    counts.update(
        {
            "LABEL_OUT_OF_SCOPE": 0,
            "LABEL_UNRESOLVED": 0,
            "unnamed_out_of_scope_label_event_count": 0,
        }
    )
    for raw in raw_events:
        if not isinstance(raw, Mapping):
            counts["LABEL_UNRESOLVED"] += 1
            continue
        classification = classify_timetree_event(raw, label_catalog=None)
        if classification.code != "TIMETREE_LABEL_CATALOG_REQUIRED":
            continue
        try:
            label_name = label_catalog.sync_label_name_for_id(raw.get("label_id"))
        except (TypeError, ValueError):
            counts["LABEL_UNRESOLVED"] += 1
            continue
        if label_name is not None:
            counts[label_name] += 1
            continue
        known_label = next(
            (
                label
                for label in label_catalog.labels
                if label.label_id == raw.get("label_id")
            ),
            None,
        )
        if known_label is not None:
            counts["LABEL_OUT_OF_SCOPE"] += 1
            if known_label.label_name is None:
                counts["unnamed_out_of_scope_label_event_count"] += 1
        else:
            counts["LABEL_UNRESOLVED"] += 1
    return counts


def _safe_recurrence_line(line: object) -> str:
    if not isinstance(line, str):
        return "<invalid-recurrence-line>"
    value = line.strip()
    if len(value) > 512 or _SAFE_RECURRENCE_LINE.fullmatch(value) is None:
        return "<redacted-invalid-recurrence-line>"
    return value


def _recurrence_property_name_for_diagnostic(line: object) -> str:
    if not isinstance(line, str):
        return "INVALID"
    try:
        return recurrence_property_name(line)
    except RecurrenceContractError:
        return "INVALID"


def _recurrence_diagnostic(
    *,
    raw: Mapping[str, Any],
    runner: BootstrapRunner,
    label_catalog: TimeTreeLabelCatalog,
) -> dict[str, Any] | None:
    raw_lines = raw.get("recurrences")
    if not isinstance(raw_lines, Sequence) or isinstance(raw_lines, (str, bytes)):
        return None

    lines = [_safe_recurrence_line(line) for line in raw_lines]
    property_names = [
        _recurrence_property_name_for_diagnostic(line) for line in raw_lines
    ]
    all_day = raw.get("all_day") if isinstance(raw.get("all_day"), bool) else None
    start_timezone = raw.get("start_timezone")
    end_timezone = raw.get("end_timezone")
    effective_start = (
        start_timezone.strip()
        if isinstance(start_timezone, str) and start_timezone.strip()
        else runner.default_timezone
    )
    effective_end = (
        end_timezone.strip()
        if isinstance(end_timezone, str) and end_timezone.strip()
        else runner.default_timezone
    )
    try:
        normalize_timetree_event(
            raw,
            default_timezone=runner.default_timezone,
            label_catalog=label_catalog,
        )
    except UnsupportedEventError as exc:
        if exc.code != "UNSUPPORTED_RECURRENCE_FEATURE":
            return None
        return {
            "event_kind": "series" if raw_lines else "single",
            "all_day": all_day,
            "start_timezone_present": bool(effective_start),
            "end_timezone_present": bool(effective_end),
            "effective_timezone_relation": (
                "same" if effective_start == effective_end else "different"
            ),
            "property_names": property_names,
            "recurrence_lines": lines,
            "reason_code": exc.code,
            "reason": str(exc),
        }
    except (ValueError, TypeError):
        return None
    return None


def _aggregate_recurrence_diagnostics(
    diagnostics: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    shapes: list[dict[str, Any]] = []
    indexes: dict[str, int] = {}
    for diagnostic in diagnostics:
        key = json.dumps(
            diagnostic,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        index = indexes.get(key)
        if index is None:
            indexes[key] = len(shapes)
            shapes.append({**diagnostic, "count": 1})
        else:
            shapes[index]["count"] += 1
    return {
        "unsupported_count": len(diagnostics),
        "shapes": shapes,
    }


async def _timetree_read_preflight(
    *,
    runner: BootstrapRunner,
    timetree_client: Any,
) -> tuple[dict[str, Any], list[str]]:
    reasons: list[str] = []
    timetree = {
        "connected": False,
        "target_calendar_found": False,
        "labels_resolved": False,
        "raw_event_count": 0,
        "eligible_count": 0,
        "ignored_count": 0,
        "ignored_reasons": {},
        "unsupported_count": 0,
        "label_counts": {
            **{name: 0 for name in SYNC_TIMETREE_LABEL_NAMES},
            "LABEL_OUT_OF_SCOPE": 0,
            "LABEL_UNRESOLVED": 0,
            "unnamed_out_of_scope_label_event_count": 0,
        },
        "exception_evidence_count": 0,
        "unsupported_reasons": {},
    }

    try:
        calendars = await timetree_client.list_calendars()
        timetree["target_calendar_found"] = any(
            str(calendar.calendar_id) == str(timetree_client.calendar_id)
            for calendar in calendars
        )
        if not timetree["target_calendar_found"]:
            _append_reason(reasons, "TIMETREE_TARGET_CALENDAR_NOT_FOUND")
    except Exception:  # noqa: BLE001 - external read boundary
        _append_reason(reasons, "TIMETREE_CALENDAR_READ_FAILED")

    label_catalog: TimeTreeLabelCatalog | None = None
    try:
        label_catalog = await timetree_client.get_calendar_labels()
        label_catalog.require_sync_labels()
        timetree["labels_resolved"] = True
    except Exception as exc:  # noqa: BLE001 - external read boundary
        _append_reason(reasons, _error_code(exc, "TIMETREE_LABELS_UNRESOLVED"))

    raw_events: Sequence[Mapping[str, Any]] = ()
    try:
        candidate_events = await timetree_client.get_events()
        if not isinstance(candidate_events, Sequence) or isinstance(
            candidate_events, (str, bytes)
        ):
            raise BootstrapError(
                "UNSAFE_TIMETREE_SNAPSHOT",
                "TimeTree full snapshot is not a sequence",
            )
        raw_events = candidate_events
        timetree["raw_event_count"] = len(raw_events)
        timetree["connected"] = timetree["target_calendar_found"] and bool(
            timetree["labels_resolved"]
        )
    except Exception as exc:  # noqa: BLE001 - external read boundary
        _append_reason(reasons, _error_code(exc, "UNSAFE_TIMETREE_SNAPSHOT"))

    if label_catalog is not None:
        timetree["label_counts"] = _label_counts(raw_events, label_catalog)

    unsupported_reasons: Counter[str] = Counter()
    sync_candidates = 0
    ignored_count = 0
    ignored_reasons: Counter[str] = Counter(
        {
            "TIMETREE_BIRTHDAY": 0,
            "TIMETREE_MEMO": 0,
            "LABEL_OUT_OF_SCOPE": 0,
        }
    )
    evidence_count = 0
    recurrence_diagnostics: list[dict[str, Any]] = []
    for raw in raw_events:
        if not isinstance(raw, Mapping):
            unsupported_reasons["UNSAFE_TIMETREE_SNAPSHOT"] += 1
            continue
        eligibility = classify_timetree_event(raw, label_catalog=label_catalog)
        if eligibility.classification is EventClassification.IGNORE_KNOWN:
            ignored_count += 1
            ignored_reasons[eligibility.code] += 1
            continue
        if eligibility.classification is EventClassification.UNSUPPORTED:
            unsupported_reasons[eligibility.code] += 1
            continue
        if has_timetree_exception_evidence(raw):
            evidence_count += 1
            unsupported_reasons["UNSUPPORTED_RECURRENCE_EXCEPTION"] += 1
            continue
        diagnostic = _recurrence_diagnostic(
            raw=raw,
            runner=runner,
            label_catalog=label_catalog,
        )
        if diagnostic is not None:
            recurrence_diagnostics.append(diagnostic)
            unsupported_reasons[diagnostic["reason_code"]] += 1
            continue
        sync_candidates += 1

    timetree["ignored_count"] = ignored_count
    timetree["ignored_reasons"] = dict(ignored_reasons)
    timetree["exception_evidence_count"] = evidence_count
    timetree["recurrence_diagnostics"] = _aggregate_recurrence_diagnostics(
        recurrence_diagnostics
    )

    eligible_count = sync_candidates
    if (
        label_catalog is not None
        and raw_events
        and not evidence_count
        and not unsupported_reasons
    ) or (label_catalog is not None and not raw_events):
        try:
            eligible = runner._timetree_preflight(
                raw_events,
                label_catalog=label_catalog,
            )
            eligible_count = len(eligible)
        except Exception as exc:  # noqa: BLE001 - normalization boundary
            code = _error_code(exc, "UNSAFE_TIMETREE_SNAPSHOT")
            unsupported_reasons[code] += 1
            eligible_count = max(sync_candidates - 1, 0)
            _append_reason(
                reasons,
                code,
            )
    timetree["eligible_count"] = eligible_count
    timetree["unsupported_count"] = sum(unsupported_reasons.values())
    timetree["unsupported_reasons"] = dict(unsupported_reasons)
    if evidence_count:
        _append_reason(reasons, "UNSUPPORTED_RECURRENCE_EXCEPTION")
    for code in unsupported_reasons:
        _append_reason(reasons, code)

    return timetree, reasons


async def run_read_only_bootstrap_gate(
    *,
    google_client: Any,
    timetree_client: Any,
    repository: StateRepository,
    default_timezone: str,
    bridge_version: str,
) -> dict[str, Any]:
    runner = BootstrapRunner(
        timetree_client=timetree_client,
        google_client=google_client,
        repository=repository,
        default_timezone=default_timezone,
        bridge_version=bridge_version,
        allow_recurrence_write=True,
    )
    database = database_preflight(repository)
    google, google_reasons = _google_read_preflight(
        runner=runner,
        google_client=google_client,
        repository=repository,
    )
    timetree, timetree_reasons = await _timetree_read_preflight(
        runner=runner,
        timetree_client=timetree_client,
    )
    recurrence_diagnostics = timetree.pop(
        "recurrence_diagnostics",
        {"unsupported_count": 0, "shapes": []},
    )

    reasons: list[str] = []
    if database["bootstrapped"]:
        _append_reason(reasons, "DB_ALREADY_BOOTSTRAPPED")
    if database["event_link_count"]:
        _append_reason(reasons, "DB_EVENT_LINKS_PRESENT")
    if database["pending_operation_count"]:
        _append_reason(reasons, "DB_PENDING_OPERATIONS_PRESENT")
    if database["open_conflict_count"]:
        _append_reason(reasons, "DB_OPEN_CONFLICTS_PRESENT")
    for reason in (*google_reasons, *timetree_reasons):
        _append_reason(reasons, reason)

    ready = not reasons
    return {
        "ok": ready,
        "command": "bootstrap",
        "dry_run": True,
        "remote_writes": 0,
        "google": google,
        "timetree": timetree,
        "recurrence_diagnostics": recurrence_diagnostics,
        "database": database,
        "gate": {
            "ready_for_live_bootstrap": ready,
            "reasons": reasons,
        },
    }


async def run_external_doctor(
    *,
    google_client: Any | None,
    timetree_client: Any | None,
) -> dict[str, Any]:
    reasons: list[str] = []
    timetree = {
        "connected": False,
        "target_calendar_found": False,
        "labels_resolved": False,
    }
    google = {
        "connected": False,
        "target_calendar_found": False,
        "full_sync_success": False,
        "event_read": False,
        "access_role": None,
        "writer_permission": False,
        "next_sync_token_observed": False,
    }

    if timetree_client is None:
        _append_reason(reasons, "TIMETREE_CREDENTIALS_MISSING")
    else:
        try:
            calendars = await timetree_client.list_calendars()
            timetree["target_calendar_found"] = any(
                str(calendar.calendar_id) == str(timetree_client.calendar_id)
                for calendar in calendars
            )
            if not timetree["target_calendar_found"]:
                _append_reason(reasons, "TIMETREE_TARGET_CALENDAR_NOT_FOUND")
            labels = await timetree_client.get_calendar_labels()
            labels.require_sync_labels()
            timetree["labels_resolved"] = True
            timetree["connected"] = True
        except Exception as exc:  # noqa: BLE001 - external read boundary
            _append_reason(reasons, _error_code(exc, "TIMETREE_DOCTOR_READ_FAILED"))

    if google_client is None:
        _append_reason(reasons, "GOOGLE_CREDENTIALS_MISSING")
    else:
        try:
            metadata = google_client.get_calendar_metadata()
            if not isinstance(metadata, Mapping):
                raise BootstrapError(
                    "GOOGLE_TARGET_PREFLIGHT_FAILED",
                    "Google target Calendar metadata is not an object",
                )
            configured_id = str(getattr(google_client, "calendar_id", ""))
            google["target_calendar_found"] = str(metadata.get("id")) == configured_id
            result = google_client.list_changes()
            token = getattr(result, "next_sync_token", None)
            changes = getattr(result, "changes", None)
            if not isinstance(changes, Sequence) or isinstance(changes, (str, bytes)):
                raise BootstrapError(
                    "UNSAFE_GOOGLE_SNAPSHOT",
                    "Google full snapshot changes are not a sequence",
                )
            if not isinstance(token, str) or not token:
                raise BootstrapError(
                    "UNSAFE_GOOGLE_SNAPSHOT",
                    "Google full snapshot is missing nextSyncToken",
                )
            google["full_sync_success"] = True
            google["event_read"] = True
            google["next_sync_token_observed"] = True
            google["connected"] = google["target_calendar_found"]
            access_role = getattr(result, "access_role", None)
            if access_role is None:
                access_role = metadata.get("accessRole")
            google["access_role"] = (
                access_role if isinstance(access_role, str) else None
            )
            google["writer_permission"] = access_role in GOOGLE_WRITE_ACCESS_ROLES
            if not google["target_calendar_found"]:
                _append_reason(reasons, "GOOGLE_TARGET_CALENDAR_MISMATCH")
            if not google["writer_permission"]:
                _append_reason(reasons, "GOOGLE_WRITER_PERMISSION_REQUIRED")
        except Exception as exc:  # noqa: BLE001 - external read boundary
            _append_reason(reasons, _error_code(exc, "GOOGLE_DOCTOR_READ_FAILED"))

    return {
        "external_services_checked": True,
        "timetree": timetree,
        "google": google,
        "ok": not reasons,
        "reasons": reasons,
    }
