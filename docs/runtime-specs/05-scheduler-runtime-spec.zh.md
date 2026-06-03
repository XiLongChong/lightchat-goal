# SchedulerRuntime 可执行实现规格

> 日期：2026-06-03
> 状态：可执行实现规格
> 上游文档：
> - `docs/companion-architecture-requirements.zh.md` (especially sections 2, 3, 4, 13.2)
> - `docs/companion-architecture-design-decisions.zh.md` (especially sections 3, 9.2)
> - `docs/companion-runtime-module-contracts.zh.md` (especially section 15)
> - `docs/companion-runtime-data-contracts.zh.md` (especially sections 5, 15)
> - `docs/companion-runtime-state-machines.zh.md` (especially sections 5, 6, 9)
> - `docs/companion-runtime-gap-analysis.zh.md` (especially section 3.4)
> - `docs/companion-runtime-implementation-plan.zh.md` (especially phase 9)
> - `docs/companion-runtime-test-plan.zh.md` (especially section 8)
> 当前源码参考：
> - `agent/scheduler.py` (ProactiveScheduler)
> - `agent/proactive_settings.py` (ProactiveSettings, INTENSITY_PROFILES)
> - `memory/store.py` (claim_due_scheduled_messages, create_scheduled_wake_up, etc.)

***

## 0. 模块目标

SchedulerRuntime manages timing and triggering. It does **NOT** generate messages, build context, or call models. It only decides **WHEN** to create a turn and **WHAT TYPE** of turn to create.

```text
SchedulerRuntime 的唯一输出是：一个 TurnRequest 或 None。

它不负责：
- 拼 prompt（ContextBuilder 的职责）
- 调模型（ProviderGateway 的职责）
- 发消息（DeliveryService 的职责）
- 写记忆（MemorySystem 的职责）
- 构上下文（TurnEngine 的职责）
```

SchedulerRuntime 是一个纯决策层。它回答两个问题：
1. 现在是否应该创建一轮 turn？
2. 如果是，应该创建什么类型的 turn？

***

## 1. 完整输入输出对象

### 1.1 SchedulerConfig

```text
SchedulerConfig
  enabled: bool                          -- 全局主动消息总开关
  autonomous_enabled: bool               -- 自主冷启动子开关
  autonomous_intensity: str              -- 频率档位: "quiet"|"daily"|"caring"|"companion"
  quiet_hours_enabled: bool              -- 勿扰时间开关
  quiet_hours_start: str                 -- 勿扰开始 HH:mm (如 "23:00")
  quiet_hours_end: str                   -- 勿扰结束 HH:mm (如 "08:00")
  heartbeat_interval_seconds: int        -- 心跳间隔 (秒), 默认 30
  daily_active_message_cap: int          -- 每日主动消息总上限 (0-200)
  cold_start_seconds: int                -- 冷启动阈值 (秒), 默认 120
  scheduled_enabled: bool                -- 定时仪式总开关
  scheduled_morning_enabled: bool        -- 早安开关
  scheduled_morning_time: str            -- 早安时间 HH:mm
  scheduled_night_enabled: bool          -- 晚安开关
  scheduled_night_time: str              -- 晚安时间 HH:mm
  reminder_enabled: bool                 -- 提醒到期处理开关 (默认 true)
  intensity_profile: IntensityProfile    -- 从 autonomous_intensity 解析的频率参数
    daily_trigger_cap: int               -- 每日自主触发次数上限
    min_minutes: int                     -- 最小唤醒间隔 (分钟)
    max_minutes: int                     -- 最大唤醒间隔 (分钟)
```

### 1.2 IntensityProfile (频率档位参数)

```text
IntensityProfile (由 autonomous_intensity 查表得到, 不可变)

  quiet:
    daily_trigger_cap: 2
    min_minutes: 360      (6h)
    max_minutes: 720      (8h, 实际 12h)
    label: "安静"

  daily:
    daily_trigger_cap: 5
    min_minutes: 120      (2h)
    max_minutes: 480      (3h, 实际 8h)
    label: "日常"

  caring:
    daily_trigger_cap: 10
    min_minutes: 30       (0.5h)
    max_minutes: 120      (1h, 实际 2h)
    label: "关心"

  companion:
    daily_trigger_cap: 20
    min_minutes: 5        (5min)
    max_minutes: 20       (20min)
    label: "陪伴"
```

注：`min_minutes` 和 `max_minutes` 定义的是模型允许选择的下一次唤醒窗口。系统在 turn 结束时传递给模型的 `next_wake_minutes` 范围即 `[min_minutes, max_minutes]`。模型在这个范围内选择具体值。

### 1.3 InitiativeState

```text
InitiativeState  (per user_id + bot_id)
  last_trigger_at: float | None         -- 上次主动触发时间戳 (unix seconds)
  last_spoke_at: float | None           -- 上次主动开口时间戳
  last_skipped_at: float | None         -- 上次主动跳过时间戳
  consecutive_skip_count: int           -- 连续跳过次数 (0 或 1, 因为不能连续 skip)
  next_wake_after: float | None         -- 系统允许的下一次唤醒最早时间 (unix seconds)
  next_wake_before: float | None        -- 系统允许的下一次唤醒最晚时间 (unix seconds)
  intensity_profile: str                -- 当前频率档位名
  last_decision: str                    -- 上次决策: "spoke" | "skipped" | "unset"
  daily_trigger_count: int              -- 今日自主触发次数 (跨日清零)
  daily_trigger_date: str               -- YYYY-MM-DD, 用于跨日重置
  proactive_paused_until: float         -- 用户 opt-out 暂停到何时 (0 = 未暂停)
```

### 1.4 ScheduledTask

```text
ScheduledTask  (对应 scheduled_messages 表)
  task_id: str                          -- 任务唯一 ID
  task_type: str                        -- "proactive_wake_up" | "user_reminder" | "scheduled_ritual" | "admin_scheduled"
  user_id: str                          -- 目标用户
  bot_id: str                           -- 目标 bot
  due_at: float                         -- 到期时间戳 (unix seconds)
  status: str                           -- "pending" | "sending" | "sent" | "failed" | "cancelled"
  claimed_at: float | None              -- 领取时间戳 (sending 时写入)
  claim_token: str | None               -- 领取令牌 (turn_id)
  created_at: float                     -- 创建时间戳
  payload: dict                         -- 任务负载
    messages: list[dict]                -- 消息包袱, 第一个元素含关键元数据
      text: str                         -- 提示文本 (model hint)
      wake_up_type: str                 -- "proactive" | "morning_greeting" | "night_summary" | "user_requested"
      is_wake_up: bool                  -- 是否强制开口 (skip 已禁用)
      skip_quiet_hours: bool            -- 是否绕过勿扰时间
      recurring: str | None             -- "daily" | None
```

### 1.5 RuntimeTrigger

```text
RuntimeTrigger
  trigger_id: str                       -- 触发事件唯一 ID
  trigger_type: str                     -- "heartbeat" | "due_reminder" | "scheduled_ritual" | "manual_admin" | "post_user_turn"
  source_task_id: str | None            -- 关联的 ScheduledTask.task_id (claim 后非空)
  reason: str                           -- 触发原因 (自然语言, 用于 audit)
  due_at: float | None                  -- 任务到期时间 (claim 后可用)
  claimed_at: float | None              -- 领取时间
  trigger_kind: str                     -- "cold_start" | "wake_up" | "scheduled" | "user_requested"
  is_wake_up: bool                      -- 是否强制开口
  intensity: str                        -- 触发时的 intensity 档位
  metadata: dict                        -- 附加元数据
```

### 1.6 InitiativeGateDecision

```text
InitiativeGateDecision
  allowed: bool                         -- 是否允许创建 turn
  reason: str                           -- 允许/拒绝原因
  intensity_profile: str                -- 当前 intensity
  daily_trigger_count: int              -- 今日已触发次数
  daily_trigger_cap: int                -- 今日触发次数上限
  min_interval: int                     -- 档位最小间隔 (分钟)
  max_interval: int                     -- 档位最大间隔 (分钟)
  quiet_hours_blocked: bool             -- 是否被勿扰时间阻挡
  technical_circuit_breaker: bool       -- 是否被技术熔断拦截
  circuit_breaker_reason: str           -- 熔断原因
  elapsed_since_last_visible: int       -- 距离上次可见对话秒数
  last_decision: str                    -- 上次决策类型
  requires_forced_speak: bool           -- 本轮是否强制开口
  blocked_checks: list[dict]            -- 被阻止的检查项列表
    name: str                           -- 检查项名称
    passed: bool                        -- 是否通过
    reason: str                         -- 不通过原因
```

### 1.7 SchedulerMetrics

```text
SchedulerMetrics  (只读, 用于 admin API)
  heartbeat_tick_count: int             -- 累计心跳次数
  eligibility_pass_count: int           -- 累计 gate 通过次数 (主动冷启动)
  eligibility_fail_count: int           -- 累计 gate 拒绝次数
  eligibility_fail_reasons: dict[str, int]  -- 各类拒绝原因的计数
  reminder_claim_count: int             -- 累计提醒领取次数
  ritual_trigger_count: int             -- 累计仪式触发次数
  is_busy: bool                         -- 是否正在执行 turn
  active_turn_id: str | None            -- 当前活跃 turn ID
  last_heartbeat_at: float | None       -- 上次心跳时间戳
  stall_detected: bool                  -- 是否检测到调度停滞
```

***

## 2. 不可违反的不变量 (Invariants)

以下不变量是 SchedulerRuntime 的硬约束。任何实现、修改、重构都必须满足这些条件。违反任何一条都是 bug。

1. **Heartbeat NEVER directly generates message text.** 心跳循环只输出 TurnRequest 或 None。文案生成永远是 TurnEngine + LLM 的职责。

2. **User reminders ALWAYS take priority over proactive cold-start.** 在同一心跳周期中, 如果同时存在到期提醒和主动冷启动条件, 必须优先 claim 提醒并创建提醒 turn。冷启动在本次心跳中被跳过。

3. **Tasks MUST be claimed (atomically) before a turn is created.** 任何 scheduled_messages 中的任务必须通过 claim 流程将状态从 `pending` 变为 `sending` 后, 才能创建对应的 TurnRequest。未经 claim 的任务不能创建 turn。

4. **Unclaimed tasks NEVER trigger model calls.** 即使 `get_due_scheduled_messages()` 返回了数据, 如果 claim 失败 (被并发抢走), 不允许调用模型。

5. **Proactive messages are TEXT ONLY.** 关系型主动 turn 和定时仪式 turn 的工具暴露永远不包含 `send_image_message`, `generate_image_message`, `send_sticker`, 或任何语音输出工具。SchedulerRuntime 必须确保创建的 TurnRequest 标记 `turn_type=relationship_proactive` 或 `scheduled_ritual`, 使 ToolExposurePolicy 能正确限制工具。

