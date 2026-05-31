# Goal Proactive Test Console v1 — 实施计划

> 基于设计文档 `11/v1.6/proactive_test_console_design.md`
> 代码现状审查: 2026-05-31 (v3.1 proactive pipeline)
> 状态: **等待确认**

---

## 0. 总体判断

### 现有基础 (可直接复用)

| 能力 | 位置 | 复用方式 |
|------|------|----------|
| 登录/鉴权 | `main.py` — `/admin/auth/login`, `require_admin()` | 直接复用 |
| ProactiveScheduler | `agent/scheduler.py` | 扩展 forced_impulse_type + debug_now 注入 |
| 主动回合执行 | `agent/chat_agent.py` — `chat(source=PROACTIVE_IMPULSE)` | dry-run 和 real-send 共用 |
| wechat_sender | `main.py:426` | real-send 复用；dry-run 注入 mock |
| write_model_history callback | `main.py` — `_build_proactive_write_model_history()` | real-send 复用；dry-run 注入 no-op |
| post_turn_hooks | `main.py` — `_build_proactive_post_turn_hooks()` | real-send 复用；dry-run 注入 no-op 变体 |
| guard_proactive_output | `agent/output_guard.py` | 两种模式共用 |

### 完全缺失 (需要新建)

| 能力 | 位置 | 说明 |
|------|------|------|
| `ProactiveDebugContext` / `ProactiveDebugReport` | `agent/proactive_debug.py` (新) | 调试数据结构和编排逻辑 |
| `proactive_debug_runs` 表 | `memory/store.py` | 调试记录持久化 |
| `/admin/proactive-test/*` 路由 | `admin_proactive_test_routes.py` (新) | 调试接口 |
| 主动消息测试台前端 | `static/proactive_test_console.*` (新) | 专项测试 UI |
| 时间模拟支持 | 穿透 `ProactiveScheduler` → eligibility → impulse | `debug_now` 参数链路 |
| forced impulse type | `ProactiveScheduler._select_impulse()` | 新增参数绕过 AUTO |
| mock sender (dry-run) | `agent/proactive_debug.py` | 返回 DRY_RUN_NOT_SENT |
| no-op history writer (dry-run) | `agent/proactive_debug.py` | 记录但不写入 |
| decision chain 收集 | `agent/proactive_debug.py` | 逐阶段收集诊断数据 |

### 无需修改

- ChatAgent 状态机 (dry-run 和 real-send 共用同一入口)
- tool_registry (skip_proactive 已注册)
- output_guard (guard_proactive_output 已实现)
- state_machine 类型 (TurnScratchpad 已有 proactive counters)
- 现有 `/admin/proactive/*` 路由 (保留不动)

---

## 1. 文件改动清单

### 新增文件

| 文件 | 职责 |
|------|------|
| `agent/proactive_debug.py` | ProactiveDebugContext, ProactiveDebugReport, dry-run/real-send 编排, mock sender, no-op callbacks, decision chain 收集, 报告生成 |
| `admin_proactive_test_routes.py` | `/admin/proactive-test/*` 路由注册, 请求解析, 鉴权复用, 调用 proactive_debug |
| `static/proactive_test_console.html` | 主动消息测试台页面 |
| `static/proactive_test_console.css` | 测试台样式 |
| `static/proactive_test_console.js` | 测试台前端逻辑 |
| `tests/test_proactive_debug.py` | 测试台专项测试 |

### 修改文件

| 文件 | 改动范围 |
|------|----------|
| `main.py` | 1) `/admin` 路由改为返回 proactive_test_console.html (保留 login/auth 路由)；2) 挂载 `admin_proactive_test_routes` 路由；3) `import` 新模块 |
| `agent/scheduler.py` | 1) `_select_impulse()` 新增 `forced_impulse_type` 参数；2) eligibility check 新增 `debug_now` 参数用于时间模拟；3) `get_metrics()` 可保持不变 |
| `memory/store.py` | 新增 `proactive_debug_runs` 表 + CRUD 方法 |
| `static/admin.html` | **最小改动**: 保留登录流程 (loginView), 登录后重定向到 `/admin/proactive-test-console` 或将旧 admin 保留为独立入口 |

