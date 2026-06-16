# 主动消息（Proactive Messaging）方案（2026-06-16 修订 v4）

**日期**: 2026-06-16（初版），2026-06-16（修订 v2, v3, v4）
**状态**: ✅ 已实施（2026-06-16）。runtime_settings 覆盖关闭 proactive 的 bug 见 [20260616-proactive-message-disabled-by-runtime-settings.md](../../bug/20260616-proactive-message-disabled-by-runtime-settings.md)
**关联**: [[20260616-scheduled-reminders-design]], [[20260615-web-search-design]]

## 目标

让 bot 在用户沉默时主动开口。四档频率 + 模型自主调度节奏 + 静默时段保护。

## 决策记录

1. **v2 修订**：禁止伪造 user 消息 + POST /chat。设计独立后台触发路径。
2. **v3 修订**：主动后台 turn 的 system instruction 使用 `role=user` 的 `<system-reminder>...</system-reminder>` text block（不添加微信 channel 包装）。**不使用不可持久化的 `provider_system`**。在调用模型前原样写入 raw_events，随后依次持久化 assistant/tool events。上下文重建时必须按原内容、原顺序恢复完整 turn（user system-reminder + assistant 响应 + tool results），不能只恢复 assistant。
3. **v3 修订**：`schedule_next_proactive` 工具只在 `TurnScratchpad` 中记录候选间隔。**至少一条主动消息确认 delivered 后，后端才创建下一 scheduled task**。未调用工具时使用中点 fallback。发送失败不计配额且不创建下一任务。
4. **v3 修订**：明确不存在 transport exactly-once。发送前失败可按策略重试；发送已开始但结果未知时标记 `unknown_after_attempt`，不自动重发。定义启动时 `scheduled`、`firing`、`unknown_after_attempt` 的恢复规则。
5. **v3 修订**：后台发送必须从 `bot_contact_state` 取得 `user_id`、`context_token`、`bot_id`，调用真实 `wechat_sender(user_id, context_token, messages)`。缺少或不匹配时记录稳定错误。
6. **v3 修订**：失败且没有有效 assistant 响应的后台 turn **不进入 active context**（不写入 ConversationContext，不追加到历史消息数组）。压缩时 reminder + 对应响应作为完整 turn 一起替换。
7. **v3 修订**：APScheduler 使用 `MemoryJobStore`，job `id=task_id`，`replace_existing=True`，`coalesce=True`，明确 `misfire_grace_time`。`requirements.txt` 固定 `apscheduler>=3.10,<4`。
8. **v3 修订**：`turns` 表增量增加 `source`/`turn_type` 列（`ALTER TABLE ADD COLUMN`，不 `_reset`）。`raw_events` 保持 `role`/`content`/`call_id` 不变。
9. **v3 修订**：`/admin/dev/reset-context` 保留 `user_reminder`，取消 `proactive_speak`。
10. **v4 修订**：后台 turn 的 raw events 全部先以 `active=0` 暂存。只有协议完整且至少一条主动消息 confirmed delivered 时，才在单个事务中激活整轮事件、标记当前任务 fired、递增一次 turn 级配额并创建下一任务。失败、崩溃或协议不完整时不会重建出半截 turn。
11. **v4 修订**：调度状态拆分为 `firing`（尚未调用 sender）和 `sending`（已经持久化发送开始）。`firing` 可在重启后安全重试；`sending` 重启后转为 `unknown_after_attempt`。本地 delivery key 仅用于 CAS/日志关联，不提供微信传输幂等。
12. **v2 修订**：调度器使用 SQLite `scheduled_tasks` 表 + APScheduler（与提醒共用基础设施）。
13. **v2 修订**：主动消息产生的 assistant 消息进入对话历史，标记 `source=proactive_speak`。
14. **v2 修订**：`schedule_next_proactive` 独立工具，不与 `create_reminder` 共用。
15. **v2 修订**：静默时段 23:00-08:00 Asia/Shanghai。到期时顺延到 08:00。
16. **v2 修订**：配额按 Asia/Shanghai 自然日持久化（SQLite）。
17. **v2 修订**：模型未调用 schedule_next_proactive 时，后端兜底使用档位范围中点。
18. **始终披露**：proactive 工具始终在工具列表中。后台专属工具在非后台 turn 调用时返回稳定拒绝。

## 核心机制

### 完整流程

