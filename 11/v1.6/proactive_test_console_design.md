# Goal Proactive Test Console v1 设计方案

> 文档状态：正式设计稿  
> 目标读者：项目实现者 / Claude Code / 后续代码审查者  
> 适用系统：Goal Chat Agent，v3.1 proactive pipeline 之后  
> 核心目标：把“主动消息”从只能等待体验，改造成可随时复现、可解释、可验证的专项测试链路。

---

## 0. 背景与定位

Goal Chat Agent 的产品目标是打造具备真人聊天陪伴感的微信 bot。系统中存在两类同等重要的聊天触发源：

1. 用户主动发消息触发一轮聊天。
2. 系统主动 impulse 触发一轮聊天。

本设计只服务第二类触发源，即主动消息。

主动消息不是“定时任务发通知”，而是与用户主动发消息同等权重的聊天入口。主动 impulse 触发后，必须进入现有主动消息链路，并经由状态机、工具调用、输出守卫、微信发送、送达确认、历史写入等环节。

本测试台的唯一核心目的：让开发者不等待真实 heartbeat，也能随时复现主动消息触发链路，并明确知道“为什么发 / 为什么不发 / 发了什么 / 是否真的发出 / 是否写入可见历史”。

---

## 1. 范围声明

### 1.1 本期必须做

本期只做主动消息专项测试台，包含以下能力：

1. 登录后进入主动消息测试台。
2. 主动消息实验室。
3. 主动消息资格检查可视化。
4. impulse 候选生成与选择可视化。
5. dry-run 完整执行。
6. real-send 完整执行。
7. 主动回合执行链展示。
8. guard、send、history write 结果展示。
9. 最近主动测试记录。
10. 一键复制主动消息调试报告。

### 1.2 本期明确不做

以下功能本期不设计、不实现、不预留复杂 UI：

1. 普通聊天测试页。
2. 普通聊天记录管理。
3. 记忆管理台。
4. 记忆预览页。
5. 情绪面板。
6. 好感度面板。
7. 多 bot 管理。
8. 搜索功能测试台。
9. 识图功能测试台。
10. 产品化聊天 UI。
11. 复杂图表、主题皮肤、动画效果。

理由：普通聊天可以直接在微信中体验；记忆、情绪、多 bot、搜索、识图等功能尚未作为本阶段核心实装目标，不应进入主动消息测试台，避免范围失控。

---

## 2. 核心原则

### 2.1 主动消息是一等聊天触发源

主动消息测试台不得把主动消息简化为“后台直接发一条文本”。主动消息触发后必须进入与生产一致的主动回合链路。

正确链路：

```text
ProactiveScheduler / proactive debug runner
→ ChatAgent.chat(source=PROACTIVE_IMPULSE)
→ proactive context 注入
→ LLM tool call 循环
→ wechat_reply / skip_proactive / done
→ guard_proactive_output + guard_single_message
→ wechat_sender
→ delivery status
→ delivered-only history write
```

禁止链路：

```text
前端输入文本
→ 后端直接 bridge.send_text(text)
→ 微信收到
```

上面这种直发链路绕过 ChatAgent、guard、主动终态、历史一致性检查，必须禁止。

### 2.2 dry-run 不能污染可见上下文

Dry-run 用于测试主动链路，不代表用户真的看到了消息。

Dry-run 可以：

1. 生成 impulse 候选。
2. 调用 LLM。
3. 执行 proactive guard。
4. 生成本次调试报告。
5. 写入主动消息调试记录表。

Dry-run 禁止：

1. 调用真实微信发送。
2. 写入 `conversation_history` 中的可见 assistant 历史。
3. 写入真实 `outbound_messages` 发送记录。
4. 触发真实记忆写入。
5. 让后续模型误以为该主动消息已经发给用户。

### 2.3 real-send 必须走生产链路

Real-send 用于真实向微信发送主动消息。Real-send 必须走现有生产主动回合链路，不允许为了实现方便绕过状态机。

Real-send 成功发送后，只有确认送达的消息可以进入可见历史。

### 2.4 测试台只解释主动消息

测试台展示普通历史、最近聊天摘要、用户沉默时间等信息时，目的只能是解释主动消息判断依据。不得扩展为普通聊天管理后台。

### 2.5 不继续扩大 main.py

