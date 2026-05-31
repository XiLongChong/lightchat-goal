# Bug 集

> 本文档用于记录 goal 项目中发现的 bug。每个 bug 一个条目，按 BUG-XXX 编号。

---

## 格式要求（AI 写入规范）

新增 bug 时，必须严格按照以下模板，确保格式一致性：

```
## BUG-XXX：{简短标题}

- **日期**：YYYY-MM-DD
- **等级**：P0 / P1 / P2 / P3
- **现象**：{用户/开发者能观察到的具体表现，含日志片段}
- **复现步骤**：{触发该 bug 的精确步骤}
- **可能原因**：{根因分析，指向具体代码位置}
- **影响范围**：{哪些场景/用户受影响，概率如何}
- **当前处理**：{当前代码如何应对，是否有 workaround}
- **最终结论**：{修复方案 / 设计决策 / 已知限制}
```

### 等级定义

| 等级 | 含义 | 判断标准 |
|------|------|----------|
| P0 | 阻断 | 系统崩溃、功能完全不可用、数据丢失、安全问题 |
| P1 | 严重 | 核心链路高频故障，用户明显感知，无有效降级 |
| P2 | 中等 | 特定条件下触发，用户体感降级但有兜底，低频 |
| P3 | 轻微 | 日志噪音、UI 瑕疵、边界情况无实际影响 |

### 维护规则

- 编号自增，新 bug 取当前最大编号 +1
- 已修复的 bug 在标题后追加 `[已修复]`，并在最终结论中注明修复 commit
- 确认为设计取舍（非 bug）的条目追加 `[已知限制]`
- 不要删除历史条目，保持完整记录

---

## BUG-001：protocol_retry 后 tool result 与 assistant tool_calls 之间存在 system 消息，导致 DeepSeek API 拒绝请求

- **日期**：2026-05-31
- **等级**：P2
- **现象**：

  日志特征：
  ```
  native_tools: 1 tool_call iter=7 name=done call_id=call_00_MzYt...
  protocol_retry: attempt=1/2
  flash_to_pro_upgrade: count=1/1 upgrading to pro
  LLM call failed: provider_bad_request | 400
    "An assistant message with 'tool_calls' must be followed by tool messages
     responding to each 'tool_call_id'. (insufficient tool messages following
     tool_calls message)"
  hard_fallback: reason=llm_error ... terminal_state=end_fallback_sent
  ```

  用户收到兜底文案 "我刚刚卡了一下 重新说"，丢失本轮真实回复。

- **复现步骤**：

  1. flash 模型在 `sent_count=0` 时调用 `done`（无参数或参数不合法，如 `done()` 或 `done(status='replied')`）
  2. `_do_validate_done_args` 校验失败，往 `turn.messages` 末尾 append 一条 `role=system` 的报错消息，返回 `PROTOCOL_RETRY`
  3. `_do_protocol_retry` 检测到 assistant tool_calls 后缺少 tool 消息，补 dummy tool result（append 到末尾）
  4. 此时消息顺序变为：`assistant(tool_calls) → system(validate报错) → tool(dummy) → system(retry_note)`
  5. 因上次调用为 flash，触发 `FLASH_TO_PRO_UPGRADE`，用 pro 重试
  6. DeepSeek API 校验发现 assistant tool_calls 后紧跟的是 system 而非 tool 消息，拒绝请求（400）
  7. LLM 错误不可重试 → `HARD_FALLBACK`

- **可能原因**：

  消息插入顺序不当。`_do_validate_done_args`（`agent/chat_agent.py:1442-1447`）先 append 了 system 报错消息，之后 `_do_protocol_retry`（`agent/chat_agent.py:1551-1573`）才在末尾补 tool result，导致 system 消息夹在 assistant tool_calls 和 tool result 之间。DeepSeek API 要求每个 `tool_call_id` 的 tool 消息必须紧跟 assistant 消息，中间不允许其他类型消息。

  同样的问题在 `_do_validate_done_args` 的其他 PROTOCOL_RETRY 返回路径（line 1454, 1462, 1468, 1476, 1484）也存在。