```
真实用户 turn 成功结束
  → 先用当前请求的 user_id/context_token/bot_id 更新 bot_contact_state
  → 检查 enabled、当日配额和 bot_contact_state
  → 条件满足才创建首次主动任务（trigger_at = now + 120s，generation=当前值）
  → APScheduler 调度（MemoryJobStore, id=task_id, replace_existing=True, coalesce=True）

120 秒后，无新用户消息
  → scheduler 触发 _fire_task(task_id, expected_generation)
  → CAS: UPDATE status='firing'
    WHERE task_id=? AND status='scheduled' AND generation=expected_generation
    → affected_rows=0 → 跳过（已被取消）
  → 获取 per-user lock（与 /chat 共用同一个锁）
  → 重新检查 enabled、当日配额和静默时段
    → 配额耗尽：取消当前任务并结束
    → 当前进入静默时段：任务恢复 scheduled 并顺延
  → 从 bot_contact_state 获取 context_token
  → 调用 Runtime.handle_background_turn(turn_type="proactive_speak")
    → 创建 running turn，写入 active=0 的 role=user system-reminder
    → ContextBuilder 构建 provider_messages（从 recent_history 转换）
    → 注入 system instruction 为 messages 末尾的 role=user block（非 provider_system）
    → ChatAgent LLM 循环（必须开口）
    → 首次调用 sender 前持久化 scheduled_task status=sending
    → 模型发消息（send_text_message）
    → TurnScratchpad 记录 schedule_next_proactive 候选间隔
    → assistant/tool events 继续以 active=0 暂存
    → 协议完整且至少一条 delivered 后，单个 SQLite 事务:
       激活整轮 raw_events + 当前任务 fired + 配额加 1 + 按需创建下一任务
    → 无 delivered / 协议不完整 → 不激活历史，不创建下一任务，并按发送阶段决定重试或 unknown
  → 释放 lock

用户发来新消息
  → /chat 入口 → scheduler.cancel_user_proactive_tasks(user_id)
  → CAS: UPDATE status='cancelled', generation=generation+1
  → 处理真实用户 turn
  → turn 成功结束 → 创建新的 120s 首次主动任务（generation=新值）
```

### 关键规则

1. **首次检查**：真实用户 turn 成功结束后，先持久化当前联系人，再检查 enabled、当日配额和有效联系人；条件满足才创建 120s 延迟的首次主动任务
2. **turn 失败不创建**：如果真实用户 turn 以非正常状态结束（END_FAILED / END_CANCELLED 等），不创建主动任务
3. **模型必须开口**：每次 proactive_speak turn 中模型必须发消息，不允许跳过
4. **模型自定节奏**：每次开口后通过 `schedule_next_proactive` 设定下次间隔（记录在 TurnScratchpad，不立即创建任务）
5. **至少一条 delivered 后才创建下一任务**：`schedule_next_proactive` 只记录候选间隔；后端在至少一条主动消息确认 delivered 后，使用该间隔（或 fallback 中点）创建下一 `scheduled_task`
6. **发送失败不计配额 + 不创建下一任务**：如果没有任何主动消息 delivered，配额不递增，也不创建下一 scheduled task。明确的发送前失败可由当前任务按策略重试；发送结果未知不自动重发
7. **范围校验**：后端校验模型选择的间隔在档位允许范围内
8. **兜底**：模型未调用 `schedule_next_proactive` 时，后端使用该档位范围的中点作为 fallback，记录 `fallback_reason`
9. **用户打断**：用户发新消息 → CAS cancel 所有 proactive_speak 任务 → 真实 turn 完成后重建
10. **配额耗尽**：当日主动消息次数达上限后，不再创建新的主动任务
11. **静默时段**：23:00-08:00 Asia/Shanghai。任务到期时在静默时段内顺延到当天/次日 08:00
12. **不自动暂停**：用户连续不回复不暂停，继续执行直到配额耗尽
13. **失败 turn 不进历史**：只有“协议事件完整且至少一条可见消息 confirmed delivered”的 proactive turn 才进入 active context。其余事件保留为 `active=0` 审计数据
14. **配额按 turn 计数**：一个 proactive turn 即使发送多个气泡，也只消耗一次配额

## 四档频率定义

| 档位 | 日上限（次） | 允许间隔范围（分钟） | 兜底间隔（中点，分钟） |
|------|-------------|---------------------|----------------------|
| 日常 (minimal) | 5 | [60, 180] | 120 |
| 平淡 (moderate) | 10 | [30, 120] | 75 |
| 热情 (enthusiastic) | 20 | [15, 60] | 37 |
| 粘人 (clingy) | 30 | [10, 30] | 20 |