### 不修改的文件

- `agent/chat_agent.py` — 状态机不变
- `agent/state_machine.py` — 类型不变
- `agent/tool_registry.py` — 工具注册不变
- `agent/output_guard.py` — 守卫逻辑不变
- `agent/context_builder.py` — 上下文构建不变
- `config.yaml` — 无需新增配置项

---

## 2. 分阶段实施计划

---

### Phase 1: 数据模型 + proactive_debug 核心模块

**目标**: 建立调试基础设施，不涉及路由和前端。

**改动点**:

1. `memory/store.py` — 新增 `proactive_debug_runs` 表:
   - `id` INTEGER PRIMARY KEY AUTOINCREMENT
   - `run_id` TEXT UNIQUE NOT NULL
   - `created_at` REAL NOT NULL
   - `mode` TEXT NOT NULL (dry_run / real_send)
   - `user_id` TEXT NOT NULL
   - `bot_id` TEXT NOT NULL
   - `forced_impulse_type` TEXT NOT NULL
   - `actual_impulse_type` TEXT
   - `terminal_state` TEXT
   - `send_status` TEXT
   - `history_written` INTEGER NOT NULL DEFAULT 0
   - `summary` TEXT
   - `decision_chain_json` TEXT — 完整决策链 JSON
   - `input_overrides_json` TEXT
   - `eligibility_json` TEXT
   - `candidates_json` TEXT
   - `selected_impulse_json` TEXT
   - `guard_json` TEXT
   - `send_json` TEXT
   - `history_write_json` TEXT
   - `proactive_context_json` TEXT
   - `llm_steps_json` TEXT
   - `tool_calls_json` TEXT
   - CRUD 方法: `insert_debug_run()`, `get_debug_run(run_id)`, `list_debug_runs(limit, mode)`

2. `agent/proactive_debug.py` — 新建模块:
   - `ProactiveDebugContext` dataclass
   - `ProactiveDebugReport` dataclass
   - `build_debug_impulse_meta()` — 根据 forced_impulse_type 构造 impulse_meta
   - `create_dry_run_sender()` — 返回 mock sender (不调微信)
   - `create_dry_run_write_history()` — 返回 no-op write_model_history callback (只记录不写入)
   - `create_dry_run_post_turn_hooks()` — 返回 no-op post_turn_hooks (不写 memory/outbound/diagnostics)
   - `run_proactive_debug()` — 核心编排函数 (根据 mode 分发 dry-run/real-send)

**验收标准**:
- `proactive_debug_runs` 表可创建, CRUD 可正常工作
- `ProactiveDebugContext` 和 `ProactiveDebugReport` 可实例化
- mock sender 返回 `{"send_status": "DRY_RUN_NOT_SENT"}` 结构
- no-op callbacks 不调用任何写操作
- `run_proactive_debug()` 可被手动调用

**风险**: 无。纯新增模块, 不影响现有链路。

---

### Phase 2: 扩展 ProactiveScheduler (forced impulse + 时间模拟)

**目标**: Scheduler 支持测试台所需的参数覆盖。

**改动点**:

1. `agent/scheduler.py`:
   - `_check_eligibility()` 新增 `debug_now: datetime | None` 参数: 当非 None 时, 用 debug_now 替代 `datetime.now()` 做睡眠时段判断和 quiet window 计算
   - `_select_impulse()` 新增 `forced_impulse_type: str = "AUTO"` 参数:
     - `"AUTO"` → 走现有逻辑 (generate → dedup → expire → score → select)
     - `"ECHO"` / `"TOPIC_CONTINUE"` / `"PRESENCE"` / `"SCHEDULED_RITUAL"` → 只生成对应类型的候选, 跳过同类去重, 跳过评分选择 (直接返回)
   - `run_heartbeat()` 和 `run_manual_trigger()` 透传新参数
   - `_in_sleep_hours()` 新增 `debug_now` 参数

2. `agent/proactive_debug.py`:
   - `run_eligibility_check()` — 调用 scheduler eligibility, 收集每项结果
   - `run_impulse_pipeline()` — 调用 scheduler impulse 生成, 收集候选信息
   - `build_decision_chain()` — 逐阶段收集决策信息

