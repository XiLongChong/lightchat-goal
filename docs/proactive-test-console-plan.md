# Goal Proactive Test Console v1 — 实施计划（修订版 v2）

> 基于设计文档 `11/v1.6/proactive_test_console_design.md`
> 代码现状审查: 2026-05-31 (v3.1 proactive pipeline)
> 修订: 2026-05-31 — v2 审查意见修正
> 补充修订: 2026-05-31 — v2.1 开工前小修订
> 状态: **等待确认**

---

## 0. 总体判断

### 现有基础 (可直接复用)

| 能力 | 位置 | 复用方式 |
|------|------|----------|
| 登录/鉴权 | `main.py` — `/admin/auth/login`, `/admin/auth/status`, `require_admin()` | 直接复用 |
| ProactiveScheduler | `agent/scheduler.py` | 新增 debug-only 方法，heartbeat 不受影响 |
| 主动回合执行 | `agent/chat_agent.py` — `chat(source=PROACTIVE_IMPULSE)` | dry-run/real-send 复用同一入口 |
| wechat_sender | `main.py:426` | real-send 复用；dry-run 注入模拟 sender |
| write_model_history callback | `main.py` — `_build_proactive_write_model_history()` | real-send 复用；dry-run 注入 no-op |
| post_turn_hooks | `main.py` — `_build_proactive_post_turn_hooks()` | real-send 复用；dry-run 注入 no-op 变体 |
| guard_proactive_output | `agent/output_guard.py` | 两种模式共用 |
| ProactiveScheduler.run_manual_trigger() | `agent/scheduler.py` | real-send 唯一入口 |

### 完全缺失 (需要新建)

| 能力 | 位置 | 说明 |
|------|------|------|
| `ProactiveDebugContext` / `ProactiveDebugReport` | `agent/proactive_debug.py` (新) | 调试数据结构 |
| `DecisionChainStage` enum | `agent/proactive_debug.py` (新) | 稳定阶段枚举 |
| `proactive_debug_runs` 表 | `memory/store.py` | 调试记录持久化 |
| `/admin/proactive-test/*` 路由 | `admin_proactive_test_routes.py` (新) | 依赖注入模式 |
| 主动消息测试台前端 | `static/proactive_test_console.*` (新) | 专项测试 UI |
| Scheduler debug-only entry point | `agent/scheduler.py` | 不接受 forced_impulse_type 的 heartbeat 不受影响 |
| 模拟 sender (dry-run) | `agent/proactive_debug.py` | 模拟 deliver 成功 |
| no-op callbacks (dry-run) | `agent/proactive_debug.py` | 不写任何生产表 |
| decision chain 收集器 | `agent/proactive_debug.py` | 按 DecisionChainStage 收集 |
| LLM trace 拦截 (dry-run) | `agent/proactive_debug.py` | 写入 debug_runs 而非 llm_traces 表 |

### 无需修改

- `agent/chat_agent.py` — ChatAgent 不感知 mode
- `agent/state_machine.py` — 类型不变
- `agent/tool_registry.py` — 工具注册不变
- `agent/output_guard.py` — 守卫逻辑不变
- `agent/context_builder.py` — 上下文构建不变
- `config.yaml` — 无需新增配置项
- 现有 `/admin/proactive/*` 路由 — 保留不动

---

## 1. 文件改动清单

### 新增文件

| 文件 | 职责 |
|------|------|
| `agent/proactive_debug.py` | `ProactiveDebugContext`, `ProactiveDebugReport`, `DecisionChainStage` enum, dry-run/real-send 编排, 模拟 sender, no-op callbacks, decision chain 收集, 报告生成 |
| `admin_proactive_test_routes.py` | `create_proactive_test_router(deps)` 工厂函数, 6 个路由, 请求解析, 鉴权复用 |
| `static/proactive_test_console.html` | 主动消息测试台页面 |
| `static/proactive_test_console.css` | 测试台样式 |
| `static/proactive_test_console.js` | 测试台前端逻辑 |
| `tests/test_proactive_debug.py` | 测试台专项测试 |

### 修改文件

| 文件 | 改动范围 |
|------|----------|
| `main.py` | 1) `/admin` 返回 `proactive_test_console.html`；2) `/admin/proactive-test-console` 也返回同页面；3) 挂载 `admin_proactive_test_routes` 路由 (通过 `create_proactive_test_router(deps)`) |
| `agent/scheduler.py` | 1) 新增 debug-only 方法 `run_debug_eligibility()` / `run_debug_impulse_pipeline()`，含 `debug_now` / `forced_impulse_type` 参数；2) `run_heartbeat()` **不接受**这些参数；3) `run_manual_trigger()` 保持不变 |
| `memory/store.py` | 新增 `proactive_debug_runs` 表 + CRUD 方法 (`insert_debug_run`, `get_debug_run`, `list_debug_runs`) |

### 不修改的文件

- `agent/chat_agent.py`, `agent/state_machine.py`, `agent/tool_registry.py`, `agent/output_guard.py`
- `agent/context_builder.py`, `config.yaml`
- `static/admin.html`, `static/admin.css`, `static/admin.js` — 保留文件, 不作为主入口

---

## 2. 分阶段实施计划

---

### Phase 1: 数据模型 + proactive_debug 核心 + DecisionChainStage

**目标**: 建立调试基础设施，定义阶段枚举，不涉及路由和前端。

**改动点**:

1. **`memory/store.py`** — 新增 `proactive_debug_runs` 表 (完整 schema 见 §5):

   ```sql
   CREATE TABLE IF NOT EXISTS proactive_debug_runs (
       id INTEGER PRIMARY KEY AUTOINCREMENT,
       run_id TEXT UNIQUE NOT NULL,
       created_at REAL NOT NULL,
       source TEXT NOT NULL DEFAULT 'admin_proactive_test',
       mode TEXT NOT NULL,                       -- 'dry_run' | 'real_send'
       user_id TEXT NOT NULL,
       bot_id TEXT NOT NULL,
       turn_id TEXT,
       forced_impulse_type TEXT NOT NULL,
       actual_impulse_type TEXT,
       terminal_state TEXT,
       send_status TEXT,
       real_send_attempted INTEGER NOT NULL DEFAULT 0,
       send_simulated INTEGER NOT NULL DEFAULT 0,
       real_delivered_count INTEGER NOT NULL DEFAULT 0,
       simulated_delivered_count INTEGER NOT NULL DEFAULT 0,
       history_written_count INTEGER NOT NULL DEFAULT 0,
       error_code TEXT,
       error_message TEXT,
       summary TEXT,
       -- JSON columns (stored as TEXT):
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

   CRUD 方法:
   - `insert_debug_run(report_dict: dict) -> str` — 写入一条调试记录
   - `get_debug_run(run_id: str) -> dict | None` — 单次记录详情
   - `list_debug_runs(limit: int = 20, mode: str = "all") -> list[dict]` — 最近记录列表

2. **`agent/proactive_debug.py`** — 新建模块:

   ```python
   from enum import StrEnum

   class DecisionChainStage(StrEnum):
       """稳定的决策链阶段枚举。测试验证关键阶段存在，不验证数量。"""
       BOOTSTRAP = "bootstrap"
       ELIGIBILITY = "eligibility"
       IMPULSE_GENERATION = "impulse_generation"
       IMPULSE_FILTERING = "impulse_filtering"
       IMPULSE_SELECTION = "impulse_selection"
       PROACTIVE_CONTEXT_BUILD = "proactive_context_build"
       LLM_STEP = "llm_step"
       MODEL_TOOL_CALL = "model_tool_call"
       GUARD = "guard"
       SEND = "send"
       HISTORY_WRITE = "history_write"
       TERMINAL = "terminal"
   ```

   ```python
   @dataclass
   class ProactiveDebugContext:
       mode: str                         # "dry_run" | "real_send"
       user_id: str
       bot_id: str
       forced_impulse_type: str          # "AUTO" | "ECHO" | "TOPIC_CONTINUE" | "PRESENCE" | "SCHEDULED_RITUAL"
       debug_now: datetime | None = None
       simulate_user_silent_seconds: int | None = None
       simulate_last_proactive_seconds: int | None = None
   ```

   ```python
   @dataclass
   class ProactiveDebugReport:
       run_id: str
       source: str                      # "admin_proactive_test"
       mode: str
       turn_id: str | None
       terminal_state: str
       selected_impulse_type: str | None
       real_send_attempted: bool
       send_simulated: bool
       real_delivered_count: int
       simulated_delivered_count: int
       history_written_count: int
       error_code: str
       error_message: str
       summary: str
       decision_chain: list[dict]       # 每项 {"stage": DecisionChainStage, "status": str, "reason": str, "raw": dict}
       # 以下为完整上下文 (JSON-serializable):
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
   ```

   **核心函数**:
   - `create_simulated_sender()` — 返回 sender callback，对 ChatAgent **模拟 send_status="delivered"**
   - `create_dry_run_write_history()` — 返回 no-op callback，记录 history_write 元数据但不写 `conversation_history`
   - `create_dry_run_post_turn_hooks()` — 返回 no-op hooks，不写 `turns`/`outbound_messages`/`turn_diagnostics`/`usage_events`/`memories`
   - `create_dry_run_llm_trace_handler(run_id)` — 返回 trace handler，将 trace 写入 `proactive_debug_runs.llm_steps_json` 而非全局 `llm_traces` 表
   - `collect_decision_chain(...)` — 按 `DecisionChainStage` 收集阶段信息
   - `generate_report(...)` — 组装 `ProactiveDebugReport`
   - `run_dry_run(ctx) -> ProactiveDebugReport` — 完整 dry-run 编排
   - `run_real_send(ctx) -> ProactiveDebugReport` — 完整 real-send 编排

   **明确不做**:
   - `proactive_debug.py` **不构造** business impulse (如 `build_debug_impulse_meta`)
   - impulse 唯一来源是 `ProactiveScheduler`
   - `proactive_debug.py` 的职责是: 组装 debug context → 调用 scheduler → 注入 callbacks → 收集报告

**验收标准**:
- `proactive_debug_runs` 表可创建, 3 个 CRUD 方法可正常工作
- `DecisionChainStage` 枚举定义完成，不含数字常量
- `ProactiveDebugContext` 和 `ProactiveDebugReport` 包含所有必需字段
- 模拟 sender 对 ChatAgent 返回 `{"send_status": "delivered"}` (不触发失败路径)
- no-op callbacks 不调用任何 `memory_store` 生产写入方法
- `run_dry_run()` 和 `run_real_send()` 签名确定

**风险**: 无。纯新增模块, 不影响现有链路。

---

### Phase 2: 扩展 ProactiveScheduler (debug-only 入口 + 时间模拟)

**目标**: Scheduler 支持测试台所需的参数覆盖，且 heartbeat 不受影响。

**改动点**:

1. **`agent/scheduler.py`** — 新增 debug-only 方法:

   ```python
   # 新增: debug-only eligibility check (heartbeat 不调用此方法)
   async def run_debug_eligibility(
       self, user_id: str, bot_id: str,
       *, debug_now: datetime | None = None,
       simulate_user_silent_seconds: int | None = None,
       simulate_last_proactive_seconds: int | None = None,
   ) -> dict:
       """返回 {"eligible": bool, "checks": [...], "final_reason": str}"""
       ...

   # 新增: debug-only impulse pipeline (heartbeat 不调用此方法)
   async def run_debug_impulse_pipeline(
       self, user_id: str, bot_id: str,
       *, forced_impulse_type: str = "AUTO",
       debug_now: datetime | None = None,
   ) -> dict:
       """返回 {"eligible": bool, "candidates": [...], "selected": {...}}"""
       ...

   # 内部方法: forced_impulse_type 只限制候选来源，仍走完整 pipeline
   async def _select_impulse(
       self, user_id: str, bot_id: str,
       *, forced_impulse_type: str = "AUTO",  # 仅 debug 路径传入
   ) -> dict | None:
       ...
   ```

   **`forced_impulse_type` 语义 (关键修正)**:
   - `"AUTO"` → 走完整真实 pipeline: generate → dedup → expire → score → select
   - `"ECHO"` / `"TOPIC_CONTINUE"` / `"PRESENCE"` / `"SCHEDULED_RITUAL"` →
     **只限制候选来源**（只生成该类型的候选），但**仍然走** dedup → expire → score → select
   - 如果候选在 dedup/expire/score 阶段被过滤掉，**返回空 selected + 过滤原因**，**不强制发送**
   - heartbeat 的 `_select_impulse()` 调用永远传 `forced_impulse_type="AUTO"`

   **`debug_now` 穿透**:
   - `_check_eligibility()` 在 `debug_now` 非 None 时用模拟时间替代 `datetime.now()`
   - `_in_sleep_hours()` 接受 `debug_now` 参数
   - `simulate_user_silent_seconds` / `simulate_last_proactive_seconds` 用于覆盖 store 读取的时间值

   **`run_heartbeat()` — 明确不受影响**:
   ```python
   async def run_heartbeat(self, user_id, bot_id, **providers):
       # 内部调用 _select_impulse(user_id, bot_id, forced_impulse_type="AUTO")
       # 不接受 forced_impulse_type / debug_now 参数
       ...
   ```

2. **`agent/proactive_debug.py`** — 新增编排函数:

   - `run_debug_eligibility_check(scheduler, ctx) -> dict` — 调用 scheduler 的 debug eligibility
   - `run_debug_impulse_pipeline(scheduler, ctx) -> dict` — 调用 scheduler 的 debug impulse pipeline
   - 这两个函数负责:
     1. 从 `ProactiveDebugContext` 提取 `forced_impulse_type`, `debug_now`, `simulate_*`
     2. 调用 scheduler 的 debug-only 方法
     3. 将结果转换为 decision_chain 阶段条目

**验收标准**:
- `forced_impulse_type="ECHO"` 时只生成 ECHO 来源候选, 仍走 dedup/expire/score
- 候选在 score 阶段被过滤掉时返回 `selected: null` + 原因, 不强制发送
- `run_heartbeat()` 不接受 `forced_impulse_type` 参数
- `debug_now` 传入时睡眠时段判断使用模拟时间
- `run_debug_eligibility()` 返回每项检查的 `{name, passed, actual, required, reason}`
- `run_debug_impulse_pipeline()` 返回 `{candidates: [{type, score, reason, dedup, expired, selected}], selected: {...}}`

**风险**:
- `debug_now` 需穿透到 sleep hours / quiet window / min interval，Phase 2 逐一审查时间判断点
- `simulate_user_silent_seconds` 和 `simulate_last_proactive_seconds` 的语义需与真实 store 字段对齐

---

### Phase 3: 路由 (依赖注入模式) + dry-run 完整链路

**目标**: 可通过 HTTP 接口执行 dry-run 并得到完整报告。

**改动点**:

1. **`admin_proactive_test_routes.py`** — 新建路由模块 (**依赖注入模式**):

   ```python
   # admin_proactive_test_routes.py
   # 不 import main.py 全局对象
   # 通过 create_proactive_test_router(deps) 注入依赖

   from fastapi import APIRouter, Request, HTTPException

   def create_proactive_test_router(
       *,
       require_admin,                # Callable[[Request], None]
       proactive_scheduler,          # ProactiveScheduler
       chat_agent,                   # ChatAgent
       memory_store,                 # MemoryStore
       get_history_async,            # Callable
       build_enriched_system_prompt, # Callable
       wechat_sender,                # Callable (真实 sender)
       make_interrupt_checker,       # Callable
       bot_id: str,
       build_proactive_write_model_history,   # Callable (真实 wmh)
       build_proactive_post_turn_hooks,       # Callable (真实 hooks)
   ) -> APIRouter:
       router = APIRouter(prefix="/admin/proactive-test")

       @router.get("/bootstrap")
       async def bootstrap(request: Request): ...

       @router.post("/check")
       async def check(request: Request): ...

       @router.post("/impulses")
       async def impulses(request: Request): ...

       @router.post("/run")
       async def run(request: Request): ...

       @router.get("/runs")
       async def list_runs(request: Request, limit: int = 20, mode: str = "all"): ...

       @router.get("/runs/{run_id}")
       async def get_run(run_id: str, request: Request): ...

       return router
   ```

   所有路由内部调用 `require_admin(request)` 鉴权。

2. **`main.py`** — 挂载路由 + 统一 `/admin` 入口:

   ```python
   from admin_proactive_test_routes import create_proactive_test_router

   # 构造依赖
   proactive_test_router = create_proactive_test_router(
       require_admin=require_admin,
       proactive_scheduler=proactive_scheduler,
       chat_agent=chat_agent,
       memory_store=memory_store,
       get_history_async=get_history_async,
       build_enriched_system_prompt=build_enriched_system_prompt,
       wechat_sender=wechat_sender,
       make_interrupt_checker=_make_interrupt_checker,
       bot_id=BOT_ID,
       build_proactive_write_model_history=_build_proactive_write_model_history,
       build_proactive_post_turn_hooks=_build_proactive_post_turn_hooks,
   )
   app.include_router(proactive_test_router)

   # 统一 /admin 路由策略:
   @app.get("/admin", response_class=HTMLResponse)
   async def admin_page():
       return HTMLResponse(proactive_test_console_html)

   @app.get("/admin/proactive-test-console", response_class=HTMLResponse)
   async def proactive_test_console_page():
       return HTMLResponse(proactive_test_console_html)
   ```

   旧 `static/admin.*` 文件保留在磁盘上，不作为主入口。

3. **`agent/proactive_debug.py`** — 实现 dry-run 编排:

   ```python
   async def run_dry_run(
       ctx: ProactiveDebugContext,
       *,
       scheduler: ProactiveScheduler,
       chat_agent: ChatAgent,
       llm_gateway,                    # 用于 trace 拦截
       memory_store: MemoryStore,      # 仅用于 write debug_runs
   ) -> ProactiveDebugReport:
       chain = []
       run_id = f"pro-debug-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:6]}"

       # 1. eligibility
       eligibility = await scheduler.run_debug_eligibility(...)
       chain.append({stage=ELIGIBILITY, ...})

       if not eligibility["eligible"]:
           # 不调 LLM
           return generate_report(run_id, mode="dry_run", chain, ...)

       # 2. impulse pipeline
       impulses = await scheduler.run_debug_impulse_pipeline(forced_impulse_type=...)
       chain.append({stage=IMPULSE_GENERATION, ...})

       if not impulses["selected"]:
           return generate_report(run_id, mode="dry_run", chain, ...)

       # 3. 构造 callbacks
       sim_sender = create_simulated_sender()          # 模拟 delivered
       noop_wmh = create_dry_run_write_history()        # 不写 conversation_history
       noop_hooks = create_dry_run_post_turn_hooks()    # 不写任何生产表

       # 4. LLM trace 拦截
       original_trace = llm_gateway._on_trace_callbacks
       dry_trace_handler = create_dry_run_llm_trace_handler(run_id)
       llm_gateway._on_trace_callbacks = [dry_trace_handler]
       try:
           # 5. 调用 ChatAgent
           result = await chat_agent.chat(
               user_message=None,
               source=SOURCE_PROACTIVE_IMPULSE,
               proactive_meta=impulses["selected"],
               wechat_sender=sim_sender,
               write_model_history=noop_wmh,
               post_turn_hooks=noop_hooks,
               ...
           )
       finally:
           llm_gateway._on_trace_callbacks = original_trace  # 恢复

       # 6. 收集 decision chain
       chain.extend(collect_decision_chain(result, ...))

       # 7. 写入 debug_runs
       report = generate_report(run_id, "dry_run", chain,
           real_send_attempted=False,
           send_simulated=True,
           real_delivered_count=0,
           simulated_delivered_count=result.proactive_delivered_count,
           history_written_count=0,
           ...)
       await memory_store.insert_debug_run(report_to_dict(report))

       return report
   ```

   **关键: 模拟 sender 返回 delivered**:
   ```python
   def create_simulated_sender():
       async def _sender(messages):
           for m in messages:
               m["send_status"] = "delivered"  # 模拟成功, ChatAgent 正常流转
           return messages
       return _sender
   ```

   这样 ChatAgent 正常走完 wechat_reply → done 流程，不进入失败路径。
   报告中的 `send_json` 明确标记 `real_send_attempted=false, send_simulated=true`。

**验收标准**:
- `POST /admin/proactive-test/run` mode=dry_run 返回完整 `ProactiveDebugReport`
- 模拟 sender 对 ChatAgent 返回 delivered，ChatAgent 正常结束
- dry-run **不写** `conversation_history`
- dry-run **不写** `outbound_messages`
- dry-run **不写** `turns` (L2 turn 记录)
- dry-run **不写** `turn_diagnostics`
- dry-run **不写** `usage_events`
- dry-run **不写** `memories` (不触发 memory_worker)
- dry-run **不写** `llm_traces` (LLM trace 写入 debug_runs.llm_steps_json)
- `ProactiveDebugReport` 包含 `real_send_attempted=false, send_simulated=true`
- decision_chain 包含所有关键 `DecisionChainStage`
- 调试记录写入 `proactive_debug_runs`
- `admin_proactive_test_routes.py` 不 import `main` 模块

**风险**:
- **LLM trace 拦截**: 当前 `llm_gateway.on_trace()` 注册全局 trace callback。dry-run 期间需临时替换，执行完毕后恢复。需加 try/finally 保证不泄漏。
- **ChatAgent 不感知 mode** — 这正是设计目标。ChatAgent 完全由注入的 callbacks 控制行为。
- **模拟 sender 返回 delivered**: 需要确认 ChatAgent 的 `_do_exec_send_one` 正确处理 `send_status="delivered"`。当前代码已支持此路径 (正常生产路径)。

---

### Phase 4: real-send 完整链路

**目标**: 从测试台真实触发主动消息, 复用生产链路。

**改动点**:

1. **`agent/proactive_debug.py`** — 实现 real-send 编排:

   ```python
   async def run_real_send(
       ctx: ProactiveDebugContext,
       *,
       scheduler: ProactiveScheduler,
       memory_store: MemoryStore,
       # Providers (由 routes 层注入, 来自 main.py):
       history_provider,
       system_prompt_provider,
       wechat_sender_provider,
       interrupt_checker_provider,
       write_model_history,         # 真实 _build_proactive_write_model_history
       post_turn_hooks,             # 真实 _build_proactive_post_turn_hooks
   ) -> ProactiveDebugReport:
       chain = []
       run_id = f"pro-debug-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:6]}"

       # 1. eligibility (debug 方法)
       eligibility = await scheduler.run_debug_eligibility(...)
       ...

       # 2. impulse pipeline (debug 方法, forced_impulse_type 可选)
       impulses = await scheduler.run_debug_impulse_pipeline(...)
       ...

       # 3. 构造 impulse_meta (从 scheduler 的 selected impulse)
       if not impulses["selected"]:
           return generate_report(...)

       # 4. 通过 ProactiveScheduler.run_manual_trigger() 执行
       #    proactive_debug.py 不直接调用 chat_agent.chat()
       scheduler_result = await scheduler.run_manual_trigger(
           user_id=ctx.user_id,
           impulse_meta=impulses["selected"],
           bot_id=ctx.bot_id,
           history_provider=history_provider,
           system_prompt_provider=system_prompt_provider,
           wechat_sender_provider=wechat_sender_provider,  # 真实 sender
           interrupt_checker_provider=interrupt_checker_provider,
           write_model_history=write_model_history,          # 真实 wmh
           post_turn_hooks=post_turn_hooks,                  # 真实 hooks
       )

       # 5. 提取 TurnResult
       turn_result = scheduler_result["result"]

       # 6. 收集 decision chain
       ...

       # 7. 写入 debug_runs
       report = generate_report(run_id, "real_send", chain,
           real_send_attempted=True,
           send_simulated=False,
           real_delivered_count=turn_result.proactive_delivered_count,
           simulated_delivered_count=0,
           history_written_count=history_written_count,
           turn_id=turn_result.turn_id,
           ...)
       await memory_store.insert_debug_run(report_to_dict(report))

       return report
   ```

   **关键: real-send 统一路径**:
   ```
   /admin/proactive-test/run (mode=real_send)
     → admin_proactive_test_routes.run()
       → proactive_debug.run_real_send(ctx, providers=...)
         → ProactiveScheduler.run_manual_trigger(user_id, impulse_meta, bot_id, providers)
           → ProactiveScheduler._execute_proactive_turn()
             → chat_agent.chat(source=PROACTIVE_IMPULSE, ...)
               → [生产状态机 + guard + 真实 sender + delivered-only history write]
   ```

   **禁止**: `proactive_debug.py` 直接调用 `chat_agent.chat()` 执行 real-send。
   **理由**: `ProactiveScheduler` 负责 impulse → turn 的完整编排和锁管理。

2. **`admin_proactive_test_routes.py`** — run 接口 mode=real_send 分支:

   调用 `proactive_debug.run_real_send()` 时传入生产 callback providers (由 `create_proactive_test_router(deps)` 注入)。

**验收标准**:
- real-send 走 `ProactiveScheduler.run_manual_trigger()` → `chat_agent.chat()` 完整链路
- `proactive_debug.py` 不直接调用 `chat_agent.chat()` 执行 real-send
- 真实调用 `wechat_sender`
- 发送成功后写入 `conversation_history` (assistant:proactive)
- 发送失败时不写可见历史
- guard BLOCK 时不发送不写历史
- `ProactiveDebugReport` 包含 `real_send_attempted=true, send_simulated=false`
- 调试记录写入 `proactive_debug_runs` (含 `turn_id`)

**风险**:
- **二次确认在前端**: 后端不实现二次确认
- **bridge 离线**: bootstrap 接口返回 bridge 状态, 前端据此禁用 real-send 按钮
- **与 run_manual_trigger 的 impulse_meta**: 确保 impulse_meta 是从 scheduler debug pipeline 产出的, 而非 proactive_debug 自行构造

---

### Phase 5: 前端 (主动消息测试台页面)

**目标**: 可用的测试台前端, 功能完整。

**改动点**:

1. **`main.py` — 统一 `/admin` 入口策略**:

   - `GET /admin` → 返回 `proactive_test_console.html`
   - `GET /admin/proactive-test-console` → 也返回 `proactive_test_console.html`
   - 现有 `/admin/auth/login`, `/admin/auth/status`, `/admin/auth/logout` 保留不动
   - 旧 `static/admin.*` 文件保留, 不作为主入口

2. **`static/proactive_test_console.html`**:
   - 登录表单 (复用现有 loginView 逻辑)
   - 登录后显示测试台
   - 页面模块: 状态栏、实验室面板、资格检查面板、候选面板、执行链面板、历史一致性面板、最近记录

3. **`static/proactive_test_console.css`**: 极简样式, 无框架, 状态色 (绿/黄/红/灰)

4. **`static/proactive_test_console.js`**:
   - 登录逻辑, bootstrap 加载, 4 个操作按钮, 复制报告, 最近记录, 详情展开
   - real-send 按钮含二次确认弹窗

**验收标准**: 与设计文档 §13 一致。详见原计划 Phase 5。

---

## 3. Dry-run 如何避免写入可见历史

### 机制

Dry-run 依赖 **回调注入** 实现隔离。ChatAgent 不感知 mode。

| 步骤 | 正常主动回合 | Dry-run |
|------|-------------|---------|
| wechat_sender | `main.py:wechat_sender()` → `_bridge.send_text()` | `create_simulated_sender()` → 返回 `{"send_status": "delivered"}` (**模拟成功**) |
| write_model_history | `_build_proactive_write_model_history()` → `append_history_async()` | no-op callback → 记录元数据, 不调 `append_history_async` |
| post_turn_hooks | `_build_proactive_post_turn_hooks()` → store writes | no-op hooks → 不写 turns/outbound/diagnostics/usage/memories |
| LLM trace | `llm_gateway._on_trace_callbacks` → `save_llm_trace()` | 临时替换 → 写入 `proactive_debug_runs.llm_steps_json` |
| memory hook | `_hook_memory` → `memory_worker.extract()` | dry-run hooks 不包含 `_hook_memory` |

### 模拟 sender 返回 delivered

```python
def create_simulated_sender():
    async def _sender(messages):
        for m in messages:
            m["send_status"] = "delivered"  # ChatAgent 正常流转
        return messages
    return _sender