- 默认档位：**平淡 (moderate)**
- 可通过 admin UI 或 runtime_settings 热切换

## 后台触发协议（关键架构设计）

### 问题

当前 `ChatAgent.chat()` 只接受真实用户消息：
- 硬编码 `if source != SOURCE_USER_MESSAGE: raise ValueError`
- 要求 `user_message` 非空
- `build_for_chat()` 将 user_message 包装为 `<channel source="wechat">`
- `/chat` HTTP endpoint 负责用户消息到达、中断检查、历史写入、联系人更新

### 方案：新增 `Runtime.handle_background_turn()`

```
Runtime.handle_background_turn(
    user_id,
    turn_type: str,  # "proactive_speak"
    system_reminder: str,  # 已包含且只包含一层 <system-reminder>...</system-reminder>
    context_token: str,  # 从 bot_contact_state 获取
) → TurnResult
```

内部调用链：

```
handle_background_turn()
  → 获取 per-user lock（与 /chat 共用同一个锁）
  → begin_staged_background_turn():
      - 创建 turns 行，status=running, source/turn_type=proactive_speak
      - 写入 active=0 的第一条 raw_event:
        role=user, content=[{"type": "text", "text": system_reminder}]
      - 不追加到全局 ConversationContext
  → 从 recent_history 构造 provider_messages
  → 在本轮局部 messages 末尾追加同一份 role=user system_reminder（非 provider_system）
  → ChatAgent LLM 循环（共享 LLM 循环、工具执行、output guard、发送回调）
  → LLM 响应写入 active=0 raw_events（role=assistant）
  → tool results 写入 active=0 raw_events（role=user, tool_result）
  → 发送回调使用真实 wechat_sender(user_id, context_token, messages)
  → 如果协议完整且至少一条 visible message confirmed delivered：
      - commit_proactive_success() 单事务将该 turn 所有 raw_events active=1
      - 标记当前 scheduled_task fired
      - turn 级配额递增一次
      - 未耗尽配额时插入下一 scheduled_task
      - COMMIT 后把同一完整 turn 追加到 ConversationContext
  → 否则：
      - raw_events 保持 active=0，turn 标记 failed/partial/unknown
      - 不写入 ConversationContext，不创建下一任务
      - sender 从未调用或明确失败：当前任务按 max_attempts 重试
      - sender 结果未知，或已有 delivered 但协议不完整：当前任务置 unknown_after_attempt
  → 释放 lock
  → 返回 TurnResult
```

### System Instruction 注入方式（v3 关键修正）

不使用 `provider_system`（不可持久化，上下文重建时丢失）。

在 provider `messages` 数组末尾追加一条 `role=user` 消息：

```python
# system_reminder 已经是最终原文，禁止再次包一层标签
turn.messages.append({
    "role": "user",
    "content": [
        {"type": "text", "text": system_reminder}
    ],
})
# 写入 active=0 raw_events（作为 turn 的第一条 event）
await self._write_event(turn, "message", {
    "message": copy.deepcopy(turn.messages[-1]),
    "message_kind": "system_reminder",
    "active": False,
})
```

上下文重建时，按 raw_events 的写入顺序恢复完整 turn：
```
role=user:   <system-reminder>This is a proactive check-in...</system-reminder>
role=assistant: [{"type": "text", "text": "Hey, 在干嘛呢"}, ...]
role=user:   [{"type": "tool_result", ...}, ...]  (如有 tool calls)
role=assistant: ...  (如有后续 LLM 响应)
```

### 压缩时的处理

压缩只读取 `active=1` 的完整 turn。reminder_due 或 proactive_speak 的 user system-reminder + assistant 响应 + tool results 作为一个整体替换为摘要，不拆散 turn，也不压缩 `active=0` 的审计事件。

### 与真实用户 turn 的区别

| 行为 | 真实用户 turn (/chat) | 后台 turn (handle_background_turn) |
|------|----------------------|-----------------------------------|
| user_message 包装 | `<channel source="wechat">` | 无 wechat channel 包装 |
| system instruction | 无（通过 provider_system） | `role=user` + `<system-reminder>` text block |
| 用户消息到达记录 | 是 | 否 |
| 输入序号更新 | 是 | 否 |
| 中断检查 | 基于 seq 比较 | 基于 generation CAS |
| 联系人状态更新 | 是 | 否 |
| raw_events | source 通过 turns 表关联 | source 通过 turns 表关联 |
| assistant 消息进历史 | 是 | 是（只有效 turn） |
| per-user lock | 是 | 是 |
| output guard | 是 | 是 |
| 发送回调 | `wechat_sender(messages)` | `wechat_sender(user_id, context_token, messages)` |
| LLM trace | 是 | 是 |
| turns 表 | source=user_message, turn_type=user_chat | source=proactive_speak, turn_type=proactive_speak |

