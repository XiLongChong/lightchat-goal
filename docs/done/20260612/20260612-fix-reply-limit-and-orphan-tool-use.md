# 回复上限对齐 iLink + 孤儿 tool_use 修复方案（2026-06-12）

## Bug 关联

- `docs/bug/20260611-max-reply-limit-orphans-tool-use.md`
- `docs/bug/20260611-wechat-message-count-truncated.md`

## 问题本质

两个 bug 的根因是同一个：**模型不知道有回复条数限制**。

- 模型无节制地调用 `send_text_message`，代码侧在达到 15 条后暴力截断
- 截断时 assistant `tool_use` 已持久化，`tool_result` 未写入 → 孤儿 tool_use → 后续对话全部 400
- iLink 最多投递 10 条，第 11 条起静默丢失，代码仍返回 `sent` → 用户实际少收到消息

**解决思路**：让模型自己知道上限、自我约束，而不是靠代码截断。代码侧截断降级为极低概率的防御性兜底，且兜底时不能留半条工具轨迹。

---

## 变更 1：`MAX_REPLY_MESSAGES_PER_TURN` 降至 10

**文件**：`agent/state_machine.py:160`

```python
# 旧
MAX_REPLY_MESSAGES_PER_TURN = 15

# 新
MAX_REPLY_MESSAGES_PER_TURN = 10
```

对齐 iLink 回复窗口上限，确保代码侧限制 ≥ iLink 实际投递能力。

---

## 变更 2：协议告知模型回复条数上限

**文件**：`agent/context_builder.py` — `ORDINARY_CHAT_GLOBAL_PROTOCOL`

在 `Each new <channel> message starts one user interaction turn.` 段落（第 10 行）中加入上限约束：

```text
Each new `<channel>` message starts one user interaction turn. A `send_text_message` result belongs to that same interaction turn; it does not mean the user has replied or that a new turn has started. Each successful call sends one visible chat bubble. You may send at most 10 messages per turn. When the tool result says `limit_reached`, you have used all 10 and must stop calling `send_text_message` for this turn. Prefer sending one natural message, reading its result, and then deciding whether another message is genuinely needed. Do not pre-compose an entire sequence of chat bubbles when the next message depends on the previous one. Do not split text meaninglessly, spam, or repeat a delivered message.
```

与原文相比新增两句：

| 新增 | 作用 |
|------|------|
| `You may send at most 10 messages per turn.` | 明确告知上限 |
| `When the tool result says limit_reached, you have used all 10 and must stop calling send_text_message for this turn.` | 告知 `limit_reached` 的含义，与变更 3 配合 |

这样模型在调用前就知道约束，自我控制条数，而不是等到被截断才发现。

---

## 变更 3：达到上限时工具结果从 `sent` 改为 `limit_reached`

**文件**：`agent/chat_agent.py`

当 `sent_message_count` 恰好达到 `MAX_REPLY_MESSAGES_PER_TURN` 时，该次 `send_text_message` 的工具结果不再是 `"sent"`，而是 `"limit_reached: maximum 10 messages per turn reached"`。

在 result_parts append 之后、上限 break 之前插入：

```python
    if turn.sent_message_count >= MAX_REPLY_MESSAGES_PER_TURN:
        for part in result_parts:
            if part.get("content") == "sent":
                part["content"] = "limit_reached: maximum 10 messages per turn reached"
        terminal = TurnState.END_MAX_LIMIT
        break
```

**效果**：模型收到 `limit_reached` 而非 `sent`，知道已到上限，不再尝试发送。即使 turn 未终止，模型下一次 LLM 调用也能从上下文中看到这个信号。

---

## 变更 4：修复上限分支的 tool_result 缺失（防御性兜底）

**文件**：`agent/chat_agent.py:305-316`

无论模型多么守规矩，代码侧必须有正确的兜底——确保任何终止路径都不留半条工具轨迹。

当前代码在上限 break 后跳过了 `tool_result_message` 的持久化。修复方式：将持久化移到 break 之前。

```python
result_parts: list[dict[str, Any]] = []
for call in calls:
    ...
    result_parts.append({...})
    if turn.sent_message_count >= MAX_REPLY_MESSAGES_PER_TURN:
        for part in result_parts:
            if part.get("content") == "sent":
                part["content"] = "limit_reached: maximum 10 messages per turn reached"
        terminal = TurnState.END_MAX_LIMIT
        break

# --- 持久化在 break 之前完成 ---
if result_parts:
    tool_result_message = {"role": "user", "content": result_parts}
    turn.messages.append(tool_result_message)
    await self._write_event(turn, "message", {
        "message": copy.deepcopy(tool_result_message),
        "message_kind": "tool_result",
    })

if terminal == TurnState.END_MAX_LIMIT:
    break
```

原来 311-316 行的 `tool_result_message` 持久化代码删除，被上述 `if result_parts:` 块替代。

---

## 变更 5：更新协议示例

**文件**：`agent/context_builder.py` — `ORDINARY_CHAT_GLOBAL_PROTOCOL`

在现有 tool result 示例段（第 34-42 行）后追加 `limit_reached` 示例：

```text
Tool result: "limit_reached: maximum 10 messages per turn reached"
Correct: stop calling `send_text_message` for this turn. Your already-sent messages have been delivered.
Incorrect: call `send_text_message` again or tell the user about the limit.
```

---

## 变更 6：文档更新

**文件**：`agent/CLAUDE.md`

```markdown
# 旧
- `MAX_REPLY_MESSAGES_PER_TURN = 15` — 单 turn 最多回复条数

# 新
- `MAX_REPLY_MESSAGES_PER_TURN = 10` — 单 turn 最多回复条数（对齐 iLink 回复窗口上限）
```

---

## 不变更的部分

| 项目 | 原因 |
|------|------|
| `output_guard.MAX_MESSAGE_COUNT = 8` | guard 层有独立职责，不改 |
| `MAX_LLM_STEPS_PER_TURN = 20` | LLM 调用轮次与回复条数是不同维度 |
| `_send_text` 内的发送逻辑 | 发送本身无误 |
| `send_text_message` 工具描述 | 上限约束放全局协议更合适，工具描述保持通用 |

## 修复逻辑链条

```
协议告知模型"最多 10 条" → 模型自我约束 → 正常情况不触发代码截断
                                          ↓ 极低概率：模型仍超限
                        工具结果返回 limit_reached → 模型立刻停止
                                          ↓ 兜底：仍需代码 break
                        先持久化 tool_result → 再 break → 无孤儿 tool_use
```

## 验收条件

- [ ] `MAX_REPLY_MESSAGES_PER_TURN` 降至 10
- [ ] 全局协议明确告知模型每 turn 最多 10 条 + `limit_reached` 含义
- [ ] 达到上限时工具结果为 `limit_reached` 而非 `sent`
- [ ] 达到上限时 `tool_result_message` 先于 break 持久化
- [ ] 协议示例包含 `limit_reached` 的正确/错误行为
- [ ] 同进程下一轮对话不报协议错误
- [ ] 重启后重建上下文不报协议错误
- [ ] `agent/CLAUDE.md` 文档更新