6. **Quiet hours block ALL proactive messages (including rituals, unless user explicitly authorized).** 勿扰时间内, 关系型主动 cold-start 和定时仪式均不可触发。用户授权提醒 (UserReminderFlow) 的勿扰行为取决于任务创建时的 `skip_quiet_hours` 标记。

7. **User-authorized reminders are NOT subject to proactive skip rules.** UserReminderFlow 中不暴露 `skip_initiative`, 模型不允许跳过。提醒是用户授权的承诺, 必须送达 (除非客观发送失败)。

8. **Model can skip a proactive turn, but cannot skip twice consecutively.** 如果上轮主动决策为 `skipped`, 下轮主动必须强制开口。`consecutive_skip_count` 最大值为 1, 因为第二轮 skip 不可能发生 (结构上不暴露 skip 工具)。

9. **After skip, next proactive turn MUST NOT expose skip_initiative.** 通过 `is_wake_up=true` 标记传递给 ToolExposurePolicy, 确保 `skip_initiative` 不在 exposed_tool_names 中。

10. **finish_turn(next_wake_minutes) is REQUIRED for proactive turns.** 关系型主动 turn 的终态工具必须携带 `next_wake_minutes` 参数。不传或超出系统允许范围的终态不接受。SchedulerRuntime 在 turn 结束后读取此值并安排下一次 wake-up。

11. **next_wake_minutes MUST be within system-provided range [min_minutes, max_minutes].** 模型选择的 `next_wake_minutes` 值必须在当前 intensity 档位定义的 `min_minutes` 和 `max_minutes` 之间。越界值会被 clamp 到最近的边界。

12. **Daily trigger count MUST respect frequency tier cap.** `daily_trigger_count >= daily_trigger_cap` 时, 关系型主动冷启动 gate 必须返回 `allowed=false`。但此 cap 不影响 UserReminderFlow 和 ScheduledRitualFlow。

13. **Technical circuit breakers prevent infinite trigger loops.** 即使没有产品上的 "stop if no reply" 规则, 系统必须检测异常模式: 同一 task 被 claim 两次、heartbeat 调用间隔异常短、同一用户无限创建 cold-start turn 等。

14. **Same task MUST NOT be claimed twice (atomic claim).** 数据库层的 `UPDATE scheduled_messages SET status='sending' WHERE id IN (...) AND status='pending'` 保证原子性。只有成功将 status 从 pending 改为 sending 的行才被返回。

15. **SchedulerRuntime MUST NOT construct prompts or call models directly.** 它只能调用 TurnEngine (或 ChatAgent) 的入口来创建 turn。所有模型交互必须走 TurnEngine -> ContextBuilder -> ProviderGateway 路径。

16. **Global switch OFF means no proactive cold-start turns.** `SchedulerConfig.autonomous_enabled = false` 时, 关系型主动不允许创建 turn。但 UserReminderFlow 和 ScheduledRitualFlow 不受 `autonomous_enabled` 控制, 受各自的 enabled 开关控制。

17. **Concurrent turns for the same (user_id, bot_id) are forbidden.** SchedulerRuntime 内部持有 lock, 同一时刻最多一个 proactive/wake-up turn 在运行。如果 lock 已被持有, 新来的触发请求返回 `status=skipped, reason=active_proactive_turn_running`。

***

## 3. 心跳循环伪代码

### 3.1 heartbeat_tick() 完整伪代码

```text
heartbeat_tick(now_ts: float) -> TurnRequest | None:

    # 0. 递增心跳计数
    heartbeat_tick_count += 1
    last_heartbeat_at = now_ts

    # 1. 检查全局主动功能开关
    if not scheduler_config.enabled:
        return None

    # 2. 检查调度器是否正在执行另一轮 turn
    if lock.is_locked():
        # 已有 turn 在运行, 不能建新 turn
        # 记录 metrics (不是 error, 是正常并发保护)
        return None

    # 3. Claim 到期用户提醒 (最高优先级)
    #    只 claim wake_up_type = "user_requested" 的任务
    reminder_tasks = claim_due_tasks(
        now_ts=now_ts,
        filter_wake_up_type="user_requested",
        limit=1
    )
    if reminder_tasks is not empty:
        task = reminder_tasks[0]
        # 检查提醒任务是否允许绕过勿扰
        skip_quiet = task.payload.messages[0].skip_quiet_hours
        if skip_quiet OR NOT in_quiet_hours(now_ts):
            return create_turn_request(
                turn_type="user_reminder_due",
                trigger_type="due_reminder",
                source_task=task
            )
        else:
            # 在勿扰时间但 task 不允许绕过 -> 重新调度到勿扰结束后
            reschedule_task_to_quiet_hours_end(task)
            return None

    # 4. Claim 到期的定时仪式 (早安/晚安)
    ritual_tasks = claim_due_tasks(
        now_ts=now_ts,
        filter_wake_up_type="morning_greeting" or "night_summary",
        limit=1
    )
    if ritual_tasks is not empty:
        task = ritual_tasks[0]
        if NOT in_quiet_hours(now_ts):
            return create_turn_request(
                turn_type="scheduled_ritual",
                trigger_type="scheduled_ritual",
                source_task=task
            )
        else:
            # 在勿扰时间: 如果仪式必须在固定时间发送 (如早安 08:00),
            # 则延迟到勿扰结束后再 claim
            # 对于恰好在勿扰结束边界的, 如 08:00 既是早安时间也是勿扰结束,
            # 应有明确的边界处理规则 (见 §7)
            reschedule_task_to_quiet_hours_end(task)
            return None

    # 5. 检查关系型主动 cold-start 或 wake-up 资格
    #    5a. 先看是否有已排期的 proactive wake-up 到期
    wake_up_tasks = claim_due_tasks(
        now_ts=now_ts,
        filter_wake_up_type="proactive",
        limit=1
    )
    if wake_up_tasks is not empty:
        task = wake_up_tasks[0]
        gate = check_proactive_gate(user_id, bot_id, now_ts)
        if gate.allowed:
            return create_turn_request(
                turn_type="relationship_proactive",
                trigger_type="heartbeat",
                source_task=task,
                gate_decision=gate
            )
        else:
            # gate 不允许, 将 task 标记回 pending 或取消
            # 安排下一次 retry (见 §10.4 heartbeat failure backoff)
            requeue_or_cancel_task(task, gate.reason)
            return None

    #    5b. 如果没有已排期 wake-up, 检查 cold-start 条件
    if is_cold_start_eligible(user_id, bot_id, now_ts):
        gate = check_proactive_gate(user_id, bot_id, now_ts)
        if gate.allowed:
            return create_turn_request(
                turn_type="relationship_proactive",
                trigger_type="heartbeat",
                source_task=None,  # cold-start 没有预排期 task
                gate_decision=gate
            )

    # 6. 无事可做, 本轮心跳结束
    return None
```

### 3.2 check_proactive_gate() 伪代码

```text
check_proactive_gate(user_id, bot_id, now_ts) -> InitiativeGateDecision:

    blocked_checks = []

    # 1. autonomous_enabled 子开关
    if NOT scheduler_config.autonomous_enabled:
        return InitiativeGateDecision(allowed=false, reason="autonomous_disabled")

    # 2. Bridge online check
    if bridge_online_check is not None and not bridge_online_check():
        blocked_checks.append({name: "bridge_online", passed: false, reason: "微信桥离线"})
        return InitiativeGateDecision(allowed=false, reason="bridge_offline", blocked_checks)

    # 3. Quiet hours check
    if scheduler_config.quiet_hours_enabled:
        if in_quiet_hours(now_ts):
            return InitiativeGateDecision(
                allowed=false,
                reason="quiet_hours",
                quiet_hours_blocked=true,
                blocked_checks=[{name: "quiet_hours", passed: false, reason: "当前在勿扰时间"}]
            )

    # 4. Daily active message cap
    sent_today = count_today_proactive_sent(today_start_ts)
    if sent_today >= scheduler_config.daily_active_message_cap:
        blocked_checks.append({name: "daily_message_cap", passed: false, reason: "今日主动消息已达上限"})
        return InitiativeGateDecision(allowed=false, reason="daily_message_cap", blocked_checks)

    # 5. Daily autonomous trigger cap (intensity-based)
    trigger_count, _ = get_autonomous_trigger_count(user_id, bot_id)
    profile = get_intensity_profile(scheduler_config.autonomous_intensity)
    if trigger_count >= profile.daily_trigger_cap:
        return InitiativeGateDecision(
            allowed=false,
            reason="trigger_cap_reached",
            daily_trigger_count=trigger_count,
            daily_trigger_cap=profile.daily_trigger_cap,
            blocked_checks=[{name: "trigger_cap", passed: false, reason: "今日自主触发次数已达intensity上限"}]
        )

    # 6. User opt-out check
    ds = get_dialogue_state(user_id, bot_id)
    paused_until = ds.proactive_paused_until
    if paused_until > now_ts:
        return InitiativeGateDecision(allowed=false, reason="user_opt_out", blocked_checks=[
            {name: "user_opt_out", passed: false, reason: f"用户暂停到 {paused_until}"}
        ])

    # 7. Cold-start elapsed time check
    last_visible = resolve_last_visible_message_at(user_id, bot_id)
    elapsed = now_ts - last_visible
    if elapsed < scheduler_config.cold_start_seconds:
        return InitiativeGateDecision(allowed=false, reason="too_soon", elapsed_since_last_visible=elapsed)

    # 8. 查找上次 decision 以确定是否强制开口
    last_decision = get_initiative_state(user_id, bot_id).last_decision
    requires_forced_speak = (last_decision == "skipped")

    # 9. Technical circuit breaker (see §10)
    cb_check = check_technical_circuit_breakers(user_id, bot_id, now_ts)
    if cb_check.tripped:
        return InitiativeGateDecision(
            allowed=false,
            reason="circuit_breaker",
            technical_circuit_breaker=true,
            circuit_breaker_reason=cb_check.reason,
            blocked_checks=[{name: "circuit_breaker", passed: false, reason: cb_check.reason}]
        )

    # All checks passed
    return InitiativeGateDecision(
        allowed=true,
        reason="",
        intensity_profile=scheduler_config.autonomous_intensity,
        daily_trigger_count=trigger_count,
        daily_trigger_cap=profile.daily_trigger_cap,
        min_interval=profile.min_minutes,
        max_interval=profile.max_minutes,
        quiet_hours_blocked=false,
        technical_circuit_breaker=false,
        elapsed_since_last_visible=elapsed,
        last_decision=last_decision,
        requires_forced_speak=requires_forced_speak
    )
```

***

## 4. 三种主动流程的完整状态机

### 4.1 RelationshipProactiveFlow (关系型主动冷启动)

#### 触发条件

- heartbeat_tick 中, 没有更高优先级任务 (user_reminder, scheduled_ritual)
- 不在勿扰时间
- SchedulerConfig.autonomous_enabled = true
- gate check 全部通过