### ContextBuilder 新增方法

```python
class ContextBuilder:
    def build_background_turn(
        self,
        *,
        turn_type: str,  # "proactive_speak"
        recent_history: list[dict[str, Any]],
        system_reminder: str,
        persona: str = "",
        tool_exposure: Any = None,
        user_id: str = "",
        turn_id: str = "",
        bot_id: str = "default",
    ) -> BuiltContextForTurn:
        """Build context for a background turn.

        Unlike build_for_chat(), this does NOT:
        - Wrap anything in wechat channel
        - Inject idle time reminders

        It DOES:
        - Convert recent_history to provider messages
        - Return system_reminder separately (caller appends as role=user
          message before LLM call, and writes it to raw_events)
        - Include tool schemas and contract block in provider_system
        """
```

### TurnType 扩充

```python
class TurnType(StrEnum):
    USER_CHAT = "user_chat"
    PROACTIVE_SPEAK = "proactive_speak"
    REMINDER_DUE = "reminder_due"
```

### TurnScratchpad 扩充

```python
# source 字段新增可选值
SOURCE_USER_MESSAGE = "user_message"
SOURCE_PROACTIVE_SPEAK = "proactive_speak"
SOURCE_REMINDER_DUE = "reminder_due"

# TurnScratchpad 新增字段
proactive_interval_candidate: int | None = None  # schedule_next_proactive 设置的候选间隔
scheduled_task_id: str = ""                      # 当前触发任务，用于事务提交
protocol_complete: bool = False                  # 工具调用/结果序列完整后置 true
```

`schedule_next_proactive` executor 通过统一 `ToolExecutionContext.turn` 修改当前 `TurnScratchpad`。不得用 module global、缺省 context variable 或仅凭 `source` 字符串定位当前 turn。

### turns 表增量迁移

```sql
-- 增量迁移（不升级 _SCHEMA_ID，不 _reset）
ALTER TABLE turns ADD COLUMN source TEXT NOT NULL DEFAULT 'user_message';
ALTER TABLE turns ADD COLUMN turn_type TEXT NOT NULL DEFAULT 'user_chat';
```

现有行自动获得默认值。`raw_events` 表结构不变。

### 数据库迁移原则

- 所有数据库变更是**增量迁移**（`ALTER TABLE ADD COLUMN`）
- **禁止**升级 `_SCHEMA_ID` 后调用 `_reset()` 删除现有对话历史
- 新列必须有 `DEFAULT` 值以兼容现有数据

## 工具定义

### schedule_next_proactive

```
name: schedule_next_proactive
description: >
  Suggest the delay until the next proactive check-in. ONLY available
  during a proactive_speak turn. The value is recorded but the actual
  next task is created only after at least one message is delivered.
  Choose a delay in seconds within the allowed range provided in the
  system instruction.
category: action_tool
side_effect: high
terminal: false
args:
  trigger_after_seconds (integer, required): Suggested seconds until
    next proactive check-in. Must be within the allowed range.
```

### 上下文校验

- `schedule_next_proactive` 只能在 `proactive_speak` turn 中调用
- 在其他 turn（user_chat、reminder_due）中调用 → 返回 `not_available: schedule_next_proactive is only available during proactive check-ins`
- `trigger_after_seconds` 必须在档位范围内 → 超范围返回 `out_of_range: allowed range is [min]-[max] seconds`

## Proactive Speak 的 System Instruction

每次 proactive_speak turn 追加到 provider messages 末尾的 `role=user` block：

```
<system-reminder>
This is a proactive check-in. The user has been inactive.
You MUST send a message now to reconnect — do not skip.

After sending, use schedule_next_proactive to suggest your next check-in.
Allowed interval: [{min_seconds}s, {max_seconds}s] ({min_minutes}-{max_minutes} minutes).
Proactive messages used today: {used}/{daily_limit}.
Quiet hours: 23:00-08:00 Asia/Shanghai.
</system-reminder>
```

## 发送凭证获取

