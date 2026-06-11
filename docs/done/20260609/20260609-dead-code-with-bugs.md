# 死代码积累（2026-06-09）

## 1. `memory/store.py` — `snapshot_changed()` 未 await 的协程

**位置**: `memory/store.py:249-257`

```python
return conv is None or conv["context_snapshot_id"] != (
    await self._get_db()
).execute_fetchall(...)
```

`await` 只作用于 `self._get_db()`，`.execute_fetchall()` 返回的协程从未被 await，导致比较结果始终为 True（字符串 != 协程对象）。该方法当前无任何调用方。

## 2. `memory/conversation.py` — `complete()` 从未被调用

**位置**: `memory/conversation.py:60-63`

设计用于清空 DB + 重置内存数组，但整个项目中无任何调用方。`main.py` 中重置上下文直接操作 `conversation_ctx._messages = []`，绕过了此方法。

## 3. `memory/conversation.py` — `reset_for_new_snapshot()` 从未被调用

**位置**: `memory/conversation.py:65-69`

与 `complete()` 完全相同的实现（调用 `store.clear_all()` + 重置 `_messages`）。prompt/tool 变更时的清空逻辑未接入此方法。

## 4. `agent/providers/deepseek.py` — `block_types` 只写不读

**位置**: `deepseek.py:106,142`

流式解析中 `block_types: dict[int, str]` 在 `content_block_start` 事件中写入每个 content block 的类型，但整个函数再无任何读取此 dict 的代码。

## 影响

均为死代码，当前不影响运行。但 `snapshot_changed()` 如果未来被调用会出错。
