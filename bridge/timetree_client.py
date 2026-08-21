from __future__ import annotations

import json
from collections.abc import AsyncIterator, Collection, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import (
    EventKind,
    NormalizedEvent,
    TimeTreeLabel,
    TimeTreeLabelCatalog,
)
from .recurrence import RecurrenceContractError, recurrence_lines_for_event

_ALLOWED_MCP_ENV = frozenset({"TIMETREE_EMAIL", "TIMETREE_PASSWORD"})
_P1_REQUIRED_EVENT_FIELDS = frozenset(
    {
        "uuid",
        "title",
        "start_at",
        "end_at",
        "all_day",
        "start_timezone",
        "end_timezone",
        "category",
        "type",
        "recurrences",
        "deactivated_at",
        "parent_id",
        "recurring_uuid",
    }
)
_SUPPORTED_UPDATE_FIELDS = frozenset(
    {
        "title",
        "all_day",
        "start",
        "end",
        "start_timezone",
        "end_timezone",
        "description",
        "location",
        "label",
        "recurrence",
    }
)


class TimeTreeClientError(RuntimeError):
    """Base error for the bridge TimeTree MCP boundary."""


class TimeTreeConfigurationError(TimeTreeClientError):
    """Raised when the MCP subprocess or calendar configuration is invalid."""


class TimeTreeTransportError(TimeTreeClientError):
    """Raised when the MCP transport itself fails."""


class TimeTreeToolError(TimeTreeClientError):
    """Raised when TimeTree-MCP returns an MCP tool error result."""

    def __init__(self, tool: str, *, error: str | None, message: str) -> None:
        self.tool = tool
        self.error = error
        self.message = message
        detail = f"{error}: {message}" if error else message
        super().__init__(f"TimeTree MCP tool {tool!r} failed: {detail}")


class TimeTreeProtocolError(TimeTreeClientError):
    """Raised when TimeTree-MCP returns an unsafe or malformed payload."""


class TimeTreeWriteGateError(TimeTreeClientError):
    """Raised when a write belongs to a later recurrence phase."""


@dataclass(frozen=True, slots=True)
class TimeTreeCalendar:
    calendar_id: str
    name: str
    alias_code: str | None
    users: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True, slots=True)
class TimeTreeWriteResult:
    event_uuid: str
    raw_event: Mapping[str, Any] | None = None


def _load_mcp_dependencies() -> tuple[Any, Any, Any]:
    try:
        from mcp import Client, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError as exc:  # pragma: no cover - exercised by local environment
        raise TimeTreeConfigurationError(
            "MCP Python SDK v2 is not installed; install mcp==2.0.0"
        ) from exc
    return Client, StdioServerParameters, stdio_client