当前 `main.py` 已承担大量路由、编排、配置、历史、主动回调等职责。主动消息测试台应尽量新增独立模块承载调试逻辑，避免继续把核心逻辑堆入 `main.py`。

---

## 3. 用户入口与页面结构

### 3.1 登录保留

保留现有登录 / 鉴权机制。

目标访问路径建议：

```text
/login
→ /proactive-test-console
```

若当前系统已有 `/admin` 入口，可将 `/admin` 登录后重定向到 `/proactive-test-console`，或让 `/admin` 返回新的主动消息测试台页面。

### 3.2 前端文件建议

新增：

```text
static/proactive_test_console.html
static/proactive_test_console.css
static/proactive_test_console.js
```

旧前端处理原则：

1. 旧 `admin.html`、`admin.css`、`admin.js` 不再作为主要测试入口。
2. 如登录流程依赖旧文件，可保留登录相关最小代码。
3. 不在旧 admin 文件上继续堆新功能。

### 3.3 页面模块

测试台只包含以下模块：

```text
Goal Proactive Test Console
├── 顶部状态栏
├── 主动消息实验室
├── 前置判定面板
├── Impulse 候选面板
├── 主动回合执行链面板
├── 历史一致性检查面板
└── 最近主动测试记录
```

---

## 4. 前端详细设计

### 4.1 顶部状态栏

显示：

1. 当前登录状态。
2. 当前 `user_id`。
3. 当前 `bot_id`。
4. 微信 bridge 状态。
5. proactive 功能开关状态。
6. heartbeat 状态。
7. 今日主动消息真实发送数量。
8. 最近一次主动尝试时间。

顶部状态栏只展示当前系统状态，不提供复杂设置。

### 4.2 主动消息实验室

#### 4.2.1 输入项

必须支持以下输入：

```text
执行模式：
- dry_run
- real_send

Impulse 类型：
- AUTO
- ECHO
- TOPIC_CONTINUE
- PRESENCE
- SCHEDULED_RITUAL

时间模拟：
- debug_now，可为空；为空代表使用真实当前时间
- simulate_user_silent_seconds，可为空
- simulate_last_proactive_seconds，可为空

基础参数：
- user_id，默认当前用户
- bot_id，默认当前 bot
```

#### 4.2.2 操作按钮

必须支持：

1. `检查资格`：只执行 eligibility，不调用 LLM，不发送微信。
2. `生成候选`：执行 eligibility + impulse candidate pipeline，不调用 LLM，不发送微信。
3. `Dry-run 完整执行`：完整跑主动链路，但不真实发送、不写可见历史。
4. `Real-send 完整执行`：完整跑主动链路，并真实发送微信。
5. `复制调试报告`：复制当前 run 的可读报告。

#### 4.2.3 按钮安全规则

1. Real-send 按钮必须有二次确认。
2. Real-send 按钮必须清楚标注“会真实发微信”。
3. Dry-run 按钮必须清楚标注“不会发微信、不会写可见历史”。
4. 当 bridge 离线时，real-send 应禁用或明确提示可能失败。

---

## 5. 后端接口设计

所有接口建议挂在：

```text
/admin/proactive-test/*
```

必须复用现有登录 / 管理鉴权。

### 5.1 获取测试台启动信息

```http
GET /admin/proactive-test/bootstrap
```

返回示例：

```json
{
  "ok": true,
  "current_user_id": "default_user",
  "current_bot_id": "default_bot",
  "bridge": {
    "online": true,
    "logged_in": true
  },
  "features": {
    "proactive": true
  },
  "proactive": {
    "heartbeat_enabled": true,
    "last_attempt_at": "2026-05-31T16:00:00+08:00",
    "today_real_send_count": 2
  }
}
```

### 5.2 主动资格检查

```http
POST /admin/proactive-test/check
```

请求：

```json
{
  "user_id": "default_user",
  "bot_id": "default_bot",
  "debug_now": "2026-05-31T23:30:00+08:00",
  "simulate_user_silent_seconds": 28800,
  "simulate_last_proactive_seconds": 43200
}
```

返回：

```json
{
  "ok": true,
  "eligible": false,
  "checks": [
    {
      "name": "features.proactive",
      "passed": true,
      "actual": true,
      "required": true,
      "reason": null
    },
    {
      "name": "user_silent_window",
      "passed": false,
      "actual": "120 seconds",
      "required": "1800 seconds",
      "reason": "user_silent_window_not_met"
    }
  ],
  "final_reason": "user_silent_window_not_met"
}
```

