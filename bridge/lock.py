from __future__ import annotations

import ctypes
import json
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_LOCK_RELATIVE_PATH = Path("state/bridge.lock")


class RunLockError(RuntimeError):
    """Base error for application-level run locking."""


class RunLockHeldError(RunLockError):
    """Raised when another live process owns the bridge run lock."""


class RunLockCorruptError(RunLockError):
    """Raised when an existing lock cannot be interpreted safely."""


def default_lock_path(project_root: str | Path) -> Path:
    return Path(project_root) / DEFAULT_LOCK_RELATIVE_PATH


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def process_exists(pid: int) -> bool:
    if pid <= 0:
        return False

    if os.name == "nt":
        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            pid,
        )
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_lock(path: str | Path) -> dict[str, Any] | None:
    lock_path = Path(path)
    if not lock_path.exists():
        return None

    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunLockCorruptError(
            f"Run lock is unreadable or invalid JSON: {lock_path}"
        ) from exc

    pid = payload.get("pid")
    started_at = payload.get("started_at")
    if not isinstance(pid, int) or pid <= 0 or not isinstance(started_at, str):
        raise RunLockCorruptError(
            f"Run lock is missing valid pid/started_at: {lock_path}"
        )

    return {"pid": pid, "started_at": started_at}


def inspect_lock(
    path: str | Path,
    *,
    process_checker: Callable[[int], bool] = process_exists,
) -> dict[str, Any]:
    payload = read_lock(path)
    if payload is None:
        return {
            "exists": False,
            "held_by_live_process": False,
            "stale": False,
            "pid": None,
            "started_at": None,
        }

    live = process_checker(payload["pid"])
    return {
        "exists": True,
        "held_by_live_process": live,
        "stale": not live,
        "pid": payload["pid"],
        "started_at": payload["started_at"],
    }


def acquire_lock(
    path: str | Path,
    *,
    pid: int | None = None,
    started_at: str | None = None,
    process_checker: Callable[[int], bool] = process_exists,
) -> dict[str, Any]:
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    owner = {
        "pid": os.getpid() if pid is None else pid,
        "started_at": _utc_now() if started_at is None else started_at,
    }

    for _ in range(3):
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            existing = read_lock(lock_path)
            assert existing is not None

            if process_checker(existing["pid"]):
                raise RunLockHeldError(
                    "Bridge run lock is held by live process "
                    f"{existing['pid']} since {existing['started_at']}"
                )

            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
            continue

        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(owner, handle, ensure_ascii=False, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
            raise

        return owner

    raise RunLockError(
        f"Could not acquire run lock after stale-lock recovery: {lock_path}"
    )


def release_lock(
    path: str | Path,
    *,
    expected_pid: int | None = None,
    expected_started_at: str | None = None,
) -> None:
    lock_path = Path(path)
    payload = read_lock(lock_path)
    if payload is None:
        return

    pid = os.getpid() if expected_pid is None else expected_pid
    if payload["pid"] != pid:
        raise RunLockError(
            f"Refusing to release run lock owned by PID {payload['pid']}"
        )
    if (
        expected_started_at is not None
        and payload["started_at"] != expected_started_at
    ):
        raise RunLockError(
            "Refusing to release run lock because started_at no longer matches"
        )

    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass


@contextmanager
def run_lock(
    path: str | Path,
    *,
    process_checker: Callable[[int], bool] = process_exists,
) -> Iterator[dict[str, Any]]:
    owner = acquire_lock(path, process_checker=process_checker)
    try:
        yield owner
    finally:
        release_lock(
            path,
            expected_pid=owner["pid"],
            expected_started_at=owner["started_at"],
        )
