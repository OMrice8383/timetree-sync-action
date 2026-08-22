from __future__ import annotations

from collections.abc import Sequence
from time import strptime

from .canonical import CanonicalizationError, canonicalize_recurrence
from .models import SUPPORTED_RECURRENCE_PROPERTIES, EventKind, NormalizedEvent


class RecurrenceContractError(ValueError):
    """Raised when recurrence syntax is outside the confirmed P6 series contract."""


_P6_RRULE_KEYS = frozenset({"FREQ", "INTERVAL", "COUNT", "UNTIL", "BYDAY"})
_WEEKDAYS = frozenset({"MO", "TU", "WE", "TH", "FR", "SA", "SU"})


def recurrence_property_name(line: str) -> str:
    if not isinstance(line, str) or ":" not in line:
        raise RecurrenceContractError(f"invalid recurrence line: {line!r}")
    left = line.split(":", 1)[0]
    property_name = left.split(";", 1)[0].strip().upper()
    if not property_name:
        raise RecurrenceContractError(f"invalid recurrence property: {line!r}")
    return property_name


def _rrule_entries(line: str) -> dict[str, str]:
    _, body = line.split(":", 1)
    entries: dict[str, str] = {}
    for raw_part in body.split(";"):
        part = raw_part.strip()
        if not part:
            continue
        if "=" not in part:
            raise RecurrenceContractError(f"invalid RRULE component: {part!r}")
        raw_key, raw_value = part.split("=", 1)
        key = raw_key.strip().upper()
        value = raw_value.strip()
        if not key or not value:
            raise RecurrenceContractError(f"invalid RRULE component: {part!r}")
        if key in entries:
            raise RecurrenceContractError(f"duplicate RRULE key: {key}")
        entries[key] = value.upper() if key == "FREQ" else value
    return entries


def _validate_compact_date(value: str, *, field_name: str) -> None:
    try:
        strptime(value, "%Y%m%d")
    except ValueError as exc:
        raise RecurrenceContractError(
            f"{field_name} must use YYYYMMDD for an all-day series"
        ) from exc


def _validate_utc_datetime(value: str, *, field_name: str) -> None:
    try:
        strptime(value, "%Y%m%dT%H%M%SZ")
    except ValueError as exc:
        raise RecurrenceContractError(
            f"{field_name} must use UTC YYYYMMDDTHHMMSSZ for a timed series"
        ) from exc


def _validate_local_datetime(value: str, *, field_name: str) -> None:
    try:
        strptime(value, "%Y%m%dT%H%M%S")
    except ValueError as exc:
        raise RecurrenceContractError(
            f"{field_name} with TZID must use YYYYMMDDTHHMMSS"
        ) from exc


def _validate_rrule(line: str, *, all_day: bool) -> None:
    entries = _rrule_entries(line)
    unexpected = set(entries) - _P6_RRULE_KEYS
    if unexpected:
        names = ", ".join(sorted(unexpected))
        raise RecurrenceContractError(f"unsupported P6 RRULE keys: {names}")

    frequency = entries.get("FREQ")
    if frequency == "YEARLY":
        if not all_day:
            raise RecurrenceContractError(
                "P6 YEARLY RRULE is supported only for all-day series"
            )
        if set(entries) != {"FREQ"}:
            raise RecurrenceContractError(
                "P6 YEARLY RRULE supports no additional parameters"
            )
        return

    if frequency != "WEEKLY":
        raise RecurrenceContractError("P6 writable RRULE requires FREQ=WEEKLY")

    for key in ("INTERVAL", "COUNT"):
        if key not in entries:
            continue
        value = int(entries[key])
        if value <= 0:
            raise RecurrenceContractError(f"RRULE {key} must be greater than zero")

    byday = entries.get("BYDAY")
    if byday is not None:
        days = tuple(item for item in byday.split(",") if item)
        if not days or any(day not in _WEEKDAYS for day in days):
            raise RecurrenceContractError(
                "P6 weekly BYDAY supports only MO,TU,WE,TH,FR,SA,SU"
            )

    until = entries.get("UNTIL")
    if until is not None:
        if all_day:
            _validate_compact_date(until, field_name="RRULE UNTIL")
        else:
            _validate_utc_datetime(until, field_name="RRULE UNTIL")


