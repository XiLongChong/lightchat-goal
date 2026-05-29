# Code Review: 当前代码 vs 目标架构

审查日期: 2026-05-29 | 分支: v3

## 核心发现

当前代码处于 **Phase 1 native tool calls** 过渡期，但距离目标架构还有多个关键缺口。下面按严重程度排列。

---

## 1. `skip_reply` 工具完全缺失（阻断级）

### 现状

- `ToolRegistry` 中 **没有** `skip_reply` 工具注册。
- `config.yaml` `native_tool_calls.allowed_tools` = `[get_time, get_weather, send_wechat_messages]`，缺少 `skip_reply`。
- `ActionRegistry` 中有旧版 `skip`（内部 action，`native_tool_call_eligible` 不适用），但不会被暴露给模型。
- `chat_agent.py:558` 的 `skip_reply` 是事后算出来的：`len(all_messages) == 0`，而不是模型显式调用终态工具的结果。
- 三个 prompt 文件（`base_protocol.md`, `tool_policy.md`, `trigger_context.md`）**没有任何关于 `skip_reply` 的说明**。

### 目标要求

- `skip_reply` 必须是正式注册的 native-tool-call-eligible 终态工具。
- 模型显式调用 `skip_reply` → `EXEC_SKIP_REPLY` → `END_SKIPPED`。
- 模型**不调用任何终态工具** = 协议违规 → `PROTOCOL_RETRY`，不是静默跳过。

### 修复方向

```python
# tool_registry.py 需新增:
TOOL_REGISTRY.register(ToolDef(
    name="skip_reply",
    description="明确决定不回复当前消息。仅在用户消息不需要回复时调用。调用后本轮结束。",
    args_schema={
        "properties": {
            "reason": {"type": "string", "description": "简短说明为什么不需要回复"},
        },
        "required": [],
    },
    side_effect_level="none",
    native_tool_call_eligible=True,
))
```

同时更新 `config.yaml` allowed_tools 和所有 prompt 文件。

---

## 2. 状态机仍是过程式循环，非显式状态机（阻断级）

### 现状

`chat_agent.py` 的 `chat()` 方法是一个 **for 循环 + if/elif/else 分支**（~280 行过程式代码），没有显式状态枚举。目标架构定义了 13 个命名状态，当前代码都没有体现。

### 关键差异对照表

| 目标状态 | 当前实现 | 问题 |
|---------|---------|------|
| `START` | 无对应 | 没有显式的 turn 初始化状态 |
| `BUILD_CONTEXT` | `chat()` 第 131-150 行 | 逻辑分散在 `chat()` 和 `main.py` 的 `build_system_prompt_for_turn()` |
| `CALL_LLM` | 第 189-205 行 | 在 for 循环中，与 tool 结果处理混在一起 |
| `EXEC_FACT_TOOL` | 第 283-312 行 | 执行后**把 tool result 附回 messages 再 continue 循环**——这部分行为正确但无状态标识 |
| `REQUIRE_TERMINAL_ACTION` | **不存在** | 没有显式检查模型是否调用了合法终态工具 |
| `PROTOCOL_RETRY` | 第 336-505 行（tool_retry） | **命名和语义都错**：当前叫 tool_retry，只要求模型调任意工具；目标要求 protocol_retry，要求调终态工具 |
| `EXEC_SEND_WECHAT` | 第 254-280 行 | **关键 bug**：tool result "已发送 N/M 条" 被 feed 回 messages（见下文） |
| `SEND_RETRY` | **不存在** | 微信发送失败后没有重试同一批 messages 的机制 |
| `EXEC_SKIP_REPLY` | **不存在** | `skip_reply` 工具不存在（见第 1 条） |
| `HARD_FALLBACK` | 分散在 4 处（第 211、511、534 行，以及 `_send_fallback_text`） | 文案不统一，部分绕过 guard |
| `WRITE_HISTORY` | `main.py` 第 1617-1628 行 | 即使 skip 也写 assistant 历史（见下文） |
| `END_SENT` / `END_SKIPPED` / `END_FAILED` | 无对应 | 结束态无区分 |

### 修复方向

将 `chat()` 重构为显式状态机。每个状态只做一件事，`while state not in TERMINAL_STATES` 驱动。

---

## 3. `send_wechat_messages` 的 tool result 被喂回模型（阻断级 bug）