#### Gate 检查顺序与逻辑

```text
1. autonomous_enabled 开关          -> 不通过则 blocked, reason="autonomous_disabled"
2. Bridge online                    -> 不通过则 blocked, reason="bridge_offline"
3. Quiet hours                      -> 不通过则 blocked, reason="quiet_hours", quiet_hours_blocked=true
4. Daily active message cap         -> 不通过则 blocked, reason="daily_message_cap"
5. Daily autonomous trigger cap     -> 不通过则 blocked, reason="trigger_cap_reached"
6. User opt-out                     -> 不通过则 blocked, reason="user_opt_out"
7. Cold-start elapsed threshold     -> 不通过则 blocked, reason="too_soon"
8. Technical circuit breaker        -> 不通过则 blocked, reason="circuit_breaker"
9. Pending wake-up already exists   -> 不创建新 turn (已有排期, 防重复触发)
```

#### Turn 创建参数

```text
TurnRequest:
  turn_type: "relationship_proactive"
  user_id: user_id
  bot_id: bot_id
  inbound_batch: null                 -- 无用户输入
  runtime_trigger:
    trigger_type: "heartbeat"
    trigger_kind: "cold_start" | "wake_up"  -- 分别对应无/有预排期 task
    is_wake_up: bool                   -- 是否强制开口 (上次 skipped 则 true)
    source_task_id: task.task_id | null
  classification:
    turn_type: "relationship_proactive"
    trigger_source: "scheduler"
```

#### 工具暴露 (由 ToolExposurePolicy 根据 turn_type 和 is_wake_up 决定)

```text
可跳过阶段 (is_wake_up = false, last_decision != "skipped"):
  exposed: send_text_message, skip_initiative, finish_turn
  denied:  send_image_message, generate_image_message, send_sticker

强制开口阶段 (is_wake_up = true, last_decision == "skipped"):
  exposed: send_text_message, finish_turn
  denied:  skip_initiative, send_image_message, generate_image_message, send_sticker
```

#### Turn 结束后的状态更新

```text
turn 结束后, 根据 TurnResult 更新 InitiativeState:

Case A: 模型 spoke (至少一条 SENT_CONFIRMED):
  last_spoke_at = now_ts
  last_decision = "spoke"
  consecutive_skip_count = 0
  daily_trigger_count = bump_autonomous_trigger_count()  -- 跨日自动重置
  last_trigger_at = now_ts

Case B: 模型 skipped (调用了 skip_initiative):
  last_skipped_at = now_ts
  last_decision = "skipped"
  consecutive_skip_count = 1
  daily_trigger_count = bump_autonomous_trigger_count()
  last_trigger_at = now_ts

Case C: turn 执行失败 (LLM error, timeout, etc.):
  不更新 spoke/skipped 状态
  daily_trigger_count 不递增
  安排 fallback retry (见 §10.4)

所有 case 的共同更新:
  next_wake_after = now_ts + next_wake_minutes * 60
  创建新的 scheduled_wake_up task:
    task_type = "proactive_wake_up"
    wake_up_type = "proactive"
    is_wake_up = (last_decision == "skipped")  -- 下次必须强制开口
    due_at = next_wake_after
    message_hint = 记录上次 hint 的前 100 字符
```

#### 失败处理

```text
LLM call failed:
  -> 不更新 InitiativeState (不递增 trigger count)
  -> 安排 fallback retry: 创建新的 scheduled_wake_up, due_at = now_ts + avg(min_minutes, max_minutes) * 60
  -> 不标记原 task 为 failed

send_text_message failed (全部消息未送达):
  -> 终端状态为 END_FAILED
  -> last_trigger_at 更新
  -> daily_trigger_count 递增 (系统已经尝试过了)
  -> 不安排新 wake-up (由终端处理)
  -> 标记 source_task 为 failed

Protocol violation (模型输出无 tool call):
  -> 由 TurnEngine 处理 retry
  -> retry 耗尽后 -> 终端状态异常
  -> 安排 fallback retry
```

### 4.2 UserReminderFlow (用户授权提醒到期)

#### 触发条件

- heartbeat_tick 中, claim 到 `wake_up_type = "user_requested"` 的到期任务
- 优先级高于 scheduled_ritual 和 relationship_proactive
- 如果 task 标记 `skip_quiet_hours = false`, 则在勿扰时间内重新调度
- 如果 task 标记 `skip_quiet_hours = true`, 或不在勿扰时间, 则创建 turn

#### Gate 检查顺序与逻辑

```text
1. 任务是否已 claim 成功           -> 未 claim 则不创建 turn
2. Bridge online                    -> 不通过则重新调度 task
3. Quiet hours (如果 skip_quiet_hours = false) -> 不通过则重新调度到勿扰结束后
4. Scheduler busy                   -> 不通过则重新调度 task (短延迟)
```

注意: UserReminderFlow **不受** daily trigger cap、autonomous trigger cap、intensity 控制, 不受 skip rules 影响。

#### Turn 创建参数

```text
TurnRequest:
  turn_type: "user_reminder_due"
  user_id: user_id
  bot_id: bot_id
  inbound_batch: null
  runtime_trigger:
    trigger_type: "due_reminder"
    trigger_kind: "user_requested"
    source_task_id: task.task_id
    is_wake_up: false               -- 提醒不适用 skip 机制
  classification:
    turn_type: "user_reminder_due"
    trigger_source: "scheduler"
```

#### 工具暴露

```text
exposed: send_text_message, mark_reminder_delivered, finish_turn
denied:  skip_initiative (提醒不能跳过)
```

#### Turn 结束后状态更新

```text
Case A: 发送成功 (至少一条 SENT_CONFIRMED):
  mark_reminder_delivered 工具调用成功
  -> task.status = "sent"
  -> 如果是 recurring = "daily" 的提醒:
     reschedule: due_at = due_at + 86400, status = "pending"

Case B: 发送失败 (全部 FAILED_CONFIRMED):
  -> task.status = "failed"
  -> task.error = terminal_state
  -> 不重新调度 (外部可查看 failed tasks)
```

#### 失败处理

```text
发送失败:
  -> mark task as failed (不重试, 不重新调度)
  -> 记录 diagnostics

mark_reminder_delivered 失败:
  -> 标记 task error, 但消息可能已送达
  -> 人工审查

LLM call failed:
  -> task.status = "failed"
  -> 不重新调度
```

### 4.3 ScheduledRitualFlow (固定仪式)

#### 触发条件

- heartbeat_tick 中, claim 到 `wake_up_type = "morning_greeting"` 或 `"night_summary"` 的到期任务
- 不在勿扰时间 (除非 08:00 早安刚好是勿扰结束边界, 见 §7)
- 优先级: 低于 UserReminderFlow, 高于 RelationshipProactiveFlow

#### Gate 检查顺序与逻辑

```text
1. scheduled_enabled 开关           -> 不通过则跳过 (不 claim, 不重新调度)
2. 具体仪式开关 (morning/night)     -> 不通过则跳过
3. 任务是否已 claim 成功            -> 未 claim 则不创建 turn
4. Bridge online                    -> 不通过则重新调度 (短延迟)
5. Quiet hours                      -> 不通过则重新调度到勿扰结束后

例外: 如果仪式时间恰好等于 quiet_hours_end (如早安 08:00 且休眠到 08:00),
       则允许在心跳中创建 turn (因为 quiet hours 已结束)
```

#### Turn 创建参数

```text
TurnRequest:
  turn_type: "scheduled_ritual"
  user_id: user_id
  bot_id: bot_id
  inbound_batch: null
  runtime_trigger:
    trigger_type: "scheduled_ritual"
    trigger_kind: "scheduled"
    source_task_id: task.task_id
    is_wake_up: false
  classification:
    turn_type: "scheduled_ritual"
    trigger_source: "scheduler"
```

#### 工具暴露

```text
exposed: send_text_message, finish_turn
denied:  skip_initiative, send_image_message, generate_image_message, send_sticker
```

默认不暴露 `skip_initiative`。如果未来需求允许 "本次仪式可跳过", 由配置控制。

#### Turn 结束后状态更新

```text
Case A: 发送成功:
  task.status = "sent"
  recurring task: reschedule to next day (due_at + 86400)

Case B: 发送失败:
  task.status = "failed"
  task.error = terminal_state

注意: ScheduledRitualFlow 不更新 InitiativeState (不更新 last_spoke_at / skip count)
```

#### 失败处理

```text
发送失败:
  -> task.status = "failed"
  -> 不重新调度 (属于 daily recurring, 由下次 scheduled_ritual builder 重新建 task)

LLM call failed:
  -> task.status = "failed"
```

***

## 5. 频率控制决策表

### 5.1 Frequency Tier Parameters

| 档位 (Intensity) | daily_trigger_cap | min_minutes | max_minutes | 标签 |
|---|---|---|---|---|
| quiet | 2 | 360 (6h) | 720 (12h) | 安静 |
| daily | 5 | 120 (2h) | 480 (8h) | 日常 |
| caring | 10 | 30 (0.5h) | 120 (2h) | 关心 |
| companion | 20 | 5 (5min) | 20 (20min) | 陪伴 |

### 5.2 Gate Decision by Condition Combination

| 条件组合 | daily_trigger_count vs cap | time_since_last_trigger vs min_minutes | cold_start_elapsed vs 120s | quiet_hours | Result | next_wake_range (min/max) |
|---|---|---|---|---|---|---|
| count < cap AND time >= min_minutes AND elapsed >= 120s AND not quiet | < cap | >= min | >= 120s | false | ALLOWED | [min_minutes, max_minutes] |
| count < cap AND time < min_minutes | < cap | < min | - | - | BLOCKED (too_soon) | [min_minutes - elapsed, max_minutes] |
| count >= cap | >= cap | - | - | - | BLOCKED (trigger_cap_reached) | N/A (cap reset at midnight) |
| elapsed < 120s | - | - | < 120s | - | BLOCKED (too_soon) | N/A |
| quiet hours | - | - | - | true | BLOCKED (quiet_hours) | N/A (wait until quiet_hours_end) |
| user_opt_out | - | - | - | - | BLOCKED (user_opt_out) | N/A |
| bridge offline | - | - | - | - | BLOCKED (bridge_offline) | N/A (heartbeat retry) |
| daily_message_cap reached | - | - | - | - | BLOCKED (daily_message_cap) | N/A |

### 5.3 Turn Type Selection (Priority Order)

