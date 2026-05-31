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

## BUG-004：部署环境默认暴露 admin=admin，且本地 `.env` 含真实凭据

- **日期**：2026-05-31
- **等级**：P0
- **现象**：
  当前工作区 `.env` 中存在真实 DeepSeek API key 和固定 `CHAT_API_KEY`；`.env.example` 仍给出 `ADMIN_PASSWORD=admin`，`main.py` 在未设置环境变量时也会默认 `ADMIN_PASSWORD = "admin"`。如果按当前 `.env` 的 `HOST=0.0.0.0` 或其他公网绑定方式启动服务，攻击者可以尝试用默认密码进入 `/admin`，读取/修改 persona、查看日志、导出记忆、触发微信发送和二维码部署等管理功能。
- **复现步骤**：
  1. 使用当前 `.env` 或未修改 `.env.example` 的部署环境启动服务。
  2. 访问 `http://<host>:8899/admin`。
  3. 使用默认密码 `admin` 登录。
  4. 调用 `/admin/config`、`/admin/logs`、`/memories/export`、`/admin/send` 等接口。
- **可能原因**：
  `main.py:87` 使用硬编码默认值 `ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")`；`main.py:1436-1452` 登录接口没有强制拒绝默认密码，也没有登录限速；`.env.example` 明确保留 `ADMIN_PASSWORD=admin`；当前本地 `.env` 还包含真实 LLM key 和固定内部 chat key，虽然 `.gitignore` 忽略了 `.env`，但工作区/备份/误传仍会暴露凭据。
- **影响范围**：
  影响任何把服务绑定到非本机网络、反向代理到公网、或上传工作区时包含 `.env` 的部署。影响等级为 P0：可导致管理后台接管、聊天记忆泄露、微信发送滥用和 API key 消耗/泄露。
- **当前处理**：
  `/admin/*` 数据接口依赖 cookie session 和密码校验；`/chat` 默认有 API key 校验；`.gitignore` 会避免 `.env` 被 Git 正常跟踪。但代码没有阻止默认 admin 密码上线，也不会在启动时把该问题作为 fatal error。
- **最终结论**：
  修复方向：生产启动时如果 `ADMIN_PASSWORD` 为空或等于 `admin` 应直接拒绝启动，登录接口应增加限速/审计，部署示例不应给出可直接使用的默认密码；当前 `.env` 内真实 DeepSeek key 和固定 `CHAT_API_KEY` 应视为已暴露并轮换。短期内不要把包含 `.env` 的工作区上传到任何服务环境。

## BUG-005：主动消息复用用户消息中断检查器，导致有过入站消息后主动发送会被永久判定为 interrupted

- **日期**：2026-05-31
- **等级**：P1
- **现象**：
  用户只要曾经通过 `/chat` 进入过一条带 `input_latest_seq > 0` 的消息，后续 heartbeat 或后台手动主动触发在真正发送前会看到：
  ```
  interrupt_check: latest_seq=<历史最大入站序号> > input_latest_seq=0 -> interrupted=True
  pre_send_interrupt: interrupted=True → END_SUPERSEDED_BEFORE_SEND
  ```
  主动消息模型可能已经生成 `wechat_reply`，但会在发送前被丢弃，用户收不到主动消息。
- **复现步骤**：
  1. WeChat bridge 接收用户消息，`bridge/wechat_poller.py` 递增 seq 并向 `/chat` 传入 `input_latest_seq=1`。
  2. `/chat` 在 `main.py:1918` 调用 `_record_user_message_arrival(user_id, seq=1)`，全局 `_user_latest_inbound_seq[user_id]` 变为 1。
  3. 等待 proactive heartbeat，或在后台调用 `/admin/proactive/trigger`。
  4. `main.py:1345` 或 `main.py:2620` 为主动回合构造 `_make_interrupt_checker(user_id, input_latest_seq=0)`。
  5. ChatAgent 在 `agent/chat_agent.py:418-433` 初始检查或 `agent/chat_agent.py:1195-1206` 发送前检查中发现 `latest_seq > 0`，终止主动回合。
- **可能原因**：
  `_make_interrupt_checker` 的语义是“当前用户输入批次的 latest seq 被更晚入站消息超过时打断”，只适用于用户消息回合。主动回合没有自己的输入边界，却传入固定 `input_latest_seq=0`，于是任何历史入站 seq 都会被误判为“更新消息已经到达”。相关位置：`main.py:517-534`、`main.py:1345`、`main.py:2620`、`agent/chat_agent.py:1201-1205`。
- **影响范围**：
  影响 heartbeat 主动消息和后台手动主动触发。只要进程内该用户发生过一次正常 WeChat 入站消息，后续主动发送大概率全部被取消；这是核心主动消息链路的高频故障。
