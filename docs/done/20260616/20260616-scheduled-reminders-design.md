# 定时任务提醒方案（2026-06-16 修订 v4）

**日期**: 2026-06-16（初版），2026-06-16（修订 v2, v3, v4）
**状态**: ✅ 已实施（2026-06-16）。运行链路修复见 [20260616-tools-scheduler-runtime-fixes.md](20260616-tools-scheduler-runtime-fixes.md)
**关联**: [[20260616-proactive-messaging-design]], [[20260615-web-search-design]]

## 目标

支持用户用自然语言设置提醒，如"10 分钟后叫我开会"、"明天下午 3 点提醒我买菜"。模型确认 + 生成最终文案 → 到时间直接发送，不再次调用 LLM。

## 决策记录

1. **v2 修订**：到期时不调用 LLM。模型在创建提醒的原始 turn 中生成 `delivery_text` 最终文案，后端持久化。到期后经过 output guard 直接发送。
2. **v2 修订**：删除 `timefhuman` 依赖。模型根据当前时间把用户表达规范化为 ISO 8601 时间，不再由后端解析自然语言。
3. **v3 修订**：reminder_due 到期发送时写入完整的 turn：一个 `role=user` 的 `<system-reminder>` 锚点 + 实际送达的 `role=assistant` 消息。**不允许只留下孤立 assistant**。来源记录在关联的 `turns.source/turn_type`，不写入 raw event。
4. **v3 修订**：`turns` 表增量增加 `source`/`turn_type` 列用于审计。`raw_events` 保持 `role`/`content`/`call_id` 不变。数据库变更是**增量迁移**，禁止升级 `_SCHEMA_ID` 后调用 `_reset()` 删除现有对话历史。
5. **v3 修订**：后台发送必须从 `bot_contact_state` 取得 `user_id`、`context_token`、`bot_id`，调用真实 `wechat_sender(user_id, context_token, messages)`。缺少或不匹配时记录稳定错误，不尝试发送。
6. **v3 修订**：明确不存在 transport exactly-once。发送前失败可按策略重试；发送已经开始但结果未知时标记 `unknown_after_attempt`，不自动重发，保留人工排查入口。定义启动时 `scheduled`、`firing`、`unknown_after_attempt` 的恢复规则。
7. **v3 修订**：APScheduler 使用 `MemoryJobStore`，job `id=task_id`，`replace_existing=True`，`coalesce=True`，明确 `misfire_grace_time`。
8. **v3 修订**：`create_reminder` 时如果当前 turn 没有权威当前时间，提示模型先调用 `get_time` 再计算 ISO 8601。
9. **v3 修订**：`/admin/dev/reset-context` 保留 `user_reminder` 类型的 `scheduled_tasks`，取消 `proactive_speak`。只有完整数据删除操作才删除所有 `scheduled_tasks`。
10. **v3 修订**：`scheduled_tasks.status` 增加 `unknown_after_attempt` 状态值。
11. **v4 修订**：增加 `sending` 状态，明确 `firing` 表示尚未调用传输层、`sending` 表示已经持久化发送开始标记。启动恢复时 `firing` 可安全重试，`sending` 一律转为 `unknown_after_attempt`，不再声称可用本地 idempotency key 查询微信是否已送达。
12. **v4 修订**：提醒送达历史使用单个 SQLite 事务一次性创建 synthetic turn、写入两条 active raw events 并把任务标记为 fired。raw event 不携带 `source`；来源只记录在 `turns.source/turn_type`。
13. **v4 修订**：后台已过期任务采用分类恢复：用户提醒立即补发并记录 lateness；过期主动消息不补发旧内容，由主动消息方案重新计算下一次触发。
14. **v2 修订**：提供 `create_reminder`、`list_reminders`、`cancel_reminder` 三个工具。
15. **始终披露**：reminder 工具始终在工具列表中。未配置时不动态删除。
16. **统一工具注册**：以 `agent/tool_registry.py` 为单一权威来源；执行路由通过按工具名注册的 dispatcher。

