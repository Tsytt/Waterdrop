# Booking Waterdrop Rooms Skill Design

## Summary

Create a Codex Skill named `booking-waterdrop-rooms` that schedules a one-time, local macOS job to reserve exactly two non-overlapping Feishu meeting-room time slots. The user supplies only the two time ranges. The Skill schedules the next calendar day's 09:00 run; that run reserves rooms for its own calendar date plus two days.

The Skill uses the logged-in Feishu user identity, creates untitled events owned by that user, adds only the selected room as a resource attendee, and never invites other people. It ignores conflicts on the user's calendar.

All room candidates must belong to 水滴大厦. Selection order is:

1. `7F-703` and `7F-704`, at equal priority
2. Other 7F rooms
3. 6F rooms
4. Other floors in 水滴大厦

Rooms in other buildings are never valid fallbacks.

## Goals

- Accept exactly two time ranges when the user invokes the Skill.
- Schedule a reliable local run for the following day at 09:00 Asia/Shanghai.
- Reserve both time ranges for the run date plus two calendar days.
- Maximize success through deterministic priority handling, concurrent slot processing, and bounded retries.
- Support installing the scheduler, creating or replacing a plan, viewing status, canceling a plan, and uninstalling the scheduler.
- Use the Feishu user's existing `lark-cli` login without copying credentials.
- Report success, partial success, failure, or a missed execution window through a macOS notification and redacted local logs.

## Non-goals

- Cloud or server deployment
- Immediate booking outside the scheduled 09:00 run
- Recurring reservation plans
- More or fewer than two time ranges
- Inviting attendees other than the selected meeting room
- Checking the user's or anyone else's free/busy state
- Choosing rooms outside 水滴大厦
- Creating titled events, descriptions, or custom meeting metadata
- Waking or powering on a sleeping Mac

## Time Rules

All calculations use `Asia/Shanghai`.

- Invocation date: the day the user creates the plan
- Execution date: invocation date plus one calendar day
- Execution time: 09:00 on the execution date
- Target meeting date: execution date plus two calendar days

Example:

- Skill invoked: 2026-07-13
- Scheduled run: 2026-07-14 09:00
- Meeting-room target date: 2026-07-16

The two input ranges apply to the target meeting date. Each range must have an end later than its start. The ranges must not overlap; touching boundaries are valid, so `10:00-11:00` and `11:00-12:00` are allowed. The Skill rejects missing, extra, invalid, identical, or overlapping ranges before saving a plan.

## User Interaction

### Create a plan

Typical request:

> 帮我安排明天抢会议室，时间是 10:00-11:00 和 15:00-16:00。

Before saving, the Skill must show:

- Execution date and time
- Target meeting date
- Both normalized time ranges
- The fixed room-priority policy
- Whether an existing plan will be replaced
- The requirement that the Mac be awake and online

The user's confirmation authorizes both future Feishu event writes. The unattended 09:00 run must not wait for another room-choice or write confirmation.

### Replace a plan

Only one plan may exist for a given execution date. If another plan already exists, show the old and new ranges and require confirmation before replacing it. Persist the replacement atomically so a crash cannot leave a partial plan.

### View, cancel, and uninstall

The Skill supports requests to:

- View the current pending plan and the latest result
- Cancel a pending plan
- Uninstall the LaunchAgent and remove pending state

Uninstalling must not delete Feishu events that were already created.

## Architecture

The repository will contain:

```text
booking-waterdrop-rooms/
├── SKILL.md
├── agents/openai.yaml
└── scripts/
    ├── manage_booking.py
    ├── run_booking.py
    └── test_booking.py
```

No additional README or user guide is needed.

### Skill instructions

`SKILL.md` defines trigger phrases and the interactive workflow. It routes authentication through the installed Feishu authentication guidance, requires `--as user`, validates two ranges, previews the plan, obtains confirmation, and invokes the task-management script.

### Task manager

`manage_booking.py` owns these operations:

- Install the user LaunchAgent
- Create or atomically replace a pending plan
- Display status and recent results
- Cancel a pending plan
- Uninstall the LaunchAgent

It should use Python's standard library where practical and avoid new runtime dependencies.

### Booking runner

`run_booking.py` is deterministic and non-interactive. It reads a due plan, performs preflight checks, waits until 09:00, runs both reservation workers, persists results, emits a notification, and consumes the plan.

### Local scheduler

Install a stable user LaunchAgent at:

`~/Library/LaunchAgents/com.codex.booking-waterdrop-rooms.plist`

It runs daily at 08:59. When there is no plan due that day, it exits immediately. This avoids installing and unloading a new scheduler for every booking while keeping each reservation plan one-time.

Runtime state lives under:

`~/Library/Application Support/Codex/booking-waterdrop-rooms/`

State and logs are readable and writable only by the current user.

## Authentication and Authorization

- Require `lark-cli` and use Feishu user identity, never bot identity.
- Verify the login with the supported auth-status command before saving a plan.
- If no valid user session exists, use a fresh QR-based split authentication flow and wait for the user to complete it before continuing.
- Request only the missing calendar scopes required to query rooms and create events.
- Do not read, copy, serialize, or log access tokens, refresh tokens, app secrets, device codes, or verification URLs.
- At 08:59, re-check that the user session is still valid. Authentication or permission failure is non-retryable during the critical window.

The plan preview confirmation is the explicit approval for the two future event creations. No destructive Feishu operations are part of this Skill.

## Room Discovery and Ranking

Query availability for both concrete target-date slots within 水滴大厦. Every returned room must be validated as belonging to that building before ranking.