| 场景 | 到期 UserReminder? | 到期 ScheduledRitual? | 到期 ProactiveWakeUp? | Cold-start eligible? | 创建的 Turn Type |
|---|---|---|---|---|---|
| 空闲心跳 | 否 | 否 | 否 | 否 | None (no turn) |
| 提醒到期 | 是 | 否/是 | 否/是 | 否/是 | user_reminder_due |
| 仪式到期 (无提醒) | 否 | 是 | 否/是 | 否/是 | scheduled_ritual |
| wake-up 到期 (无提醒/仪式) | 否 | 否 | 是 | - | relationship_proactive |
| cold-start (无任何到期) | 否 | 否 | 否 | 是 | relationship_proactive |
| 提醒 + 冷启动同时满足 | 是 | 否 | 否 | 是 | user_reminder_due (cold-start 跳过) |
| 仪式 + 冷启动同时满足 | 否 | 是 | 否 | 是 | scheduled_ritual (cold-start 跳过) |
| wake-up + 冷启动同时满足 | 否 | 否 | 是 | 是 | relationship_proactive (wake-up 优先) |
| 全部到期 (极端) | 是 | 是 | 是 | 是 | user_reminder_due (最高优先级) |

注: "是/否" 表示无论该项为是或否, 只要前序高优先级有内容即会被优先处理。

### 5.4 InitiativeState Update Matrix by Turn Outcome

| Turn 终态 | last_trigger_at | last_spoke_at | last_skipped_at | consecutive_skip_count | last_decision | daily_trigger_count | next_wake_after | 新 scheduled_wake_up |
|---|---|---|---|---|---|---|---|---|
| spoke (delivered) | now_ts | now_ts | unchanged | 0 | "spoke" | bump(+1) | now_ts + next_wake_minutes*60 | 是 (is_wake_up=false) |
| skipped | now_ts | unchanged | now_ts | 1 | "skipped" | bump(+1) | now_ts + next_wake_minutes*60 | 是 (is_wake_up=true) |
| failed (all undelivered) | now_ts | unchanged | unchanged | unchanged | unchanged | bump(+1) | 不创建 | 否 (fallback retry 另处理) |
| LLM error | unchanged | unchanged | unchanged | unchanged | unchanged | 不递增 | 不创建 | 是 (fallback retry) |
| reminder (user_reminder_due) | 不更新 | 不更新 | 不更新 | 不更新 | 不更新 | 不更新 | 不更新 | 否 (recurring 由 reminder 自身处理) |
| ritual (scheduled_ritual) | 不更新 | 不更新 | 不更新 | 不更新 | 不更新 | 不更新 | 不更新 | 否 (recurring 由 ritual builder 自身处理) |

### 5.5 Quiet Hours Behavior by Flow Type

| Flow Type | 在勿扰时间的行为 | 何时重新调度 |
|---|---|---|
| RelationshipProactiveFlow | 完全 blocked, 不创建 turn | 下一次 heartbeat (自然等待勿扰结束) |
| UserReminderFlow (skip_quiet_hours=false) | blocked, task 被 reschedule | 重新调度到 quiet_hours_end |
| UserReminderFlow (skip_quiet_hours=true) | ALLOWED, 正常创建 turn | N/A (立即创建 turn) |
| ScheduledRitualFlow | blocked, task 被 reschedule | 重新调度到 quiet_hours_end |
| ScheduledRitualFlow (边界: 仪式时间 == quiet_hours_end) | ALLOWED, 正常创建 turn | N/A (勿扰刚刚结束) |

***

## 6. 勿扰时间规则

### 6.1 核心逻辑

```text
in_quiet_hours(now_ts: float, quiet_start: str, quiet_end: str) -> bool:

    start = HH:mm -> datetime.time (e.g. 23:00)
    end   = HH:mm -> datetime.time (e.g. 08:00)
    now_time = datetime.now(tz=CST).time()

    if start <= end:
        # 不跨日: e.g. 00:00-08:00
        return start <= now_time <= end
    else:
        # 跨日: e.g. 23:00-08:00
        return now_time >= start OR now_time <= end
```

### 6.2 Exactly What Is Blocked

- 关系型主动 cold-start gate 返回 blocked
- 关系型 proactive wake-up gate 返回 blocked (除非是 `is_wake_up=true` 且仍需等勿扰结束)
- ScheduledRitualFlow gate 返回 blocked (如果 quiet_hours_enabled)

### 6.3 Exactly What Is NOT Blocked

- UserReminderFlow 如果 task 的 `skip_quiet_hours = true`
- 用户手动发送的消息 (不受 scheduler 控制)
- Admin manual trigger (不受 scheduler 控制)

### 6.4 边界处理

```text
Edge case: quiet_hours end at 08:00, morning ritual also at 08:00

如果 heartbeat 在 08:00:00 触发:
  in_quiet_hours(08:00:00) = false (因为 end=08:00, now <= end 在跨日情况下为 true,
  但 08:00 刚好等于 end, 这是否应该算在勿扰内?)

决策: 采用开区间 [start, end) 语义。
  - start 时刻进入勿扰 (inclusive)
  - end 时刻退出勿扰 (exclusive, 或 exactly-at-end = not quiet)

实现:
  if start <= end:
      return start <= now_time < end
  else:
      return now_time >= start OR now_time < end

如此:
  - 23:00:00 -> in quiet hours (inclusive start)
  - 08:00:00 -> NOT in quiet hours (exclusive end)
  - 08:00 的早安可以立即触发
```

### 6.5 勿扰结束后的行为

勿扰结束后, 下一次 heartbeat 会自动重新检查所有资格。不需要额外的 "wake after quiet hours" 回调。

对于被 reschedule 到 `quiet_hours_end` 的 task, 务必确保 `due_at` 设置为 `quiet_hours_end` 之后 (通常设为 `quiet_hours_end + 60s`, 给 heartbeat 时间到达)。

***

## 7. 任务领取 (Claim) 机制

### 7.1 原子领取伪代码

```text
claim_due_tasks(now_ts, filter_wake_up_type=None, limit=1) -> list[ScheduledTask]:

    # Step 1: Read pending due task IDs
    pending_ids = SELECT id FROM scheduled_messages
                  WHERE status = 'pending'
                    AND due_at <= now_ts
                    AND (filter by wake_up_type if provided)
                  ORDER BY due_at ASC
                  LIMIT limit

    if pending_ids is empty:
        return []

    # Step 2: Atomically transition only those still 'pending' to 'sending'
    #         关键: WHERE status = 'pending' 保证只有一个 caller 能抢到
    UPDATE scheduled_messages
    SET status = 'sending',
        updated_at = now(),
        claim_token = claim_token_generated
    WHERE id IN (pending_ids)
      AND status = 'pending'    -- 原子性关键条件

    # Step 3: Read back only the rows THIS call actually transitioned
    claimed = SELECT * FROM scheduled_messages
              WHERE id IN (pending_ids)
                AND status = 'sending'

    # Step 4: Return the tasks (may be empty if concurrent claim won)
    return parse_to_ScheduledTask(claimed)
```

### 7.2 Claim Token 格式

```text
claim_token = f"claim_{task_id}_{int(now_ts * 1000)}_{random_hex6}"

或等效简化为:
claim_token = f"claim_{turn_id}"
```

用途: 关联 ScheduledTask 与后续创建的 TurnRequest。在 turn 结束后用于标记 task 完成或失败。

### 7.3 并发领取防止 (Double-Claim Prevention)

```text
Scenario: 两个 heartbeat (或 heartbeat + admin trigger) 同时看到同一个 pending task

Timeline:
  T0: Heartbeat A reads pending_ids = [task_42]
  T1: Heartbeat B reads pending_ids = [task_42]  (同一时刻)
  T2: Heartbeat A executes UPDATE WHERE id=42 AND status='pending' -> 1 row changed
  T3: Heartbeat B executes UPDATE WHERE id=42 AND status='pending' -> 0 rows changed (status 已经是 'sending')
  T4: Heartbeat A reads back WHERE status='sending' -> [task_42] -> claim 成功
  T5: Heartbeat B reads back WHERE status='sending' -> [] (concurrent claim failed) -> claim 失败

Result:
  Heartbeat A: 创建 turn, 正常执行
  Heartbeat B: 返回空列表, 不创建 turn, 按 "no task due" 继续后续检查
```

### 7.4 Claim 失败后的行为

```text
claim_due_tasks() 返回空列表的原因:
  1. 没有 pending + due 的任务
  2. 并发 claim 失败 (被其他 caller 抢走)
  3. 任务已被过滤 (wake_up_type 不匹配)

对于原因 2, caller 应继续后续逻辑 (如检查 cold-start), 不要等待或重试 claim。
```

***

## 8. 主动状态更新

### 8.1 完整 InitiativeState 更新矩阵

```text
After proactive turn ends, update InitiativeState:

Input: TurnResult, GateDecision (from turn creation), now_ts

--- 对于 relationship_proactive turn ---

1. last_trigger_at:
   - 设置: now_ts (只要 turn 被创建并开始执行)
   - 例外: 不设置 (如果 turn creation 本身就失败了, 实际上不会有 TurnResult)

2. last_spoke_at:
   - 设置: TurnResult.delivered_count > 0 AND TurnResult.terminal_state 不是 failed
   - 不设置: 其他情况

3. last_skipped_at:
   - 设置: TurnResult.skipped == true
   - 不设置: 其他情况

4. consecutive_skip_count:
   - 设为 0: TurnResult.skipped == false
   - 设为 1: TurnResult.skipped == true
   - 不改: TurnResult.failed (LLM error)

5. last_decision:
   - 设为 "spoke": TurnResult.delivered_count > 0
   - 设为 "skipped": TurnResult.skipped == true
   - 不改: TurnResult.failed

6. daily_trigger_count:
   - bump(+1): turn 正常完成 (spoke 或 skipped)
   - 不 bump: turn 因 LLM error 失败

7. next_wake_after:
   - 设置: now_ts + next_wake_minutes * 60 (next_wake_minutes from TurnResult)
   - 不设置: turn failed

8. next_wake_before:
   - 设置: next_wake_after + (max_minutes - min_minutes) * 60
   - 不设置: turn failed

9. intensity_profile:
   - 不变 (外部可改, scheduler 内部不做档位自动切换)

10. proactive_paused_until:
    - 不变 (用户 opt-out 由外部 (如 admin API) 管理)

--- 创建新 scheduled_wake_up task ---

如果 turn 正常完成 (spoke 或 skipped):
  wake_up_type = "proactive"
  is_wake_up = (consecutive_skip_count == 1)
  due_at = next_wake_after
  message_hint = f"主动消息自动唤醒（上次: {previous_hint[:100]}）"
  skip_quiet_hours = false
  recurring = None

如果 turn 失败 (LLM error):
  创建 fallback wake_up:
    wake_up_type = "proactive"
    is_wake_up = last_decision == "skipped"  -- 保持上次的 is_wake_up 状态
    due_at = now_ts + avg(min_minutes, max_minutes) * 60
    message_hint = "故障恢复重试"
    skip_quiet_hours = false
    recurring = None

--- 标记原 source_task ---

如果 turn 是从 scheduled_wake_up task claim 而来:
  delivered > 0:
    task.status = "sent"
  delivered == 0 AND not skipped:
    task.status = "failed"
    task.error = terminal_state
  skipped:
    task.status = "sent"  -- skip 也是一种正常完成
```