## 完整流程

```
用户: "10分钟后提醒我开会"

→ LLM 检查当前 turn 是否有权威当前时间（系统已提供则跳过）
→ 如无 → 先调用 get_time 获取权威时间
→ LLM 根据当前时间计算 target_time
→ 调用 create_reminder(
    trigger_at="2026-06-16T14:40:00+08:00",
    reminder_note="开会",
    delivery_text="⏰ 到时间了，该开会了哦"
  )
→ 后端校验 trigger_at 在未来、不超最大提前量
→ delivery_text 经 output guard 检查
→ 写入 SQLite scheduled_tasks 表（status=scheduled）
→ 返回确认信息给 LLM
→ LLM 回复用户: "好，10分钟后叫你开会 ⏰"

（600秒后，trigger_at 到达）
→ APScheduler 触发 → 从 DB 读取任务，CAS 更新 status=scheduled→firing
→ 从 bot_contact_state 获取 user_id、context_token、bot_id
→ 取 delivery_text → output guard
→ 持久化 status=firing→sending、delivery_started_at=now
→ wechat_sender(user_id, context_token, [msg])
→ confirmed delivered 后使用一个 SQLite 事务:
    1. 创建 turns 行（turn_type=reminder_due, source=reminder_due）
    2. 写入 role=user 的系统生成历史锚点（不包含用户可控 reminder_note）
    3. 写入 role=assistant 的实际 guarded delivery_text
    4. 更新 scheduled_tasks: status=fired, fired_at=now
```

## 调度基础设施

### SQLite scheduled_tasks 表（v3 更新）

```sql
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    task_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    bot_id TEXT NOT NULL DEFAULT 'default',
    task_type TEXT NOT NULL CHECK(task_type IN (
        'user_reminder', 'proactive_speak'
    )),
    trigger_at_utc TEXT NOT NULL,  -- ISO 8601 UTC
    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'scheduled'
        CHECK(status IN (
            'scheduled','firing','sending','fired','cancelled','failed',
            'unknown_after_attempt'
        )),
    generation INTEGER NOT NULL DEFAULT 1,
    delivery_key TEXT NOT NULL UNIQUE,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    last_error TEXT,
    delivery_started_at TEXT,
    last_attempt_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    fired_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_status_trigger
    ON scheduled_tasks(status, trigger_at_utc);
CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_user
    ON scheduled_tasks(user_id, status);
```

### 字段说明

| 字段 | 说明 |
|------|------|
| `task_id` | UUID，创建时生成 |
| `task_type` | `user_reminder` 或 `proactive_speak` |
| `trigger_at_utc` | 规范化 UTC 时间 `YYYY-MM-DDTHH:MM:SS.ffffffZ`，调度器以此为准 |
| `timezone` | 用户时区，用于展示和日志 |
| `payload_json` | 任务负载（reminder: delivery_text + reminder_note；proactive: frequency_tier） |
| `status` | scheduled → firing → sending → fired/failed/unknown_after_attempt；也可从 scheduled → cancelled |
| `firing` | 已由 CAS 领取任务，但尚未调用 sender；进程恢复后可以安全重试 |
| `sending` | 已持久化“即将调用 sender”；进程崩溃后无法判断是否送达，恢复为 unknown_after_attempt |
| `unknown_after_attempt` | 发送已开始但结果未知（网络断开等），不自动重发，人工排查 |
| `generation` | 乐观锁版本号，cancel 时递增使旧任务失效 |
| `delivery_key` | 本地去重和日志关联键，基于 task_id + generation；不代表微信传输幂等 |
| `attempt_count` / `max_attempts` | 重试控制 |

### 数据库迁移（增量，不 _reset）