**验收标准**:
- `forced_impulse_type="ECHO"` 时只生成 ECHO 类型候选
- `debug_now` 传入时睡眠时段判断使用模拟时间
- `run_eligibility_check()` 返回每项检查的 pass/fail/actual/required/reason
- `run_impulse_pipeline()` 返回候选列表+选中项

**风险**: `debug_now` 需要穿透到 sleep hours / quiet window / min interval 等时间判断点。需确保 `simulate_user_silent_seconds` 和 `simulate_last_proactive_seconds` 从 debug_now 计算而非当前时间。

---

### Phase 3: 路由 + dry-run 完整链路

**目标**: 可通过 HTTP 接口执行 dry-run 并得到完整报告。

**改动点**:

1. `admin_proactive_test_routes.py` — 新建路由模块:
   - `GET /admin/proactive-test/bootstrap` — 返回当前状态
   - `POST /admin/proactive-test/check` — 执行 eligibility check
   - `POST /admin/proactive-test/impulses` — 执行 impulse 候选生成
   - `POST /admin/proactive-test/run` — 执行完整测试 (dry_run / real_send)
   - `GET /admin/proactive-test/runs` — 最近测试记录列表
   - `GET /admin/proactive-test/runs/{run_id}` — 单次测试详情
   - 所有路由复用 `require_admin()` 鉴权

2. `main.py`:
   - 导入 `admin_proactive_test_routes`
   - `app.include_router(admin_proactive_test_routes.router)`
   - `/admin` 路由改为返回 `proactive_test_console.html` (或保留旧 admin.html, 新增 `/admin/proactive-test-console` 返回新页面)

3. `agent/proactive_debug.py` — 完善:
   - `run_proactive_debug()` 实现 dry-run 完整链路:
     1. eligibility check
     2. impulse pipeline
     3. 构造 proactive_meta
     4. 调用 `chat_agent.chat(source=PROACTIVE_IMPULSE, proactive_meta=...)`
     5. 注入 mock sender (不调微信)
     6. 注入 no-op write_model_history (不写 conversation_history)
     7. 注入 no-op post_turn_hooks (不写 memory/outbound/diagnostics)
     8. 收集 decision_chain
     9. 构造 ProactiveDebugReport
     10. 写入 proactive_debug_runs

**验收标准**:
- `POST /admin/proactive-test/run` mode=dry_run 返回完整决策链
- dry-run 不调用 `wechat_sender` (mock sender 拦截)
- dry-run 不写入 `conversation_history`
- dry-run 不写入 `outbound_messages`
- dry-run 不触发 `memory_worker`
- decision_chain 包含 12 阶段完整信息
- 调试记录写入 `proactive_debug_runs`

**风险**:
- **ChatAgent 的 post-terminal pipeline**: ChatAgent 调用 `write_model_history` 和 `post_turn_hooks` 回调。dry-run 必须注入专用 callbacks, **不能依赖 ChatAgent 内部判断 mode**。ChatAgent 对 mode 无感知, 完全由注入的 callbacks 决定行为。
- **memory_hook 在 main.py 条件启用**: dry-run 的 post_turn_hooks 必须不包含 `_hook_memory`。

---

### Phase 4: real-send 完整链路

**目标**: 从测试台真实触发主动消息, 复用生产链路。

**改动点**:

1. `agent/proactive_debug.py`:
   - `run_proactive_debug()` 实现 real-send 路径:
     1. eligibility check
     2. impulse pipeline
     3. 构造 proactive_meta
     4. 调用 `chat_agent.chat(source=PROACTIVE_IMPULSE, proactive_meta=..., wechat_sender=真实sender, write_model_history=真实callback, post_turn_hooks=真实hooks)`
     5. 收集 decision_chain
     6. 构造 ProactiveDebugReport
     7. 写入 proactive_debug_runs

2. `admin_proactive_test_routes.py`:
   - run 接口 mode=real_send 分支: 调用真实的 wechat_sender_provider, write_model_history, post_turn_hooks

