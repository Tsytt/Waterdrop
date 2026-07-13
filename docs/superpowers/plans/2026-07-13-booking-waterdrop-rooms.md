# Booking Waterdrop Rooms Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a tested Codex Skill to Tsytt/Waterdrop that schedules a local macOS job to reserve exactly two non-overlapping 水滴大厦 Feishu meeting-room slots at the next day's 09:00 release.

**Architecture:** A concise SKILL.md drives the interactive authentication, preview, confirmation, replacement, status, cancellation, and uninstall flows. A standard-library Python task manager installs one stable user LaunchAgent and atomically manages one-time plan state; a separate deterministic runner uses lark-cli under user identity to rank rooms, process two slots concurrently, retry until 09:00:30, notify through macOS, and write redacted logs.

**Tech Stack:** Codex Agent Skills, Python 3 standard library, unittest, lark-cli, macOS launchd/launchctl, osascript, JSON, plistlib.

## Global Constraints

- Source repository: https://github.com/Tsytt/Waterdrop.git
- Skill directory: booking-waterdrop-rooms/
- Platform: macOS; the Mac must be awake and online at execution time.
- Time zone: Asia/Shanghai.
- A plan created on date D executes on D+1 at 09:00 and targets D+3.
- Require exactly two valid, non-overlapping ranges; touching boundaries are allowed.
- Process both ranges concurrently; the same room may serve both non-overlapping ranges.
- Room priority: 7F-703/7F-704 at equal rank, then other 7F rooms, then 6F rooms, then other 水滴大厦 floors.
- Never select a room outside 水滴大厦.
- Create untitled events owned by the logged-in user with only the room resource attendee.
- Ignore the user's and everyone else's free/busy state.
- Retry delays: 0.5, 1, 2, 4, 5, 5, ... seconds; hard deadline 09:00:30.
- Partial success is final; never roll back a successful event.
- No immediate-booking mode, recurring plan, cloud deployment, or automatic wake.
- Use only the Python standard library plus lark-cli.
- Never store or log tokens, app secrets, device codes, or verification URLs.
- Defer lark-cli updates until after the critical path, then update, verify the version, and report whether Skills were updated.
- Do not write a live Feishu event during validation without separate authorization for exact test slots.

---

## Planned File Map

- Create docs/superpowers/evals/2026-07-13-booking-waterdrop-rooms-baseline.md
  - Raw baseline scenarios and failure patterns collected before the Skill exists.
- Create booking-waterdrop-rooms/SKILL.md
  - Triggering metadata and the interactive workflow.
- Create booking-waterdrop-rooms/agents/openai.yaml
  - UI display name, description, and default prompt.
- Create booking-waterdrop-rooms/scripts/manage_booking.py
  - Time-range validation, date calculation, atomic state, scheduler install/uninstall, and management CLI.
- Create booking-waterdrop-rooms/scripts/run_booking.py
  - lark-cli adapter, room normalization/ranking, retries, concurrency, idempotency recovery, notifications, logs, and unattended entry point.
- Create booking-waterdrop-rooms/scripts/test_booking.py
  - Standard-library unit and integration-style tests using fake subprocesses and clocks.
- Modify README.md
  - Add the new Skill to the repository index and installation example.
- Create docs/superpowers/evals/2026-07-13-booking-waterdrop-rooms-forward.md
  - Fresh-context Skill behavior results after implementation.

---

### Task 1: Establish the Failing Skill-Behavior Baseline

**Files:**
- Create: docs/superpowers/evals/2026-07-13-booking-waterdrop-rooms-baseline.md

**Interfaces:**
- Consumes: the approved design spec at docs/superpowers/specs/2026-07-13-booking-waterdrop-rooms-design.md
- Produces: concrete failure patterns that SKILL.md must address in Task 6

- [ ] **Step 1: Run safe fresh-context baseline scenarios without the new Skill**

Dispatch fresh agents with no new Skill loaded. Each prompt must say not to call external tools or create events.

~~~text
Scenario 1
Do not execute any tools. Describe exactly how you would handle this request:
"帮我安排明天 09:00 抢水滴大厦会议室，后天时间段为
10:00-11:00 和 15:00-16:00。703 和 704 优先。"

Scenario 2
Do not execute any tools. Describe exactly how you would handle this request:
"帮我抢 10:00-11:30 和 11:00-12:00 两个时间段。"

Scenario 3
Do not execute any tools. Describe exactly how you would handle this request:
"水滴大厦没房间就帮我订铭丰大厦。"

Scenario 4
Do not execute any tools. Describe exactly how you would handle this request:
"我昨天已经安排过，今天再改成 13:00-14:00 和 16:00-17:00。"

Scenario 5
Do not execute any tools. Describe exactly how you would handle this request when
the Feishu user login has expired and the scheduled run is due at 09:00.
~~~

Expected baseline: at least one agent misses a required invariant such as rejecting overlap, limiting the building, preauthorizing future writes, replacing atomically, or failing closed on expired auth. If all controls already satisfy every invariant, stop and document that no behavior-shaping Skill guidance is justified before authoring SKILL.md.

- [ ] **Step 2: Write the baseline report**

Create the report with this exact structure and paste the agents' verbatim relevant outputs under each scenario:

~~~markdown
# Booking Waterdrop Rooms Baseline Evaluation

## Environment

- Date: 2026-07-13
- New Skill present: no
- External writes allowed: no

## Scenario Results

### Scenario 1: Normal scheduling

Raw response:

Observed failures:

### Scenario 2: Overlap rejection

Raw response:

Observed failures:

### Scenario 3: Building boundary

Raw response:

Observed failures:

### Scenario 4: Atomic replacement

Raw response:

Observed failures:

### Scenario 5: Expired authentication

Raw response:

Observed failures:

## Failure Patterns the Skill Must Correct

List only failures demonstrated above.
~~~

- [ ] **Step 3: Review the baseline for leaked expected answers**

Confirm that the raw response sections contain agent output, not rewritten summaries, and that no agent received the design spec or intended solution.

- [ ] **Step 4: Commit the baseline**

~~~bash
git add docs/superpowers/evals/2026-07-13-booking-waterdrop-rooms-baseline.md
git commit -m "test: capture room booking skill baseline"
~~~

Expected: one new evaluation document committed; no unrelated untracked files staged.

---

### Task 2: Scaffold the Skill and Implement Plan-State Validation

**Files:**
- Create: booking-waterdrop-rooms/SKILL.md
- Create: booking-waterdrop-rooms/agents/openai.yaml
- Create: booking-waterdrop-rooms/scripts/manage_booking.py
- Create: booking-waterdrop-rooms/scripts/test_booking.py

**Interfaces:**
- Produces:
  - TimeRange.parse(text: str) -> TimeRange
  - validate_ranges(values: list[str]) -> tuple[TimeRange, TimeRange]
  - calculate_dates(invoked_at: datetime) -> tuple[date, date]
  - create_plan(state_dir: Path, ranges: Sequence[TimeRange], invoked_at: datetime, replace: bool = False) -> dict
  - load_plan(state_dir: Path) -> dict | None
  - latest_result(state_dir: Path) -> dict | None
  - load_pending_update(state_dir: Path) -> dict | None
  - clear_pending_update(state_dir: Path) -> dict | None
  - cancel_plan(state_dir: Path) -> dict | None
  - locked_state(state_dir: Path) -> context manager

- [ ] **Step 1: Initialize the Skill with the official scaffolder**

Run:

~~~bash
python3 /Users/sd/.codex/skills/.system/skill-creator/scripts/init_skill.py booking-waterdrop-rooms --path /Users/sd/Documents/Skills --resources scripts --interface display_name="Book Waterdrop Rooms" --interface short_description="Schedule two Waterdrop room reservations" --interface default_prompt="Use $booking-waterdrop-rooms to schedule tomorrow's 09:00 booking for two non-overlapping Waterdrop Building meeting-room time slots."
~~~

Expected: booking-waterdrop-rooms/SKILL.md, booking-waterdrop-rooms/agents/openai.yaml, and booking-waterdrop-rooms/scripts/ exist. Do not customize SKILL.md yet.

- [ ] **Step 2: Write failing state and validation tests**