`turns` 表增量增加列（`ALTER TABLE ... ADD COLUMN`），**不升级 `_SCHEMA_ID` 导致 `_reset()` 删除现有对话历史**：

```sql
-- 增量迁移：仅在新列不存在时添加
ALTER TABLE turns ADD COLUMN source TEXT NOT NULL DEFAULT 'user_message';
ALTER TABLE turns ADD COLUMN turn_type TEXT NOT NULL DEFAULT 'user_chat';
```

现有行自动获得默认值 `user_message`/`user_chat`。

`raw_events` 表**不做结构变更**，保持 `role`/`content`/`call_id`。`source` 和 `turn_type` 语义通过 `turns` 表关联查询。

### APScheduler 配置

```python
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.memory import MemoryJobStore

scheduler = AsyncIOScheduler(
    jobstores={"default": MemoryJobStore()},
    job_defaults={
        "coalesce": True,         # 错过多次只执行一次
        "replace_existing": True,  # 同 id 自动替换
        "misfire_grace_time": 60,  # 错过 60 秒内仍可触发
    },
)
```

- `MemoryJobStore`：进程内调度，不持久化 job 状态（持久化由 SQLite `scheduled_tasks` 表负责）
- Job `id=task_id`：与 DB 记录一一对应
- `replace_existing=True`：同 task_id 重新调度时自动替换旧 job
- `coalesce=True`：进程暂停恢复后，同一个 job 的多次 misfire 合并为一次执行

### 调度器设计（agent/scheduler.py）