```python
async def _get_sender_for_user(self, user_id: str, bot_id: str):
    """从 bot_contact_state 获取发送凭证，构造 wechat_sender"""
    contact = await self._store.get_bot_contact(user_id, bot_id)
    if not contact or not contact.get('context_token'):
        raise DeliveryError("no_contact",
            f"No contact state for user={user_id}, cannot send proactive message")
    return self._sender_factory(
        user_id=user_id,
        context_token=contact['context_token'],
        bot_id=bot_id,
    )
```

## 下一任务创建逻辑

```python
async def _maybe_create_next_proactive_task(self, turn: TurnScratchpad, user_id: str):
    """计算提交参数；真正写库由 commit_proactive_success 单事务完成。"""
    # 1. 检查协议完整性和 delivered 消息
    if not turn.protocol_complete:
        return None
    delivered = [m for m in turn.visible_messages
                 if m.delivery_status == DeliveryStatus.SENT_CONFIRMED]
    if not delivered:
        logger.warning("Proactive turn %s had no delivered messages, skipping next task", turn.turn_id)
        return None

    # 2. 无论模型是否给候选，先取得档位，避免未定义 tier
    tier = await self._get_frequency_tier(user_id)

    # 3. 确定间隔
    interval = turn.proactive_interval_candidate
    if interval is None:
        interval = self._fallback_interval(tier)
        logger.info("Proactive turn %s: model did not call schedule_next_proactive, "
                     "using fallback %ds", turn.turn_id, interval)

    # 4. 校验间隔在档位范围内
    min_s, max_s = self._get_interval_range(tier)
    if interval < min_s or interval > max_s:
        interval = (min_s + max_s) // 2  # 中点兜底
        logger.warning("Proactive turn %s: interval %ds out of range, using fallback %ds",
                       turn.turn_id, turn.proactive_interval_candidate, interval)

    # 5. 处理静默时段顺延
    trigger_at = self._resolve_trigger_time(interval)

    # 6. 单个 DB 事务完成：
    #    - WHERE 当前 task.status='sending'，防止重复提交
    #    - 激活 turn 全部 raw_events
    #    - 当前任务置 fired
    #    - quota used_count + 1（每 turn 一次）
    #    - 若加一后仍低于 daily_limit，则 INSERT 下一 scheduled_task
    await self._store.commit_proactive_success(
        current_task_id=turn.scheduled_task_id,
        turn_id=turn.turn_id,
        user_id=user_id,
        daily_limit=self._daily_limit(tier),
        next_trigger_at=trigger_at,
        frequency_tier=tier,
    )
```

## 发送失败处理与 exactly-once

不存在 transport exactly-once。共用调度器的 `firing`/`sending` 边界如下：

| 阶段 | 失败处理 | 配额 | 下一任务 |
|------|---------|------|---------|
| `firing`：尚未调用 sender（guard 拒绝、无 contact） | 重试（最多 max_attempts） | 不消耗 | 不创建 |
| `sending`：sender 明确返回失败 | 重试（最多 max_attempts） | 不消耗 | 不创建 |
| `sending`：结果未知或进程崩溃 | **不自动重发**，标记 `unknown_after_attempt` | 不消耗 | 不创建 |
| 发送成功（delivered） | — | 消耗 | 创建 |

`unknown_after_attempt` 的恢复规则（与 reminder 一致）：
- 启动时：记录 WARNING 日志，不自动重发
- admin UI：展示此类任务，提供人工操作入口

`wechat_sender`/bridge 必须返回与提醒方案相同的四态结果：`delivered`、`failed_before_send`、`failed_confirmed`、`unknown_after_attempt`。一旦传输调用开始，无法证明未送达的异常必须归入 `unknown_after_attempt`，不能折叠成普通 `failed` 后自动重试。

服务启动时若 `proactive_speak` 的 `trigger_at_utc` 已经过期，不补发原本应在停机期间发生的主动检查。将旧任务标记 `cancelled`（reason=`stale_after_restart`），重新读取当前档位、当日配额和静默时段：
- 配额已耗尽：不创建任务
- 当前在静默时段：创建 08:00 的下一任务
- 其他情况：以当前时间为基准使用该档位 fallback 中点创建一条新任务

## 静默时段处理

```python
def _resolve_trigger_time(desired_seconds_from_now: int) -> float:
    """计算实际触发时间，处理静默时段顺延"""
    target = now_utc + desired_seconds_from_now
    target_shanghai = target.astimezone(ZoneInfo("Asia/Shanghai"))

    if target_shanghai.hour >= 23 or target_shanghai.hour < 8:
        # 在静默时段内 → 顺延到当天 08:00
        next_8am = target_shanghai.replace(hour=8, minute=0, second=0, microsecond=0)
        if target_shanghai.hour >= 23:
            next_8am += timedelta(days=1)  # 23点后顺延到次日
        return next_8am.astimezone(ZoneInfo("UTC")).timestamp()

    return target.timestamp()
```

