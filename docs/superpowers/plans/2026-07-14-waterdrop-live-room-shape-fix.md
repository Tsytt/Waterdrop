# Waterdrop Live Room Shape Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the fixed 703/704 priority work with the live Feishu room-name shape `水滴大厦-7F-703(10)` without weakening the 水滴大厦 boundary.

**Architecture:** Keep the existing fail-closed building proof and floor parsing unchanged. Normalize only the terminal numeric capacity suffix in `Room.short_name`, then let the existing exact preferred-name set and stable ranking logic operate as designed.

**Tech Stack:** Python 3 standard library, `unittest`, existing `run_booking.py` adapter.

## Global Constraints

- Use the redacted live response shape observed from the read-only `calendar +room-find` check on 2026-07-14.
- Do not call `calendar +create`, `launchctl`, or `osascript`.
- Do not accept another building or infer 水滴大厦 from an unanchored substring.
- Preserve equal rank and API order for 703/704.
- Change no scheduling, retry, persistence, notification, or update behavior.

---

### Task 1: Normalize the Live Capacity Suffix

**Files:**
- Modify: `booking-waterdrop-rooms/scripts/test_booking.py`
- Modify: `booking-waterdrop-rooms/scripts/run_booking.py`

**Interfaces:**
- Consumes: `collect_room_records(payload)`, `normalize_room(record)`, and `rank_rooms(rooms)`
- Produces: `Room.short_name` without a terminal numeric capacity suffix such as `(10)`

- [ ] **Step 1: Add the failing live-shape regression test**

Add this test to `RoomRankingTests`:

```python
def test_live_room_names_with_capacity_suffix_keep_fixed_priority(self):
    payload = {
        "ok": True,
        "identity": "user",
        "data": {
            "time_slots": [{
                "start": "2026-07-16T10:00:00+08:00",
                "end": "2026-07-16T10:30:00+08:00",
                "meeting_rooms": [
                    {"room_id": "omm_706", "room_name": "水滴大厦-7F-706(6)", "capacity": 6},
                    {"room_id": "omm_805", "room_name": "水滴大厦-8F-805(14)", "capacity": 14},
                    {"room_id": "omm_703", "room_name": "水滴大厦-7F-703(10)", "capacity": 10},
                    {"room_id": "omm_704", "room_name": "水滴大厦-7F-704(10)", "capacity": 10},
                ],
            }],
        },
    }
    rooms = [
        room
        for record in runner.collect_room_records(payload)
        if (room := runner.normalize_room(record))
    ]
    self.assertTrue(all(room.building == "水滴大厦" for room in rooms))
    self.assertEqual(
        [room.room_id for room in runner.rank_rooms(rooms)],
        ["omm_703", "omm_704", "omm_706", "omm_805"],
    )
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
python3 -m unittest booking-waterdrop-rooms.scripts.test_booking.RoomRankingTests.test_live_room_names_with_capacity_suffix_keep_fixed_priority -v
```

Expected: FAIL because the current order starts with `omm_706`; the live names produce short names `7F-703(10)` and `7F-704(10)`, which do not equal the exact preferred names.

- [ ] **Step 3: Implement the minimal source normalization**

Change only `Room.short_name`:

```python
@property
def short_name(self) -> str:
    marker = f"{BUILDING}-"
    value = self.room_name.split(marker, 1)[-1]
    return re.sub(r"\(\d+\)$", "", value)
```

The anchored numeric suffix preserves all other names and does not participate in proving the building.

- [ ] **Step 4: Verify GREEN and regressions**

Run:

```bash
python3 -m unittest booking-waterdrop-rooms.scripts.test_booking.RoomRankingTests -v
python3 -m unittest booking-waterdrop-rooms/scripts/test_booking.py -q
PYTHONPYCACHEPREFIX=/tmp/booking-pycache python3 -m py_compile booking-waterdrop-rooms/scripts/run_booking.py booking-waterdrop-rooms/scripts/test_booking.py
git diff --check
```

Expected: the focused class passes, the full suite reports 104 tests and `OK`, syntax succeeds, and the diff check prints nothing.

- [ ] **Step 5: Commit the focused fix**

```bash
git add booking-waterdrop-rooms/scripts/run_booking.py booking-waterdrop-rooms/scripts/test_booking.py docs/superpowers/plans/2026-07-14-waterdrop-live-room-shape-fix.md
git commit -m "fix: rank live Waterdrop room names"
```
