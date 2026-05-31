# Project Inventory — 项目文件结构盘点

> 生成日期: 2026-05-31
> 基于 `git status` + 实际文件系统扫描

---

## 一级目录总览

| 目录 | 作用 | 关键文件 | 是否仍在使用 | 备注 |
|------|------|----------|:---:|------|
| `agent/` | 核心 AI 逻辑层 | `chat_agent.py`, `state_machine.py`, `tool_registry.py`, `tools.py`, `output_guard.py`, `llm_gateway.py`, `llm_types.py` | 是 | 主业务模块 |
| `bridge/` | 微信桥接层 | `ilink_client.py`, `wechat_poller.py` | 是 | iLink API + 消息轮询 |
| `memory/` | 记忆存储层 | `store.py`, `dedup.py` | 是 | SQLite + CJK 去重 |
| `prompts/` | 提示词文件 | `persona.md`, `base_protocol.md`, `tool_policy.md` 等 | 是 | 运行时读取 |
| `static/` | 管理界面 | `admin.html`, `admin.css`, `admin.js`, `index.html` | 是 | 纯 HTML/CSS/JS |
| `tests/` | 测试套件 | `test_chat_agent.py`, `test_output_guard.py` 等 | 是 | pytest |
| `scripts/` | 运维脚本 | `reset_runtime_data.py`, `gray_native_tools.py` | 是 | 手动运行 |
| `utils/` | 通用工具 | `json_parser.py` | 是 | JSON 安全解析 |
| `data/` | 运行时数据 | `memory.db`, `agent.log`, `runtime_settings.json` | 是 | gitignore 忽略 |
| `docs/` | 文档 | 各版本计划和报告 | 是 | 设计文档 |
| `11/` | 历史设计文档 | v0 ~ v1.5 状态机设计文档 | **否** | 【疑似历史遗留】 |
| `D:projectsgoalagentdialogue_state/` | 空目录 | 无 | **否** | 【疑似遗留/误创建】 |
| `D:projectsgoalagentemotion/` | 空目录 | 无 | **否** | 【疑似遗留/误创建】 |

---

## 二级目录详解

### `agent/` — 核心 AI 逻辑层

```
agent/
├── __init__.py              (空)
├── chat_agent.py            ← v1.5 状态机主循环
├── state_machine.py         ← TurnState 枚举、TurnScratchpad、TurnResult、VisibleMessage
├── tool_registry.py         ← ToolDef / ActionDef / TOOL_REGISTRY 注册
├── tool_classifier.py       ← 工具调用分类 (FACT/ACTION/TERMINAL/STATE)
├── tools.py                 ← 工具实现 (get_time, get_weather)
├── output_guard.py          ← 输出守卫 (协议泄露检测、敏感信息过滤、batch guard)
├── llm_gateway.py           ← Provider-neutral LLM 网关
├── llm_types.py             ← LLMRequest / LLMResponse / LLMUsage 类型
├── context_builder.py       ← Token 估算、历史压缩、系统提示词构建
├── turn_context.py          ← ContextBuilder (v0.2 Phase 3, 扩展点预留)
├── message_envelope.py      ← MessageEnvelope 内部消息结构 (兼容层)
├── memory_worker.py         ← 后台异步记忆提取
├── memory_hook.py           ← 记忆检索/提取钩子 (bridges ContextBuilder ↔ MemoryStore)
└── providers/
    ├── __init__.py          (空)
    ├── base.py              ← ProviderAdapter Protocol + ProviderConfig
    └── deepseek.py          ← DeepSeek OpenAI-compatible 适配器
```

