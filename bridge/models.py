from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum


class Source(str, Enum):
    TIMETREE = "timetree"
    GOOGLE = "google"


class EventKind(str, Enum):
    SINGLE = "single"
    SERIES = "series"
    EXCEPTION = "exception"


class EventClassification(str, Enum):
    SYNC = "SYNC"
    IGNORE_KNOWN = "IGNORE_KNOWN"
    UNSUPPORTED = "UNSUPPORTED"


class ChangeType(str, Enum):
    UPSERT = "UPSERT"
    DELETE = "DELETE"
    RECURRENCE_EXCEPTION_DELETE = "RECURRENCE_EXCEPTION_DELETE"


SUPPORTED_RECURRENCE_PROPERTIES = frozenset({"RRULE", "EXDATE"})


@dataclass(frozen=True, slots=True)
class Eligibility:
    classification: EventClassification
    code: str


@dataclass(frozen=True, slots=True)
class Recurrence:
    lines: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.lines)


@dataclass(frozen=True, slots=True)
class NormalizedEvent:
    source: Source
    source_calendar_id: str
    source_event_id: str

    kind: EventKind
    parent_source_event_id: str | None
    original_start: date | datetime | None

    title: str
    all_day: bool
    start: date | datetime
    end: date | datetime
    start_timezone: str | None
    end_timezone: str | None
    description: str | None
    location: str | None

    recurrence: Recurrence = field(default_factory=Recurrence)
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.source_calendar_id:
            raise ValueError("source_calendar_id must not be empty")
        if not self.source_event_id:
            raise ValueError("source_event_id must not be empty")
        if not self.title.strip():
            raise ValueError("title must not be empty")

        if self.all_day:
            if isinstance(self.start, datetime) or isinstance(self.end, datetime):
                raise ValueError("all-day start/end must be date values")
            if self.start_timezone is not None or self.end_timezone is not None:
                raise ValueError("all-day timezones must be None")
        else:
            if (
                not isinstance(self.start, datetime)
                or not isinstance(self.end, datetime)
            ):
                raise ValueError("timed start/end must be datetime values")
            if self.start.tzinfo is None or self.end.tzinfo is None:
                raise ValueError("timed start/end must be timezone-aware")
            if not self.start_timezone or not self.end_timezone:
                raise ValueError("timed effective timezones must be set")

        if self.end <= self.start:
            raise ValueError("event end must be after start")

        if (
            self.kind is EventKind.EXCEPTION
            and (not self.parent_source_event_id or self.original_start is None)
        ):
            raise ValueError(
                "exception requires parent_source_event_id and original_start"
            )


@dataclass(frozen=True, slots=True)
class EventChange:
    change_type: ChangeType
    source_event_id: str
    parent_source_event_id: str | None = None
    original_start: date | datetime | None = None
    event: NormalizedEvent | None = None

    def __post_init__(self) -> None:
        if not self.source_event_id:
            raise ValueError("source_event_id must not be empty")

        if self.change_type is ChangeType.UPSERT:
            if self.event is None:
                raise ValueError("UPSERT requires event")
            if self.event.source_event_id != self.source_event_id:
                raise ValueError("UPSERT source_event_id must match event")
            return

        if self.event is not None:
            raise ValueError("delete changes must not carry a full event")

        if (
            self.change_type is ChangeType.RECURRENCE_EXCEPTION_DELETE
            and (not self.parent_source_event_id or self.original_start is None)
        ):
            raise ValueError(
                "RECURRENCE_EXCEPTION_DELETE requires parent_source_event_id "
                "and original_start"
            )