- **当前处理**：
  当前代码把该状态记录为 interrupted / superseded，没有用户可见降级，也不会自动改用新的主动消息专用中断边界。管理员只能从日志或诊断状态间接发现主动消息生成后未送达。
- **最终结论**：
  修复方向：主动回合不能复用 `input_latest_seq=0` 的用户回合 checker。应为主动回合捕获触发时刻的当前 latest seq 作为基线，或实现专用 checker：只在触发之后出现新入站消息时打断。

## BUG-006：ChatAgent post-turn hook 先更新 draft/outbound 状态，main.py 后创建记录，导致后台状态长期显示 generated/created

- **日期**：2026-05-31
- **等级**：P1
- **现象**：
  用户实际收到回复后，后台 drafts/outbound 仍可能显示 `generated` 或 `created`，而不是 `delivered`。这会让管理端误判“已生成但未发送”，尤其影响排查主动消息、fallback、跳过回复和部分发送失败。
- **复现步骤**：
  1. 用户发送正常消息并触发 `chat_agent.chat(...)`。
  2. ChatAgent 在返回前执行 `post_turn_hooks`。
  3. `_hook_dev_logs` 在 `main.py:1783-1803` 调用 `mark_turn_draft_status(...)` 和 `mark_outbound_batch_status(...)`。
  4. 此时 `turn_drafts` 和 `outbound_messages` 还没有对应行，因为 `main.py:2034-2054` 要等 ChatAgent 返回后才创建 outbound 和 draft。
  5. `memory/store.py:1539-1546` 的 `UPDATE turn_drafts` 行数为 0 但调用方未检查；`memory/store.py:1938-1944` 的 outbound batch 更新也命中 0 行。
  6. main.py 随后创建 draft，默认状态为 `generated`；创建 outbound，默认 `send_status="created"`。
- **可能原因**：
  状态更新和记录创建的生命周期顺序反了：ChatAgent 内部 hook 已经被设计为“post-terminal pipeline inside ChatAgent”，但 main.py 仍在 pipeline 之后创建需要被 hook 更新的记录。相关位置：`agent/chat_agent.py:246-251`、`main.py:1783-1803`、`main.py:2034-2054`、`memory/store.py:1503-1523`、`memory/store.py:1868-1908`。
- **影响范围**：
  影响普通聊天回合的后台 drafts/outbound 可观测性，也会影响依赖这些状态判断送达、失败、跳过的管理端功能。实际微信发送可能成功，但数据库视图是错的，排障时容易做出错误判断。
- **当前处理**：
  `turn_diagnostics` 会先被 hook 写入并能记录部分状态；`turns` 表也会在 main.py 后续记录 `send_status`。但 draft/outbound 状态没有有效补偿更新，`mark_*` 返回 false 被静默忽略。
- **最终结论**：
  修复方向：要么在发送前创建 draft/outbound，再由 hook 更新状态；要么 main.py 创建这些行时直接使用 `result.terminal_state` / per-message delivery 状态；同时对 `mark_turn_draft_status` 和 `mark_outbound_batch_status` 的 0-row update 记录 warning。

## BUG-007：ChatAgent 单例把 interrupt_checker 存在实例字段，并发用户/主动回合会互相覆盖中断判断

- **日期**：2026-05-31
- **等级**：P1
- **现象**：
  两个用户或一个用户回合与一个主动/调试回合并发执行时，后进入的 `chat_agent.chat(...)` 会覆盖前一个回合的中断检查器。前一个回合后续的 `INTERRUPT_CHECK`、`PRE_SEND_INTERRUPT_CHECK` 或 `POST_SEND_INTERRUPT_CHECK` 可能使用了另一个用户/另一个触发源的 seq 边界，表现为错误取消、错误不断开、或发送了本应被新消息打断的回复。
- **复现步骤**：
  1. 请求 A 调用 `chat_agent.chat(..., interrupt_checker=checker_A)`，进入 LLM 调用或 post-send delay。
  2. 请求 B 在 A 尚未结束时调用同一个 `chat_agent.chat(..., interrupt_checker=checker_B)`。
  3. `agent/chat_agent.py:181-183` 将 `self._interrupt_checker` 覆盖为 `checker_B`。
  4. 请求 A 后续在 `agent/chat_agent.py:1201` 或 `agent/chat_agent.py:1406` 调用 `self._is_interrupted()` 时使用 checker_B。
- **可能原因**：
  `ChatAgent` 是全局单例，`interrupt_checker` 是 turn-scoped 依赖，却被保存在实例级可变字段 `self._interrupt_checker`。`main.py` 只有 per-user lock（`main.py:713-720`），不同 user_id 之间没有全局锁；主动 scheduler 的锁也独立于用户聊天锁。因此并发路径会共享同一个 ChatAgent 实例字段。