```

**重要**: 模拟 sender **不**返回 `"DRY_RUN_NOT_SENT"`。ChatAgent 的 `_do_exec_send_one` 根据 `send_status` 判断送达/失败/限流, 返回非 `"delivered"` 会进入失败路径, 影响 LLM 后续决策。模拟 sender 对 ChatAgent 模拟成功, 但报告明确标记 `real_send_attempted=false, send_simulated=true`。

### 禁止写入的生产表

| 表 | dry-run 写入? | 替代 |
|----|:---:|------|
| `conversation_history` | 否 | `proactive_debug_runs.history_write_json` 记录"将要写什么" |
| `outbound_messages` | 否 | — |
| `turns` (L2) | 否 | — |
| `turn_diagnostics` | 否 | `proactive_debug_runs.decision_chain_json` |
| `usage_events` | 否 | — |
| `memories` | 否 | — |
| `llm_traces` | 否 | `proactive_debug_runs.llm_steps_json` (标记 `dry_run_id`) |
| `proactive_debug_runs` | **是** (唯一) | — |

### 安全保障

1. ChatAgent 不感知 mode, 无法绕过回调注入。
2. 模拟 sender 不导入 `bridge` 模块。
3. no-op callbacks 不调用任何 `memory_store` 生产写入方法。
4. LLM trace 临时拦截 + try/finally 恢复, 不泄漏到全局 trace。
5. `proactive_debug_runs` 不在 `conversation_history` 读取路径中。

---

## 4. Real-send 如何复用现有主动链路

### 路径 (统一, 不可绕过)

```
POST /admin/proactive-test/run (mode=real_send)
  → admin_proactive_test_routes.run()
    → proactive_debug.run_real_send(ctx, providers=...)
      → ProactiveScheduler.run_manual_trigger(user_id, impulse_meta, bot_id, providers...)
        → ProactiveScheduler._execute_proactive_turn()
          → chat_agent.chat(
              user_message=None,
              source=SOURCE_PROACTIVE_IMPULSE,
              proactive_meta=impulses["selected"],  # 来自 scheduler
              wechat_sender=真实sender,              # 来自 main.py
              write_model_history=真实wmh,           # 来自 main.py
              post_turn_hooks=真实hooks,             # 来自 main.py
            )
            → [状态机完整执行]
            → [guard_proactive_output + guard_single_message]
            → [EXEC_SEND_ONE → wechat_sender → bridge.send_text → iLink API]
            → [delivered-only history write → conversation_history]
            → [post_turn_hooks → turns + outbound + diagnostics + usage_events]
            → TurnResult
      → decision_chain 收集
      → proactive_debug_runs 写入 (含 turn_id)
      → ProactiveDebugReport 返回
