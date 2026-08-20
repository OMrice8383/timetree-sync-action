from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import SUPPORTED_RECURRENCE_PROPERTIES, NormalizedEvent


class CanonicalizationError(ValueError):
    pass


_RRULE_LIST_KEYS = frozenset(
    {
        "BYSECOND",
        "BYMINUTE",
        "BYHOUR",
        "BYDAY",
        "BYMONTHDAY",
        "BYYEARDAY",
        "BYWEEKNO",
        "BYMONTH",
        "BYSETPOS",
    }
)

_RRULE_UPPER_VALUE_KEYS = frozenset({"FREQ", "BYDAY", "WKST"})
_RRULE_INTEGER_KEYS = frozenset({"INTERVAL", "COUNT"})
_RRULE_INTEGER_LIST_KEYS = frozenset(
    {
        "BYSECOND",
        "BYMINUTE",
        "BYHOUR",
        "BYMONTHDAY",
        "BYYEARDAY",
        "BYWEEKNO",
        "BYMONTH",
        "BYSETPOS",
    }
)


def _normalize_newlines(value: str | None) -> str:
    if value is None:
        return ""
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _datetime_epoch_ms(value: datetime) -> int:
    if value.tzinfo is None:
        raise CanonicalizationError("timed datetime must be timezone-aware")
    utc_value = value.astimezone(UTC)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    delta = utc_value - epoch
    return (
        (delta.days * 86_400 + delta.seconds) * 1_000
        + delta.microseconds // 1_000
    )


def _canonical_temporal(value: date | datetime | None) -> str | int:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return _datetime_epoch_ms(value)
    return value.isoformat()


def _canonicalize_rrule(line: str) -> str:
    _, body = line.split(":", 1)
    entries: dict[str, str] = {}

    for raw_part in body.split(";"):
        part = raw_part.strip()
        if not part:
            continue
        if "=" not in part:
            raise CanonicalizationError(f"invalid RRULE component: {part!r}")
        raw_key, raw_value = part.split("=", 1)
        key = raw_key.strip().upper()
        value = raw_value.strip()
        if not key or not value:
            raise CanonicalizationError(f"invalid RRULE component: {part!r}")
        if key in entries:
            raise CanonicalizationError(f"duplicate RRULE key: {key}")

        if key in _RRULE_UPPER_VALUE_KEYS:
            value = value.upper()
        if key in _RRULE_INTEGER_KEYS:
            try:
                value = str(int(value))
            except ValueError as exc:
                raise CanonicalizationError(
                    f"RRULE {key} must be an integer"
                ) from exc
        if key == "UNTIL" and value.endswith("z"):
            value = value[:-1] + "Z"
        if key in _RRULE_LIST_KEYS:
            values = [item.strip() for item in value.split(",") if item.strip()]
            if key in _RRULE_UPPER_VALUE_KEYS:
                values = [item.upper() for item in values]
            if key in _RRULE_INTEGER_LIST_KEYS:
                try:
                    numeric_values = sorted({int(item) for item in values})
                except ValueError as exc:
                    raise CanonicalizationError(
                        f"RRULE {key} must contain integers"
                    ) from exc
                value = ",".join(str(item) for item in numeric_values)
            else:
                value = ",".join(sorted(set(values)))

        if key == "INTERVAL" and value == "1":
            continue
        if key == "WKST" and value == "MO":
            continue
        entries[key] = value

    if "FREQ" not in entries:
        raise CanonicalizationError("RRULE requires FREQ")
    if "COUNT" in entries and "UNTIL" in entries:
        raise CanonicalizationError("RRULE must not include both COUNT and UNTIL")

    return "RRULE:" + ";".join(f"{key}={entries[key]}" for key in sorted(entries))


