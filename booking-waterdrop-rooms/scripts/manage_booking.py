from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import tempfile
import uuid
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