- **影响范围**：

  - 仅影响 v1.5 `done` 校验失败 + flash → pro 升级的组合路径
  - flash 模型正确调用 `done`（`status='no_reply'` with note 等合法参数）不受影响
  - pro 模型直接调用无效 `done` → protocol_retry → 同模型重试（不升级），也不受影响（但消息顺序问题依然存在，取决于 DeepSeek 对同模型重试的校验宽松度）
  - 触发频率：取决于 flash 模型对 `done` 参数的合规率。从当前日志看至少已触发 1 次

- **当前处理**：

  触发后走 HARD_FALLBACK，用户收到兜底文案 "我刚刚卡了一下 重新说"。本轮对话的模型输出完全丢失（不写模型历史）。系统可继续处理后续消息，不会卡死。

- **最终结论**：

  修复方向：在 `_do_protocol_retry` 中，将 dummy tool result **插入到** assistant tool_calls 消息的紧后面，而非 append 到消息列表末尾。或者在 `_do_validate_done_args` 的各 PROTOCOL_RETRY 路径中，先 append tool result 再 append system 报错消息。

  如短期内不修复，需监控 `protocol_retry + flash_to_pro_upgrade + llm_error` 组合的出现频率。若频繁触发，升级为 P1。
## BUG-002：约 55 分钟后恢复对话时，模型对时间间隔感知不强

- **日期**：2026-05-31
- **等级**：P2
- **现象**：
  用户在 14:38:26 前后结束上一轮互动，15:33:05 发送“好累”。日志显示当前轮确实是隔了约 54 分 39 秒后恢复：
  ```
  2026-05-31 14:38:26,110 ... delivered=1 msgs, 2 iters, terminal=end_replied
  2026-05-31 15:33:05,269 ... 好累 msg_id=7466753598320346 (buffered)
  2026-05-31 15:33:08,646 ... preview=累也得干啊，不然越拖越多
  2026-05-31 15:33:12,335 ... preview=先动起来，干一点是一点
  ```
  用户体感是：上一轮“安排工作任务”已经过去接近 1 小时，新回复仍像普通连续聊天，没有明显利用“过了一段时间后用户反馈累了”的上下文。
- **复现步骤**：
  1. 用户先让 bot 安排工作任务，bot 回复完成。
  2. 等待接近 1 小时但略小于 1 小时。
  3. 用户发送“好累”。
  4. 观察 bot 回复是否体现“隔了较久才回来、任务执行可能已经消耗精力”的时间感。
- **可能原因**：
  `agent/chat_agent.py:549-560` 已接入 `TemporalContextBuilder`，会把当前时间、上次互动和 gap 写入 system 消息；`agent/temporal_context.py:79-108` 也会计算 `gap_seconds` 与 diagnostics。
  但当前阈值和提示策略偏弱：
  - `agent/temporal_context.py:15` 将 1 小时以内都归为 `_SHORT_THRESHOLD` / `short_resume`，本次约 54 分钟不会进入“同天但 ≥ 1 小时”的更强恢复类型。
  - `agent/temporal_context.py:207-208` 明确要求“时间差只是事实，只有和用户当前消息有关时才自然提及”，这会降低模型主动利用 gap 的概率。
  - 日志只显示 `turn.add_fallback_event("temporal_context", ...)` 被写入诊断，但没有在普通日志中直接打印 temporal diagnostics，排查时无法确认本轮实际传给模型的 gap 文案。
- **影响范围**：
  影响接近 10 分钟到 1 小时之间的恢复聊天，尤其是用户用“累了、做不动了、刚忙完”等短句反馈上一轮任务状态时。不是消息丢失或系统崩溃，但会明显降低陪伴感和连续性。
