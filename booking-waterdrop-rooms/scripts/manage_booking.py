from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import json
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Iterator, Sequence
from zoneinfo import ZoneInfo


APP_NAME = "booking-waterdrop-rooms"
LABEL = "com.codex.booking-waterdrop-rooms"
TZ = ZoneInfo("Asia/Shanghai")
TIME_RANGE_RE = re.compile(
    r"^\s*(\d{1,2}):(\d{2})\s*[-–—~]\s*(\d{1,2}):(\d{2})\s*$"
)
RunCommand = Callable[..., object]
LAUNCHCTL_TIMEOUT = 10
POLL_INTERVAL_SECONDS = 15
CANCEL_TRANSACTION_NAME = ".cancel-transaction.json"
CANCEL_TOMBSTONE_NAME = ".cancel-pending.json"
SCHEDULER_TRANSACTION_NAME = ".scheduler-transaction.json"
PLAN_ALLOWED_KEYS = frozenset(
    {
        "schema_version",
        "plan_id",
        "created_at",
        "execution_date",
        "target_date",
        "status",
        "slots",
    }
)
SLOT_ALLOWED_KEYS = frozenset(
    {
        "slot_id",
        "start",
        "end",
        "status",
        "room_id",
        "room_name",
        "event_id",
        "error",
    }
)
HISTORY_ALLOWED_KEYS = frozenset({*PLAN_ALLOWED_KEYS, "completed_at"})
SAFE_SLOT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
ROOM_ID_RE = re.compile(r"^omm_[A-Za-z0-9_-]+$")
WATERDROP_ROOM_RE = re.compile(r"^水滴大厦(?:-|$)")
SAFE_SLOT_ERRORS = frozenset(
    {
        "room no longer available",
        "temporary rate limit",
        "temporary Feishu or network error",
        "event creation result is uncertain",
        "non-retryable Feishu error",
        "Feishu operation failed",
        "no available room",
        "execution window missed",
    }
)
PENDING_UPDATE = {
    "status": "needs_approval",
    "error": "interactive approval is required for lark-cli update",
}


class ExistingPlanError(RuntimeError):
    pass


@dataclass(frozen=True)
class TimeRange:
    start: str
    end: str

    @classmethod
    def parse(cls, text: str) -> "TimeRange":
        match = TIME_RANGE_RE.match(text)
        if not match:
            raise ValueError(f"invalid time range: {text}")
        sh, sm, eh, em = (int(part) for part in match.groups())
        try:
            start_value = time(sh, sm)
            end_value = time(eh, em)
        except ValueError as exc:
            raise ValueError(f"invalid time range: {text}") from exc
        if start_value >= end_value:
            raise ValueError("range end must be later than start")
        return cls(
            start=start_value.strftime("%H:%M"),
            end=end_value.strftime("%H:%M"),
        )

    def start_minutes(self) -> int:
        hour, minute = (int(part) for part in self.start.split(":"))
        return hour * 60 + minute

    def end_minutes(self) -> int:
        hour, minute = (int(part) for part in self.end.split(":"))
        return hour * 60 + minute


def validate_ranges(values: Sequence[str]) -> tuple[TimeRange, TimeRange]:
    if len(values) != 2:
        raise ValueError("exactly two time ranges are required")
    parsed = tuple(TimeRange.parse(value) for value in values)
    first, second = sorted(parsed, key=lambda item: item.start_minutes())
    if first.end_minutes() > second.start_minutes():
        raise ValueError("time ranges overlap")
    return parsed


def calculate_dates(invoked_at: datetime) -> tuple[date, date]:
    local_date = invoked_at.astimezone(TZ).date()
    execution_date = local_date + timedelta(days=1)
    return execution_date, execution_date + timedelta(days=2)


def default_state_dir() -> Path:
    return (
        Path.home()
        / "Library"
        / "Application Support"
        / "Codex"
        / APP_NAME
    )


def plan_path(state_dir: Path) -> Path:
    return state_dir / "pending-plan.json"


def update_state_path(state_dir: Path) -> Path:
    return state_dir / "pending-update.json"