## 配额管理

### SQLite 持久化

```sql
CREATE TABLE IF NOT EXISTS proactive_quota (
    user_id TEXT NOT NULL,
    date TEXT NOT NULL,  -- YYYY-MM-DD in Asia/Shanghai
    used_count INTEGER NOT NULL DEFAULT 0,
    frequency_tier TEXT NOT NULL DEFAULT 'moderate',
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, date)
);
```

### 配额操作

```python
async def get_today_used(user_id: str) -> int:
    today = datetime.now(SHANGHAI).strftime("%Y-%m-%d")
    row = await db.execute("SELECT used_count FROM proactive_quota WHERE user_id=? AND date=?", ...)
    return row["used_count"] if row else 0

async def increment_quota(user_id: str):
    today = datetime.now(SHANGHAI).strftime("%Y-%m-%d")
    await db.execute(
        "INSERT INTO proactive_quota (user_id, date, used_count) VALUES (?, ?, 1) "
        "ON CONFLICT(user_id, date) DO UPDATE SET used_count=used_count+1, updated_at=datetime('now')",
        ...
    )

async def is_quota_exhausted(user_id: str, daily_limit: int) -> bool:
    return await get_today_used(user_id) >= daily_limit
```

- 配额按 Asia/Shanghai 自然日计算
- 仅在完整 turn 至少一条 delivered 后递增；一个 turn 多个气泡只递增一次
- 发送失败不消耗配额
- 当前任务 fired、事件激活、配额递增和下一任务插入在一个事务中，使用当前 task status 防止重复提交

## 配置热更新行为

| 变更 | 行为 |
|------|------|
| `enabled=false` | 立即取消所有待执行 proactive_speak 任务（CAS generation+1） |
| `enabled=true` | 下次真实用户 turn 完成后恢复 120s 首次任务 |
| 档位切换 | 不影响当日已用次数；新调度使用新范围；已排程的旧任务被 cancel 重建 |
| 静默时段变更 | 不影响已排程任务；新任务使用新静默时段 |
| 日上限变更 | 不影响当日已用次数；新上限立即生效 |

## /admin/dev/reset-context 行为

- 取消所有 `proactive_speak` 任务（`status='cancelled'`，`generation+1`）
- **保留** `user_reminder` 任务
- 重置当日主动配额为 0
- 只有完整数据删除才删除所有 `scheduled_tasks`

## 配置

### config.yaml

```yaml
proactive:
  enabled: true
  default_frequency: "moderate"
  quiet_hours_start: 23
  quiet_hours_end: 8
  quiet_hours_timezone: "Asia/Shanghai"
  first_check_delay_seconds: 120
  frequencies:
    minimal:
      daily_limit: 5
      interval_range_seconds: [3600, 10800]  # [60min, 180min]
    moderate:
      daily_limit: 10
      interval_range_seconds: [1800, 7200]   # [30min, 120min]
    enthusiastic:
      daily_limit: 20
      interval_range_seconds: [900, 3600]    # [15min, 60min]
    clingy:
      daily_limit: 30
      interval_range_seconds: [600, 1800]    # [10min, 30min]
```

### requirements.txt

```
apscheduler>=3.10,<4
```

## 工具选择规则（追加到稳定提示词块）

```
schedule_next_proactive: ONLY available during proactive check-in turns.
  Do NOT call this during normal user chat. The system will tell you
  the allowed interval range. Choose a delay within that range.
```

## 涉及改动清单

