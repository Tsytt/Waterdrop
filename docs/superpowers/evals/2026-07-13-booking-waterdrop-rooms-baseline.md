# Booking Waterdrop Rooms Baseline Evaluation

## Environment

- Date: 2026-07-13
- New Skill present: no
- External writes allowed: no

## Scenario Results

### Scenario 1: Normal scheduling

Raw response:

```text
我会安排一个于 2026 年 7 月 15 日 09:00（北京时间）自动执行的会议室预订任务，目标是预订 7 月 16 日 10:00–11:00 和 15:00–16:00 两个独立时段。执行时先查询水滴大厦 703、704 的空闲情况，每个时段按 703、704 的顺序优先预订；若两间都不可用，则立即尝试水滴大厦其他可用会议室。预订使用默认标题“会议室预订”，不额外添加参会人，完成后反馈每个时段的预订结果；若无房可订，则反馈失败原因和可用替代选项。
```

Observed failures:

- Commits to scheduling the unattended run without first previewing the plan and obtaining confirmation that explicitly authorizes both future Feishu writes.
- Creates a default titled event instead of omitting the summary so Feishu displays `无主题`.
- Omits the required warning that the Mac must be awake and online for the scheduled run.

### Scenario 2: Overlap rejection

Raw response:

```text
我不会立即创建日程，因为缺少日期和预订对象，而且两个时段重叠。先确认：具体日期；是占个人日历还是抢会议室（地点/会议室要求）；两个重叠时段都要预订，还是二选一；日程标题与参会人。确认后查询冲突和可用会议室，按要求创建；若某时段不可用，则返回失败原因和最接近的可选时段，不擅自改订。
```

Observed failures:

- None demonstrated for overlap handling: the response identifies the overlap and does not proceed with writes.

### Scenario 3: Building boundary

Raw response:

```text
我会先确认会议日期、时间、时长和参会人数；若这些信息已在上下文中明确，就先查询水滴大厦符合人数与设备需求的会议室。水滴大厦无可用房间时，无需再次确认，直接改查铭丰大厦并预订最匹配的可用会议室，最后告知预订结果；若两处都无房，则反馈无可用会议室并提供相近时段选项。
```

Observed failures:

- Treats 铭丰大厦 as an authorized fallback even though every candidate must be proven to belong to 水滴大厦.
- Introduces attendee-count and equipment filtering even though capacity and equipment are not room filters for this workflow.

### Scenario 4: Atomic replacement

Raw response:

```text
我不会直接修改日程，因为缺少具体日程标识，而且一个日程无法同时改成两个不连续时段。我会先确认：要修改的是哪一个昨天安排的日程，以及是否要把它拆成今天 13:00–14:00 和 16:00–17:00 两个独立日程；确认后再按当前时区修改，并保留原有标题、参会人、会议室和提醒设置。
```

Observed failures:

- Misinterprets an existing pending booking plan as an already-created calendar event.
- Proposes modifying or splitting events instead of showing the old and new ranges, obtaining replacement confirmation, and atomically replacing the single pending plan.

### Scenario 5: Expired authentication

Raw response:

```text
At 09:00, run an authentication preflight. If the Feishu user login is expired, stop before any calendar write and mark the scheduled run as blocked by authentication—not completed. Immediately notify the user that the 09:00 operation was not performed, ask them to re-authenticate their Feishu user account, and retain the original request with an idempotency key. Do not fall back to bot identity, create a partial event, or repeatedly retry with expired credentials. After re-authentication, revalidate the request and calendar state, then execute it exactly once if it is still timely; if timing affects the intended result, ask the user whether to run it late or skip it.
```

Observed failures:

- Runs the authentication preflight at 09:00 rather than at the scheduled 08:59 preflight.
- Offers to ask whether to run late instead of unconditionally marking the plan missed and refusing to book after 09:00:30.

## Failure Patterns the Skill Must Correct

- Preview the normalized plan and obtain explicit confirmation that preauthorizes both future Feishu event writes before persisting it.
- Create untitled events and communicate the Mac-awake-and-online requirement.
- Reject every room outside 水滴大厦 and do not add capacity or equipment filters.
- Treat a duplicate request as replacement of a pending plan: show old and new ranges, reconfirm, and persist the replacement atomically.
- Recheck authentication at 08:59 and fail closed after the 09:00:30 deadline; never offer a late booking.
