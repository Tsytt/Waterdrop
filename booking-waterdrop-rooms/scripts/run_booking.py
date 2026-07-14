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
import uuid
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
NOTIFICATION_TIMEOUT = 10
BUILDING_PREFIX_RE = re.compile(rf"^{re.escape(BUILDING)}(?:-|$)")
ROOM_BUILDING_RE = re.compile(r"^([^-]*大厦)(?:-|$)")
FLOOR_RE = re.compile(
    r"(?:^|[-\s])(\d{1,2})F(?:[-\s]|$)", re.IGNORECASE
)
SENSITIVE_KEY_RE = re.compile(
    r"token|secret|authorization|device[\s_-]*code|"
    r"verification[\s_-]*(?:url|uri)",
    re.IGNORECASE,
)
BEARER_RE = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+")
SENSITIVE_TEXT_RE = re.compile(
    r"(?i)\b("
    r"access[_-]?token|refresh[_-]?token|app[_-]?secret|client[_-]?secret|"
    r"token|secret|authorization|device(?:[_\s-]+)code|"
    r"verification[_-]?(?:url|uri)(?:[_-]complete)?"
    r")(\s*(?:[:=]|\bis\b)\s*|\s+)([^\s,;]+)"
)
URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
AUTH_URL_HINT_RE = re.compile(
    r"auth|oauth|authorize|verify|verification|device[_-]?code",
    re.IGNORECASE,
)
SAFE_SLOT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
ROOM_ID_RE = re.compile(r"^omm_[A-Za-z0-9_-]+$")
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
HISTORY_ALLOWED_KEYS = frozenset(
    {*PLAN_ALLOWED_KEYS, "completed_at"}
)
SKILLS_UPDATED_RE = re.compile(
    r"^\s*(?:"
    r"skills?\s+(?:were\s+)?updated|"
    r"updated\s+(?:ai\s+)?skills?|"
    r"skills?\s+updated\s*:\s*true"
    r")[.!]?\s*$",
    re.IGNORECASE | re.MULTILINE,
)
SKILLS_UNCHANGED_RE = re.compile(
    r"^\s*(?:"
    r"0\s+skills?\s+updated|"
    r"skills?\s+updated\s*:\s*false|"
    r"skills?\s+(?:are\s+)?already\s+up\s+to\s+date|"
    r"no\s+(?:ai\s+)?skills?\s+updates?|"
    r"skills?\s+(?:are\s+)?unchanged|"
    r"skills?\s+(?:were\s+)?not\s+updated"
    r")[.!]?\s*$",
    re.IGNORECASE | re.MULTILINE,
)


class LarkError(RuntimeError):
    def __init__(self, message: str, kind: str = "fatal"):
        super().__init__(message)
        self.kind = kind


