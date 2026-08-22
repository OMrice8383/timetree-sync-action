from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from typing import Any

from .adapters import (
    EventEligibilityError,
    NormalizationError,
    normalize_google_event,
    normalize_timetree_event,
)
from .canonical import canonical_event_json, event_hash
from .google_client import FullResyncRequired
from .models import (
    DEFAULT_TIMETREE_LABEL_NAME,
    GOOGLE_BRIDGE_SYNC_SOURCE,
    GOOGLE_TIMETREE_LABEL_PROPERTY,
    SYNC_TIMETREE_LABEL_NAMES,
    ChangeType,
    EventChange,
    EventKind,
    NormalizedEvent,
    TimeTreeLabelCatalog,
)
from .repository import NEEDS_MANUAL_RECOVERY, StateRepository
from .timetree_client import timetree_event_body, timetree_update_body

GOOGLE_TO_TIMETREE_DIRECTION = "google_to_timetree"
CREATE_ACTION = "create"
UPDATE_ACTION = "update"
DELETE_ACTION = "delete"
RECURRENCE_EXCEPTION_DELETE_ACTION = "recurrence_exception_delete"
_RECOVERY_STATES = frozenset({"prepared", "remote_applied", "mapping_saved"})


class GoogleToTimeTreeError(RuntimeError):
    """A safe-stop result from the Google to TimeTree P9 path."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        confirmed_remote_writes: int = 0,
        remote_write_outcome_unknown: bool = False,
    ) -> None:
        self.code = code
        self.confirmed_remote_writes = confirmed_remote_writes
        self.remote_write_outcome_unknown = remote_write_outcome_unknown
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class GoogleToTimeTreeResult:
    status: str
    source_sync_token: str
    next_sync_token: str
    token_committed: bool
    processed_change_count: int
    created_event_count: int
    updated_event_count: int
    skipped_event_count: int
    deferred_delete_count: int
    conflict_count: int
    confirmed_remote_writes: int
    remote_write_outcome_unknown: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _await_call(method: Any, *args: Any, **kwargs: Any) -> Any:
    result = method(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


def _payload_hash(body: Mapping[str, Any]) -> str:
    payload = json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _timestamp_ms(value: str | None) -> int:
    if not value:
        return 0
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GoogleToTimeTreeError(
            "INVALID_LAST_SYNCED_AT",
            "stored last_synced_at is not a valid timestamp",
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _event_change_event(change: EventChange) -> NormalizedEvent:
    if change.change_type is not ChangeType.UPSERT or change.event is None:
        raise GoogleToTimeTreeError(
            "GOOGLE_CHANGE_EVENT_REQUIRED",
            "an UPSERT change must carry a normalized Google event",
        )
    return change.event


class GoogleToTimeTreeRunner:
    """P9-A incremental Google to TimeTree synchronizer.

    Google reads and TimeTree writes deliberately have different client
    boundaries: the Google client is synchronous, while the TimeTree MCP
    client is asynchronous.  This runner owns the ordering and persistence
    rules between them, and contains no live-service discovery of its own.
    """

    def __init__(
        self,
        *,
        google_client: Any,
        timetree_client: Any,
        repository: StateRepository,
        default_timezone: str,
        google_new_default_label: str = DEFAULT_TIMETREE_LABEL_NAME,
        bridge_version: str = "p9-test",
        overlap_seconds: int = 30,
        allow_recurrence_write: bool = True,
    ) -> None:
        if google_new_default_label not in SYNC_TIMETREE_LABEL_NAMES:
            raise ValueError(
                "google_new_default_label must be an in-scope TimeTree label"
            )
        if overlap_seconds < 0:
            raise ValueError("overlap_seconds must be non-negative")
        self.google_client = google_client
        self.timetree_client = timetree_client
        self.repository = repository
        self.default_timezone = default_timezone
        self.google_new_default_label = google_new_default_label
        self.bridge_version = bridge_version
        self.overlap_seconds = overlap_seconds
        self.allow_recurrence_write = allow_recurrence_write
        self._label_catalog: TimeTreeLabelCatalog | None = None
        self.confirmed_remote_writes = 0
        self.remote_write_outcome_unknown = False

    def _error(self, code: str, message: str) -> GoogleToTimeTreeError:
        return GoogleToTimeTreeError(
            code,
            message,
            confirmed_remote_writes=self.confirmed_remote_writes,
            remote_write_outcome_unknown=self.remote_write_outcome_unknown,
        )

    def _remote_write_succeeded(self) -> None:
        self.confirmed_remote_writes += 1

    def _remote_write_ambiguous(self) -> None:
        self.remote_write_outcome_unknown = True

    async def _get_label_catalog(self) -> TimeTreeLabelCatalog:
        if self._label_catalog is not None:
            return self._label_catalog
        getter = getattr(self.timetree_client, "get_calendar_labels", None)
        if not callable(getter):
            raise GoogleToTimeTreeError(
                "TIMETREE_LABEL_CATALOG_REQUIRED",
                "TimeTree label catalog is required before a write",
            )
        try:
            catalog = await _await_call(getter)
        except Exception as exc:
            raise GoogleToTimeTreeError(
                "TIMETREE_LABEL_CATALOG_FAILED",
                "TimeTree label catalog could not be resolved",
            ) from exc
        if not isinstance(catalog, TimeTreeLabelCatalog):
            raise GoogleToTimeTreeError(
                "TIMETREE_LABEL_CATALOG_INVALID",
                "TimeTree label catalog has an unsafe shape",
            )
        try:
            catalog.require_sync_labels()
        except ValueError as exc:
            raise GoogleToTimeTreeError(
                "UNSUPPORTED_TIMETREE_LABEL",
                "TimeTree sync labels are not uniquely resolvable",
            ) from exc
        self._label_catalog = catalog
        return catalog

    async def _get_events(self) -> tuple[Mapping[str, Any], ...]:
        getter = getattr(self.timetree_client, "get_events", None)
        if not callable(getter):
            raise GoogleToTimeTreeError(
                "TIMETREE_READ_UNAVAILABLE",
                "TimeTree events.get is required for P9 reconciliation",
            )
        try:
            raw_events = await _await_call(getter)
        except Exception as exc:
            raise GoogleToTimeTreeError(
                "TIMETREE_READ_FAILED",
                "TimeTree event read failed during reconciliation",
            ) from exc
        if not isinstance(raw_events, Sequence) or isinstance(raw_events, (str, bytes)):
            raise GoogleToTimeTreeError(
                "TIMETREE_READ_INVALID",
                "TimeTree event read returned an unsafe shape",
            )
        events: list[Mapping[str, Any]] = []
        for raw in raw_events:
            if not isinstance(raw, Mapping):
                raise GoogleToTimeTreeError(
                    "TIMETREE_READ_INVALID",
                    "TimeTree event read contained a non-object",
                )
            events.append(raw)
        return tuple(events)

    async def _get_updated_events(
        self, updated_after_ms: int
    ) -> tuple[Mapping[str, Any], ...]:
        getter = getattr(self.timetree_client, "get_updated_events", None)
        if not callable(getter):
            return await self._get_events()
        try:
            raw_events = await _await_call(getter, updated_after_ms)
        except Exception as exc:
            raise GoogleToTimeTreeError(
                "TIMETREE_CONFLICT_GUARD_FAILED",
                "TimeTree updated-event read failed before write",
            ) from exc
        if not isinstance(raw_events, Sequence) or isinstance(raw_events, (str, bytes)):
            raise GoogleToTimeTreeError(
                "TIMETREE_CONFLICT_GUARD_INVALID",
                "TimeTree updated-event read returned an unsafe shape",
            )
        return tuple(raw for raw in raw_events if isinstance(raw, Mapping))

    def _normalize_timetree(self, raw: Mapping[str, Any]) -> NormalizedEvent:
        catalog = self._label_catalog
        if catalog is None:
            raise GoogleToTimeTreeError(
                "TIMETREE_LABEL_CATALOG_REQUIRED",
                "TimeTree label catalog is required for normalization",
            )
        try:
            return normalize_timetree_event(
                raw,
                default_timezone=self.default_timezone,
                label_catalog=catalog,
            )
        except (NormalizationError, EventEligibilityError) as exc:
            code = getattr(exc, "code", None)
            if code is None and isinstance(exc, EventEligibilityError):
                code = exc.eligibility.code
            raise GoogleToTimeTreeError(
                code or "TIMETREE_NORMALIZATION_FAILED",
                "TimeTree event could not be normalized safely",
            ) from exc

    def _normalize_google_raw(self, raw: Mapping[str, Any]) -> NormalizedEvent:
        calendar_id = getattr(self.google_client, "calendar_id", None)
        if not isinstance(calendar_id, str) or not calendar_id:
            raise GoogleToTimeTreeError(
                "GOOGLE_CALENDAR_ID_REQUIRED",
                "Google calendar ID is required for recovery normalization",
            )
        try:
            return normalize_google_event(
                raw,
                source_calendar_id=calendar_id,
                default_timezone=self.default_timezone,
                google_new_default=self.google_new_default_label,
            )
        except (NormalizationError, EventEligibilityError) as exc:
            code = getattr(exc, "code", None)
            if code is None and isinstance(exc, EventEligibilityError):
                code = exc.eligibility.code
            raise GoogleToTimeTreeError(
                code or "GOOGLE_NORMALIZATION_FAILED",
                "Google event could not be normalized safely",
            ) from exc

    def _operation_id(
        self,
        *,
        action: str,
        event: NormalizedEvent,
        source_hash: str,
    ) -> str:
        suffix = "" if action == CREATE_ACTION else f":{source_hash}"
        return f"p9:{GOOGLE_TO_TIMETREE_DIRECTION}:{action}:{event.source_event_id}{suffix}"

    @staticmethod
    def _delete_value(value: date | datetime | None) -> str | None:
        return value.isoformat() if value is not None else None

    def _deferred_delete_operation_data(
        self,
        change: EventChange,
        link: Mapping[str, Any] | None,
    ) -> tuple[str, str, str, dict[str, Any]]:
        if change.change_type is ChangeType.DELETE:
            action = DELETE_ACTION
        elif change.change_type is ChangeType.RECURRENCE_EXCEPTION_DELETE:
            action = RECURRENCE_EXCEPTION_DELETE_ACTION
        else:
            raise self._error(
                "DEFERRED_DELETE_INVALID",
                "only Google DELETE changes can be journaled as deferred deletes",
            )

        payload = {
            "change_type": change.change_type.value,
            "source_event_id": change.source_event_id,
            "parent_source_event_id": change.parent_source_event_id,
            "original_start": self._delete_value(change.original_start),
            "timetree_event_id": (
                str(link["timetree_event_id"])
                if link is not None and link.get("timetree_event_id") is not None
                else None
            ),
        }
        payload_hash = _payload_hash(payload)
        source_hash = _payload_hash(
            {
                "change_type": change.change_type.value,
                "source_event_id": change.source_event_id,
                "parent_source_event_id": change.parent_source_event_id,
                "original_start": self._delete_value(change.original_start),
            }
        )
        operation_id = (
            f"p9:{GOOGLE_TO_TIMETREE_DIRECTION}:{action}:{change.source_event_id}"
        )
        return operation_id, source_hash, payload_hash, payload

    def _prepare_deferred_delete(self, change: EventChange) -> None:
        link = self.repository.get_event_link_by_google_id(change.source_event_id)
        operation_id, source_hash, payload_hash, payload = (
            self._deferred_delete_operation_data(change, link)
        )
        existing = self.repository.get_operation(operation_id)
        if existing is not None:
            if (
                existing.get("direction") != GOOGLE_TO_TIMETREE_DIRECTION
                or existing.get("action")
                != (
                    DELETE_ACTION
                    if change.change_type is ChangeType.DELETE
                    else RECURRENCE_EXCEPTION_DELETE_ACTION
                )
                or existing.get("source_event_id") != change.source_event_id
                or existing.get("source_hash") != source_hash
                or existing.get("payload_hash") != payload_hash
                or existing.get("target_event_id") != payload.get("timetree_event_id")
                or existing.get("state") != "prepared"
            ):
                raise self._manual_recovery(
                    operation_id,
                    "deferred delete journal does not match the retained Google change",
                )
            return

        self.repository.create_operation(
            operation_id=operation_id,
            direction=GOOGLE_TO_TIMETREE_DIRECTION,
            action=(
                DELETE_ACTION
                if change.change_type is ChangeType.DELETE
                else RECURRENCE_EXCEPTION_DELETE_ACTION
            ),
            source_event_id=change.source_event_id,
            target_event_id=payload.get("timetree_event_id"),
            source_hash=source_hash,
            payload_hash=payload_hash,
        )

    def _create_payload(
        self,
        event: NormalizedEvent,
    ) -> tuple[dict[str, Any], str]:
        catalog = self._label_catalog
        if catalog is None:
            raise GoogleToTimeTreeError(
                "TIMETREE_LABEL_CATALOG_REQUIRED",
                "TimeTree label catalog is required before create",
            )
        try:
            body = timetree_event_body(
                event,
                calendar_id=str(self.timetree_client.calendar_id),
                default_timezone=self.default_timezone,
                allow_recurrence_write=self.allow_recurrence_write,
                label_catalog=catalog,
            )
        except Exception as exc:
            code = getattr(exc, "code", None) or "TIMETREE_PAYLOAD_UNSUPPORTED"
            raise GoogleToTimeTreeError(
                code, "TimeTree create payload is unsupported"
            ) from exc
        return body, _payload_hash(body)

    def _update_payload(
        self,
        event: NormalizedEvent,
        fields: set[str],
    ) -> tuple[dict[str, Any], str]:
        catalog = self._label_catalog if "label" in fields else None
        try:
            body = timetree_update_body(
                event,
                fields=fields,
                calendar_id=str(self.timetree_client.calendar_id),
                default_timezone=self.default_timezone,
                allow_recurrence_write=self.allow_recurrence_write,
                label_catalog=catalog,
            )
        except Exception as exc:
            code = getattr(exc, "code", None) or "TIMETREE_PAYLOAD_UNSUPPORTED"
            raise GoogleToTimeTreeError(
                code, "TimeTree update payload is unsupported"
            ) from exc
        return body, _payload_hash(body)

    def _event_payload_hash(self, event: NormalizedEvent) -> str:
        body, payload_hash = self._create_payload(event)
        del body
        return payload_hash

    def _manual_recovery(
        self,
        operation_id: str,
        message: str = "remote create identity cannot be proven uniquely",
    ) -> GoogleToTimeTreeError:
        operation = self.repository.get_operation(operation_id)
        if operation is not None and operation["state"] in _RECOVERY_STATES:
            self.repository.mark_manual_recovery(operation_id)
        return GoogleToTimeTreeError(NEEDS_MANUAL_RECOVERY, message)

    def _validate_operation(
        self,
        operation: Mapping[str, Any],
        *,
        action: str,
        event: NormalizedEvent,
        source_hash: str,
        payload_hash: str,
    ) -> None:
        if (
            operation.get("direction") != GOOGLE_TO_TIMETREE_DIRECTION
            or operation.get("action") != action
            or operation.get("source_event_id") != event.source_event_id
            or operation.get("source_hash") != source_hash
            or operation.get("payload_hash") != payload_hash
        ):
            raise self._manual_recovery(
                str(operation["operation_id"]),
                "stored operation does not match the current normalized Google event",
            )

    async def _target_event(
        self,
        target_id: str,
        *,
        response_raw: Mapping[str, Any] | None = None,
    ) -> tuple[Mapping[str, Any], NormalizedEvent]:
        if response_raw is not None:
            try:
                if response_raw.get("uuid") == target_id:
                    return response_raw, self._normalize_timetree(response_raw)
            except GoogleToTimeTreeError:
                pass

        matches = [
            raw for raw in await self._get_events() if raw.get("uuid") == target_id
        ]
        if len(matches) != 1:
            raise GoogleToTimeTreeError(
                "TIMETREE_TARGET_RECONCILIATION_FAILED",
                "TimeTree create/update target could not be read back uniquely",
            )
        return matches[0], self._normalize_timetree(matches[0])

    def _save_create_mapping(
        self,
        event: NormalizedEvent,
        *,
        target_id: str,
        source_hash: str,
    ) -> None:
        existing_google = self.repository.get_event_link_by_google_id(
            event.source_event_id
        )
        existing_timetree = self.repository.get_event_link_by_timetree_id(target_id)
        for existing in (existing_google, existing_timetree):
            if existing is None:
                continue
            if (
                existing.get("google_event_id") != event.source_event_id
                or existing.get("timetree_event_id") != target_id
                or existing.get("last_synced_hash") not in (None, source_hash)
            ):
                raise GoogleToTimeTreeError(
                    "MAPPING_MISMATCH",
                    "existing SQLite mapping identifies a different event",
                )

        if existing_google is None and existing_timetree is None:
            self.repository.create_event_link(
                timetree_event_id=target_id,
                google_event_id=event.source_event_id,
                google_parent_event_id=event.parent_source_event_id,
                event_kind=event.kind.value,
                last_synced_hash=source_hash,
                status="synced",
                last_synced_at=_utc_now_iso(),
            )
            return

        existing = existing_google or existing_timetree
        assert existing is not None
        self.repository.update_event_link(
            int(existing["id"]),
            status="synced",
            last_synced_hash=source_hash,
            last_synced_at=_utc_now_iso(),
            deleted_at=None,
            clear_deleted_at=True,
        )

    def _google_metadata(
        self, event: NormalizedEvent, target_id: str
    ) -> dict[str, str]:
        return {
            "sync_source": GOOGLE_BRIDGE_SYNC_SOURCE,
            "timetree_id": target_id,
            GOOGLE_TIMETREE_LABEL_PROPERTY: event.label,
            "bridge_version": self.bridge_version,
        }

    @staticmethod
    def _private_google_metadata(raw: Any) -> Mapping[str, Any] | None:
        if not isinstance(raw, Mapping):
            return None
        extended = raw.get("extendedProperties")
        if not isinstance(extended, Mapping):
            return None
        private = extended.get("private")
        return private if isinstance(private, Mapping) else None

    def _verify_google_metadata(
        self,
        raw: Any,
        *,
        event: NormalizedEvent,
        target_id: str,
    ) -> None:
        private = self._private_google_metadata(raw)
        expected = self._google_metadata(event, target_id)
        if private is None or any(
            private.get(key) != value for key, value in expected.items()
        ):
            raise self._error(
                "GOOGLE_METADATA_MISMATCH",
                "Google metadata read-back does not match the canonical bridge metadata",
            )

    async def _patch_and_verify_google_metadata(
        self,
        event: NormalizedEvent,
        target_id: str,
    ) -> None:
        patcher = getattr(self.google_client, "patch_event", None)
        getter = getattr(self.google_client, "get_event", None)
        if not callable(patcher) or not callable(getter):
            raise self._error(
                "GOOGLE_METADATA_PATCH_UNAVAILABLE",
                "Google events.patch and events.get are required for create completion",
            )
        body = {
            "extendedProperties": {"private": self._google_metadata(event, target_id)}
        }
        try:
            patched = await _await_call(patcher, event.source_event_id, body)
        except Exception as exc:
            self._remote_write_ambiguous()
            raise self._error(
                "GOOGLE_METADATA_PATCH_FAILED",
                "Google metadata patch failed; mapping_saved recovery is retained",
            ) from exc
        self._remote_write_succeeded()
        self._verify_google_metadata(patched, event=event, target_id=target_id)
        try:
            read_back = await _await_call(getter, event.source_event_id)
        except Exception as exc:
            raise self._error(
                "GOOGLE_METADATA_VERIFY_FAILED",
                "Google metadata could not be read back after patch",
            ) from exc
        self._verify_google_metadata(read_back, event=event, target_id=target_id)

    async def _finish_create(
        self,
        operation: Mapping[str, Any],
        event: NormalizedEvent,
        *,
        source_hash: str,
        target_id: str,
        target_raw: Mapping[str, Any] | None = None,
        remote_create_succeeded: bool = False,
    ) -> None:
        operation_id = str(operation["operation_id"])
        current = self.repository.get_operation(operation_id)
        assert current is not None
        stored_target = current.get("target_event_id")
        if stored_target not in (None, target_id):
            raise self._manual_recovery(
                operation_id,
                "operation journal and remote target identify different events",
            )
        if current["state"] == "prepared" and remote_create_succeeded:
            self.repository.transition_operation(
                operation_id,
                "remote_applied",
                target_event_id=target_id,
            )
        elif current["state"] in {"remote_applied", "mapping_saved", "done"}:
            if stored_target != target_id:
                raise self._manual_recovery(
                    operation_id,
                    "applied operation is missing its target UUID",
                )
        elif current["state"] == "prepared":
            raise self._manual_recovery(
                operation_id,
                "prepared create has no persisted target UUID",
            )
        else:
            raise self._manual_recovery(operation_id, "operation is not recoverable")

        try:
            _, target_event = await self._target_event(
                target_id,
                response_raw=target_raw,
            )
        except GoogleToTimeTreeError as exc:
            raise self._error(exc.code, str(exc)) from exc
        if event_hash(target_event) != source_hash:
            self.repository.mark_manual_recovery(operation_id)
            raise self._error(
                "TIMETREE_TARGET_HASH_MISMATCH",
                "TimeTree create response changed the normalized event meaning",
            )

        current = self.repository.get_operation(operation_id)
        assert current is not None
        if current["state"] == "remote_applied":
            self._save_create_mapping(
                event,
                target_id=target_id,
                source_hash=source_hash,
            )
            self.repository.transition_operation(operation_id, "mapping_saved")
        elif current["state"] == "mapping_saved":
            link = self.repository.get_event_link_by_google_id(event.source_event_id)
            if link is None or link.get("timetree_event_id") != target_id:
                raise self._manual_recovery(
                    operation_id,
                    "mapping_saved create has no exact SQLite mapping",
                )
        elif current["state"] == "done":
            return

        current = self.repository.get_operation(operation_id)
        assert current is not None
        if current["state"] == "mapping_saved":
            await self._patch_and_verify_google_metadata(event, target_id)
            self.repository.transition_operation(operation_id, "done")

    async def _create_or_recover(self, event: NormalizedEvent) -> bool:
        source_hash = event_hash(event)
        _, payload_hash = self._create_payload(event)
        operation_id = self._operation_id(
            action=CREATE_ACTION,
            event=event,
            source_hash=source_hash,
        )
        operation = self.repository.get_operation(operation_id)
        if operation is not None:
            self._validate_operation(
                operation,
                action=CREATE_ACTION,
                event=event,
                source_hash=source_hash,
                payload_hash=payload_hash,
            )
            if operation["state"] == "failed":
                raise GoogleToTimeTreeError(
                    NEEDS_MANUAL_RECOVERY,
                    "Google to TimeTree create requires manual recovery",
                )
            if operation["state"] == "done":
                link = self.repository.get_event_link_by_google_id(
                    event.source_event_id
                )
                if link is None:
                    raise self._manual_recovery(
                        operation_id,
                        "done create operation has no SQLite mapping",
                    )
                return False

            if operation["state"] == "prepared":
                raise self._manual_recovery(
                    operation_id,
                    "prepared create has no persisted target UUID",
                )

            target_id = operation.get("target_event_id")
            if not isinstance(target_id, str) or not target_id:
                raise self._manual_recovery(
                    operation_id,
                    "applied create operation has no target UUID",
                )
            await self._finish_create(
                operation,
                event,
                source_hash=source_hash,
                target_id=target_id,
            )
            return False

        self.repository.create_operation(
            operation_id=operation_id,
            direction=GOOGLE_TO_TIMETREE_DIRECTION,
            action=CREATE_ACTION,
            source_event_id=event.source_event_id,
            source_hash=source_hash,
            payload_hash=payload_hash,
        )
        operation = self.repository.get_operation(operation_id)
        assert operation is not None

        try:
            result = await _await_call(
                self.timetree_client.create_event,
                event,
                allow_recurrence_write=self.allow_recurrence_write,
            )
        except Exception as exc:
            self._remote_write_ambiguous()
            raise self._error(
                "TIMETREE_CREATE_FAILED",
                "TimeTree create failed; the prepared operation was retained",
            ) from exc

        self._remote_write_succeeded()

        target_id = getattr(result, "event_uuid", None)
        target_raw = getattr(result, "raw_event", None)
        if not isinstance(target_id, str) or not target_id:
            self.repository.mark_manual_recovery(operation_id)
            raise GoogleToTimeTreeError(
                NEEDS_MANUAL_RECOVERY,
                "TimeTree create did not return a canonical event UUID",
            )
        await self._finish_create(
            operation,
            event,
            source_hash=source_hash,
            target_id=target_id,
            target_raw=target_raw if isinstance(target_raw, Mapping) else None,
            remote_create_succeeded=True,
        )
        return True

    def _fields_changed(
        self,
        source: NormalizedEvent,
        target: NormalizedEvent,
    ) -> set[str]:
        if EventKind.EXCEPTION in {source.kind, target.kind}:
            raise GoogleToTimeTreeError(
                "UNSUPPORTED_RECURRENCE_EXCEPTION",
                "recurrence exception transitions remain closed",
            )
        fields: set[str] = set()
        if source.kind is not target.kind:
            fields.add("recurrence")
        if source.title != target.title:
            fields.add("title")
        if (source.description or "") != (target.description or ""):
            fields.add("description")
        if (source.location or "") != (target.location or ""):
            fields.add("location")
        if source.label != target.label:
            fields.add("label")
        if source.all_day != target.all_day:
            fields.add("all_day")
        if source.start != target.start:
            fields.add("start")
        if source.end != target.end:
            fields.add("end")
        if source.start_timezone != target.start_timezone:
            fields.add("start_timezone")
        if source.end_timezone != target.end_timezone:
            fields.add("end_timezone")
        if source.recurrence.lines != target.recurrence.lines:
            fields.add("recurrence")
        return fields

    async def _update_mapped(
        self,
        event: NormalizedEvent,
        link: Mapping[str, Any],
        current: NormalizedEvent,
    ) -> bool:
        source_hash = event_hash(event)
        fields = self._fields_changed(event, current)
        link_id = int(link["id"])
        if not fields:
            if link.get("last_synced_hash") != source_hash:
                self.repository.update_event_link(
                    link_id,
                    status="synced",
                    last_synced_hash=source_hash,
                    last_synced_at=_utc_now_iso(),
                    event_kind=event.kind.value,
                    deleted_at=None,
                    clear_deleted_at=True,
                )
            return False

        if "label" in fields:
            await self._get_label_catalog()
        _, payload_hash = self._update_payload(event, fields)
        operation_id = self._operation_id(
            action=UPDATE_ACTION,
            event=event,
            source_hash=source_hash,
        )
        operation = self.repository.get_operation(operation_id)
        if operation is not None:
            self._validate_operation(
                operation,
                action=UPDATE_ACTION,
                event=event,
                source_hash=source_hash,
                payload_hash=payload_hash,
            )
            if operation["state"] == "done":
                return False
            raise self._manual_recovery(
                operation_id,
                "a prior update operation is not safely retryable in P9-A",
            )

        self.repository.create_operation(
            operation_id=operation_id,
            direction=GOOGLE_TO_TIMETREE_DIRECTION,
            action=UPDATE_ACTION,
            source_event_id=event.source_event_id,
            target_event_id=str(link["timetree_event_id"]),
            source_hash=source_hash,
            payload_hash=payload_hash,
        )
        operation = self.repository.get_operation(operation_id)
        assert operation is not None

        try:
            result = await _await_call(
                self.timetree_client.update_event,
                str(link["timetree_event_id"]),
                event,
                fields=fields,
                allow_recurrence_write=self.allow_recurrence_write,
            )
        except Exception as exc:
            self._remote_write_ambiguous()
            raise self._error(
                "TIMETREE_UPDATE_FAILED",
                "TimeTree update failed; the prepared operation was retained",
            ) from exc

        self._remote_write_succeeded()

        returned_id = getattr(result, "event_uuid", None)
        if returned_id != link["timetree_event_id"]:
            self.repository.mark_manual_recovery(operation_id)
            raise self._error(
                NEEDS_MANUAL_RECOVERY,
                "TimeTree update returned a different event UUID",
            )
        self.repository.transition_operation(
            operation_id,
            "remote_applied",
            target_event_id=str(returned_id),
        )
        target_raw = getattr(result, "raw_event", None)
        try:
            _, target_event = await self._target_event(
                str(returned_id),
                response_raw=target_raw if isinstance(target_raw, Mapping) else None,
            )
        except GoogleToTimeTreeError as exc:
            raise self._error(exc.code, str(exc)) from exc
        target_hash = event_hash(target_event)
        if target_hash != source_hash:
            self.repository.mark_manual_recovery(operation_id)
            raise self._error(
                "TIMETREE_TARGET_HASH_MISMATCH",
                "TimeTree update response changed the normalized event meaning",
            )
        self.repository.update_event_link(
            link_id,
            status="synced",
            last_synced_hash=target_hash,
            last_synced_at=_utc_now_iso(),
            event_kind=target_event.kind.value,
            deleted_at=None,
            clear_deleted_at=True,
        )
        self.repository.transition_operation(operation_id, "mapping_saved")
        self.repository.transition_operation(operation_id, "done")
        return True

    async def _raw_google_change(self, change: EventChange) -> Mapping[str, Any]:
        if isinstance(change.raw, Mapping):
            return change.raw
        getter = getattr(self.google_client, "get_event", None)
        if not callable(getter):
            raise self._error(
                "GOOGLE_METADATA_VALIDATION_REQUIRED",
                "raw Google event metadata is required before P9 routing",
            )
        try:
            raw = await _await_call(getter, change.source_event_id)
        except Exception as exc:
            raise self._error(
                "GOOGLE_METADATA_VALIDATION_FAILED",
                "Google event metadata could not be validated before P9 routing",
            ) from exc
        if not isinstance(raw, Mapping):
            raise self._error(
                "GOOGLE_METADATA_VALIDATION_FAILED",
                "Google event metadata has an unsafe shape",
            )
        return raw

    async def _validate_google_mapping_metadata(
        self,
        change: EventChange,
        link: Mapping[str, Any] | None,
    ) -> None:
        raw = await self._raw_google_change(change)
        private = self._private_google_metadata(raw)
        if private is None:
            private = {}
        identity_keys = {
            "sync_source",
            "timetree_id",
            "bridge_version",
        }
        has_bridge_metadata = any(key in private for key in identity_keys)
        label = private.get(GOOGLE_TIMETREE_LABEL_PROPERTY)
        if link is None:
            if has_bridge_metadata:
                raise self._error(
                    "GOOGLE_METADATA_MAPPING_REQUIRED",
                    "Google bridge metadata exists without an SQLite mapping",
                )
            return

        if not isinstance(label, str) or label not in SYNC_TIMETREE_LABEL_NAMES:
            raise self._error(
                "UNSUPPORTED_GOOGLE_LABEL_METADATA",
                "mapped Google event is missing or has invalid TimeTree label metadata",
            )
        if private.get("sync_source") != GOOGLE_BRIDGE_SYNC_SOURCE:
            raise self._error(
                "GOOGLE_METADATA_MAPPING_MISMATCH",
                "mapped Google event has missing or foreign sync_source metadata",
            )
        timetree_id = private.get("timetree_id")
        if not isinstance(timetree_id, str) or not timetree_id:
            raise self._error(
                "GOOGLE_METADATA_MAPPING_MISMATCH",
                "managed Google event is missing its TimeTree UUID metadata",
            )
        if timetree_id != link.get("timetree_event_id"):
            raise self._error(
                "GOOGLE_METADATA_MAPPING_MISMATCH",
                "Google timetree_id metadata does not match SQLite mapping",
            )

    async def _conflict_guard(
        self,
        mapped: Sequence[tuple[EventChange, Mapping[str, Any]]],
    ) -> dict[str, NormalizedEvent]:
        if not mapped:
            return {}
        catalog = await self._get_label_catalog()
        del catalog
        oldest = min(_timestamp_ms(link.get("last_synced_at")) for _, link in mapped)
        updated_after = max(0, oldest - self.overlap_seconds * 1000)
        await self._get_updated_events(updated_after)
        target_ids = {str(link["timetree_event_id"]) for _, link in mapped}
        current_raw = await self._get_events()
        current_by_id: dict[str, NormalizedEvent] = {}
        for raw in current_raw:
            event_id = raw.get("uuid")
            if isinstance(event_id, str) and event_id in target_ids:
                current_by_id[event_id] = self._normalize_timetree(raw)

        for change, link in mapped:
            target_id = str(link["timetree_event_id"])
            current_event = current_by_id.get(target_id)
            if current_event is None:
                raise GoogleToTimeTreeError(
                    "CONFLICT_GUARD_TARGET_MISSING",
                    "mapped TimeTree event is not present for a safe update",
                )
            current_hash = event_hash(current_event)
            if current_hash != link.get("last_synced_hash"):
                self._record_conflict(
                    change,
                    link,
                    current_event,
                    event_hash(_event_change_event(change)),
                )
                raise GoogleToTimeTreeError(
                    "CONFLICT",
                    "TimeTree changed after the last synchronized checkpoint",
                )
        return current_by_id

    def _record_conflict(
        self,
        change: EventChange,
        link: Mapping[str, Any],
        timetree_event: NormalizedEvent,
        google_hash: str,
    ) -> None:
        conflict_id = (
            f"p9:conflict:{change.source_event_id}:"
            f"{link.get('last_synced_hash') or 'none'}:{google_hash}"
        )
        if self.repository.get_conflict(conflict_id) is None:
            self.repository.create_conflict(
                conflict_id=conflict_id,
                event_link_id=int(link["id"]),
                conflict_type="google_update_timetree_update",
                timetree_snapshot_json=canonical_event_json(timetree_event),
                google_snapshot_json=canonical_event_json(_event_change_event(change)),
                status="open",
            )
        self.repository.update_event_link(int(link["id"]), status="conflict")

    async def _recover_pending_creates(self) -> None:
        operations = self.repository.list_operations(
            direction=GOOGLE_TO_TIMETREE_DIRECTION,
            action=CREATE_ACTION,
            states=_RECOVERY_STATES,
        )
        if not operations:
            return
        for operation in operations:
            if operation.get("state") == "prepared":
                raise self._manual_recovery(
                    str(operation["operation_id"]),
                    "prepared create has no persisted target UUID",
                )
        await self._get_label_catalog()
        getter = getattr(self.google_client, "get_event", None)
        if not callable(getter):
            raise GoogleToTimeTreeError(
                "RECOVERY_LOOKUP_FAILED",
                "Google events.get is required for pending create recovery",
            )
        for operation in operations:
            source_id = operation.get("source_event_id")
            if not isinstance(source_id, str) or not source_id:
                raise self._manual_recovery(str(operation["operation_id"]))
            try:
                raw = await _await_call(getter, source_id)
            except Exception as exc:
                raise GoogleToTimeTreeError(
                    "RECOVERY_LOOKUP_FAILED",
                    "Google source event could not be read for pending recovery",
                ) from exc
            if not isinstance(raw, Mapping) or raw.get("status") == "cancelled":
                raise self._manual_recovery(str(operation["operation_id"]))
            event = self._normalize_google_raw(raw)
            source_hash = event_hash(event)
            _, payload_hash = self._create_payload(event)
            self._validate_operation(
                operation,
                action=CREATE_ACTION,
                event=event,
                source_hash=source_hash,
                payload_hash=payload_hash,
            )
            await self._create_or_recover(event)

    async def run(self) -> GoogleToTimeTreeResult:
        source_token = self.repository.get_sync_state("google_sync_token")
        if not source_token:
            raise GoogleToTimeTreeError(
                "GOOGLE_SYNC_TOKEN_MISSING",
                "P9 incremental sync requires the P8 google_sync_token",
            )
        if not self.repository.get_sync_state("bridge_bootstrapped_at"):
            raise self._error(
                "P9_BOOTSTRAP_REQUIRED",
                "P9 incremental sync requires the completed P8 bootstrap state",
            )

        await self._recover_pending_creates()
        try:
            result = await _await_call(
                self.google_client.list_changes,
                sync_token=source_token,
            )
        except FullResyncRequired as exc:
            raise GoogleToTimeTreeError(
                FullResyncRequired.code,
                "Google sync token is invalid; P11 full recovery is required",
            ) from exc
        except (NormalizationError, EventEligibilityError) as exc:
            code = getattr(exc, "code", None)
            if code is None and isinstance(exc, EventEligibilityError):
                code = exc.eligibility.code
            raise self._error(
                code or "GOOGLE_NORMALIZATION_FAILED",
                "Google incremental event could not be normalized safely",
            ) from exc
        except Exception as exc:
            raise self._error(
                "GOOGLE_INCREMENTAL_READ_FAILED",
                "Google incremental read failed; the stored token was retained",
            ) from exc

        changes = getattr(result, "changes", None)
        next_token = getattr(result, "next_sync_token", None)
        if not isinstance(changes, Sequence) or isinstance(changes, (str, bytes)):
            raise GoogleToTimeTreeError(
                "GOOGLE_INCREMENTAL_INVALID",
                "Google incremental result has an unsafe changes shape",
            )
        if not isinstance(next_token, str) or not next_token:
            raise GoogleToTimeTreeError(
                "GOOGLE_INCREMENTAL_INVALID",
                "Google incremental result has no next sync token",
            )

        mapped: list[tuple[EventChange, Mapping[str, Any]]] = []
        unchanged_google_ids: set[str] = set()
        for change in changes:
            if not isinstance(change, EventChange):
                raise GoogleToTimeTreeError(
                    "GOOGLE_INCREMENTAL_INVALID",
                    "Google incremental result contains an invalid EventChange",
                )
            if change.change_type is ChangeType.UPSERT:
                link = self.repository.get_event_link_by_google_id(
                    change.source_event_id
                )
                await self._validate_google_mapping_metadata(change, link)
                if link is not None:
                    google_hash = event_hash(_event_change_event(change))
                    if google_hash == link.get("last_synced_hash"):
                        unchanged_google_ids.add(change.source_event_id)
                        continue
                    mapped.append((change, link))

        current_by_id = await self._conflict_guard(mapped)
        created = 0
        updated = 0
        skipped = 0
        deferred_delete = 0
        conflict_count = 0

        for change in changes:
            if change.change_type is ChangeType.DELETE:
                self._prepare_deferred_delete(change)
                deferred_delete += 1
                continue
            if change.change_type is ChangeType.RECURRENCE_EXCEPTION_DELETE:
                self._prepare_deferred_delete(change)
                deferred_delete += 1
                continue

            event = _event_change_event(change)
            if event.source_event_id in unchanged_google_ids:
                skipped += 1
                continue
            link = self.repository.get_event_link_by_google_id(event.source_event_id)
            if link is None:
                if event.kind is EventKind.EXCEPTION:
                    raise GoogleToTimeTreeError(
                        "UNSUPPORTED_RECURRENCE_EXCEPTION",
                        "Google recurrence exception writes remain closed",
                    )
                await self._get_label_catalog()
                if await self._create_or_recover(event):
                    created += 1
                else:
                    skipped += 1
                continue

            current = current_by_id.get(str(link["timetree_event_id"]))
            if current is None:
                raise GoogleToTimeTreeError(
                    "CONFLICT_GUARD_TARGET_MISSING",
                    "mapped TimeTree event is not available for update",
                )
            if await self._update_mapped(event, link, current):
                updated += 1
            else:
                skipped += 1

        if deferred_delete:
            return GoogleToTimeTreeResult(
                status="deferred_delete",
                source_sync_token=source_token,
                next_sync_token=next_token,
                token_committed=False,
                processed_change_count=len(changes),
                created_event_count=created,
                updated_event_count=updated,
                skipped_event_count=skipped,
                deferred_delete_count=deferred_delete,
                conflict_count=conflict_count,
                confirmed_remote_writes=self.confirmed_remote_writes,
                remote_write_outcome_unknown=self.remote_write_outcome_unknown,
            )

        self.repository.set_sync_state("google_sync_token", next_token)
        self.repository.set_sync_state("last_google_sync_at", _utc_now_iso())
        return GoogleToTimeTreeResult(
            status="synced",
            source_sync_token=source_token,
            next_sync_token=next_token,
            token_committed=True,
            processed_change_count=len(changes),
            created_event_count=created,
            updated_event_count=updated,
            skipped_event_count=skipped,
            deferred_delete_count=deferred_delete,
            conflict_count=conflict_count,
            confirmed_remote_writes=self.confirmed_remote_writes,
            remote_write_outcome_unknown=self.remote_write_outcome_unknown,
        )