For each slot, build a separate candidate queue:

| Rank | Candidate rule |
| --- | --- |
| 1 | Exact room name `7F-703` or `7F-704` |
| 2 | Another room on 7F |
| 3 | A room on 6F |
| 4 | A room on any other floor in 水滴大厦 |

`7F-703` and `7F-704` are equal in business priority. Candidates within the same rank retain the Feishu API result order; the Skill introduces no further preference. Capacity and equipment are not filters.

Use structured building and floor fields when available. If a response lacks enough information to prove that a room belongs to 水滴大厦, discard it. Never infer another building as a fallback.

## Reservation Algorithm

Process the two non-overlapping slots concurrently. Each slot is independent and may reserve the same room as the other slot.

For each slot:

1. Query currently available candidates.
2. Rank the validated candidates.
3. Try candidates in rank order.
4. Create an event without passing a summary, allowing Feishu to display `无主题`.
5. Make the logged-in user the event owner and add only the chosen room as a resource attendee.
6. If creation reports that the room was taken concurrently, immediately try the next candidate.
7. If the queue is exhausted, query availability again after the next backoff interval.

Retry delays are `0.5, 1, 2, 4, 5, 5, ...` seconds, capped by a shared deadline of 09:00:30. A successful slot stops immediately. The other worker continues until it succeeds, encounters a non-retryable error, or reaches the deadline.

Partial success is final: never delete or roll back a successful event because the other slot failed.

## Idempotency and Concurrency Safety

- Hold a process lock so only one runner can execute a plan.
- Give each plan and slot a stable local identifier.
- Persist a successful Feishu event ID immediately using an atomic state update.
- Never retry a slot already marked successful.
- If a transport timeout leaves the create result unknown, query the target time and intended room before retrying. Treat a matching event as success.
- Consuming a completed plan prevents it from running on later days.
- Replacing or canceling a plan is atomic with respect to the runner.

## Late Starts and Failure Handling

The runner may start before or during the booking window:

- Before 09:00: wait until 09:00:00.
- From 09:00:00 through 09:00:30: start immediately and use only the remaining window.
- After 09:00:30: mark the plan as missed and do not book.

Retryable failures include transient network failures, rate limits that can be honored before the deadline, and rooms lost to concurrent booking.

Non-retryable failures include invalid authentication, missing permissions, malformed or unsafe room data, invalid plan state, personal booking-limit errors, and starts after the deadline.

If response parsing cannot prove the building boundary, fail closed. One slot's non-retryable failure does not undo or stop a success already recorded for the other slot.

## Notifications and Logging

After execution, send a macOS notification with one of:

- Both slots succeeded
- Partial success
- Both slots failed
- Execution window missed

The detailed result includes each range, exact Feishu `room_name` when successful, event ID, or a concise failure reason.

Keep structured, redacted logs for 30 days. Logs may contain timestamps, plan and slot IDs, command stages, room names and IDs, candidate ranks, retry counts, event IDs, exit codes, and normalized error types. Logs must never contain authentication secrets or raw credential material.

## lark-cli Updates

If a `lark-cli` command reports an available update:

- Never update during the 08:59-09:00:30 critical path.
- Finish the booking operation first.
- Then run `lark-cli update`.
- Verify with `lark-cli --version` and report the installed version and whether Skills were updated in the interactive response or post-run macOS notification, as well as the redacted log.
- If an unattended update requires elevated permission, notify the user. On the next interactive invocation, request approval immediately and complete the update flow.

## Testing Strategy

Follow test-first development.

### Deterministic automated tests

Use a fake `lark-cli`, a fake clock, and isolated temporary state to test:

- Execution-date and target-date calculation
- Exactly-two-ranges validation
- Non-overlap validation, including touching boundaries
- Authentication gating and user-identity enforcement
- Room-building validation and all four ranking tiers
- Equal priority for `7F-703` and `7F-704`
- Exclusion of other buildings
- Concurrent workers and reuse of the same room across different slots
- Backoff sequence and the 09:00:30 deadline
- Partial success without rollback
- Ambiguous create-response recovery
- Locking, atomic replacement, cancellation, and plan consumption
- Redaction of tokens and secrets
- Update notices deferred until after the critical path

### Skill behavior tests

Run baseline scenarios without the new Skill, record failure patterns, then run the same fresh-context scenarios with the Skill. Include:

- A normal two-range scheduling request
- Overlapping ranges
- A duplicate plan that needs replacement
- A request that tempts the agent to use another building
- A request that arrives without valid Feishu authentication
- A late runner start

### Integration validation

- Run all automated tests without writing to a live calendar.
- Run the standard Skill structure validator.
- Verify login and meeting-room discovery with read-only calls.
- Do not create a real Feishu event during validation unless the user separately authorizes the exact test date and time ranges.
- Test LaunchAgent installation and notification delivery locally without invoking a live Feishu write.

## Acceptance Criteria

The Skill is complete when:

- It installs and uninstalls the stable user LaunchAgent safely.
- A confirmed request creates one plan for the next day's 09:00 run.
- The plan targets the execution date plus two calendar days.
- Exactly two valid, non-overlapping ranges are required.
- Both slots are attempted concurrently with the approved room priority.
- No room outside 水滴大厦 can be selected.
- Retries stop by 09:00:30.
- Repeated execution cannot duplicate an already successful slot.
- Success, partial success, failure, and missed execution are all reported.
- State and logs contain no secrets and logs expire after 30 days.
- All automated tests and Skill validation pass.
