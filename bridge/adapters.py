from __future__ import annotations

import warnings
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import (
    DEFAULT_TIMETREE_LABEL_NAME,
    GOOGLE_BRIDGE_SYNC_SOURCE,
    GOOGLE_TIMETREE_LABEL_PROPERTY,
    SUPPORTED_RECURRENCE_PROPERTIES,
    SYNC_TIMETREE_LABEL_NAMES,
    Eligibility,
    EventClassification,
    EventKind,
    NormalizedEvent,
    Recurrence,
    Source,
    TimeTreeLabelCatalog,
)
from .recurrence import (
    RecurrenceContractError,
    recurrence_property_name,
    validate_recurrence_lines,
)


class NormalizationError(ValueError):
    """Raised when a raw payload cannot form a valid normalized event."""


class EventEligibilityError(ValueError):
    def __init__(self, eligibility: Eligibility) -> None:
        self.eligibility = eligibility
        super().__init__(f"{eligibility.classification.value}: {eligibility.code}")


class UnsupportedEventError(NormalizationError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class TimezoneFallbackWarning(UserWarning):
    pass


def _timetree_label_eligibility(
    raw: Mapping[str, Any],
    *,
    label_catalog: TimeTreeLabelCatalog | None,
) -> tuple[Eligibility, str | None]:
    if label_catalog is None:
        return (
            Eligibility(
                EventClassification.UNSUPPORTED,
                "TIMETREE_LABEL_CATALOG_REQUIRED",
            ),
            None,
        )

    label_id = raw.get("label_id")
    if label_id is None:
        return (
            Eligibility(EventClassification.UNSUPPORTED, "TIMETREE_LABEL_MISSING"),
            None,
        )

    try:
        sync_label_name = label_catalog.sync_label_name_for_id(label_id)
    except ValueError:
        return (
            Eligibility(
                EventClassification.UNSUPPORTED,
                "TIMETREE_LABEL_NAME_MISSING",
            ),
            None,
        )
    except TypeError:
        return (
            Eligibility(
                EventClassification.UNSUPPORTED,
                "TIMETREE_LABEL_UNKNOWN_ID",
            ),
            None,
        )

    if sync_label_name is not None:
        return (
            Eligibility(EventClassification.SYNC, "TIMETREE_LABEL_IN_SCOPE"),
            sync_label_name,
        )

    known_id = any(label.label_id == label_id for label in label_catalog.labels)
    if known_id:
        return (
            Eligibility(EventClassification.IGNORE_KNOWN, "LABEL_OUT_OF_SCOPE"),
            None,
        )
    return (
        Eligibility(EventClassification.UNSUPPORTED, "TIMETREE_LABEL_UNKNOWN_ID"),
        None,
    )


def has_timetree_exception_evidence(raw: Mapping[str, Any]) -> bool:
    """Return whether a raw TimeTree event carries P7 exception evidence."""
    if raw.get("parent_id") is not None or raw.get("recurring_uuid") is not None:
        return True
    recurrences = raw.get("recurrences")
    if not isinstance(recurrences, Sequence) or isinstance(recurrences, (str, bytes)):
        return False
    for line in recurrences:
        if not isinstance(line, str):
            continue
        try:
            if recurrence_property_name(line) == "EXDATE":
                return True
        except RecurrenceContractError:
            continue
    return False


def classify_timetree_event(
    raw: Mapping[str, Any],
    *,
    label_catalog: TimeTreeLabelCatalog | None = None,
) -> Eligibility:
    category = raw.get("category")
    event_type = raw.get("type")

    if event_type == 1:
        return Eligibility(EventClassification.IGNORE_KNOWN, "TIMETREE_BIRTHDAY")
    if category == 2:
        return Eligibility(EventClassification.IGNORE_KNOWN, "TIMETREE_MEMO")
    if category != 1 or event_type != 0:
        return Eligibility(
            EventClassification.UNSUPPORTED,
            f"TIMETREE_CATEGORY_{category}_TYPE_{event_type}",
        )
    label_eligibility, _ = _timetree_label_eligibility(
        raw,
        label_catalog=label_catalog,
    )
    if label_eligibility.classification is EventClassification.SYNC:
        return Eligibility(EventClassification.SYNC, "TIMETREE_CALENDAR_EVENT")
    return label_eligibility


def classify_google_event(raw: Mapping[str, Any]) -> Eligibility:
    if raw.get("status") == "cancelled":
        return Eligibility(
            EventClassification.UNSUPPORTED,
            "GOOGLE_CANCELLED_CHANGE_REQUIRES_EVENT_CHANGE",
        )
    if raw.get("eventType", "default") != "default":
        return Eligibility(EventClassification.UNSUPPORTED, "GOOGLE_SPECIAL_EVENT_TYPE")
    summary = raw.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return Eligibility(
            EventClassification.UNSUPPORTED,
            "UNSUPPORTED_GOOGLE_EMPTY_TITLE",
        )
    return Eligibility(EventClassification.SYNC, "GOOGLE_DEFAULT_EVENT")


def _require_sync(eligibility: Eligibility) -> None:
    if eligibility.classification is not EventClassification.SYNC:
        raise EventEligibilityError(eligibility)


def _zoneinfo(name: str, *, code: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise UnsupportedEventError(code, f"invalid IANA timezone: {name!r}") from exc


def _effective_timetree_timezone(
    raw_timezone: Any,
    *,
    default_timezone: str,
    field_name: str,
) -> str:
    if isinstance(raw_timezone, str) and raw_timezone:
        try:
            ZoneInfo(raw_timezone)
        except (ZoneInfoNotFoundError, ValueError):
            pass
        else:
            return raw_timezone

    _zoneinfo(default_timezone, code="INVALID_DEFAULT_TIMEZONE")
    warnings.warn(
        f"{field_name} missing/invalid; using default timezone {default_timezone}",
        TimezoneFallbackWarning,
        stacklevel=3,
    )
    return default_timezone


def _from_unix_ms(value: Any, *, field_name: str) -> datetime:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise NormalizationError(f"{field_name} must be unix milliseconds")
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def _parse_rfc3339(value: Any, *, field_name: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise NormalizationError(f"{field_name} must be RFC3339 dateTime")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise NormalizationError(f"invalid {field_name}: {value!r}") from exc
    if parsed.tzinfo is None:
        raise NormalizationError(f"{field_name} must include an offset")
    return parsed


def _parse_date(value: Any, *, field_name: str) -> date:
    if not isinstance(value, str) or not value:
        raise NormalizationError(f"{field_name} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise NormalizationError(f"invalid {field_name}: {value!r}") from exc


def _google_effective_timezone(
    endpoint: Mapping[str, Any],
    parsed: datetime,
    *,
    default_timezone: str,
    field_name: str,
) -> str:
    explicit = endpoint.get("timeZone")
    if explicit is not None:
        if not isinstance(explicit, str) or not explicit:
            raise UnsupportedEventError(
                "UNSUPPORTED_GOOGLE_TIMEZONE",
                f"{field_name}.timeZone is invalid",
            )
        _zoneinfo(explicit, code="UNSUPPORTED_GOOGLE_TIMEZONE")
        return explicit

    default_zone = _zoneinfo(default_timezone, code="INVALID_DEFAULT_TIMEZONE")
    default_view = parsed.astimezone(default_zone)
    if default_view.utcoffset() != parsed.utcoffset():
        raise UnsupportedEventError(
            "UNSUPPORTED_GOOGLE_TIMEZONE",
            f"{field_name} offset does not match default timezone {default_timezone}",
        )
    return default_timezone


def _normalize_recurrence(raw_lines: Any) -> Recurrence:
    if raw_lines is None:
        return Recurrence()
    if not isinstance(raw_lines, Sequence) or isinstance(raw_lines, (str, bytes)):
        raise NormalizationError("recurrence must be a sequence of strings")

    lines: list[str] = []
    for raw_line in raw_lines:
        if not isinstance(raw_line, str) or ":" not in raw_line:
            raise NormalizationError(f"invalid recurrence line: {raw_line!r}")
        property_name = raw_line.split(":", 1)[0].split(";", 1)[0].strip().upper()
        if property_name not in SUPPORTED_RECURRENCE_PROPERTIES:
            raise UnsupportedEventError(
                "UNSUPPORTED_RECURRENCE_FEATURE",
                f"recurrence feature {property_name!r} is not enabled for V1",
            )
        lines.append(raw_line)
    return Recurrence(tuple(lines))


def _normalize_google_label(
    raw: Mapping[str, Any],
    *,
    google_new_default: str = DEFAULT_TIMETREE_LABEL_NAME,
    allow_missing_managed_label: bool = False,
) -> str:
    if google_new_default not in SYNC_TIMETREE_LABEL_NAMES:
        raise UnsupportedEventError(
            "UNSUPPORTED_GOOGLE_LABEL",
            f"invalid configured Google default label: {google_new_default!r}",
        )
    extended = raw.get("extendedProperties")
    if extended is None:
        return google_new_default
    if not isinstance(extended, Mapping):
        raise UnsupportedEventError(
            "UNSUPPORTED_GOOGLE_LABEL_METADATA",
            "Google extendedProperties must be an object",
        )

    private = extended.get("private")
    if private is None:
        return google_new_default
    if not isinstance(private, Mapping):
        raise UnsupportedEventError(
            "UNSUPPORTED_GOOGLE_LABEL_METADATA",
            "Google private properties must be an object",
        )

    label_name = private.get(GOOGLE_TIMETREE_LABEL_PROPERTY)
    managed = private.get("sync_source") == GOOGLE_BRIDGE_SYNC_SOURCE
    if label_name is None:
        if managed:
            if allow_missing_managed_label:
                return google_new_default
            raise UnsupportedEventError(
                "UNSUPPORTED_GOOGLE_LABEL_METADATA",
                "managed Google event is missing TimeTree label metadata",
            )
        return google_new_default
    if not isinstance(label_name, str) or label_name not in SYNC_TIMETREE_LABEL_NAMES:
        raise UnsupportedEventError(
            "UNSUPPORTED_GOOGLE_LABEL_METADATA",
            f"unknown Google TimeTree label metadata: {label_name!r}",
        )
    return label_name


def normalize_timetree_event(
    raw: Mapping[str, Any],
    *,
    default_timezone: str,
    label_catalog: TimeTreeLabelCatalog | None = None,
) -> NormalizedEvent:
    eligibility = classify_timetree_event(
        raw,
        label_catalog=label_catalog,
    )
    _require_sync(eligibility)
    _, label_name = _timetree_label_eligibility(
        raw,
        label_catalog=label_catalog,
    )

    source_event_id = raw.get("uuid")
    if not isinstance(source_event_id, str) or not source_event_id:
        raise NormalizationError("TimeTree uuid is required")

    calendar_id = raw.get("calendar_id")
    if calendar_id is None:
        raise NormalizationError("TimeTree calendar_id is required")

    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        raise NormalizationError("TimeTree title is required")

    if raw.get("parent_id") is not None or raw.get("recurring_uuid") is not None:
        raise UnsupportedEventError(
            "UNSUPPORTED_RECURRENCE_EXCEPTION",
            "TimeTree recurrence exception mapping is deferred to the P7 contract gate",
        )

    recurrence = _normalize_recurrence(raw.get("recurrences"))
    all_day = raw.get("all_day")
    if not isinstance(all_day, bool):
        raise NormalizationError("TimeTree all_day must be boolean")

    start_utc = _from_unix_ms(raw.get("start_at"), field_name="start_at")
    end_utc = _from_unix_ms(raw.get("end_at"), field_name="end_at")

    if all_day:
        start_tz = _effective_timetree_timezone(
            raw.get("start_timezone"),
            default_timezone=default_timezone,
            field_name="start_timezone",
        )
        end_tz = _effective_timetree_timezone(
            raw.get("end_timezone"),
            default_timezone=default_timezone,
            field_name="end_timezone",
        )
        start = start_utc.astimezone(ZoneInfo(start_tz)).date()
        inclusive_end = end_utc.astimezone(ZoneInfo(end_tz)).date()
        end = inclusive_end + timedelta(days=1)
        normalized_start_tz = None
        normalized_end_tz = None
    else:
        start_tz = _effective_timetree_timezone(
            raw.get("start_timezone"),
            default_timezone=default_timezone,
            field_name="start_timezone",
        )
        end_tz = _effective_timetree_timezone(
            raw.get("end_timezone"),
            default_timezone=default_timezone,
            field_name="end_timezone",
        )
        start = start_utc.astimezone(ZoneInfo(start_tz))
        end = end_utc.astimezone(ZoneInfo(end_tz))
        normalized_start_tz = start_tz
        normalized_end_tz = end_tz

    if recurrence and not all_day and normalized_start_tz != normalized_end_tz:
        raise UnsupportedEventError(
            "UNSUPPORTED_RECURRENCE_TIMEZONE",
            "recurring series requires the same effective start/end timezone",
        )
    if recurrence:
        try:
            recurrence = Recurrence(
                validate_recurrence_lines(
                    recurrence.lines,
                    all_day=all_day,
                    timezone=normalized_start_tz,
                )
            )
        except RecurrenceContractError as exc:
            raise UnsupportedEventError(
                "UNSUPPORTED_RECURRENCE_FEATURE",
                str(exc),
            ) from exc

    return NormalizedEvent(
        source=Source.TIMETREE,
        source_calendar_id=str(calendar_id),
        source_event_id=source_event_id,
        kind=EventKind.SERIES if recurrence else EventKind.SINGLE,
        parent_source_event_id=None,
        original_start=None,
        title=title,
        all_day=all_day,
        start=start,
        end=end,
        start_timezone=normalized_start_tz,
        end_timezone=normalized_end_tz,
        description=raw.get("note") if isinstance(raw.get("note"), str) else None,
        location=raw.get("location") if isinstance(raw.get("location"), str) else None,
        label=label_name or "",
        recurrence=recurrence,
        updated_at=(
            _from_unix_ms(raw["updated_at"], field_name="updated_at")
            if raw.get("updated_at") is not None
            else None
        ),
    )


def _normalize_google_original_start(
    value: Mapping[str, Any],
    *,
    default_timezone: str,
) -> date | datetime:
    if "date" in value:
        return _parse_date(value.get("date"), field_name="originalStartTime.date")
    parsed = _parse_rfc3339(
        value.get("dateTime"),
        field_name="originalStartTime.dateTime",
    )
    effective = _google_effective_timezone(
        value,
        parsed,
        default_timezone=default_timezone,
        field_name="originalStartTime",
    )
    return parsed.astimezone(ZoneInfo(effective))


def normalize_google_event(
    raw: Mapping[str, Any],
    *,
    source_calendar_id: str,
    default_timezone: str,
    google_new_default: str = DEFAULT_TIMETREE_LABEL_NAME,
    allow_missing_managed_label: bool = False,
) -> NormalizedEvent:
    _require_sync(classify_google_event(raw))

    source_event_id = raw.get("id")
    if not isinstance(source_event_id, str) or not source_event_id:
        raise NormalizationError("Google id is required")
    if not source_calendar_id:
        raise NormalizationError("Google source_calendar_id is required")

    title = raw["summary"]
    start_raw = raw.get("start")
    end_raw = raw.get("end")
    if not isinstance(start_raw, Mapping) or not isinstance(end_raw, Mapping):
        raise NormalizationError("Google start/end objects are required")

    recurrence = _normalize_recurrence(raw.get("recurrence"))
    label_name = _normalize_google_label(
        raw,
        google_new_default=google_new_default,
        allow_missing_managed_label=allow_missing_managed_label,
    )

    start_is_date = "date" in start_raw
    end_is_date = "date" in end_raw
    if start_is_date != end_is_date:
        raise NormalizationError("Google start/end must both be all-day or both timed")

    if start_is_date:
        start: date | datetime = _parse_date(
            start_raw.get("date"),
            field_name="start.date",
        )
        end: date | datetime = _parse_date(end_raw.get("date"), field_name="end.date")
        all_day = True
        start_timezone = None
        end_timezone = None
    else:
        start_parsed = _parse_rfc3339(
            start_raw.get("dateTime"),
            field_name="start.dateTime",
        )
        end_parsed = _parse_rfc3339(end_raw.get("dateTime"), field_name="end.dateTime")
        start_timezone = _google_effective_timezone(
            start_raw,
            start_parsed,
            default_timezone=default_timezone,
            field_name="start",
        )
        end_timezone = _google_effective_timezone(
            end_raw,
            end_parsed,
            default_timezone=default_timezone,
            field_name="end",
        )
        start = start_parsed.astimezone(ZoneInfo(start_timezone))
        end = end_parsed.astimezone(ZoneInfo(end_timezone))
        all_day = False

    if recurrence and not all_day and start_timezone != end_timezone:
        raise UnsupportedEventError(
            "UNSUPPORTED_RECURRENCE_TIMEZONE",
            "recurring series requires the same effective start/end timezone",
        )
    if recurrence:
        try:
            recurrence = Recurrence(
                validate_recurrence_lines(
                    recurrence.lines,
                    all_day=all_day,
                    timezone=start_timezone,
                )
            )
        except RecurrenceContractError as exc:
            raise UnsupportedEventError(
                "UNSUPPORTED_RECURRENCE_FEATURE",
                str(exc),
            ) from exc

    recurring_event_id = raw.get("recurringEventId")
    original_start_raw = raw.get("originalStartTime")
    if recurring_event_id is not None:
        if not isinstance(recurring_event_id, str) or not recurring_event_id:
            raise NormalizationError(
                "Google recurringEventId must be a non-empty string"
            )
        if not isinstance(original_start_raw, Mapping):
            raise UnsupportedEventError(
                "UNSUPPORTED_GOOGLE_RECURRENCE_EXCEPTION",
                "Google recurrence exception is missing originalStartTime",
            )
        kind = EventKind.EXCEPTION
        parent_source_event_id = recurring_event_id
        original_start = _normalize_google_original_start(
            original_start_raw,
            default_timezone=default_timezone,
        )
    else:
        kind = EventKind.SERIES if recurrence else EventKind.SINGLE
        parent_source_event_id = None
        original_start = None

    return NormalizedEvent(
        source=Source.GOOGLE,
        source_calendar_id=source_calendar_id,
        source_event_id=source_event_id,
        kind=kind,
        parent_source_event_id=parent_source_event_id,
        original_start=original_start,
        title=title,
        all_day=all_day,
        start=start,
        end=end,
        start_timezone=start_timezone,
        end_timezone=end_timezone,
        description=(
            raw.get("description") if isinstance(raw.get("description"), str) else None
        ),
        location=raw.get("location") if isinstance(raw.get("location"), str) else None,
        label=label_name,
        recurrence=recurrence,
        updated_at=(
            _parse_rfc3339(raw["updated"], field_name="updated")
            if raw.get("updated") is not None
            else None
        ),
    )