```python
class TaskScheduler:
    """管理 scheduled_tasks 的调度和执行"""

    def __init__(self, memory_store: MemoryStore, wechat_sender_factory,
                 output_guard, apscheduler: AsyncIOScheduler):
        self._store = memory_store
        self._sender_factory = wechat_sender_factory  # 工厂：根据 user_id 获取 sender
        self._guard = output_guard
        self._scheduler = apscheduler

    async def startup(self):
        """启动时扫描 scheduled/firing/sending/unknown_after_attempt 任务并恢复"""
        # 1. scheduled 且未来到期 → 重新调度 APScheduler job
        # 2. scheduled 且 user_reminder 已过期 → run_date=now，补发一次并记录 lateness
        # 3. scheduled 且 proactive_speak 已过期 → 不回放旧检查，交给主动消息策略重建
        # 4. firing → sender 尚未被调用，恢复为 scheduled 后安全重试
        # 5. sending → 无法确认是否送达，转为 unknown_after_attempt
        # 6. unknown_after_attempt → 不自动重发，记录 WARNING 日志，
        #    保留人工排查入口（admin UI 可查看）
        tasks = await self._store.get_scheduled_tasks(
            statuses=['scheduled', 'firing', 'sending', 'unknown_after_attempt']
        )
        for task in tasks:
            if task['status'] == 'scheduled':
                await self._recover_scheduled(task)
            elif task['status'] == 'firing':
                await self._return_to_scheduled_and_retry(task)
            elif task['status'] == 'sending':
                await self._mark_unknown_after_attempt(task, "process_restarted_while_sending")
            elif task['status'] == 'unknown_after_attempt':
                logger.warning("Task %s in unknown_after_attempt, manual review needed", task['task_id'])

    async def shutdown(self):
        """停止 scheduler，不删除持久化任务"""
        self._scheduler.shutdown(wait=False)

    async def schedule_reminder(self, user_id, trigger_at_utc,
                                 delivery_text, reminder_note) -> str:
        """创建提醒任务 → 写入 DB → 调度 APScheduler job"""
        task_id = f"rem_{uuid.uuid4().hex[:12]}"
        delivery_key = f"{task_id}:v1"
        # INSERT INTO scheduled_tasks ...
        # 调度: scheduler.add_job(_fire_task, 'date', run_date=trigger_at_utc,
        #                         id=task_id, args=[task_id, generation])
        return task_id

    async def cancel_reminder(self, user_id, task_id) -> bool:
        """取消提醒 → CAS 更新 DB → 移除 APScheduler job"""
        # UPDATE scheduled_tasks SET status='cancelled', generation=generation+1
        # WHERE task_id=? AND user_id=? AND status='scheduled' AND generation=?
        # 移除 job: scheduler.remove_job(task_id)

    async def _fire_task(self, task_id: str, expected_generation: int):
        """任务到期回调"""
        # 1. CAS: UPDATE status='firing'
        #    WHERE task_id=? AND status='scheduled' AND generation=expected_generation
        #    generation 只在取消/替换时递增，领取任务时不递增。
        #    → affected_rows=0 则跳过（已被取消或正在处理）
        # 2. 根据 task_type 分发
        #    - user_reminder: _deliver_reminder(task)
        #    - proactive_speak: _trigger_proactive_speak(task)
        # 3. 调用 sender 前先持久化 status='sending', delivery_started_at=now
        # 4. 发送成功 → 原子写入完整 synthetic turn 并标记 fired
        # 5. 明确的发送前/发送失败 → attempt_count+1, 重试或标记 failed
        # 6. 发送结果未知 → UPDATE status='unknown_after_attempt', last_error=...

    async def _deliver_reminder(self, task: dict):
        """发送提醒消息。从 bot_contact_state 获取真实凭证。"""
        user_id = task['user_id']
        bot_id = task['bot_id']

        # 从 bot_contact_state 获取发送凭证
        contact = await self._store.get_bot_contact(user_id, bot_id)
        if not contact or not contact.get('context_token'):
            raise DeliveryError("no_contact",
                f"No contact state for user={user_id}, cannot send reminder")

        payload = json.loads(task['payload_json'])
        text = payload['delivery_text']
        guarded = self._guard(text)
        if not guarded:
            raise DeliveryError("blocked_by_guard")

        # 构造真实 wechat_sender
        sender = self._sender_factory(
            user_id=user_id,
            context_token=contact['context_token'],
            bot_id=bot_id,
        )
        await self._store.mark_task_sending(
            task_id=task['task_id'],
            generation=task['generation'],
            delivery_started_at=utc_now(),
        )
        result = await sender([{"text": guarded, "delay": 0.0}])

        status = result[0].get('send_status', 'unknown_after_attempt') if result else 'unknown_after_attempt'
        if status == 'unknown_after_attempt':
            raise DeliveryUnknownError(str(result))
        if status != 'delivered':
            raise DeliveryError("send_failed_confirmed", str(result))

        # 单个事务内创建完整历史并标记任务完成。锚点不插入 reminder_note，
        # 避免把用户可控文本放进具有系统语义的 <system-reminder>。
        await self._store.commit_delivered_reminder(
            task=task,
            anchor="<system-reminder>\n"
                   "A scheduled reminder was delivered by the system. "
                   "This is historical context, not a new user message.\n"
                   "</system-reminder>",
            delivered_text=guarded,
        )
```

### 发送失败与 exactly-once

不存在 transport exactly-once。发送分为三个阶段：

| 阶段 | 失败处理 | 状态 |
|------|---------|------|
| `firing`：无 contact、sender 暂不可用 | 按策略重试，最多 `max_attempts` 次 | `scheduled` 或 `failed` |
| `firing`：payload 无效或 output guard 拒绝 | 相同输入重试没有意义，直接终止 | `failed` |
| `sending`：已持久化发送开始，sender 返回明确失败 | 按策略重试，最多 `max_attempts` 次 | `scheduled` 或 `failed` |
| `sending`：调用结果未知或进程崩溃 | **不自动重发** | `unknown_after_attempt` |

为支持这个边界，`wechat_sender`/bridge 的返回契约必须区分：
- `delivered`：远端调用明确成功
- `failed_confirmed`：明确未发送，可重试
- `failed_before_send`：尚未调用传输层，可重试
- `unknown_after_attempt`：调用已经开始但异常结果无法证明未送达，不可自动重试