def _exdate_params(line: str) -> tuple[dict[str, str], tuple[str, ...]]:
    left, raw_values = line.split(":", 1)
    parts = [part for part in left.split(";") if part]
    params: dict[str, str] = {}
    for raw_param in parts[1:]:
        key, value = raw_param.split("=", 1)
        params[key.upper()] = value
    values = tuple(value for value in raw_values.split(",") if value)
    return params, values


def _validate_exdate(line: str, *, all_day: bool, timezone: str | None) -> None:
    params, values = _exdate_params(line)
    if not values:
        raise RecurrenceContractError("EXDATE requires at least one value")

    if all_day:
        if params != {"VALUE": "DATE"}:
            raise RecurrenceContractError(
                "all-day P6 EXDATE requires exactly VALUE=DATE"
            )
        for value in values:
            _validate_compact_date(value, field_name="EXDATE")
        return

    unexpected = set(params) - {"TZID"}
    if unexpected:
        names = ", ".join(sorted(unexpected))
        raise RecurrenceContractError(f"unsupported timed EXDATE parameters: {names}")

    tzid = params.get("TZID")
    if tzid is not None:
        if timezone is None or tzid != timezone:
            raise RecurrenceContractError(
                "timed EXDATE TZID must match the effective series timezone"
            )
        for value in values:
            _validate_local_datetime(value, field_name="EXDATE")
        return

    for value in values:
        _validate_utc_datetime(value, field_name="EXDATE")


def validate_recurrence_lines(
    lines: Sequence[str],
    *,
    all_day: bool,
    timezone: str | None,
) -> tuple[str, ...]:
    if not lines:
        return ()
    if all_day and timezone is not None:
        raise RecurrenceContractError("all-day recurrence must not carry a timezone")
    if not all_day and not timezone:
        raise RecurrenceContractError("timed recurrence requires an effective timezone")

    # Validate raw EXDATE context before canonicalization. Timed EXDATE
    # canonicalization intentionally converts TZID forms to UTC, which would
    # otherwise erase evidence of a mismatched effective series timezone.
    yearly_rrule = False
    for line in lines:
        property_name = recurrence_property_name(line)
        if property_name == "RRULE":
            raw_entries = _rrule_entries(line)
            if raw_entries.get("FREQ") == "YEARLY":
                yearly_rrule = True
                if set(raw_entries) != {"FREQ"}:
                    raise RecurrenceContractError(
                        "P6 YEARLY RRULE supports no additional parameters"
                    )
        elif property_name == "EXDATE":
            _validate_exdate(
                line,
                all_day=all_day,
                timezone=timezone,
            )

    if yearly_rrule:
        if not all_day:
            raise RecurrenceContractError(
                "P6 YEARLY RRULE is supported only for all-day series"
            )
        if len(lines) != 1:
            raise RecurrenceContractError(
                "P6 YEARLY recurrence requires exactly RRULE:FREQ=YEARLY"
            )

    try:
        canonical = canonicalize_recurrence(tuple(lines))
    except CanonicalizationError as exc:
        raise RecurrenceContractError(str(exc)) from exc

    properties = tuple(recurrence_property_name(line) for line in canonical)
    unsupported = set(properties) - SUPPORTED_RECURRENCE_PROPERTIES
    if unsupported:
        names = ", ".join(sorted(unsupported))
        raise RecurrenceContractError(f"unsupported recurrence properties: {names}")
    if properties.count("RRULE") != 1:
        raise RecurrenceContractError("P6 recurrence series requires exactly one RRULE")

    for line, property_name in zip(canonical, properties, strict=True):
        if property_name == "RRULE":
            _validate_rrule(line, all_day=all_day)
        elif property_name == "EXDATE":
            _validate_exdate(line, all_day=all_day, timezone=timezone)

    return canonical


def recurrence_lines_for_event(event: NormalizedEvent) -> tuple[str, ...]:
    if event.kind is EventKind.EXCEPTION:
        raise RecurrenceContractError(
            "recurrence exception writes remain gated until P7"
        )
    if not event.recurrence:
        if event.kind is EventKind.SERIES:
            raise RecurrenceContractError("series event requires recurrence rules")
        return ()
    if event.kind is not EventKind.SERIES:
        raise RecurrenceContractError(
            "event carrying recurrence rules must have kind=series"
        )
    if not event.all_day and event.start_timezone != event.end_timezone:
        raise RecurrenceContractError(
            "recurring series requires matching effective start/end timezones"
        )
    return validate_recurrence_lines(
        event.recurrence.lines,
        all_day=event.all_day,
        timezone=event.start_timezone,
    )