- **当前处理**：
  当前代码会构建 temporal system message 和时间分割线；如果模型自行使用该信息，可以表现正常。但没有强约束要求在“用户反馈任务状态”时承接时间差，也没有把 temporal diagnostics 打入主日志方便核查。
- **最终结论**：
  确认为体验型 bug。修复方向是让 temporal context 对“任务/疲惫/进度反馈”类恢复消息更强：例如把 45-60 分钟从普通 `short_resume` 提升为更显著的 same-day resume，或在 prompt 中增加“当用户反馈上一轮任务执行状态时，应自然承接经过的时间和可能的消耗”。同时建议把 `temporal_context` diagnostics 以结构化字段写入 `turn_diagnostics` 或普通 info 日志，方便确认每轮实际 gap。

## BUG-003：短上下文追问时，bot 容易把自己上一条建议当成用户说过的话

- **日期**：2026-05-31
- **等级**：P2
- **现象**：
  在用户说“已经干了很久了？”后，bot 回复了“那歇十分钟，喝口水，别刷手机”。随后用户问“你没看见吗？”，bot 第二条回复变成“哦你说歇十分钟那个，看见了，那你自己看着办呗”，语气上像是把“歇十分钟”归因成用户说过的话：
  ```
  2026-05-31 15:33:23,671 ... 用户: 已经干了很久了？
  2026-05-31 15:33:26,809 ... bot preview=那歇十分钟，喝口水，别刷手机
  2026-05-31 15:33:41,263 ... 用户: 你没看见吗？
  2026-05-31 15:33:44,490 ... bot preview=看见啥？你也没发图啊
  2026-05-31 15:33:48,145 ... bot preview=哦你说歇十分钟那个，看见了，那你自己看着办呗
  ```
  这不证明数据库把 user/assistant 角色写反了，但从最终回复看，模型确实发生了发言归属混淆或指代解析错误。
- **复现步骤**：
  1. bot 先给出一个短建议，例如“那歇十分钟，喝口水，别刷手机”。
  2. 用户紧接着用省略指代追问，例如“你没看见吗？”。
  3. 观察 bot 是否把自己上一条建议说成“你说的那个”。
- **可能原因**：
  当前历史写入本身区分了角色：`main.py:1564-1572` 会先写 user，再把 delivered assistant 消息以 `role="assistant"` 写入历史。
  但构建上下文时，`agent/chat_agent.py:574-580` 把历史消息逐条作为 role-based message 加入；`agent/chat_agent.py:1684-1703` 的 `_format_history_entry()` 明确“不加 labels、不加 metadata”，只返回纯 content。对普通上下文这通常足够，但遇到“你没看见吗？”这类省略指代时，模型可能只抓到最近文本“歇十分钟”，没有被额外提醒“这是你刚才自己说的建议，不是用户说的原话”。
  另外，本轮模型先回“看见啥？你也没发图啊”，再在下一次 `wechat_reply` 里自行修正为“哦你说歇十分钟那个...”，说明不是后端发送顺序错乱，而是同一 turn 内的模型二次生成发生了指代/归因漂移。
- **影响范围**：
  影响短间隔、多气泡、强省略指代的聊天。用户容易感觉 bot “忘了谁说过什么”或“把自己的话安到用户头上”。频率取决于模型对 role message 的遵循程度和 prompt 对自我发言归属的约束强度。
- **当前处理**：
  当前代码依赖 provider 的 `role=user/assistant/system/tool` 来区分说话者，没有在历史 content 中加显式说话者标签；也没有针对“引用上一条消息时必须区分你说/我说”的输出约束。
- **最终结论**：
  确认为模型归因/指代稳定性 bug，而不是已证实的数据写反。修复方向可以是：在历史格式中为最近若干条加入轻量 speaker label，或在 system prompt 中增加“引用最近消息时必须区分‘我刚才说’和‘你刚才说’”；同时增加一条回归测试，覆盖 assistant 上一条建议后用户用省略句追问的场景。