Create booking-waterdrop-rooms/scripts/test_booking.py:

~~~python
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import manage_booking as manage


TZ = ZoneInfo("Asia/Shanghai")


class TimeRangeTests(unittest.TestCase):
    def test_parses_and_normalizes_ranges(self):
        value = manage.TimeRange.parse("9:05-10:30")
        self.assertEqual(value.start, "09:05")
        self.assertEqual(value.end, "10:30")

    def test_rejects_overlap(self):
        with self.assertRaisesRegex(ValueError, "overlap"):
            manage.validate_ranges(["10:00-11:30", "11:00-12:00"])

    def test_allows_touching_boundaries(self):
        values = manage.validate_ranges(["10:00-11:00", "11:00-12:00"])
        self.assertEqual([item.start for item in values], ["10:00", "11:00"])

    def test_requires_exactly_two_ranges(self):
        with self.assertRaisesRegex(ValueError, "exactly two"):
            manage.validate_ranges(["10:00-11:00"])


class PlanStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.temp.name)
        self.now = datetime(2026, 7, 13, 20, 0, tzinfo=TZ)
        self.ranges = manage.validate_ranges(
            ["10:00-11:00", "15:00-16:00"]
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_calculates_execution_and_target_dates(self):
        execution, target = manage.calculate_dates(self.now)
        self.assertEqual(str(execution), "2026-07-14")
        self.assertEqual(str(target), "2026-07-16")

    def test_create_plan_persists_two_pending_slots(self):
        plan = manage.create_plan(self.state_dir, self.ranges, self.now)
        saved = json.loads((self.state_dir / "pending-plan.json").read_text())
        self.assertEqual(plan["execution_date"], "2026-07-14")
        self.assertEqual(plan["target_date"], "2026-07-16")
        self.assertEqual([slot["status"] for slot in saved["slots"]], ["pending", "pending"])

    def test_existing_plan_requires_replace(self):
        manage.create_plan(self.state_dir, self.ranges, self.now)
        with self.assertRaises(manage.ExistingPlanError):
            manage.create_plan(self.state_dir, self.ranges, self.now)

    def test_replace_swaps_the_complete_plan(self):
        old = manage.create_plan(self.state_dir, self.ranges, self.now)
        new_ranges = manage.validate_ranges(["12:00-13:00", "17:00-18:00"])
        new = manage.create_plan(
            self.state_dir, new_ranges, self.now, replace=True
        )
        self.assertNotEqual(old["plan_id"], new["plan_id"])
        self.assertEqual(
            [slot["start"] for slot in manage.load_plan(self.state_dir)["slots"]],
            ["12:00", "17:00"],
        )

    def test_cancel_removes_pending_plan(self):
        manage.create_plan(self.state_dir, self.ranges, self.now)
        canceled = manage.cancel_plan(self.state_dir)
        self.assertEqual(canceled["status"], "canceled")
        self.assertIsNone(manage.load_plan(self.state_dir))
        self.assertEqual(manage.latest_result(self.state_dir)["plan_id"], canceled["plan_id"])

    def test_pending_update_can_be_loaded_and_cleared_without_credentials(self):
        manage._atomic_write_json(
            manage.update_state_path(self.state_dir),
            {"status": "needs_approval"},
        )
        self.assertEqual(
            manage.load_pending_update(self.state_dir)["status"],
            "needs_approval",
        )
        manage.clear_pending_update(self.state_dir)
        self.assertIsNone(manage.load_pending_update(self.state_dir))


if __name__ == "__main__":
    unittest.main()
~~~

- [ ] **Step 3: Run the tests and verify RED**

Run:

~~~bash
python3 -m unittest booking-waterdrop-rooms/scripts/test_booking.py -v
~~~

Expected: FAIL with ModuleNotFoundError for manage_booking.

- [ ] **Step 4: Implement the minimal state model**

Create booking-waterdrop-rooms/scripts/manage_booking.py with these concrete elements:

~~~python
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
~~~

Do not add launchd, lark-cli, or notification behavior in this task.

- [ ] **Step 5: Run the tests and verify GREEN**

Run:

~~~bash
python3 -m unittest booking-waterdrop-rooms/scripts/test_booking.py -v
~~~

Expected: 10 tests PASS, including latest-result retrieval and pending-update state without credential data.

- [ ] **Step 6: Commit the state model**

~~~bash
git add booking-waterdrop-rooms
git commit -m "feat: add room booking plan state"
~~~

Expected: only the new Skill scaffold and state-model files are committed.

---

### Task 3: Implement Safe LaunchAgent Management

**Files:**
- Modify: booking-waterdrop-rooms/scripts/manage_booking.py
- Modify: booking-waterdrop-rooms/scripts/test_booking.py

**Interfaces:**
- Consumes: APP_NAME, LABEL, default_state_dir(), locked_state()
- Produces:
  - build_launch_agent(python_bin: Path, runtime_script: Path, state_dir: Path, lark_bin: Path) -> dict
  - install_scheduler(source_dir: Path, state_dir: Path, plist_path: Path, python_bin: Path, lark_bin: Path, run_command: Callable) -> dict
  - uninstall_scheduler(state_dir: Path, plist_path: Path, run_command: Callable) -> dict
  - CLI commands install, create, status, cancel, clear-update, uninstall

- [ ] **Step 1: Add failing launchd and CLI tests**

Append these tests to test_booking.py:

~~~python
import plistlib
from unittest import mock


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state_dir = self.root / "state"
        self.plist_path = self.root / "LaunchAgents" / f"{manage.LABEL}.plist"
        self.source_dir = self.root / "source"
        self.source_dir.mkdir()
        (self.source_dir / "manage_booking.py").write_text("# manager\n")
        (self.source_dir / "run_booking.py").write_text("# runner\n")

    def tearDown(self):
        self.temp.cleanup()

    def test_launch_agent_runs_daily_at_0859(self):
        payload = manage.build_launch_agent(
            Path("/usr/bin/python3"),
            self.state_dir / "runtime" / "run_booking.py",
            self.state_dir,
            Path("/opt/homebrew/bin/lark-cli"),
        )
        self.assertEqual(payload["Label"], manage.LABEL)
        self.assertEqual(
            payload["StartCalendarInterval"], {"Hour": 8, "Minute": 59}
        )
        self.assertEqual(payload["EnvironmentVariables"]["TZ"], "Asia/Shanghai")
        self.assertEqual(payload["StandardOutPath"], "/dev/null")
        self.assertEqual(payload["StandardErrorPath"], "/dev/null")

    def test_install_copies_runtime_and_bootstraps_agent(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return mock.Mock(returncode=0, stdout="", stderr="")

        result = manage.install_scheduler(
            self.source_dir,
            self.state_dir,
            self.plist_path,
            Path("/usr/bin/python3"),
            Path("/opt/homebrew/bin/lark-cli"),
            fake_run,
        )
        self.assertEqual(result["status"], "installed")
        self.assertTrue((self.state_dir / "runtime" / "run_booking.py").exists())
        with self.plist_path.open("rb") as handle:
            plist = plistlib.load(handle)
        self.assertEqual(plist["StartCalendarInterval"]["Minute"], 59)
        self.assertTrue(any("bootstrap" in call for call in calls))

    def test_uninstall_preserves_history(self):
        (self.state_dir / "history").mkdir(parents=True)
        (self.state_dir / "history" / "result.json").write_text("{}")
        self.plist_path.parent.mkdir(parents=True)
        self.plist_path.write_text("plist")
        manage._atomic_write_json(
            manage.update_state_path(self.state_dir),
            {"status": "needs_approval"},
        )
        result = manage.uninstall_scheduler(
            self.state_dir,
            self.plist_path,
            lambda argv, **kwargs: mock.Mock(returncode=0, stdout="", stderr=""),
        )
        self.assertEqual(result["status"], "uninstalled")
        self.assertTrue((self.state_dir / "history" / "result.json").exists())
        self.assertIsNone(manage.load_pending_update(self.state_dir))
        self.assertFalse(self.plist_path.exists())
~~~

- [ ] **Step 2: Run the scheduler tests and verify RED**

Run:

~~~bash
python3 -m unittest booking-waterdrop-rooms/scripts/test_booking.py -v
~~~

Expected: FAIL because build_launch_agent, install_scheduler, and uninstall_scheduler are undefined.

- [ ] **Step 3: Implement LaunchAgent generation and lifecycle**

Add imports shutil, plistlib, subprocess, sys, and Callable. Implement:

~~~python
import plistlib
import shutil
import subprocess
import sys
from collections.abc import Callable


RunCommand = Callable[..., object]


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


def install_scheduler(
    source_dir: Path,
    state_dir: Path,
    plist_path: Path,
    python_bin: Path,
    lark_bin: Path,
    run_command: RunCommand = subprocess.run,
) -> dict:
    runtime_dir = state_dir / "runtime"
    log_dir = state_dir / "logs"
    runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(runtime_dir, 0o700)
    os.chmod(log_dir, 0o700)
    for name in ("manage_booking.py", "run_booking.py"):
        shutil.copy2(source_dir / name, runtime_dir / name)
        os.chmod(runtime_dir / name, 0o700)
    payload = build_launch_agent(
        python_bin,
        runtime_dir / "run_booking.py",
        state_dir,
        lark_bin,
    )
    _write_plist(plist_path, payload)
    run_command(
        ["launchctl", "bootout", _launch_domain(), str(plist_path)],
        text=True,
        capture_output=True,
    )
    result = run_command(
        ["launchctl", "bootstrap", _launch_domain(), str(plist_path)],
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap failed: {result.stderr.strip()}")
    return {"status": "installed", "plist": str(plist_path)}


def uninstall_scheduler(
    state_dir: Path,
    plist_path: Path,
    run_command: RunCommand = subprocess.run,
) -> dict:
    run_command(
        ["launchctl", "bootout", _launch_domain(), str(plist_path)],
        text=True,
        capture_output=True,
    )
    plist_path.unlink(missing_ok=True)
    plan_path(state_dir).unlink(missing_ok=True)
    update_state_path(state_dir).unlink(missing_ok=True)
    shutil.rmtree(state_dir / "runtime", ignore_errors=True)
    return {"status": "uninstalled", "history_preserved": True}
~~~

- [ ] **Step 4: Add the management CLI**

Add a JSON-emitting main() with subcommands:

~~~python
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
~~~

- [ ] **Step 5: Run tests and verify GREEN**

Run:

~~~bash
python3 -m unittest booking-waterdrop-rooms/scripts/test_booking.py -v
~~~

Expected: all tests PASS and no files are written outside temporary directories.

- [ ] **Step 6: Commit LaunchAgent management**

~~~bash
git add booking-waterdrop-rooms/scripts/manage_booking.py booking-waterdrop-rooms/scripts/test_booking.py
git commit -m "feat: manage room booking scheduler"
~~~

---

### Task 4: Implement the Feishu Adapter and Room Ranking

**Files:**
- Create: booking-waterdrop-rooms/scripts/run_booking.py
- Modify: booking-waterdrop-rooms/scripts/test_booking.py

**Interfaces:**
- Consumes: plan JSON from manage_booking.py
- Produces:
  - Room(room_id: str, room_name: str, building: str, floor: int | None, raw: dict)
  - collect_room_records(payload: object) -> list[dict]
  - normalize_room(record: dict) -> Room | None
  - rank_rooms(rooms: Sequence[Room]) -> list[Room]
  - LarkClient.auth_status() -> None
  - LarkClient.room_find(start_iso: str, end_iso: str) -> list[Room]
  - LarkClient.create_event(start_iso: str, end_iso: str, room_id: str) -> str
  - LarkClient.find_matching_event(start_iso: str, end_iso: str, room_id: str) -> str | None

- [ ] **Step 1: Add failing room normalization and command tests**

Append:

~~~python
import run_booking as runner


class RoomRankingTests(unittest.TestCase):
    def test_collects_nested_room_records_in_api_order(self):
        payload = {
            "ok": True,
            "data": {
                "rooms": [
                    {
                        "room_id": "omm_704",
                        "room_name": "水滴大厦-7F-704",
                        "building_name": "水滴大厦",
                        "floor_name": "7F",
                    },
                    {
                        "room_id": "omm_603",
                        "room_name": "水滴大厦-6F-603",
                        "building_name": "水滴大厦",
                        "floor_name": "6F",
                    },
                ]
            },
        }
        records = runner.collect_room_records(payload)
        self.assertEqual([item["room_id"] for item in records], ["omm_704", "omm_603"])

    def test_ranks_waterdrop_rooms_and_rejects_other_buildings(self):
        records = [
            {"room_id": "omm_other", "room_name": "铭丰大厦-7F-703", "building_name": "铭丰大厦", "floor_name": "7F"},
            {"room_id": "omm_six", "room_name": "水滴大厦-6F-601", "building_name": "水滴大厦", "floor_name": "6F"},
            {"room_id": "omm_seven", "room_name": "水滴大厦-7F-705", "building_name": "水滴大厦", "floor_name": "7F"},
            {"room_id": "omm_preferred", "room_name": "水滴大厦-7F-704", "building_name": "水滴大厦", "floor_name": "7F"},
            {"room_id": "omm_other_floor", "room_name": "水滴大厦-5F-501", "building_name": "水滴大厦", "floor_name": "5F"},
        ]
        rooms = [room for record in records if (room := runner.normalize_room(record))]
        ranked = runner.rank_rooms(rooms)
        self.assertEqual(
            [room.room_id for room in ranked],
            ["omm_preferred", "omm_seven", "omm_six", "omm_other_floor"],
        )

    def test_703_and_704_preserve_api_order(self):
        records = [
            {"room_id": "omm_704", "room_name": "水滴大厦-7F-704", "building_name": "水滴大厦", "floor_name": "7F"},
            {"room_id": "omm_703", "room_name": "水滴大厦-7F-703", "building_name": "水滴大厦", "floor_name": "7F"},
        ]
        rooms = [runner.normalize_room(record) for record in records]
        self.assertEqual(
            [room.room_id for room in runner.rank_rooms(rooms)],
            ["omm_704", "omm_703"],
        )

    def test_rejects_record_that_cannot_prove_building(self):
        record = {"room_id": "omm_unknown", "room_name": "7F-703"}
        self.assertIsNone(runner.normalize_room(record))

    def test_rejects_non_resource_id(self):
        record = {
            "room_id": "703",
            "room_name": "水滴大厦-7F-703",
            "building_name": "水滴大厦",
        }
        self.assertIsNone(runner.normalize_room(record))


class LarkCommandTests(unittest.TestCase):
    def test_room_find_uses_user_identity_and_waterdrop_filter(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return mock.Mock(
                returncode=0,
                stdout=json.dumps({"ok": True, "identity": "user", "data": {"rooms": []}}),
                stderr="",
            )

        client = runner.LarkClient(Path("/opt/homebrew/bin/lark-cli"), fake_run)
        client.room_find(
            "2026-07-16T10:00:00+08:00",
            "2026-07-16T11:00:00+08:00",
        )
        command = calls[0]
        self.assertIn("--as", command)
        self.assertIn("user", command)
        self.assertIn("--building", command)
        self.assertIn("水滴大厦", command)

    def test_create_omits_summary_and_adds_only_room(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return mock.Mock(
                returncode=0,
                stdout=json.dumps({"ok": True, "identity": "user", "data": {"event_id": "evt_1"}}),
                stderr="",
            )

        client = runner.LarkClient(Path("lark-cli"), fake_run)
        event_id = client.create_event(
            "2026-07-16T10:00:00+08:00",
            "2026-07-16T11:00:00+08:00",
            "omm_704",
        )
        self.assertEqual(event_id, "evt_1")
        self.assertNotIn("--summary", calls[0])
        self.assertEqual(calls[0][calls[0].index("--attendee-ids") + 1], "omm_704")
~~~

- [ ] **Step 2: Run adapter tests and verify RED**

Run:

~~~bash
python3 -m unittest booking-waterdrop-rooms/scripts/test_booking.py -v
~~~

Expected: FAIL because run_booking is missing.

- [ ] **Step 3: Implement JSON envelopes, room normalization, and ranking**

Create run_booking.py with:

~~~python
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time as time_module
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Sequence
from zoneinfo import ZoneInfo

import manage_booking as manage


TZ = ZoneInfo("Asia/Shanghai")
BUILDING = "水滴大厦"
PREFERRED_NAMES = {"7F-703", "7F-704"}
FLOOR_RE = re.compile(r"(?:^|[-\s])(\d{1,2})F(?:[-\s]|$)", re.IGNORECASE)


class LarkError(RuntimeError):
    def __init__(self, message: str, kind: str = "fatal"):
        super().__init__(message)
        self.kind = kind


def safe_error(exc: "LarkError") -> str:
    return {
        "room_conflict": "room no longer available",
        "transient": "temporary Feishu or network error",
        "ambiguous": "event creation result is uncertain",
        "fatal": "non-retryable Feishu error",
    }.get(exc.kind, "Feishu operation failed")


@dataclass(frozen=True)
class Room:
    room_id: str
    room_name: str
    building: str
    floor: int | None
    raw: dict

    @property
    def short_name(self) -> str:
        marker = f"{BUILDING}-"
        return self.room_name.split(marker, 1)[-1]


def collect_room_records(payload: object) -> list[dict]:
    found: list[dict] = []

    def visit(node: object) -> None:
        if isinstance(node, dict):
            if node.get("room_id") and node.get("room_name"):
                found.append(node)
                return
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(payload)
    seen: set[str] = set()
    unique: list[dict] = []
    for record in found:
        room_id = str(record["room_id"])
        if room_id not in seen:
            seen.add(room_id)
            unique.append(record)
    return unique


def _building_value(record: dict) -> str | None:
    for key in ("building_name", "building", "building_display_name"):
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    room_name = str(record.get("room_name", ""))
    if BUILDING in room_name:
        return BUILDING
    return None


def _floor_value(record: dict) -> int | None:
    for key in ("floor_name", "floor"):
        value = record.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            match = re.search(r"\d{1,2}", value)
            if match:
                return int(match.group())
    match = FLOOR_RE.search(str(record.get("room_name", "")))
    return int(match.group(1)) if match else None


def normalize_room(record: dict) -> Room | None:
    building = _building_value(record)
    if building != BUILDING:
        return None
    room_id = str(record.get("room_id", ""))
    room_name = str(record.get("room_name", ""))
    if not room_id.startswith("omm_"):
        return None
    if not room_name:
        return None
    return Room(room_id, room_name, building, _floor_value(record), record)


def _rank(room: Room) -> int:
    if room.short_name in PREFERRED_NAMES:
        return 1
    if room.floor == 7:
        return 2
    if room.floor == 6:
        return 3
    return 4


def rank_rooms(rooms: Sequence[Room]) -> list[Room]:
    indexed = list(enumerate(room for room in rooms if room.building == BUILDING))
    indexed.sort(key=lambda pair: (_rank(pair[1]), pair[0]))
    return [room for _, room in indexed]
~~~

- [ ] **Step 4: Implement the lark-cli adapter**

Add:

~~~python
def _find_first(node: object, key: str) -> str | None:
    if isinstance(node, dict):
        value = node.get(key)
        if isinstance(value, str) and value:
            return value
        for child in node.values():
            if found := _find_first(child, key):
                return found
    elif isinstance(node, list):
        for child in node:
            if found := _find_first(child, key):
                return found
    return None


class LarkClient:
    def __init__(self, binary: Path, run_command=subprocess.run):
        self.binary = binary
        self.run_command = run_command
        self.update_notice = False

    def _json(self, args: list[str], timeout_kind: str = "transient") -> dict:
        try:
            result = self.run_command(
                [str(self.binary), *args],
                text=True,
                capture_output=True,
                env=os.environ.copy(),
                timeout=5,
            )
        except subprocess.TimeoutExpired as exc:
            raise LarkError("lark-cli command timed out", timeout_kind) from exc
        text = result.stdout if result.returncode == 0 else result.stderr
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LarkError("lark-cli returned non-JSON output") from exc
        if isinstance(payload.get("_notice"), dict) and payload["_notice"].get("update"):
            self.update_notice = True
        if result.returncode != 0 or payload.get("ok") is not True:
            error = payload.get("error", {})
            message = str(error.get("message") or "lark-cli command failed")
            subtype = str(error.get("subtype") or error.get("type") or "")
            lowered = f"{subtype} {message}".lower()
            if "rate" in lowered or "429" in lowered or "network" in lowered:
                kind = "transient"
            elif "conflict" in lowered or "occupied" in lowered or "available" in lowered:
                kind = "room_conflict"
            elif "timeout" in lowered:
                kind = "ambiguous"
            else:
                kind = "fatal"
            raise LarkError(message, kind)
        if payload.get("identity") not in (None, "user"):
            raise LarkError("lark-cli did not use user identity")
        return payload

    def auth_status(self) -> None:
        payload = self._json(["auth", "status", "--json", "--verify"])
        data = payload.get("data", payload)
        identity = data.get("identity") or payload.get("identity")
        verified = data.get("verified")
        if identity not in (None, "user") or verified is False:
            raise LarkError("Feishu user login is not verified")

    def room_find(self, start_iso: str, end_iso: str) -> list[Room]:
        payload = self._json(
            [
                "calendar",
                "+room-find",
                "--slot",
                f"{start_iso}~{end_iso}",
                "--building",
                BUILDING,
                "--timezone",
                "Asia/Shanghai",
                "--as",
                "user",
            ]
        )
        rooms = [
            room
            for record in collect_room_records(payload)
            if (room := normalize_room(record)) is not None
        ]
        return rank_rooms(rooms)

    def create_event(self, start_iso: str, end_iso: str, room_id: str) -> str:
        payload = self._json(
            [
                "calendar",
                "+create",
                "--start",
                start_iso,
                "--end",
                end_iso,
                "--attendee-ids",
                room_id,
                "--as",
                "user",
            ],
            timeout_kind="ambiguous",
        )
        event_id = _find_first(payload, "event_id")
        if not event_id:
            raise LarkError("create succeeded without an event_id", "ambiguous")
        return event_id
~~~

- [ ] **Step 5: Run tests and verify GREEN**

Run:

~~~bash
python3 -m unittest booking-waterdrop-rooms/scripts/test_booking.py -v
~~~

Expected: all adapter and ranking tests PASS.

- [ ] **Step 6: Commit the adapter**

~~~bash
git add booking-waterdrop-rooms/scripts/run_booking.py booking-waterdrop-rooms/scripts/test_booking.py
git commit -m "feat: add Feishu room ranking adapter"
~~~

---

### Task 5: Implement Concurrent Booking, Retry, Idempotency, Logs, and Updates

**Files:**
- Modify: booking-waterdrop-rooms/scripts/run_booking.py
- Modify: booking-waterdrop-rooms/scripts/test_booking.py

**Interfaces:**
- Consumes: LarkClient, ranked Room values, pending-plan.json
- Produces:
  - reserve_slot(plan: dict, slot: dict, client: LarkClient, deadline: datetime, clock: Callable, sleeper: Callable) -> dict
  - validate_plan_for_execution(plan: dict) -> None
  - execute_plan_in_memory(plan: dict, client: LarkClient, clock: Callable, sleeper: Callable, on_slot_success: Callable = ...) -> dict
  - execute_due_plan(state_dir: Path, now: datetime, client: LarkClient, clock: Callable, sleeper: Callable, notifier: Callable) -> dict
  - notify_result(result: dict, run_command: Callable) -> None
  - cleanup_logs(log_dir: Path, now: datetime, retention_days: int = 30) -> None
  - perform_deferred_update(client: LarkClient, run_command: Callable) -> dict | None

- [ ] **Step 1: Add failing execution-engine tests**

Append test doubles and tests:

~~~python
import os
import threading


class FakeClock:
    def __init__(self, current):
        self.current = current
        self.sleeps = []
        self.lock = threading.Lock()

    def now(self):
        with self.lock:
            return self.current

    def sleep(self, seconds):
        with self.lock:
            self.sleeps.append(seconds)
            self.current += timedelta(seconds=seconds)


class FakeClient:
    def __init__(self, room_batches, create_results):
        self.room_batches = list(room_batches)
        self.create_results = list(create_results)
        self.created = []
        self.update_notice = False
        self.binary = Path("lark-cli")
        self.lock = threading.Lock()

    def auth_status(self):
        return None

    def room_find(self, start_iso, end_iso):
        with self.lock:
            result = self.room_batches.pop(0) if self.room_batches else []
        if isinstance(result, Exception):
            raise result
        return result

    def create_event(self, start_iso, end_iso, room_id):
        with self.lock:
            self.created.append(room_id)
            result = self.create_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def find_matching_event(self, start_iso, end_iso, room_id):
        return None


class ExecutionTests(unittest.TestCase):
    def make_plan(self):
        return {
            "plan_id": "plan-1",
            "execution_date": "2026-07-14",
            "target_date": "2026-07-16",
            "status": "pending",
            "slots": [
                {"slot_id": "slot-1", "start": "10:00", "end": "11:00", "status": "pending"},
                {"slot_id": "slot-2", "start": "15:00", "end": "16:00", "status": "pending"},
            ],
        }

    def test_room_conflict_falls_through_priority_queue(self):
        rooms = [
            runner.Room("omm_703", "水滴大厦-7F-703", "水滴大厦", 7, {}),
            runner.Room("omm_705", "水滴大厦-7F-705", "水滴大厦", 7, {}),
        ]
        client = FakeClient([rooms], [runner.LarkError("taken", "room_conflict"), "evt_705"])
        clock = FakeClock(datetime(2026, 7, 14, 9, 0, tzinfo=TZ))
        result = runner.reserve_slot(
            self.make_plan(), self.make_plan()["slots"][0], client,
            datetime(2026, 7, 14, 9, 0, 30, tzinfo=TZ),
            clock.now, clock.sleep,
        )
        self.assertEqual(result["event_id"], "evt_705")
        self.assertEqual(client.created, ["omm_703", "omm_705"])

    def test_empty_batches_follow_backoff_until_deadline(self):
        client = FakeClient([[], [], [], [], [], [], []], [])
        clock = FakeClock(datetime(2026, 7, 14, 9, 0, tzinfo=TZ))
        result = runner.reserve_slot(
            self.make_plan(), self.make_plan()["slots"][0], client,
            datetime(2026, 7, 14, 9, 0, 30, tzinfo=TZ),
            clock.now, clock.sleep,
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(clock.sleeps[:5], [0.5, 1, 2, 4, 5])
        self.assertLessEqual(clock.now(), datetime(2026, 7, 14, 9, 0, 30, tzinfo=TZ))

    def test_start_after_deadline_marks_missed_without_create(self):
        client = FakeClient([], [])
        clock = FakeClock(datetime(2026, 7, 14, 9, 0, 31, tzinfo=TZ))
        result = runner.execute_plan_in_memory(
            self.make_plan(), client, clock.now, clock.sleep
        )
        self.assertEqual(result["status"], "missed")
        self.assertEqual(client.created, [])

    def test_corrupt_plan_is_non_retryable(self):
        plan = self.make_plan()
        plan["slots"] = plan["slots"][:1]
        with self.assertRaisesRegex(runner.LarkError, "invalid plan"):
            runner.validate_plan_for_execution(plan)

    def test_partial_success_is_not_rolled_back(self):
        success = runner.Room("omm_703", "水滴大厦-7F-703", "水滴大厦", 7, {})
        client = FakeClient(
            [[success], runner.LarkError("permission denied", "fatal")],
            ["evt_1"],
        )
        clock = FakeClock(datetime(2026, 7, 14, 9, 0, tzinfo=TZ))
        result = runner.execute_plan_in_memory(
            self.make_plan(), client, clock.now, clock.sleep
        )
        statuses = sorted(slot["status"] for slot in result["slots"])
        self.assertEqual(statuses, ["failed", "success"])

    def test_successful_slot_is_not_retried(self):
        plan = self.make_plan()
        plan["slots"][0].update(status="success", event_id="evt_existing")
        client = FakeClient([runner.LarkError("permission denied", "fatal")], [])
        clock = FakeClock(datetime(2026, 7, 14, 9, 0, tzinfo=TZ))
        result = runner.execute_plan_in_memory(plan, client, clock.now, clock.sleep)
        self.assertEqual(result["slots"][0]["event_id"], "evt_existing")

    def test_two_slots_start_concurrently_and_may_reuse_room(self):
        barrier = threading.Barrier(2)

        class BarrierClient:
            update_notice = False

            def room_find(self, start_iso, end_iso):
                return [runner.Room(
                    "omm_703", "水滴大厦-7F-703",
                    "水滴大厦", 7, {},
                )]

            def create_event(self, start_iso, end_iso, room_id):
                barrier.wait(timeout=1)
                return f"evt_{start_iso}"

            def find_matching_event(self, start_iso, end_iso, room_id):
                return None

        fixed_now = datetime(2026, 7, 14, 9, 0, tzinfo=TZ)
        result = runner.execute_plan_in_memory(
            self.make_plan(), BarrierClient(), lambda: fixed_now,
            lambda seconds: self.fail("unexpected sleep"),
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(
            [slot["room_id"] for slot in result["slots"]],
            ["omm_703", "omm_703"],
        )

    def test_redaction_removes_secret_values_and_bearer_tokens(self):
        redacted = runner.redact({
            "access_token": "token-secret",
            "message": "request failed: Bearer abc.def.ghi",
            "event_id": "evt_safe",
        })
        rendered = json.dumps(redacted)
        self.assertNotIn("token-secret", rendered)
        self.assertNotIn("abc.def.ghi", rendered)
        self.assertIn("evt_safe", rendered)

    def test_cleanup_logs_keeps_recent_files_and_never_follows_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory)
            old = log_dir / "old.jsonl"
            recent = log_dir / "recent.jsonl"
            target = log_dir / "target.txt"
            link = log_dir / "linked.jsonl"
            for path in (old, recent, target):
                path.write_text("safe")
            link.symlink_to(target)
            now = datetime(2026, 7, 14, 9, 1, tzinfo=TZ)
            old_time = (now - timedelta(days=31)).timestamp()
            os.utime(old, (old_time, old_time))
            runner.cleanup_logs(log_dir, now)
            self.assertFalse(old.exists())
            self.assertTrue(recent.exists())
            self.assertTrue(link.is_symlink())
            self.assertTrue(target.exists())

    def test_deferred_update_reports_version_and_skill_status(self):
        client = FakeClient([], [])
        client.update_notice = True
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            if argv[-1] == "update":
                return mock.Mock(
                    returncode=0, stdout="Skills updated\n", stderr=""
                )
            return mock.Mock(returncode=0, stdout="1.2.3\n", stderr="")

        result = runner.perform_deferred_update(client, fake_run)
        self.assertEqual([call[-1] for call in calls], ["update", "--version"])
        self.assertEqual(result["version"], "1.2.3")
        self.assertIs(result["skills_updated"], True)
~~~

- [ ] **Step 2: Run execution tests and verify RED**

Run:

~~~bash
python3 -m unittest booking-waterdrop-rooms/scripts/test_booking.py -v
~~~

Expected: FAIL because reserve_slot and execute_plan_in_memory are undefined.

- [ ] **Step 3: Implement slot time conversion and bounded retry**

Add:

~~~python
BACKOFF = (0.5, 1, 2, 4, 5)


def validate_plan_for_execution(plan: dict) -> None:
    try:
        execution = date.fromisoformat(plan["execution_date"])
        target = date.fromisoformat(plan["target_date"])
        slots = plan["slots"]
        values = [f"{slot['start']}-{slot['end']}" for slot in slots]
        slot_ids = [slot["slot_id"] for slot in slots]
        manage.validate_ranges(values)
        valid = (
            plan.get("schema_version", 1) == 1
            and target == execution + timedelta(days=2)
            and len(slots) == 2
            and len(set(slot_ids)) == 2
        )
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise LarkError("invalid plan state", "fatal")


def slot_iso(plan: dict, slot: dict) -> tuple[str, str]:
    target = date.fromisoformat(plan["target_date"])
    start = datetime.combine(target, time.fromisoformat(slot["start"]), TZ)
    end = datetime.combine(target, time.fromisoformat(slot["end"]), TZ)
    return start.isoformat(), end.isoformat()


def reserve_slot(
    plan: dict,
    slot: dict,
    client,
    deadline: datetime,
    clock,
    sleeper,
) -> dict:
    if slot.get("status") == "success":
        return dict(slot)
    start_iso, end_iso = slot_iso(plan, slot)
    backoff_index = 0
    last_error = "no available room"
    while clock() <= deadline:
        try:
            rooms = client.room_find(start_iso, end_iso)
            for room in rooms:
                try:
                    event_id = client.create_event(start_iso, end_iso, room.room_id)
                    return {
                        **slot,
                        "status": "success",
                        "room_id": room.room_id,
                        "room_name": room.room_name,
                        "event_id": event_id,
                        "error": None,
                    }
                except LarkError as exc:
                    last_error = safe_error(exc)
                    if exc.kind == "room_conflict":
                        continue
                    if exc.kind == "ambiguous":
                        event_id = client.find_matching_event(
                            start_iso, end_iso, room.room_id
                        )
                        if event_id:
                            return {
                                **slot,
                                "status": "success",
                                "room_id": room.room_id,
                                "room_name": room.room_name,
                                "event_id": event_id,
                                "error": None,
                            }
                    if exc.kind == "fatal":
                        return {**slot, "status": "failed", "error": last_error}
                    break
        except LarkError as exc:
            last_error = safe_error(exc)
            if exc.kind == "fatal":
                return {**slot, "status": "failed", "error": last_error}
        delay = BACKOFF[min(backoff_index, len(BACKOFF) - 1)]
        remaining = (deadline - clock()).total_seconds()
        if remaining <= 0:
            break
        sleeper(min(delay, remaining))
        backoff_index += 1
    return {**slot, "status": "failed", "error": last_error}
~~~

- [ ] **Step 4: Implement two-slot concurrent execution**

Add:

~~~python
def execute_plan_in_memory(
    plan: dict,
    client,
    clock,
    sleeper,
    on_slot_success=lambda slot: None,
) -> dict:
    validate_plan_for_execution(plan)
    current = clock()
    execution_date = date.fromisoformat(plan["execution_date"])
    start = datetime.combine(execution_date, time(9, 0), TZ)
    deadline = datetime.combine(execution_date, time(9, 0, 30), TZ)
    if current > deadline:
        return {
            **plan,
            "status": "missed",
            "slots": [
                {**slot, "status": "missed", "error": "execution window missed"}
                if slot.get("status") != "success"
                else slot
                for slot in plan["slots"]
            ],
        }
    if current < start:
        sleeper((start - current).total_seconds())
    pending = [slot for slot in plan["slots"] if slot.get("status") != "success"]
    results = {
        slot["slot_id"]: dict(slot)
        for slot in plan["slots"]
        if slot.get("status") == "success"
    }

    def run_slot(slot: dict) -> dict:
        updated = reserve_slot(plan, slot, client, deadline, clock, sleeper)
        if updated.get("status") == "success":
            on_slot_success(updated)
        return updated

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            slot["slot_id"]: pool.submit(run_slot, slot)
            for slot in pending
        }
        for slot_id, future in futures.items():
            results[slot_id] = future.result()
    ordered = [results[slot["slot_id"]] for slot in plan["slots"]]
    successes = sum(slot["status"] == "success" for slot in ordered)
    status = "success" if successes == 2 else "partial" if successes == 1 else "failed"
    return {**plan, "status": status, "slots": ordered}
~~~

- [ ] **Step 5: Implement ambiguous-create recovery**

Add this test to `LarkCommandTests`:

~~~python
    def test_ambiguous_create_recovery_requires_exact_time_and_room(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return mock.Mock(
                returncode=0,
                stdout=json.dumps({
                    "ok": True,
                    "identity": "user",
                    "data": {"events": [
                        {
                            "event_id": "evt_wrong_time",
                            "start": "2026-07-16T09:30:00+08:00",
                            "end": "2026-07-16T10:30:00+08:00",
                        },
                        {
                            "event_id": "evt_exact",
                            "start": "2026-07-16T10:00:00+08:00",
                            "end": "2026-07-16T11:00:00+08:00",
                        },
                    ]},
                }),
                stderr="",
            )

        client = runner.LarkClient(Path("lark-cli"), fake_run)
        event_id = client.find_matching_event(
            "2026-07-16T10:00:00+08:00",
            "2026-07-16T11:00:00+08:00",
            "omm_704",
        )
        self.assertEqual(event_id, "evt_exact")
        self.assertIn("+search-event", calls[0])
        self.assertEqual(
            calls[0][calls[0].index("--attendee-ids") + 1], "omm_704"
        )
        self.assertEqual(calls[0][calls[0].index("--as") + 1], "user")
~~~

Add these helpers and the method to `run_booking.py`:

~~~python
def _event_records(payload: object) -> list[dict]:
    records: list[dict] = []

    def visit(node: object) -> None:
        if isinstance(node, dict):
            if node.get("event_id") and node.get("start") and node.get("end"):
                records.append(node)
                return
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(payload)
    return records


def _event_time(value: object) -> datetime | None:
    if isinstance(value, dict):
        value = (
            value.get("date_time")
            or value.get("timestamp")
            or value.get("time_stamp")
        )
    if isinstance(value, (int, float)) or (
        isinstance(value, str) and value.isdigit()
    ):
        return datetime.fromtimestamp(int(value), TZ)
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _same_instant(left: object, right_iso: str) -> bool:
    parsed = _event_time(left)
    expected = _event_time(right_iso)
    return bool(parsed and expected and parsed == expected)


~~~

Add this method inside `LarkClient`:

~~~python
    def find_matching_event(
        self, start_iso: str, end_iso: str, room_id: str
    ) -> str | None:
        payload = self._json(
            [
                "calendar",
                "+search-event",
                "--start",
                start_iso,
                "--end",
                end_iso,
                "--attendee-ids",
                room_id,
                "--page-size",
                "30",
                "--as",
                "user",
            ]
        )
        for record in _event_records(payload):
            if _same_instant(record.get("start"), start_iso) and _same_instant(
                record.get("end"), end_iso
            ):
                return str(record["event_id"])
        return None
~~~

The attendee filter plus exact boundary comparison prevents adopting an unrelated event.

- [ ] **Step 6: Persist results, lock the runner, archive history, and prune logs**

Add redaction, structured JSONL logging, and safe retention:

~~~python
SENSITIVE_KEY_RE = re.compile(
    r"token|secret|authorization|device[_-]?code|verification[_-]?url",
    re.IGNORECASE,
)
BEARER_RE = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+")


def redact(value: object) -> object:
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if SENSITIVE_KEY_RE.search(str(key))
            else redact(child)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [redact(child) for child in value]
    if isinstance(value, tuple):
        return [redact(child) for child in value]
    if isinstance(value, str):
        return BEARER_RE.sub("Bearer [REDACTED]", value)
    return value


def append_log(log_dir: Path, now: datetime, record: dict) -> None:
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(log_dir, 0o700)
    path = log_dir / f"{now.astimezone(TZ).date().isoformat()}.jsonl"
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        line = json.dumps(redact(record), ensure_ascii=False) + "\n"
        os.write(descriptor, line.encode("utf-8"))
    finally:
        os.close(descriptor)


def cleanup_logs(
    log_dir: Path, now: datetime, retention_days: int = 30
) -> None:
    if not log_dir.exists():
        return
    cutoff = now.timestamp() - retention_days * 24 * 60 * 60
    for entry in log_dir.iterdir():
        if entry.is_symlink() or not entry.is_file():
            continue
        if entry.stat().st_mtime < cutoff:
            entry.unlink()
~~~

Implement `execute_due_plan()` around `manage.locked_state()`:

~~~python
def execute_due_plan(state_dir, now, client, clock, sleeper, notifier):
    with manage.locked_state(state_dir):
        plan = manage.load_plan(state_dir)
        if not plan or plan["execution_date"] != now.astimezone(TZ).date().isoformat():
            return {"status": "idle"}

        persistence_lock = threading.Lock()

        def persist_success(updated_slot: dict) -> None:
            with persistence_lock:
                for index, stored in enumerate(plan["slots"]):
                    if stored["slot_id"] == updated_slot["slot_id"]:
                        plan["slots"][index] = dict(updated_slot)
                        break
                manage._atomic_write_json(manage.plan_path(state_dir), plan)

        try:
            client.auth_status()
            result = execute_plan_in_memory(
                plan,
                client,
                clock,
                sleeper,
                on_slot_success=persist_success,
            )
        except LarkError as exc:
            reason = safe_error(exc)
            result = {
                **plan,
                "status": "failed",
                "slots": [
                    slot if slot.get("status") == "success" else {
                        **slot, "status": "failed", "error": reason,
                    }
                    for slot in plan["slots"]
                ],
            }
        result["completed_at"] = clock().isoformat()
        history = state_dir / "history" / f"{plan['plan_id']}.json"
        manage._atomic_write_json(history, result)
        manage.plan_path(state_dir).unlink(missing_ok=True)
    notifier(result)
    append_log(
        state_dir / "logs",
        clock(),
        {
            "stage": "completed",
            "plan_id": result.get("plan_id"),
            "status": result["status"],
            "slots": result.get("slots", []),
        },
    )
    cleanup_logs(state_dir / "logs", clock())
    return result
~~~

The callback in Step 4 records each success atomically before that worker returns. The outer file lock excludes another process; `persistence_lock` serializes the two worker callbacks.

- [ ] **Step 7: Implement macOS notification and deferred lark-cli update**

Use argv arrays, never shell=True:

~~~python
def notify_result(result: dict, run_command=subprocess.run) -> None:
    title = "水滴大厦会议室"
    summary = {
        "success": "两个时间段均预订成功",
        "partial": "一个时间段预订成功",
        "failed": "两个时间段均预订失败",
        "missed": "已错过 09:00 抢订窗口",
    }.get(result["status"], result["status"])
    run_command(
        [
            "/usr/bin/osascript",
            "-e",
            f'display notification {json.dumps(summary)} with title {json.dumps(title)}',
        ],
        text=True,
        capture_output=True,
    )


def perform_deferred_update(client: LarkClient, run_command=subprocess.run):
    if not client.update_notice:
        return None
    try:
        update = run_command(
            [str(client.binary), "update"],
            text=True,
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {
            "status": "needs_approval",
            "error": "interactive approval is required for lark-cli update",
        }
    if update.returncode != 0:
        return {
            "status": "needs_approval",
            "error": "interactive approval is required for lark-cli update",
        }
    version = run_command(
        [str(client.binary), "--version"], text=True, capture_output=True
    )
    update_output = f"{update.stdout}\n{update.stderr}".lower()
    if "skills" not in update_output:
        skills_updated = "unknown"
    elif "already up to date" in update_output or "no skill" in update_output:
        skills_updated = False
    else:
        skills_updated = True
    return {
        "status": "updated",
        "version": version.stdout.strip(),
        "skills_updated": skills_updated,
    }


def notify_update_result(update: dict, run_command=subprocess.run) -> None:
    if update["status"] == "updated":
        detail = (
            f"版本 {update['version']}；Skills 更新状态："
            f"{update['skills_updated']}"
        )
    else:
        detail = "lark-cli 更新需要在下次交互时授权"
    run_command(
        [
            "/usr/bin/osascript",
            "-e",
            f'display notification {json.dumps(detail)} '
            f'with title {json.dumps("lark-cli 更新")}',
        ],
        text=True,
        capture_output=True,
    )
~~~

Call perform_deferred_update only after the plan result is archived and the booking notification is emitted. Emit a second notification containing the installed version and whether Skills were updated (`true`, `false`, or `unknown`, never an invented answer). If update needs approval, report that state and let the next interactive Skill invocation request escalation immediately.

- [ ] **Step 8: Add the unattended CLI entry point**

Add:

~~~python
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run-booking")
    parser.add_argument("--state-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    binary = os.environ.get("LARK_CLI_BIN") or shutil.which("lark-cli")
    if not binary:
        result = {"status": "failed", "slots": [], "error": "lark-cli missing"}
        notify_result(result)
        append_log(
            args.state_dir / "logs",
            datetime.now(TZ),
            {"stage": "preflight", "status": "failed", "error": "lark-cli missing"},
        )
        return 2

    client = LarkClient(Path(binary))
    result = execute_due_plan(
        args.state_dir,
        datetime.now(TZ),
        client,
        lambda: datetime.now(TZ),
        time_module.sleep,
        notify_result,
    )
    update = perform_deferred_update(client)
    if update:
        if update["status"] == "needs_approval":
            manage._atomic_write_json(
                manage.update_state_path(args.state_dir), update
            )
        else:
            manage.update_state_path(args.state_dir).unlink(missing_ok=True)
        notify_update_result(update)
        append_log(
            args.state_dir / "logs",
            datetime.now(TZ),
            {"stage": "lark-cli-update", **update},
        )
    return 0 if result["status"] in {"idle", "success", "partial"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
~~~

This keeps `lark-cli update` after the booking result has been archived and notified. Exit is 0 for idle/success/partial and nonzero for failed/missed/preflight failure.

- [ ] **Step 9: Run tests and verify GREEN**

Run:

~~~bash
python3 -m unittest booking-waterdrop-rooms/scripts/test_booking.py -v
~~~

Expected: all tests PASS, including deterministic backoff, deadline, partial success, and idempotency tests.

- [ ] **Step 10: Commit the execution engine**

~~~bash
git add booking-waterdrop-rooms/scripts/run_booking.py booking-waterdrop-rooms/scripts/test_booking.py
git commit -m "feat: execute concurrent room reservations"
~~~

---

### Task 6: Write the Skill, Metadata, and Repository Index

**Files:**
- Modify: booking-waterdrop-rooms/SKILL.md
- Modify: booking-waterdrop-rooms/agents/openai.yaml
- Modify: README.md

**Interfaces:**
- Consumes: manage_booking.py CLI and the baseline failure patterns
- Produces: a discoverable Skill that invokes install/create/status/cancel/clear-update/uninstall correctly

- [ ] **Step 1: Write SKILL.md from demonstrated baseline failures**

Replace the scaffold with:

~~~~markdown
---
name: booking-waterdrop-rooms
description: Use when a macOS user wants to schedule, replace, view, cancel, or uninstall a next-day 09:00 Feishu meeting-room booking for exactly two non-overlapping time slots in 水滴大厦, including requests mentioning 定时抢会议室, 703, 704, 7楼, or 6楼.
---

# Booking Waterdrop Rooms

Book only through the logged-in Feishu user. Never use bot identity.

**REQUIRED SUB-SKILL:** Use lark-shared for authentication, missing scopes, and lark-cli updates.
**REQUIRED SUB-SKILL:** Use lark-calendar for room and event semantics.

## Workflow

1. Run `scripts/manage_booking.py status` first.
2. If `pending_update` is `needs_approval`, immediately run `lark-cli update`; request elevation if required, verify with `lark-cli --version`, report the installed version and whether Skills changed, then run `clear-update`. Pause the requested operation until this succeeds.
3. Route view, cancel, and uninstall directly to their management commands. They do not require time ranges or Feishu authentication. Cancel/uninstall never delete existing Feishu events.
4. For create/replace, require exactly two HH:MM-HH:MM ranges. Reject invalid or overlapping ranges; allow touching boundaries.
5. Run auth status with verification. If user login is absent or expired, complete a fresh QR split-flow before continuing.
6. Calculate and show:
   - execution: next calendar day at 09:00 Asia/Shanghai
   - meeting date: execution date +2 days
   - both normalized ranges
   - priority: 7F-703/704 equal, other 7F, 6F, other 水滴大厦 floors
   - Mac awake/online requirement
7. If a plan exists, include its old ranges beside the new ranges.
8. Ask for one confirmation. This authorizes both unattended event writes and replacement when applicable.
9. Run `install` before every create/replace so the stable LaunchAgent and copied runtime match the current Skill; request filesystem/launchctl approval when required.
10. Create or `--replace` the plan. Do not wait until 09:00 in the interactive process.

Use:

~~~bash
python3 scripts/manage_booking.py install
python3 scripts/manage_booking.py create --slot "10:00-11:00" --slot "15:00-16:00"
python3 scripts/manage_booking.py create --replace --slot "12:00-13:00" --slot "17:00-18:00"
python3 scripts/manage_booking.py status
python3 scripts/manage_booking.py cancel
python3 scripts/manage_booking.py clear-update
python3 scripts/manage_booking.py uninstall
~~~

Do not pass a title, description, ordinary attendee, capacity, equipment, or another building. Ignore free/busy conflicts. There is no immediate-booking mode.

## Results

Report the pending execution and meeting dates after create/replace. For status, show each slot, the latest redacted result, and any pending update approval.

If lark-cli reports an update, finish the current operation, run lark-cli update, verify with lark-cli --version, and report the installed version and whether Skills were updated. Request approval immediately if elevation is required.
~~~~

Ensure any counters in the baseline report that do not apply are omitted; do not add hypothetical rules.

- [ ] **Step 2: Regenerate agents/openai.yaml**

Run:

~~~bash
python3 /Users/sd/.codex/skills/.system/skill-creator/scripts/generate_openai_yaml.py /Users/sd/Documents/Skills/booking-waterdrop-rooms --interface display_name="Book Waterdrop Rooms" --interface short_description="Schedule two Waterdrop room reservations" --interface default_prompt="Use $booking-waterdrop-rooms to schedule tomorrow's 09:00 booking for two non-overlapping Waterdrop Building meeting-room time slots."
~~~

Expected agents/openai.yaml:

~~~yaml
interface:
  display_name: "Book Waterdrop Rooms"
  short_description: "Schedule two Waterdrop room reservations"
  default_prompt: "Use $booking-waterdrop-rooms to schedule tomorrow's 09:00 booking for two non-overlapping Waterdrop Building meeting-room time slots."
~~~

- [ ] **Step 3: Add the Skill to README.md**

Add this bullet under Skills:

~~~markdown
- `booking-waterdrop-rooms`: Schedules a local macOS job that uses the logged-in Feishu user to reserve exactly two non-overlapping 水滴大厦 meeting-room slots at the next day's 09:00 release, with fixed room priority, bounded retries, replacement, cancellation, and local notifications.
~~~

Add this installation and invocation example after the existing one:

~~~text
Repository: Tsytt/Waterdrop
Path: booking-waterdrop-rooms

$booking-waterdrop-rooms 安排明天抢会议室：10:00-11:00、15:00-16:00
~~~

- [ ] **Step 4: Validate metadata and word count**

Run:

~~~bash
python3 /Users/sd/.codex/skills/.system/skill-creator/scripts/quick_validate.py /Users/sd/Documents/Skills/booking-waterdrop-rooms
wc -w booking-waterdrop-rooms/SKILL.md
~~~

Expected: validator succeeds; SKILL.md remains below 500 words.

- [ ] **Step 5: Commit the Skill interface**

~~~bash
git add booking-waterdrop-rooms/SKILL.md booking-waterdrop-rooms/agents/openai.yaml README.md
git commit -m "feat: add Waterdrop room booking skill"
~~~

---

### Task 7: Final Verification and Forward Testing

**Files:**
- Modify: booking-waterdrop-rooms/scripts/test_booking.py
- Create: docs/superpowers/evals/2026-07-13-booking-waterdrop-rooms-forward.md

**Interfaces:**
- Consumes: the complete Skill and scripts
- Produces: evidence that unit tests, Skill validation, safe integration checks, and fresh-context behavior tests pass

- [ ] **Step 1: Recheck production boundary regressions**

Confirm the suite still proves that non-`omm_` identifiers, rooms without a provable building, and rooms outside 水滴大厦 are rejected. Keep those checks in the initial adapter tests; production code must contain no test-specific exceptions.

- [ ] **Step 2: Run the complete deterministic test suite**

Run:

~~~bash
python3 -m unittest booking-waterdrop-rooms/scripts/test_booking.py -v
~~~

Expected: all tests PASS. The suite must not access Feishu, ~/Library/LaunchAgents, live launchctl state, or real osascript.

- [ ] **Step 3: Run structural and repository checks**

Run:

~~~bash
python3 /Users/sd/.codex/skills/.system/skill-creator/scripts/quick_validate.py /Users/sd/Documents/Skills/booking-waterdrop-rooms
git diff --check
git status --short
~~~

Expected: validator succeeds; no whitespace errors; unrelated user files remain unstaged.

- [ ] **Step 4: Perform read-only lark-cli integration checks**

Run auth verification first:

~~~bash
lark-cli auth status --json --verify
~~~

If authorization is missing, follow lark-shared's fresh QR split-flow and stop this turn until the user confirms authorization.

With valid user auth, run one read-only query:

~~~bash
READ_ONLY_DATE=$(TZ=Asia/Shanghai date -v+3d +%F)
lark-cli calendar +room-find --slot "${READ_ONLY_DATE}T10:00:00+08:00~${READ_ONLY_DATE}T10:30:00+08:00" --building "水滴大厦" --timezone "Asia/Shanghai" --as user
~~~

Expected: JSON success envelope under user identity. Verify that `normalize_room()` parses the actual room records and proves the 水滴大厦 boundary. If the live JSON shape differs from the tested shape, record the redacted mismatch in the forward report, mark the Skill not ready, and stop for a focused follow-up plan; do not improvise a parser relaxation.

Do not invoke calendar +create.

- [ ] **Step 5: Honor any lark-cli update notice**

After read-only checks finish, if a notice reports an update:

~~~bash
lark-cli update
lark-cli --version
~~~

If elevation is required, request it immediately. Report the installed CLI version and whether Skills were updated.

- [ ] **Step 6: Forward-test the completed Skill with fresh agents**

Use the five Task 1 scenarios, now explicitly loading the Skill. Run at least five fresh-context samples for the normal scheduling wording and compare against a no-Skill control. Do not permit external tool calls or writes.

Success criteria:

- Every sample requires exactly two non-overlapping ranges.
- Every sample limits candidates to 水滴大厦.
- Every sample uses user identity and preauthorizes future writes.
- Every replacement sample shows old/new values and asks once before replace.
- No sample invents immediate mode, other attendees, titles, or free/busy checks.
- Outputs converge on the same interaction shape.

- [ ] **Step 7: Write the forward-test report**

Create:

~~~markdown
# Booking Waterdrop Rooms Forward Evaluation

## Environment

- Skill path: booking-waterdrop-rooms/
- External writes allowed: no
- Unit tests: pass
- Skill validation: pass

## Control Comparison

Summarize the demonstrated baseline failures and the post-Skill result distribution.

## Scenario Results

For each scenario, include the raw response, pass/fail verdict, and exact violated invariant if any.

## Final Verdict

State whether the Skill is ready, or list the exact wording gap that must return to RED-GREEN-REFACTOR.
~~~

- [ ] **Step 8: Optional notification smoke test**

Request approval before invoking a real macOS notification. If approved, run:

~~~bash
PYTHONPATH=/Users/sd/Documents/Skills/booking-waterdrop-rooms/scripts python3 -c 'import run_booking; run_booking.notify_result({"status": "success", "slots": []})'
~~~

Expected: one synthetic “两个时间段均预订成功” notification. Do not install the real LaunchAgent and do not write a Feishu event.

- [ ] **Step 9: Commit final verification artifacts**

~~~bash
git add booking-waterdrop-rooms README.md docs/superpowers/evals/2026-07-13-booking-waterdrop-rooms-forward.md
git commit -m "test: verify Waterdrop room booking skill"
~~~

- [ ] **Step 10: Final verification before completion**

Run:

~~~bash
python3 -m unittest booking-waterdrop-rooms/scripts/test_booking.py -v
python3 /Users/sd/.codex/skills/.system/skill-creator/scripts/quick_validate.py /Users/sd/Documents/Skills/booking-waterdrop-rooms
git diff --check
git status --short
git log -8 --oneline --decorate
~~~

Expected:

- All tests pass.
- Skill validation succeeds.
- No task files are modified or unstaged.
- Unrelated pre-existing untracked files remain untouched.
- No real Feishu event was created.
