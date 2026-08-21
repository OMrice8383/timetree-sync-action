from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .adapters import NormalizationError, UnsupportedEventError, normalize_google_event
from .models import (
    GOOGLE_TIMETREE_LABEL_PROPERTY,
    ChangeType,
    EventChange,
    EventKind,
    NormalizedEvent,
)
from .recurrence import RecurrenceContractError, recurrence_lines_for_event

GOOGLE_CALENDAR_SCOPES = ("https://www.googleapis.com/auth/calendar",)

GOOGLE_SYNC_QUERY: dict[str, Any] = {
    "singleEvents": False,
    "showDeleted": True,
    "eventTypes": "default",
    "maxResults": 2500,
}

FORBIDDEN_INCREMENTAL_PARAMETERS = frozenset(
    {
        "iCalUID",
        "orderBy",
        "privateExtendedProperty",
        "q",
        "sharedExtendedProperty",
        "timeMin",
        "timeMax",
        "updatedMin",
    }
)

_ALLOWED_SYNC_PARAMETERS = frozenset(GOOGLE_SYNC_QUERY) | {"syncToken", "pageToken"}


class GoogleClientError(RuntimeError):
    """Base error for the bridge Google Calendar boundary."""


class GoogleQueryContractError(GoogleClientError):
    """Raised when a list request would violate the fixed sync contract."""


class GoogleProtocolError(GoogleClientError):
    """Raised when Google returns a shape that cannot be synchronized safely."""


class FullResyncRequired(GoogleClientError):
    """Signal that the current incremental sync token is no longer usable."""

    code = "FULL_RESYNC_REQUIRED"


@dataclass(frozen=True, slots=True)
class GoogleSyncResult:
    changes: tuple[EventChange, ...]
    next_sync_token: str

    def __post_init__(self) -> None:
        if not self.next_sync_token:
            raise ValueError("next_sync_token must not be empty")


def _http_status(exc: BaseException) -> int | None:
    response = getattr(exc, "resp", None)
    status = getattr(response, "status", None)
    if status is None:
        status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def validate_google_sync_query(params: Mapping[str, Any]) -> None:
    unexpected = set(params) - _ALLOWED_SYNC_PARAMETERS
    if unexpected:
        names = ", ".join(sorted(unexpected))
        raise GoogleQueryContractError(f"unexpected Google sync parameters: {names}")

    if params.get("singleEvents") is not False:
        raise GoogleQueryContractError("singleEvents must remain false")
    if params.get("showDeleted") is not True:
        raise GoogleQueryContractError("showDeleted must remain true")
    if params.get("eventTypes") != "default":
        raise GoogleQueryContractError("eventTypes must remain default")
    if params.get("maxResults") != 2500:
        raise GoogleQueryContractError("maxResults must remain 2500")

    sync_token = params.get("syncToken")
    if sync_token is not None:
        if not isinstance(sync_token, str) or not sync_token:
            raise GoogleQueryContractError("syncToken must be a non-empty string")
        forbidden = FORBIDDEN_INCREMENTAL_PARAMETERS.intersection(params)
        if forbidden:
            names = ", ".join(sorted(forbidden))
            raise GoogleQueryContractError(
                f"parameters forbidden with syncToken: {names}"
            )

    page_token = params.get("pageToken")
    if page_token is not None and (not isinstance(page_token, str) or not page_token):
        raise GoogleQueryContractError("pageToken must be a non-empty string")


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


