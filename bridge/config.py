from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomllib


class ConfigError(RuntimeError):
    """Raised when bridge configuration is missing or invalid."""


@dataclass(frozen=True)
class SecretBundle:
    timetree_email: str | None
    timetree_password: str | None
    google_service_account_file: str | None

    def values(self) -> tuple[str, ...]:
        return tuple(
            value
            for value in (
                self.timetree_email,
                self.timetree_password,
                self.google_service_account_file,
            )
            if value
        )

    def mcp_env(self) -> dict[str, str]:
        env: dict[str, str] = {}
        if self.timetree_email:
            env["TIMETREE_EMAIL"] = self.timetree_email
        if self.timetree_password:
            env["TIMETREE_PASSWORD"] = self.timetree_password
        return env


@dataclass(frozen=True)
class BridgeConfig:
    config_path: Path
    project_root: Path
    version: str
    default_timezone: str
    timetree_calendar_id: str
    timetree_incremental_interval_seconds: int
    timetree_overlap_seconds: int
    google_calendar_id: str
    google_incremental_interval_seconds: int
    reconcile_interval_seconds: int
    verify_interval_seconds: int
    exporter_calendar_code: str
    database_path: Path
    log_path: Path


def _required(data: dict[str, Any], section: str, key: str) -> Any:
    section_value = data.get(section)
    if not isinstance(section_value, dict):
        raise ConfigError(f"Missing configuration section: [{section}]")
    if key not in section_value:
        raise ConfigError(f"Missing configuration key: [{section}].{key}")
    value = section_value[key]
    if value is None or value == "":
        raise ConfigError(f"Empty configuration key: [{section}].{key}")
    return value


def _as_positive_int(value: Any, name: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"Configuration key must be an integer: {name}")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Configuration key must be an integer: {name}") from exc
    minimum = 0 if allow_zero else 1
    if parsed < minimum:
        qualifier = "non-negative" if allow_zero else "positive"
        raise ConfigError(f"Configuration key must be {qualifier}: {name}")
    return parsed


def _resolve_project_path(project_root: Path, raw: Any, name: str) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise ConfigError(f"Configuration path must be a non-empty string: {name}")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = project_root / path
    return path.resolve()


def load_config(path: str | Path = "config/bridge.toml") -> BridgeConfig:
    config_path = Path(path).expanduser().resolve()
    if not config_path.is_file():
        raise ConfigError(f"Configuration file not found: {config_path}")

    try:
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML configuration: {config_path}") from exc

    project_root = (
        config_path.parent.parent
        if config_path.parent.name == "config"
        else config_path.parent
    )

    return BridgeConfig(
        config_path=config_path,
        project_root=project_root,
        version=str(_required(data, "bridge", "version")),
        default_timezone=str(_required(data, "bridge", "default_timezone")),
        timetree_calendar_id=str(_required(data, "timetree", "calendar_id")),
        timetree_incremental_interval_seconds=_as_positive_int(
            _required(data, "timetree", "incremental_interval_seconds"),
            "[timetree].incremental_interval_seconds",
        ),
        timetree_overlap_seconds=_as_positive_int(
            _required(data, "timetree", "overlap_seconds"),
            "[timetree].overlap_seconds",
            allow_zero=True,
        ),
        google_calendar_id=str(_required(data, "google", "calendar_id")),
        google_incremental_interval_seconds=_as_positive_int(
            _required(data, "google", "incremental_interval_seconds"),
            "[google].incremental_interval_seconds",
        ),
        reconcile_interval_seconds=_as_positive_int(
            _required(data, "reconcile", "interval_seconds"),
            "[reconcile].interval_seconds",
        ),
        verify_interval_seconds=_as_positive_int(
            _required(data, "verify", "interval_seconds"),
            "[verify].interval_seconds",
        ),
        exporter_calendar_code=str(_required(data, "exporter", "calendar_code")),
        database_path=_resolve_project_path(
            project_root,
            _required(data, "state", "database"),
            "[state].database",
        ),
        log_path=_resolve_project_path(
            project_root,
            _required(data, "logging", "path"),
            "[logging].path",
        ),
    )


def load_secrets(*, required: bool = False) -> SecretBundle:
    google_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") or os.getenv(
        "GOOGLE_SERVICE_ACCOUNT_FILE"
    )
    bundle = SecretBundle(
        timetree_email=os.getenv("TIMETREE_EMAIL"),
        timetree_password=os.getenv("TIMETREE_PASSWORD"),
        google_service_account_file=google_file,
    )

    if required:
        missing: list[str] = []
        if not bundle.timetree_email:
            missing.append("TIMETREE_EMAIL")
        if not bundle.timetree_password:
            missing.append("TIMETREE_PASSWORD")
        if not bundle.google_service_account_file:
            missing.append(
                "GOOGLE_SERVICE_ACCOUNT_JSON (or GOOGLE_SERVICE_ACCOUNT_FILE)"
            )
        if missing:
            raise ConfigError(
                "Missing required secret environment variables: " + ", ".join(missing)
            )

    return bundle


def secret_presence() -> dict[str, bool]:
    return {
        "TIMETREE_EMAIL": bool(os.getenv("TIMETREE_EMAIL")),
        "TIMETREE_PASSWORD": bool(os.getenv("TIMETREE_PASSWORD")),
        "GOOGLE_SERVICE_ACCOUNT_FILE": bool(
            os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
            or os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
        ),
    }
