# 时间提醒语义歧义修复方案（2026-06-12）

## Bug 关联

`docs/bug/20260611-time-reminder-gap-misattribution.md`

## 问题摘要

`_time_reminder_block` 生成的 `The previous conversation was 22 minutes ago.` 语义模糊——模型将 "22 minutes ago" 误归因到当前 `<channel>` 消息，而非理解为两条消息之间的空闲间隔。

## 修复范围

三处代码变更 + 一处测试更新，不引入新依赖，不改数据流。

---

## 变更 1：重写提醒文案

**文件**：`agent/runtime_context_builder.py:262-277`（`_time_reminder_block`）

### 当前代码

```python
return (
    "<system-reminder>\n"
    f"Current time: {now:%Y-%m-%d %H:%M:%S} {now:%A}. "
    f"The previous conversation was {elapsed} ago. "
    "This is a system reminder. Use this information when relevant; "
    "otherwise ignore it.\n"
    "</system-reminder>"
)
```

### 替换为

```python
return (
    "<system-reminder>\n"
    f"Current time: {now:%Y-%m-%d %H:%M:%S} {now:%A}. "
    f"The latest message was received just now, after {elapsed} of inactivity since the previous user message. "
    "Use this idle gap only as conversational context when relevant; otherwise ignore it.\n"
    "</system-reminder>"
)
```

### 变更要点

| 旧 | 新 | 原因 |
|---|---|---|
| `The previous conversation was X ago.` | `The latest message was received just now, after X of inactivity since the previous user message.` | 同时明确两个时间事实：当前消息刚到、X 是空闲间隔 |
| `This is a system reminder. Use this information when relevant; otherwise ignore it.` | `Use this idle gap only as conversational context when relevant; otherwise ignore it.` | 缩短，且 "idle gap" 呼应上文，强化指代 |

### 效果对比

**旧输出**：

```text
<system-reminder>
Current time: 2026-06-11 21:00:45 Thursday. The previous conversation was 22 minutes ago. This is a system reminder. Use this information when relevant; otherwise ignore it.
</system-reminder>
```

**新输出**：

```text
<system-reminder>
Current time: 2026-06-11 21:00:45 Thursday. The latest message was received just now, after 22 minutes of inactivity since the previous user message. Use this idle gap only as conversational context when relevant; otherwise ignore it.
</system-reminder>
```

---

## 变更 2：补充全局协议说明

**文件**：`agent/context_builder.py:3-7`（`ORDINARY_CHAT_GLOBAL_PROTOCOL`）

在 `<system-reminder>` 规则段（第 7 行）之后追加一句：

```text
A time reminder describes the idle gap before the latest <channel> message. It never means the latest message itself is old.
```

### 插入位置

在 `Continue from the latest real message inside <channel> and from existing tool results.` 之后另起一行。

### 原因

- 全局协议作为兜底语义约束，当模型注意力落在协议段而非具体提醒时仍能正确理解。
- 不能只依赖全局协议（模型可能不回看），必须在每次注入的提醒文案中同时明确。

---

## 变更 3：更新测试断言

**文件**：`tests/test_context_builder.py:330-349`

### 需要更新的测试

`test_injects_english_reminder_after_channel_when_idle` — 第 346 行：

```python
# 旧
assert "The previous conversation was 12 minutes ago." in reminder

# 新
assert "The latest message was received just now, after 12 minutes of inactivity since the previous user message." in reminder
```

第 347 行：

```python
# 旧
assert "Use this information when relevant; otherwise ignore it." in reminder

# 新
assert "Use this idle gap only as conversational context when relevant; otherwise ignore it." in reminder
```

### 不需要改的测试

- `test_does_not_inject_without_previous_conversation` — 只检查不注入，无文案断言
- `test_does_not_inject_at_ten_minute_threshold` — 只检查不注入，无文案断言
- `test_summary_order_is_summary_channel_then_reminder` — 只检查顺序（`startswith("<system-reminder>")`），不检查文案

### 建议新增的回归测试

在 `TestIdleTimeReminder` 类中新增：

```python
def test_reminder_explicitly_states_just_now(self):
    """时间提醒必须明确标记当前消息是刚收到的。"""
    built = ContextBuilder().build_for_chat(
        user_message="嗯 还早",
        idle_seconds=22 * 60,
        current_time=datetime(2026, 6, 11, 21, 0, 45, tzinfo=timezone(timedelta(hours=8))),
    )
    reminder = built.messages[-1]["content"][1]["text"]
    assert "received just now" in reminder
    assert "of inactivity since the previous user message" in reminder
    assert "ago" not in reminder  # 不再使用 "was X ago" 句式

def test_reminder_does_not_contain_location(self):
    """提醒不应包含地点。"""
    built = ContextBuilder().build_for_chat(
        user_message="hi",
        idle_seconds=15 * 60,
        current_time=datetime(2026, 6, 11, 20, 0, tzinfo=timezone(timedelta(hours=8))),
    )
    reminder = built.messages[-1]["content"][1]["text"]
    assert "Asia/Shanghai" not in reminder
    assert "Beijing" not in reminder
```

---

## 不变更的部分

| 项目 | 原因 |
|------|------|
| `idle_seconds` 计算逻辑（`main.py:1417-1425`） | 计算正确，问题不在计算 |
| 注入位置（当前消息 content 数组内，channel 之后） | 位置正确，保持不变 |
| 阈值逻辑（`> 10min` 注入） | 行为正确，保持不变 |
| `_format_elapsed` 辅助方法 | 输出格式无需变化 |
| 数据流（main → runtime → agent → ContextBuilder） | 不改传递链路 |

## 验收条件（对齐 Bug 文档）

- [ ] 最新消息被明确标记为 `received just now`
- [ ] 时间差被明确标记为上一条用户消息到最新 `<channel>` 消息之间的空闲间隔
- [ ] 不再使用 `The previous conversation was ... ago.`
- [ ] 提醒不包含地点
- [ ] 未达到冷场阈值时不注入提醒（已有测试覆盖）
- [ ] 增加回归测试验证时间指代语义，保留 10 分钟阈值行为测试

## 风险评估

- **低风险**：仅修改文案和断言，不改逻辑和数据流
- **模型行为不可自动验证**：文案是否彻底消除歧义需在真实对话中观察，建议修复后持续监控 LLM trace 中模型对时间提醒的理解