### 位置

`chat_agent.py:267-271`:

```python
messages.append({
    "role": "tool",
    "tool_call_id": getattr(tc, "id", ""),
    "content": result_text,  # ← "已发送 2/2 条微信消息"
})
```

### 问题

目标架构明确说："send_wechat_messages 是真实副作用工具，成功后不能再把 tool result 喂回模型。"

当前代码不仅把 "已发送 N/M 条" 注入 messages，还在第 314-323 行用一段复杂逻辑判断是否 break——这个逻辑不可靠，因为 **tool result 已经被注入 messages 数组了**，如果模型在同一轮还调用了 fact tool，它会在下一轮看到这个 tool result。

### 修复方向

`send_wechat_messages` 成功后：不追加 tool result 到 messages；直接进入 `END_SENT`。

---

## 4. `assistant.content` fallback 三跳路径（阻断级 bug）

### 位置

`chat_agent.py:336-505`

### 当前流程

```
模型输出 assistant.content（没有 tool_calls）
  → FIRST_PASS: 检测到 assistant_content（line 338）
  → TOOL_RETRY: assistant.content 被注入回 messages（line 362-365: role="assistant"）
  → 注入 system retry note（lines 367-369）
  → 重新调 LLM
  → 如果 retry 成功 → 处理 tool calls
  → 如果 retry 失败（仍无 tool_calls）→ FINAL_FALLBACK:
      _send_fallback_text(retry_content or assistant_content, ...)  # ← 行 491
      模型原始 content 直接变成微信消息
```

### 三个关键问题

1. **assistant.content 被注入回 messages**（行 362-365）：PROTOCOL_RETRY 不应该把 assistant.content 塞回模型——这会导致语义漂移。

2. **FINAL_FALLBACK 把模型 content 直接发微信**（行 491）：完全绕过了 `send_wechat_messages` 终态工具，违反核心架构约束。

3. **`_send_fallback_text` 有多条路径**：
   - Line 491: `retry_content or assistant_content` — 模型生成的任意内容
   - Line 503: `assistant_content` — 同样
   - 只有 guard 过滤后 ≠ "我刚刚卡了一下 重新说" 才用模型内容，否则用硬兜底——但模型内容仍然有机会通过 guard 发出去。

### 修复方向

- PROTOCOL_RETRY **绝对不注入 assistant.content 回 messages**，只追加简短系统提醒。
- HARD_FALLBACK **只能用固定系统兜底文案**，不使用 retry_content 或 assistant_content。
- 删除 `_send_fallback_text()` 的三跳 fallback 逻辑。

---

## 5. LLM error fallback 绕过统一 guard（中高级 bug）

### 位置

- `chat_agent.py:211`: `all_messages.append({"text": "（信号不太好，稍等我一下）", "delay": 0})`
- `chat_agent.py:511`: `all_messages.append({"text": "（信号不太好，稍等我一下）", "delay": 0})`
- `chat_agent.py:534`: `all_messages.append({"text": "（让我想想...）", "delay": 0})`

### 问题

这些硬兜底消息直接 `append` 到 `all_messages`，没有经过 `guard_single_message()` 或 `guard_send_message_batch()`。虽然这些固定文案本身是安全的，但如果未来有人改了文案，或者兜底逻辑被扩展到使用动态内容，就会绕过 guard。

### 修复方向

所有出站消息（包括硬兜底）必须经过 `VisibleMessageBatch` → guard → `wechat_sender`。不要在 `chat_agent.py` 里直接构造 `all_messages`。

---

## 6. SEND_RETRY 不存在（中高级 gap）

### 目标要求

"微信发送失败后，系统重试发送同一批 messages。不能重新调用模型生成。"

### 现状

`main.py` 的 `wechat_sender()` 回调（行 362-406）对 `_bridge.send_text()` 用 try/except 包裹，失败消息标记 `send_status: "failed"`，但：
- 不重试
- 不回到 `chat_agent.py` 重新发送同一批 messages
- `chat_agent.py` 的 `_handle_send_wechat_messages()` 中，`wechat_sender` 抛出异常时直接返回 `"发送失败：{e}"`（行 707），不做任何重试

### 修复方向

`EXEC_SEND_WECHAT` → 失败 → `SEND_RETRY`（重试同一批 `VisibleMessageBatch`，限制重试次数，不过模型）。

