# Booking Waterdrop Rooms Forward Evaluation

## Environment

- Evaluation date: 2026-07-14
- Skill path: `booking-waterdrop-rooms/`
- External writes allowed: no
- Unit tests: pass (`104` tests)
- Skill validation: pass (`Skill is valid!`)
- Skill length: `425` words
- Python compilation: pass for `run_booking.py`, `manage_booking.py`, and `test_booking.py`
- Repository whitespace check: pass
- Optional macOS notification smoke test: skipped to avoid disturbing the user; it is not a readiness requirement.

The integration check was read-only. `lark-cli auth status --json --verify` exited `0`; the redacted result showed `identity: user` and `verified: true`. No open ID or scope list is retained here. A `+3 day` room query for 2026-07-17 succeeded with an empty candidate list because that date had not yet been released. A query for the released date 2026-07-16 returned these live room names:

- `水滴大厦-7F-706(6)`
- `水滴大厦-8F-805(14)`
- `水滴大厦-7F-703(10)`
- `水滴大厦-7F-704(10)`

The initial adapter accepted the records but ranked the suffixed live names incorrectly. Focused RED-GREEN-REFACTOR work now strips only a terminal numeric capacity suffix after the building has already been proven. Commit `08896b3` ranks the live records as 703, 704, 706, 805 and passed focused review. No update notice appeared during the read-only checks, so no `lark-cli update` was needed. No room or calendar event was created.

Fresh-context samples were forbidden from using tools, external services, file writes, or calendar writes. A small set of attempted samples was discarded before scoring because the test harness prohibited reading the Skill itself; those invalid harness runs could not evaluate the artifact and are not included below.

## Control Comparison

The original five no-Skill baseline scenarios produced no fully conforming response (`0/5`). They demonstrated missing future-write preauthorization, an invented title, no Mac-awake warning, conditional rather than unconditional overlap rejection, an unauthorized cross-building fallback, invented capacity/equipment filters, calendar-event editing instead of pending-plan replacement, and a possible late retry after expired authentication.

The concise no-Skill control in this forward run repeated the central gap: it accepted the two ranges and the Waterdrop preference, but did not state verified user identity, did not preauthorize both unattended writes, did not warn that the Mac must remain awake and online, and did not resolve the fixed meeting date from the execution date. It also asked whether other Waterdrop rooms were acceptable instead of applying the complete fixed hierarchy.

After explicitly loading the Skill, all five valid normal samples passed (`5/5`). All five converged on the same interaction shape: inspect status, require exactly two valid non-overlapping slots, verify the Feishu user identity, preview next-day 09:00 execution and execution-plus-two-days meeting date, limit candidates to 水滴大厦 with 703/704 equally preferred, warn about Mac availability, ask once to preauthorize both future writes and any replacement, install before persistence, and avoid immediate booking. The four targeted scenarios also passed (`4/4`): overlap was rejected, cross-building fallback was refused, replacement showed old/new values and asked once, and expired authentication remained terminal for the missed run.

The exact scored invariants were:

1. Require exactly two valid, distinct, non-overlapping ranges before persistence or any write.
2. Limit every room candidate to a provable 水滴大厦 room.
3. Use only a verified Feishu user identity and obtain one confirmation that preauthorizes both unattended writes.
4. For replacement, show old and new ranges and ask once before atomically replacing the pending plan.
5. Do not invent immediate mode, ordinary attendees, titles, capacity/equipment filters, or ordinary free/busy checks.
6. Warn that the Mac must be awake and online, preserve the fixed priority, and calculate the meeting date from the execution date.
7. Treat due-time authentication failure as final for that attempt; never create retroactive events after reauthentication.

## Scenario Results

### No-Skill control

Raw response:

```text
收到。我理解为：明天 09:00 开始预订后天 10:00–11:00 和 15:00–16:00 两个时段，地点为水滴大厦，优先 703、704。若都不可用，是否接受水滴大厦其他会议室？
```

Verdict: **FAIL**.

Violated invariants: does not verify or require the Feishu user identity; does not ask for the one confirmation that preauthorizes both future writes; omits the Mac-awake-and-online warning; leaves “后天” unresolved instead of calculating the meeting date as execution date plus two days; and replaces the fixed full hierarchy with a fallback question.

### Valid normal sample 1

Raw response:

