from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .config import ConfigError, load_config, load_secrets, secret_presence
from .db import (
    CORE_TABLES,
    core_tables,
    ensure_database,
    schema_version,
    sync_state_presence,
    table_counts,
)
from .google_client import GoogleCalendarClient
from .lock import default_lock_path, inspect_lock
from .p8_gate import (
    database_preflight,
    run_external_doctor,
    run_read_only_bootstrap_gate,
)
from .repository import StateRepository
from .timetree_client import TimeTreeMCPClient

_NOT_IMPLEMENTED = "NOT_IMPLEMENTED_P2_FOUNDATION"


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        default="config/bridge.toml",
        help="Path to bridge.toml",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Emit machine-readable JSON",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="For write-capable commands, prohibit remote writes",
    )
    parser.add_argument(
        "--mcp-entrypoint",
        default=None,
        help="Path to the TimeTree-MCP dist/index.js entrypoint",
    )
    parser.add_argument(
        "--node-command",
        default="node",
        help="Node executable used for TimeTree-MCP",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m bridge")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("doctor", "status", "tick", "sync", "bootstrap"):
        subparser = subparsers.add_parser(command)
        _add_common_options(subparser)
    return parser


def _default_mcp_entrypoint(project_root: Path) -> Path:
    return project_root.parent / "TimeTree-MCP" / "dist" / "index.js"


class _UnavailableGoogleReadClient:
    def __init__(self, calendar_id: str) -> None:
        self.calendar_id = calendar_id

    def get_calendar_metadata(self) -> Any:
        raise RuntimeError("Google credentials are unavailable")

    def list_changes(self) -> Any:
        raise RuntimeError("Google credentials are unavailable")


async def _run_external_doctor(
    *,
    config: Any,
    mcp_entrypoint: str | Path | None,
    node_command: str,
    google_client: Any | None,
    timetree_client: Any | None,
) -> dict[str, Any]:
    secrets = load_secrets(required=False)
    missing: list[str] = []
    if google_client is None and not secrets.google_service_account_file:
        missing.append("GOOGLE_CREDENTIALS_MISSING")
    if timetree_client is None and (
        not secrets.timetree_email or not secrets.timetree_password
    ):
        missing.append("TIMETREE_CREDENTIALS_MISSING")

    if google_client is None and secrets.google_service_account_file:
        google_client = GoogleCalendarClient.from_service_account_file(
            secrets.google_service_account_file,
            calendar_id=config.google_calendar_id,
            default_timezone=config.default_timezone,
        )

    if timetree_client is not None:
        result = await run_external_doctor(
            google_client=google_client,
            timetree_client=timetree_client,
        )
        result["reasons"] = list(dict.fromkeys([*missing, *result["reasons"]]))
        result["ok"] = not result["reasons"]
        return result

    if not secrets.timetree_email or not secrets.timetree_password:
        result = await run_external_doctor(
            google_client=google_client,
            timetree_client=None,
        )
        result["reasons"] = list(dict.fromkeys([*missing, *result["reasons"]]))
        result["ok"] = not result["reasons"]
        return result

    entrypoint = (
        Path(mcp_entrypoint)
        if mcp_entrypoint
        else _default_mcp_entrypoint(config.project_root)
    )
    async with TimeTreeMCPClient.connect(
        mcp_entrypoint=entrypoint,
        calendar_id=config.timetree_calendar_id,
        default_timezone=config.default_timezone,
        env=secrets.mcp_env(),
        node_command=node_command,
    ) as connected_timetree:
        result = await run_external_doctor(
            google_client=google_client,
            timetree_client=connected_timetree,
        )
        result["reasons"] = list(dict.fromkeys([*missing, *result["reasons"]]))
        result["ok"] = not result["reasons"]
        return result


def run_doctor(
    config_path: str | Path,
    *,
    dry_run: bool = False,
    external: bool = False,
    mcp_entrypoint: str | Path | None = None,
    node_command: str = "node",
    google_client: Any | None = None,
    timetree_client: Any | None = None,
) -> dict[str, Any]:
    config = load_config(config_path)
    with ensure_database(config.database_path) as connection:
        tables = core_tables(connection)
        lock = inspect_lock(default_lock_path(config.project_root))
        presence = secret_presence()
        required_checks = {
            "config": True,
            "sqlite": set(tables) == set(CORE_TABLES),
            "timetree_credentials": bool(
                presence["TIMETREE_EMAIL"] and presence["TIMETREE_PASSWORD"]
            )
            or timetree_client is not None,
            "google_credentials": presence["GOOGLE_SERVICE_ACCOUNT_FILE"]
            or google_client is not None,
        }
        result: dict[str, Any] = {
            "ok": all(required_checks.values()),
            "command": "doctor",
            "external_services_checked": external,
            "dry_run": dry_run,
            "config": {
                "path": str(config.config_path),
                "version": config.version,
                "default_timezone": config.default_timezone,
            },
            "database": {
                "path": str(config.database_path),
                "schema_version": schema_version(connection),
                "core_tables": list(tables),
            },
            "run_lock": lock,
            "secret_environment": {
                key: "set" if present else "missing"
                for key, present in presence.items()
            },
            "required_checks": required_checks,
        }
        if not external:
            return result

        try:
            external_result = asyncio.run(
                _run_external_doctor(
                    config=config,
                    mcp_entrypoint=mcp_entrypoint,
                    node_command=node_command,
                    google_client=google_client,
                    timetree_client=timetree_client,
                )
            )
        except Exception as exc:  # noqa: BLE001 - diagnostic boundary
            external_result = {
                "external_services_checked": True,
                "ok": False,
                "reasons": [f"EXTERNAL_DOCTOR_{type(exc).__name__.upper()}"],
            }
        result["external"] = external_result
        result["ok"] = bool(result["ok"] and external_result["ok"])
        return result


def _bootstrap_failure_result(
    repository: StateRepository,
    reasons: Sequence[str],
) -> dict[str, Any]:
    database = database_preflight(repository)
    return {
        "ok": False,
        "command": "bootstrap",
        "dry_run": True,
        "remote_writes": 0,
        "google": {
            "connected": False,
            "access_role": None,
            "live_event_count": 0,
            "tombstone_count": 0,
            "managed_count": 0,
            "unmanaged_count": 0,
            "next_sync_token_observed": False,
        },
        "timetree": {
            "connected": False,
            "target_calendar_found": False,
            "labels_resolved": False,
            "raw_event_count": 0,
            "eligible_count": 0,
            "ignored_count": 0,
            "ignored_reasons": {
                "TIMETREE_BIRTHDAY": 0,
                "TIMETREE_MEMO": 0,
                "LABEL_OUT_OF_SCOPE": 0,
            },
            "unsupported_count": 0,
            "exception_evidence_count": 0,
            "unsupported_reasons": {},
        },
        "recurrence_diagnostics": {
            "unsupported_count": 0,
            "shapes": [],
        },
        "database": database,
        "gate": {
            "ready_for_live_bootstrap": False,
            "reasons": list(dict.fromkeys(reasons)),
        },
    }


async def _run_bootstrap_dry_run(
    *,
    config: Any,
    repository: StateRepository,
    mcp_entrypoint: str | Path | None,
    node_command: str,
    google_client: Any | None,
    timetree_client: Any | None,
) -> dict[str, Any]:
    secrets = load_secrets(required=False)
    missing: list[str] = []
    if google_client is None and not secrets.google_service_account_file:
        missing.append("GOOGLE_CREDENTIALS_MISSING")
    if timetree_client is None and (
        not secrets.timetree_email or not secrets.timetree_password
    ):
        missing.append("TIMETREE_CREDENTIALS_MISSING")
    if google_client is None and secrets.google_service_account_file:
        google_client = GoogleCalendarClient.from_service_account_file(
            secrets.google_service_account_file,
            calendar_id=config.google_calendar_id,
            default_timezone=config.default_timezone,
        )

    if google_client is None:
        google_client = _UnavailableGoogleReadClient(config.google_calendar_id)

    def add_missing(result: dict[str, Any]) -> dict[str, Any]:
        if missing:
            result["gate"]["reasons"] = list(
                dict.fromkeys([*missing, *result["gate"]["reasons"]])
            )
            result["gate"]["ready_for_live_bootstrap"] = False
            result["ok"] = False
        return result

    if timetree_client is not None:
        result = await run_read_only_bootstrap_gate(
            google_client=google_client,
            timetree_client=timetree_client,
            repository=repository,
            default_timezone=config.default_timezone,
            bridge_version=config.version,
        )
        return add_missing(result)

    if not secrets.timetree_email or not secrets.timetree_password:
        return _bootstrap_failure_result(repository, missing)

    entrypoint = (
        Path(mcp_entrypoint)
        if mcp_entrypoint
        else _default_mcp_entrypoint(config.project_root)
    )
    async with TimeTreeMCPClient.connect(
        mcp_entrypoint=entrypoint,
        calendar_id=config.timetree_calendar_id,
        default_timezone=config.default_timezone,
        env=secrets.mcp_env(),
        node_command=node_command,
    ) as connected_timetree:
        result = await run_read_only_bootstrap_gate(
            google_client=google_client,
            timetree_client=connected_timetree,
            repository=repository,
            default_timezone=config.default_timezone,
            bridge_version=config.version,
        )
        return add_missing(result)


def run_bootstrap_dry_run(
    config_path: str | Path,
    *,
    mcp_entrypoint: str | Path | None = None,
    node_command: str = "node",
    google_client: Any | None = None,
    timetree_client: Any | None = None,
) -> dict[str, Any]:
    config = load_config(config_path)
    with ensure_database(config.database_path) as connection:
        repository = StateRepository(connection)
        try:
            return asyncio.run(
                _run_bootstrap_dry_run(
                    config=config,
                    repository=repository,
                    mcp_entrypoint=mcp_entrypoint,
                    node_command=node_command,
                    google_client=google_client,
                    timetree_client=timetree_client,
                )
            )
        except Exception as exc:  # noqa: BLE001 - read-only gate boundary
            return _bootstrap_failure_result(
                repository,
                [f"BOOTSTRAP_DRY_RUN_{type(exc).__name__.upper()}"],
            )


def run_status(config_path: str | Path, *, dry_run: bool = False) -> dict[str, Any]:
    config = load_config(config_path)
    with ensure_database(config.database_path) as connection:
        return {
            "ok": True,
            "command": "status",
            "external_services_checked": False,
            "dry_run": dry_run,
            "database": {
                "path": str(config.database_path),
                "schema_version": schema_version(connection),
                "core_tables": list(core_tables(connection)),
                "row_counts": table_counts(connection),
            },
            "sync_state_present": sync_state_presence(connection),
            "run_lock": inspect_lock(default_lock_path(config.project_root)),
        }


def run_unimplemented(command: str, *, dry_run: bool) -> dict[str, Any]:
    return {
        "ok": False,
        "command": command,
        "dry_run": dry_run,
        "error": _NOT_IMPLEMENTED,
        "message": (
            "P2 Foundation exposes the CLI safely, but sync execution is not "
            "implemented yet."
        ),
    }


def _render_human(result: dict[str, Any]) -> str:
    if result.get("ok"):
        return f"{result.get('command', 'bridge')}: OK\n" + json.dumps(
            result,
            ensure_ascii=False,
            indent=2,
        )
    return json.dumps(result, ensure_ascii=False, indent=2)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        if args.command == "doctor":
            result = run_doctor(
                args.config,
                dry_run=args.dry_run,
                external=True,
                mcp_entrypoint=args.mcp_entrypoint,
                node_command=args.node_command,
            )
            exit_code = 0 if result["ok"] else 2
        elif args.command == "status":
            result = run_status(args.config, dry_run=args.dry_run)
            exit_code = 0 if result["ok"] else 2
        elif args.command == "bootstrap":
            if not args.dry_run:
                result = {
                    "ok": False,
                    "command": "bootstrap",
                    "dry_run": False,
                    "remote_writes": 0,
                    "error": "LIVE_BOOTSTRAP_WRITE_DISABLED_P8B",
                    "message": "P8-B only permits bootstrap --dry-run",
                }
            else:
                result = run_bootstrap_dry_run(
                    args.config,
                    mcp_entrypoint=args.mcp_entrypoint,
                    node_command=args.node_command,
                )
            exit_code = 0 if result["ok"] else 2
        else:
            result = run_unimplemented(args.command, dry_run=args.dry_run)
            exit_code = 2
    except (ConfigError, OSError) as exc:
        result = {
            "ok": False,
            "command": args.command,
            "error": type(exc).__name__,
            "message": str(exc),
        }
        exit_code = 2
    except Exception as exc:  # noqa: BLE001 - CLI must fail safely
        result = {
            "ok": False,
            "command": args.command,
            "error": type(exc).__name__,
            "message": "operation failed safely without exposing external payloads",
        }
        exit_code = 2

    if args.json_output:
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    else:
        print(_render_human(result))

    return exit_code