def _zoneinfo(name: str, *, code: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise UnsupportedEventError(code, f"invalid IANA timezone: {name!r}") from exc


def _normalize_original_start(
    raw: Mapping[str, Any],
    *,
    default_timezone: str,
) -> date | datetime:
    if "date" in raw:
        value = raw.get("date")
        if not isinstance(value, str) or not value:
            raise NormalizationError("originalStartTime.date must be YYYY-MM-DD")
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise NormalizationError(
                f"invalid originalStartTime.date: {value!r}"
            ) from exc

    parsed = _parse_rfc3339(
        raw.get("dateTime"),
        field_name="originalStartTime.dateTime",
    )
    explicit_timezone = raw.get("timeZone")
    if explicit_timezone is not None:
        if not isinstance(explicit_timezone, str) or not explicit_timezone:
            raise UnsupportedEventError(
                "UNSUPPORTED_GOOGLE_TIMEZONE",
                "originalStartTime.timeZone is invalid",
            )
        effective_timezone = explicit_timezone
        _zoneinfo(effective_timezone, code="UNSUPPORTED_GOOGLE_TIMEZONE")
    else:
        effective_timezone = default_timezone
        zone = _zoneinfo(default_timezone, code="INVALID_DEFAULT_TIMEZONE")
        if parsed.astimezone(zone).utcoffset() != parsed.utcoffset():
            raise UnsupportedEventError(
                "UNSUPPORTED_GOOGLE_TIMEZONE",
                "originalStartTime offset does not match default timezone",
            )

    return parsed.astimezone(ZoneInfo(effective_timezone))


def google_event_change(
    raw: Mapping[str, Any],
    *,
    source_calendar_id: str,
    default_timezone: str,
) -> EventChange:
    if raw.get("status") != "cancelled":
        event = normalize_google_event(
            raw,
            source_calendar_id=source_calendar_id,
            default_timezone=default_timezone,
        )
        return EventChange(
            change_type=ChangeType.UPSERT,
            source_event_id=event.source_event_id,
            event=event,
        )

    source_event_id = raw.get("id")
    if not isinstance(source_event_id, str) or not source_event_id:
        raise NormalizationError("cancelled Google event requires id")

    recurring_event_id = raw.get("recurringEventId")
    original_start_raw = raw.get("originalStartTime")

    if recurring_event_id is None and original_start_raw is None:
        return EventChange(
            change_type=ChangeType.DELETE,
            source_event_id=source_event_id,
        )

    if (
        not isinstance(recurring_event_id, str)
        or not recurring_event_id
        or not isinstance(original_start_raw, Mapping)
    ):
        raise UnsupportedEventError(
            "UNSUPPORTED_GOOGLE_CANCELLED_EXCEPTION",
            "cancelled recurrence exception requires recurringEventId and "
            "originalStartTime",
        )

    original_start = _normalize_original_start(
        original_start_raw,
        default_timezone=default_timezone,
    )
    return EventChange(
        change_type=ChangeType.RECURRENCE_EXCEPTION_DELETE,
        source_event_id=source_event_id,
        parent_source_event_id=recurring_event_id,
        original_start=original_start,
    )


def google_event_body(
    event: NormalizedEvent,
    *,
    private_properties: Mapping[str, str] | None = None,
    allow_recurrence_write: bool = False,
    clear_recurrence: bool = False,
) -> dict[str, Any]:
    if event.kind is EventKind.EXCEPTION:
        raise GoogleClientError(
            "recurrence exception writes are gated until P7 safety gate"
        )

    series_write = event.kind is EventKind.SERIES or bool(event.recurrence)
    if (series_write or clear_recurrence) and not allow_recurrence_write:
        raise GoogleClientError(
            "recurrence writes are gated until P6 Recurrence Series Core"
        )
    if clear_recurrence and (
        event.kind is not EventKind.SINGLE or event.recurrence
    ):
        raise GoogleClientError(
            "recurrence removal requires a single event with empty recurrence"
        )

    body: dict[str, Any] = {
        "summary": event.title,
        "description": event.description or "",
        "location": event.location or "",
    }

    if event.all_day:
        body["start"] = {"date": event.start.isoformat()}
        body["end"] = {"date": event.end.isoformat()}
    else:
        body["start"] = {
            "dateTime": event.start.isoformat(),
            "timeZone": event.start_timezone,
        }
        body["end"] = {
            "dateTime": event.end.isoformat(),
            "timeZone": event.end_timezone,
        }

    if series_write:
        try:
            recurrence_lines = recurrence_lines_for_event(event)
        except RecurrenceContractError as exc:
            raise GoogleClientError(str(exc)) from exc
        body["recurrence"] = list(recurrence_lines)
    elif clear_recurrence:
        body["recurrence"] = []

    properties = dict(private_properties or {})
    properties[GOOGLE_TIMETREE_LABEL_PROPERTY] = event.label
    body["extendedProperties"] = {"private": properties}

    return body


class GoogleCalendarClient:
    def __init__(
        self,
        service: Any,
        *,
        calendar_id: str,
        default_timezone: str,
    ) -> None:
        if not calendar_id:
            raise ValueError("calendar_id must not be empty")
        if not default_timezone:
            raise ValueError("default_timezone must not be empty")
        _zoneinfo(default_timezone, code="INVALID_DEFAULT_TIMEZONE")
        self._service = service
        self.calendar_id = calendar_id
        self.default_timezone = default_timezone

    @classmethod
    def from_service_account_file(
        cls,
        service_account_file: str | Path,
        *,
        calendar_id: str,
        default_timezone: str,
    ) -> GoogleCalendarClient:
        path = Path(service_account_file).expanduser().resolve()
        if not path.is_file():
            raise GoogleClientError(f"Google service account file not found: {path}")

        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise GoogleClientError(
                "google-api-python-client dependencies are not installed"
            ) from exc

        credentials = service_account.Credentials.from_service_account_file(
            str(path),
            scopes=list(GOOGLE_CALENDAR_SCOPES),
        )
        service = build(
            "calendar",
            "v3",
            credentials=credentials,
            cache_discovery=False,
        )
        return cls(
            service,
            calendar_id=calendar_id,
            default_timezone=default_timezone,
        )

    def _list_params(
        self,
        *,
        sync_token: str | None,
        page_token: str | None,
    ) -> dict[str, Any]:
        params = dict(GOOGLE_SYNC_QUERY)
        params["calendarId"] = self.calendar_id
        if sync_token is not None:
            params["syncToken"] = sync_token
        if page_token is not None:
            params["pageToken"] = page_token

        contract_params = {key: value for key, value in params.items() if key != "calendarId"}
        validate_google_sync_query(contract_params)
        return params

    def list_changes(self, *, sync_token: str | None = None) -> GoogleSyncResult:
        changes: list[EventChange] = []
        page_token: str | None = None

        while True:
            params = self._list_params(sync_token=sync_token, page_token=page_token)
            try:
                response = self._service.events().list(**params).execute()
            except Exception as exc:
                if sync_token is not None and _http_status(exc) == 410:
                    raise FullResyncRequired(
                        "Google sync token is invalid; full resync is required"
                    ) from exc
                raise

            if not isinstance(response, Mapping):
                raise GoogleProtocolError("Google events.list returned a non-object")

            raw_items = response.get("items", [])
            if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)):
                raise GoogleProtocolError("Google events.list items must be a sequence")

            for raw in raw_items:
                if not isinstance(raw, Mapping):
                    raise GoogleProtocolError("Google events.list item must be an object")
                changes.append(
                    google_event_change(
                        raw,
                        source_calendar_id=self.calendar_id,
                        default_timezone=self.default_timezone,
                    )
                )

            next_page_token = response.get("nextPageToken")
            if next_page_token is not None:
                if not isinstance(next_page_token, str) or not next_page_token:
                    raise GoogleProtocolError("nextPageToken must be a non-empty string")
                page_token = next_page_token
                continue

            next_sync_token = response.get("nextSyncToken")
            if not isinstance(next_sync_token, str) or not next_sync_token:
                raise GoogleProtocolError(
                    "final Google events.list page is missing nextSyncToken"
                )
            return GoogleSyncResult(tuple(changes), next_sync_token)

    def get_calendar_metadata(self) -> Mapping[str, Any]:
        response = (
            self._service.calendars().get(calendarId=self.calendar_id).execute()
        )
        if not isinstance(response, Mapping):
            raise GoogleProtocolError("Google calendars.get returned a non-object")
        return response

    def get_event(self, event_id: str) -> Mapping[str, Any]:
        if not event_id:
            raise ValueError("event_id must not be empty")
        response = (
            self._service.events()
            .get(calendarId=self.calendar_id, eventId=event_id)
            .execute()
        )
        if not isinstance(response, Mapping):
            raise GoogleProtocolError("Google events.get returned a non-object")
        return response

    def insert_event(self, body: Mapping[str, Any]) -> Mapping[str, Any]:
        response = (
            self._service.events()
            .insert(calendarId=self.calendar_id, body=dict(body))
            .execute()
        )
        if not isinstance(response, Mapping):
            raise GoogleProtocolError("Google events.insert returned a non-object")
        return response

    def patch_event(
        self,
        event_id: str,
        body: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if not event_id:
            raise ValueError("event_id must not be empty")
        response = (
            self._service.events()
            .patch(
                calendarId=self.calendar_id,
                eventId=event_id,
                body=dict(body),
            )
            .execute()
        )
        if not isinstance(response, Mapping):
            raise GoogleProtocolError("Google events.patch returned a non-object")
        return response

    def delete_event(self, event_id: str) -> Any:
        if not event_id:
            raise ValueError("event_id must not be empty")
        return (
            self._service.events()
            .delete(calendarId=self.calendar_id, eventId=event_id)
            .execute()
        )