### 8.2 跨日重置逻辑

```text
在 get_autonomous_trigger_count() 中内建日期检查:
  daily_trigger_date != today_str -> daily_trigger_count 重置为 0

在 count_today_proactive_sent() 中按 today_start_ts 过滤:
  WHERE sent = 1 AND created_at >= today_start_ts

不需要单独的跨日重置定时任务。
```

***

## 9. 技术熔断规则

### 9.1 Max Consecutive Triggers Without User Reply (Soft Warning)

```text
要求文档 §13.2: 不设置 "用户不回复几次后必须停" 的产品规则。
但保留技术级熔断。

实现:
  如果 last_trigger_at 和 last_spoke_at 之间的 spoke 次数占比过低,
  系统发出 warning log 但不强制停止:

  if daily_trigger_count >= 5 AND spoke_percentage < 20%:
    logger.warning("scheduler: low engagement: %d triggers, only %d%% spoke",
                   daily_trigger_count, spoke_percentage)

  不阻止后续触发。这是给开发者的 signal, 不是用户可见的限制。
```

### 9.2 Same Task Type Max Frequency

```text
防止同一个 wake_up_type 的任务在短时间内重复创建:

  create_scheduled_wake_up() 前检查:
    pending_proactive = get_pending_proactive_wake_up(user_id, bot_id)
    if pending_proactive exists:
      不创建新的 pending task (已有排期)

  这防止了每次 heartbeat 都创建新 wake-up task 导致的堆积。
```

### 9.3 Duplicate Claim Prevention

```text
§7 中描述的原子 claim 机制。

SQL 层面的保护:
  UPDATE ... WHERE status = 'pending'   -- 只能改 pending 行

Python 层面的保护:
  async lock 保护 claim_due_scheduled_messages() 内部操作
```

### 9.4 Heartbeat Failure Backoff

```text
如果 heartbeat 本身失败 (非 turn 内失败):

  记录 heartbeat failure count
  递增退避: failure_count * 30s, 最大 300s
  下一次 heartbeat 在 backoff 后执行

  如果 heartbeat 连续失败 >= 10 次:
    logger.error("scheduler: heartbeat stall detected")
    stall_detected = true
    (没有自动恢复机制, 需人工介入)

heartbeat 恢复后:
  failure_count 重置为 0
  stall_detected = false
```

### 9.5 Scheduler Stall Detection

```text
检测逻辑:
  if (last_heartbeat_at is not None AND now_ts - last_heartbeat_at > heartbeat_interval * 10):
    stall_detected = true
    logger.critical("scheduler: stalled for %.0fs, last heartbeat at %s",
                    now_ts - last_heartbeat_at, last_heartbeat_at)

恢复:
  下次 heartbeat_tick() 成功执行后:
    stall_detected = false

暴露:
  admin API GET /scheduler/state 返回 stall_detected
```

### 9.6 Max Simultaneous Wake-Up Tasks Per User

```text
防止单个用户堆积过多 pending wake-up task:

  在 create_scheduled_wake_up() 前:
    cancel_pending_proactive_wake_ups(user_id, bot_id)  -- 先取消旧 pending
    然后再创建新的

  这保证每个 (user_id, bot_id) 最多只有一个 pending proactive wake-up task。
```

### 9.7 Scheduler Lock Deadlock Prevention

```text
lock acquisition 必须有超时:
  try:
    acquired = await asyncio.wait_for(lock.acquire(), timeout=5.0)
    if not acquired:
      logger.error("scheduler: lock timeout, skipping heartbeat")
      return None
  except asyncio.TimeoutError:
    logger.error("scheduler: lock acquire timed out")
    return None

lock 持有时间上限:
  整个 turn 执行过程持有 lock
  如果 turn 超过 300s 仍未完成(由 TurnEngine 的 budget 控制):
    记录 warning, lock 在 finally 中释放
```

***

## 10. 决策表

### 10.1 Gate Check Outcomes by Condition Combination

| 检查项 | 条件 | 通过 | 不通过原因 | 影响流 |
|---|---|---|---|---|
| global_enabled | config.enabled=false | false | proactive_disabled | 所有流 |
| autonomous_enabled | config.autonomous_enabled=false | false | autonomous_disabled | RelationshipProactiveFlow only |
| bridge_online | bridge offline | false | bridge_offline | 所有流 |
| quiet_hours | 在勿扰时间内 | false | sleep_hours | RelationshipProactiveFlow, ScheduledRitualFlow |
| quiet_hours (reminder with skip) | 在勿扰, task.skip=true | true | - | UserReminderFlow allowed |
| daily_message_cap | sent_today >= cap | false | daily_cap_exceeded | 所有流 (但 UserReminderFlow 不受此限制) |
| trigger_cap | count >= intensity_cap | false | trigger_cap_reached | RelationshipProactiveFlow only |
| user_opt_out | paused_until > now | false | user_opt_out | RelationshipProactiveFlow only |
| cold_start_elapsed | elapsed < 120s | false | too_soon | RelationshipProactiveFlow cold-start variant only |
| scheduler_busy | lock.is_locked() | false | scheduler_busy | 所有流 |
| circuit_breaker | cb_tripped | false | circuit_breaker_<reason> | 所有流 |

### 10.2 Turn Type Selection (reminder vs ritual vs proactive vs none)

见 §5.3 的决策表。完整优先级顺序:

```text
1. UserReminderFlow   (最高)
2. ScheduledRitualFlow
3. RelationshipProactiveFlow (wake-up variant)
4. RelationshipProactiveFlow (cold-start variant)
5. None                (无事可做)
```

### 10.3 InitiativeState Update Matrix by Turn Outcome

见 §8.1 完整更新矩阵。

### 10.4 Frequency Tier Parameters

见 §5.1 频率档位参数表。

### 10.5 Quiet Hours Behavior by Flow Type

见 §5.5 勿扰时间行为表。

### 10.6 Scheduled Task Status Transitions

| From | To | Trigger | 条件 |
|---|---|---|---|
| pending | sending | claim_due_tasks() | due_at <= now AND status = 'pending' |
| sending | sent | turn 完成 (delivered > 0 或 skipped) | mark_scheduled_message_status() |
| sending | failed | turn 完成 (delivered = 0, not skipped) | mark_scheduled_message_status() |
| pending | cancelled | cancel_pending_proactive_wake_ups() | wake_up_type = 'proactive' |
| sent | pending | reschedule (recurring daily) | 仅 recurring 任务, due_at += 86400 |
| failed | pending | 人工重试 | admin API |

***

## 11. 核心伪代码

### 11.1 run_heartbeat() 完整伪代码

```text
async run_heartbeat() -> SchedulerResult:

    now_ts = time.time()

    # ---- Pre-checks ----
    if not config.enabled:
        return SchedulerResult(turn_created=false, reason="global_disabled")

    if lock.is_locked():
        return SchedulerResult(turn_created=false, reason="scheduler_busy")

    # ---- Attempt claim + execute ----
    try:
        acquired = await asyncio.wait_for(lock.acquire(), timeout=5.0)
    except asyncio.TimeoutError:
        logger.error("run_heartbeat: lock acquire timeout")
        return SchedulerResult(turn_created=false, reason="lock_timeout")

    try:
        turn_id = f"sched_{int(now_ts * 1000)}"
        active_turn_id = turn_id

        # ---- Priority 1: User Reminder ----
        reminder_task = await claim_due_reminder(now_ts)
        if reminder_task:
            if _should_deliver_reminder_now(reminder_task, now_ts):
                trigger = RuntimeTrigger(
                    trigger_type="due_reminder",
                    source_task_id=reminder_task.task_id,
                    reason=f"用户授权提醒到期: {reminder_task.payload.messages[0].text[:100]}",
                    due_at=reminder_task.due_at,
                    claimed_at=now_ts,
                    trigger_kind="user_requested",
                    is_wake_up=false
                )
                turn_request = TurnRequest(
                    turn_type="user_reminder_due",
                    user_id=reminder_task.user_id,
                    bot_id=reminder_task.bot_id,
                    inbound_batch=None,
                    runtime_trigger=trigger
                )
                result = await turn_engine.execute(turn_request)
                await _update_reminder_state(reminder_task, result)
                return SchedulerResult(turn_created=true, turn_type="user_reminder_due", result=result)
            else:
                await _reschedule_to_quiet_hours_end(reminder_task)
                return SchedulerResult(turn_created=false, reason="reminder_quiet_hours_blocked")

        # ---- Priority 2: Scheduled Ritual ----
        ritual_task = await claim_due_ritual(now_ts)
        if ritual_task:
            if not in_quiet_hours(now_ts):
                trigger = RuntimeTrigger(
                    trigger_type="scheduled_ritual",
                    source_task_id=ritual_task.task_id,
                    reason=f"定时仪式: {ritual_task.payload.messages[0].wake_up_type}",
                    due_at=ritual_task.due_at,
                    claimed_at=now_ts,
                    trigger_kind="scheduled",
                    is_wake_up=false
                )
                turn_request = TurnRequest(
                    turn_type="scheduled_ritual",
                    user_id=ritual_task.user_id,
                    bot_id=ritual_task.bot_id,
                    inbound_batch=None,
                    runtime_trigger=trigger
                )
                result = await turn_engine.execute(turn_request)
                await _update_ritual_state(ritual_task, result)
                return SchedulerResult(turn_created=true, turn_type="scheduled_ritual", result=result)
            else:
                await _reschedule_to_quiet_hours_end(ritual_task)
                return SchedulerResult(turn_created=false, reason="ritual_quiet_hours_blocked")

        # ---- Priority 3: Proactive Wake-Up (already scheduled) ----
        wake_up_task = await claim_due_wake_up(now_ts)
        if wake_up_task:
            gate = await check_proactive_gate(user_id, bot_id, now_ts)
            if gate.allowed:
                trigger = RuntimeTrigger(
                    trigger_type="heartbeat",
                    source_task_id=wake_up_task.task_id,
                    reason=f"主动唤醒: {wake_up_task.payload.messages[0].text[:100]}",
                    due_at=wake_up_task.due_at,
                    claimed_at=now_ts,
                    trigger_kind="wake_up",
                    is_wake_up=wake_up_task.payload.messages[0].is_wake_up
                )
                turn_request = TurnRequest(
                    turn_type="relationship_proactive",
                    user_id=wake_up_task.user_id,
                    bot_id=wake_up_task.bot_id,
                    inbound_batch=None,
                    runtime_trigger=trigger
                )
                result = await turn_engine.execute(turn_request)
                await _update_proactive_state(
                    user_id, bot_id, result, gate, wake_up_task
                )
                return SchedulerResult(turn_created=true, turn_type="relationship_proactive", result=result)
            else:
                # gate not allowed: cancel or requeue
                await _handle_blocked_wake_up(wake_up_task, gate)
                return SchedulerResult(turn_created=false, reason=gate.reason)

        # ---- Priority 4: Cold-Start Proactive ----
        if await is_cold_start_eligible(user_id, bot_id, now_ts):
            gate = await check_proactive_gate(user_id, bot_id, now_ts)
            if gate.allowed:
                trigger = RuntimeTrigger(
                    trigger_type="heartbeat",
                    source_task_id=None,
                    reason="cold_start: 距离上次可见对话超过阈值",
                    claimed_at=now_ts,
                    trigger_kind="cold_start",
                    is_wake_up=gate.requires_forced_speak
                )
                turn_request = TurnRequest(
                    turn_type="relationship_proactive",
                    user_id=user_id,
                    bot_id=bot_id,
                    inbound_batch=None,
                    runtime_trigger=trigger
                )
                result = await turn_engine.execute(turn_request)
                await _update_proactive_state(
                    user_id, bot_id, result, gate, None
                )
                return SchedulerResult(turn_created=true, turn_type="relationship_proactive", result=result)

        # ---- Nothing to do ----
        return SchedulerResult(turn_created=false, reason="nothing_due")

    except Exception as e:
        logger.exception("run_heartbeat: unhandled error")
        return SchedulerResult(turn_created=false, reason=f"error: {str(e)[:200]}")

    finally:
        active_turn_id = None
        lock.release()
        last_heartbeat_at = now_ts
```