| 文件 | 当前作用 | 被谁 import | 是否仍在使用 |
|------|----------|------------|:---:|
| `chat_agent.py` | 状态机主循环, 23+ state handler | `main.py` | 是 |
| `state_machine.py` | 类型定义, 常量, 枚举 | `main.py`, `chat_agent.py`, tests | 是 |
| `tool_registry.py` | 工具注册表 | `chat_agent.py`, `tools.py`, `tool_classifier.py`, tests | 是 |
| `tool_classifier.py` | 单工具调用分类 | `chat_agent.py` | 是 |
| `tools.py` | get_time / get_weather 实现 + execute_tool | `main.py`, tests | 是 |
| `output_guard.py` | 输出守卫 pipeline | `main.py`, `chat_agent.py`, tests | 是 |
| `llm_gateway.py` | LLM 调用网关 | `main.py` | 是 |
| `llm_types.py` | LLM 类型定义 | `llm_gateway.py`, `providers/*.py`, `memory_worker.py` | 是 |
| `context_builder.py` | 历史压缩 + 系统提示词 | `main.py` | 是 |
| `turn_context.py` | ContextBuilder (扩展点预留) | 【不确定】未发现直接 import | 【不确定】可能未使用 |
| `message_envelope.py` | MessageEnvelope 内部结构 | 【不确定】未发现直接 import | 【不确定】可能未使用 |
| `memory_worker.py` | 后台记忆提取 | `main.py` (条件导入) | 是 (features.memory=true 时) |
| `memory_hook.py` | 记忆钩子 | `main.py` (条件导入) | 是 (features.memory=true 时) |
| `providers/base.py` | ProviderAdapter Protocol | `providers/deepseek.py`, `llm_gateway.py` | 是 |
| `providers/deepseek.py` | DeepSeek 适配器 | `llm_gateway.py` | 是 |

### `agent/dialogue_state/` — 空目录 (仅 `__pycache__`)

- 无 `.py` 源文件
- 仅存在 `__pycache__` (编译缓存)
- **疑似历史遗留**: 可能是旧版 dialogue_state 模块的残留
- 【不确定】是否有其他代码引用此包

### `agent/emotion/` — 空目录 (仅 `__pycache__`)

- 无 `.py` 源文件
- 仅存在 `__pycache__`
- **疑似历史遗留**: 可能是旧版 emotion 模块的残留
- 【不确定】是否有其他代码引用此包

### `bridge/` — 微信桥接层

```
bridge/
├── __init__.py     (空)
├── ilink_client.py ← iLink Bot API 客户端 (7 个 HTTP 端点)
└── wechat_poller.py ← 消息轮询 + 缓冲去抖 + 转发到 /chat
```

| 文件 | 当前作用 | 被谁 import | 是否仍在使用 |
|------|----------|------------|:---:|
| `ilink_client.py` | iLink API 调用 (登录、发消息、轮询) | `wechat_poller.py`, `main.py` (延迟导入) | 是 |
| `wechat_poller.py` | WeChatBridge 类, 长轮询 + 转发 | `main.py` | 是 |

### `memory/` — 记忆存储层

```
memory/
├── __init__.py   (空)
├── store.py      ← SQLite 记忆存储 (WAL 模式)
└── dedup.py      ← CJK 二元组 Jaccard 去重
```

| 文件 | 当前作用 | 被谁 import | 是否仍在使用 |
|------|----------|------------|:---:|
| `store.py` | MemoryStore: 对话历史、记忆、turns、outbound 等 | `main.py`, `memory_worker.py`, `memory_hook.py` | 是 |
| `dedup.py` | Jaccard 相似度、去重、重要度计算 | `store.py`, `memory_worker.py` | 是 |

### `prompts/` — 提示词文件

```
prompts/
├── persona.md              ← V2 旧人设 (default_bot 用 bot_identity_default.md)
├── base_protocol.md        ← V2.1 基础协议: 输出格式、工具调用规则、安全规则
├── bot_identity_default.md ← V2.1 default_bot 人设
├── relationship_context.md ← V2.1 关系上下文模板
├── memory_policy.md        ← V2.1 记忆查询策略
├── memory_extract.md       ← 记忆提取提示词
├── tool_policy.md          ← V2.1 工具约束
└── trigger_context.md      ← V2.1 触发源模板
```

| 文件 | 是否仍在使用 | 证据 |
|------|:---:|------|
| `persona.md` | 是 | `main.py` 读取 (`read_persona_file()`, `_read_persona()`) |
| `base_protocol.md` | 【不确定】 | `context_builder.py` 的 `build_chat_system_prompt` 可能引用 |
| `bot_identity_default.md` | 【不确定】 | CLAUDE.md 提及但未发现直接 import |
| `relationship_context.md` | 【不确定】 | features.relationship=false, 可能未启用 |
| `memory_policy.md` | 【不确定】 | features.memory=false (默认), 可能未启用 |
| `memory_extract.md` | 是 | `memory_worker.py` 引用 (`PROMPTS_DIR`) |
| `tool_policy.md` | 【不确定】 | CLAUDE.md 提及但未发现直接 import |
| `trigger_context.md` | 【不确定】 | CLAUDE.md 提及但未发现直接 import |