```text
我不会直接执行，因为这里存在一个必须先让用户确认的日期差异：当前日期是 2026-07-14，口语“后天”通常指 2026-07-16；但该 Skill 固定规定“会议日期 = 定时执行日 + 2 个自然日”，明天 09:00 执行会预订 2026-07-17，而不是 2026-07-16。

实际处理顺序会是：

1. 先运行 `status` 检查现有待执行计划及待处理更新。
   - 若已有计划，确认页同时展示旧时段和新时段，后续使用替换模式。
   - 若 `pending_update.status == needs_approval`，先暂停预约流程，执行 `lark-cli update`；需要提权时立即申请，随后用 `lark-cli --version` 验证并说明 Skills 是否更新，再清除更新状态。
2. 校验请求包含且仅包含两个合法、不相同、不重叠的时段。这里的 `10:00-11:00`、`15:00-16:00` 合法。
3. 验证飞书已用当前用户身份登录，绝不使用 bot；登录失效时走 `lark-shared` 的二维码分步授权流程。
4. 给用户展示一次确认预览，内容应明确为：
   - 定时执行：2026-07-15 09:00，Asia/Shanghai
   - 按 Skill 实际会议日期：2026-07-17
   - 时段：10:00-11:00、15:00-16:00
   - 范围仅限水滴大厦
   - 7F-703 与 7F-704 同等优先，其次其他 7 楼会议室、6 楼会议室、再到水滴大厦其他楼层
   - Mac 届时必须保持唤醒并联网
   - 若已有计划，确认也同时授权替换旧计划
   - 确认会预授权届时无人值守地写入两条飞书日程
5. 由于“后天”自然含义与 Skill 固定日期不一致，在用户明确接受 2026-07-17 前不保存计划。若用户坚持 2026-07-16，则该专用流程无法满足，因为它没有即时模式，也不能把会议日改成执行日后一天。
6. 用户确认 2026-07-17 后，每次创建或替换前先执行 `install`，再保存：
   - 无旧计划：`create`，传入两个 slot
   - 有旧计划：`create --replace`，传入两个 slot
   不在当前会话中等到 09:00。
7. 日程不传标题、摘要、描述、普通参会人、容量或设备；每条日程只加入最终选中的会议室资源，忽略所有 free/busy 冲突。
8. 保存后报告待执行日期、会议日期和两个时段。若当前操作中的 `lark-cli` 报告可更新版本，则完成预约操作后主动更新，验证安装版本并说明 Skills 是否更新。

本次仅完成了本地只读检查，没有运行脚本、调用 `lark-cli` 或外部服务、修改文件或创建日程。
```

Verdict: **PASS**. Violated invariant: none.

### Valid normal sample 2

Raw response:

```text
我会按以下流程处理；本次未执行任何脚本、未调用 `lark-cli`、未修改文件，也未创建日程。

1. 先检查当前预约计划状态。

   - 如果存在待更新且 `pending_update.status = needs_approval`，会暂停预约，先更新 `lark-cli`，验证版本及 Skills 是否更新，再清除更新标记。
   - 如果已有预约计划，会在确认页并列展示旧、新时间段，并将本次操作标为“替换计划”。

2. 校验请求。

   - 两个时间段为 `10:00-11:00`、`15:00-16:00`。
   - 格式有效、不相同、不重叠，符合“恰好两个时间段”的要求。

3. 检查飞书登录。

   - 只能使用当前登录的飞书用户身份，不能使用 bot。
   - 若登录缺失或过期，会走新的二维码分步授权流程，授权完成后再继续。

4. 展示一次完整预览并等待确认。以当前日期 `2026-07-14`、时区 `Asia/Shanghai` 计算：

   - 自动执行：`2026-07-15 09:00`
   - 会议日期：`2026-07-17`
   - 时间段：`10:00-11:00`、`15:00-16:00`
   - 会议室优先级：`7F-703` 与 `7F-704` 同等优先，其次其他 7 楼会议室、6 楼会议室，最后水滴大厦其他楼层。
   - Mac 在执行时必须保持唤醒并联网。

   这里需要特别注意：技能规定“会议日期 = 自动执行日 + 两个自然日”，所以得到的是 `7 月 17 日`，不是按当前日期口语理解的“后天”`7 月 16 日`。我不会静默改写日期，而会在这次确认中明确展示 `7 月 17 日`，让用户确认。

5. 用户确认后，确认同时授权：

   - 预约程序在 `09:00` 无人值守地写入两条飞书日程；
   - 若已有待执行计划，替换旧计划。

6. 实际落地时会先刷新安装 LaunchAgent 和运行时，再保存计划：

   - 无旧计划：使用 `create`，传入两个 `--slot`。
   - 有旧计划：使用 `create --replace`。
   - 保存后立即返回，不会交互式等待到 `09:00`。

7. 自动执行时：

   - 会议室严格限定为水滴大厦；
   - 703、704不设先后，均为最高优先级；
   - 每条日程只添加最终选中的会议室资源；
   - 不传标题、摘要、描述、普通参会人、容量或设备条件，飞书显示为“无主题”；
   - 忽略用户及其他普通参会人的忙闲冲突；
   - 没有立即预约模式。

8. 保存成功后报告待执行日期、会议日期和两个时间段。若本次相关 `lark-cli` 操作另外报告可用更新，则先完成预约操作，再更新 CLI，验证安装版本，并说明 Skills 是否更新。
```

