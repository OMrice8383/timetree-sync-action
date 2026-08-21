from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .adapters import (
    EventEligibilityError,
    UnsupportedEventError,
    classify_timetree_event,
    normalize_google_event,
    normalize_timetree_event,
)
from .canonical import event_hash
from .google_client import google_event_body
from .models import (
    GOOGLE_BRIDGE_SYNC_SOURCE,
    GOOGLE_TIMETREE_LABEL_PROPERTY,
    ChangeType,
    EventClassification,
    NormalizedEvent,
    TimeTreeLabelCatalog,
)
from .repository import StateRepository

BOOTSTRAP_OPERATION_PREFIX = "bootstrap:timetree_to_google:create:"
_HEX_UUID = re.compile(
    r"(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)
_GOOGLE_EVENT_ID = re.compile(r"[a-v0-9]{5,1024}")


def deterministic_google_event_id(timetree_uuid: str) -> str:
    """Return the stable Google Event ID for a confirmed TimeTree UUID.

    The current confirmed TimeTree identity is a 32-hex UUID, optionally in
    the conventional hyphenated form.  We intentionally reject other shapes
    instead of inventing a lossy encoding for an unconfirmed identity format.
    Hexadecimal characters are already a subset of Google's base32hex ID
    alphabet; the ``tt`` prefix makes the namespace explicit.
    """
    if not isinstance(timetree_uuid, str) or not _HEX_UUID.fullmatch(timetree_uuid):
        raise ValueError("TimeTree UUID must be a confirmed hexadecimal UUID")
    normalized = timetree_uuid.replace("-", "").lower()
    event_id = "tt" + normalized
    if not _GOOGLE_EVENT_ID.fullmatch(event_id):
        raise ValueError("generated Google Event ID is outside the safe contract")
    return event_id


def _http_status(exc: BaseException) -> int | None:
    response = getattr(exc, "resp", None)
    status = getattr(response, "status", None)
    if status is None:
        status = getattr(exc, "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


class BootstrapError(RuntimeError):
    """Raised when Bootstrap must stop without unsafe continuation."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    status: str
    bootstrap_started_ms: int | None
    eligible_event_count: int
    created_event_count: int
    recovered_event_count: int
    google_sync_token: str | None


@dataclass(frozen=True, slots=True)
class _ManagedGoogleEvent:
    raw: Mapping[str, Any]
    event: NormalizedEvent


@dataclass(frozen=True, slots=True)
class _EligibleTimeTreeEvent:
    raw: Mapping[str, Any]
    event: NormalizedEvent
    source_hash: str


def _default_now_utc() -> datetime:
    return datetime.now(UTC)


def _default_clock_ms() -> int:
    return int(time.time() * 1000)


def _private_properties(raw: Mapping[str, Any]) -> Mapping[str, Any] | None:
    extended = raw.get("extendedProperties")
    if not isinstance(extended, Mapping):
        return None
    private = extended.get("private")
    if not isinstance(private, Mapping):
        return None
    return private


def _managed_timetree_id(raw: Mapping[str, Any]) -> str | None:
    private = _private_properties(raw)
    if private is None:
        return None
    if private.get("sync_source") != GOOGLE_BRIDGE_SYNC_SOURCE:
        return None
    timetree_id = private.get("timetree_id")
    if not isinstance(timetree_id, str) or not timetree_id:
        raise BootstrapError(
            "UNSUPPORTED_GOOGLE_METADATA",
            "managed Google event is missing timetree_id",
        )
    return timetree_id


def _recurrence_property_name(line: object) -> str | None:
    if not isinstance(line, str) or ":" not in line:
        return None
    return line.split(":", 1)[0].split(";", 1)[0].strip().upper()


def _raise_normalization_failure(exc: BaseException, *, source: str) -> BootstrapError:
    code = getattr(exc, "code", None)
    if not isinstance(code, str) or not code:
        code = f"UNSUPPORTED_{source.upper()}_SNAPSHOT"
    return BootstrapError(code, f"{source} snapshot failed the Bootstrap contract")


class BootstrapRunner:
    """P8-A TimeTree full-snapshot to Google bootstrap orchestrator.

    The runner deliberately accepts the existing client boundaries instead of
    constructing transports itself.  A fake can therefore exercise the whole
    orchestration without credentials or live writes.
    """

    def __init__(
        self,
        *,
        timetree_client: Any,
        google_client: Any,
        repository: StateRepository,
        default_timezone: str,
        bridge_version: str,
        allow_recurrence_write: bool = True,
        clock_ms: Callable[[], int] = _default_clock_ms,
        now_utc: Callable[[], datetime] = _default_now_utc,
    ) -> None:
        if not default_timezone:
            raise ValueError("default_timezone must not be empty")
        if not bridge_version:
            raise ValueError("bridge_version must not be empty")
        self.timetree_client = timetree_client
        self.google_client = google_client
        self.repository = repository
        self.default_timezone = default_timezone
        self.bridge_version = bridge_version
        self.allow_recurrence_write = allow_recurrence_write
        self.clock_ms = clock_ms
        self.now_utc = now_utc
        self._google_calendar_id = str(
            getattr(google_client, "calendar_id", "google-calendar")
        )
        self._managed_google: dict[str, tuple[_ManagedGoogleEvent, ...]] = {}

    async def run(self) -> BootstrapResult:
        if self.repository.get_sync_state("bridge_bootstrapped_at"):
            return BootstrapResult(
                status="already_bootstrapped",
                bootstrap_started_ms=None,
                eligible_event_count=0,
                created_event_count=0,
                recovered_event_count=0,
                google_sync_token=self.repository.get_sync_state(
                    "google_sync_token"
                ),
            )

        bootstrap_started_ms = self.clock_ms()
        if isinstance(bootstrap_started_ms, bool) or not isinstance(
            bootstrap_started_ms, int
        ):
            raise BootstrapError(
                "INVALID_BOOTSTRAP_WATERMARK",
                "bootstrap clock must return an integer Unix millisecond timestamp",
            )

        self._google_target_preflight()
        self._google_preflight()
        label_catalog = await self.timetree_client.get_calendar_labels()
        raw_timetree_events = await self.timetree_client.get_events()
        eligible = self._timetree_preflight(
            raw_timetree_events,
            label_catalog=label_catalog,
        )

        unexpected_managed = set(self._managed_google) - {
            item.event.source_event_id for item in eligible
        }
        if unexpected_managed:
            raise BootstrapError(
                "GOOGLE_MANAGED_EVENT_NOT_IN_TIMETREE_SNAPSHOT",
                "managed Google event does not have an eligible TimeTree source",
            )

        created = 0
        recovered = 0
        for item in eligible:
            was_created, was_recovered = self._create_or_recover(item)
            created += was_created
            recovered += was_recovered

        final_changes, final_google_token = self._google_snapshot()
        self._consistency_check(
            eligible,
            final_changes,
        )

        # These are committed only after every remote write and the final
        # read-only consistency check have succeeded.
        self.repository.set_sync_state("google_sync_token", final_google_token)
        self.repository.set_sync_state(
            "timetree_updated_after_ms",
            str(bootstrap_started_ms),
        )
        self.repository.set_sync_state(
            "bridge_bootstrapped_at",
            self._now_iso(),
        )

        return BootstrapResult(
            status="bootstrapped",
            bootstrap_started_ms=bootstrap_started_ms,
            eligible_event_count=len(eligible),
            created_event_count=created,
            recovered_event_count=recovered,
            google_sync_token=final_google_token,
        )

    def _now_iso(self) -> str:
        value = self.now_utc()
        if value.tzinfo is None:
            raise BootstrapError(
                "INVALID_BOOTSTRAP_CLOCK",
                "bootstrap completion clock must be timezone-aware",
            )
        return value.astimezone(UTC).isoformat()

    def _google_snapshot(
        self,
    ) -> tuple[tuple[Any, ...], str]:
        try:
            result = self.google_client.list_changes()
        except BootstrapError:
            raise
        except Exception as exc:
            raise BootstrapError(
                "UNSAFE_GOOGLE_SNAPSHOT",
                "Google full snapshot could not be read safely",
            ) from exc
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
        return tuple(changes), token

    def _google_target_preflight(self) -> None:
        getter = getattr(self.google_client, "get_calendar_metadata", None)
        if not callable(getter):
            raise BootstrapError(
                "GOOGLE_TARGET_PREFLIGHT_FAILED",
                "Google client does not expose target Calendar metadata",
            )
        try:
            metadata = getter()
        except Exception as exc:
            raise BootstrapError(
                "GOOGLE_TARGET_PREFLIGHT_FAILED",
                "Google target Calendar metadata could not be read",
            ) from exc
        if not isinstance(metadata, Mapping):
            raise BootstrapError(
                "GOOGLE_TARGET_PREFLIGHT_FAILED",
                "Google target Calendar metadata is not an object",
            )
        metadata_id = metadata.get("id")
        if not isinstance(metadata_id, (str, int)) or (
            str(metadata_id) != self._google_calendar_id
        ):
            raise BootstrapError(
                "GOOGLE_TARGET_PREFLIGHT_FAILED",
                "Google target Calendar id does not match configuration",
            )

    def _raw_google_change(self, change: Any) -> Mapping[str, Any] | None:
        raw = getattr(change, "raw", None)
        if isinstance(raw, Mapping):
            return dict(raw)
        if getattr(change, "change_type", None) is not ChangeType.UPSERT:
            return None

        getter = getattr(self.google_client, "get_event", None)
        if not callable(getter):
            return None
        try:
            fetched = getter(getattr(change, "source_event_id", ""))
        except Exception:  # noqa: BLE001 - metadata read must fail closed
            return None
        return dict(fetched) if isinstance(fetched, Mapping) else None

    def _normalize_google_raw(self, raw: Mapping[str, Any]) -> NormalizedEvent:
        try:
            return normalize_google_event(
                raw,
                source_calendar_id=self._google_calendar_id,
                default_timezone=self.default_timezone,
            )
        except (UnsupportedEventError, ValueError) as exc:
            raise _raise_normalization_failure(exc, source="Google") from exc

    def _google_preflight(self) -> tuple[tuple[Any, ...], str]:
        changes, token = self._google_snapshot()
        managed: dict[str, list[_ManagedGoogleEvent]] = {}
        for change in changes:
            if getattr(change, "change_type", None) is not ChangeType.UPSERT:
                # DELETE and cancelled recurrence tombstones do not represent
                # a live event and therefore do not block the empty preflight.
                continue
            raw = self._raw_google_change(change)
            if raw is None:
                raise BootstrapError(
                    "UNSAFE_GOOGLE_SNAPSHOT",
                    "Google live event metadata could not be read",
                )
            timetree_id = _managed_timetree_id(raw)
            if timetree_id is None:
                raise BootstrapError(
                    "UNMANAGED_GOOGLE_EVENT",
                    "Google dedicated Calendar contains a live unmanaged event",
                )
            event = self._normalize_google_raw(raw)
            managed.setdefault(timetree_id, []).append(
                _ManagedGoogleEvent(raw=raw, event=event)
            )

        self._managed_google = {
            timetree_id: tuple(events)
            for timetree_id, events in managed.items()
        }
        duplicates = [
            timetree_id
            for timetree_id, events in self._managed_google.items()
            if len(events) > 1
        ]
        if duplicates:
            raise BootstrapError(
                "DUPLICATE_GOOGLE_MANAGED_EVENT",
                "Google metadata identifies multiple live events for one TimeTree UUID",
            )
        return changes, token

    def _timetree_preflight(
        self,
        raw_events: Sequence[Mapping[str, Any]],
        *,
        label_catalog: TimeTreeLabelCatalog,
    ) -> tuple[_EligibleTimeTreeEvent, ...]:
        if not isinstance(raw_events, Sequence) or isinstance(raw_events, (str, bytes)):
            raise BootstrapError(
                "UNSAFE_TIMETREE_SNAPSHOT",
                "TimeTree full snapshot is not a sequence",
            )

        # This pass is intentionally before normalization.  P6 can parse
        # EXDATE as a series feature, but P7 evidence makes it unsafe for P8
        # bootstrap and must stop all writes.
        for raw in raw_events:
            if not isinstance(raw, Mapping):
                raise BootstrapError(
                    "UNSAFE_TIMETREE_SNAPSHOT",
                    "TimeTree full snapshot item is not an object",
                )
            if raw.get("parent_id") is not None or raw.get("recurring_uuid") is not None:
                raise BootstrapError(
                    "UNSUPPORTED_RECURRENCE_EXCEPTION",
                    "TimeTree child recurrence evidence blocks Bootstrap",
                )
            recurrences = raw.get("recurrences")
            if isinstance(recurrences, Sequence) and not isinstance(
                recurrences, (str, bytes)
            ):
                for line in recurrences:
                    if _recurrence_property_name(line) == "EXDATE":
                        raise BootstrapError(
                            "UNSUPPORTED_RECURRENCE_EXCEPTION",
                            "TimeTree master EXDATE evidence blocks Bootstrap",
                        )

        eligible: list[_EligibleTimeTreeEvent] = []
        seen_ids: set[str] = set()
        for raw in raw_events:
            eligibility = classify_timetree_event(
                raw,
                label_catalog=label_catalog,
            )
            if eligibility.classification is EventClassification.IGNORE_KNOWN:
                continue
            if eligibility.classification is not EventClassification.SYNC:
                raise BootstrapError(
                    eligibility.code,
                    "TimeTree event classification is unsupported for Bootstrap",
                )
            try:
                event = normalize_timetree_event(
                    raw,
                    default_timezone=self.default_timezone,
                    label_catalog=label_catalog,
                )
            except (EventEligibilityError, UnsupportedEventError, ValueError) as exc:
                raise _raise_normalization_failure(exc, source="TimeTree") from exc
            try:
                deterministic_google_event_id(event.source_event_id)
            except ValueError as exc:
                raise BootstrapError(
                    "UNSAFE_TIMETREE_UUID",
                    "TimeTree UUID cannot form a deterministic Google Event ID",
                ) from exc
            if event.source_event_id in seen_ids:
                raise BootstrapError(
                    "DUPLICATE_TIMETREE_UUID",
                    "TimeTree full snapshot contains duplicate eligible UUIDs",
                )
            seen_ids.add(event.source_event_id)
            eligible.append(
                _EligibleTimeTreeEvent(
                    raw=raw,
                    event=event,
                    source_hash=event_hash(event),
                )
            )
        return tuple(eligible)

    def _operation_id(self, event: NormalizedEvent) -> str:
        return BOOTSTRAP_OPERATION_PREFIX + event.source_event_id

    def _payload_hash(self, body: Mapping[str, Any]) -> str:
        payload = json.dumps(
            body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _create_operation(
        self,
        item: _EligibleTimeTreeEvent,
        operation_id: str,
        *,
        payload_hash: str | None = None,
    ) -> dict[str, Any]:
        self.repository.create_operation(
            operation_id=operation_id,
            direction="timetree_to_google",
            action="create",
            source_event_id=item.event.source_event_id,
            source_hash=item.source_hash,
            payload_hash=payload_hash,
        )
        operation = self.repository.get_operation(operation_id)
        assert operation is not None
        return operation

    def _fail_operation(self, operation_id: str, code: str) -> None:
        operation = self.repository.get_operation(operation_id)
        if operation is None or operation["state"] in {"failed", "done"}:
            return
        self.repository.transition_operation(
            operation_id,
            "failed",
            last_error=code,
        )

    def _validate_operation_source(
        self,
        operation: Mapping[str, Any],
        item: _EligibleTimeTreeEvent,
    ) -> None:
        if operation.get("source_hash") != item.source_hash:
            raise BootstrapError(
                "BOOTSTRAP_SOURCE_CHANGED",
                "source hash changed since the Bootstrap operation was prepared",
            )

    def _get_google_event(self, event_id: str) -> Mapping[str, Any] | None:
        getter = getattr(self.google_client, "get_event", None)
        if not callable(getter):
            raise BootstrapError(
                "RECOVERY_LOOKUP_FAILED",
                "Google client does not expose events.get for Bootstrap recovery",
            )
        try:
            raw = getter(event_id)
        except Exception as exc:
            if _http_status(exc) == 404:
                return None
            raise BootstrapError(
                "RECOVERY_LOOKUP_FAILED",
                "deterministic Google Event lookup was ambiguous or unavailable",
            ) from exc
        if not isinstance(raw, Mapping):
            raise BootstrapError(
                "RECOVERY_LOOKUP_FAILED",
                "Google events.get returned an unsafe response",
            )
        return dict(raw)

    def _primary_recovery_event(
        self,
        item: _EligibleTimeTreeEvent,
    ) -> tuple[str, Mapping[str, Any] | None]:
        event_id = deterministic_google_event_id(item.event.source_event_id)
        return event_id, self._get_google_event(event_id)

    def _assert_target_metadata(
        self,
        raw: Mapping[str, Any],
        item: _EligibleTimeTreeEvent,
    ) -> None:
        private = _private_properties(raw)
        if private is None:
            raise BootstrapError(
                "GOOGLE_METADATA_MISMATCH",
                "Google target is missing private bridge metadata",
            )
        if private.get("sync_source") != GOOGLE_BRIDGE_SYNC_SOURCE:
            raise BootstrapError(
                "GOOGLE_METADATA_MISMATCH",
                "Google target has an unexpected bridge sync source",
            )
        if private.get("timetree_id") != item.event.source_event_id:
            raise BootstrapError(
                "GOOGLE_METADATA_MISMATCH",
                "Google target timetree_id does not match the source UUID",
            )
        if private.get(GOOGLE_TIMETREE_LABEL_PROPERTY) != item.event.label:
            raise BootstrapError(
                "GOOGLE_METADATA_MISMATCH",
                "Google target TimeTree label metadata does not match the source",
            )

    def _save_mapping(
        self,
        item: _EligibleTimeTreeEvent,
        *,
        google_event_id: str,
        operation: Mapping[str, Any],
    ) -> None:
        existing = self.repository.get_event_link_by_timetree_id(
            item.event.source_event_id
        )
        if existing is None:
            self.repository.create_event_link(
                timetree_event_id=item.event.source_event_id,
                google_event_id=google_event_id,
                event_kind=item.event.kind.value,
                last_synced_hash=item.source_hash,
                status="synced",
                last_synced_at=self._now_iso(),
            )
            return
        if existing.get("google_event_id") != google_event_id:
            raise BootstrapError(
                "MAPPING_MISMATCH",
                "existing SQLite mapping points to a different Google event",
            )
        if existing.get("last_synced_hash") not in (None, item.source_hash):
            raise BootstrapError(
                "MAPPING_HASH_MISMATCH",
                "existing SQLite mapping has a different source hash",
            )
        self.repository.update_event_link(
            int(existing["id"]),
            status="synced",
            last_synced_hash=item.source_hash,
            last_synced_at=self._now_iso(),
        )

    def _commit_target(
        self,
        item: _EligibleTimeTreeEvent,
        operation: Mapping[str, Any],
        raw: Mapping[str, Any],
    ) -> None:
        operation_id = str(operation["operation_id"])
        target_id = raw.get("id")
        if not isinstance(target_id, str) or not target_id:
            self._fail_operation(operation_id, "GOOGLE_TARGET_ID_MISSING")
            raise BootstrapError(
                "GOOGLE_TARGET_ID_MISSING",
                "Google target is missing id",
            )

        expected_target_id = deterministic_google_event_id(
            item.event.source_event_id
        )
        if target_id != expected_target_id:
            self._fail_operation(operation_id, "GOOGLE_DETERMINISTIC_ID_MISMATCH")
            raise BootstrapError(
                "GOOGLE_DETERMINISTIC_ID_MISMATCH",
                "Google target id is not the deterministic TimeTree-derived id",
            )

        if operation.get("target_event_id") not in (None, target_id):
            self._fail_operation(operation_id, "GOOGLE_TARGET_ID_MISMATCH")
            raise BootstrapError(
                "GOOGLE_TARGET_ID_MISMATCH",
                "Google target id differs from the operation journal",
            )
        try:
            self._assert_target_metadata(raw, item)
            target_event = self._normalize_google_raw(raw)
            target_hash = event_hash(target_event)
            if target_hash != item.source_hash:
                raise BootstrapError(
                    "GOOGLE_TARGET_HASH_MISMATCH",
                    "Google target normalized hash differs from the source",
                )
            if target_event.kind is not item.event.kind:
                raise BootstrapError(
                    "GOOGLE_TARGET_KIND_MISMATCH",
                    "Google target event kind differs from the source",
                )
        except BootstrapError as exc:
            self._fail_operation(operation_id, exc.code)
            raise
        except Exception as exc:
            self._fail_operation(operation_id, "GOOGLE_TARGET_VALIDATION_FAILED")
            raise _raise_normalization_failure(exc, source="Google target") from exc

        current = self.repository.get_operation(operation_id)
        assert current is not None
        if current["state"] == "prepared":
            self.repository.transition_operation(
                operation_id,
                "remote_applied",
                target_event_id=target_id,
            )
        elif current.get("target_event_id") not in (None, target_id):
            self._fail_operation(operation_id, "GOOGLE_TARGET_ID_MISMATCH")
            raise BootstrapError(
                "GOOGLE_TARGET_ID_MISMATCH",
                "Google target id differs from the operation journal",
            )

        current = self.repository.get_operation(operation_id)
        assert current is not None
        if current["state"] == "remote_applied":
            self._save_mapping(item, google_event_id=target_id, operation=current)
            self.repository.transition_operation(operation_id, "mapping_saved")
        elif current["state"] == "mapping_saved":
            # Mapping may already have been saved before a crash.
            self._save_mapping(item, google_event_id=target_id, operation=current)
        elif current["state"] == "done":
            self._save_mapping(item, google_event_id=target_id, operation=current)
            return
        else:
            raise BootstrapError(
                "UNSAFE_OPERATION_STATE",
                "Bootstrap operation is not recoverable from its stored state",
            )

        current = self.repository.get_operation(operation_id)
        assert current is not None
        if current["state"] == "mapping_saved":
            self.repository.transition_operation(operation_id, "done")

    def _create_or_recover(
        self,
        item: _EligibleTimeTreeEvent,
    ) -> tuple[int, int]:
        event = item.event
        deterministic_id = deterministic_google_event_id(event.source_event_id)
        operation_id = self._operation_id(event)
        operation = self.repository.get_operation(operation_id)
        link = self.repository.get_event_link_by_timetree_id(event.source_event_id)

        if operation is not None:
            self._validate_operation_source(operation, item)
        if link is not None:
            if link.get("google_event_id") is None:
                raise BootstrapError(
                    "MAPPING_MISMATCH",
                    "existing SQLite mapping has no Google event id",
                )
            if link.get("google_event_id") != deterministic_id:
                raise BootstrapError(
                    "GOOGLE_DETERMINISTIC_ID_MISMATCH",
                    "existing SQLite mapping is not the deterministic Google event id",
                )
            if link.get("last_synced_hash") not in (None, item.source_hash):
                raise BootstrapError(
                    "MAPPING_HASH_MISMATCH",
                    "existing SQLite mapping has a different source hash",
                )
            if operation is None:
                return 0, 1
            if operation["state"] == "failed":
                raise BootstrapError(
                    "NEEDS_MANUAL_RECOVERY",
                    "Bootstrap operation is marked failed",
                )
            if operation.get("target_event_id") not in (
                None,
                link.get("google_event_id"),
            ):
                raise BootstrapError(
                    "MAPPING_MISMATCH",
                    "operation and SQLite mapping identify different Google events",
                )
            if operation["state"] == "prepared":
                self.repository.transition_operation(
                    operation_id,
                    "remote_applied",
                    target_event_id=str(link["google_event_id"]),
                )
            current = self.repository.get_operation(operation_id)
            assert current is not None
            if current["state"] == "remote_applied":
                self.repository.transition_operation(operation_id, "mapping_saved")
            current = self.repository.get_operation(operation_id)
            assert current is not None
            if current["state"] == "mapping_saved":
                self.repository.transition_operation(operation_id, "done")
            return 0, 1

        if operation is None:
            operation = self._create_operation(item, operation_id)

        if operation["state"] == "failed":
            raise BootstrapError(
                "NEEDS_MANUAL_RECOVERY",
                "Bootstrap operation is marked failed",
            )
        if operation["state"] == "done":
            raise BootstrapError(
                "NEEDS_MANUAL_RECOVERY",
                    "completed Bootstrap operation has no SQLite mapping",
                )

        if operation.get("target_event_id") not in (None, deterministic_id):
            self._fail_operation(operation_id, "GOOGLE_DETERMINISTIC_ID_MISMATCH")
            raise BootstrapError(
                "GOOGLE_DETERMINISTIC_ID_MISMATCH",
                "operation journal is not the deterministic Google event id",
            )

        _event_id, recovered = self._primary_recovery_event(item)
        if recovered is not None:
            self._commit_target(
                item,
                operation,
                recovered,
            )
            return 0, 1

        if self._managed_google.get(event.source_event_id):
            self._fail_operation(operation_id, "GOOGLE_DETERMINISTIC_ID_MISMATCH")
            raise BootstrapError(
                "GOOGLE_DETERMINISTIC_ID_MISMATCH",
                "managed Google event exists under an unexpected event id",
            )

        if operation["state"] != "prepared":
            self._fail_operation(operation_id, "NEEDS_MANUAL_RECOVERY")
            raise BootstrapError(
                "NEEDS_MANUAL_RECOVERY",
                "remote-applied Bootstrap operation has no unique Google event",
            )

        try:
            body = google_event_body(
                event,
                private_properties={
                    "sync_source": GOOGLE_BRIDGE_SYNC_SOURCE,
                    "timetree_id": event.source_event_id,
                    "timetree_label_name": event.label,
                    "bridge_version": self.bridge_version,
                },
                allow_recurrence_write=self.allow_recurrence_write,
            )
            body["id"] = deterministic_id
            if operation.get("payload_hash") is None:
                # The operation is already durable before the remote write;
                # payload_hash is diagnostic only and is not used as identity.
                self.repository.connection.execute(
                    "UPDATE sync_operations SET payload_hash = ? WHERE operation_id = ?",
                    (self._payload_hash(body), operation_id),
                )
                self.repository.connection.commit()
            self.repository.increment_operation_attempts(operation_id)
            try:
                response = self.google_client.insert_event(body)
            except Exception as exc:
                status = _http_status(exc)
                if status == 409:
                    conflict = self._get_google_event(deterministic_id)
                    if conflict is None:
                        raise BootstrapError(
                            "GOOGLE_CREATE_CONFLICT_UNRESOLVED",
                            "Google reported an insert conflict but the deterministic event was not found",
                        ) from exc
                    self._commit_target(item, operation, conflict)
                    return 0, 1
                if status is None or status == 429 or status >= 500:
                    raise BootstrapError(
                        "GOOGLE_CREATE_RETRYABLE",
                        "Google insert outcome is ambiguous; retry recovery before creating",
                    ) from exc
                self._fail_operation(operation_id, "GOOGLE_CREATE_FAILED")
                raise BootstrapError(
                    "GOOGLE_CREATE_FAILED",
                    "Google create failed before Bootstrap could commit the mapping",
                ) from exc

            raw_response = dict(response) if isinstance(response, Mapping) else None
            if raw_response is None:
                self._fail_operation(operation_id, "GOOGLE_CREATE_RESPONSE_INVALID")
                raise BootstrapError(
                    "GOOGLE_CREATE_RESPONSE_INVALID",
                    "Google insert response is not an object",
                )
            target_id = raw_response.get("id")
            if target_id != deterministic_id:
                self._fail_operation(operation_id, "GOOGLE_DETERMINISTIC_ID_MISMATCH")
                raise BootstrapError(
                    "GOOGLE_DETERMINISTIC_ID_MISMATCH",
                    "Google insert response did not return the requested deterministic event id",
                )
            if not isinstance(raw_response.get("start"), Mapping):
                fetched = self._get_google_event(deterministic_id)
                if fetched is None:
                    self._fail_operation(operation_id, "GOOGLE_CREATE_RESPONSE_INVALID")
                    raise BootstrapError(
                        "GOOGLE_CREATE_RESPONSE_INVALID",
                        "Google insert response could not be re-read by deterministic event id",
                    )
                raw_response = fetched
            self._commit_target(
                item,
                operation,
                raw_response,
            )
            return 1, 0
        except BootstrapError:
            raise
        except Exception as exc:
            self._fail_operation(operation_id, "GOOGLE_CREATE_FAILED")
            raise BootstrapError(
                "GOOGLE_CREATE_FAILED",
                "Google create failed before Bootstrap could commit the mapping",
            ) from exc

    def _consistency_check(
        self,
        eligible: Sequence[_EligibleTimeTreeEvent],
        changes: Sequence[Any],
    ) -> None:
        managed: dict[str, list[_ManagedGoogleEvent]] = {}
        for change in changes:
            if getattr(change, "change_type", None) is not ChangeType.UPSERT:
                continue
            raw = self._raw_google_change(change)
            if raw is None:
                raise BootstrapError(
                    "CONSISTENCY_GOOGLE_SNAPSHOT_UNSAFE",
                    "final Google snapshot event metadata could not be read",
                )
            try:
                timetree_id = _managed_timetree_id(raw)
            except BootstrapError as exc:
                raise BootstrapError(
                    "BOOTSTRAP_CONSISTENCY_MISMATCH",
                    "final Google snapshot has incomplete bridge metadata",
                ) from exc
            if timetree_id is None:
                raise BootstrapError(
                    "CONSISTENCY_UNMANAGED_GOOGLE_EVENT",
                    "final Google snapshot contains an unmanaged live event",
                )
            managed.setdefault(timetree_id, []).append(
                _ManagedGoogleEvent(
                    raw=raw,
                    event=self._normalize_google_raw(raw),
                )
            )

        expected_ids = {item.event.source_event_id for item in eligible}
        if set(managed) != expected_ids:
            raise BootstrapError(
                "BOOTSTRAP_CONSISTENCY_MISMATCH",
                "eligible TimeTree UUIDs and managed Google UUIDs differ",
            )
        for item in eligible:
            matches = managed.get(item.event.source_event_id, [])
            if len(matches) != 1:
                raise BootstrapError(
                    "BOOTSTRAP_CONSISTENCY_MISMATCH",
                    "a TimeTree UUID does not map to exactly one Google event",
                )
            match = matches[0]
            try:
                self._assert_target_metadata(match.raw, item)
            except BootstrapError as exc:
                raise BootstrapError(
                    "BOOTSTRAP_CONSISTENCY_MISMATCH",
                    "final Google metadata does not match the TimeTree source",
                ) from exc
            if match.event.kind is not item.event.kind:
                raise BootstrapError(
                    "BOOTSTRAP_CONSISTENCY_MISMATCH",
                    "Google event kind differs from TimeTree normalized kind",
                )
            if event_hash(match.event) != item.source_hash:
                raise BootstrapError(
                    "BOOTSTRAP_CONSISTENCY_MISMATCH",
                    "Google normalized event hash differs from TimeTree source hash",
                )
            link = self.repository.get_event_link_by_timetree_id(
                item.event.source_event_id
            )
            if link is None or link.get("google_event_id") != match.raw.get("id"):
                raise BootstrapError(
                    "BOOTSTRAP_CONSISTENCY_MISMATCH",
                    "SQLite mapping is missing or points to a different Google event",
                )
            if link.get("status") != "synced" or link.get("last_synced_hash") != item.source_hash:
                raise BootstrapError(
                    "BOOTSTRAP_CONSISTENCY_MISMATCH",
                    "SQLite mapping hash/status is not committed",
                )


BootstrapService = BootstrapRunner


async def run_bootstrap(**kwargs: Any) -> BootstrapResult:
    return await BootstrapRunner(**kwargs).run()


async def bootstrap(**kwargs: Any) -> BootstrapResult:
    return await run_bootstrap(**kwargs)