### `static/` — 管理界面

```
static/
├── admin.html   ← 管理面板主页面
├── admin.css    ← 管理面板样式
├── admin.js     ← 管理面板逻辑
└── index.html   ← 落地页
```

| 文件 | 是否仍在使用 | 证据 |
|------|:---:|------|
| `admin.html` | 是 | `main.py` 的 `/admin` 路由读取 |
| `admin.css` | 是 | `admin.html` 引用 |
| `admin.js` | 是 | `admin.html` 引用 |
| `index.html` | 是 | `main.py` 的 `/` 路由读取 |

### `tests/` — 测试套件

```
tests/
├── __init__.py
├── conftest.py                 ← 共享 fixture (Windows tmp_path 处理)
├── fixtures/                   ← 测试数据
│   ├── chat_input_weather.json
│   ├── memory_dedup_input.json
│   └── scheduler_scenarios.json
├── test_chat_agent.py          ← 状态机生命周期测试
├── test_output_guard.py        ← 输出守卫测试
├── test_tool_registry.py       ← 工具注册表测试
├── test_tools.py               ← 工具实现测试
├── test_message_actions.py     ← 消息动作测试
├── test_prompt_externalization.py ← 提示词外部化测试
├── test_memory.py              ← 记忆系统测试
├── test_native_tool_calls.py   ← 原生工具调用测试
├── test_message_envelope.py    ← MessageEnvelope 测试
├── test_llm_gateway.py         ← LLM 网关测试
├── test_chat_security.py       ← 聊天安全测试
├── test_channel_guard.py       ← 渠道守卫测试
├── test_admin_events.py        ← 管理事件测试
├── test_scheduled_store.py     ← 定时任务存储测试
└── test_wechat_bridge.py       ← 微信桥接测试
```

### `scripts/` — 运维脚本

```
scripts/
├── reset_runtime_data.py   ← 重置运行时数据 (保留人设/配置)
└── gray_native_tools.py    ← Native Tool Calls 灰度验证脚本
```

| 文件 | 是否仍在使用 | 证据 |
|------|:---:|------|
| `reset_runtime_data.py` | 是 | 独立脚本, 手动运行 |
| `gray_native_tools.py` | 是 | 独立脚本, 手动运行 |

### `utils/` — 通用工具

```
utils/
├── __init__.py      (空)
└── json_parser.py   ← 安全 JSON 解析 (多层回退)
```

| 文件 | 是否仍在使用 | 证据 |
|------|:---:|------|
| `json_parser.py` | 【不确定】 | 未发现直接 import (可能是旧版遗留) |

### `data/` — 运行时数据 (gitignore 忽略)

```
data/
├── memory.db           ← SQLite 数据库 (主数据)
├── memory.db-shm       ← SQLite WAL 共享内存
├── memory.db-wal       ← SQLite WAL 日志
├── agent.log           ← 应用日志 (~1MB)
├── runtime_settings.json ← 运行时设置持久化
├── backups/            ← 数据库备份目录
├── codex_server_stderr.log  ← 空文件 (历史遗留?)
├── codex_server_stdout.log  ← 空文件 (历史遗留?)
├── uvicorn-smoke.err.log    ← 空文件 (历史遗留?)
└── uvicorn-smoke.out.log    ← 空文件 (历史遗留?)
```

### `docs/` — 文档

```
docs/
├── BUG_TRACKER.md              ← Bug 追踪
├── v0.2-acceptance-report.md   ← v0.2 验收报告
├── v0.2-pytest-results.txt     ← v0.2 测试结果
├── v1.2-acceptance-report.md   ← v1.2 验收报告
├── v1.2-migration-plan.md      ← v1.2 迁移计划
├── v1.3-model-router-plan.md   ← v1.3 模型路由计划
└── v1.5-implementation-plan.md ← v1.5 实现计划
```

### `11/` — 历史设计文档