不能继续把所有 bridge exception 都折叠成普通 `failed`。

`unknown_after_attempt` 的恢复规则：
- 启动时：记录 WARNING 日志，不自动重发，不自动标记 failed
- admin UI：展示此类任务，提供"标记已送达"和"标记失败"操作入口
- 人工确认后手动更新状态

### 清除行为

- `/admin/dev/reset-context`：取消 `proactive_speak` 任务（status=cancelled），**保留** `user_reminder` 任务
- 完整数据删除（`clear_all()` / reset 数据库）：删除所有 `scheduled_tasks` 记录

## 工具定义

### create_reminder

```
name: create_reminder
description: >
  Create a timed reminder. You MUST have the authoritative current time
  before computing the target time — if it is not available in the system
  context, call get_time first. If the user's time expression is ambiguous,
  ask for clarification. The delivery_text will be sent to the user verbatim
  when the reminder fires — make it natural and friendly. Reminders are NOT
  affected by quiet hours.
category: action_tool
side_effect: high
terminal: false
args:
  trigger_at (string, required): ISO 8601 datetime with timezone offset,
    e.g. "2026-06-16T14:40:00+08:00". Must be in the future.
  reminder_note (string, required): Original reminder topic for audit,
    e.g. "开会"
  delivery_text (string, required): The exact message to send when the
    reminder fires. Must be natural and friendly, suitable for a chat
    message. Max 500 characters.
```

### list_reminders

```
name: list_reminders
description: >
  List pending reminders for the current user. These are local reminders
  created via create_reminder — NOT Microsoft To Do tasks.
category: fact_tool
side_effect: none
terminal: false
args: none
```

### cancel_reminder

```
name: cancel_reminder
description: >
  Cancel a pending reminder by its task_id. Use list_reminders first
  to find the task_id. Only cancels local reminders, not Microsoft To Do.
category: action_tool
side_effect: high
terminal: false
args:
  task_id (string, required): The reminder task_id from list_reminders
```

## 后端校验

| 校验 | 规则 | 失败返回 |
|------|------|---------|
| trigger_at 格式 | 必须为有效 ISO 8601 + 含时区 | `invalid_format: trigger_at must be ISO 8601 with timezone offset` |
| 未来时间 | trigger_at > now + min_advance | `too_soon: reminder must be at least 10 seconds in the future` |
| 最大提前量 | trigger_at < now + 90 days | `too_far: reminders can be at most 90 days in advance` |
| delivery_text 长度 | ≤ 500 字符 | `too_long: delivery_text must be ≤ 500 characters` |
| delivery_text guard | 通过 output guard | `blocked_by_guard: delivery_text rejected` |

## 到期发送（不调用 LLM）

```
scheduler._fire_task(task_id, expected_generation) →
  1. CAS: UPDATE scheduled_tasks SET status='firing'
     WHERE task_id=? AND status='scheduled' AND generation=expected_generation
     → affected_rows=0 → 跳过（已取消或正在处理）
  2. 从 bot_contact_state 获取 user_id、context_token、bot_id
     → 缺失 → DeliveryError("no_contact")
  3. 解析 payload_json → delivery_text
  4. output_guard(delivery_text)
     → 拒绝 → terminal DeliveryError("blocked_by_guard")，不重试
  5. UPDATE status='sending', delivery_started_at=now
     WHERE task_id=? AND status='firing' AND generation=?
  6. wechat_sender(user_id, context_token, [{"text": guarded, "delay": 0.0}])
     → 发送前失败 → attempt_count+1 → 重试或标记 failed
     → 发送结果未知 → UPDATE status='unknown_after_attempt'
  7. confirmed delivered 后启动 SQLite 事务，写入完整 turn:
     a. role=user, content=[
          {"type": "text", "text": "<system-reminder>\nA scheduled reminder was delivered by the system. This is historical context, not a new user message.\n</system-reminder>"}
        ]
     b. role=assistant, content=[{"type": "text", "text": "{guarded_text}"}]
     c. turns 行记录 turn_type=reminder_due, source=reminder_due
     d. UPDATE scheduled_tasks SET status='fired', fired_at=now
     e. COMMIT；任一步失败则 ROLLBACK，不产生半个 active turn
```

