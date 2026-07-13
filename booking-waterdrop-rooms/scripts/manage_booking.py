from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import plistlib
import re
import shutil
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


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextlib.contextmanager
def locked_state(state_dir: Path) -> Iterator[None]:
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state_dir, 0o700)
    lock_path = state_dir / "state.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        os.chmod(lock_path, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_plan(state_dir: Path) -> dict | None:
    path = plan_path(state_dir)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def latest_result(state_dir: Path) -> dict | None:
    history_dir = state_dir / "history"
    if not history_dir.exists():
        return None
    candidates = [
        path for path in history_dir.glob("*.json")
        if path.is_file() and not path.is_symlink()
    ]
    if not candidates:
        return None
    newest = max(candidates, key=lambda path: path.stat().st_mtime_ns)
    return json.loads(newest.read_text(encoding="utf-8"))


def load_pending_update(state_dir: Path) -> dict | None:
    path = update_state_path(state_dir)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


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


def cancel_plan(state_dir: Path) -> dict | None:
    with locked_state(state_dir):
        plan = load_plan(state_dir)
        if not plan:
            return None
        plan["status"] = "canceled"
        history_dir = state_dir / "history"
        _atomic_write_json(history_dir / f"{plan['plan_id']}.json", plan)
        plan_path(state_dir).unlink(missing_ok=True)
        return plan


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
        "StartCalendarInterval": {"Hour": 8, "Minute": 59},
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
    return run_command(
        ["launchctl", action, _launch_domain(), str(plist_path)],
        text=True,
        capture_output=True,
    )


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


def _path_exists(path: Path) -> bool:
    return os.path.lexists(path)


def _remove_path(path: Path) -> None:
    if not _path_exists(path):
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def install_scheduler(
    source_dir: Path,
    state_dir: Path,
    plist_path: Path,
    python_bin: Path,
    lark_bin: Path,
    run_command: RunCommand = subprocess.run,
) -> dict:
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state_dir, 0o700)
    runtime_dir = state_dir / "runtime"
    log_dir = state_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(log_dir, 0o700)

    token = uuid.uuid4().hex
    staged_runtime = state_dir / f".runtime.{token}.new"
    backup_runtime = state_dir / f".runtime.{token}.backup"
    staged_plist = plist_path.parent / f".{plist_path.name}.{token}.new"
    backup_plist = plist_path.parent / f".{plist_path.name}.{token}.backup"
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

    old_runtime = _path_exists(runtime_dir)
    old_plist = _path_exists(plist_path)
    new_runtime_installed = False
    new_plist_installed = False
    old_agent_was_loaded = False
    try:
        bootout_result = _bootout(plist_path, run_command)
        old_agent_was_loaded = getattr(bootout_result, "returncode", 1) == 0
        if old_runtime:
            os.replace(runtime_dir, backup_runtime)
        if old_plist:
            os.replace(plist_path, backup_plist)
        os.replace(staged_runtime, runtime_dir)
        new_runtime_installed = True
        os.replace(staged_plist, plist_path)
        new_plist_installed = True
        _bootstrap(plist_path, run_command)
    except Exception as install_error:
        if new_plist_installed:
            try:
                _launchctl("bootout", plist_path, run_command)
            except Exception:
                pass
            _remove_path(plist_path)
        if new_runtime_installed:
            _remove_path(runtime_dir)
        if old_runtime and _path_exists(backup_runtime):
            os.replace(backup_runtime, runtime_dir)
        if old_plist and _path_exists(backup_plist):
            os.replace(backup_plist, plist_path)
        if old_agent_was_loaded and old_plist:
            try:
                _bootstrap(plist_path, run_command)
            except Exception as restore_error:
                raise RuntimeError(
                    f"{install_error}; previous agent restore failed: {restore_error}"
                ) from install_error
        raise
    else:
        _remove_path(backup_runtime)
        _remove_path(backup_plist)
        return {"status": "installed", "plist": str(plist_path)}
    finally:
        _remove_path(staged_runtime)
        _remove_path(staged_plist)


def uninstall_scheduler(
    state_dir: Path,
    plist_path: Path,
    run_command: RunCommand = subprocess.run,
) -> dict:
    _bootout(plist_path, run_command)
    plist_path.unlink(missing_ok=True)
    plan_path(state_dir).unlink(missing_ok=True)
    update_state_path(state_dir).unlink(missing_ok=True)
    shutil.rmtree(state_dir / "runtime", ignore_errors=True)
    return {"status": "uninstalled", "history_preserved": True}


def _json_print(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


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
    create.add_argument("--now")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    state_dir = args.state_dir
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    source_dir = Path(__file__).resolve().parent
    if args.command == "status":
        _json_print({
            "pending": load_plan(state_dir),
            "latest_result": latest_result(state_dir),
            "pending_update": load_pending_update(state_dir),
        })
        return 0
    if args.command == "cancel":
        _json_print({"canceled": cancel_plan(state_dir)})
        return 0
    if args.command == "clear-update":
        _json_print({"cleared_update": clear_pending_update(state_dir)})
        return 0
    if args.command == "create":
        now = datetime.fromisoformat(args.now) if args.now else datetime.now(TZ)
        ranges = validate_ranges(args.slot)
        _json_print(create_plan(state_dir, ranges, now, replace=args.replace))
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