**验收标准**:
- real-send 调用真实 wechat_sender
- real-send 走 ProactiveScheduler → ChatAgent → guard → sender 完整链路
- 发送成功后写入 `conversation_history` (assistant:proactive)
- 发送失败时不写可见历史
- guard BLOCK 时不发送不写历史
- 调试记录写入 `proactive_debug_runs`

**风险**:
- **real-send 不绕过 ChatAgent**: real-send 必须通过 `ProactiveScheduler.run_manual_trigger()` → `_execute_proactive_turn()` → `chat_agent.chat()` 链路, **禁止**直接用 `wechat_sender(messages)` 发送。
- **二次确认在前端**: 后端不实现二次确认逻辑, 由前端 JS 在调用 real-send 前弹出确认框。
- **bridge 离线**: 如果 bridge 离线, real-send 会失败。bootstrap 接口返回 bridge 状态, 前端据此禁用按钮或给出提示。

---

### Phase 5: 前端 (主动消息测试台页面)

**目标**: 可用的测试台前端, 功能完整。

**改动点**:

1. `static/proactive_test_console.html`:
   - 登录表单 (复用现有 loginView 逻辑, 密码发 `/admin/auth/login`)
   - 登录成功后显示测试台
   - 页面模块: 状态栏、实验室面板、资格检查面板、候选面板、执行链面板、历史记录

2. `static/proactive_test_console.css`:
   - 极简样式: 无框架, 纯 CSS
   - 状态色: 绿色(pass/sent)、黄色(skip/dry-run)、红色(fail/block)、灰色(not executed)

3. `static/proactive_test_console.js`:
   - 登录逻辑 (POST /admin/auth/login, cookie)
   - bootstrap 加载 (GET /admin/proactive-test/bootstrap)
   - 检查资格按钮 (POST /admin/proactive-test/check)
   - 生成候选按钮 (POST /admin/proactive-test/impulses)
   - Dry-run 按钮 (POST /admin/proactive-test/run, mode=dry_run)
   - Real-send 按钮 (POST /admin/proactive-test/run, mode=real_send) — **含二次确认**
   - 复制报告按钮
   - 最近记录列表 (GET /admin/proactive-test/runs)
   - 单次详情展开 (GET /admin/proactive-test/runs/{run_id})

4. `main.py`:
   - `/admin/proactive-test-console` 返回新 HTML 页面
   - 旧 `/admin` 保留不动 (向后兼容)

**验收标准**:
- 登录后可进入测试台
- 顶部状态栏显示 user_id, bot_id, bridge 状态, proactive 开关, 今日发送数
- 可选择 impulse type (AUTO/ECHO/TOPIC_CONTINUE/PRESENCE/SCHEDULED_RITUAL)
- 可设置 debug_now, simulate_user_silent_seconds, simulate_last_proactive_seconds
- 可选择 mode (dry_run/real_send)
- real-send 按钮有二次确认
- 执行后显示一句话摘要
- 执行后显示决策链
- 可查看最近 20 条测试记录
- 可复制调试报告

**风险**:
- **不追求美观**: 前端极简, 不引入框架。只保证可操作、可解释、可复制报告。
- **旧 admin 保留**: 旧 admin.html 保留不动, 新测试台是独立页面。这样不影响现有管理功能。

---

## 3. Dry-run 如何避免写入可见历史

### 机制

Dry-run 依赖 **回调注入** 实现隔离, 不修改 ChatAgent 内部逻辑。

| 步骤 | 正常主动回合 | Dry-run |
|------|-------------|---------|
| wechat_sender | `main.py:wechat_sender()` → `_bridge.send_text()` | `proactive_debug.create_dry_run_sender()` → 返回 `{"send_status": "DRY_RUN_NOT_SENT"}` |
| write_model_history | `_build_proactive_write_model_history()` → `append_history_async()` | `proactive_debug.create_dry_run_write_history()` → 只记录 `history_write` 元数据, 不调 `append_history_async` |
| post_turn_hooks | `_build_proactive_post_turn_hooks()` → memory store writes | `proactive_debug.create_dry_run_post_turn_hooks()` → 只记录诊断信息, 不写 store |
| memory hook | `_hook_memory` → `memory_worker.extract()` | dry-run hooks 不包含 `_hook_memory` |