- 不调用 LLM
- 不经过 ChatAgent 对话循环
- 仍经 output guard、发送回调、事件记录
- raw_events 中两条记录保证上下文重建时 turn 完整

## delivery_text 双重 guard

1. **创建时**：`create_reminder` 工具执行时对 `delivery_text` 做 output guard，guard 失败则拒绝创建
2. **发送时**：到期发送前再次 guard，防止 guard 规则更新后旧文案不合规

## 静默时段

- 用户明确设置的提醒**不受静默时段限制**
- 静默时段仅限制主动消息（见 proactive messaging 方案）

## 工具选择规则（追加到稳定提示词块）

```
create_reminder: when user asks for a future reminder. If you don't have
  the authoritative current time in system context, call get_time first.
  Compute the target time from the current time. Do NOT guess ambiguous times.
list_reminders / cancel_reminder: manage local reminders only, not To Do tasks.
Use task_id from list_reminders to cancel.
```

## 配置

### config.yaml

```yaml
reminders:
  enabled: true
  max_advance_days: 90
  min_advance_seconds: 10
  max_delivery_text_chars: 500
  max_retry_attempts: 3
  retry_delay_seconds: 30
```

### requirements.txt

```
apscheduler>=3.10,<4
```

固定 `<4`，本方案使用 APScheduler 3.x 的 `AsyncIOScheduler` API。

## 涉及改动清单

| 文件 | 改动 |
|------|------|
| `agent/scheduler.py` | **新建** — TaskScheduler（SQLite + APScheduler MemoryJobStore + 到期发送 + bot_contact_state 凭证获取） |
| `memory/store.py` | 新增 `scheduled_tasks` 表；`turns` 表增量增加 `source`/`turn_type` 列（不 `_reset`）；CRUD 方法；`get_bot_contact()` 方法 |
| `agent/tool_registry.py` | 注册 `create_reminder` + `list_reminders` + `cancel_reminder` |
| `agent/tool_exposure_policy.py` | 从 TOOL_REGISTRY.iter_defs() 派生 |
| `agent/tools.py` | 添加 3 个 reminder executor 到 dispatcher dict |
| `agent/context_builder.py` | 稳定工具提示词块追加 reminder 规则 |
| `config.yaml` | 新增 `reminders` 配置段 |
| `main.py` | 初始化 scheduler + wechat_sender_factory；startup 恢复任务；shutdown 停止；/admin/dev/reset-context 保留 user_reminder |
| `bridge/wechat_poller.py` / `wechat_sender` | 扩充发送结果契约，区分 failed_before_send、failed_confirmed、unknown_after_attempt |
| `requirements.txt` | 固定 `apscheduler>=3.10,<4` |
| `tests/` | 新增 `test_scheduler.py`, `test_reminders.py` |

## 测试矩阵