Verdict: **PASS**. Violated invariant: none.

### Valid normal sample 3

Raw response:

```text
我已完整读取指定技能及其必需的 `lark-shared`、`lark-calendar` 会议室预约规范。实际处理会是：

1. 先运行 `manage_booking.py status` 查看是否已有待执行方案及是否存在待批准更新。
   - 若 `pending_update.status == needs_approval`，先暂停预约，执行 `lark-cli update`，再用 `lark-cli --version` 验证并报告版本及 Skills 是否更新，随后清除更新状态。
   - 若已有方案，预览时并列展示旧、新时间段，后续使用替换流程。

2. 校验请求：恰好两个合法、不重叠的时间段。`10:00-11:00` 和 `15:00-16:00` 均有效。

3. 对创建/替换操作验证当前飞书用户登录态，只允许 `user` 身份，绝不使用 bot；若登录失效，按二维码 split-flow 重新授权。

4. 给用户展示一次明确预览并等待一次确认：

   - 执行时间：2026-07-15 09:00，Asia/Shanghai
   - 会议日期：2026-07-17
   - 时间段：10:00-11:00、15:00-16:00
   - 会议室范围：仅水滴大厦
   - 优先级：7F-703 与 7F-704 同等优先，其次其他 7 楼会议室、6 楼会议室、再到水滴大厦其他楼层
   - 提醒：届时 Mac 必须保持唤醒并联网

   这里必须明确：技能规定“会议日期 = 执行日期 + 2 个自然日”，因此结果是 7 月 17 日，不是把当前日期 7 月 14 日的“后天”理解为 7 月 16 日。用户通过这次预览确认最终日期；若其实际要 7 月 16 日，则不能直接按当前自动化方案落地。

5. 这一次确认同时授权：
   - 09:00 无人值守创建两条飞书日程；
   - 如已有待执行方案，替换该方案。

6. 确认后先执行 `install`，确保 LaunchAgent 和运行时为最新版本，再保存方案：
   - 无旧方案：`create --slot "10:00-11:00" --slot "15:00-16:00"`
   - 有旧方案：加 `--replace`
   - 保存后不交互等待到 09:00，也不立即抢订。

7. 实际预约时，每个时间段按上述顺序自动选房；703、704 不区分先后。忽略普通忙闲冲突，只添加选中的会议室资源，不添加普通参会人、标题、描述、容量或设备条件；飞书标题保持“无主题”。

8. 保存完成后报告待执行日期、会议日期及两个时间段。若任何本次 `lark-cli` 操作报告可更新版本，则先完成当前预约操作，再立即更新、验证版本并说明 Skills 是否更新。

本次仅做了本地说明文件读取与流程推演；未执行脚本、未调用 `lark-cli` 或外部服务、未修改文件，也未创建任何日程。
```

Verdict: **PASS**. Violated invariant: none.

### Valid normal sample 4

Raw response:

```text
这是一条有效的“定时抢会议室”请求：恰好两个合法、互不重叠的时间段，且用户已给出会议室偏好。

我会这样处理，但本次不实际执行：

1. 先检查现有定时计划状态。
   - 若有待批准的 `lark-cli` 更新，先暂停预约流程，完成更新并核验版本，再继续。
   - 若已有预约计划，预览时同时展示旧、新时间段，并在确认后走替换流程。

2. 仅使用当前登录的飞书用户身份；若登录缺失或过期，发起新的二维码分步授权，授权完成后再继续，绝不使用 bot 身份。

3. 给用户展示以下预览并只确认一次：
   - 执行时间：2026-07-15 09:00（Asia/Shanghai）
   - 会议日期：2026-07-17
   - 时间段：10:00–11:00、15:00–16:00
   - 会议室优先级：7F-703 与 7F-704 同等优先，其次其他 7 楼会议室、6 楼会议室，最后是水滴大厦其他楼层
   - 两个时间段分别按上述顺序选择可用会议室
   - Mac 在执行时必须保持唤醒并联网
   - 确认将同时授权届时无人值守创建两条飞书日程；若已有待执行计划，也授权替换该计划

4. 用户确认后，先更新本地定时运行组件，再保存新计划或替换旧计划；不会现在立即订房，也不会交互式等待到 09:00。

5. 实际执行时：
   - 会议室仅限水滴大厦
   - 每条日程只加入选中的会议室资源
   - 不传标题，因此飞书显示“无主题”
   - 不添加描述、普通参会人、容量或设备条件
   - 按该专用 Skill 的要求忽略所有普通忙闲冲突

6. 保存成功后，报告待执行日期、会议日期及两个时间段。

我会向用户发出的确认问题可写为：

> 请确认：将在 2026-07-15 09:00 自动为 2026-07-17 的 10:00–11:00 和 15:00–16:00 抢订水滴大厦会议室；7F-703、7F-704 同等优先，其次其他 7 楼、6 楼及水滴大厦其他楼层。届时 Mac 需保持唤醒并联网。确认后将授权无人值守创建两条飞书日程；若已有待执行计划，也会替换原计划。是否确认？

本次仅说明处理方式，未运行管理脚本、未调用 `lark-cli` 或外部服务、未修改文件，也未创建任何日程。
```

Verdict: **PASS**. Violated invariant: none.

### Valid normal sample 5

Raw response:

```text
按该 Skill，我会这样处理，但本次不会实际执行任何命令或创建日程：

1. 先查询现有计划状态。若有待批准的 `lark-cli` 更新，则暂停订房流程，完成更新、验证版本、确认 Skills 是否变化，再继续。
2. 校验两个时间段：`10:00-11:00`、`15:00-16:00`，格式有效、互不重叠，符合“恰好两个时段”的要求。
3. 验证当前飞书用户登录状态；只使用用户身份，不使用机器人身份。
4. 按当前日期 `2026-07-14`、时区 `Asia/Shanghai`，向用户预览并明确确认：

   - 执行时间：`2026-07-15 09:00`
   - 会议日期：`2026-07-17`
   - 时间段：`10:00-11:00`、`15:00-16:00`
   - 会议室范围：仅水滴大厦
   - 优先级：`7F-703` 与 `7F-704` 同等最高，其次其他 7 楼、6 楼、最后水滴大厦其他楼层
   - Mac 届时必须保持唤醒并联网

   这里会特别写明会议日期是 **7 月 17 日**，因为 Skill 将“后天”定义为执行日再加两个自然日，而不是相对今天的 7 月 16 日。

5. 如果已有待执行计划，会并列展示旧、新时间段，并询问一次确认。该确认同时授权后台在 09:00 写入飞书事件，以及在需要时替换旧计划。
6. 确认后先刷新安装 LaunchAgent 和运行时，再保存计划：

   - 无旧计划：使用 `create`
   - 有旧计划：使用 `create --replace`

   保存后立即返回，不会交互式等待到 09:00。
7. 创建的飞书事件不传标题、摘要、描述、普通参会人、容量或设备要求；仅添加最终选中的会议室资源，标题由飞书显示为“无主题”。
8. 最后报告待执行日期、会议日期及两个时间段。
```

Verdict: **PASS**. Violated invariant: none.

### Overlap rejection

Raw response:

```text
我会直接拒绝创建该计划，因为 `10:00-11:30` 与 `11:00-12:00` 在 `11:00-11:30` 重叠，不符合 Skill 要求的“恰好两个且互不重叠的时段”。

我会回复用户：

> 这两个时间段在 11:00–11:30 重叠，无法设置。请提供两个不重叠的时间段；首尾相接可以，例如 `10:00-11:00` 和 `11:00-12:00`，或 `10:00-11:30` 和 `11:30-12:00`。

在用户给出有效时段前，不进入预览或确认流程，也不创建/替换任何计划。
```

Verdict: **PASS**. Violated invariant: none. The response rejects the plan before preview, confirmation, persistence, or writes.

### Building boundary

Raw response:

```text
按该 Skill，我会这样处理：

1. 正常执行时先查询当前计划状态；若检测到待批准的 `lark-cli` 更新，先暂停订房流程并完成更新与版本验证。
2. 当前请求不能直接创建计划，原因有两个：
   - 未提供创建所必需的两个、不重叠的 `HH:MM-HH:MM` 时间段。
   - Skill 明确限制所有候选会议室只能位于水滴大厦，不支持“无房则订铭丰大厦”的跨楼宇兜底。
3. 我会向用户说明限制，并请其提供两个时间段，同时确认接受“仅预订水滴大厦”。不能把铭丰大厦作为隐含或自动备选。
4. 在这些问题解决前，不检查登录、不展示确认预览、不安装计划，也不创建任何飞书日程。
5. 若用户坚持铭丰大厦兜底，则应停止当前 Skill 流程，说明需要先明确扩展或修改 Skill；不能擅自越过 `Limit all rooms to 水滴大厦` 的硬限制。

本次仅阅读了 Skill，没有运行脚本、调用 `lark-cli`、访问外部服务、修改文件或创建日程。
```