### 注入点

```python
# agent/proactive_debug.py (伪代码)
async def run_dry_run(ctx: ProactiveDebugContext) -> ProactiveDebugReport:
    mock_sender = create_dry_run_sender()       # 返回 DRY_RUN_NOT_SENT
    noop_wmh = create_dry_run_write_history()    # 不写 conversation_history
    noop_hooks = create_dry_run_post_turn_hooks() # 不写 memory/outbound

    result = await chat_agent.chat(
        user_message=None,
        source=SOURCE_PROACTIVE_IMPULSE,
        proactive_meta=meta,
        wechat_sender=mock_sender,           # ← 注入 mock sender
        write_model_history=noop_wmh,        # ← 注入 no-op callback
        post_turn_hooks=noop_hooks,          # ← 注入 no-op hooks
        ...
    )
```

### 安全保障

1. ChatAgent 不感知 mode (dry_run vs real_send), 无法绕过回调注入。
2. mock sender 不导入 `bridge` 模块, 无法访问微信 API。
3. no-op callbacks 不调用任何 `memory_store` 写入方法。
4. `proactive_debug_runs` 表不在 `conversation_history` 的读取路径中, 不会被上下文构造器读取。

---

## 4. Real-send 如何复用现有主动链路

### 路径

```
POST /admin/proactive-test/run (mode=real_send)
  → admin_proactive_test_routes.run()
    → agent/proactive_debug.run_real_send(ctx)
      → ProactiveScheduler.run_manual_trigger(user_id, impulse_meta, bot_id, providers...)
        → ProactiveScheduler._execute_proactive_turn()
          → chat_agent.chat(source=PROACTIVE_IMPULSE, wechat_sender=真实sender, write_model_history=真实wmh, post_turn_hooks=真实hooks)
            → [状态机完整执行]
            → [guard_proactive_output + guard_single_message]
            → [EXEC_SEND_ONE → wechat_sender → bridge.send_text]
            → [delivered-only history write]
            → [post_turn_hooks: dev_logs + turn_record + usage_events]
            → TurnResult
      → decision_chain 收集
      → proactive_debug_runs 写入
      → ProactiveDebugReport 返回
```

### 与现有 `/admin/proactive/trigger` 的关系

- 现有 `/admin/proactive/trigger` **保留不动**
- 新 `/admin/proactive-test/run` (mode=real_send) 复用相同的 `ProactiveScheduler.run_manual_trigger()` 入口
- 区别: 新接口额外收集 decision_chain 并写入 proactive_debug_runs

---

## 5. 数据库改动计划

### 新增表

`proactive_debug_runs` — 主动消息调试记录

```sql
CREATE TABLE IF NOT EXISTS proactive_debug_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT UNIQUE NOT NULL,
    created_at REAL NOT NULL,
    mode TEXT NOT NULL,                  -- 'dry_run' | 'real_send'
    user_id TEXT NOT NULL,
    bot_id TEXT NOT NULL,
    forced_impulse_type TEXT NOT NULL,
    actual_impulse_type TEXT,
    terminal_state TEXT,
    send_status TEXT,
    history_written INTEGER NOT NULL DEFAULT 0,
    summary TEXT,
    -- JSON columns:
    input_overrides_json TEXT DEFAULT '{}',
    eligibility_json TEXT DEFAULT '{}',
    candidates_json TEXT DEFAULT '[]',
    selected_impulse_json TEXT DEFAULT '{}',
    proactive_context_json TEXT DEFAULT '{}',
    llm_steps_json TEXT DEFAULT '[]',
    tool_calls_json TEXT DEFAULT '[]',
    guard_json TEXT DEFAULT '{}',
    send_json TEXT DEFAULT '{}',
    history_write_json TEXT DEFAULT '{}',
    decision_chain_json TEXT DEFAULT '[]'
);
```

### CRUD 方法