```
11/
├── v0/     ← goal_chat_agent_design_principles.md, goal_chat_agent_level0_state_machine.md
├── v0.2/   ← v0.2 版本设计文档
├── v1/     ← v1 版本设计文档
├── v1.2/   ← v1.2 版本设计文档
├── v1.3/   ← v1.3 model_router 设计文档
├── v1.4/   ← v1.4 proactive_impulse 设计文档
└── v1.5/   ← v1.5 causal chain 设计文档
```

- **疑似历史遗留**: 目录名 `11` 无语义, 内容为各版本设计文档
- 可能是从其他位置复制过来的历史快照
- 【不确定】是否有代码引用此目录

### `D:projectsgoalagentdialogue_state/` — 疑似遗留

- **空目录** (无任何文件, 仅 `.` 和 `..`)
- 路径名包含 Windows 路径分隔符被截断 (`D:projects` 而非 `D:\projects`)
- **高度疑似误创建**: 可能是某个脚本或工具在错误的工作目录下创建的
- 无任何代码引用

### `D:projectsgoalagentemotion/` — 疑似遗留

- **空目录** (无任何文件)
- 同上, 路径名异常
- **高度疑似误创建**
- 无任何代码引用

---

## 根目录文件

| 文件 | 作用 | 是否仍在使用 |
|------|------|:---:|
| `main.py` | **主入口** — FastAPI 所有路由 + interrupt_checker + post-terminal callbacks | 是 |
| `config.yaml` | 所有配置 (支持 `${ENV_VAR}` 替换) | 是 |
| `requirements.txt` | Python 依赖 | 是 |
| `CLAUDE.md` | AI agent 指令文件 | 是 |
| `.env` | 环境变量 (gitignore) | 是 |
| `.env.example` | 环境变量示例 | 是 |
| `.gitignore` | Git 忽略规则 | 是 |
| `.dockerignore` | Docker 忽略规则 | 是 |
| `Dockerfile` | Docker 构建文件 | 是 |
| `docker-compose.yml` | Docker Compose 配置 | 是 |
| `deploy.sh` | 部署脚本 | 是 |
| `run.bat` | Windows 启动脚本 | 是 |
| `pytest.ini` | pytest 配置 | 是 |

---

## 疑似历史遗留总结

| 路径 | 状态 | 证据 |
|------|------|------|
| `11/` | 疑似遗留 | 目录名无语义, 内容为历史设计文档快照 |
| `agent/dialogue_state/` | 疑似遗留 | 空目录, 仅 `__pycache__`, 无 `.py` 文件 |
| `agent/emotion/` | 疑似遗留 | 空目录, 仅 `__pycache__`, 无 `.py` 文件 |
| `D:projectsgoalagentdialogue_state/` | 疑似误创建 | 空目录, 路径名异常 |
| `D:projectsgoalagentemotion/` | 疑似误创建 | 空目录, 路径名异常 |
| `data/codex_server_*.log` | 疑似遗留 | 空文件, codex_server 未在代码中出现 |
| `data/uvicorn-smoke.*.log` | 疑似遗留 | 空文件, 可能是手动测试残留 |
| `utils/json_parser.py` | 【不确定】 | 未发现直接 import |
| `agent/turn_context.py` | 【不确定】 | 未发现直接 import |
| `agent/message_envelope.py` | 【不确定】 | 未发现直接 import |

## 疑似重复/冲突目录

| 路径 | 冲突说明 |
|------|----------|
| `D:projectsgoalagentdialogue_state/` vs `agent/dialogue_state/` | 两个路径都指向 dialogue_state, 一个在项目内, 一个在项目外 (路径异常) |
| `D:projectsgoalagentemotion/` vs `agent/emotion/` | 同上, 两个路径都指向 emotion |

## CLAUDE.md 提及但实际不存在的文件

| CLAUDE.md 中的路径 | 实际状态 |
|--------------------|----------|
| `agent/bot_profile.py` | **不存在** — BotProfile / BotProfileManager 未实现 |
| `agent/scheduler.py` | **不存在** — 主动消息调度未实现 |
| `prompts/bot_identity_default.md` | **存在** — 但未发现直接 import |
| `prompts/relationship_context.md` | **存在** — 但 features.relationship=false |
| `prompts/memory_policy.md` | **存在** — 但 features.memory=false (默认) |
| `prompts/trigger_context.md` | **存在** — 但未发现直接 import |
