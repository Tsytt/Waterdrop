---
name: booking-waterdrop-rooms
description: Use when a macOS user wants to schedule, replace, view, cancel, or uninstall a next-day 09:00 Feishu meeting-room booking for exactly two non-overlapping time slots in 水滴大厦, including requests mentioning 定时抢会议室, 703, 704, 7楼, or 6楼.
---

# Booking Waterdrop Rooms

Use only the logged-in Feishu user identity; never use bot identity.

**REQUIRED SUB-SKILL:** Use lark-shared for authentication, missing scopes, and lark-cli updates.
**REQUIRED SUB-SKILL:** Use lark-calendar for room and event semantics.

## Workflow

1. Run `python3 scripts/manage_booking.py status` first.
2. If `pending_update.status` is `needs_approval`, pause the requested operation, run `lark-cli update` (request elevation immediately if required), verify with `lark-cli --version`, report the installed version and whether Skills changed, then run `python3 scripts/manage_booking.py clear-update`.
3. Route view/status, cancel, clear-update, and uninstall directly to their management commands. These operations require neither time ranges nor Feishu authentication. Cancel and uninstall never delete existing Feishu events.
4. For create or replace, require exactly two `HH:MM-HH:MM` ranges. Reject missing, extra, invalid, identical, or overlapping ranges; touching boundaries are allowed.
5. Verify the Feishu user login. If absent or expired, complete lark-shared's fresh QR split-flow before continuing.
6. Preview:
   - execution: next calendar day at 09:00 Asia/Shanghai;
   - meeting date: execution date plus two calendar days;
   - both normalized ranges;
   - priority: 7F-703 and 7F-704 equally, other 7F, 6F, then other 水滴大厦 floors;
   - the Mac must be awake and online.
7. If a plan exists, show its old ranges beside the new ranges.
8. Ask once for confirmation. It preauthorizes both unattended Feishu event writes and, when applicable, replacement of the pending plan.
9. Run `install` before every create/replace so the LaunchAgent and copied runtime are current. Request filesystem or launchctl approval when required.
10. Save the plan with `create` or `create --replace`; do not wait interactively for 09:00.

```bash
python3 scripts/manage_booking.py install
python3 scripts/manage_booking.py create --slot "10:00-11:00" --slot "15:00-16:00"
python3 scripts/manage_booking.py create --replace --slot "12:00-13:00" --slot "17:00-18:00"
python3 scripts/manage_booking.py status
python3 scripts/manage_booking.py cancel
python3 scripts/manage_booking.py clear-update
python3 scripts/manage_booking.py uninstall
```

Limit all rooms to 水滴大厦. Do not pass a title or summary (Feishu then displays `无主题`), description, ordinary attendee, capacity, or equipment. Add only the selected room resource and ignore all free/busy conflicts. There is no immediate mode.

## Results

After create/replace, report the pending execution date, meeting date, and both ranges. For status, show both slots, the latest redacted result, and any pending update approval.

If any current lark-cli operation reports an update, finish the requested operation, update lark-cli, verify its version, and report whether Skills changed. Request elevation immediately if required.