- **影响范围**：
  单用户低并发时不明显；多用户、后台主动触发、测试控制台 dry-run/real-send 与普通聊天同时发生时风险升高。影响核心发送时序，可能造成静默丢回复、过时回复继续发送或诊断状态错误。
- **当前处理**：
  `_is_interrupted` 捕获 checker 异常并返回 False（`agent/chat_agent.py:133-136`），这会进一步把错误隐藏成“没有被打断”。没有 per-turn checker 字段，也没有并发保护或回归测试覆盖跨用户并发。
- **最终结论**：
  修复方向：把 interrupt_checker 存入 `TurnScratchpad` 或作为 `_is_interrupted(turn, token)` 的显式参数，禁止在 ChatAgent 单例字段中保存 turn-scoped 依赖；同时增加两个并发 chat 调用互不污染 checker 的测试。

## BUG-008：`/chat` 对 input seq/cutoff 的类型转换不受保护，畸形内部 payload 可直接触发 500

- **日期**：2026-05-31
- **等级**：P2
- **现象**：
  如果 `/chat` 收到的 `input_first_seq`、`input_latest_seq` 或 `input_cutoff_time` 不是可转换的数字字符串，服务端会在请求主 try 块之前抛出 `ValueError`，返回 500，而不是稳定返回 400。桥接端会把 HTTP 错误当作 Goal chat 失败，给用户发送“（信号不好，稍等）”兜底。
- **复现步骤**：
  1. 向 `/chat` 发送合法鉴权和 JSON body，但设置 `"input_latest_seq": "abc"` 或 `"input_cutoff_time": "bad"`。
  2. 代码执行到 `main.py:1899-1901` 的 `int(...)` / `float(...)`。
  3. 转换异常发生在 `main.py:1925` 的大 try 块之前，未被转换为 `HTTPException(400)`。
- **可能原因**：
  `_normalize_chat_messages` 和 `_validate_chat_user_id` 对输入做了 HTTPException 级别校验，但 v1.2 input boundary 字段直接强制类型转换，没有局部异常处理。相关位置：`main.py:1895-1901`。
- **影响范围**：
  正常 WeChat bridge 传入整数时不受影响；外部调用者、版本不一致的桥接端、或异常 JSON 编码会触发。属于边缘输入导致核心入口 500 的问题。
- **当前处理**：
  FastAPI 会返回内部错误；bridge 捕获 HTTPStatusError 后发送固定兜底。服务本身不会崩溃，但本轮真实回复完全丢失。
- **最终结论**：
  修复方向：为 input boundary 字段添加显式解析函数，转换失败时返回 400，并在日志中标出字段名；也可把这些字段纳入 Pydantic 请求模型。

## BUG-009：`create_turn` 在锁外计算 turn_number，并发记录会产生重复或乱序 turn_number

- **日期**：2026-05-31
- **等级**：P2
- **现象**：
  在两个同一 user_id/bot_id 的回合几乎同时落库时，两个调用都可能读到相同的 `MAX(turn_number)+1`，随后分别插入不同 turn id 但相同 turn_number。后台按 turn_number 排序时会出现顺序不稳定，recap 的 since_number 过滤也可能漏算或重复计算阶段对话。
- **复现步骤**：
  1. 让普通用户回合和主动回合、后台手动 proactive、或两个不同入口同时调用 `memory_store.create_turn(...)`。
  2. 两个协程先后进入 `memory/store.py:1735-1741`，在锁外读取相同 `next`。
  3. 两个协程再进入 `memory/store.py:1745-1756` 插入，各自使用相同 `turn_number`。
  4. `/admin/turns` 或 recap 读取 `turn_number` 时出现同号回合。
- **可能原因**：
  `create_turn` 的“读取下一序号”和“插入回合”不是同一个临界区；SQLite 表也没有 `(user_id, bot_id, turn_number)` 唯一约束兜底。相关位置：`memory/store.py:1735-1756`。普通 `/chat` 只有 per-user lock，但 proactive scheduler 使用另一把锁，且 ChatAgent callbacks 和 main.py 后置记录之间也存在异步时序。
- **影响范围**：
  低并发单用户聊天中概率低；主动消息、后台触发、测试控制台或未来多用户/多进程时概率升高。影响长期历史、recap 边界和后台排序，不一定立即导致用户可见错误。
- **当前处理**：
  当前代码使用 `INSERT OR REPLACE` 按 turn id 去重，但没有保护 turn_number 的单调唯一性；如果重复发生，不会报错。
- **最终结论**：
  修复方向：把 next turn_number 查询移入同一 `async with self._lock` 临界区，或使用数据库事务/唯一索引保证 `(user_id, bot_id, turn_number)` 唯一；并增加并发 create_turn 的回归测试。