def _canonicalize_exdate(line: str) -> str:
    left, raw_values = line.split(":", 1)
    left_parts = [part.strip() for part in left.split(";") if part.strip()]
    if not left_parts or left_parts[0].upper() != "EXDATE":
        raise CanonicalizationError(f"invalid EXDATE line: {line!r}")

    params: list[tuple[str, str]] = []
    seen_param_keys: set[str] = set()
    for raw_param in left_parts[1:]:
        if "=" not in raw_param:
            raise CanonicalizationError(f"invalid EXDATE parameter: {raw_param!r}")
        raw_key, raw_value = raw_param.split("=", 1)
        key = raw_key.strip().upper()
        value = raw_value.strip()
        if not key or not value:
            raise CanonicalizationError(f"invalid EXDATE parameter: {raw_param!r}")
        if key in seen_param_keys:
            raise CanonicalizationError(f"duplicate EXDATE parameter: {key}")
        seen_param_keys.add(key)
        if key == "VALUE":
            value = value.upper()
        params.append((key, value))

    values = [value.strip() for value in raw_values.split(",") if value.strip()]
    if not values:
        raise CanonicalizationError("EXDATE requires at least one value")

    param_map = dict(params)
    if param_map.get("VALUE") == "DATE":
        left_canonical = "EXDATE" + "".join(
            f";{key}={value}" for key, value in sorted(params)
        )
        return left_canonical + ":" + ",".join(sorted(set(values)))

    tzid = param_map.get("TZID")
    if tzid is not None:
        try:
            zone = ZoneInfo(tzid)
        except ZoneInfoNotFoundError as exc:
            raise CanonicalizationError(
                f"invalid EXDATE TZID: {tzid!r}"
            ) from exc

        utc_values: list[str] = []
        for value in values:
            try:
                local_value = datetime.strptime(
                    value,
                    "%Y%m%dT%H%M%S",
                ).replace(tzinfo=zone)
            except ValueError as exc:
                raise CanonicalizationError(
                    f"invalid TZID EXDATE value: {value!r}"
                ) from exc
            utc_values.append(
                local_value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
            )
        return "EXDATE:" + ",".join(sorted(set(utc_values)))

    if all(value.endswith("Z") for value in values):
        utc_values = []
        for value in values:
            try:
                parsed = datetime.strptime(
                    value,
                    "%Y%m%dT%H%M%S%z",
                )
            except ValueError as exc:
                raise CanonicalizationError(
                    f"invalid UTC EXDATE value: {value!r}"
                ) from exc
            utc_values.append(
                parsed.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
            )
        return "EXDATE:" + ",".join(sorted(set(utc_values)))

    left_canonical = "EXDATE" + "".join(
        f";{key}={value}" for key, value in sorted(params)
    )
    return left_canonical + ":" + ",".join(sorted(set(values)))


def canonicalize_recurrence_line(line: str) -> str:
    if not isinstance(line, str) or ":" not in line:
        raise CanonicalizationError(f"invalid recurrence line: {line!r}")
    property_name = line.split(":", 1)[0].split(";", 1)[0].strip().upper()
    if property_name not in SUPPORTED_RECURRENCE_PROPERTIES:
        raise CanonicalizationError(
            f"unsupported recurrence property during canonicalization: {property_name}"
        )
    if property_name == "RRULE":
        return _canonicalize_rrule(line)
    return _canonicalize_exdate(line)


def canonicalize_recurrence(lines: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted(canonicalize_recurrence_line(line) for line in lines))


def canonical_event_dict(event: NormalizedEvent) -> dict[str, Any]:
    if event.all_day:
        start_timezone = ""
        end_timezone = ""
    else:
        start_timezone = event.start_timezone or ""
        end_timezone = event.end_timezone or ""

    return {
        "all_day": event.all_day,
        "description": _normalize_newlines(event.description),
        "end": _canonical_temporal(event.end),
        "end_timezone": end_timezone,
        "kind": event.kind.value,
        "location": _normalize_newlines(event.location),
        "original_start": _canonical_temporal(event.original_start),
        "parent_source_event_id": event.parent_source_event_id or "",
        "recurrence": list(canonicalize_recurrence(event.recurrence.lines)),
        "start": _canonical_temporal(event.start),
        "start_timezone": start_timezone,
        "title": _normalize_newlines(event.title),
    }


def canonical_event_json(event: NormalizedEvent) -> str:
    return json.dumps(
        canonical_event_dict(event),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def event_hash(event: NormalizedEvent) -> str:
    payload = canonical_event_json(event).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