def _zoneinfo(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise TimeTreeConfigurationError(f"invalid IANA timezone: {name!r}") from exc


def _calendar_id_as_int(calendar_id: str) -> int:
    if not calendar_id:
        raise TimeTreeConfigurationError("calendar_id must not be empty")
    try:
        parsed = int(calendar_id)
    except ValueError as exc:
        raise TimeTreeConfigurationError(
            "TimeTree write tools require a numeric calendar_id"
        ) from exc
    if str(parsed) != calendar_id.strip():
        raise TimeTreeConfigurationError(
            "TimeTree calendar_id must be a canonical numeric string"
        )
    return parsed


def _rfc3339_to_ms(value: str, *, field_name: str) -> int:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise TimeTreeProtocolError(f"invalid {field_name}: {value!r}") from exc
    if parsed.tzinfo is None:
        raise TimeTreeProtocolError(f"{field_name} must include a UTC offset")
    return int(parsed.timestamp() * 1000)


def _timestamp_to_ms(value: Any, *, field_name: str, required: bool) -> int | None:
    if value is None:
        if required:
            raise TimeTreeProtocolError(f"{field_name} is required")
        return None
    if isinstance(value, bool):
        raise TimeTreeProtocolError(f"{field_name} must be a timestamp")
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value:
        return _rfc3339_to_ms(value, field_name=field_name)
    raise TimeTreeProtocolError(f"{field_name} must be ISO 8601 or Unix ms")


def _datetime_to_ms(value: datetime) -> int:
    if value.tzinfo is None:
        raise TimeTreeConfigurationError("timed datetime must be timezone-aware")
    return int(value.astimezone(UTC).timestamp() * 1000)


def _utc_midnight_ms(value: date) -> int:
    utc_midnight = datetime.combine(value, time.min, tzinfo=UTC)
    return _datetime_to_ms(utc_midnight)


def _validate_mcp_env(env: Mapping[str, str]) -> dict[str, str]:
    unexpected = set(env) - _ALLOWED_MCP_ENV
    if unexpected:
        names = ", ".join(sorted(unexpected))
        raise TimeTreeConfigurationError(
            f"unexpected environment variables for TimeTree-MCP: {names}"
        )

    output: dict[str, str] = {}
    for key in sorted(_ALLOWED_MCP_ENV):
        value = env.get(key)
        if not isinstance(value, str) or not value:
            raise TimeTreeConfigurationError(f"missing required MCP credential: {key}")
        output[key] = value
    return output


def _text_blocks(result: Any) -> list[str]:
    texts: list[str] = []
    content = getattr(result, "content", None)
    if not isinstance(content, Sequence) or isinstance(content, (str, bytes)):
        return texts
    for block in content:
        if getattr(block, "type", None) == "text":
            text_value = getattr(block, "text", None)
            if isinstance(text_value, str):
                texts.append(text_value)
    return texts


def _tool_result_json(result: Any, *, tool: str) -> Mapping[str, Any]:
    structured = getattr(result, "structured_content", None)
    payload: Any = structured if isinstance(structured, Mapping) else None

    if payload is None:
        texts = _text_blocks(result)
        if len(texts) != 1:
            raise TimeTreeProtocolError(
                f"TimeTree MCP tool {tool!r} must return exactly one JSON text block"
            )
        try:
            payload = json.loads(texts[0])
        except json.JSONDecodeError as exc:
            raise TimeTreeProtocolError(
                f"TimeTree MCP tool {tool!r} returned invalid JSON"
            ) from exc

    if not isinstance(payload, Mapping):
        raise TimeTreeProtocolError(
            f"TimeTree MCP tool {tool!r} returned a non-object payload"
        )

    if bool(getattr(result, "is_error", False)):
        error = payload.get("error")
        message = payload.get("message")
        raise TimeTreeToolError(
            tool,
            error=error if isinstance(error, str) else None,
            message=message if isinstance(message, str) else "unknown TimeTree-MCP error",
        )

    return payload


def _coerce_read_event(raw: Mapping[str, Any], *, calendar_id: str) -> dict[str, Any]:
    missing = _P1_REQUIRED_EVENT_FIELDS - set(raw)
    if missing:
        names = ", ".join(sorted(missing))
        raise TimeTreeProtocolError(
            "TimeTree-MCP read contract is missing P1 fields: " + names
        )

    event = dict(raw)
    uuid = event.get("uuid")
    if not isinstance(uuid, str) or not uuid:
        raise TimeTreeProtocolError("TimeTree event uuid must be a non-empty string")

    returned_calendar_id = event.get("calendar_id")
    if returned_calendar_id is not None and str(returned_calendar_id) != calendar_id:
        raise TimeTreeProtocolError(
            f"TimeTree event {uuid!r} belongs to unexpected calendar {returned_calendar_id!r}"
        )
    event["calendar_id"] = int(calendar_id)

    if isinstance(event.get("type"), bool) or not isinstance(event.get("type"), int):
        raise TimeTreeProtocolError(
            "TimeTree event type must be an integer; P1 type=0 preservation may have regressed"
        )
    if isinstance(event.get("category"), bool) or not isinstance(event.get("category"), int):
        raise TimeTreeProtocolError("TimeTree event category must be an integer")
    if not isinstance(event.get("all_day"), bool):
        raise TimeTreeProtocolError("TimeTree event all_day must be boolean")

    recurrences = event.get("recurrences")
    if not isinstance(recurrences, Sequence) or isinstance(recurrences, (str, bytes)):
        raise TimeTreeProtocolError("TimeTree event recurrences must be a sequence")
    if not all(isinstance(line, str) for line in recurrences):
        raise TimeTreeProtocolError("TimeTree event recurrence lines must be strings")
    event["recurrences"] = list(recurrences)

    event["start_at"] = _timestamp_to_ms(
        event.get("start_at"), field_name="start_at", required=True
    )
    event["end_at"] = _timestamp_to_ms(
        event.get("end_at"), field_name="end_at", required=True
    )
    for field_name in ("created_at", "updated_at", "deactivated_at"):
        if field_name in event:
            event[field_name] = _timestamp_to_ms(
                event.get(field_name), field_name=field_name, required=False
            )
    return event


def _dedupe_by_uuid(events: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    by_uuid: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for raw in events:
        uuid = raw.get("uuid")
        if not isinstance(uuid, str) or not uuid:
            raise TimeTreeProtocolError("TimeTree event uuid must be a non-empty string")
        current = dict(raw)
        if uuid not in by_uuid:
            order.append(uuid)
            by_uuid[uuid] = current
            continue

        previous = by_uuid[uuid]
        previous_updated = previous.get("updated_at")
        current_updated = current.get("updated_at")
        if isinstance(previous_updated, int) and isinstance(current_updated, int):
            if current_updated >= previous_updated:
                by_uuid[uuid] = current
        elif current != previous:
            raise TimeTreeProtocolError(
                f"duplicate TimeTree uuid {uuid!r} has conflicting payloads without updated_at"
            )
    return tuple(by_uuid[uuid] for uuid in order)


def timetree_event_body(
    event: NormalizedEvent,
    *,
    calendar_id: str,
    default_timezone: str,
    allow_recurrence_write: bool = False,
    label_catalog: TimeTreeLabelCatalog | None = None,
    include_label: bool = True,
) -> dict[str, Any]:
    if event.kind is EventKind.EXCEPTION:
        raise TimeTreeWriteGateError(
            "recurrence exception writes are gated until P7 safety gate"
        )
    series_write = event.kind is EventKind.SERIES or bool(event.recurrence)
    if series_write and not allow_recurrence_write:
        raise TimeTreeWriteGateError(
            "recurrence series writes are gated until P6 Recurrence Series Core"
        )

    body: dict[str, Any] = {
        "calendar_id": _calendar_id_as_int(calendar_id),
        "title": event.title,
        "all_day": event.all_day,
        "category": 1,
    }

    if include_label:
        if label_catalog is None:
            raise TimeTreeConfigurationError(
                "TimeTree label catalog is required for create writes"
            )
        try:
            body["label_id"] = label_catalog.label_id_for_name(event.label)
        except ValueError as exc:
            raise TimeTreeConfigurationError(str(exc)) from exc

    if event.all_day:
        if isinstance(event.start, datetime) or isinstance(event.end, datetime):
            raise TimeTreeConfigurationError("all-day start/end must be date values")
        inclusive_end = event.end - timedelta(days=1)
        body.update(
            {
                "start_at": _utc_midnight_ms(event.start),
                "start_timezone": "UTC",
                "end_at": _utc_midnight_ms(inclusive_end),
                "end_timezone": "UTC",
            }
        )
    else:
        if not isinstance(event.start, datetime) or not isinstance(event.end, datetime):
            raise TimeTreeConfigurationError("timed start/end must be datetime values")
        body.update(
            {
                "start_at": _datetime_to_ms(event.start),
                "start_timezone": event.start_timezone,
                "end_at": _datetime_to_ms(event.end),
                "end_timezone": event.end_timezone,
            }
        )

    if event.description is not None:
        body["note"] = event.description
    if event.location is not None:
        body["location"] = event.location
    if series_write:
        try:
            recurrence_lines = recurrence_lines_for_event(event)
        except RecurrenceContractError as exc:
            raise TimeTreeWriteGateError(str(exc)) from exc
        body["recurrences"] = list(recurrence_lines)
    return body


def timetree_update_body(
    event: NormalizedEvent,
    *,
    fields: Collection[str],
    calendar_id: str,
    default_timezone: str,
    allow_recurrence_write: bool = False,
    label_catalog: TimeTreeLabelCatalog | None = None,
) -> dict[str, Any]:
    requested = set(fields)
    if not requested:
        raise ValueError("update fields must not be empty")
    unsupported = requested - _SUPPORTED_UPDATE_FIELDS
    if unsupported:
        names = ", ".join(sorted(unsupported))
        raise TimeTreeWriteGateError(f"unsupported TimeTree update fields: {names}")
    recurrence_requested = "recurrence" in requested
    if recurrence_requested and not allow_recurrence_write:
        raise TimeTreeWriteGateError(
            "recurrence writes are gated until P6 Recurrence Series Core"
        )
    if event.kind is EventKind.EXCEPTION:
        raise TimeTreeWriteGateError(
            "recurrence exception writes are gated until P7 safety gate"
        )
    if (event.kind is EventKind.SERIES or event.recurrence) and not allow_recurrence_write:
        raise TimeTreeWriteGateError(
            "recurrence series writes are gated until P6 Recurrence Series Core"
        )

    full = timetree_event_body(
        event,
        calendar_id=calendar_id,
        default_timezone=default_timezone,
        allow_recurrence_write=allow_recurrence_write,
        label_catalog=label_catalog,
        include_label="label" in requested,
    )
    body: dict[str, Any] = {"calendar_id": full["calendar_id"]}

    if "title" in requested:
        body["title"] = full["title"]
    if "description" in requested:
        body["note"] = event.description or ""
    if "location" in requested:
        body["location"] = event.location or ""
    if "label" in requested:
        body["label_id"] = full["label_id"]

    time_fields = {"all_day", "start", "end", "start_timezone", "end_timezone"}
    if "all_day" in requested:
        requested |= time_fields
    if requested & {"start", "start_timezone"}:
        body["start_at"] = full["start_at"]
        body["start_timezone"] = full["start_timezone"]
    if requested & {"end", "end_timezone"}:
        body["end_at"] = full["end_at"]
        body["end_timezone"] = full["end_timezone"]
    if "all_day" in requested:
        body["all_day"] = full["all_day"]
    if recurrence_requested:
        try:
            recurrence_lines = recurrence_lines_for_event(event)
        except RecurrenceContractError as exc:
            raise TimeTreeWriteGateError(str(exc)) from exc
        body["recurrences"] = list(recurrence_lines)

    return body


class TimeTreeMCPClient:
    def __init__(
        self,
        client: Any,
        *,
        calendar_id: str,
        default_timezone: str,
    ) -> None:
        self._client = client
        self.calendar_id = calendar_id
        self.calendar_id_int = _calendar_id_as_int(calendar_id)
        self.default_timezone = default_timezone
        _zoneinfo(default_timezone)

    @classmethod
    @asynccontextmanager
    async def connect(
        cls,
        *,
        mcp_entrypoint: str | Path,
        calendar_id: str,
        default_timezone: str,
        env: Mapping[str, str],
        node_command: str = "node",
    ) -> AsyncIterator[TimeTreeMCPClient]:
        entrypoint = Path(mcp_entrypoint).expanduser().resolve()
        if not entrypoint.is_file():
            raise TimeTreeConfigurationError(
                f"TimeTree-MCP runtime entrypoint not found: {entrypoint}"
            )
        if not node_command:
            raise TimeTreeConfigurationError("node_command must not be empty")

        safe_env = _validate_mcp_env(env)
        Client, StdioServerParameters, stdio_client = _load_mcp_dependencies()
        server = StdioServerParameters(
            command=node_command,
            args=[str(entrypoint)],
            env=safe_env,
            cwd=str(entrypoint.parent.parent),
        )

        async with AsyncExitStack() as stack:
            try:
                client = await stack.enter_async_context(Client(stdio_client(server)))
            except Exception as exc:
                raise TimeTreeTransportError(
                    "TimeTree-MCP stdio connection failed"
                ) from exc
            yield cls(
                client,
                calendar_id=calendar_id,
                default_timezone=default_timezone,
            )

    async def _call_json(
        self,
        tool: str,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        try:
            result = await self._client.call_tool(tool, dict(arguments))
        except Exception as exc:
            raise TimeTreeTransportError(
                f"TimeTree MCP request failed for tool {tool!r}"
            ) from exc
        return _tool_result_json(result, tool=tool)

    async def list_calendars(self) -> tuple[TimeTreeCalendar, ...]:
        payload = await self._call_json("list_calendars", {})
        raw_calendars = payload.get("calendars")
        if not isinstance(raw_calendars, Sequence) or isinstance(
            raw_calendars, (str, bytes)
        ):
            raise TimeTreeProtocolError("list_calendars.calendars must be a sequence")

        calendars: list[TimeTreeCalendar] = []
        for raw in raw_calendars:
            if not isinstance(raw, Mapping):
                raise TimeTreeProtocolError("list_calendars item must be an object")
            calendar_id = raw.get("id")
            name = raw.get("name")
            users = raw.get("users", [])
            if not isinstance(calendar_id, (str, int)) or not str(calendar_id):
                raise TimeTreeProtocolError("TimeTree calendar id is required")
            if not isinstance(name, str) or not name:
                raise TimeTreeProtocolError("TimeTree calendar name is required")
            if not isinstance(users, Sequence) or isinstance(users, (str, bytes)):
                raise TimeTreeProtocolError("TimeTree calendar users must be a sequence")
            user_mappings: list[Mapping[str, Any]] = []
            for user in users:
                if not isinstance(user, Mapping):
                    raise TimeTreeProtocolError("TimeTree calendar user must be an object")
                user_mappings.append(dict(user))
            alias_code = raw.get("alias_code")
            if alias_code is not None and not isinstance(alias_code, str):
                raise TimeTreeProtocolError("TimeTree calendar alias_code must be a string")
            calendars.append(
                TimeTreeCalendar(
                    calendar_id=str(calendar_id),
                    name=name,
                    alias_code=alias_code,
                    users=tuple(user_mappings),
                )
            )
        return tuple(calendars)

    async def get_calendar_labels(self) -> TimeTreeLabelCatalog:
        payload = await self._call_json(
            "get_calendar_labels",
            {"calendar_id": self.calendar_id_int},
        )
        result_calendar_id = payload.get("calendar_id")
        if result_calendar_id is not None and str(result_calendar_id) != self.calendar_id:
            raise TimeTreeProtocolError(
                "get_calendar_labels returned unexpected calendar_id "
                f"{result_calendar_id!r}"
            )

        raw_labels = payload.get("labels")
        if not isinstance(raw_labels, Sequence) or isinstance(
            raw_labels, (str, bytes)
        ):
            raise TimeTreeProtocolError(
                "get_calendar_labels.labels must be a sequence"
            )

        labels: list[TimeTreeLabel] = []
        for raw in raw_labels:
            if not isinstance(raw, Mapping):
                raise TimeTreeProtocolError(
                    "get_calendar_labels label must be an object"
                )
            label_id = raw.get("id")
            if isinstance(label_id, bool) or not isinstance(label_id, int):
                raise TimeTreeProtocolError(
                    "get_calendar_labels label id must be an integer"
                )
            label_name = raw.get("name")
            if label_name is not None and not isinstance(label_name, str):
                raise TimeTreeProtocolError(
                    "get_calendar_labels label name must be a string"
                )
            labels.append(
                TimeTreeLabel(
                    label_id=label_id,
                    label_name=label_name or None,
                )
            )

        try:
            catalog = TimeTreeLabelCatalog(tuple(labels))
            catalog.require_sync_labels()
        except ValueError as exc:
            raise TimeTreeProtocolError(str(exc)) from exc
        return catalog

    async def _read_events(
        self,
        tool: str,
        arguments: Mapping[str, Any],
    ) -> tuple[dict[str, Any], ...]:
        payload = await self._call_json(tool, arguments)
        result_calendar_id = payload.get("calendar_id")
        if result_calendar_id is not None and str(result_calendar_id) != self.calendar_id:
            raise TimeTreeProtocolError(
                f"{tool} returned unexpected calendar_id {result_calendar_id!r}"
            )
        raw_events = payload.get("events")
        if not isinstance(raw_events, Sequence) or isinstance(raw_events, (str, bytes)):
            raise TimeTreeProtocolError(f"{tool}.events must be a sequence")

        events: list[dict[str, Any]] = []
        for raw in raw_events:
            if not isinstance(raw, Mapping):
                raise TimeTreeProtocolError(f"{tool} event must be an object")
            events.append(_coerce_read_event(raw, calendar_id=self.calendar_id))
        return _dedupe_by_uuid(events)

    async def get_events(self) -> tuple[dict[str, Any], ...]:
        return await self._read_events(
            "get_events",
            {"calendar_id": self.calendar_id},
        )

    async def get_updated_events(
        self,
        updated_after_ms: int,
    ) -> tuple[dict[str, Any], ...]:
        if isinstance(updated_after_ms, bool) or not isinstance(updated_after_ms, int):
            raise TypeError("updated_after_ms must be an integer Unix ms timestamp")
        return await self._read_events(
            "get_updated_events",
            {
                "calendar_id": self.calendar_id,
                "updated_after": updated_after_ms,
            },
        )

    async def create_event(
        self,
        event: NormalizedEvent,
        *,
        allow_recurrence_write: bool = False,
    ) -> TimeTreeWriteResult:
        if event.kind is EventKind.EXCEPTION:
            raise TimeTreeWriteGateError(
                "recurrence exception writes are gated until P7 safety gate"
            )
        if (
            (event.kind is EventKind.SERIES or event.recurrence)
            and not allow_recurrence_write
        ):
            raise TimeTreeWriteGateError(
                "recurrence series writes are gated until P6 Recurrence Series Core"
            )
        label_catalog = await self.get_calendar_labels()
        body = timetree_event_body(
            event,
            calendar_id=self.calendar_id,
            default_timezone=self.default_timezone,
            allow_recurrence_write=allow_recurrence_write,
            label_catalog=label_catalog,
        )
        payload = await self._call_json("create_event", body)
        if payload.get("success") is not True:
            raise TimeTreeProtocolError("create_event did not confirm success")
        raw_event = payload.get("event")
        if not isinstance(raw_event, Mapping):
            raise TimeTreeProtocolError("create_event.event must be an object")
        event_uuid = raw_event.get("uuid")
        if not isinstance(event_uuid, str) or not event_uuid:
            raise TimeTreeProtocolError("create_event result is missing event uuid")
        return TimeTreeWriteResult(event_uuid=event_uuid, raw_event=dict(raw_event))

    async def update_event(
        self,
        event_uuid: str,
        event: NormalizedEvent,
        *,
        fields: Collection[str],
        allow_recurrence_write: bool = False,
    ) -> TimeTreeWriteResult:
        if not event_uuid:
            raise ValueError("event_uuid must not be empty")
        requested_fields = set(fields)
        if event.kind is EventKind.EXCEPTION:
            raise TimeTreeWriteGateError(
                "recurrence exception writes are gated until P7 safety gate"
            )
        if "recurrence" in requested_fields and not allow_recurrence_write:
            raise TimeTreeWriteGateError(
                "recurrence writes are gated until P6 Recurrence Series Core"
            )
        if (
            (event.kind is EventKind.SERIES or event.recurrence)
            and not allow_recurrence_write
        ):
            raise TimeTreeWriteGateError(
                "recurrence series writes are gated until P6 Recurrence Series Core"
            )
        label_catalog = (
            await self.get_calendar_labels() if "label" in requested_fields else None
        )
        body = timetree_update_body(
            event,
            fields=requested_fields,
            calendar_id=self.calendar_id,
            default_timezone=self.default_timezone,
            allow_recurrence_write=allow_recurrence_write,
            label_catalog=label_catalog,
        )
        body["event_uuid"] = event_uuid
        payload = await self._call_json("update_event", body)
        if payload.get("success") is not True:
            raise TimeTreeProtocolError("update_event did not confirm success")
        raw_event = payload.get("event")
        if not isinstance(raw_event, Mapping):
            raise TimeTreeProtocolError("update_event.event must be an object")
        returned_uuid = raw_event.get("uuid")
        if returned_uuid != event_uuid:
            raise TimeTreeProtocolError(
                "update_event returned a UUID different from the requested UUID"
            )
        return TimeTreeWriteResult(event_uuid=event_uuid, raw_event=dict(raw_event))

    async def delete_event(
        self,
        event_uuid: str,
        *,
        event_kind: EventKind,
        allow_recurrence_write: bool = False,
    ) -> TimeTreeWriteResult:
        if not event_uuid:
            raise ValueError("event_uuid must not be empty")
        if event_kind is EventKind.EXCEPTION:
            raise TimeTreeWriteGateError(
                "recurrence exception writes are gated until P7 safety gate"
            )
        if event_kind is EventKind.SERIES and not allow_recurrence_write:
            raise TimeTreeWriteGateError(
                "recurrence series writes are gated until P6 Recurrence Series Core"
            )
        payload = await self._call_json(
            "delete_event",
            {
                "calendar_id": self.calendar_id_int,
                "event_uuid": event_uuid,
            },
        )
        if payload.get("success") is not True:
            raise TimeTreeProtocolError("delete_event did not confirm success")
        deleted_uuid = payload.get("deleted_event_uuid")
        if deleted_uuid != event_uuid:
            raise TimeTreeProtocolError(
                "delete_event returned a UUID different from the requested UUID"
            )
        return TimeTreeWriteResult(event_uuid=event_uuid)
