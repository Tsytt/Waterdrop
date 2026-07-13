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
FLOOR_RE = re.compile(
    r"(?:^|[-\s])(\d{1,2})F(?:[-\s]|$)", re.IGNORECASE
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
        if (
            isinstance(payload.get("_notice"), dict)
            and payload["_notice"].get("update")
        ):
            self.update_notice = True
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

    def create_event(
        self,
        start_iso: str,
        end_iso: str,
        room_id: str,
    ) -> str:
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