### 5.3 impulse 候选生成

```http
POST /admin/proactive-test/impulses
```

请求：

```json
{
  "user_id": "default_user",
  "bot_id": "default_bot",
  "forced_impulse_type": "AUTO",
  "debug_now": "2026-05-31T23:30:00+08:00",
  "simulate_user_silent_seconds": 28800,
  "simulate_last_proactive_seconds": 43200
}
```

返回：

```json
{
  "ok": true,
  "eligible": true,
  "candidates": [
    {
      "type": "ECHO",
      "score": 0.82,
      "reason": "最近用户表达疲惫，适合轻量关心",
      "dedup": "pass",
      "expired": false,
      "selected": true
    },
    {
      "type": "PRESENCE",
      "score": 0.55,
      "reason": "用户长时间未互动",
      "dedup": "pass",
      "expired": false,
      "selected": false
    }
  ],
  "selected": {
    "type": "ECHO",
    "score": 0.82
  }
}
```

### 5.4 完整执行主动测试

```http
POST /admin/proactive-test/run
```

请求：

```json
{
  "mode": "dry_run",
  "user_id": "default_user",
  "bot_id": "default_bot",
  "forced_impulse_type": "ECHO",
  "debug_now": "2026-05-31T23:30:00+08:00",
  "simulate_user_silent_seconds": 28800,
  "simulate_last_proactive_seconds": 43200
}
```

`mode` 允许值：

```text
dry_run
real_send
```

返回：

```json
{
  "ok": true,
  "run_id": "pro-debug-20260531-001",
  "mode": "dry_run",
  "terminal_state": "END_REPLIED",
  "selected_impulse": {
    "type": "ECHO",
    "score": 0.82
  },
  "generated_messages": [
    "还在累吗？要不要先歇一会儿。"
  ],
  "guard": {
    "proactive_guard": "pass",
    "general_guard": "pass"
  },
  "send": {
    "attempted": false,
    "status": "DRY_RUN_NOT_SENT"
  },
  "history_write": {
    "written": false,
    "reason": "dry_run_never_writes_visible_history"
  },
  "decision_chain": []
}
```

### 5.5 最近主动测试记录

```http
GET /admin/proactive-test/runs?limit=20&mode=all
```

返回：

```json
{
  "ok": true,
  "items": [
    {
      "run_id": "pro-debug-20260531-001",
      "created_at": "2026-05-31T23:30:00+08:00",
      "mode": "dry_run",
      "impulse_type": "ECHO",
      "terminal_state": "END_REPLIED",
      "send_status": "DRY_RUN_NOT_SENT",
      "history_written": false,
      "summary": "dry-run 生成 1 条主动消息，未发送，未写历史"
    }
  ]
}
```

### 5.6 单次主动测试详情

```http
GET /admin/proactive-test/runs/{run_id}
```

返回应包含：

1. 输入参数。
2. eligibility 结果。
3. candidates。
4. selected impulse。
5. proactive context 摘要。
6. LLM steps。
7. tool calls。
8. guard 结果。
9. send 结果。
10. history write 结果。
11. terminal_state。
12. 可复制报告文本。

---

## 6. 后端模块设计

### 6.1 新增模块建议

建议新增：

```text
agent/proactive_debug.py
```

职责：

1. 定义 `ProactiveDebugContext`。
2. 定义 `ProactiveDebugReport`。
3. 构造测试 impulse meta。
4. 执行 eligibility check。
5. 执行 impulse candidate pipeline。
6. 执行 dry-run。
7. 执行 real-send。
8. 汇总 decision_chain。
9. 生成可复制调试报告。

该模块不应直接实现 LLM 调用，不应直接实现微信发送。LLM 调用仍走 ChatAgent，微信发送仍由现有 sender provider 注入。

### 6.2 新增路由模块建议

建议新增：

```text
admin_proactive_test_routes.py
```

职责：

1. 注册 `/admin/proactive-test/*` 路由。
2. 做请求参数解析。
3. 调用 `agent.proactive_debug`。
4. 返回 JSON。
5. 复用现有登录 / 管理鉴权。

不建议把大量测试逻辑直接写入 `main.py`。

### 6.3 数据结构

建议新增：

