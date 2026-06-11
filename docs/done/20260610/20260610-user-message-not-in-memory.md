# P0: 用户消息未写入内存，导致历史上下文丢失 + LLM 幻觉（2026-06-09 / 2026-06-10）

## 现象

两个独立观测到的 bug，根因相同：

**Bug A — LLM 幻觉（2026-06-09）**：模型在 thinking 中凭空编造用户回复（"嫌弃我是吧"），自导自演一轮对话，发送了用户从未触发的消息。

**Bug B — 历史丢失（2026-06-10）**：抓包发现对话历史几乎全是模型 thinking 和 tool_result（sent），没有真实用户发言。多轮对话后模型"失忆"。

## 根因

`main.py:_build_protocol_event_writer` 的 `turn_started` 事件处理器只将用户消息写入 DB（`begin_turn`），但**从未写入 `conversation_ctx` 内存**：

```python
# main.py:1078-1085（修复前）
if event_type == "turn_started":
    user_message = payload.get("user_message")
    await memory_store.begin_turn(...)  # 写 DB ✓
    return                               # 漏！没写内存 ✗
```

对比 `message` 事件处理器——assistant 消息和 tool_result **同时写入 DB 和内存**。用户消息是唯一只写 DB 不写内存的事件。

### Bug A（幻觉）的因果链

1. 内存中缺少历史用户消息 → `prebuilt_messages` 只有 assistant 和 tool_result
2. 模型在前几轮只能看到自己的输出，看不到用户到底说了什么
3. 累积到第 N 轮，模型在 thinking 中尝试"重建"对话上下文
4. thinking 中编造的用户消息被当作事实，驱动了额外的 tool_call
5. 表现：模型自导自演

### Bug B（历史丢失）的因果链

1. 每一轮结束后，`conversation_ctx._messages` 缺少用户消息
2. 下一轮 `prebuilt_messages` 不包含历史用户发言
3. 模型无法延续话题，只能从自己的历史回复中猜测上下文
4. 表现：模型"失忆"

## 为什么重启后能恢复

`rebuild()` 从 DB 加载 `active=1` 的 event。`begin_turn` 写入的 seq=0 用户消息在 DB 中且 active=1，重启重建后正常出现。问题只在连续会话期间暴露。

## 修复

`main.py:1085`，`turn_started` 分支加一行 `conversation_ctx.add_event`：

```python
# main.py（修复后）
if event_type == "turn_started":
    user_message = payload.get("user_message")
    await memory_store.begin_turn(
        turn_id=turn.turn_id,
        conversation_id=cid,
        user_message=None if user_message is None else str(user_message),
    )
    if user_message:
        conversation_ctx.add_event("user", str(user_message))
    return
```

`turn_started` 在 `_build_context` 读取 `prebuilt_messages` 快照**之后**才触发，当前 turn 不会出现用户消息重复。

## 日期

发现：2026-06-09 / 2026-06-10
修复：2026-06-10