def safe_error(exc: "LarkError") -> str:
    return {
        "room_conflict": "room no longer available",
        "safe_retry": "temporary rate limit",
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
        without_labeled_secrets = SENSITIVE_TEXT_RE.sub(
            lambda match: (
                f"{match.group(1)}{match.group(2)}[REDACTED]"
            ),
            without_bearer,
        )
        return URL_RE.sub(
            lambda match: (
                "[REDACTED URL]"
                if AUTH_URL_HINT_RE.search(match.group(0))
                else match.group(0)
            ),
            without_labeled_secrets,
        )
    return value


def append_log(log_dir: Path, now: datetime, record: dict) -> None:
    if log_dir.is_symlink():
        raise OSError("refusing to use a symbolic-link log directory")
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
    if log_dir.is_symlink():
        raise OSError("refusing to use a symbolic-link log directory")
    if not log_dir.exists():
        return
    cutoff = now.timestamp() - retention_days * 24 * 60 * 60
    for entry in log_dir.iterdir():
        if entry.is_symlink() or not entry.is_file():
            continue
        if entry.stat().st_mtime < cutoff:
            entry.unlink()


def _nonempty_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _waterdrop_room(room_id: object, room_name: object) -> bool:
    return (
        isinstance(room_id, str)
        and ROOM_ID_RE.fullmatch(room_id) is not None
        and isinstance(room_name, str)
        and BUILDING_PREFIX_RE.match(room_name) is not None
    )


def validate_plan_for_execution(plan: dict) -> None:
    try:
        if not isinstance(plan, dict) or set(plan) != PLAN_ALLOWED_KEYS:
            raise ValueError("invalid plan fields")
        plan_id = plan["plan_id"]
        if not isinstance(plan_id, str) or str(uuid.UUID(plan_id)) != plan_id:
            raise ValueError("invalid plan id")
        created_at = datetime.fromisoformat(plan["created_at"])
        if created_at.tzinfo is None:
            raise ValueError("created_at must include a timezone")
        execution = date.fromisoformat(plan["execution_date"])
        target = date.fromisoformat(plan["target_date"])
        slots = plan["slots"]
        if not isinstance(slots, list) or len(slots) != 2:
            raise ValueError("invalid slots")
        if any(
            not isinstance(slot, dict)
            or set(slot) != SLOT_ALLOWED_KEYS
            for slot in slots
        ):
            raise ValueError("invalid slot fields")
        values = [f"{slot['start']}-{slot['end']}" for slot in slots]
        slot_ids = [slot["slot_id"] for slot in slots]
        manage.validate_ranges(values)
        statuses_valid = True
        for slot in slots:
            slot_status = slot["status"]
            room_id = slot["room_id"]
            room_name = slot["room_name"]
            event_id = slot["event_id"]
            error = slot["error"]
            if slot_status == "pending":
                state_valid = (
                    room_id is None
                    and room_name is None
                    and event_id is None
                    and error is None
                )
            elif slot_status == "uncertain":
                state_valid = (
                    _waterdrop_room(room_id, room_name)
                    and event_id is None
                    and _nonempty_text(error)
                )
            elif slot_status == "success":
                state_valid = (
                    _waterdrop_room(room_id, room_name)
                    and _nonempty_text(event_id)
                    and error is None
                )
            elif slot_status == "failed":
                state_valid = (
                    room_id is None
                    and room_name is None
                    and event_id is None
                    and _nonempty_text(error)
                )
            else:
                state_valid = False
            statuses_valid = statuses_valid and state_valid
        valid = (
            type(plan["schema_version"]) is int
            and plan["schema_version"] == 1
            and plan["status"] == "pending"
            and target == execution + timedelta(days=2)
            and len(set(slot_ids)) == 2
            and all(
                isinstance(slot_id, str)
                and SAFE_SLOT_ID_RE.fullmatch(slot_id) is not None
                for slot_id in slot_ids
            )
            and statuses_valid
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        valid = False
    if not valid:
        raise LarkError("invalid plan state", "fatal")


def _canonical_slot(slot: dict) -> dict:
    return redact({key: slot.get(key) for key in SLOT_ALLOWED_KEYS})


def _canonical_history(result: dict) -> dict:
    payload = {
        key: result.get(key)
        for key in HISTORY_ALLOWED_KEYS
        if key != "slots"
    }
    payload["slots"] = [
        _canonical_slot(slot)
        for slot in result.get("slots", [])
        if isinstance(slot, dict)
    ]
    return redact(payload)


def _pending_plan_from_result(plan: dict, result: dict) -> dict:
    slots = []
    for slot in result["slots"]:
        canonical = _canonical_slot(slot)
        if canonical["status"] == "missed":
            canonical.update(
                status="failed",
                room_id=None,
                room_name=None,
                event_id=None,
                error=canonical["error"] or "execution window missed",
            )
        slots.append(canonical)
    pending = {
        key: plan[key]
        for key in PLAN_ALLOWED_KEYS
        if key not in {"status", "slots"}
    }
    pending["status"] = "pending"
    pending["slots"] = slots
    validate_plan_for_execution(pending)
    return pending


def _history_path(state_dir: Path, plan_id: str) -> Path:
    try:
        if str(uuid.UUID(plan_id)) != plan_id:
            raise ValueError("invalid plan id")
    except (TypeError, ValueError, AttributeError) as exc:
        raise LarkError("history path is not confined", "fatal") from exc
    history_dir = state_dir / "history"
    if history_dir.is_symlink():
        raise LarkError("history path is not confined", "fatal")
    resolved_state = state_dir.resolve(strict=False)
    resolved_history = history_dir.resolve(strict=False)
    candidate = history_dir / f"{plan_id}.json"
    if (
        resolved_history.parent != resolved_state
        or candidate.resolve(strict=False).parent != resolved_history
    ):
        raise LarkError("history path is not confined", "fatal")
    return candidate


def slot_iso(plan: dict, slot: dict) -> tuple[str, str]:
    target = date.fromisoformat(plan["target_date"])
    start = datetime.combine(target, time.fromisoformat(slot["start"]), TZ)
    end = datetime.combine(target, time.fromisoformat(slot["end"]), TZ)
    return start.isoformat(), end.isoformat()


def _recovered_slot(slot: dict, event_id: str) -> dict:
    return {
        **slot,
        "status": "success",
        "event_id": event_id,
        "error": None,
    }


def _uncertain_slot(slot: dict) -> dict:
    updated = dict(slot)
    updated["status"] = "uncertain"
    updated["event_id"] = None
    updated["error"] = updated.get("error") or safe_error(
        LarkError("uncertain", "ambiguous")
    )
    return updated


def _recover_uncertain_once(plan: dict, slot: dict, client) -> dict:
    start_iso, end_iso = slot_iso(plan, slot)
    updated = _uncertain_slot(slot)
    try:
        event_id = client.find_matching_event(
            start_iso, end_iso, updated["room_id"]
        )
        if event_id:
            return _recovered_slot(updated, event_id)
    except LarkError as exc:
        updated["error"] = safe_error(exc)
    return updated


def _recover_uncertain_slot(
    plan: dict,
    slot: dict,
    client,
    deadline: datetime,
    clock,
    sleeper,
) -> dict:
    start_iso, end_iso = slot_iso(plan, slot)
    backoff_index = 0
    updated = _uncertain_slot(slot)
    while clock() <= deadline:
        try:
            event_id = client.find_matching_event(
                start_iso, end_iso, updated["room_id"]
            )
            if event_id:
                return _recovered_slot(updated, event_id)
        except LarkError as exc:
            updated["error"] = safe_error(exc)
            if exc.kind == "fatal":
                return updated
        delay = BACKOFF[min(backoff_index, len(BACKOFF) - 1)]
        remaining = (deadline - clock()).total_seconds()
        if remaining <= 0:
            break
        sleeper(min(delay, remaining))
        backoff_index += 1
    return updated


def reserve_slot(
    plan: dict,
    slot: dict,
    client,
    deadline: datetime,
    clock,
    sleeper,
    on_slot_uncertain=lambda slot: None,
) -> dict:
    validate_plan_for_execution(plan)
    if not isinstance(slot, dict):
        raise LarkError("invalid plan state", "fatal")
    matching_slots = [
        stored
        for stored in plan["slots"]
        if stored["slot_id"] == slot.get("slot_id")
    ]
    if len(matching_slots) != 1 or matching_slots[0] != slot:
        raise LarkError("invalid plan state", "fatal")
    slot = matching_slots[0]
    if slot.get("status") == "success":
        return dict(slot)
    if slot.get("status") == "failed":
        return dict(slot)
    if slot.get("status") == "uncertain":
        return _recover_uncertain_slot(
            plan, slot, client, deadline, clock, sleeper
        )
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
                    if exc.kind in {"ambiguous", "transient"}:
                        uncertain = {
                            **slot,
                            "status": "uncertain",
                            "room_id": room.room_id,
                            "room_name": room.room_name,
                            "event_id": None,
                            "error": safe_error(
                                LarkError("uncertain", "ambiguous")
                            ),
                        }
                        on_slot_uncertain(dict(uncertain))
                        return _recover_uncertain_slot(
                            plan,
                            uncertain,
                            client,
                            deadline,
                            clock,
                            sleeper,
                        )
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
    on_slot_uncertain=lambda slot: None,
) -> dict:
    validate_plan_for_execution(plan)
    current = clock()
    execution_date = date.fromisoformat(plan["execution_date"])
    start = datetime.combine(execution_date, time(9, 0), TZ)
    deadline = datetime.combine(execution_date, time(9, 0, 30), TZ)
    if current > deadline:
        if any(
            slot.get("status") == "uncertain"
            for slot in plan["slots"]
        ):
            ordered = []
            for slot in plan["slots"]:
                if slot.get("status") == "uncertain":
                    updated = _recover_uncertain_once(plan, slot, client)
                    if updated.get("status") == "success":
                        on_slot_success(updated)
                    ordered.append(updated)
                elif slot.get("status") == "pending":
                    ordered.append(
                        {
                            **slot,
                            "status": "missed",
                            "error": "execution window missed",
                        }
                    )
                else:
                    ordered.append(dict(slot))
            successes = sum(
                slot["status"] == "success" for slot in ordered
            )
            if successes == 2:
                status = "success"
            elif successes == 1:
                status = "partial"
            else:
                status = "failed"
            return {**plan, "status": status, "slots": ordered}
        return {
            **plan,
            "status": "missed",
            "slots": [
                {
                    **slot,
                    "status": "missed",
                    "error": "execution window missed",
                }
                if slot.get("status") not in {
                    "success",
                    "uncertain",
                    "failed",
                }
                else slot
                for slot in plan["slots"]
            ],
        }
    if current < start:
        sleeper((start - current).total_seconds())
    pending = [
        slot
        for slot in plan["slots"]
        if slot.get("status") in {"pending", "uncertain"}
    ]
    results = {
        slot["slot_id"]: dict(slot)
        for slot in plan["slots"]
        if slot.get("status") in {"success", "failed"}
    }

    def run_slot(slot: dict) -> dict:
        updated = reserve_slot(
            plan,
            slot,
            client,
            deadline,
            clock,
            sleeper,
            on_slot_uncertain=on_slot_uncertain,
        )
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
        try:
            plan = manage.load_plan(state_dir)
        except ValueError as exc:
            raise LarkError("invalid plan state", "fatal") from exc
        if not plan:
            return {"status": "idle"}
        validate_plan_for_execution(plan)
        history = _history_path(state_dir, plan["plan_id"])
        local_date = now.astimezone(TZ).date()
        execution_date = date.fromisoformat(plan["execution_date"])
        has_uncertain = any(
            slot.get("status") == "uncertain"
            for slot in plan["slots"]
        )
        if local_date < execution_date or (
            local_date > execution_date and not has_uncertain
        ):
            return {"status": "idle"}
        poll_window = datetime.combine(
            execution_date,
            time(8, 59),
            TZ,
        )
        if local_date == execution_date and now.astimezone(TZ) < poll_window:
            return {"status": "idle"}

        persistence_lock = threading.Lock()

        def persist_slot(updated_slot: dict) -> None:
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
                on_slot_success=persist_slot,
                on_slot_uncertain=persist_slot,
            )
        except LarkError as exc:
            reason = safe_error(exc)
            result = {
                **plan,
                "status": "failed",
                "slots": [
                    slot
                    if slot.get("status")
                    in {"success", "uncertain", "failed"}
                    else {**slot, "status": "failed", "error": reason}
                    for slot in plan["slots"]
                ],
            }
        result["completed_at"] = clock().isoformat()
        unresolved = any(
            slot.get("status") == "uncertain"
            for slot in result["slots"]
        )
        if unresolved:
            pending = _pending_plan_from_result(plan, result)
            manage._atomic_write_json(manage.plan_path(state_dir), pending)
        result = _canonical_history(result)
        manage._atomic_write_json(history, result)
        if not unresolved:
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
    try:
        completed = run_command(
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
            timeout=NOTIFICATION_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LarkError("booking notification failed", "fatal") from exc
    if getattr(completed, "returncode", 1) != 0:
        raise LarkError("booking notification failed", "fatal")


def _update_needs_approval() -> dict:
    return {
        "status": "needs_approval",
        "error": "interactive approval is required for lark-cli update",
    }


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
        return _update_needs_approval()
    if update.returncode != 0:
        return _update_needs_approval()
    try:
        version = run_command(
            [str(client.binary), "--version"],
            text=True,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return _update_needs_approval()
    installed_version = str(getattr(version, "stdout", "") or "").strip()
    if version.returncode != 0 or not installed_version:
        return _update_needs_approval()
    update_output = f"{update.stdout}\n{update.stderr}".lower()
    if SKILLS_UNCHANGED_RE.search(update_output):
        skills_updated = False
    elif SKILLS_UPDATED_RE.search(update_output):
        skills_updated = True
    else:
        skills_updated = "unknown"
    return {
        "status": "updated",
        "version": installed_version,
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
    try:
        completed = run_command(
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
            timeout=NOTIFICATION_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LarkError("update notification failed", "fatal") from exc
    if getattr(completed, "returncode", 1) != 0:
        raise LarkError("update notification failed", "fatal")


def _persist_update_result(state_dir: Path, update: dict) -> None:
    with manage.locked_state(state_dir):
        if update["status"] == "needs_approval":
            manage._atomic_write_json(
                manage.update_state_path(state_dir), update
            )
        else:
            manage.update_state_path(state_dir).unlink(missing_ok=True)


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
        value = self.room_name.split(marker, 1)[-1]
        return re.sub(r"\(\d+\)$", "", value)


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


def _attendee_records(payload: object) -> list[dict]:
    records: list[dict] = []

    def visit(node: object) -> None:
        if isinstance(node, dict):
            attendees = node.get("attendees")
            if isinstance(attendees, list):
                records.extend(
                    attendee
                    for attendee in attendees
                    if isinstance(attendee, dict)
                )
            for key, value in node.items():
                if key != "attendees":
                    visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(payload)
    return records


def _event_has_room(payload: object, room_id: str) -> bool:
    for attendee in _attendee_records(payload):
        attendee_type = str(
            attendee.get("type") or attendee.get("attendee_type") or ""
        ).lower()
        attendee_id = (
            attendee.get("room_id")
            or attendee.get("attendee_id")
            or attendee.get("id")
        )
        if attendee_type == "resource" and attendee_id == room_id:
            return True
    return False


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


def _classify_lark_error(
    subtype: str,
    message: str,
    transport_kind: str,
    unknown_error_kind: str,
    code: object = None,
    status: object = None,
) -> str:
    def is_exact_429(value: object) -> bool:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and value == 429
        ) or (
            isinstance(value, str) and value.strip() == "429"
        )

    if is_exact_429(code) or is_exact_429(status):
        return "safe_retry"
    normalized_subtype = re.sub(r"[_-]+", " ", subtype.lower())
    normalized_message = re.sub(r"[_-]+", " ", message.lower())
    normalized = re.sub(
        r"[_-]+", " ", f"{subtype} {message}".lower()
    )
    terminal_markers = (
        "auth",
        "permission",
        "forbidden",
        "unauthorized",
        "invalid",
        "parameter",
        "argument",
        "bad request",
    )
    if any(marker in normalized_subtype for marker in terminal_markers):
        return "fatal"
    if any(
        marker in normalized_message
        for marker in (
            "permission denied",
            "authentication failed",
            "not authorized",
            "unauthorized",
            "forbidden",
            "invalid parameter",
            "invalid argument",
            "bad request",
        )
    ):
        return "fatal"
    if any(
        marker in normalized
        for marker in ("rate limit", "too many requests")
    ):
        return "safe_retry"
    if any(
        marker in normalized
        for marker in ("conflict", "occupied", "already booked")
    ) or (
        "room" in normalized
        and any(
            marker in normalized
            for marker in ("not available", "no longer available")
        )
    ):
        return "room_conflict"
    if any(
        marker in normalized
        for marker in (
            "network",
            "timeout",
            "reset",
            "socket",
            "connection",
            "transport",
            "unavailable",
            "internal server",
        )
    ):
        return transport_kind
    return unknown_error_kind


class LarkClient:
    def __init__(self, binary: Path, run_command=subprocess.run):
        self.binary = binary
        self.run_command = run_command
        self.update_notice = False

    def _json(
        self,
        args: list[str],
        timeout_kind: str = "transient",
        protocol_kind: str = "fatal",
        transport_kind: str = "transient",
        unknown_error_kind: str = "fatal",
    ) -> dict:
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
        except (FileNotFoundError, PermissionError) as exc:
            raise LarkError("lark-cli could not be started", "fatal") from exc
        except OSError as exc:
            raise LarkError(
                "lark-cli transport failed", transport_kind
            ) from exc
        text = result.stdout if result.returncode == 0 else result.stderr
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LarkError(
                "lark-cli returned non-JSON output", protocol_kind
            ) from exc
        if not isinstance(payload, dict):
            raise LarkError(
                "lark-cli returned a non-object JSON envelope",
                protocol_kind,
            )
        if (
            isinstance(payload.get("_notice"), dict)
            and payload["_notice"].get("update")
        ):
            self.update_notice = True
        if "error" in payload and not isinstance(payload["error"], dict):
            raise LarkError(
                "lark-cli returned a malformed error envelope",
                protocol_kind,
            )
        if result.returncode != 0 or payload.get("ok") is not True:
            error = payload.get("error", {})
            message = str(error.get("message") or "lark-cli command failed")
            subtype = str(error.get("subtype") or error.get("type") or "")
            kind = _classify_lark_error(
                subtype,
                message,
                transport_kind,
                unknown_error_kind,
                error.get("code"),
                error.get("status"),
            )
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
        try:
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
                protocol_kind="ambiguous",
                transport_kind="ambiguous",
                unknown_error_kind="ambiguous",
            )
        except LarkError as exc:
            if exc.kind in {"transient", "ambiguous"}:
                raise LarkError(
                    "event creation result is uncertain", "ambiguous"
                ) from exc
            raise
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
                event_id = str(record["event_id"])
                detail = self._json(
                    [
                        "calendar",
                        "+get",
                        "--event-id",
                        event_id,
                        "--as",
                        "user",
                    ]
                )
                if _event_has_room(detail, room_id):
                    return event_id
        return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run-booking")
    parser.add_argument("--state-dir", type=Path, required=True)
    return parser


def _preserved_slots(state_dir: Path) -> list[dict]:
    for loader in (manage.load_plan, manage.latest_result):
        try:
            state = loader(state_dir)
        except Exception:
            continue
        if isinstance(state, dict) and isinstance(state.get("slots"), list):
            redacted = redact(state["slots"])
            if isinstance(redacted, list):
                return [slot for slot in redacted if isinstance(slot, dict)]
    return []


def _runtime_failure_result(state_dir: Path) -> dict:
    return {
        "status": "failed",
        "slots": _preserved_slots(state_dir),
        "error": "booking runtime failure",
    }


def _best_effort_report(state_dir: Path, result: dict) -> None:
    try:
        notify_result(result)
    except Exception:
        pass
    try:
        append_log(
            state_dir / "logs",
            datetime.now(TZ),
            {
                "stage": "runtime-failure",
                "status": "failed",
                "error": "booking runtime failure",
                "slots": result.get("slots", []),
            },
        )
    except Exception:
        pass


def _run_main(args: argparse.Namespace) -> int:
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
        _persist_update_result(args.state_dir, update)
        append_log(
            args.state_dir / "logs",
            datetime.now(TZ),
            {"stage": "lark-cli-update", **update},
        )
        notify_update_result(update)
    return 0 if result["status"] in {"idle", "success", "partial"} else 1


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _run_main(args)
    except Exception:
        result = _runtime_failure_result(args.state_dir)
        _best_effort_report(args.state_dir, result)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