```python
@dataclass
class ProactiveDebugContext:
    mode: Literal["dry_run", "real_send"]
    user_id: str
    bot_id: str
    forced_impulse_type: Literal[
        "AUTO",
        "ECHO",
        "TOPIC_CONTINUE",
        "PRESENCE",
        "SCHEDULED_RITUAL",
    ] = "AUTO"
    debug_now: datetime | None = None
    simulate_user_silent_seconds: int | None = None
    simulate_last_proactive_seconds: int | None = None
```

```python
@dataclass
class ProactiveDebugReport:
    run_id: str
    mode: str
    user_id: str
    bot_id: str
    input_overrides: dict
    eligibility: dict
    candidates: list[dict]
    selected_impulse: dict | None
    proactive_context_summary: dict
    llm_steps: list[dict]
    tool_calls: list[dict]
    guard: dict
    send: dict
    history_write: dict
    terminal_state: str
    summary: str
```

---

## 7. Dry-run 语义

### 7.1 Dry-run 必须执行的步骤

Dry-run 必须尽可能接近 real-send 的主动链路：

1. 读取当前配置和状态。
2. 执行 eligibility check。
3. 生成 impulse candidates。
4. 选择 impulse。
5. 构造 proactive_meta。
6. 调用 ChatAgent 主动回合。
7. 允许 LLM 生成 `wechat_reply` / `skip_proactive` / `done`。
8. 执行 proactive guard。
9. 生成 send mock 结果。
10. 汇总 history write mock 结果。
11. 写入 proactive_debug_runs。

### 7.2 Dry-run sender

Dry-run 必须使用 mock sender，不得调用真实微信发送。

建议返回结构：

```json
{
  "ok": true,
  "dry_run": true,
  "delivery_status": "DRY_RUN_NOT_SENT",
  "messages": ["..."]
}
```

### 7.3 Dry-run 历史写入

Dry-run 不得写入可见历史。

若当前 ChatAgent 的 post-terminal pipeline 会自动写历史，则 dry-run 必须注入专用 `write_model_history` callback：

```text
write_model_history_dry_run:
- 记录将要写入什么
- 不实际写入 conversation_history
- 返回 history_write.written=false
- reason=dry_run_never_writes_visible_history
```

### 7.4 Dry-run 记忆写入

Dry-run 不得触发真实记忆写入。若 post-turn hooks 包含 memory hook，则 dry-run 必须禁用真实 memory hook，或替换为 no-op hook。

---

## 8. Real-send 语义

### 8.1 Real-send 必须执行的步骤

Real-send 必须走真实主动链路：

1. eligibility check。
2. impulse candidate pipeline。
3. selected impulse。
4. ChatAgent 主动回合。
5. guard。
6. 真实 `wechat_sender`。
7. 微信桥接发送。
8. delivery status 判断。
9. delivered-only history write。
10. post-turn hooks。
11. proactive_debug_runs 记录。

### 8.2 Real-send 历史写入

只有确认送达的 proactive 文本可以写入 `conversation_history`，角色或元信息应保持现有系统的主动消息标记规则，例如 `assistant:proactive`。

发送失败、guard block、skip_proactive、max limit、LLM failure 等情况下，不得把用户未看到的主动消息写入可见历史。

---

## 9. 决策链展示规范

每次 run 必须输出结构化 decision_chain。建议包含以下阶段：

```text
bootstrap
eligibility
impulse_generation
impulse_filtering
impulse_selection
proactive_context_build
llm_step
model_tool_call
guard
send
history_write
terminal
```

每个阶段至少包含：

```json
{
  "stage": "eligibility",
  "status": "pass | fail | skipped | warning",
  "reason": "human-readable reason",
  "raw": {}
}
```

前端必须既能显示人类可读摘要，也能展开 raw JSON。

---

## 10. 前端展示规范

### 10.1 状态颜色

建议：

```text
绿色：通过 / 发送成功 / 已写历史
黄色：跳过 / dry-run / 无需发送
红色：失败 / guard block / 发送失败 / 历史异常
灰色：未执行 / 不适用
```

### 10.2 必须显示的最终摘要

每次执行后，页面顶部必须显示一句摘要：

示例：

```text
Dry-run：生成 1 条 ECHO 主动消息，guard 通过，未发送微信，未写入可见历史。
```

```text
Real-send：生成 1 条 PRESENCE 主动消息，发送成功，已写入 assistant:proactive 可见历史。
```