---

## 7. Hard fallback 文案不统一（中级）

当前至少有 3 种不同的硬兜底文案：

| 位置 | 文案 |
|------|------|
| `chat_agent.py:211` LLM error | `（信号不太好，稍等我一下）` |
| `chat_agent.py:511` empty response | `（信号不太好，稍等我一下）` |
| `chat_agent.py:534` max loop exit | `（让我想想...）` |
| `output_guard.py:22` `HARD_FALLBACK_TEXT` | `我刚刚卡了一下 重新说` |
| `chat_agent.py:953` `_send_fallback_text` | `我刚刚卡了一下 重新说` |
| `chat_agent.py:682` guard all blocked | `（信号不太好，稍等我一下）` |

### 修复方向

统一为一个 `HARD_FALLBACK_TEXT` 常量，所有路径使用同一个文案。

---

## 8. 工具处理代码重复（中级维护性问题）

`chat_agent.py` 中，主循环（第 218-333 行）和 retry 路径（第 438-482 行）中处理 `send_wechat_messages`、`get_time`/`get_weather`、unknown tool 的逻辑几乎完全重复。

目标架构的状态机模型可以自然消除这个重复：`PROTOCOL_RETRY` 回到 `CALL_LLM`，而不是内联重复整个工具处理。

---

## 9. skip 时仍写 assistant 历史（中级 bug）

### 位置

`main.py:1617-1628`:

```python
await append_history_async(user_id, "user", user_message, ...)
assistant_text = "\n".join(m["text"] for m in result["messages"] if m.get("text"))
if result["messages"]:   # ← 仅当 messages 非空才写 assistant
    await append_history_async(user_id, "assistant", assistant_text, ...)
```

这段实际上**有条件判断** `if result["messages"]`，所以当 `all_messages` 为空时不会写 assistant 历史。这部分看起来是**对的**。

但需要确认边界：如果 `result["messages"]` 不为空但 `skip_reply=True`（理论上不应同时出现），此时的行为是否正确？

另外 `main.py:1574-1575` 的 `assistant_actions` 字段仍然把 `result["messages"]` 当作 JSON actions 存入 `create_turn`——这是旧 JSON actions 格式的遗留。应该只存文本，不包装成 actions 格式。

### 修复方向

- `END_SKIPPED` → WRITE_HISTORY 只写用户真实消息。
- `END_SENT` → WRITE_HISTORY 写用户消息 + 助手实际发出的文本。
- `END_FAILED` → 不写 "助手成功回复" 历史。

---

## 10. `already_sent` 字段仍在返回（中级）

### 位置

`chat_agent.py:557`:

```python
"already_sent": wechat_sent,
```

目标架构说：`already_sent` 这个 bool 混淆了 "已经发了东西" 和 "模型是否遵守协议"。应该用显式的终态来替代。

当前 `main.py:1693` 还依赖这个字段判断 `bridge_sent`：

```python
bridge_sent = bool(result.get("already_sent"))
```

### 修复方向

用终态明确区分：`END_SENT` / `END_SKIPPED` / `END_FAILED`。

---

## 11. 死代码和需清理项（低级但影响可维护性）

### 应删除的代码

| 文件 | 代码 | 原因 |
|------|------|------|
| `tools.py:132-179` | `_send_email()` | 邮件功能已关闭 |
| `tools.py:16` | `import smtplib` | 仅 send_email 使用 |
| `tools.py:18` | `from email.message import EmailMessage` | 仅 send_email 使用 |
| `tools.py:259-278` | `search_memory` 分支 | 长期记忆已关闭 |
| `tools.py:280-287` | `read_persona_file` 分支 | tool_policy.md 标注为 disabled |
| `tool_registry.py:247-253` | `search_memory` 注册 | 已关闭 |
| `tool_registry.py:283-296` | `read_persona_file` / `propose_persona_change` 注册 | prompt 标注为 disabled |
| `tool_registry.py:297-310` | `send_email` 注册 | 已关闭 |
| `tool_registry.py:201-214` | `wechat_reply` / `skip` action 注册 | 目标架构不需要它们 |
| `agent/bot_profile.py` | 整个文件 | 多 bot 已关闭，PromptPack 组装逻辑不再需要 |
| `context_builder.py:106-134` | `build_system_prompt_v21()` | 依赖 BotProfileManager，已不需要 |
| `context_builder.py:24-103` | `build_system_prompt()` (legacy) | 含 relationship/memory/trigger 等已关闭特性 |
| `output_guard.py:57-88` | `_unwrap_nested_protocol()` | 旧 JSON actions 解包逻辑 |
| `output_guard.py:67-87` | `_extract_visible_text()` 中 JSON actions 解析部分 | 旧格式兼容 |
| `config.yaml:145-168` | `prompt_pack` / `bots` / `outbound` / `recap` legacy sections | 已关闭功能配置 |