```

### 关键约束

1. **`proactive_debug.py` 不直接调用 `chat_agent.chat()`** — 必须通过 `ProactiveScheduler.run_manual_trigger()`
2. **impulse_meta 来自 scheduler** — `proactive_debug.py` 不自行为 real-send 构造 impulse
3. **providers 来自 main.py** — 通过 `create_proactive_test_router(deps)` 注入

---

## 5. 数据库改动计划

### 新增表

```sql
CREATE TABLE IF NOT EXISTS proactive_debug_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT UNIQUE NOT NULL,
    created_at REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'admin_proactive_test',
    mode TEXT NOT NULL,                       -- 'dry_run' | 'real_send'
    user_id TEXT NOT NULL,
    bot_id TEXT NOT NULL,
    turn_id TEXT,
    forced_impulse_type TEXT NOT NULL,
    actual_impulse_type TEXT,
    terminal_state TEXT,
    send_status TEXT,
    real_send_attempted INTEGER NOT NULL DEFAULT 0,
    send_simulated INTEGER NOT NULL DEFAULT 0,
    real_delivered_count INTEGER NOT NULL DEFAULT 0,
    simulated_delivered_count INTEGER NOT NULL DEFAULT 0,
    history_written_count INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    error_message TEXT,
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

CREATE INDEX IF NOT EXISTS idx_debug_runs_user_bot
ON proactive_debug_runs(user_id, bot_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_debug_runs_mode
ON proactive_debug_runs(mode, created_at DESC);
```

### CRUD 方法

| 方法 | 签名 | 用途 |
|------|------|------|
| `insert_debug_run` | `(report_dict: dict) -> str` | 写入一条调试记录, 返回 run_id |
| `get_debug_run` | `(run_id: str) -> dict \| None` | 单次记录详情 |
| `list_debug_runs` | `(user_id: str, bot_id: str, limit: int = 20, mode: str = "all") -> list[dict]` | 最近记录列表 |

### 不改动现有表

不新增字段到 `conversation_history`, `outbound_messages`, `turns`, `turn_diagnostics`, `usage_events`, `memories`, `llm_traces`。

---

## 6. 接口计划

所有接口挂在 `/admin/proactive-test/*`，复用 `require_admin()` 鉴权。

| 方法 | 路径 | 请求 | 响应 | Phase |
|------|------|------|------|-------|
| `GET` | `/admin/proactive-test/bootstrap` | — | `{user_id, bot_id, bridge, features, proactive_stats}` | 3 |
| `POST` | `/admin/proactive-test/check` | `{user_id, bot_id, debug_now?, simulate_*}` | `{eligible, checks[], final_reason}` | 3 |
| `POST` | `/admin/proactive-test/impulses` | `{user_id, bot_id, forced_impulse_type, debug_now?}` | `{eligible, candidates[], selected}` | 3 |
| `POST` | `/admin/proactive-test/run` | `{mode, user_id, bot_id, forced_impulse_type, debug_now?, simulate_*}` | `ProactiveDebugReport` | 3, 4 |
| `GET` | `/admin/proactive-test/runs` | `?limit=20&mode=all` | `{items[]}` | 3 |
| `GET` | `/admin/proactive-test/runs/{run_id}` | — | 单次详情 (含 decision_chain) | 3 |

接口统一返回 `{"ok": true/false, ...}` 包装。

---

## 7. 前端计划

与设计文档 §4, §10 一致。新增 `static/proactive_test_console.*` 三个文件。
路由: `/admin` 和 `/admin/proactive-test-console` 都返回该页面。
旧 `static/admin.*` 保留文件, 不作为主入口。

---

## 8. 测试计划

### 新增测试文件: `tests/test_proactive_debug.py`

| # | 测试项 | 验证目标 |
|---|--------|----------|
| 1 | `test_simulated_sender_returns_delivered` | 模拟 sender 对 ChatAgent 返回 delivered (不触发失败路径) |
| 2 | `test_dry_run_not_write_conversation_history` | dry-run 不写 conversation_history |
| 3 | `test_dry_run_not_write_outbound_messages` | dry-run 不写 outbound_messages |
| 4 | `test_dry_run_not_write_turns` | dry-run 不写 turns (L2) |
| 5 | `test_dry_run_not_write_turn_diagnostics` | dry-run 不写 turn_diagnostics |
| 6 | `test_dry_run_not_write_usage_events` | dry-run 不写 usage_events |
| 7 | `test_dry_run_not_trigger_memory_write` | dry-run 不写 memories |
| 8 | `test_dry_run_intercepts_llm_trace` | dry-run 期间 LLM trace 写入 debug_runs 不写 llm_traces |
| 9 | `test_dry_run_writes_only_debug_runs` | dry-run 唯一写入的表是 proactive_debug_runs |
| 10 | `test_dry_run_report_has_simulated_flags` | report 包含 real_send_attempted=false, send_simulated=true |
| 11 | `test_real_send_uses_scheduler_not_direct_chat` | real-send 走 scheduler.run_manual_trigger(), 不直接调 chat_agent.chat() |
| 12 | `test_forced_impulse_type_limits_source_only` | forced_impulse_type="ECHO" 只生成 ECHO 来源, 仍走 dedup/expire/score |
| 13 | `test_forced_impulse_filtered_out_shows_reason` | 候选被过滤时返回 selected=null + 原因, 不强制发送 |
| 14 | `test_auto_impulse_type_uses_full_pipeline` | AUTO 走完整 generate→dedup→expire→score→select |
| 15 | `test_eligibility_fail_no_llm_call` | eligibility 不通过时不调 LLM |
| 16 | `test_guard_block_no_send_no_history` | guard BLOCK 时不发送不写历史 (dry-run 和 real-send 分别测) |
| 17 | `test_send_failure_no_delivered_history` | 发送失败不写 delivered history (real-send 用 fake sender 模拟失败) |
| 18 | `test_debug_runs_crud` | proactive_debug_runs 可写入、查询、列表 |
| 19 | `test_debug_now_affects_sleep_check` | debug_now 影响睡眠时段判断 |
| 20 | `test_decision_chain_has_key_stages` | decision_chain 包含所有 DecisionChainStage 枚举值 |
| 21 | `test_decision_chain_not_hardcoded_count` | 测试不验证 decision_chain 长度等于固定数字 |
| 22 | `test_report_generation_all_fields` | ProactiveDebugReport 包含 §5 表的所有非 JSON 字段 |
| 23 | `test_proactive_debug_context_validation` | ProactiveDebugContext 参数校验 (mode/forced_impulse_type 枚举) |
| 24 | `test_heartbeat_does_not_accept_forced_type` | run_heartbeat() 不接受 forced_impulse_type 参数 |
| 25 | `test_router_uses_dependency_injection` | admin_proactive_test_routes 不 import main 模块 |

### real-send 单测说明

real-send 单测使用 **fake sender** 验证链路结构:
- fake sender 返回 `{"send_status": "delivered"}` 或 `{"send_status": "failed"}`
- 验证链路走完了 `ProactiveScheduler.run_manual_trigger()` → `chat_agent.chat()`
- 验证 delivered-only history write 逻辑 (只有 delivered 写入)
- 不调用真实 `wechat_sender` / `bridge.send_text`

### 不覆盖的测试

- 前端 E2E 测试 (本期不做)
- 真实微信发送集成测试 (需要真实 bridge)
- 性能/并发测试

---

## 9. 风险与规避

| # | 风险 | 规避 |
|---|------|------|
| 1 | **dry-run 误写可见历史** | no-op callbacks 注入, ChatAgent 不感知 mode |
| 2 | **模拟 sender 导致 ChatAgent 走失败路径** | 模拟 sender 返回 `"delivered"`, ChatAgent 正常流转 |
| 3 | **LLM trace 泄漏到生产表** | try/finally 临时替换 + 恢复 llm_gateway trace callbacks |
| 4 | **real-send 绕过 ChatAgent** | real-send 必须走 `ProactiveScheduler.run_manual_trigger()` |
| 5 | **`proactive_debug.py` 自行构造 impulse** | impulse 唯一来源是 ProactiveScheduler, debug 模块只做编排 |
| 6 | **forced_impulse_type 强制绕过过滤** | 只限制来源, 仍走完整 pipeline, 过滤掉返回原因 |
| 7 | **debug_now 时间模拟不全** | Phase 2 逐一审查所有时间判断点 |
| 8 | **run_heartbeat 被测试参数污染** | heartbeat 不接受 forced_impulse_type/debug_now |
| 9 | **前端复杂度失控** | 极简 CSS, 无框架, CR 检查 |
| 10 | **main.py 继续膨胀** | 路由在独立模块, 依赖注入模式 |
| 11 | **循环 import** | `admin_proactive_test_routes.py` 不 import `main.py`, 依赖通过 `create_proactive_test_router(deps)` 注入 |
| 12 | **proactive_debug_runs 膨胀** | SQLite 可承受, 必要时按时间清理 |
| 13 | **decision_chain 阶段数量硬编码** | 使用 `DecisionChainStage` 枚举; 测试验证关键阶段存在, 不验证数量 |

---

## 10. 本期不做清单

| 项目 | 原因 |
|------|------|
| 普通聊天测试页 | 设计文档 §1.2 |
| 记忆管理/预览/情绪/好感度/多 bot/搜索/识图 | 设计文档 §1.2 |
| 前端框架/图表/动画/皮肤 | 设计文档 §15 |
| 产品化 UI | 设计文档 §2.5 |
| Heartbeat 自动调试记录 | 后移; 本期手动 run 必须记录 |
| 前端 E2E 测试 | 本期只做后端单元测试 |
| 修改 ChatAgent 状态机 | ChatAgent 不感知 mode |
| 修改现有 `/admin/proactive/*` 路由 | 保留不动 |
| 修改 `config.yaml` | 无需新配置 |
| 修改 `agent/state_machine.py` | 类型不変 |
| `proactive_debug_runs` 自动清理 | 数据量小; 后续可补充 |
| `proactive_debug.py` 构造 business impulse | impulse 必须来自 ProactiveScheduler |
| `run_heartbeat` 支持测试参数 | heartbeat 是生产入口 |
| `admin_proactive_test_routes.py` import main 全局 | 依赖注入模式 |

---

## 附录 A: v2.1 开工前小修订

> 基于 v2 审查意见"方向通过，补充以下 4 点后再开工"。
> 本附录覆盖必须修改的 4 点 + 3 点顺手补充。
> 不作为独立文档，嵌入原计划对应章节的修正。

---

### A.1 LLM trace 拦截：request-scoped trace override（必须修改）

**问题**: v2 计划中 dry-run 通过 `try/finally` 临时替换 `llm_gateway._on_trace_callbacks` 拦截 trace。这是全局共享对象，dry-run 与普通聊天 / real-send 并发时会污染 trace。

**修正方案**: request-scoped trace override。

**实现方式**:

`LLMGateway.complete()` 新增可选参数 `trace_handler: Callable | None = None`:

```python
# agent/llm_gateway.py (小改动)
async def complete(self, request: LLMRequest, *,
                   trace_handler: Callable | None = None) -> LLMResponse:
    response = await self.adapter.complete(request)
    if trace_handler is not None:
        # 局部 trace handler，不影响全局 _on_trace_callbacks
        await trace_handler(self._build_trace(request, response))
    else:
        # 走全局 trace callbacks
        for cb in self._on_trace_callbacks:
            await cb(self._build_trace(request, response))
    return response
```

ChatAgent 侧透传:

`ChatAgent._do_call_llm()` 从 turn scratchpad 读取可选的 `_debug_trace_handler`，传给 gateway:

```python
# agent/chat_agent.py (小改动)
_trace_handler = getattr(turn, "_debug_trace_handler", None)
response = await self.llm_gateway.complete(request, trace_handler=_trace_handler)
```

Dry-run 侧设置:

```python
# agent/proactive_debug.py
turn._debug_trace_handler = create_dry_run_trace_handler(run_id)
```

`create_dry_run_trace_handler(run_id)` 返回一个 async callable，将 trace 写入 `proactive_debug_runs.llm_steps_json`，标记 `dry_run_id=run_id`，**不**写入全局 `llm_traces` 表。

**优于原有方案**: 不修改全局状态，并发安全；正常路径的 trace handler 为 None，走原有全局逻辑，零性能开销。

---

### A.2 模拟 sender 契约验证（必须修改）

**问题**: v2 的 `m["send_status"] = "delivered"` 伪代码需要对照真实契约确认。

**已审查的代码**:

1. `WeChatSender` 类型定义 (`chat_agent.py:56`):
   ```python
   WeChatSender = Callable[[list[dict[str, Any]]], Coroutine[Any, Any, list[dict[str, Any]]]]
   ```
   输入 `list[dict]`，输出 `list[dict]`。单参数，无 user_id/context_token。

2. 真实 `wechat_sender` (`main.py:426`):
   ```python
   async def wechat_sender(user_id: str, context_token: str, messages: list[dict]) -> list[dict]:
   ```
   三参数版本被 `_bound_sender` 包装为单参数后注入 ChatAgent。

3. ChatAgent 调用点 (`chat_agent.py:1291`):
   ```python
   send_batch = [{"text": guarded_text, "delay": 0.0, "_vm_index": 0}]
   raw_results = await wechat_sender(send_batch)
   ```

4. 结果读取 (`chat_agent.py:1309`):
   ```python
   send_status = raw_results[0].get("send_status", "unknown")
   # "delivered" → success path (sent_message_count += 1)
   # "failed" / "rate_limited" / "blocked" / "unknown" → failure path
   ```

**结论**: v2 伪代码 `m["send_status"] = "delivered"` **直接正确**，因为:
- 真实 wechat_sender 就是在原 dict 上设置 `send_status` 后返回
- `"delivered"` 是唯一触发成功路径的值
- 模拟 sender 不需要导入 bridge 模块

**修正后的模拟 sender 实现**:

```python
def create_simulated_sender() -> WeChatSender:
    """模拟 sender：不调用微信，对 ChatAgent 返回 delivered。"""
    async def _sender(messages: list[dict]) -> list[dict]:
        for m in messages:
            m["send_status"] = "delivered"
        return messages
    return _sender
```

**补充验证**:
- 模拟 sender 需确保 `messages` 不为空且为 list
- 模拟 sender 不调用 `asyncio.sleep` (dry-run 不需要模拟延迟)

---

### A.3 并发安全：dry-run 复用 scheduler 锁（必须修改）

**问题**: v2 中 dry-run 直接调用 `chat_agent.chat()`，绕过 `ProactiveScheduler._lock`。如果 heartbeat 或 real-send 同时在使用 scheduler，会并发跑主动回合。

**修正方案**: 新增 `ProactiveScheduler.run_debug_turn()`，由 scheduler 获取同一把 proactive lock。

**实现**:

```python
# agent/scheduler.py (新增)
async def run_debug_turn(
    self,
    user_id: str,
    bot_id: str,
    *,
    impulse_meta: dict[str, Any],
    chat_agent_kwargs: dict[str, Any],  # system_prompt, history, wechat_sender 等
) -> dict[str, Any]:
    """Debug-only：在 scheduler 锁内执行主动回合。
    
    与 run_manual_trigger / run_heartbeat 共用同一把 _lock。
    chat_agent_kwargs 由 proactive_debug 组装（含模拟 sender 和 no-op callbacks）。
    """
    if self._lock.locked():
        return {"status": "skipped", "reason": "active_proactive_turn_running"}
    
    async with self._lock:
        turn_id = f"debug_{user_id}_{int(time.time() * 1000)}"
        self._active_proactive_turn_id = turn_id
        try:
            from agent.state_machine import SOURCE_PROACTIVE_IMPULSE
            result = await self._chat_agent.chat(
                user_message=None,
                source=SOURCE_PROACTIVE_IMPULSE,
                proactive_meta=impulse_meta,
                **chat_agent_kwargs,
            )
            return {"status": "ok", "result": result, "turn_id": turn_id}
        except Exception as e:
            logger.error("proactive_scheduler: debug turn failed: %s", e)
            return {"status": "error", "reason": str(e)[:500], "turn_id": turn_id}
        finally:
            self._active_proactive_turn_id = None
```

**`proactive_debug.py` 调用方式**:

```python
# dry-run
scheduler_result = await scheduler.run_debug_turn(
    user_id=ctx.user_id,
    bot_id=ctx.bot_id,
    impulse_meta=impulses["selected"],
    chat_agent_kwargs={
        "history": history,
        "system_prompt": system_prompt,
        "wechat_sender": create_simulated_sender(),
        "write_model_history": create_dry_run_write_history(),
        "post_turn_hooks": create_dry_run_post_turn_hooks(),
        "trigger": "proactive",
        ...
    },
)
```

```python
# real-send: 仍走 run_manual_trigger（已有锁）
scheduler_result = await scheduler.run_manual_trigger(
    user_id=ctx.user_id,
    impulse_meta=impulses["selected"],
    bot_id=ctx.bot_id,
    providers...,
)
```

**并发安全保证**:
- dry-run、real-send、heartbeat 三者共用 `ProactiveScheduler._lock`
- 同一时刻只有一个针对同一 user_id 的主动回合在执行
- `run_debug_turn()` 如果锁被占用，返回 `{"status": "skipped"}`

---

### A.4 Real-send precheck 与 execution 分离（必须修改）

**问题**: v2 的 `ProactiveDebugReport` 把 eligibility / impulse / execution 混在一起。如果 `run_debug_eligibility` 返回 eligible=true，但 `run_manual_trigger` 内部再次检查时却不通过（时序差异、状态变化），前端解释不清。

**修正方案**: `ProactiveDebugReport` 新增 precheck/execution 双层结构。

```python
@dataclass
class ProactiveDebugReport:
    # ... 原有字段 ...

    # ── v2.1: Precheck vs execution 分离 ──
    precheck_eligibility: dict | None         # run_debug_eligibility 的结果
    precheck_selected_impulse: dict | None    # run_debug_impulse_pipeline 的结果
    precheck_passed: bool                     # 预检是否全部通过
    execution_result: dict | None             # run_manual_trigger / run_debug_turn 的实际 TurnResult
    execution_skip_reason: str                # 如果 execution 被跳过，原因是什么
```

**前端展示逻辑**:

```
Precheck 面板:
  eligibility: ✅ eligible
  selected impulse: ECHO (score 0.82)

Execution 面板:
  实际结果: ✅ END_REPLIED
  发送: real_send_attempted=true, 2 delivered
  历史写入: 2 条 assistant:proactive

---
或者:

Precheck 面板:
  eligibility: ✅ eligible
  selected impulse: ECHO (score 0.82)

Execution 面板:
  实际结果: ⚠️ SKIPPED
  跳过原因: active_proactive_turn_running（heartbeat 正在执行主动回合）
  发送: 未发送
  历史写入: 未写入
```

**关键规则**:
1. `precheck_*` 是预检的快照，只展示"如果现在发，检查结果是什么"
2. `execution_result` 是实际执行的结果，以 `run_manual_trigger` / `run_debug_turn` 返回值为准
3. 当 `execution_skip_reason` 非空时，前端明确标注"预检通过但实际执行跳过"

---

### A.5 run_id 区分前缀（顺手补充）

- dry-run → `pro-dry-{YYYYMMDD}-{HHMMSS}-{6位随机}`
- real-send → `pro-real-{YYYYMMDD}-{HHMMSS}-{6位随机}`

便于前端列表快速区分模式。

---

### A.6 decision_chain IMPULSE_FILTERING.raw 固定子结构（顺手补充）

```json
{
  "stage": "impulse_filtering",
  "status": "pass",
  "reason": "3 candidates → 1 after filtering",
  "raw": {
    "dedup": {"input": 3, "output": 3, "removed": []},
    "expire": {"input": 3, "output": 2, "removed": [{"type": "ECHO", "reason": "TTL expired 4h"}]},
    "score": {"input": 2, "scored": [{"type": "PRESENCE", "score": 55}, {"type": "TOPIC_CONTINUE", "score": 70}]},
    "threshold": {"min_score": 0, "passed": true}
  }
}
```

测试可验证 `decision_chain[*].raw` 包含这 4 个子键（不验证具体值）。

---

### A.7 list_debug_runs 支持过滤（顺手补充）

`GET /admin/proactive-test/runs` 新增可选 query params:

| param | 默认值 | 说明 |
|-------|--------|------|
| `limit` | 20 | 返回条数 |
| `mode` | all | dry_run / real_send / all |
| `terminal_state` | — | 按终态过滤 |
| `send_simulated` | — | true=仅 dry-run, false=仅 real-send |
| `real_send_attempted` | — | true=仅 real-send, false=仅 dry-run |
| `impulse_type` | — | 按 impulse_type 过滤 |

SQLite 层实现: 在 `list_debug_runs()` 中动态拼接 WHERE 条件。不新增索引，查询量小 (< 1000 条)。

---

### 修正涉及的文件变更

| 文件 | 原计划 | v2.1 修正 |
|------|--------|----------|
| `agent/llm_gateway.py` | 不改 | `complete()` 新增 `trace_handler` 可选参数 |
| `agent/chat_agent.py` | 不改 | `_do_call_llm()` 从 turn 读取 `_debug_trace_handler` 传给 gateway |
| `agent/scheduler.py` | `run_debug_eligibility()` / `run_debug_impulse_pipeline()` | 新增 `run_debug_turn()` (含锁) |
| `agent/proactive_debug.py` | 直接调 `chat_agent.chat()` | 调 `scheduler.run_debug_turn()` (dry-run)；调 `scheduler.run_manual_trigger()` (real-send) |
| `agent/proactive_debug.py` | `ProactiveDebugReport` 无 precheck/execution 分离 | 新增 6 个 precheck/execution 字段 |
| `agent/proactive_debug.py` | run_id 格式 | pro-dry-* / pro-real-* 前缀 |
| `memory/store.py` | `list_debug_runs(limit, mode)` | 新增 4 个可选过滤参数 |