```text
跳过：用户刚刚说过话，未满足静默窗口，本次未调用模型。
```

### 10.3 必须显示的历史一致性结果

每次执行必须显示：

```text
是否调用真实微信发送：是 / 否
是否确认送达：是 / 否 / 不适用
是否写入 conversation_history：是 / 否
写入内容：...
未写入原因：...
```

---

## 11. 调试记录表

建议新增表：

```text
proactive_debug_runs
```

字段建议：

```text
id INTEGER PRIMARY KEY
run_id TEXT UNIQUE NOT NULL
created_at TEXT NOT NULL
mode TEXT NOT NULL                  -- dry_run / real_send / heartbeat_debug
user_id TEXT NOT NULL
bot_id TEXT NOT NULL
forced_impulse_type TEXT NOT NULL
actual_impulse_type TEXT
terminal_state TEXT
send_status TEXT
history_written INTEGER NOT NULL DEFAULT 0
summary TEXT
input_overrides_json TEXT
eligibility_json TEXT
candidates_json TEXT
selected_impulse_json TEXT
proactive_context_json TEXT
llm_steps_json TEXT
tool_calls_json TEXT
guard_json TEXT
send_json TEXT
history_write_json TEXT
decision_chain_json TEXT
```

说明：

1. dry-run 必须进入该表。
2. real-send 也应进入该表，方便和真实发送记录交叉验证。
3. heartbeat 自动触发是否进入该表可由实现者评估；本期至少手动测试 run 必须记录。
4. 该表不等于 conversation_history，不作为模型可见上下文来源。

---

## 12. 错误与终态分类

前端应统一展示主动消息结果类别。

### 12.1 跳过类

```text
proactive_disabled
bridge_offline
sleep_window
user_silent_window_not_met
daily_limit_reached
user_opt_out
min_interval_not_met
scheduler_busy
no_impulse_candidate
all_candidates_expired
all_candidates_deduped
score_too_low
model_skip_proactive
```

### 12.2 阻断类

```text
proactive_guard_blocked
mechanism_leak
fabricated_observation
notification_tone
customer_service_tone
protocol_leak
empty_message
invalid_reply_args
```

### 12.3 失败类

```text
llm_error
llm_max_steps
protocol_retry_exceeded
send_failed
rate_limited
bridge_error
max_reply_limit
unknown_error
```

### 12.4 成功类

```text
dry_run_generated
real_send_delivered
real_send_partially_delivered
model_done_no_reply
```

---

## 13. 验收标准

### 13.1 入口验收

1. 登录保留。
2. 登录后可以进入主动消息测试台。
3. 页面能显示当前 proactive 开关、bridge 状态、user_id、bot_id。

### 13.2 主动测试验收

1. 可以选择 `AUTO / ECHO / TOPIC_CONTINUE / PRESENCE / SCHEDULED_RITUAL`。
2. 可以设置 `debug_now`。
3. 可以设置 `simulate_user_silent_seconds`。
4. 可以设置 `simulate_last_proactive_seconds`。
5. 可以只检查 eligibility。
6. 可以只生成 impulse candidates。
7. 可以 dry-run 完整执行。
8. 可以 real-send 完整执行。

### 13.3 Dry-run 验收

1. dry-run 会经过主动链路并生成报告。
2. dry-run 不真实发微信。
3. dry-run 不写 `conversation_history` 可见历史。
4. dry-run 不写真实 `outbound_messages`。
5. dry-run 不触发真实记忆写入。
6. dry-run 写入 `proactive_debug_runs`。

### 13.4 Real-send 验收

1. real-send 真实调用微信发送链路。
2. real-send 不绕过 ChatAgent。
3. 发送成功后，只写 confirmed delivered messages 到可见历史。
4. 发送失败时，不把未送达内容写入可见历史。
5. guard block 时，不发送、不写可见历史。

### 13.5 决策链验收

每次 run 后必须能看到：

1. eligibility 每一项。
2. impulse candidates。
3. selected impulse。
4. proactive context 摘要。
5. LLM steps。
6. tool calls。
7. guard result。
8. send result。
9. history write result。
10. terminal_state。
11. 一句话 summary。
12. raw JSON。

### 13.6 测试验收

至少新增以下测试：

