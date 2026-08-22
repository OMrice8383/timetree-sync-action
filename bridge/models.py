from __future__ import annotations

from collections.abc import Mapping
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
DEFAULT_TIMETREE_LABEL_NAME = "大河予定"
SYNC_TIMETREE_LABEL_NAMES = frozenset({"大河予定", "共通予定"})
GOOGLE_TIMETREE_LABEL_PROPERTY = "timetree_label_name"
GOOGLE_BRIDGE_SYNC_SOURCE = "timetree-chatgpt-bridge"


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
class TimeTreeLabel:
    label_id: int
    label_name: str | None


@dataclass(frozen=True, slots=True)
class TimeTreeLabelCatalog:
    labels: tuple[TimeTreeLabel, ...]

    def __post_init__(self) -> None:
        ids = [label.label_id for label in self.labels]
        if len(ids) != len(set(ids)):
            raise ValueError("TimeTree label IDs must be unique")

        names = [
            label.label_name for label in self.labels if label.label_name is not None
        ]
        if len(names) != len(set(names)):
            raise ValueError("TimeTree label names must be unique")

    @classmethod
    def from_mapping(cls, labels: Mapping[int, str | None]) -> TimeTreeLabelCatalog:
        return cls(
            tuple(
                TimeTreeLabel(label_id=label_id, label_name=label_name)
                for label_id, label_name in labels.items()
            )
        )

    def label_name_for_id(self, label_id: object) -> str:
        if isinstance(label_id, bool) or not isinstance(label_id, int):
            raise TypeError("TimeTree label_id must be an integer")
        for label in self.labels:
            if label.label_id == label_id:
                if not label.label_name:
                    raise ValueError("TimeTree label name is missing")
                return label.label_name
        raise ValueError(f"unknown TimeTree label_id: {label_id!r}")

    def label_id_for_name(self, label_name: str) -> int:
        for label in self.labels:
            if label.label_name == label_name:
                return label.label_id
        raise ValueError(f"unknown TimeTree label name: {label_name!r}")

    def sync_label_name_for_id(self, label_id: object) -> str | None:
        """Resolve only the runtime IDs of the two in-scope labels."""
        if isinstance(label_id, bool) or not isinstance(label_id, int):
            raise TypeError("TimeTree label_id must be an integer")

        current = next(
            (label for label in self.labels if label.label_id == label_id),
            None,
        )
        if current is None:
            return None
        if current.label_name in SYNC_TIMETREE_LABEL_NAMES:
            return current.label_name

        named_sync_labels = {
            label.label_name
            for label in self.labels
            if label.label_name in SYNC_TIMETREE_LABEL_NAMES
        }
        if named_sync_labels != SYNC_TIMETREE_LABEL_NAMES:
            raise ValueError("TimeTree label name is missing")
        return None

    def require_sync_labels(self) -> None:
        for required_name in SYNC_TIMETREE_LABEL_NAMES:
            matches = [
                label for label in self.labels if label.label_name == required_name
            ]
            if len(matches) != 1:
                raise ValueError(
                    f"TimeTree sync label must resolve exactly once: {required_name!r}"
                )


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
    label: str = DEFAULT_TIMETREE_LABEL_NAME

    recurrence: Recurrence = field(default_factory=Recurrence)
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.source_calendar_id:
            raise ValueError("source_calendar_id must not be empty")
        if not self.source_event_id:
            raise ValueError("source_event_id must not be empty")
        if not self.title.strip():
            raise ValueError("title must not be empty")
        if self.label not in SYNC_TIMETREE_LABEL_NAMES:
            raise ValueError(f"unsupported normalized TimeTree label: {self.label!r}")

        if self.all_day:
            if isinstance(self.start, datetime) or isinstance(self.end, datetime):
                raise ValueError("all-day start/end must be date values")
            if self.start_timezone is not None or self.end_timezone is not None:
                raise ValueError("all-day timezones must be None")
        else:
            if not isinstance(self.start, datetime) or not isinstance(
                self.end, datetime
            ):
                raise ValueError("timed start/end must be datetime values")
            if self.start.tzinfo is None or self.end.tzinfo is None:
                raise ValueError("timed start/end must be timezone-aware")
            if not self.start_timezone or not self.end_timezone:
                raise ValueError("timed effective timezones must be set")

        if self.end <= self.start:
            raise ValueError("event end must be after start")

        if self.kind is EventKind.EXCEPTION and (
            not self.parent_source_event_id or self.original_start is None
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
    # The raw Google object is retained only when a Google client parsed the
    # change.  Bootstrap needs the private metadata to distinguish a managed
    # event from an unmanaged event during its preflight.  It is optional so
    # the existing partial-delete/EventChange API remains unchanged.
    raw: Mapping[str, object] | None = None

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

        if self.change_type is ChangeType.RECURRENCE_EXCEPTION_DELETE and (
            not self.parent_source_event_id or self.original_start is None
        ):
            raise ValueError(
                "RECURRENCE_EXCEPTION_DELETE requires parent_source_event_id "
                "and original_start"
            )
