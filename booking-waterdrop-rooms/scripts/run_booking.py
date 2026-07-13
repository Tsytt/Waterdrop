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
BACKOFF = (0.5, 1, 2, 4, 5)
BUILDING_PREFIX_RE = re.compile(rf"^{re.escape(BUILDING)}(?:-|$)")
ROOM_BUILDING_RE = re.compile(r"^([^-]*大厦)(?:-|$)")
FLOOR_RE = re.compile(
    r"(?:^|[-\s])(\d{1,2})F(?:[-\s]|$)", re.IGNORECASE
)
SENSITIVE_KEY_RE = re.compile(
    r"token|secret|authorization|device[_-]?code|verification[_-]?(?:url|uri)",
    re.IGNORECASE,
)
BEARER_RE = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+")
SENSITIVE_TEXT_RE = re.compile(
    r"(?i)\b("
    r"access[_-]?token|refresh[_-]?token|app[_-]?secret|client[_-]?secret|"
    r"token|secret|authorization|device[_-]?code|"
    r"verification[_-]?(?:url|uri)(?:_complete)?"
    r")(\s*[:=]\s*)([^\s,;]+)"
)


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


def redact(value: object) -> object:
    if isinstance(value, dict):
        return {
            str(key): (
                "[REDACTED]"
                if SENSITIVE_KEY_RE.search(str(key))
                else redact(child)
            )
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [redact(child) for child in value]
    if isinstance(value, tuple):
        return [redact(child) for child in value]
    if isinstance(value, str):
        without_bearer = BEARER_RE.sub("Bearer [REDACTED]", value)
        return SENSITIVE_TEXT_RE.sub(
            lambda match: (
                f"{match.group(1)}{match.group(2)}[REDACTED]"
            ),
            without_bearer,
        )
    return value


def append_log(log_dir: Path, now: datetime, record: dict) -> None:
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(log_dir, 0o700)
    path = log_dir / f"{now.astimezone(TZ).date().isoformat()}.jsonl"
    if path.is_symlink():
        raise OSError("refusing to append through a symbolic link")
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
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
                if clock() > deadline:
                    break
                try:
                    event_id = client.create_event(
                        start_iso, end_iso, room.room_id
                    )
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
                        return {
                            **slot,
                            "status": "failed",
                            "error": last_error,
                        }
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
                {
                    **slot,
                    "status": "missed",
                    "error": "execution window missed",
                }
                if slot.get("status") != "success"
                else slot
                for slot in plan["slots"]
            ],
        }
    if current < start:
        sleeper((start - current).total_seconds())
    pending = [
        slot for slot in plan["slots"] if slot.get("status") != "success"
    ]
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
    if successes == 2:
        status = "success"
    elif successes == 1:
        status = "partial"
    else:
        status = "failed"
    return {**plan, "status": status, "slots": ordered}


def execute_due_plan(
    state_dir: Path,
    now: datetime,
    client,
    clock,
    sleeper,
    notifier,
) -> dict:
    with manage.locked_state(state_dir):
        plan = manage.load_plan(state_dir)
        local_date = now.astimezone(TZ).date().isoformat()
        if not plan or plan["execution_date"] != local_date:
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
                    slot
                    if slot.get("status") == "success"
                    else {**slot, "status": "failed", "error": reason}
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
            (
                f"display notification "
                f"{json.dumps(summary, ensure_ascii=False)} with title "
                f"{json.dumps(title, ensure_ascii=False)}"
            ),
        ],
        text=True,
        capture_output=True,
    )


def perform_deferred_update(
    client: "LarkClient", run_command=subprocess.run
) -> dict | None:
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
        skills_updated: bool | str = "unknown"
    elif (
        "already up to date" in update_output
        or "no skill" in update_output
    ):
        skills_updated = False
    else:
        skills_updated = True
    return {
        "status": "updated",
        "version": version.stdout.strip(),
        "skills_updated": skills_updated,
    }


def notify_update_result(
    update: dict, run_command=subprocess.run
) -> None:
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
            (
                f"display notification "
                f"{json.dumps(detail, ensure_ascii=False)} with title "
                f"{json.dumps('lark-cli 更新', ensure_ascii=False)}"
            ),
        ],
        text=True,
        capture_output=True,
    )


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


def _building_value(record: dict) -> str | None:
    has_authoritative_value = False
    for key in ("building_name", "building", "building_display_name"):
        if key not in record or record[key] in (None, ""):
            continue
        has_authoritative_value = True
        if not isinstance(record[key], str) or record[key] != BUILDING:
            return None

    room_name = str(record.get("room_name", ""))
    name_building = ROOM_BUILDING_RE.match(room_name)
    if (
        has_authoritative_value
        and name_building
        and name_building.group(1) != BUILDING
    ):
        return None
    if has_authoritative_value or BUILDING_PREFIX_RE.match(room_name):
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
    indexed = list(
        enumerate(room for room in rooms if room.building == BUILDING)
    )
    indexed.sort(key=lambda pair: (_rank(pair[1]), pair[0]))
    return [room for _, room in indexed]


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
            raise LarkError(
                "lark-cli command timed out", timeout_kind
            ) from exc
        text = result.stdout if result.returncode == 0 else result.stderr
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LarkError("lark-cli returned non-JSON output") from exc
        if not isinstance(payload, dict):
            raise LarkError("lark-cli returned a non-object JSON envelope")
        if (
            isinstance(payload.get("_notice"), dict)
            and payload["_notice"].get("update")
        ):
            self.update_notice = True
        if "error" in payload and not isinstance(payload["error"], dict):
            raise LarkError("lark-cli returned a malformed error envelope")
        if result.returncode != 0 or payload.get("ok") is not True:
            error = payload.get("error", {})
            message = str(error.get("message") or "lark-cli command failed")
            subtype = str(error.get("subtype") or error.get("type") or "")
            lowered = f"{subtype} {message}".lower()
            if "rate" in lowered or "429" in lowered or "network" in lowered:
                kind = "transient"
            elif (
                "conflict" in lowered
                or "occupied" in lowered
                or "available" in lowered
            ):
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
        if identity != "user" or verified is not True:
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

    def create_event(
        self,
        start_iso: str,
        end_iso: str,
        room_id: str,
    ) -> str:
        if not isinstance(room_id, str) or not room_id.startswith("omm_"):
            raise LarkError("invalid Feishu room resource id")
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
            raise LarkError(
                "create succeeded without an event_id", "ambiguous"
            )
        return event_id

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
            if _same_instant(
                record.get("start"), start_iso
            ) and _same_instant(record.get("end"), end_iso):
                return str(record["event_id"])
        return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run-booking")
    parser.add_argument("--state-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    binary = os.environ.get("LARK_CLI_BIN") or shutil.which("lark-cli")
    if not binary:
        result = {
            "status": "failed",
            "slots": [],
            "error": "lark-cli missing",
        }
        notify_result(result)
        append_log(
            args.state_dir / "logs",
            datetime.now(TZ),
            {
                "stage": "preflight",
                "status": "failed",
                "error": "lark-cli missing",
            },
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