def _path_exists(path: Path) -> bool:
    return os.path.lexists(path)


def _ensure_safe_directory(
    path: Path,
    *,
    create: bool,
    mode: int = 0o700,
    label: str = "directory",
) -> None:
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=mode)
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        if create:
            raise
        return
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"unsafe {label}: symbolic link or non-directory")
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"unsafe {label}") from exc
    try:
        if create:
            os.fchmod(descriptor, mode)
    finally:
        os.close(descriptor)


def _ensure_safe_state_dir(state_dir: Path, *, create: bool) -> None:
    _ensure_safe_directory(
        state_dir,
        create=create,
        mode=0o700,
        label="state directory",
    )


def _read_json_no_follow(path: Path, *, label: str) -> object | None:
    if not _path_exists(path):
        return None
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"unsafe {label}: symbolic link")
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"unsafe {label}: non-regular file")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return None
        raise ValueError(f"unsafe {label}") from exc
    try:
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            try:
                return json.load(handle)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid {label}: malformed JSON") from exc
    except Exception:
        # fdopen owns the descriptor once constructed. If construction itself
        # failed, close the raw descriptor here.
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _canonical_uuid(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid plan id")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError("invalid plan id") from exc
    if str(parsed) != value:
        raise ValueError("invalid plan id")
    return value


def _valid_room(room_id: object, room_name: object) -> bool:
    return (
        isinstance(room_id, str)
        and ROOM_ID_RE.fullmatch(room_id) is not None
        and isinstance(room_name, str)
        and WATERDROP_ROOM_RE.match(room_name) is not None
    )


def _valid_slot_state(slot: dict, *, allow_missed: bool) -> bool:
    slot_status = slot["status"]
    room_id = slot["room_id"]
    room_name = slot["room_name"]
    event_id = slot["event_id"]
    error = slot["error"]
    if slot_status == "pending" and not allow_missed:
        return all(
            value is None
            for value in (room_id, room_name, event_id, error)
        )
    if slot_status == "success":
        return (
            _valid_room(room_id, room_name)
            and isinstance(event_id, str)
            and bool(event_id.strip())
            and error is None
        )
    if slot_status == "uncertain":
        return (
            _valid_room(room_id, room_name)
            and event_id is None
            and error in SAFE_SLOT_ERRORS
        )
    if slot_status == "failed":
        return (
            room_id is None
            and room_name is None
            and event_id is None
            and error in SAFE_SLOT_ERRORS
        )
    if allow_missed and slot_status == "missed":
        return (
            room_id is None
            and room_name is None
            and event_id is None
            and error == "execution window missed"
        )
    return False


def _validate_common_plan_fields(plan: dict) -> tuple[date, date, list[dict]]:
    _canonical_uuid(plan["plan_id"])
    if type(plan["schema_version"]) is not int or plan["schema_version"] != 1:
        raise ValueError
    created_at = datetime.fromisoformat(plan["created_at"])
    if created_at.tzinfo is None:
        raise ValueError
    execution = date.fromisoformat(plan["execution_date"])
    target = date.fromisoformat(plan["target_date"])
    if target != execution + timedelta(days=2):
        raise ValueError
    slots = plan["slots"]
    if not isinstance(slots, list) or len(slots) != 2:
        raise ValueError
    if any(
        not isinstance(slot, dict) or set(slot) != SLOT_ALLOWED_KEYS
        for slot in slots
    ):
        raise ValueError
    validate_ranges([f"{slot['start']}-{slot['end']}" for slot in slots])
    slot_ids = [slot["slot_id"] for slot in slots]
    if len(set(slot_ids)) != 2 or any(
        not isinstance(slot_id, str)
        or SAFE_SLOT_ID_RE.fullmatch(slot_id) is None
        for slot_id in slot_ids
    ):
        raise ValueError
    return execution, target, slots


def _validate_plan(plan: object) -> dict:
    try:
        if not isinstance(plan, dict) or set(plan) != PLAN_ALLOWED_KEYS:
            raise ValueError
        _, _, slots = _validate_common_plan_fields(plan)
        if plan["status"] != "pending":
            raise ValueError
        if any(
            not _valid_slot_state(slot, allow_missed=False)
            for slot in slots
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ValueError("invalid plan state") from exc
    return plan


def _validate_history(result: object) -> dict:
    try:
        if not isinstance(result, dict) or set(result) not in {
            PLAN_ALLOWED_KEYS,
            HISTORY_ALLOWED_KEYS,
        }:
            raise ValueError
        _canonical_uuid(result["plan_id"])
        status = result["status"]
        if status not in {"canceled", "success", "partial", "failed", "missed"}:
            raise ValueError
        if status == "canceled":
            if set(result) != PLAN_ALLOWED_KEYS:
                raise ValueError
            _validate_plan({**result, "status": "pending"})
            return result
        if set(result) != HISTORY_ALLOWED_KEYS:
            raise ValueError
        _, _, slots = _validate_common_plan_fields(result)
        completed_at = datetime.fromisoformat(result["completed_at"])
        if completed_at.tzinfo is None:
            raise ValueError
        if any(
            not _valid_slot_state(slot, allow_missed=True)
            for slot in slots
        ):
            raise ValueError
        statuses = [slot["status"] for slot in slots]
        successes = statuses.count("success")
        overall_valid = {
            "success": successes == 2,
            "partial": successes == 1,
            "missed": successes == 0 and set(statuses) == {"missed"},
            "failed": (
                successes == 0
                and set(statuses) != {"missed"}
                and set(statuses) <= {"failed", "uncertain", "missed"}
            ),
        }.get(status, False)
        if not overall_valid:
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ValueError("invalid history result") from exc
    return result


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_json(path: Path, payload: dict) -> None:
    _ensure_safe_directory(
        path.parent,
        create=True,
        mode=0o700,
        label="state subdirectory",
    )
    if _path_exists(path) and path.is_symlink():
        raise ValueError("unsafe JSON target: symbolic link")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _directory_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _verify_private_directory_fd(descriptor: int, label: str) -> None:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise ValueError(f"unsafe {label}")


@contextlib.contextmanager
def _opened_history_dir(
    state_dir: Path,
    *,
    create: bool,
) -> Iterator[int | None]:
    _ensure_safe_state_dir(state_dir, create=create)
    state_descriptor = None
    try:
        state_descriptor = os.open(state_dir, _directory_flags())
    except FileNotFoundError:
        if create:
            raise
        yield None
        return
    except OSError as exc:
        raise ValueError("unsafe state directory") from exc
    history_descriptor = None
    try:
        _verify_private_directory_fd(state_descriptor, "state directory")
        if create:
            try:
                os.mkdir("history", 0o700, dir_fd=state_descriptor)
            except FileExistsError:
                pass
        try:
            history_descriptor = os.open(
                "history",
                _directory_flags(),
                dir_fd=state_descriptor,
            )
        except FileNotFoundError:
            if create:
                raise
            yield None
            return
        except OSError as exc:
            raise ValueError("unsafe history directory") from exc
        _verify_private_directory_fd(
            history_descriptor,
            "history directory",
        )
        yield history_descriptor
    finally:
        if history_descriptor is not None:
            os.close(history_descriptor)
        if state_descriptor is not None:
            os.close(state_descriptor)


def _verify_private_regular_file(
    descriptor: int,
    label: str,
) -> os.stat_result:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise ValueError(f"unsafe {label}")
    return metadata


def _read_json_at(
    directory_descriptor: int,
    name: str,
    *,
    label: str,
) -> tuple[object, os.stat_result]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(
            name,
            flags,
            dir_fd=directory_descriptor,
        )
    except OSError as exc:
        raise ValueError(f"unsafe {label}") from exc
    try:
        metadata = _verify_private_regular_file(descriptor, label)
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            try:
                return json.load(handle), metadata
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid {label}: malformed JSON") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _atomic_write_json_at(
    directory_descriptor: int,
    name: str,
    payload: dict,
) -> None:
    try:
        existing = os.stat(
            name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        existing = None
    if existing is not None and (
        not stat.S_ISREG(existing.st_mode)
        or existing.st_uid != os.geteuid()
        or stat.S_IMODE(existing.st_mode) & 0o077
    ):
        raise ValueError("unsafe history result")
    temporary = f".{name}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(
        temporary,
        flags,
        0o600,
        dir_fd=directory_descriptor,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fchmod(handle.fileno(), 0o600)
            os.fsync(handle.fileno())
        os.replace(
            temporary,
            name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )
        os.fsync(directory_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory_descriptor)
        except FileNotFoundError:
            pass


def write_history(state_dir: Path, payload: dict) -> dict:
    result = _validate_history(payload)
    name = f"{_canonical_uuid(result['plan_id'])}.json"
    with _opened_history_dir(state_dir, create=True) as history_descriptor:
        if history_descriptor is None:
            raise AssertionError("history directory was not created")
        _atomic_write_json_at(history_descriptor, name, result)
    return result


@contextlib.contextmanager
def locked_state(state_dir: Path) -> Iterator[None]:
    _ensure_safe_state_dir(state_dir, create=True)
    lock_path = state_dir / "state.lock"
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise ValueError("unsafe state lock") from exc
    with os.fdopen(descriptor, "a+", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            _recover_cancel_transaction_unlocked(state_dir)
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_plan(state_dir: Path) -> dict | None:
    _ensure_safe_state_dir(state_dir, create=False)
    payload = _read_json_no_follow(plan_path(state_dir), label="plan")
    if payload is None:
        return None
    return _validate_plan(payload)


def latest_result(state_dir: Path) -> dict | None:
    with _opened_history_dir(state_dir, create=False) as history_descriptor:
        if history_descriptor is None:
            return None
        candidates = []
        for name in os.listdir(history_descriptor):
            if not name.endswith(".json"):
                continue
            stem = name[:-5]
            try:
                _canonical_uuid(stem)
            except ValueError as exc:
                raise ValueError("history path is not canonical") from exc
            payload, metadata = _read_json_at(
                history_descriptor,
                name,
                label="history result",
            )
            result = _validate_history(payload)
            if stem != result["plan_id"]:
                raise ValueError("history path does not match plan id")
            candidates.append((metadata.st_mtime_ns, result))
        if not candidates:
            return None
        return max(candidates, key=lambda item: item[0])[1]


def load_pending_update(state_dir: Path) -> dict | None:
    _ensure_safe_state_dir(state_dir, create=False)
    payload = _read_json_no_follow(
        update_state_path(state_dir),
        label="pending update",
    )
    if payload is None:
        return None
    if not isinstance(payload, dict) or payload != PENDING_UPDATE:
        raise ValueError("invalid pending update")
    return payload


def clear_pending_update(state_dir: Path) -> dict | None:
    with locked_state(state_dir):
        pending = load_pending_update(state_dir)
        update_state_path(state_dir).unlink(missing_ok=True)
        return pending


def create_plan(
    state_dir: Path,
    ranges: Sequence[TimeRange],
    invoked_at: datetime,
    replace: bool = False,
) -> dict:
    execution_date, target_date = calculate_dates(invoked_at)
    with locked_state(state_dir):
        existing = load_plan(state_dir)
        if existing and not replace:
            raise ExistingPlanError("a pending plan already exists")
        plan = {
            "schema_version": 1,
            "plan_id": str(uuid.uuid4()),
            "created_at": invoked_at.astimezone(TZ).isoformat(),
            "execution_date": execution_date.isoformat(),
            "target_date": target_date.isoformat(),
            "status": "pending",
            "slots": [
                {
                    "slot_id": f"slot-{index}",
                    "start": value.start,
                    "end": value.end,
                    "status": "pending",
                    "room_id": None,
                    "room_name": None,
                    "event_id": None,
                    "error": None,
                }
                for index, value in enumerate(ranges, start=1)
            ],
        }
        _atomic_write_json(plan_path(state_dir), plan)
        return plan


def _cancel_transaction_path(state_dir: Path) -> Path:
    return state_dir / CANCEL_TRANSACTION_NAME


def _cancel_tombstone_path(state_dir: Path) -> Path:
    return state_dir / CANCEL_TOMBSTONE_NAME


def _validate_cancel_transaction(payload: object) -> dict:
    if (
        not isinstance(payload, dict)
        or set(payload) != {"operation", "plan"}
        or payload.get("operation") != "cancel"
    ):
        raise ValueError("invalid cancellation transaction")
    plan = payload.get("plan")
    validated = _validate_history(plan)
    if validated["status"] != "canceled":
        raise ValueError("invalid cancellation transaction")
    return payload


def _recover_cancel_transaction_unlocked(state_dir: Path) -> None:
    marker_path = _cancel_transaction_path(state_dir)
    marker = _read_json_no_follow(
        marker_path,
        label="cancellation transaction",
    )
    if marker is None:
        if _path_exists(_cancel_tombstone_path(state_dir)):
            raise ValueError("orphaned cancellation tombstone")
        return
    transaction = _validate_cancel_transaction(marker)
    canceled = transaction["plan"]
    pending_path = plan_path(state_dir)
    tombstone_path = _cancel_tombstone_path(state_dir)
    if _path_exists(pending_path):
        pending = load_plan(state_dir)
        if pending is None or pending["plan_id"] != canceled["plan_id"]:
            raise ValueError("cancellation transaction plan mismatch")
        if _path_exists(tombstone_path):
            raise ValueError("duplicate cancellation tombstone")
        os.replace(pending_path, tombstone_path)
        _fsync_directory(state_dir)
    if _path_exists(tombstone_path):
        tombstone = _read_json_no_follow(
            tombstone_path,
            label="cancellation tombstone",
        )
        pending = _validate_plan(tombstone)
        if pending["plan_id"] != canceled["plan_id"]:
            raise ValueError("cancellation tombstone plan mismatch")
    write_history(state_dir, canceled)
    tombstone_path.unlink(missing_ok=True)
    marker_path.unlink(missing_ok=True)


def cancel_plan(state_dir: Path) -> dict | None:
    with locked_state(state_dir):
        plan = load_plan(state_dir)
        if not plan:
            return None
        canceled = {**plan, "status": "canceled"}
        _atomic_write_json(
            _cancel_transaction_path(state_dir),
            {"operation": "cancel", "plan": canceled},
        )
        os.replace(plan_path(state_dir), _cancel_tombstone_path(state_dir))
        _fsync_directory(state_dir)
        write_history(state_dir, canceled)
        _cancel_tombstone_path(state_dir).unlink(missing_ok=True)
        _cancel_transaction_path(state_dir).unlink(missing_ok=True)
        return canceled


def build_launch_agent(
    python_bin: Path,
    runtime_script: Path,
    state_dir: Path,
    lark_bin: Path,
) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": [
            str(python_bin),
            str(runtime_script),
            "--state-dir",
            str(state_dir),
        ],
        # launchd calendar intervals use the Mac's system timezone. Polling is
        # timezone-independent; the runner itself decides in Asia/Shanghai and
        # returns immediately when no plan is due.
        "StartInterval": POLL_INTERVAL_SECONDS,
        "ProcessType": "Background",
        "EnvironmentVariables": {
            "TZ": "Asia/Shanghai",
            "PYTHONUNBUFFERED": "1",
            "LARK_CLI_BIN": str(lark_bin),
        },
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
    }


def _write_plist(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            plistlib.dump(payload, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _launch_domain() -> str:
    return f"gui/{os.getuid()}"


def _launchctl(
    action: str,
    plist_path: Path,
    run_command: RunCommand,
) -> object:
    argv = ["launchctl", action, _launch_domain(), str(plist_path)]
    try:
        return run_command(
            argv,
            text=True,
            capture_output=True,
            timeout=LAUNCHCTL_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"launchctl {action} timed out") from exc
    except OSError as exc:
        raise RuntimeError(f"launchctl {action} could not be started") from exc


def _launchctl_error(result: object) -> str:
    stderr = str(getattr(result, "stderr", "") or "").strip()
    stdout = str(getattr(result, "stdout", "") or "").strip()
    return stderr or stdout or f"exit status {getattr(result, 'returncode', 1)}"


def _service_is_missing(result: object) -> bool:
    output = " ".join(
        str(getattr(result, name, "") or "")
        for name in ("stdout", "stderr")
    ).lower()
    return any(
        marker in output
        for marker in (
            "no such process",
            "could not find specified service",
            "could not find service",
            "service not found",
            "service is not loaded",
            "not loaded",
        )
    )


def _bootout(plist_path: Path, run_command: RunCommand) -> object:
    result = _launchctl("bootout", plist_path, run_command)
    if getattr(result, "returncode", 1) != 0 and not _service_is_missing(result):
        raise RuntimeError(f"launchctl bootout failed: {_launchctl_error(result)}")
    return result


def _bootstrap(plist_path: Path, run_command: RunCommand) -> None:
    result = _launchctl("bootstrap", plist_path, run_command)
    if getattr(result, "returncode", 1) != 0:
        raise RuntimeError(f"launchctl bootstrap failed: {_launchctl_error(result)}")


def _remove_path(path: Path) -> None:
    if not _path_exists(path):
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _scheduler_transaction_path(state_dir: Path) -> Path:
    return state_dir / SCHEDULER_TRANSACTION_NAME


def _scheduler_artifact_paths(
    state_dir: Path,
    plist_path: Path,
    token: str,
) -> tuple[Path, Path, Path, Path]:
    return (
        state_dir / f".runtime.{token}.new",
        state_dir / f".runtime.{token}.backup",
        plist_path.parent / f".{plist_path.name}.{token}.new",
        plist_path.parent / f".{plist_path.name}.{token}.backup",
    )


def _validate_scheduler_transaction(payload: object) -> dict:
    if not isinstance(payload, dict) or payload.get("operation") not in {
        "install",
        "uninstall",
    }:
        raise ValueError("invalid scheduler transaction")
    if payload["operation"] == "uninstall":
        if set(payload) != {"operation", "phase"} or payload["phase"] != "prepared":
            raise ValueError("invalid scheduler transaction")
        return payload
    if set(payload) != {
        "operation",
        "phase",
        "token",
        "old_runtime",
        "old_plist",
        "old_agent_was_loaded",
    }:
        raise ValueError("invalid scheduler transaction")
    if payload["phase"] not in {"prepared", "committed"}:
        raise ValueError("invalid scheduler transaction")
    if not isinstance(payload["token"], str) or re.fullmatch(
        r"[0-9a-f]{32}", payload["token"]
    ) is None:
        raise ValueError("invalid scheduler transaction")
    if type(payload["old_runtime"]) is not bool or type(payload["old_plist"]) is not bool:
        raise ValueError("invalid scheduler transaction")
    if payload["old_agent_was_loaded"] not in {None, True, False}:
        raise ValueError("invalid scheduler transaction")
    return payload


def _cleanup_scheduler_orphans(state_dir: Path, plist_path: Path) -> None:
    for pattern in (".runtime.*.new", ".runtime.*.backup"):
        for path in state_dir.glob(pattern):
            _remove_path(path)
    if plist_path.parent.exists():
        for suffix in ("new", "backup"):
            for path in plist_path.parent.glob(
                f".{plist_path.name}.*.{suffix}"
            ):
                _remove_path(path)


def _finish_uninstall_unlocked(
    state_dir: Path,
    plist_path: Path,
    run_command: RunCommand,
) -> None:
    _bootout(plist_path, run_command)
    _remove_path(plist_path)
    _remove_path(plan_path(state_dir))
    _remove_path(update_state_path(state_dir))
    _remove_path(state_dir / "runtime")


def _recover_scheduler_transaction_unlocked(
    state_dir: Path,
    plist_path: Path,
    run_command: RunCommand,
) -> None:
    marker_path = _scheduler_transaction_path(state_dir)
    payload = _read_json_no_follow(
        marker_path,
        label="scheduler transaction",
    )
    if payload is None:
        _cleanup_scheduler_orphans(state_dir, plist_path)
        return
    transaction = _validate_scheduler_transaction(payload)
    if transaction["operation"] == "uninstall":
        _finish_uninstall_unlocked(state_dir, plist_path, run_command)
        marker_path.unlink(missing_ok=True)
        _cleanup_scheduler_orphans(state_dir, plist_path)
        return

    runtime_dir = state_dir / "runtime"
    staged_runtime, backup_runtime, staged_plist, backup_plist = (
        _scheduler_artifact_paths(
            state_dir,
            plist_path,
            transaction["token"],
        )
    )
    # Always establish a known unloaded baseline before choosing old or new.
    # If the previous process died inside bootout, repeating it is safe.
    _bootout(plist_path, run_command)
    if transaction["phase"] == "committed":
        if not _path_exists(runtime_dir) or not _path_exists(plist_path):
            raise RuntimeError("committed scheduler transaction is incomplete")
        _bootstrap(plist_path, run_command)
        for path in (
            staged_runtime,
            backup_runtime,
            staged_plist,
            backup_plist,
        ):
            _remove_path(path)
        marker_path.unlink(missing_ok=True)
        _cleanup_scheduler_orphans(state_dir, plist_path)
        return

    if _path_exists(backup_runtime):
        _remove_path(runtime_dir)
        os.replace(backup_runtime, runtime_dir)
    elif not transaction["old_runtime"]:
        _remove_path(runtime_dir)
    elif not _path_exists(runtime_dir):
        raise RuntimeError("previous runtime cannot be recovered")

    if _path_exists(backup_plist):
        _remove_path(plist_path)
        os.replace(backup_plist, plist_path)
    elif not transaction["old_plist"]:
        _remove_path(plist_path)
    elif not _path_exists(plist_path):
        raise RuntimeError("previous plist cannot be recovered")

    _remove_path(staged_runtime)
    _remove_path(staged_plist)
    if (
        transaction["old_plist"]
        and transaction["old_agent_was_loaded"] is not False
    ):
        _bootstrap(plist_path, run_command)
    marker_path.unlink(missing_ok=True)
    _cleanup_scheduler_orphans(state_dir, plist_path)


def _install_scheduler_unlocked(
    source_dir: Path,
    state_dir: Path,
    plist_path: Path,
    python_bin: Path,
    lark_bin: Path,
    run_command: RunCommand,
) -> dict:
    runtime_dir = state_dir / "runtime"
    _ensure_safe_directory(
        state_dir / "logs",
        create=True,
        mode=0o700,
        label="log directory",
    )
    token = uuid.uuid4().hex
    staged_runtime, backup_runtime, staged_plist, backup_plist = (
        _scheduler_artifact_paths(state_dir, plist_path, token)
    )
    staged_runtime.mkdir(mode=0o700)
    os.chmod(staged_runtime, 0o700)
    try:
        for name in ("manage_booking.py", "run_booking.py"):
            shutil.copy2(source_dir / name, staged_runtime / name)
            os.chmod(staged_runtime / name, 0o700)
        payload = build_launch_agent(
            python_bin,
            runtime_dir / "run_booking.py",
            state_dir,
            lark_bin,
        )
        _write_plist(staged_plist, payload)
    except Exception:
        _remove_path(staged_runtime)
        _remove_path(staged_plist)
        raise

    transaction = {
        "operation": "install",
        "phase": "prepared",
        "token": token,
        "old_runtime": _path_exists(runtime_dir),
        "old_plist": _path_exists(plist_path),
        "old_agent_was_loaded": None,
    }
    _atomic_write_json(_scheduler_transaction_path(state_dir), transaction)
    try:
        bootout_result = _bootout(plist_path, run_command)
        transaction["old_agent_was_loaded"] = (
            getattr(bootout_result, "returncode", 1) == 0
        )
        _atomic_write_json(_scheduler_transaction_path(state_dir), transaction)
        if transaction["old_runtime"]:
            os.replace(runtime_dir, backup_runtime)
        if transaction["old_plist"]:
            os.replace(plist_path, backup_plist)
        os.replace(staged_runtime, runtime_dir)
        os.replace(staged_plist, plist_path)
        _bootstrap(plist_path, run_command)
        transaction["phase"] = "committed"
        _atomic_write_json(_scheduler_transaction_path(state_dir), transaction)
    except Exception as install_error:
        try:
            _recover_scheduler_transaction_unlocked(
                state_dir,
                plist_path,
                run_command,
            )
        except Exception as recovery_error:
            raise RuntimeError(
                f"{install_error}; cleanup bootout failed: {recovery_error}; "
                "live files and recovery artifacts were preserved"
            ) from install_error
        raise
    for path in (
        backup_runtime,
        backup_plist,
        staged_runtime,
        staged_plist,
    ):
        _remove_path(path)
    _scheduler_transaction_path(state_dir).unlink(missing_ok=True)
    _cleanup_scheduler_orphans(state_dir, plist_path)
    return {"status": "installed", "plist": str(plist_path)}


def install_scheduler(
    source_dir: Path,
    state_dir: Path,
    plist_path: Path,
    python_bin: Path,
    lark_bin: Path,
    run_command: RunCommand = subprocess.run,
) -> dict:
    with locked_state(state_dir):
        _recover_scheduler_transaction_unlocked(
            state_dir,
            plist_path,
            run_command,
        )
        return _install_scheduler_unlocked(
            source_dir,
            state_dir,
            plist_path,
            python_bin,
            lark_bin,
            run_command,
        )


def _uninstall_scheduler_unlocked(
    state_dir: Path,
    plist_path: Path,
    run_command: RunCommand,
) -> dict:
    _atomic_write_json(
        _scheduler_transaction_path(state_dir),
        {"operation": "uninstall", "phase": "prepared"},
    )
    _finish_uninstall_unlocked(state_dir, plist_path, run_command)
    _scheduler_transaction_path(state_dir).unlink(missing_ok=True)
    _cleanup_scheduler_orphans(state_dir, plist_path)
    return {"status": "uninstalled", "history_preserved": True}


def uninstall_scheduler(
    state_dir: Path,
    plist_path: Path,
    run_command: RunCommand = subprocess.run,
) -> dict:
    with locked_state(state_dir):
        _recover_scheduler_transaction_unlocked(
            state_dir,
            plist_path,
            run_command,
        )
        return _uninstall_scheduler_unlocked(
            state_dir,
            plist_path,
            run_command,
        )


def _json_print(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def status_state(state_dir: Path) -> dict:
    with locked_state(state_dir):
        return {
            "pending": load_plan(state_dir),
            "latest_result": latest_result(state_dir),
            "pending_update": load_pending_update(state_dir),
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=APP_NAME)
    parser.add_argument("--state-dir", type=Path, default=default_state_dir())
    subcommands = parser.add_subparsers(dest="command", required=True)

    subcommands.add_parser("status")
    subcommands.add_parser("cancel")
    subcommands.add_parser("clear-update")
    subcommands.add_parser("uninstall")
    subcommands.add_parser("install")

    create = subcommands.add_parser("create")
    create.add_argument("--slot", action="append", required=True)
    create.add_argument("--replace", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    state_dir = args.state_dir
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    source_dir = Path(__file__).resolve().parent
    if args.command == "status":
        _json_print(status_state(state_dir))
        return 0
    if args.command == "cancel":
        _json_print({"canceled": cancel_plan(state_dir)})
        return 0
    if args.command == "clear-update":
        _json_print({"cleared_update": clear_pending_update(state_dir)})
        return 0
    if args.command == "create":
        ranges = validate_ranges(args.slot)
        _json_print(
            create_plan(
                state_dir,
                ranges,
                datetime.now(TZ),
                replace=args.replace,
            )
        )
        return 0
    if args.command == "install":
        python_bin = Path(sys.executable).resolve()
        lark_path = shutil.which("lark-cli")
        if not lark_path:
            raise RuntimeError("lark-cli is required")
        _json_print(
            install_scheduler(
                source_dir, state_dir, plist_path, python_bin, Path(lark_path)
            )
        )
        return 0
    if args.command == "uninstall":
        _json_print(uninstall_scheduler(state_dir, plist_path))
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