| 方法 | 签名 | 用途 |
|------|------|------|
| `insert_debug_run` | `(report: ProactiveDebugReport) -> str` | 写入一条调试记录 |
| `get_debug_run` | `(run_id: str) -> dict \| None` | 单次记录详情 |
| `list_debug_runs` | `(limit: int = 20, mode: str = "all") -> list[dict]` | 最近记录列表 |

### 不改动现有表

不新增字段到 `conversation_history`, `outbound_messages`, `turns`, `usage_events` 等表。

---

## 6. 接口计划

所有接口挂在 `/admin/proactive-test/*`，复用 `require_admin()` 鉴权。

| 方法 | 路径 | 请求 | 响应 | Phase |
|------|------|------|------|-------|
| `GET` | `/admin/proactive-test/bootstrap` | — | 当前状态 (user, bot, bridge, features, proactive stats) | 2 |
| `POST` | `/admin/proactive-test/check` | `{user_id, bot_id, debug_now?, simulate_*}` | `{eligible, checks[], final_reason}` | 2 |
| `POST` | `/admin/proactive-test/impulses` | `{user_id, bot_id, forced_impulse_type, debug_now?, simulate_*}` | `{eligible, candidates[], selected}` | 2 |
| `POST` | `/admin/proactive-test/run` | `{mode, user_id, bot_id, forced_impulse_type, debug_now?, simulate_*}` | `ProactiveDebugReport` | 3, 4 |
| `GET` | `/admin/proactive-test/runs` | `?limit=20&mode=all` | `{items[]}` | 3 |
| `GET` | `/admin/proactive-test/runs/{run_id}` | — | 单次详情 (含 decision_chain) | 3 |

所有接口返回 `{"ok": true/false, ...}` 包装。

---

## 7. 前端计划

### 页面文件

| 文件 | 用途 |
|------|------|
| `static/proactive_test_console.html` | 主页面 (含登录表单 + 测试台) |
| `static/proactive_test_console.css` | 极简样式 |
| `static/proactive_test_console.js` | 前端逻辑 (登录、API 调用、DOM 渲染) |

### 模块清单

| 模块 | 功能 |
|------|------|
| 登录 | 密码输入 → POST /admin/auth/login → 设置 cookie → 显示测试台 |
| 顶部状态栏 | GET /admin/proactive-test/bootstrap → 显示 user_id, bot_id, bridge, proactive 开关, 今日发送数, 上次尝试时间 |
| 主动消息实验室 | 参数输入 (mode, impulse_type, debug_now, simulate_*) + 操作按钮 |
| 前置判定面板 | POST /admin/proactive-test/check → 逐项展示 pass/fail + actual vs required |
| Impulse 候选面板 | POST /admin/proactive-test/impulses → 候选列表 (type, score, reason, dedup, expired, selected) |
| 主动回合执行链面板 | POST /admin/proactive-test/run → 12 阶段 decision_chain 展开 + 一句话 summary |
| 历史一致性检查面板 | 显示 send/sent/delivered/history_written + 写入内容/未写入原因 |
| 最近主动测试记录 | GET /admin/proactive-test/runs → 列表, 点击展开详情 |

### 颜色规范

| 颜色 | 含义 |
|------|------|
| 绿色 | pass / sent / delivered / written |
| 黄色 | skipped / dry-run / not applicable |
| 红色 | fail / guard block / send failed / history anomaly |
| 灰色 | not executed / N/A |

---

## 8. 测试计划

### 新增测试文件: `tests/test_proactive_debug.py`