| 文件 | 改动 |
|------|------|
| `agent/scheduler.py` | **新建** — TaskScheduler（SQLite + APScheduler MemoryJobStore + proactive 专项方法 + bot_contact_state 凭证获取） |
| `memory/store.py` | 新增 `scheduled_tasks` 表 + `proactive_quota` 表 + CRUD；staged background turn 与 `commit_proactive_success()` 原子事务；`turns` 表增量增加 `source`/`turn_type` 列（不 `_reset`）；`get_bot_contact()` 方法 |
| `runtime_contracts/types.py` | `TurnType` 新增 `PROACTIVE_SPEAK`, `REMINDER_DUE` |
| `agent/state_machine.py` | 新增 `SOURCE_PROACTIVE_SPEAK`, `SOURCE_REMINDER_DUE`；`TurnScratchpad` 新增候选间隔、scheduled_task_id、protocol_complete |
| `agent/tool_registry.py` | 注册 `schedule_next_proactive` ToolDef |
| `agent/tool_exposure_policy.py` | 从 TOOL_REGISTRY.iter_defs() 派生 |
| `agent/tools.py` | 添加统一 `ToolExecutionContext`；`schedule_next_proactive` executor 通过 context.turn 记录候选间隔，不创建任务 |
| `agent/runtime_context_builder.py` | 新增 `build_background_turn()` 方法；上下文重建支持完整 turn 恢复 |
| `agent/chat_agent.py` | 新增后台 turn 路径；system reminder 注入为 role=user message（非 provider_system）；移除硬编码 source 检查；raw_events 以 active=0 暂存；首次发送前标记任务 sending |
| `agent/context_builder.py` | 稳定工具提示词块追加 proactive 规则 |
| `runtime/chat_runtime.py` | 新增 `handle_background_turn()` 方法 |
| `config.yaml` | 新增 `proactive` 配置段 |
| `main.py` | 初始化 scheduler + wechat_sender_factory；startup 恢复任务；/chat 成功后先更新 bot_contact_state 再创建首次 proactive 任务；shutdown 停止；热加载处理；/admin/dev/reset-context 保留 user_reminder |
| `bridge/wechat_poller.py` / `wechat_sender` | 用户消息一到达即取消 proactive 任务；发送结果区分 failed_before_send、failed_confirmed、unknown_after_attempt |
| `requirements.txt` | 固定 `apscheduler>=3.10,<4` |
| `tests/` | 新增 `test_proactive.py` |

## 测试矩阵

| # | 测试场景 | 预期 |
|---|---------|------|
| 1 | 真实用户 turn 成功结束 | 创建 120s 首次 proactive 任务 |
| 2 | 真实用户 turn 失败 | 不创建 proactive 任务 |
| 3 | 120s 到期 → proactive_speak turn | LLM 被唤醒，必须发消息 |
| 4 | system instruction 注入为 role=user message | provider messages 末尾有 `<system-reminder>` block，非 provider_system |
| 5 | raw_events 写入 user anchor | 第一条 event 为 role=user 的 system-reminder，初始 active=0 |
| 6 | 上下文重建恢复完整 turn | 仅成功提交的 active=1 user anchor + assistant + tool results 原顺序恢复 |
| 7 | 模型调用 schedule_next_proactive(900) | TurnScratchpad.proactive_interval_candidate=900，不立即创建任务 |
| 8 | 至少一条 delivered → 创建下一任务 | 配额 +1，scheduled_task 创建 |
| 9 | 无 delivered → 不创建下一任务 | 配额不变，无新 scheduled_task |
| 10 | 模型选择间隔超出范围 | 返回 `out_of_range` |
| 11 | 模型未调用 schedule_next_proactive | 兜底使用范围中点，记录 fallback_reason |
| 12 | 在 user_chat turn 中调用 schedule_next_proactive | 返回 `not_available` |
| 13 | 发送成功 → 配额 +1 | used_count 递增 |
| 14 | 发送失败 → 配额不变 + 不创建下一任务 | used_count 不变，无新任务 |
| 15 | 发送结果未知 | status=unknown_after_attempt，不自动重发 |
| 16 | 配额耗尽 | 不再创建新 proactive 任务 |
| 17 | 配额跨日 | 次日 00:00 后配额重置 |
| 18 | 用户发消息 → cancel proactive 任务 | CAS generation+1，旧任务不触发 |
| 19 | 旧 generation 任务到期 | CAS 检查失败，跳过执行 |
| 20 | 静默时段内到期（如 23:30） | 顺延到次日 08:00 |
| 21 | 静默时段内到期（如 03:00） | 顺延到当天 08:00 |
| 22 | 静默时段边界（22:59 vs 23:00） | 22:59 正常触发，23:00 顺延 |
| 23 | enabled=false 热切换 | 取消所有待执行 proactive 任务 |
| 24 | 档位热切换 moderate→clingy | 已用次数不变，新任务用新范围 |
| 25 | 服务重启 → 恢复 scheduled 任务 | startup 扫描并重新调度 APScheduler job |
| 26 | 重启恢复 firing 任务 | sender 尚未调用，恢复 scheduled 后安全重试 |
| 27 | 重启恢复 sending 任务 | 转为 unknown_after_attempt，不自动重发 |
| 28 | 重启恢复 unknown_after_attempt 任务 | 记录 WARNING，不自动重发 |
| 29 | 重启后 provider message 序列完全一致 | 上下文重建 = 原始 active turn 内容 |
| 30 | 调度回调重复执行 | task status + generation CAS 只允许一个回调领取；不声称传输幂等 |
| 31 | 无孤立 assistant（reminder_due） | user anchor + assistant 在同一事务成对提交 |
| 32 | 失败 turn 不进 active context | active=0 事件不进入 ConversationContext 或 rebuild |
| 33 | 协议不完整但已有 delivered | 不激活可能破坏 provider 协议的 turn，记录高优先级审计 |
| 34 | 压缩时完整 turn 替换 | reminder + 响应一起替换为摘要 |
| 35 | 一个 turn 发送多个气泡 | 配额只递增一次 |
| 36 | 用户连续不回复 ×10 | 继续执行直到配额耗尽 |
| 37 | 并发：用户消息 vs proactive 到期 | lock 保证串行，generation CAS 防止旧任务误发 |
| 38 | bot_contact_state 缺失 | 返回 no_contact 错误，不尝试发送 |
| 39 | APScheduler MemoryJobStore coalesce | 多次 misfire 合并为一次 |
| 40 | APScheduler replace_existing | 同 task_id 重新调度替换旧 job |
| 41 | /admin/dev/reset-context | proactive_speak 取消，user_reminder 保留，配额重置 |
| 42 | turns 表增量迁移 | ALTER TABLE ADD COLUMN，现有数据不丢失 |
| 43 | system_reminder 原文注入 | 只包含一层标签，DB 与 provider messages 字节内容一致 |
| 44 | commit_proactive_success 原子性 | 事件激活、当前任务 fired、quota +1、下一任务插入全成或全不成 |
| 45 | 服务离线期间 proactive 任务过期 | 不补发陈旧检查，按当前时间和静默时段重新安排 |
| 46 | 到期执行前配额已耗尽 | 不调用 LLM，当前任务取消 |
| 47 | 排程后配置进入静默时段 | 到期时二次 guard，顺延而不是发送 |
| 48 | 旧 callback 携带旧 generation | CAS 失败，不触发后台 turn |
| 49 | bridge 异常无法确认是否送达 | 当前任务 unknown_after_attempt，不自动重发 |