### 11.2 claim_and_create_turn() 伪代码

```text
async claim_and_create_turn(
    now_ts: float,
    wake_up_type_filter: str,
    turn_type: TurnType,
) -> TurnRequest | None:

    # Step 1: Atomic claim
    tasks = await claim_due_scheduled_messages(
        now_ts=now_ts,
        limit=1,
        user_id=None  # claim for any user
    )

    # Filter by wake_up_type in application layer
    matching = [t for t in tasks
                if t.payload.messages[0].wake_up_type == wake_up_type_filter]

    if not matching:
        return None  # nothing claimed

    task = matching[0]

    # Step 2: Check quiet hours (if applicable)
    if wake_up_type_filter in ("morning_greeting", "night_summary"):
        if in_quiet_hours(now_ts):
            # Reschedule to quiet hours end
            reschedule_to = quiet_hours_end_timestamp(now_ts) + 60
            await reschedule_scheduled_message(task.task_id, reschedule_to)
            return None

    # Step 3: Build RuntimeTrigger
    trigger = RuntimeTrigger(
        trigger_id=f"trig_{task.task_id}_{int(now_ts*1000)}",
        trigger_type=_trigger_type_for_wake_up(wake_up_type_filter),
        source_task_id=task.task_id,
        reason=f"claimed task {task.task_id}: {wake_up_type_filter}",
        due_at=task.due_at,
        claimed_at=now_ts,
        trigger_kind=_trigger_kind_for_wake_up(wake_up_type_filter),
        is_wake_up=task.payload.messages[0].is_wake_up,
        intensity=scheduler_config.autonomous_intensity,
        metadata={"task": task}
    )

    # Step 4: Build TurnRequest
    turn_request = TurnRequest(
        turn_id=f"turn_{task.task_id}_{int(now_ts*1000)}",
        turn_type=turn_type,
        user_id=task.user_id,
        bot_id=task.bot_id,
        inbound_batch=None,
        runtime_trigger=trigger,
        classification=TurnClassification(
            turn_type=turn_type,
            trigger_source="scheduler"
        ),
        created_at=datetime.fromtimestamp(now_ts)
    )

    return turn_request
```

### 11.3 _update_proactive_state() 伪代码

```text
async _update_proactive_state(
    user_id, bot_id, turn_result, gate_decision, source_task
) -> None:

    now_ts = time.time()
    settings = get_settings()
    profile = get_intensity_profile(settings.autonomous_intensity)

    # 1. Determine outcome
    delivered = turn_result.delivered_message_count
    skipped = turn_result.skipped
    failed = turn_result.failed

    # 2. Update InitiativeState fields
    last_decision = "unset"

    if failed:
        # LLM error: don't update spoke/skipped, don't bump trigger count
        last_decision = gate_decision.last_decision  # keep previous
        next_wake_delay = (profile.min_minutes + profile.max_minutes) // 2
    elif skipped:
        # Model chose to skip
        last_decision = "skipped"
        await bump_autonomous_trigger_count(user_id, bot_id)
        next_wake_delay = turn_result.next_wake_minutes or profile.min_minutes
    elif delivered > 0:
        # Model spoke
        last_decision = "spoke"
        await bump_autonomous_trigger_count(user_id, bot_id)
        next_wake_delay = turn_result.next_wake_minutes or profile.min_minutes
    else:
        # Edge: neither spoke nor skipped nor failed
        # treat as failed
        next_wake_delay = (profile.min_minutes + profile.max_minutes) // 2

    # 3. Clamp next_wake_delay to intensity range
    next_wake_delay = max(profile.min_minutes, min(profile.max_minutes, int(next_wake_delay)))

    # 4. Update dialogue_state
    await store.update_dialogue_state(user_id, bot_id, {
        "last_proactive_trigger_at": now_ts,
        "last_proactive_spoke_at": now_ts if delivered > 0 else None,
        "last_proactive_skipped_at": now_ts if skipped else None,
        "consecutive_skip_count": 1 if skipped else 0,
        "last_proactive_decision": last_decision,
        "next_wake_after": now_ts + next_wake_delay * 60,
    })

    # 5. Create next scheduled wake-up
    if not failed or (failed and turn_result.terminal_state == "hard_fallback"):
        await cancel_pending_proactive_wake_ups(user_id, bot_id)
        await create_scheduled_wake_up(user_id, bot_id, {
            "message_hint": f"主动消息自动唤醒（上次: {gate_decision.reason[:100]}）",
            "trigger_at": datetime.fromtimestamp(
                now_ts + next_wake_delay * 60, tz=timezone(timedelta(hours=8))
            ).isoformat(),
            "recurring": None,
            "skip_quiet_hours": False,
            "wake_up_type": "proactive",
            "is_wake_up": (last_decision == "skipped"),
        })

    # 6. Mark source task
    if source_task:
        if delivered > 0 or skipped:
            await mark_scheduled_message_status(source_task.task_id, "sent", "")
        else:
            await mark_scheduled_message_status(
                source_task.task_id, "failed", str(turn_result.terminal_state)[:200]
            )
```

***

## 12. 禁止行为

1. **Generating message text in scheduler.** SchedulerRuntime 不能生成任何文案、提示词、或模型输入。它只创建 TurnRequest。

2. **Calling model directly.** SchedulerRuntime 不能调用 ProviderGateway、LLMGateway 或任何 LLM API。模型调用只能通过 TurnEngine。

3. **Creating turns for unclaimed tasks.** 任何 scheduled_messages 中的任务必须先 claim (status: pending -> sending) 才能创建 TurnRequest。

4. **Exposing image/voice/sticker tools in proactive turns.** 关系型主动 turn 和仪式 turn 的工具暴露中禁止 `send_image_message`, `generate_image_message`, `send_sticker`, 及任何语音工具。SchedulerRuntime 必须通过 turn_type 标记确保 ToolExposurePolicy 正确限制。

5. **Allowing consecutive skip without forced speak.** 如果 last_decision = "skipped", 下一轮必须设置 is_wake_up=true。ToolExposurePolicy 根据此标记不暴露 skip_initiative。

6. **Exceeding frequency caps.** daily_trigger_count 超过 intensity 的 daily_trigger_cap 时, gate 必须返回 blocked。冷启动触发不计入此 cap (因为 cold-start 进入时也会进行 gate check)。

7. **Bypassing quiet hours for non-reminder flows.** 关系型主动和定时仪式在勿扰时间内不可触发。只有标记 skip_quiet_hours=true 的 UserReminderFlow 可绕过。

8. **Constructing prompts in scheduler.** SchedulerRuntime 不参与 ContextBuilder 的任何工作。它不构建系统提示、不拼历史、不选择记忆。

9. **Writing history in scheduler.** SchedulerRuntime 不调用任何历史写入方法。WRITE_MODEL_HISTORY 由 TurnEngine 的 post-terminal pipeline 处理。

10. **Double-claiming tasks.** 原子 claim 机制已保证, 任何代码路径都不能绕过这个机制直接 claim 任务。

11. **Creating proactive turns when global switch is off.** SchedulerConfig.enabled=false 时, 任何 heartbeat 都不能创建关系型主动 turn。但提醒和仪式 turn 受各自开关独立控制。

12. **Confusing reminder tasks with proactive cold-start.** UserReminderFlow 的任务 wake_up_type="user_requested", RelationshipProactiveFlow 的任务 wake_up_type="proactive"。代码不能混用两个类型。

13. **Incrementing daily_trigger_count on LLM failure.** 如果 turn 因 LLM error 失败 (不是模型选择 skip), daily_trigger_count 不递增。用户不应为系统故障买单。

14. **Leaving hanging 'sending' tasks.** 如果 turn 创建后崩溃, scheduled_message 的 status 可能永远留在 'sending'。需要 watchdog 定期将超时的 'sending' 任务重置为 'pending' (建议 threshold: 600s)。

***

## 13. 具体输入输出例子

### 例子 1: 正常心跳 — 无事发生

```text
输入:
  heartbeat_tick(now_ts=2026-06-03T14:30:00+08:00)
  config.enabled=true, autonomous_enabled=true
  没有到期的 scheduled_messages (pending + due_at <= now)
  cold_start: last_visible_message_at=2026-06-03T14:28:00+08:00, elapsed=120s
  在勿扰时间之外 (14:30)
  daily trigger count: 2/5 (daily profile)

执行:
  1. claim_due_reminder() -> []
  2. claim_due_ritual() -> []
  3. claim_due_wake_up() -> []
  4. is_cold_start_eligible():
       elapsed >= 120s -> true
  5. check_proactive_gate() -> InitiativeGateDecision(allowed=true)
  6. 创建 turn_request (turn_type="relationship_proactive", trigger_kind="cold_start")
  7. turn_engine.execute(turn_request)
  8. 模型选择 speak, 发送一条消息
  9. turn_result: delivered_count=1, skipped=false, next_wake_minutes=180

输出:
  SchedulerResult(turn_created=true, turn_type="relationship_proactive")
  状态更新:
    InitiativeState.last_trigger_at = now_ts
    InitiativeState.last_spoke_at = now_ts
    InitiativeState.last_decision = "spoke"
    InitiativeState.consecutive_skip_count = 0
    daily_trigger_count 从 2 -> 3
    创建新 scheduled_wake_up (due_at = now_ts + 180*60, is_wake_up=false)
```