| # | 测试场景 | 预期 |
|---|---------|------|
| 1 | create_reminder 正常创建 | 写入 DB，scheduled 状态 |
| 2 | trigger_at 在过去 | 返回 `too_soon` |
| 3 | trigger_at 超过 90 天 | 返回 `too_far` |
| 4 | delivery_text 超过 500 字符 | 返回 `too_long` |
| 5 | delivery_text 被 guard 拒绝 | 返回 `blocked_by_guard` |
| 6 | 到期触发 → 发送成功 | delivery_text 正确发送，status=fired |
| 7 | 到期触发 → 原子写完整 turn | user system-reminder + assistant delivery_text 在同一事务提交 |
| 8 | raw_events 无孤立 assistant | raw_events 不存 source；关联 turns.source=reminder_due，且两条 active 记录成对出现 |
| 9 | 到期触发 → guard 拒绝 | status=failed，记录 last_error |
| 10 | 发送失败（发送前）→ 重试 | attempt_count 递增，最多 3 次 |
| 11 | 3 次重试后 | status=failed |
| 12 | 发送结果未知 | status=unknown_after_attempt，不自动重发 |
| 13 | list_reminders | 返回 pending 提醒列表 |
| 14 | cancel_reminder | CAS 更新 status=cancelled，job 从 scheduler 移除 |
| 15 | 重启恢复 scheduled 任务 | startup 扫描并重新调度 APScheduler job |
| 16 | 重启恢复 firing 任务 | sender 尚未调用，恢复 scheduled 后安全重试 |
| 17 | 重启恢复 sending 任务 | 转为 unknown_after_attempt，不自动重发 |
| 18 | 重启恢复 unknown_after_attempt 任务 | 记录 WARNING，不自动重发 |
| 19 | 到期和取消并发 | generation CAS 保证只有一个生效，领取时不递增 generation |
| 20 | 静默时段内提醒到期 | 正常发送（不受限制） |
| 21 | 时区正确 | Asia/Shanghai 下 "14:00" 正确计算 UTC |
| 22 | ISO 8601 不含时区 | 返回 `invalid_format` |
| 23 | 跨天提醒 | 正确触发 |
| 24 | bot_contact_state 缺失 | 返回 no_contact 错误，不尝试发送 |
| 25 | /admin/dev/reset-context | user_reminder 保留，proactive_speak 取消 |
| 26 | 完整数据删除 | 所有 scheduled_tasks 被删除 |
| 27 | APScheduler MemoryJobStore coalesce | 多次 misfire 只执行一次 |
| 28 | turns 表 source/turn_type 列 | 增量迁移，现有数据不丢失 |
| 29 | 服务离线期间用户提醒过期 | 启动后立即补发一次并记录 lateness |
| 30 | 服务离线期间主动任务过期 | 不回放旧主动检查，按主动策略重新安排 |
| 31 | 历史锚点注入防护 | reminder_note 不进入 system-reminder，用户文本不能伪造系统指令 |
| 32 | 旧 APScheduler callback 与新 generation 并发 | job 参数携带 expected_generation，旧 callback CAS 失败 |
| 33 | bridge 调用异常且无法确认结果 | 返回 unknown_after_attempt，不被普通失败重试 |

## 依赖顺序

本方案是 **依赖序列的第 5 步**（在持久化 scheduler 基础设施之后）。

## 已否决方案

1. **timefhuman 自然语言解析** — v2 改为模型计算 ISO 8601 时间
2. **纯正则解析时间** — 覆盖面太窄
3. **到期后调用 LLM 生成提醒语** — v2 改为模型在创建时预生成 delivery_text
4. **纯内存 asyncio.sleep 调度** — v2 改为 SQLite + APScheduler 持久化
5. **用户消息取消所有任务（含提醒）** — 提醒不应被打断
6. **无重试机制** — v2 补全有限重试（3 次）
7. **7 天最大提前量** — v2 改为 90 天
8. **到期只写孤立 assistant** — v3 改为写完整 turn（user anchor + assistant）
9. **_SCHEMA_ID 升级 + _reset 迁移** — v3 改为增量 ALTER TABLE
10. **reminder 发送不查 bot_contact_state** — v3 改为从 bot_contact_state 获取凭证

## 参考

- APScheduler 3.x: https://apscheduler.readthedocs.io/en/3.x/
- ISO 8601: https://en.wikipedia.org/wiki/ISO_8601
- 调度器与主动消息共用基础设施，见 [[20260616-proactive-messaging-design]]