| # | 测试项 | 验证目标 |
|---|--------|----------|
| 1 | `test_dry_run_sender_returns_not_sent` | mock sender 不调微信, 返回 DRY_RUN_NOT_SENT |
| 2 | `test_dry_run_not_write_conversation_history` | dry-run 不写 conversation_history |
| 3 | `test_dry_run_not_write_outbound_messages` | dry-run 不写 outbound_messages |
| 4 | `test_dry_run_not_trigger_memory_write` | dry-run 不触发 memory_worker |
| 5 | `test_dry_run_writes_debug_runs` | dry-run 写入 proactive_debug_runs |
| 6 | `test_real_send_uses_existing_proactive_chain` | real-send 走 ChatAgent → sender → history |
| 7 | `test_forced_impulse_type_echo` | forced_impulse_type="ECHO" 生效 |
| 8 | `test_forced_impulse_type_presence` | forced_impulse_type="PRESENCE" 生效 |
| 9 | `test_auto_impulse_type_uses_real_selection` | AUTO 走真实选择逻辑 |
| 10 | `test_eligibility_fail_no_llm_call` | eligibility 不通过时不调 LLM |
| 11 | `test_guard_block_no_send_no_history` | guard block 时不发送不写历史 |
| 12 | `test_send_failure_no_delivered_history` | 发送失败不写 delivered history |
| 13 | `test_debug_runs_crud` | proactive_debug_runs 可写入、查询、列表 |
| 14 | `test_debug_now_affects_sleep_check` | debug_now 影响睡眠时段判断 |
| 15 | `test_decision_chain_has_all_stages` | decision_chain 包含 12 阶段 |
| 16 | `test_report_generation` | ProactiveDebugReport 包含所有必需字段 |
| 17 | `test_proactive_debug_context_validation` | ProactiveDebugContext 参数校验 |

### 不覆盖的测试

- 前端 E2E 测试 (本期不做)
- 真实微信发送集成测试 (需要真实 bridge, 做不了)
- 性能/并发测试

---

## 9. 风险与规避

| # | 风险 | 规避 |
|---|------|------|
| 1 | **dry-run 误写可见历史**: ChatAgent 的 post-terminal pipeline 自动写历史 | dry-run 注入 no-op write_model_history callback, 不在 callback 内判断 mode |
| 2 | **real-send 绕过 ChatAgent**: 开发者图方便直接用 wechat_sender | real-send 必须走 ProactiveScheduler.run_manual_trigger(), code review 检查 |
| 3 | **debug_now 时间模拟不全**: 某些检查点仍用真实时间 | Phase 2 逐一审查所有 `time.time()` / `datetime.now()` 调用点 |
| 4 | **前端复杂度失控**: 加入非必要的产品化 UI | 极简 CSS, 无框架, CR 检查禁止引入图表库/CSS 框架 |
| 5 | **main.py 继续膨胀**: 测试路由直接写进 main.py | 路由强制放在独立模块 `admin_proactive_test_routes.py` |
| 6 | **旧 admin 功能受影响**: 改 /admin 路由影响现有管理功能 | 旧 /admin 保留不动, 新测试台用 `/admin/proactive-test-console` 独立路径 |
| 7 | **forced impulse type 与 AUTO 不一致**: 强制类型跳过评分选择, 可能导致与生产行为差异 | 文档明确标注 forced 行为, 保留 AUTO 作为默认 |
| 8 | **proactive_debug_runs 膨胀**: 大量 JSON 字段导致表过大 | 单次 run 约 50KB, 100 次约 5MB — SQLite 可承受; 必要时按时间清理 |

---

## 10. 本期不做清单

| 项目 | 原因 |
|------|------|
| 普通聊天测试页 | 设计文档 §1.2 明确排除 |
| 记忆管理/预览 | 设计文档 §1.2 明确排除 |
| 情绪面板 | 设计文档 §1.2 明确排除 |
| 好感度面板 | 设计文档 §1.2 明确排除 |
| 多 bot 管理 | 设计文档 §1.2 明确排除 |
| 搜索/识图 | 设计文档 §1.2 明确排除 |
| 前端框架迁移 | 设计文档 §15 明确排除 |
| 复杂图表/动画/皮肤 | 设计文档 §15 明确排除 |
| 产品化 UI | 设计文档 §2.5 明确排除 |
| Heartbeat 自动调试记录 | 本期手动测试 run 必须记录; heartbeat 自动记录后移 |
| 前端 E2E 测试 | 本期只做后端单元测试 |
| 修改 ChatAgent 状态机 | dry-run/real-send 共用同一入口, ChatAgent 不感知 mode |
| 修改现有 proactive 路由 | `/admin/proactive/*` 保留不动 |
| proactive_debug_runs 自动清理 | 数据量小, 手动清理即可; Phase 7 可补充 |
| 产品化聊天 UI | 真实聊天去微信体验 |