### 例子 2: 心跳 — 用户提醒到期

```text
输入:
  heartbeat_tick(now_ts=2026-06-03T15:00:00+08:00)
  scheduled_messages 表中有 task_001:
    status="pending", due_at=14:59:00, wake_up_type="user_requested",
    skip_quiet_hours=false
  cold_start 也满足 (elapsed=600s)
  在勿扰时间之外

执行:
  1. claim_due_reminder():
       claim_due_scheduled_messages(now_ts) -> [task_001]
       过滤 wake_up_type="user_requested" -> [task_001]
       task_001.status 从 "pending" 变为 "sending"
  2. reminder_task != None: _should_deliver_reminder_now():
       skip_quiet_hours=false AND not in_quiet_hours -> true
  3. 创建 turn_request (turn_type="user_reminder_due")
  4. turn_engine.execute(turn_request)
  5. 模型发送提醒消息
  6. turn_result: delivered_count=1

输出:
  SchedulerResult(turn_created=true, turn_type="user_reminder_due")
  task_001.status = "sent"
  不创建新的 proactive wake-up
  不更新 InitiativeState
  心跳没有检查 cold-start (提醒优先)
```

### 例子 3: 心跳 — 定时仪式 (早安)

```text
输入:
  heartbeat_tick(now_ts=2026-06-03T08:00:30+08:00)
  scheduled_messages 表中有 task_morning:
    status="pending", due_at=08:00:00, wake_up_type="morning_greeting"
  quiet_hours: 23:00-08:00
  now_time=08:00:30 -> NOT in_quiet_hours (end exclusive: 08:00 已结束)

执行:
  1. claim_due_reminder() -> []
  2. claim_due_ritual():
       claim_due_scheduled_messages(now_ts) -> [task_morning]
       过滤 wake_up_type="morning_greeting" -> [task_morning]
  3. in_quiet_hours(08:00:30) -> false
  4. 创建 turn_request (turn_type="scheduled_ritual")
  5. turn_engine.execute(turn_request)
  6. 模型发送早安消息

输出:
  SchedulerResult(turn_created=true, turn_type="scheduled_ritual")
  task_morning.status = "sent"
  recurring daily -> reschedule due_at += 86400, status = "pending"
```

### 例子 4: 心跳 — 主动 skip 后状态更新

```text
输入:
  heartbeat_tick(now_ts=2026-06-03T16:00:00+08:00)
  InitiativeState.last_decision = None (之前没有 skip)
  冷启动满足, gate passed

执行:
  1-3. 无提醒, 无仪式, 无 wake-up
  4. cold_start eligible
  5. 创建 turn_request (is_wake_up=false)
  6. turn_engine.execute(turn_request)
  7. 模型调用 skip_initiative(reason="用户刚聊过，没什么新鲜话题")
  8. 模型调用 finish_turn(next_wake_minutes=240)
  9. turn_result: skipped=true, delivered_count=0, next_wake_minutes=240

输出:
  InitiativeState.last_trigger_at = now_ts
  InitiativeState.last_skipped_at = now_ts
  InitiativeState.last_decision = "skipped"
  InitiativeState.consecutive_skip_count = 1
  daily_trigger_count += 1
  创建新 scheduled_wake_up:
    is_wake_up = true    (下次必须强制开口)
    due_at = now_ts + 240*60
```

### 例子 5: 心跳 — 上次 skip 后本次强制开口

```text
输入:
  heartbeat_tick(now_ts=2026-06-03T20:00:00+08:00)
  scheduled_messages 中有 task_wake_42:
    status="pending", due_at=20:00:00, wake_up_type="proactive", is_wake_up=true
  InitiativeState.last_decision = "skipped"
  InitiativeState.consecutive_skip_count = 1

执行:
  1-2. 无提醒, 无仪式
  3. claim_due_wake_up() -> claim_due_scheduled_messages() -> [task_wake_42]
     过滤 wake_up_type="proactive" -> [task_wake_42]
  4. check_proactive_gate() -> InitiativeGateDecision(
       allowed=true, requires_forced_speak=true (因为 task_wake_42.is_wake_up=true)
     )
  5. 创建 turn_request (is_wake_up=true)
  6. turn_engine.execute(turn_request)
  7. ToolExposurePolicy 看到 is_wake_up=true, 不暴露 skip_initiative
  8. 模型暴露的工具: [send_text_message, finish_turn]
  9. 模型发送一条消息
  10. turn_result: delivered_count=1, skipped=false

输出:
  InitiativeState.last_decision = "spoke"
  InitiativeState.consecutive_skip_count = 0
  创建新 scheduled_wake_up (is_wake_up=false)
```

### 例子 6: 心跳 — 勿扰时间阻止所有

```text
输入:
  heartbeat_tick(now_ts=2026-06-04T02:30:00+08:00)
  config.enabled=true, autonomous_enabled=true
  scheduled_messages 中有:
    task_reminder (wake_up_type="user_requested", skip_quiet_hours=false, due_at=02:00)
    task_proactive (wake_up_type="proactive", due_at=02:15)
  quiet_hours: 23:00-08:00
  in_quiet_hours(02:30) -> true

执行:
  1. claim_due_reminder():
       claim_due_scheduled_messages() -> [task_reminder]
       _should_deliver_reminder_now():
         skip_quiet_hours=false AND in_quiet_hours=true -> false
       -> reschedule task_reminder to 08:00:00 + 60s
  2-3. claim_due_ritual() -> [] (没有仪式任务)
  4. claim_due_wake_up():
       claim_due_scheduled_messages() -> [task_proactive]
       但 check_proactive_gate() -> allowed=false (quiet_hours_blocked=true)
       -> 不创建 turn, 不 cancel task (等下次心跳)

输出:
  SchedulerResult(turn_created=false, reason="reminder_quiet_hours_blocked")
  task_reminder 被 reschedule 到 08:01:00
  task_proactive 留在 pending, 等勿扰结束后下次心跳 claim
```

### 例子 7: 心跳 — 全局开关关闭

```text
输入:
  heartbeat_tick(now_ts=2026-06-03T14:00:00+08:00)
  config.enabled = FALSE

执行:
  立即返回, 不进行任何 claim 或 gate check

输出:
  SchedulerResult(turn_created=false, reason="global_disabled")
```

### 例子 8: 心跳 — 提醒和冷启动同时满足, 提醒优先

```text
输入:
  heartbeat_tick(now_ts=2026-06-03T15:00:00+08:00)
  task_reminder: status="pending", due_at=14:59, wake_up_type="user_requested"
  cold_start: eligible (elapsed=200s, gate would pass)

执行:
  1. claim_due_reminder() -> [task_reminder] (claim 成功)
  2. reminder 处理: 创建 user_reminder_due turn
  3. (不继续检查 cold-start, 因为已经创建了 turn)

输出:
  SchedulerResult(turn_created=true, turn_type="user_reminder_due")
  本次心跳没有创建 relationship_proactive turn
```

### 例子 9: 心跳 — 每日 cap 达到, 冷启动 blocked

```text
输入:
  heartbeat_tick(now_ts=2026-06-03T18:00:00+08:00)
  intensity="daily", daily_trigger_cap=5
  daily_trigger_count=5 (已达上限)
  cold_start: eligible (elapsed=500s)

执行:
  1-3. 无提醒, 无仪式, 无 wake-up
  4. is_cold_start_eligible() -> true
  5. check_proactive_gate():
       trigger_cap check: count(5) >= cap(5) -> false
       return InitiativeGateDecision(allowed=false, reason="trigger_cap_reached")
  6. 不创建 turn

输出:
  SchedulerResult(turn_created=false, reason="trigger_cap_reached")
  下一次心跳仍然会到达 trigger_cap check, 直到跨日重置
```

### 例子 10: 心跳 — 提醒任务不受 daily trigger cap 限制

```text
输入:
  heartbeat_tick(now_ts=2026-06-03T19:00:00+08:00)
  daily_trigger_cap=5, daily_trigger_count=5 (cap reached)
  task_reminder: status="pending", due_at=18:59, wake_up_type="user_requested"
  cold_start: eligible

执行:
  1. claim_due_reminder() -> [task_reminder]
  2. _should_deliver_reminder_now() -> true (不在勿扰)
  3. 创建 user_reminder_due turn (不经过 proactive gate check)
  4. turn 成功完成

输出:
  SchedulerResult(turn_created=true, turn_type="user_reminder_due")
  提醒成功送达, daily_trigger_count 保持 5 (不递增, 因为 reminder 不更新 InitiativeState)
```

### 例子 11: 并发 claim 导致 claim 失败

```text
输入:
  两个 heartbeat (A 和 B) 几乎同时到达
  task_shared: status="pending", due_at 刚刚到期

执行:
  Heartbeat A:
    claim_due_scheduled_messages() -> SELECT pending ids -> [task_shared]
    UPDATE WHERE id=task_shared AND status='pending' -> 1 row changed
    SELECT WHERE status='sending' -> [task_shared]
    claim 成功 -> 创建 turn

  Heartbeat B (稍晚几毫秒):
    claim_due_scheduled_messages() -> SELECT pending ids -> [task_shared]
    UPDATE WHERE id=task_shared AND status='pending' -> 0 rows changed (已被 A 改为 sending)
    SELECT WHERE status='sending' -> [task_shared] (但 UPDATE 返回 0, 说明不是 B 改的)
    claim 失败 -> []

输出:
  Heartbeat A: turn_created=true
  Heartbeat B: 没有创建 turn (task_shared claim 失败, 继续后续检查)
  不会出现同一 task 被两次创建 turn
```

### 例子 12: 连续触发但用户无回复 — Soft Warning

```text
输入:
  一天内 heartbeat 触发了 5 次 proactive turn
  其中 4 次模型 skipped, 1 次 spoke
  spoke_percentage = 20%

执行:
  第 6 次 heartbeat:
    check_proactive_gate() 通过 (count=5 < cap=5 -> 如果是 daily 档位, 刚好到达上限)
    在 gate check 后或 metrics 收集中:
      logger.warning("scheduler: low engagement: 5 triggers, only 20% spoke")

输出:
  不阻止第 6 次触发 (如果是 caring 或 companion 档位)
  如果 count 已达 cap, 则在 trigger_cap check 中被阻止
  soft warning 仅作为日志记录
```

***

## 14. 当前代码迁移点

### 14.1 agent/scheduler.py (ProactiveScheduler)