1. dry-run 不调用真实 wechat sender。
2. dry-run 不写 conversation_history。
3. dry-run 不写真实 outbound_messages。
4. real-send 调用现有 proactive chain。
5. forced impulse type 生效。
6. AUTO impulse type 走真实选择逻辑。
7. eligibility 不通过时不调用 LLM。
8. guard block 时不发送、不写历史。
9. 发送失败时不写 delivered history。
10. proactive_debug_runs 可记录并查询。

---

## 14. 实施分期

### Phase 1：测试台壳与 bootstrap

目标：登录后进入新主动消息测试台。

交付：

1. 新前端页面。
2. bootstrap 接口。
3. 顶部状态栏。
4. 不接入真实执行逻辑。

### Phase 2：eligibility 与 impulse candidate 可视化

目标：能解释“为什么现在不能主动发 / 候选是什么”。

交付：

1. check 接口。
2. impulses 接口。
3. 前置判定面板。
4. impulse 候选面板。

### Phase 3：dry-run 完整链路

目标：不用发微信，也能完整跑主动消息链路。

交付：

1. dry-run mode。
2. mock sender。
3. no-op history writer。
4. proactive_debug_runs 记录。
5. 决策链展示。

### Phase 4：real-send 完整链路

目标：从测试台真实触发主动消息。

交付：

1. real-send mode。
2. 二次确认。
3. 真实 sender。
4. delivered-only history write 展示。
5. send/history 一致性校验。

### Phase 5：最近主动测试记录与报告复制

目标：方便复盘。

交付：

1. runs 列表。
2. run 详情。
3. 一键复制调试报告。
4. 最近 20 次主动测试记录。

---

## 15. 非目标再次确认

实现者不得在本期顺手加入以下内容：

1. 记忆管理。
2. 情绪面板。
3. 好感度。
4. 多 bot 创建 / 切换。
5. 普通聊天 UI。
6. 搜索。
7. 识图。
8. 大规模重构。
9. 前端框架迁移。
10. 复杂图表。

如实现中发现必须改动相关模块，应只做最小接口适配，并在计划中单独说明原因。

---

## 16. 给 Claude Code 的启动词

下面这段可直接交给 Claude Code，让它基于本设计文档输出实施计划。注意：先让它产出计划，不要直接改代码。

```text
你是一位严谨的软件工程师。请阅读项目当前代码与本设计文档《Goal Proactive Test Console v1 设计方案》，然后先输出实施计划，不要直接修改代码。

本任务目标：实现“主动消息专项测试台”，不是通用后台。主动消息是系统中的一等聊天触发源，与用户主动发消息触发聊天具有相同权重。本测试台只服务主动消息链路验证。

必须遵守以下边界：
1. 保留现有登录 / 鉴权。
2. 登录后进入主动消息专项测试台。
3. 本期只做主动消息测试，不做普通聊天测试页、不做记忆管理、不做情绪面板、不做好感度、不做多 bot 管理、不做搜索、不做识图。
4. Dry-run 必须完整跑主动链路，但不得真实发微信，不得写 conversation_history 可见历史，不得写真实 outbound_messages，不得触发真实记忆写入。
5. Real-send 必须走现有 ProactiveScheduler / ChatAgent / guard / wechat_sender / delivered-only history write 链路，禁止绕过 ChatAgent 直接 bridge.send_text。
6. 所有调试接口建议挂在 /admin/proactive-test/*，并复用现有管理鉴权。
7. 不要继续把大量业务逻辑塞入 main.py。优先新增 agent/proactive_debug.py 或等价模块承载调试逻辑；路由可拆到独立 admin_proactive_test_routes.py 或等价模块。
8. 前端不追求产品化美观，只追求可测试、可解释、可复制报告。

请先完成以下工作：
1. 阅读当前项目结构，确认与设计文档的差异。
2. 列出需要新增 / 修改的文件。
3. 给出分阶段实施计划，每阶段包含目标、改动点、验收标准、风险。
4. 明确 dry-run 如何避免写入可见历史。
5. 明确 real-send 如何复用现有主动链路。
6. 明确需要补充哪些测试。
7. 明确哪些内容本期不做。

输出格式：
- 总体判断
- 文件改动清单
- Phase 1 / Phase 2 / Phase 3 ... 分阶段计划
- 数据库改动计划
- 接口计划
- 前端计划
- 测试计划
- 风险与规避
- 本期不做清单

注意：本轮只输出计划，等待我确认后再开始改代码。
```
