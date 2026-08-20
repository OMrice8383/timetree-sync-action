from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .config import ConfigError, load_config, secret_presence
from .db import (
    CORE_TABLES,
    core_tables,
    ensure_database,
    schema_version,
    sync_state_presence,
    table_counts,
)
from .lock import default_lock_path, inspect_lock

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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m bridge")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("doctor", "status", "tick", "sync"):
        subparser = subparsers.add_parser(command)
        _add_common_options(subparser)
    return parser


def run_doctor(config_path: str | Path, *, dry_run: bool = False) -> dict[str, Any]:
    config = load_config(config_path)
    with ensure_database(config.database_path) as connection:
        tables = core_tables(connection)
        lock = inspect_lock(default_lock_path(config.project_root))
        return {
            "ok": set(tables) == set(CORE_TABLES),
            "command": "doctor",
            "external_services_checked": False,
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
                for key, present in secret_presence().items()
            },
        }


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
            result = run_doctor(args.config, dry_run=args.dry_run)
            exit_code = 0 if result["ok"] else 2
        elif args.command == "status":
            result = run_status(args.config, dry_run=args.dry_run)
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

    if args.json_output:
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    else:
        print(_render_human(result))

    return exit_code