### 应更新的文件

| 文件 | 位置 | 问题 |
|------|------|------|
| `config.yaml:24-27` | `allowed_tools` | 添加 `skip_reply` |
| `base_protocol.md` | 全文 | 添加 `skip_reply` 使用说明 |
| `tool_policy.md` | 全文 | 添加 `skip_reply` 作为终态工具 |
| `main.py:314` | `ChatAgent.__init__` | `personality=_read_persona()` 语义混乱——persona 应该是人设，personality 是性格扩展 |

---

## 12. 配置和 prompt 不一致

### `config.yaml` vs `main.py` 的 persona 读取

- `main.py:316`: `persona=cfg["agent"]["persona"]`（内联文本）
- `main.py:317`: `personality=_read_persona()`（**persona.md 文件内容**）

这里 `personality` 参数实际被传入的是 persona 文件的全文，但参数名叫 `personality`。在 `_default_system_prompt()`（`chat_agent.py:576-578`）中两个都被拼接进 system prompt。这导致了重复和语义混乱。

### 旧 JSON actions 格式残留

`base_protocol.md` 已更新为 native tool calls 协议，但 `output_guard.py` 仍有 JSON actions 解包逻辑。`main.py:2082-2084` 的 `_assistant_history_json()` 仍把消息包装成 `{"actions":[{"type":"tool","name":"wechat_reply",...}]}` 格式写入历史。

---

## 13. 架构文档过时

`docs/architecture.md`（当前存在）有以下问题：

- 数据流图显示 `wechat_reply` 仍在使用，而非仅 `send_wechat_messages`
- `search_memory` 标注为 `native_tool_call_eligible: Yes`，但功能已关闭
- 未提及 `skip_reply` 终态工具
- 引用了已删除的 scheduler/emotion/dialogue_state 模块
- 提到 `BotProfileManager` 和多 bot，但用户说多 bot 已关闭

该文件已过时需要重写。

---

## 优先级汇总

### P0 — 阻断：不改则核心架构不成立

1. `skip_reply` 工具缺失 → 模型无法显式选择 "不回复"
2. 状态机需要改为显式状态枚举 → 当前过程式代码无法保证分支正确
3. `send_wechat_messages` tool result 被喂回模型 → 污染模型上下文
4. `assistant.content` fallback 三跳路径 → 模型 content 绕过终态工具直接发微信

### P1 — 重要：直接影响行为正确性

5. LLM error / empty response / max loop 的硬兜底绕过 guard
6. SEND_RETRY 不存在
7. 工具处理代码重复维护风险

### P2 — 改善：清理和一致性

8. Hard fallback 文案统一
9. 历史写入逻辑清理（skip 时不写 assistant）
10. `already_sent` → 终态替代
11. 死代码清理
12. 配置和 prompt 一致性修正
13. `docs/architecture.md` 重写

---

## 当前正确的部分（保留）

- `LLMGateway` + `ProviderAdapter` + `DeepSeekAdapter` 抽象：干净、可扩展。
- `ToolRegistry.get_native_tool_specs()`：正确构建 OpenAI-compatible function specs。
- `guard_send_message_batch()` + `_has_protocol_leak()`：出站 guard 逻辑完备，包括 `call_` 模式硬过滤。
- `compute_message_delay()`：发送间隔逻辑合理。
- `_build_context_sample()` 等诊断函数：只采样不泄露。
- `_format_history_entry()` 的 `call_` 污染过滤：已正确防御。
- WeChat iLink 桥接代码：收发路径独立、可测试。
- 微信轮询 + buffer 去抖：逻辑清晰。
- 管理面板 UI：符合 WeChat 风格设计方向。