## 依赖顺序

本方案是 **依赖序列的第 6 步**（最后一步，依赖所有前置基础设施）。

前置依赖：
1. 统一静态工具注册和暴露
2. 持久化 scheduler 基础设施（SQLite scheduled_tasks + APScheduler MemoryJobStore）
3. 后台触发协议（Runtime.handle_background_turn + role=user system-reminder 注入）
4. TurnType / source 扩充
5. turns 表增量迁移

## 已否决方案

1. **固定间隔定时器** — 缺乏随机感
2. **模型可选跳过** — 用户要求简化，每次必须开口
3. **统一 2 分钟检查** — 低频率档位浪费 LLM 调用
4. **伪造 user 消息 + POST /chat** — v2 改为独立后台触发路径
5. **纯内存 asyncio.sleep** — v2 改为 SQLite + APScheduler
6. **schedule_task 共用工具** — v2 拆分为 `create_reminder` 和 `schedule_next_proactive`
7. **配额放 ChatRuntime 内存** — v2 改为 SQLite 持久化
8. **连续忽略自动暂停** — 用户要求不暂停
9. **工具按 turn 类型动态增删** — v2 始终披露，非适用上下文返回 `not_available`
10. **system instruction 放 provider_system** — v3 改为 `role=user` + `<system-reminder>` text block（可持久化，可上下文重建）
11. **schedule_next_proactive 立即创建任务** — v3 改为先记录候选，delivered 确认后再创建
12. **发送失败仍计配额** — v3 改为不计配额、不创建下一任务
13. **_SCHEMA_ID 升级 + _reset 迁移** — v3 改为增量 ALTER TABLE ADD COLUMN
14. **依靠 idempotency_key 判断微信是否已发送** — v4 明确不可行，改用 firing/sending 边界和 unknown_after_attempt
15. **边生成边写 active history** — v4 改为 active=0 暂存，成功后整轮原子激活

## 参考

- astrbot_plugin_proactive_chat: 参考"冷场检测 + 上下文感知"理念
- APScheduler 3.x: https://apscheduler.readthedocs.io/en/3.x/
- 调度基础设施与提醒共用，见 [[20260616-scheduled-reminders-design]]