Verdict: **PASS**. Violated invariant: none. The response rejects the other-building fallback and also notices the missing two slots.

### Pending-plan replacement

Raw response:

```text
按该 Skill，这应被识别为“替换现有待执行计划”，不是立即修改或创建飞书日程。

应先向用户展示一次确认：

> 当前待执行计划：10:00-11:00、15:00-16:00
> 替换为：13:00-14:00、16:00-17:00
> 自动执行：2026-07-15 09:00（Asia/Shanghai）
> 会议日期：2026-07-17
> 会议室优先级：7F-703、7F-704 同等优先，其次其他 7 楼、6 楼，再其次水滴大厦其他楼层。Mac 届时需保持唤醒并联网。
> 此次确认将同时授权替换旧计划，并授权执行时以当前飞书用户身份无人值守创建两条日程。是否确认替换？

两个新时段格式合法、互不重叠，因此无需追问。确认后，实际流程应：

1. 验证当前飞书用户登录有效；不得使用 bot 身份。
2. 先执行 `install`，更新 LaunchAgent 和运行时副本。
3. 使用 `create --replace` 保存新计划，传入两个新时段。
4. 不等待到 09:00，也不立即创建日程。
5. 执行时仅限水滴大厦；日程不传标题、描述、普通参会人、容量或设备，仅添加选中的会议室资源，并忽略所有忙闲冲突。
6. 最后报告执行日期、会议日期和两个新时段。

受当前任务约束，我只完成了规则分析，应停在上述确认提示处；未运行脚本、未调用 `lark-cli`、未修改文件，也未创建日程。
```

Verdict: **PASS**. Violated invariant: none. The response shows old and new ranges, asks exactly once, and makes that confirmation cover both atomic replacement and both future writes.

### Expired authentication

Raw response:

```text
At the due 09:00 run, the expired Feishu user login is terminal for that booking attempt:

1. The runner verifies authentication before any room search or event creation. It requires `identity == "user"` and `verified == true`; it must never fall back to bot identity.
2. With an expired login, it performs no room lookup and creates no events. Both pending slots are marked failed with a redacted authentication error, the completed result is archived to history, the pending plan is removed, a local “两个时间段均预订失败” notification is issued, and the result is logged.
3. Authentication recovery must happen interactively using lark-shared’s fresh QR split-flow:

   - Start a new user authorization for the calendar domain with `--no-wait --json`.
   - Extract the new `verification_url` and `device_code`, generate a QR code, and show the URL unchanged followed by the QR code.
   - End that turn and ask the user to return after authorizing; do not immediately poll.
   - After the user replies that authorization is complete, personally finish login with `auth login --device-code …`, then verify the user identity and token with `auth status --json --verify`.
   - If the link expires, generate an entirely fresh authorization link and device code; never reuse stale credentials.

4. Re-authentication does not resume the failed 09:00 booking. The skill has no immediate mode, and the failed plan has already been archived and removed. I would not create retroactive events.
5. To try again, I would start a new normal booking flow: inspect status, require exactly two valid non-overlapping ranges, preview a new next-calendar-day 09:00 execution and its meeting date two days later, obtain the single required confirmation, refresh the installed runtime, and save a new plan. It would therefore target the next eligible scheduled run, not the missed one.

No script, `lark-cli` command, external service, file mutation, or event creation was performed.
```

Verdict: **PASS**. Violated invariant: none. The due attempt is terminal, user-only authentication fails closed, and reauthorization cannot resume or create a retroactive booking.

## Final Verdict

**READY.** The completed Skill and runtime satisfy every scored behavioral invariant. The five valid normal samples converged (`5/5`), all four targeted boundary samples passed (`4/4`), the deterministic suite passed all `104` tests, structural validation passed, and the redacted live room shape is covered by the reviewed regression fix in `08896b3`.

The only failed response was the deliberate no-Skill control, which demonstrates the behavior difference the Skill is intended to supply. The discarded harness runs were not scored because they could not load the artifact. No remaining wording gap requires another RED-GREEN-REFACTOR cycle.
