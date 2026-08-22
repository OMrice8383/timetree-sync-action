from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bridge.cli import main, run_doctor, run_status
from bridge.config import ConfigError, load_config, load_secrets
from bridge.db import CORE_TABLES, core_tables, ensure_database, schema_version
from bridge.logging import REDACTED, JsonlLogger, redact


def write_config(root: Path, *, omit_google_calendar: bool = False) -> Path:
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)

    lines = [
        "[bridge]",
        'version = "0.1"',
        'default_timezone = "Asia/Tokyo"',
        "",
        "[timetree]",
        'calendar_id = "101"',
        "incremental_interval_seconds = 300",
        "overlap_seconds = 30",
        "",
        "[google]",
    ]
    if not omit_google_calendar:
        lines.append('calendar_id = "google-calendar"')
    lines.extend(
        [
            "incremental_interval_seconds = 60",
            "",
            "[reconcile]",
            "interval_seconds = 3600",
            "",
            "[verify]",
            "interval_seconds = 86400",
            "",
            "[exporter]",
            'calendar_code = "calendar-code"',
            "",
            "[state]",
            'database = "state/test.db"',
            "",
            "[logging]",
            'path = "logs/test.jsonl"',
            "",
        ]
    )

    path = config_dir / "bridge.toml"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


class FoundationTests(unittest.TestCase):
    def test_config_load_resolves_project_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = load_config(write_config(root))

            self.assertEqual(config.timetree_calendar_id, "101")
            self.assertEqual(config.google_calendar_id, "google-calendar")
            self.assertEqual(config.exporter_calendar_code, "calendar-code")
            self.assertEqual(config.database_path, (root / "state/test.db").resolve())
            self.assertEqual(config.log_path, (root / "logs/test.jsonl").resolve())

    def test_missing_config_key_fails_safely(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(ConfigError, r"\[google\]\.calendar_id"):
                load_config(write_config(root, omit_google_calendar=True))

    def test_secret_loader_and_recursive_redaction(self) -> None:
        env = {
            "TIMETREE_EMAIL": "secret-email@example.invalid",
            "TIMETREE_PASSWORD": "secret-password-value",
            "GOOGLE_SERVICE_ACCOUNT_JSON": r"C:\secret\service-account.json",
        }
        with patch.dict(os.environ, env, clear=True):
            secrets = load_secrets(required=True)
            payload = {
                "email": secrets.timetree_email,
                "password": secrets.timetree_password,
                "nested": {
                    "message": "path=" + str(secrets.google_service_account_file),
                    "access_token": "not-even-allowed",
                },
            }
            safe = redact(payload, secret_values=secrets.values())
            rendered = json.dumps(safe)

            for secret in secrets.values():
                self.assertNotIn(secret, rendered)
            self.assertIn(REDACTED, rendered)

    def test_jsonl_logger_never_writes_secret_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "bridge.jsonl"
            secret = "top-secret-value"
            logger = JsonlLogger(log_path, secret_values=[secret])
            logger.log(
                "info",
                "test",
                message=f"value={secret}",
                password="another-secret",
            )

            text = log_path.read_text(encoding="utf-8")
            self.assertNotIn(secret, text)
            self.assertNotIn("another-secret", text)
            self.assertIn(REDACTED, text)
            json.loads(text)

    def test_migration_is_idempotent_and_creates_four_core_tables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "calendar.db"

            with ensure_database(db_path) as connection:
                first_tables = core_tables(connection)
                first_version = schema_version(connection)

            with ensure_database(db_path) as connection:
                second_tables = core_tables(connection)
                second_version = schema_version(connection)

            self.assertEqual(set(first_tables), set(CORE_TABLES))
            self.assertEqual(set(second_tables), set(CORE_TABLES))
            self.assertEqual(first_version, 1)
            self.assertEqual(second_version, 1)

            with contextlib.closing(sqlite3.connect(db_path)) as connection:
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(event_links)")
                }
            self.assertIn("last_synced_hash", columns)
            self.assertNotIn("last_timetree_hash", columns)
            self.assertNotIn("last_google_hash", columns)

    def test_doctor_and_status_work_without_external_services(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)

            with patch.dict(
                os.environ,
                {
                    "TIMETREE_EMAIL": "fixture@example.invalid",
                    "TIMETREE_PASSWORD": "fixture-password",
                    "GOOGLE_SERVICE_ACCOUNT_FILE": "fixture-service-account.json",
                },
            ):
                doctor = run_doctor(config_path, dry_run=True)
            status = run_status(config_path, dry_run=True)

            self.assertTrue(doctor["ok"])
            self.assertFalse(doctor["external_services_checked"])
            self.assertEqual(
                set(doctor["database"]["core_tables"]),
                set(CORE_TABLES),
            )
            self.assertTrue(status["ok"])
            self.assertFalse(status["external_services_checked"])
            self.assertEqual(
                status["database"]["row_counts"],
                {name: 0 for name in CORE_TABLES},
            )

    def test_cli_json_and_tick_sync_are_safe_skeletons(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = write_config(root)

            output = io.StringIO()

            async def fake_external_doctor(**kwargs):
                return {
                    "external_services_checked": True,
                    "timetree": {
                        "connected": True,
                        "target_calendar_found": True,
                        "labels_resolved": True,
                    },
                    "google": {
                        "connected": True,
                        "target_calendar_found": True,
                        "full_sync_success": True,
                        "event_read": True,
                        "access_role": "writer",
                        "writer_permission": True,
                        "next_sync_token_observed": True,
                    },
                    "ok": True,
                    "reasons": [],
                }

            with (
                patch.dict(
                    os.environ,
                    {
                        "TIMETREE_EMAIL": "fixture@example.invalid",
                        "TIMETREE_PASSWORD": "fixture-password",
                        "GOOGLE_SERVICE_ACCOUNT_FILE": "fixture-service-account.json",
                    },
                ),
                patch("bridge.cli._run_external_doctor", fake_external_doctor),
                contextlib.redirect_stdout(output),
            ):
                code = main(
                    [
                        "doctor",
                        "--config",
                        str(config_path),
                        "--json",
                        "--dry-run",
                    ]
                )
            result = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertTrue(result["ok"])
            self.assertTrue(result["dry_run"])

            for command in ("tick", "sync"):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = main(
                        [
                            command,
                            "--config",
                            str(config_path),
                            "--json",
                            "--dry-run",
                        ]
                    )
                result = json.loads(output.getvalue())
                self.assertEqual(code, 2)
                self.assertEqual(result["error"], "NOT_IMPLEMENTED_P2_FOUNDATION")


if __name__ == "__main__":
    unittest.main()