| 当前代码 | 目标对应 | 说明 |
|---|---|---|
| `ProactiveScheduler._get_settings()` | `SchedulerRuntime.load_config()` | 设置加载逻辑, 从 config.yaml 和 runtime_settings.json 读取 |
| `ProactiveScheduler._check_eligibility()` | `check_proactive_gate()` | 资格检查, 需扩展为返回完整的 `InitiativeGateDecision` |
| `ProactiveScheduler._claim_due_wake_up()` | `claim_due_tasks()` | claim 逻辑, 需支持按 wake_up_type 过滤 |
| `ProactiveScheduler.dispatch_wake_up()` | `run_heartbeat()` 中的 turn 执行部分 | 统一分发入口 |
| `ProactiveScheduler._schedule_next_from_turn_result()` | `_update_proactive_state()` | 状态更新 + 排程下一轮 |
| `ProactiveScheduler._schedule_fallback_retry()` | 合入 `_update_proactive_state()` | 失败重试逻辑 |
| `ProactiveScheduler.check_cold_start()` | `is_cold_start_eligible()` | 冷启动检测 |
| `ProactiveScheduler.COLD_START_SECONDS` | `SchedulerConfig.cold_start_seconds` | 移至配置 |
| `ProactiveScheduler._in_sleep_hours()` | `in_quiet_hours()` | 勿扰时间判断, 边界用开区间 [start, end) |

### 14.2 main.py heartbeat loop

| 当前代码 | 目标对应 | 说明 |
|---|---|---|
| `_heartbeat_loop()` | `SchedulerRuntime.run_heartbeat_loop()` | 心跳循环, 改为调用 SchedulerRuntime |
| `features.proactive` 开关检查 | `SchedulerConfig.enabled` | 全局开关 |
| 直接调用 `scheduler.run_heartbeat()` | 改为 `scheduler_runtime.heartbeat_tick()` | 统一入口 |

### 14.3 memory/store.py scheduled_messages

| 当前方法 | 目标对应 | 说明 |
|---|---|---|
| `claim_due_scheduled_messages()` | 保留 `RuntimeStore.claim_due_tasks()` | claim 逻辑不变, 增加语义封装 |
| `create_scheduled_wake_up()` | `RuntimeStore.create_scheduled_task()` | 通用化 task 创建 |
| `mark_scheduled_message_status()` | `RuntimeStore.update_task_status()` | 状态标记 |
| `reschedule_scheduled_message()` | `RuntimeStore.reschedule_task()` | 重新调度 |
| `get_pending_proactive_wake_up()` | 合入 `claim_due_tasks()` 前置检查 | pending 重复检查 |
| `cancel_pending_proactive_wake_ups()` | `RuntimeStore.cancel_pending_tasks()` | 取消旧任务 |

### 14.4 agent/proactive_settings.py

| 当前代码 | 目标对应 | 说明 |
|---|---|---|
| `ProactiveSettings` | `SchedulerConfig` | 配置数据结构 |
| `INTENSITY_PROFILES` | `IntensityProfile` table | 频率档位不变, 接口更明确 |
| `get_intensity_profile()` | `SchedulerConfig.get_intensity_profile()` | 档位解析 |
| `load_proactive_settings()` | `SchedulerConfig.from_config()` | 配置加载 |

### 14.5 wake_up payload 兼容

当前 `scheduled_messages.messages_json` 中的 `messages[0]` 结构已包含:
- `text` (message_hint)
- `wake_up_type` ("proactive" | "morning_greeting" | "night_summary" | "user_requested")
- `is_wake_up` (bool)
- `skip_quiet_hours` (bool)
- `recurring` (str | None)

这些字段与目标 `ScheduledTask.payload` 兼容。迁移时不需要修改表结构, 只需要在 code 层面添加语义封装。

### 14.6 数据库表

当前 `scheduled_messages` 表字段:
- `id` (INTEGER PRIMARY KEY)
- `user_id` (TEXT)
- `bot_id` (TEXT)
- `contact_id` (TEXT)
- `messages_json` (TEXT)
- `due_at` (REAL)
- `status` (TEXT: pending | sending | sent | failed | cancelled)
- `error` (TEXT)
- `created_at` (REAL)
- `updated_at` (REAL)

迁移时不需要改表结构。未来可添加:
- `claim_token` (TEXT) - 关联 turn_id
- `task_type` (TEXT) - 显式类型字段 (proactive_wake_up | user_reminder | scheduled_ritual | admin_scheduled)

当前 task_type 隐含在 `messages_json[0].wake_up_type` 中。

***

## 15. 必须测试的用例列表

### 15.1 Heartbeat 基本流程

1. **TC-SR-001: 无事发生的 heartbeat**
   - 无到期 task, cold-start 不满足 -> heartbeat 返回 turn_created=false

2. **TC-SR-002: 全局开关关闭**
   - config.enabled=false -> heartbeat 立即返回, 不进行任何 claim

3. **TC-SR-003: autonomous 子开关关闭**
   - autonomous_enabled=false -> 不创建 relationship_proactive turn
   - 但 reminder 和 ritual 仍可触发

### 15.2 Claim 机制

4. **TC-SR-004: 正常 claim 一个 reminder task**
   - 到期 reminder, claim 成功 -> 创建 user_reminder_due turn
    
5. **TC-SR-005: 并发 claim 防止 double-claim**
   - 两个 heartbeat 同时 claim 同一 task -> 只有一个成功

6. **TC-SR-006: claim 返回空时行为**
   - 没有 pending + due task -> 正确跳过, 继续 cold-start 检查

7. **TC-SR-007: claim 后 task 状态为 'sending'**
   - claim 成功后 task.status 必须为 "sending"

### 15.3 优先级

8. **TC-SR-008: 提醒优先于仪式**
   - reminder + ritual 同时到期 -> 创建 reminder turn

9. **TC-SR-009: 仪式优先于 proactive cold-start**
   - ritual 到期 + cold-start eligible -> 创建 ritual turn

10. **TC-SR-010: 提醒优先于 proactive cold-start**
    - reminder 到期 + cold-start eligible -> 创建 reminder turn

### 15.4 频率控制

11. **TC-SR-011: trigger cap 达到时 blocked**
    - daily_trigger_count >= daily_trigger_cap -> gate 返回 allowed=false, reason="trigger_cap_reached"

12. **TC-SR-012: 跨日重置 trigger count**
    - 昨天 count=5, 今天第一次查询 -> count=0

13. **TC-SR-013: 提醒不受 trigger cap 限制**
    - cap reached 时 reminder 到期 -> 仍创建 turn

### 15.5 勿扰时间

14. **TC-SR-014: 勿扰时间 block proactive cold-start**
    - 在 quiet hours 内 -> gate 返回 allowed=false, reason="quiet_hours"

15. **TC-SR-015: 勿扰时间 block 仪式**
    - 在 quiet hours 内 -> ritual task 被 reschedule

16. **TC-SR-016: skip_quiet_hours=true 的提醒可绕过勿扰**
    - 在 quiet hours 内, task.skip_quiet_hours=true -> 创建 reminder turn

17. **TC-SR-017: 勿扰边界 (跨日 23:00-08:00)**
    - 07:59 -> in quiet hours
    - 08:00 -> NOT in quiet hours (end exclusive)
    - 23:00 -> in quiet hours (start inclusive)

### 15.6 Skip / Forced Speak

18. **TC-SR-018: 上次 spoke, 本次可 skip**
    - last_decision="spoke" -> is_wake_up=false -> skip_initiative 暴露

19. **TC-SR-019: 上次 skipped, 本次强制开口**
    - last_decision="skipped" -> is_wake_up=true -> skip_initiative 不暴露

20. **TC-SR-020: skip 后 consecutive_skip_count 正确**
    - 模型 skip -> count=1, 模型 spoke -> count=0

21. **TC-SR-021: 强制开口后 spoke 重置 skip count**
    - 上次 skip (count=1), 本次 spoke -> count=0, last_decision="spoke"

### 15.7 状态更新

22. **TC-SR-022: spoke 后 InitiativeState 正确更新**
    - last_spoke_at 更新, last_decision="spoke", consecutive_skip_count=0

23. **TC-SR-023: skipped 后 InitiativeState 正确更新**
    - last_skipped_at 更新, last_decision="skipped", consecutive_skip_count=1

24. **TC-SR-024: LLM failure 不 bump trigger count**
    - turn 因 LLM error 失败 -> daily_trigger_count 不递增

25. **TC-SR-025: daily_message_cap 正确计算**
    - 只统计 sent=1 的 proactive 消息

### 15.8 熔断

26. **TC-SR-026: lock busy 时 heartbeat 跳过**
    - lock 被持有 -> heartbeat 返回 turn_created=false, reason="scheduler_busy"

27. **TC-SR-027: bridge offline 时 blocked**
    - bridge_online_check() -> false -> gate 返回 blocked

28. **TC-SR-028: user opt-out 时 blocked**
    - proactive_paused_until > now -> gate 返回 blocked

29. **TC-SR-029: 不创建重复 pending wake-up**
    - 已有 pending proactive wake-up -> create_scheduled_wake_up 前先 cancel 旧任务

30. **TC-SR-030: 任务堆积防止 (cancel before create)**
    - cancel_pending_proactive_wake_ups() 在 create_scheduled_wake_up() 之前调用

### 15.9 完整流程集成

31. **TC-SR-031: proactive 全流程 (cold-start -> spoke -> schedule next)**
    - cold-start -> turn -> spoke -> next wake-up scheduled with is_wake_up=false

32. **TC-SR-032: proactive 全流程 (cold-start -> skip -> forced speak next)**
    - cold-start -> skip -> next wake-up scheduled with is_wake_up=true -> next heartbeat 强制开口

33. **TC-SR-033: reminder 全流程 (create -> due -> claim -> send -> mark delivered)**
    - reminder task 完整生命周期

34. **TC-SR-034: ritual 全流程 (due -> claim -> send -> reschedule daily)**
    - morning greeting 完整生命周期, recurring daily

35. **TC-SR-035: 在同一 heartbeat 内检查所有优先级**
    - 同时有 reminder + ritual + proactive -> 按优先级依次处理

***

## 16. 附录: 当前配置参数默认值

| 参数 | 默认值 | 来源 |
|---|---|---|
| enabled | false | config.features.proactive |
| autonomous_enabled | false | config.features.proactive |
| autonomous_intensity | "daily" | ProactiveSettings |
| quiet_hours_enabled | true | ProactiveSettings |
| quiet_hours_start | "00:00" | ProactiveSettings (实际应配置为 "23:00") |
| quiet_hours_end | "08:00" | ProactiveSettings |
| daily_active_message_cap | 50 | config.proactive.eligibility.daily_cap |
| heartbeat_interval_seconds | 30 | (hardcoded in main.py) |
| cold_start_seconds | 120 | ProactiveScheduler.COLD_START_SECONDS |
| scheduled_enabled | true | ProactiveSettings |
| scheduled_morning_time | "08:00" | ProactiveSettings |
| scheduled_night_time | "23:00" | ProactiveSettings |

***

> 本文档由需求文档、设计文档、模块契约、数据契约、状态机设计和当前源码交叉验证后编写。如需修改 SchedulerRuntime 的行为, 请先更新本规格文档, 再修改代码。
