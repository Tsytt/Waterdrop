from __future__ import annotations

import contextlib
import io
import json
import os
import plistlib
import stat
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
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

    def launchctl_call(self, action):
        return (
            [
                "launchctl",
                action,
                manage._launch_domain(),
                str(self.plist_path),
            ],
            {"text": True, "capture_output": True},
        )

    def assert_no_install_artifacts(self):
        self.assertFalse((self.state_dir / "runtime").exists())
        self.assertFalse(self.plist_path.exists())
        self.assertEqual(list(self.state_dir.glob(".runtime.*")), [])
        if self.plist_path.parent.exists():
            self.assertEqual(
                list(self.plist_path.parent.glob(f".{self.plist_path.name}.*")),
                [],
            )

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
            calls.append((argv, kwargs))
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
        runtime_dir = self.state_dir / "runtime"
        manager = runtime_dir / "manage_booking.py"
        runner = runtime_dir / "run_booking.py"
        self.assertEqual(manager.read_text(), "# manager\n")
        self.assertEqual(runner.read_text(), "# runner\n")
        with self.plist_path.open("rb") as handle:
            plist = plistlib.load(handle)
        self.assertEqual(plist["StartCalendarInterval"]["Minute"], 59)
        self.assertEqual(
            stat.S_IMODE(self.state_dir.stat().st_mode),
            0o700,
        )
        self.assertEqual(stat.S_IMODE(runtime_dir.stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE((self.state_dir / "logs").stat().st_mode),
            0o700,
        )
        self.assertEqual(stat.S_IMODE(manager.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(runner.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.plist_path.stat().st_mode), 0o600)
        self.assertEqual(
            calls,
            [
                self.launchctl_call("bootout"),
                self.launchctl_call("bootstrap"),
            ],
        )

    def test_missing_runner_leaves_no_partial_install(self):
        (self.source_dir / "run_booking.py").unlink()
        calls = []

        with self.assertRaises(FileNotFoundError):
            manage.install_scheduler(
                self.source_dir,
                self.state_dir,
                self.plist_path,
                Path("/usr/bin/python3"),
                Path("/opt/homebrew/bin/lark-cli"),
                lambda argv, **kwargs: calls.append((argv, kwargs)),
            )

        self.assertEqual(calls, [])
        self.assert_no_install_artifacts()

    def test_copy_failure_leaves_no_partial_install(self):
        calls = []
        real_copy = manage.shutil.copy2

        def fail_runner_copy(source, destination):
            if Path(source).name == "run_booking.py":
                raise OSError("copy failed")
            return real_copy(source, destination)

        with mock.patch.object(
            manage.shutil,
            "copy2",
            side_effect=fail_runner_copy,
        ):
            with self.assertRaisesRegex(OSError, "copy failed"):
                manage.install_scheduler(
                    self.source_dir,
                    self.state_dir,
                    self.plist_path,
                    Path("/usr/bin/python3"),
                    Path("/opt/homebrew/bin/lark-cli"),
                    lambda argv, **kwargs: calls.append((argv, kwargs)),
                )

        self.assertEqual(calls, [])
        self.assert_no_install_artifacts()

    def test_bootstrap_failure_removes_new_install(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs))
            if argv[1] == "bootstrap":
                return mock.Mock(
                    returncode=5,
                    stdout="",
                    stderr="Input/output error",
                )
            return mock.Mock(returncode=0, stdout="", stderr="")

        with self.assertRaisesRegex(RuntimeError, "bootstrap failed"):
            manage.install_scheduler(
                self.source_dir,
                self.state_dir,
                self.plist_path,
                Path("/usr/bin/python3"),
                Path("/opt/homebrew/bin/lark-cli"),
                fake_run,
            )

        self.assertEqual(
            calls,
            [
                self.launchctl_call("bootout"),
                self.launchctl_call("bootstrap"),
                self.launchctl_call("bootout"),
            ],
        )
        self.assert_no_install_artifacts()

    def test_cleanup_bootout_failure_preserves_new_install_for_recovery(self):
        calls = []
        bootout_count = 0

        def fake_run(argv, **kwargs):
            nonlocal bootout_count
            calls.append((argv, kwargs))
            if argv[1] == "bootstrap":
                return mock.Mock(
                    returncode=5,
                    stdout="",
                    stderr="new bootstrap failed",
                )
            bootout_count += 1
            if bootout_count == 2:
                return mock.Mock(
                    returncode=5,
                    stdout="",
                    stderr="cleanup permission denied",
                )
            return mock.Mock(returncode=0, stdout="", stderr="")

        with self.assertRaises(RuntimeError) as caught:
            manage.install_scheduler(
                self.source_dir,
                self.state_dir,
                self.plist_path,
                Path("/usr/bin/python3"),
                Path("/opt/homebrew/bin/lark-cli"),
                fake_run,
            )

        message = str(caught.exception)
        self.assertIn("bootstrap failed", message)
        self.assertIn("cleanup bootout failed", message)
        self.assertIn("cleanup permission denied", message)
        runtime_dir = self.state_dir / "runtime"
        self.assertEqual(
            (runtime_dir / "manage_booking.py").read_text(),
            "# manager\n",
        )
        self.assertEqual(
            (runtime_dir / "run_booking.py").read_text(),
            "# runner\n",
        )
        self.assertTrue(self.plist_path.exists())
        self.assertEqual(list(self.state_dir.glob(".runtime.*.backup")), [])
        self.assertEqual(list(self.state_dir.glob(".runtime.*.new")), [])
        self.assertEqual(
            list(self.plist_path.parent.glob(f".{self.plist_path.name}.*.backup")),
            [],
        )
        self.assertEqual(
            list(self.plist_path.parent.glob(f".{self.plist_path.name}.*.new")),
            [],
        )
        self.assertEqual(
            calls,
            [
                self.launchctl_call("bootout"),
                self.launchctl_call("bootstrap"),
                self.launchctl_call("bootout"),
            ],
        )

    def test_failed_reinstall_restores_and_reloads_old_version(self):
        runtime_dir = self.state_dir / "runtime"
        runtime_dir.mkdir(parents=True)
        (runtime_dir / "manage_booking.py").write_text("old manager\n")
        (runtime_dir / "run_booking.py").write_text("old runner\n")
        old_plist = {
            "Label": manage.LABEL,
            "ProgramArguments": ["/old/python", "/old/runner"],
        }
        manage._write_plist(self.plist_path, old_plist)
        old_plist_bytes = self.plist_path.read_bytes()
        calls = []
        bootstrap_count = 0

        def fake_run(argv, **kwargs):
            nonlocal bootstrap_count
            calls.append((argv, kwargs))
            if argv[1] == "bootstrap":
                bootstrap_count += 1
                if bootstrap_count == 1:
                    return mock.Mock(
                        returncode=5,
                        stdout="",
                        stderr="new version failed",
                    )
            return mock.Mock(returncode=0, stdout="", stderr="")

        with self.assertRaisesRegex(RuntimeError, "bootstrap failed"):
            manage.install_scheduler(
                self.source_dir,
                self.state_dir,
                self.plist_path,
                Path("/usr/bin/python3"),
                Path("/opt/homebrew/bin/lark-cli"),
                fake_run,
            )

        self.assertEqual(
            (runtime_dir / "manage_booking.py").read_text(),
            "old manager\n",
        )
        self.assertEqual(
            (runtime_dir / "run_booking.py").read_text(),
            "old runner\n",
        )
        self.assertEqual(self.plist_path.read_bytes(), old_plist_bytes)
        self.assertEqual(
            calls,
            [
                self.launchctl_call("bootout"),
                self.launchctl_call("bootstrap"),
                self.launchctl_call("bootout"),
                self.launchctl_call("bootstrap"),
            ],
        )
        self.assertEqual(list(self.state_dir.glob(".runtime.*")), [])
        self.assertEqual(
            list(self.plist_path.parent.glob(f".{self.plist_path.name}.*")),
            [],
        )

    def test_cleanup_bootout_failure_preserves_old_backups_without_reload(self):
        runtime_dir = self.state_dir / "runtime"
        runtime_dir.mkdir(parents=True)
        (runtime_dir / "manage_booking.py").write_text("old manager\n")
        (runtime_dir / "run_booking.py").write_text("old runner\n")
        old_plist = {
            "Label": manage.LABEL,
            "ProgramArguments": ["/old/python", "/old/runner"],
        }
        manage._write_plist(self.plist_path, old_plist)
        old_plist_bytes = self.plist_path.read_bytes()
        calls = []
        bootout_count = 0
        bootstrap_count = 0

        def fake_run(argv, **kwargs):
            nonlocal bootout_count, bootstrap_count
            calls.append((argv, kwargs))
            if argv[1] == "bootstrap":
                bootstrap_count += 1
                if bootstrap_count == 1:
                    return mock.Mock(
                        returncode=5,
                        stdout="",
                        stderr="new bootstrap failed",
                    )
                return mock.Mock(returncode=0, stdout="", stderr="")
            bootout_count += 1
            if bootout_count == 2:
                return mock.Mock(
                    returncode=5,
                    stdout="",
                    stderr="cleanup input/output error",
                )
            return mock.Mock(returncode=0, stdout="", stderr="")

        with self.assertRaises(RuntimeError) as caught:
            manage.install_scheduler(
                self.source_dir,
                self.state_dir,
                self.plist_path,
                Path("/usr/bin/python3"),
                Path("/opt/homebrew/bin/lark-cli"),
                fake_run,
            )

        message = str(caught.exception)
        self.assertIn("bootstrap failed", message)
        self.assertIn("cleanup bootout failed", message)
        self.assertIn("cleanup input/output error", message)
        self.assertNotIn("restored", message)
        self.assertEqual(
            (runtime_dir / "manage_booking.py").read_text(),
            "# manager\n",
        )
        self.assertEqual(
            (runtime_dir / "run_booking.py").read_text(),
            "# runner\n",
        )
        with self.plist_path.open("rb") as handle:
            live_plist = plistlib.load(handle)
        self.assertEqual(
            live_plist["ProgramArguments"][1],
            str(runtime_dir / "run_booking.py"),
        )
        runtime_backups = list(self.state_dir.glob(".runtime.*.backup"))
        plist_backups = list(
            self.plist_path.parent.glob(f".{self.plist_path.name}.*.backup")
        )
        self.assertEqual(len(runtime_backups), 1)
        self.assertEqual(len(plist_backups), 1)
        self.assertEqual(
            (runtime_backups[0] / "manage_booking.py").read_text(),
            "old manager\n",
        )
        self.assertEqual(
            (runtime_backups[0] / "run_booking.py").read_text(),
            "old runner\n",
        )
        self.assertEqual(plist_backups[0].read_bytes(), old_plist_bytes)
        self.assertEqual(list(self.state_dir.glob(".runtime.*.new")), [])
        self.assertEqual(
            list(self.plist_path.parent.glob(f".{self.plist_path.name}.*.new")),
            [],
        )
        self.assertEqual(
            calls,
            [
                self.launchctl_call("bootout"),
                self.launchctl_call("bootstrap"),
                self.launchctl_call("bootout"),
            ],
        )

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
            lambda argv, **kwargs: mock.Mock(
                returncode=3,
                stdout="",
                stderr="Boot-out failed: 3: No such process",
            ),
        )
        self.assertEqual(result["status"], "uninstalled")
        self.assertTrue((self.state_dir / "history" / "result.json").exists())
        self.assertIsNone(manage.load_pending_update(self.state_dir))
        self.assertFalse(self.plist_path.exists())

    def test_uninstall_bootout_failure_preserves_all_state(self):
        runtime_dir = self.state_dir / "runtime"
        runtime_dir.mkdir(parents=True)
        (runtime_dir / "run_booking.py").write_text("runner\n")
        manage._atomic_write_json(
            manage.plan_path(self.state_dir),
            {"status": "pending"},
        )
        manage._atomic_write_json(
            manage.update_state_path(self.state_dir),
            {"status": "needs_approval"},
        )
        self.plist_path.parent.mkdir(parents=True)
        self.plist_path.write_text("plist")
        calls = []

        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs))
            return mock.Mock(
                returncode=5,
                stdout="",
                stderr="Boot-out failed: 5: Input/output error",
            )

        with self.assertRaisesRegex(RuntimeError, "bootout failed"):
            manage.uninstall_scheduler(
                self.state_dir,
                self.plist_path,
                fake_run,
            )

        self.assertEqual(calls, [self.launchctl_call("bootout")])
        self.assertTrue(self.plist_path.exists())
        self.assertTrue(manage.plan_path(self.state_dir).exists())
        self.assertTrue(manage.update_state_path(self.state_dir).exists())
        self.assertEqual(
            (runtime_dir / "run_booking.py").read_text(),
            "runner\n",
        )

    def test_management_cli_exposes_required_commands(self):
        parser = manage.build_parser()
        common = ["--state-dir", str(self.state_dir)]
        for command in ("install", "status", "cancel", "clear-update", "uninstall"):
            with self.subTest(command=command):
                self.assertEqual(parser.parse_args([*common, command]).command, command)
        created = parser.parse_args(
            [
                *common,
                "create",
                "--slot",
                "10:00-11:00",
                "--slot",
                "15:00-16:00",
            ]
        )
        self.assertEqual(created.command, "create")
        self.assertEqual(created.slot, ["10:00-11:00", "15:00-16:00"])

    def test_status_cli_emits_json_without_external_commands(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = manage.main(
                ["--state-dir", str(self.state_dir), "status"]
            )
        self.assertEqual(exit_code, 0)
        self.assertEqual(
            json.loads(output.getvalue()),
            {
                "pending": None,
                "latest_result": None,
                "pending_update": None,
            },
        )


import run_booking as runner


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


class ExecutionRetryTests(unittest.TestCase):
    def make_plan(self):
        return {
            "schema_version": 1,
            "plan_id": "plan-1",
            "execution_date": "2026-07-14",
            "target_date": "2026-07-16",
            "status": "pending",
            "slots": [
                {
                    "slot_id": "slot-1",
                    "start": "10:00",
                    "end": "11:00",
                    "status": "pending",
                },
                {
                    "slot_id": "slot-2",
                    "start": "15:00",
                    "end": "16:00",
                    "status": "pending",
                },
            ],
        }

    def test_corrupt_plan_is_non_retryable(self):
        plan = self.make_plan()
        plan["slots"] = plan["slots"][:1]

        with self.assertRaisesRegex(runner.LarkError, "invalid plan") as caught:
            runner.validate_plan_for_execution(plan)

        self.assertEqual(caught.exception.kind, "fatal")

    def test_room_conflict_falls_through_priority_queue(self):
        rooms = [
            runner.Room(
                "omm_703", "水滴大厦-7F-703", "水滴大厦", 7, {}
            ),
            runner.Room(
                "omm_705", "水滴大厦-7F-705", "水滴大厦", 7, {}
            ),
        ]
        client = FakeClient(
            [rooms],
            [runner.LarkError("taken", "room_conflict"), "evt_705"],
        )
        clock = FakeClock(datetime(2026, 7, 14, 9, 0, tzinfo=TZ))

        result = runner.reserve_slot(
            self.make_plan(),
            self.make_plan()["slots"][0],
            client,
            datetime(2026, 7, 14, 9, 0, 30, tzinfo=TZ),
            clock.now,
            clock.sleep,
        )

        self.assertEqual(result["event_id"], "evt_705")
        self.assertEqual(client.created, ["omm_703", "omm_705"])

    def test_empty_batches_follow_backoff_until_deadline(self):
        client = FakeClient([[], [], [], [], [], [], []], [])
        clock = FakeClock(datetime(2026, 7, 14, 9, 0, tzinfo=TZ))
        deadline = datetime(2026, 7, 14, 9, 0, 30, tzinfo=TZ)

        result = runner.reserve_slot(
            self.make_plan(),
            self.make_plan()["slots"][0],
            client,
            deadline,
            clock.now,
            clock.sleep,
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(clock.sleeps[:5], [0.5, 1, 2, 4, 5])
        self.assertLessEqual(clock.now(), deadline)

    def test_successful_slot_is_not_retried(self):
        slot = self.make_plan()["slots"][0]
        slot.update(status="success", event_id="evt_existing")
        client = FakeClient([], [])
        clock = FakeClock(datetime(2026, 7, 14, 9, 0, tzinfo=TZ))

        result = runner.reserve_slot(
            self.make_plan(),
            slot,
            client,
            datetime(2026, 7, 14, 9, 0, 30, tzinfo=TZ),
            clock.now,
            clock.sleep,
        )

        self.assertEqual(result["event_id"], "evt_existing")
        self.assertEqual(client.created, [])

    def test_room_result_arriving_after_deadline_is_not_created(self):
        clock = FakeClock(datetime(2026, 7, 14, 9, 0, 29, tzinfo=TZ))
        room = runner.Room(
            "omm_703", "水滴大厦-7F-703", "水滴大厦", 7, {}
        )

        class SlowFindClient(FakeClient):
            def room_find(self, start_iso, end_iso):
                with clock.lock:
                    clock.current += timedelta(seconds=2)
                return [room]

        client = SlowFindClient([], ["evt_too_late"])

        result = runner.reserve_slot(
            self.make_plan(),
            self.make_plan()["slots"][0],
            client,
            datetime(2026, 7, 14, 9, 0, 30, tzinfo=TZ),
            clock.now,
            clock.sleep,
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(client.created, [])

    def test_ambiguous_create_adopts_matching_existing_event(self):
        room = runner.Room(
            "omm_703", "水滴大厦-7F-703", "水滴大厦", 7, {}
        )

        class RecoveringClient(FakeClient):
            def find_matching_event(self, start_iso, end_iso, room_id):
                return "evt_recovered"

        client = RecoveringClient(
            [[room]], [runner.LarkError("request timed out", "ambiguous")]
        )
        fixed_now = datetime(2026, 7, 14, 9, 0, tzinfo=TZ)

        result = runner.reserve_slot(
            self.make_plan(),
            self.make_plan()["slots"][0],
            client,
            fixed_now,
            lambda: fixed_now,
            lambda seconds: self.fail("unexpected sleep"),
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["event_id"], "evt_recovered")


class ExecutionConcurrencyTests(unittest.TestCase):
    def make_plan(self):
        return {
            "schema_version": 1,
            "plan_id": "plan-1",
            "execution_date": "2026-07-14",
            "target_date": "2026-07-16",
            "status": "pending",
            "slots": [
                {
                    "slot_id": "slot-1",
                    "start": "10:00",
                    "end": "11:00",
                    "status": "pending",
                },
                {
                    "slot_id": "slot-2",
                    "start": "15:00",
                    "end": "16:00",
                    "status": "pending",
                },
            ],
        }

    def test_start_after_deadline_marks_missed_without_create(self):
        client = FakeClient([], [])
        fixed_now = datetime(2026, 7, 14, 9, 0, 31, tzinfo=TZ)

        result = runner.execute_plan_in_memory(
            self.make_plan(),
            client,
            lambda: fixed_now,
            lambda seconds: self.fail("unexpected sleep"),
        )

        self.assertEqual(result["status"], "missed")
        self.assertEqual(client.created, [])

    def test_two_slots_start_concurrently_and_may_reuse_room(self):
        barrier = threading.Barrier(2)
        room = runner.Room(
            "omm_703", "水滴大厦-7F-703", "水滴大厦", 7, {}
        )

        class BarrierClient:
            def room_find(self, start_iso, end_iso):
                return [room]

            def create_event(self, start_iso, end_iso, room_id):
                barrier.wait(timeout=1)
                return f"evt_{start_iso}"

            def find_matching_event(self, start_iso, end_iso, room_id):
                return None

        fixed_now = datetime(2026, 7, 14, 9, 0, tzinfo=TZ)

        result = runner.execute_plan_in_memory(
            self.make_plan(),
            BarrierClient(),
            lambda: fixed_now,
            lambda seconds: self.fail("unexpected sleep"),
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(
            [slot["room_id"] for slot in result["slots"]],
            ["omm_703", "omm_703"],
        )

    def test_partial_success_is_not_rolled_back(self):
        barrier = threading.Barrier(2)
        room = runner.Room(
            "omm_703", "水滴大厦-7F-703", "水滴大厦", 7, {}
        )

        class TimeKeyedClient:
            def room_find(self, start_iso, end_iso):
                return [room]

            def create_event(self, start_iso, end_iso, room_id):
                barrier.wait(timeout=1)
                if "T15:00" in start_iso:
                    raise runner.LarkError("permission denied", "fatal")
                return "evt_1"

            def find_matching_event(self, start_iso, end_iso, room_id):
                return None

        fixed_now = datetime(2026, 7, 14, 9, 0, tzinfo=TZ)

        result = runner.execute_plan_in_memory(
            self.make_plan(),
            TimeKeyedClient(),
            lambda: fixed_now,
            lambda seconds: self.fail("unexpected sleep"),
        )

        self.assertEqual(result["status"], "partial")
        self.assertEqual(
            [slot["status"] for slot in result["slots"]],
            ["success", "failed"],
        )

    def test_successful_slot_is_not_submitted_again(self):
        plan = self.make_plan()
        plan["slots"][0].update(
            status="success",
            event_id="evt_existing",
            room_id="omm_existing",
        )
        calls = []

        class PendingOnlyClient:
            def room_find(self, start_iso, end_iso):
                calls.append(start_iso)
                raise runner.LarkError("permission denied", "fatal")

        fixed_now = datetime(2026, 7, 14, 9, 0, tzinfo=TZ)

        result = runner.execute_plan_in_memory(
            plan,
            PendingOnlyClient(),
            lambda: fixed_now,
            lambda seconds: self.fail("unexpected sleep"),
        )

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["slots"][0]["event_id"], "evt_existing")
        self.assertEqual(len(calls), 1)
        self.assertIn("T15:00", calls[0])

    def test_launch_before_window_sleeps_until_exactly_0900(self):
        barrier = threading.Barrier(2)
        room = runner.Room(
            "omm_703", "水滴大厦-7F-703", "水滴大厦", 7, {}
        )
        clock = FakeClock(datetime(2026, 7, 14, 8, 59, 55, tzinfo=TZ))

        class BarrierClient:
            def room_find(self, start_iso, end_iso):
                return [room]

            def create_event(self, start_iso, end_iso, room_id):
                barrier.wait(timeout=1)
                return f"evt_{start_iso}"

            def find_matching_event(self, start_iso, end_iso, room_id):
                return None

        result = runner.execute_plan_in_memory(
            self.make_plan(), BarrierClient(), clock.now, clock.sleep
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(clock.sleeps, [5.0])


class LogSafetyTests(unittest.TestCase):
    def test_redaction_removes_credentials_from_keys_and_text(self):
        redacted = runner.redact(
            {
                "access_token": "token-secret",
                "appSecret": "app-secret",
                "message": (
                    "Bearer abc.def.ghi device_code=device-secret "
                    "verification_url=https://auth.example/verify?code=url-secret"
                ),
                "event_id": "evt_safe",
            }
        )

        rendered = json.dumps(redacted)
        for secret in (
            "token-secret",
            "app-secret",
            "abc.def.ghi",
            "device-secret",
            "url-secret",
        ):
            self.assertNotIn(secret, rendered)
        self.assertIn("evt_safe", rendered)

    def test_append_log_sets_private_modes_and_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log_dir = root / "logs"
            now = datetime(2026, 7, 14, 9, 1, tzinfo=TZ)
            runner.append_log(
                log_dir,
                now,
                {"event_id": "evt_safe", "access_token": "secret"},
            )
            log_path = log_dir / "2026-07-14.jsonl"

            self.assertEqual(stat.S_IMODE(log_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(log_path.stat().st_mode), 0o600)
            self.assertNotIn("secret", log_path.read_text())

            log_path.unlink()
            target = root / "outside.txt"
            target.write_text("unchanged")
            log_path.symlink_to(target)
            with self.assertRaises(OSError):
                runner.append_log(log_dir, now, {"event_id": "evt_new"})
            self.assertEqual(target.read_text(), "unchanged")

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


class ExecutionPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.temp.name) / "state"
        self.now = datetime(2026, 7, 14, 9, 0, tzinfo=TZ)
        self.plan = {
            "schema_version": 1,
            "plan_id": "plan-persist",
            "execution_date": "2026-07-14",
            "target_date": "2026-07-16",
            "status": "pending",
            "slots": [
                {
                    "slot_id": "slot-1",
                    "start": "10:00",
                    "end": "11:00",
                    "status": "pending",
                },
                {
                    "slot_id": "slot-2",
                    "start": "15:00",
                    "end": "16:00",
                    "status": "pending",
                },
            ],
        }
        manage._atomic_write_json(
            manage.plan_path(self.state_dir), self.plan
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_due_plan_archives_before_notifying_and_removes_pending(self):
        barrier = threading.Barrier(2)
        room = runner.Room(
            "omm_703", "水滴大厦-7F-703", "水滴大厦", 7, {}
        )

        class SuccessClient:
            update_notice = False

            def __init__(self):
                self.auth_checked = False

            def auth_status(self):
                self.auth_checked = True

            def room_find(self, start_iso, end_iso):
                return [room]

            def create_event(self, start_iso, end_iso, room_id):
                barrier.wait(timeout=1)
                return f"evt_{start_iso}"

            def find_matching_event(self, start_iso, end_iso, room_id):
                return None

        client = SuccessClient()
        notifications = []

        def notifier(result):
            notifications.append(result["status"])
            self.assertFalse(manage.plan_path(self.state_dir).exists())
            history = self.state_dir / "history" / "plan-persist.json"
            self.assertEqual(json.loads(history.read_text())["status"], "success")

        result = runner.execute_due_plan(
            self.state_dir,
            self.now,
            client,
            lambda: self.now,
            lambda seconds: self.fail("unexpected sleep"),
            notifier,
        )

        self.assertTrue(client.auth_checked)
        self.assertEqual(result["status"], "success")
        self.assertEqual(notifications, ["success"])
        self.assertTrue((self.state_dir / "logs" / "2026-07-14.jsonl").exists())

    def test_success_is_persisted_before_other_worker_crashes(self):
        barrier = threading.Barrier(2)
        room = runner.Room(
            "omm_703", "水滴大厦-7F-703", "水滴大厦", 7, {}
        )

        class CrashingClient:
            update_notice = False

            def auth_status(self):
                return None

            def room_find(self, start_iso, end_iso):
                return [room]

            def create_event(self, start_iso, end_iso, room_id):
                barrier.wait(timeout=1)
                if "T15:00" in start_iso:
                    raise RuntimeError("simulated process failure")
                return "evt_durable"

            def find_matching_event(self, start_iso, end_iso, room_id):
                return None

        with self.assertRaisesRegex(RuntimeError, "simulated process failure"):
            runner.execute_due_plan(
                self.state_dir,
                self.now,
                CrashingClient(),
                lambda: self.now,
                lambda seconds: self.fail("unexpected sleep"),
                lambda result: self.fail("notification must not run"),
            )

        pending = manage.load_plan(self.state_dir)
        self.assertEqual(pending["slots"][0]["status"], "success")
        self.assertEqual(pending["slots"][0]["event_id"], "evt_durable")
        self.assertEqual(pending["slots"][1]["status"], "pending")
        self.assertFalse((self.state_dir / "history").exists())

    def test_fatal_auth_error_is_archived_without_secret_text(self):
        class AuthFailureClient:
            update_notice = False

            def auth_status(self):
                raise runner.LarkError(
                    "Bearer credential-secret device_code=device-secret",
                    "fatal",
                )

        result = runner.execute_due_plan(
            self.state_dir,
            self.now,
            AuthFailureClient(),
            lambda: self.now,
            lambda seconds: self.fail("unexpected sleep"),
            lambda result: None,
        )

        rendered = json.dumps(result)
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("credential-secret", rendered)
        self.assertNotIn("device-secret", rendered)
        self.assertFalse(manage.plan_path(self.state_dir).exists())

    def test_non_due_plan_is_left_untouched(self):
        result = runner.execute_due_plan(
            self.state_dir,
            datetime(2026, 7, 13, 9, 0, tzinfo=TZ),
            object(),
            lambda: self.now,
            lambda seconds: self.fail("unexpected sleep"),
            lambda result: self.fail("unexpected notification"),
        )

        self.assertEqual(result, {"status": "idle"})
        self.assertTrue(manage.plan_path(self.state_dir).exists())


class NotificationUpdateTests(unittest.TestCase):
    def test_booking_notification_uses_argv_and_status_summary(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs))
            return mock.Mock(returncode=0, stdout="", stderr="")

        runner.notify_result({"status": "partial"}, fake_run)

        argv, kwargs = calls[0]
        self.assertEqual(argv[:2], ["/usr/bin/osascript", "-e"])
        self.assertIn("一个时间段预订成功", argv[2])
        self.assertEqual(kwargs, {"text": True, "capture_output": True})

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

    def test_deferred_update_without_notice_runs_nothing(self):
        client = FakeClient([], [])
        calls = []

        result = runner.perform_deferred_update(
            client, lambda argv, **kwargs: calls.append(argv)
        )

        self.assertIsNone(result)
        self.assertEqual(calls, [])

    def test_failed_update_requests_later_interactive_approval(self):
        client = FakeClient([], [])
        client.update_notice = True
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return mock.Mock(
                returncode=1,
                stdout="",
                stderr="permission denied secret=raw-secret",
            )

        result = runner.perform_deferred_update(client, fake_run)

        self.assertEqual(result["status"], "needs_approval")
        self.assertNotIn("raw-secret", json.dumps(result))
        self.assertEqual([call[-1] for call in calls], ["update"])

    def test_update_notification_contains_version_and_skills_state(self):
        calls = []

        runner.notify_update_result(
            {
                "status": "updated",
                "version": "1.2.3",
                "skills_updated": "unknown",
            },
            lambda argv, **kwargs: calls.append((argv, kwargs)),
        )

        argv, kwargs = calls[0]
        self.assertEqual(argv[:2], ["/usr/bin/osascript", "-e"])
        self.assertIn("1.2.3", argv[2])
        self.assertIn("unknown", argv[2])
        self.assertEqual(kwargs, {"text": True, "capture_output": True})


class RunnerCliTests(unittest.TestCase):
    def test_parser_requires_state_directory(self):
        parser = runner.build_parser()

        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args([])
        parsed = parser.parse_args(["--state-dir", "/tmp/booking-state"])
        self.assertEqual(parsed.state_dir, Path("/tmp/booking-state"))

    def test_missing_lark_cli_notifies_logs_and_returns_2(self):
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch.dict(os.environ, {}, clear=True),
                mock.patch.object(runner.shutil, "which", return_value=None),
                mock.patch.object(
                    runner,
                    "notify_result",
                    side_effect=lambda result: calls.append(
                        ("notify", result["error"])
                    ),
                ),
                mock.patch.object(
                    runner,
                    "append_log",
                    side_effect=lambda log_dir, now, record: calls.append(
                        ("log", record["error"])
                    ),
                ),
            ):
                exit_code = runner.main(["--state-dir", directory])

        self.assertEqual(exit_code, 2)
        self.assertEqual(
            calls,
            [
                ("notify", "lark-cli missing"),
                ("log", "lark-cli missing"),
            ],
        )

    def test_update_is_deferred_until_after_booking_notification(self):
        calls = []

        class MainClient:
            binary = Path("/fake/lark-cli")
            update_notice = True

        def fake_execute(
            state_dir, now, client, clock, sleeper, notifier
        ):
            calls.append("execute")
            notifier({"status": "partial"})
            return {"status": "partial"}

        def fake_update(client):
            calls.append("update")
            return {
                "status": "needs_approval",
                "error": "interactive approval is required for lark-cli update",
            }

        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            with (
                mock.patch.dict(
                    os.environ,
                    {"LARK_CLI_BIN": "/fake/lark-cli"},
                    clear=True,
                ),
                mock.patch.object(runner, "LarkClient", return_value=MainClient()),
                mock.patch.object(
                    runner, "execute_due_plan", side_effect=fake_execute
                ),
                mock.patch.object(
                    runner,
                    "notify_result",
                    side_effect=lambda result: calls.append("booking-notify"),
                ),
                mock.patch.object(
                    runner,
                    "perform_deferred_update",
                    side_effect=fake_update,
                ),
                mock.patch.object(
                    runner,
                    "notify_update_result",
                    side_effect=lambda update: calls.append("update-notify"),
                ),
                mock.patch.object(
                    runner,
                    "append_log",
                    side_effect=lambda log_dir, now, record: calls.append(
                        "update-log"
                    ),
                ),
            ):
                exit_code = runner.main(
                    ["--state-dir", str(state_dir)]
                )

            pending = manage.load_pending_update(state_dir)

        self.assertEqual(exit_code, 0)
        self.assertEqual(pending["status"], "needs_approval")
        self.assertEqual(
            calls,
            [
                "execute",
                "booking-notify",
                "update",
                "update-notify",
                "update-log",
            ],
        )

    def test_successful_update_clears_pending_state_and_failed_run_exits_1(self):
        class MainClient:
            binary = Path("/fake/lark-cli")
            update_notice = True

        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            manage._atomic_write_json(
                manage.update_state_path(state_dir),
                {"status": "needs_approval"},
            )
            with (
                mock.patch.dict(
                    os.environ,
                    {"LARK_CLI_BIN": "/fake/lark-cli"},
                    clear=True,
                ),
                mock.patch.object(runner, "LarkClient", return_value=MainClient()),
                mock.patch.object(
                    runner,
                    "execute_due_plan",
                    return_value={"status": "failed"},
                ),
                mock.patch.object(
                    runner,
                    "perform_deferred_update",
                    return_value={
                        "status": "updated",
                        "version": "1.2.3",
                        "skills_updated": False,
                    },
                ),
                mock.patch.object(runner, "notify_update_result"),
                mock.patch.object(runner, "append_log"),
            ):
                exit_code = runner.main(
                    ["--state-dir", str(state_dir)]
                )

            pending_exists = manage.update_state_path(state_dir).exists()

        self.assertEqual(exit_code, 1)
        self.assertFalse(pending_exists)


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
        self.assertEqual(
            [item["room_id"] for item in records],
            ["omm_704", "omm_603"],
        )

    def test_ranks_waterdrop_rooms_and_rejects_other_buildings(self):
        records = [
            {
                "room_id": "omm_other",
                "room_name": "铭丰大厦-7F-703",
                "building_name": "铭丰大厦",
                "floor_name": "7F",
            },
            {
                "room_id": "omm_six",
                "room_name": "水滴大厦-6F-601",
                "building_name": "水滴大厦",
                "floor_name": "6F",
            },
            {
                "room_id": "omm_seven",
                "room_name": "水滴大厦-7F-705",
                "building_name": "水滴大厦",
                "floor_name": "7F",
            },
            {
                "room_id": "omm_preferred",
                "room_name": "水滴大厦-7F-704",
                "building_name": "水滴大厦",
                "floor_name": "7F",
            },
            {
                "room_id": "omm_other_floor",
                "room_name": "水滴大厦-5F-501",
                "building_name": "水滴大厦",
                "floor_name": "5F",
            },
        ]
        rooms = [
            room
            for record in records
            if (room := runner.normalize_room(record))
        ]
        ranked = runner.rank_rooms(rooms)
        self.assertEqual(
            [room.room_id for room in ranked],
            ["omm_preferred", "omm_seven", "omm_six", "omm_other_floor"],
        )

    def test_703_and_704_preserve_api_order(self):
        records = [
            {
                "room_id": "omm_704",
                "room_name": "水滴大厦-7F-704",
                "building_name": "水滴大厦",
                "floor_name": "7F",
            },
            {
                "room_id": "omm_703",
                "room_name": "水滴大厦-7F-703",
                "building_name": "水滴大厦",
                "floor_name": "7F",
            },
        ]
        rooms = [runner.normalize_room(record) for record in records]
        self.assertEqual(
            [room.room_id for room in runner.rank_rooms(rooms)],
            ["omm_704", "omm_703"],
        )

    def test_rejects_record_that_cannot_prove_building(self):
        record = {"room_id": "omm_unknown", "room_name": "7F-703"}
        self.assertIsNone(runner.normalize_room(record))

    def test_rejects_building_name_substring_fallback(self):
        record = {
            "room_id": "omm_imposter",
            "room_name": "非水滴大厦-7F-703",
        }
        self.assertIsNone(runner.normalize_room(record))

    def test_rejects_conflicting_building_metadata_and_room_name(self):
        record = {
            "room_id": "omm_conflict",
            "room_name": "铭丰大厦-7F-703",
            "building_name": "水滴大厦",
        }
        self.assertIsNone(runner.normalize_room(record))

    def test_rejects_conflicting_authoritative_building_fields(self):
        record = {
            "room_id": "omm_conflict",
            "room_name": "水滴大厦-7F-703",
            "building_name": "水滴大厦",
            "building_display_name": "铭丰大厦",
        }
        self.assertIsNone(runner.normalize_room(record))

    def test_accepts_anchored_building_name_fallback(self):
        record = {
            "room_id": "omm_fallback",
            "room_name": "水滴大厦-7F-703",
        }
        room = runner.normalize_room(record)
        self.assertIsNotNone(room)
        self.assertEqual(room.building, "水滴大厦")

    def test_rejects_non_resource_id(self):
        record = {
            "room_id": "703",
            "room_name": "水滴大厦-7F-703",
            "building_name": "水滴大厦",
        }
        self.assertIsNone(runner.normalize_room(record))


class LarkCommandTests(unittest.TestCase):
    def test_rejects_non_object_json_envelope_as_fatal(self):
        client = runner.LarkClient(
            Path("lark-cli"),
            lambda argv, **kwargs: mock.Mock(
                returncode=0,
                stdout=json.dumps([]),
                stderr="",
            ),
        )

        with self.assertRaises(runner.LarkError) as caught:
            client.room_find(
                "2026-07-16T10:00:00+08:00",
                "2026-07-16T11:00:00+08:00",
            )
        self.assertEqual(caught.exception.kind, "fatal")

    def test_rejects_non_object_error_envelope_as_fatal(self):
        client = runner.LarkClient(
            Path("lark-cli"),
            lambda argv, **kwargs: mock.Mock(
                returncode=1,
                stdout="",
                stderr=json.dumps(
                    {
                        "ok": False,
                        "identity": "user",
                        "error": "malformed",
                    }
                ),
            ),
        )

        with self.assertRaises(runner.LarkError) as caught:
            client.room_find(
                "2026-07-16T10:00:00+08:00",
                "2026-07-16T11:00:00+08:00",
            )
        self.assertEqual(caught.exception.kind, "fatal")

    def test_auth_status_requires_explicit_verified_user_identity(self):
        invalid_payloads = {
            "missing identity": {
                "ok": True,
                "data": {"verified": True},
            },
            "missing verified": {
                "ok": True,
                "identity": "user",
                "data": {"identity": "user"},
            },
            "bot identity": {
                "ok": True,
                "identity": "bot",
                "data": {"identity": "bot", "verified": True},
            },
            "verification failed": {
                "ok": True,
                "identity": "user",
                "data": {"identity": "user", "verified": False},
            },
        }

        for case, payload in invalid_payloads.items():
            with self.subTest(case=case):
                client = runner.LarkClient(
                    Path("lark-cli"),
                    lambda argv, payload=payload, **kwargs: mock.Mock(
                        returncode=0,
                        stdout=json.dumps(payload),
                        stderr="",
                    ),
                )
                with self.assertRaises(runner.LarkError) as caught:
                    client.auth_status()
                self.assertEqual(caught.exception.kind, "fatal")

    def test_auth_status_accepts_explicit_verified_user_identity(self):
        payload = {
            "ok": True,
            "identity": "user",
            "data": {"identity": "user", "verified": True},
        }
        client = runner.LarkClient(
            Path("lark-cli"),
            lambda argv, **kwargs: mock.Mock(
                returncode=0,
                stdout=json.dumps(payload),
                stderr="",
            ),
        )

        self.assertIsNone(client.auth_status())

    def test_room_find_uses_user_identity_and_waterdrop_filter(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return mock.Mock(
                returncode=0,
                stdout=json.dumps(
                    {
                        "ok": True,
                        "identity": "user",
                        "data": {"rooms": []},
                    }
                ),
                stderr="",
            )

        client = runner.LarkClient(
            Path("/opt/homebrew/bin/lark-cli"), fake_run
        )
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
                stdout=json.dumps(
                    {
                        "ok": True,
                        "identity": "user",
                        "data": {"event_id": "evt_1"},
                    }
                ),
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
        self.assertEqual(
            calls[0][calls[0].index("--attendee-ids") + 1],
            "omm_704",
        )

    def test_create_rejects_invalid_room_ids_before_runner(self):
        for room_id in ("", "ou_person"):
            with self.subTest(room_id=room_id):
                calls = []

                def fake_run(argv, **kwargs):
                    calls.append(argv)
                    return mock.Mock(
                        returncode=0,
                        stdout=json.dumps(
                            {
                                "ok": True,
                                "identity": "user",
                                "data": {"event_id": "evt_1"},
                            }
                        ),
                        stderr="",
                    )

                client = runner.LarkClient(Path("lark-cli"), fake_run)
                with self.assertRaises(runner.LarkError) as caught:
                    client.create_event(
                        "2026-07-16T10:00:00+08:00",
                        "2026-07-16T11:00:00+08:00",
                        room_id,
                    )
                self.assertEqual(caught.exception.kind, "fatal")
                self.assertEqual(calls, [])

    def test_ambiguous_create_recovery_requires_exact_time_and_room(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return mock.Mock(
                returncode=0,
                stdout=json.dumps(
                    {
                        "ok": True,
                        "identity": "user",
                        "data": {
                            "events": [
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
                            ]
                        },
                    }
                ),
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


if __name__ == "__main__":
    unittest.main()
