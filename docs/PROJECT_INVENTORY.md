# Project Inventory — 项目文件结构盘点

> 最后更新: 2026-05-31
> 基于 git status + 实际文件系统扫描 (v3.1 proactive pipeline)

---

## 一级目录总览

| 目录 | 作用 | 关键文件 | 是否仍在使用 | 备注 |
|------|------|----------|:---:|------|
| `agent/` | 核心 AI 逻辑层 | `chat_agent.py`, `state_machine.py`, `tool_registry.py`, `tools.py`, `output_guard.py`, `scheduler.py`, `llm_gateway.py` | 是 | 主业务模块 |
| `bridge/` | 微信桥接层 | `ilink_client.py`, `wechat_poller.py` | 是 | iLink API + 消息轮询 |
| `memory/` | 记忆存储层 | `store.py`, `dedup.py` | 是 | SQLite + CJK 去重 |
| `prompts/` | 提示词文件 | `persona.md`, `base_protocol.md`, `tool_policy.md` 等 | 是 | 运行时读取 |
| `static/` | 管理界面 | `admin.html`, `admin.css`, `admin.js`, `index.html` | 是 | 纯 HTML/CSS/JS |
| `tests/` | 测试套件 | `test_chat_agent.py`, `test_proactive.py` 等 | 是 | pytest, 414 passed |
| `scripts/` | 运维脚本 | `reset_runtime_data.py`, `gray_native_tools.py` | 是 | 手动运行 |
| `utils/` | 通用工具 | `json_parser.py` | 是 | JSON 安全解析 |
| `data/` | 运行时数据 | `memory.db`, `agent.log`, `runtime_settings.json` | 是 | gitignore 忽略 |
| `docs/` | 文档 | 架构/边界/盘点/流程/计划/报告 | 是 | 设计文档 |
| `11/` | 历史设计文档 | v0 ~ v1.5 状态机设计文档 | 否 | 历史快照 |

---

## 二级目录详解

### `agent/` — 核心 AI 逻辑层

```
agent/
├── __init__.py              (空)
├── chat_agent.py            ← v3.1 状态机主循环 (30+ state handler)
├── state_machine.py         ← TurnState (41 状态)、TurnScratchpad、TurnResult、VisibleMessage
├── tool_registry.py         ← ToolDef / ActionDef / TOOL_REGISTRY (含 skip_proactive)
├── tool_classifier.py       ← 工具调用分类 (FACT/ACTION/TERMINAL/STATE)
├── tools.py                 ← 工具实现 (get_time, get_weather)
├── output_guard.py          ← 输出守卫 (proactive guard, emotion profanity, batch guard)
├── scheduler.py             ← v3.1: ProactiveScheduler (heartbeat + eligibility + impulse)
├── llm_gateway.py           ← Provider-neutral LLM 网关
├── llm_types.py             ← LLMRequest / LLMResponse / LLMUsage 类型
├── context_builder.py       ← 历史压缩、系统提示词构建
├── memory_worker.py         ← 后台异步记忆提取 (features.memory=true)
├── memory_hook.py           ← 记忆检索/提取钩子 (features.memory=true)
├── temporal_context.py      ← v1.6: Temporal Context Layer (时间感知)
└── providers/
    ├── __init__.py          (空)
    ├── base.py              ← ProviderAdapter Protocol + ProviderConfig
    └── deepseek.py          ← DeepSeek OpenAI-compatible 适配器
```

| 文件 | 当前作用 | 被谁 import | 是否仍在使用 |
|------|----------|------------|:---:|
| `chat_agent.py` | 状态机主循环, 30+ state handler, proactive routing | `main.py`, `scheduler.py` | 是 |
| `state_machine.py` | 类型定义, 常量, 枚举, SOURCE_PROACTIVE_IMPULSE | `main.py`, `chat_agent.py`, `scheduler.py`, tests | 是 |
| `tool_registry.py` | 工具注册表 (含 skip_proactive) | `chat_agent.py`, `tools.py`, `tool_classifier.py`, tests | 是 |
| `tool_classifier.py` | 单工具调用分类 | `chat_agent.py` | 是 |
| `tools.py` | get_time / get_weather 实现 + execute_tool | `main.py`, tests | 是 |
| `output_guard.py` | 输出守卫 pipeline, proactive guard, emotion profanity | `main.py`, `chat_agent.py`, tests | 是 |
| `scheduler.py` | v3.1: ProactiveScheduler (主动编排) | `main.py` | 是 |
| `llm_gateway.py` | LLM 调用网关 | `main.py` | 是 |
| `llm_types.py` | LLM 类型定义 | `llm_gateway.py`, `providers/*.py`, `memory_worker.py` | 是 |
| `context_builder.py` | 历史压缩 + 系统提示词 | `main.py` | 是 |
| `temporal_context.py` | v1.6: Temporal Context Layer | `chat_agent.py` | 是 |
| `memory_worker.py` | 后台记忆提取 | `main.py` (条件导入) | 是 (features.memory=true) |
| `memory_hook.py` | 记忆钩子 | `main.py` (条件导入) | 是 (features.memory=true) |
| `providers/base.py` | ProviderAdapter Protocol | `providers/deepseek.py`, `llm_gateway.py` | 是 |
| `providers/deepseek.py` | DeepSeek 适配器 | `llm_gateway.py` | 是 |

### `bridge/` — 微信桥接层

```
bridge/
├── __init__.py     (空)
├── ilink_client.py ← iLink Bot API 客户端 (7 个 HTTP 端点)
└── wechat_poller.py ← 消息轮询 + 缓冲去抖 + 转发到 /chat
```

### `memory/` — 记忆存储层

```
memory/
├── __init__.py   (空)
├── store.py      ← SQLite 记忆存储 (WAL 模式, 20+ 表)
└── dedup.py      ← CJK 二元组 Jaccard 去重
```

### `prompts/` — 提示词文件

```
prompts/
├── persona.md              ← V2 旧人设
├── base_protocol.md        ← V2.1 基础协议
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
| `base_protocol.md` | 是 | `context_builder.py` 的 `build_chat_system_prompt` 引用 |
| `bot_identity_default.md` | 是 | bot 画像系统引用 |
| `relationship_context.md` | 否 | features.relationship=false (默认) |
| `memory_policy.md` | 否 | features.memory=false (默认) |
| `memory_extract.md` | 是 | `memory_worker.py` 引用 |
| `tool_policy.md` | 是 | `context_builder.py` 的 `build_chat_system_prompt` 引用 |
| `trigger_context.md` | 是 | `context_builder.py` 的 prompt pack 引用 |

### `tests/` — 测试套件 (414 passed, 5 skipped)

```
tests/
├── __init__.py
├── conftest.py                 ← 共享 fixture (Windows tmp_path 处理)
├── fixtures/                   ← 测试数据
│   ├── chat_input_weather.json
│   ├── memory_dedup_input.json
│   └── scheduler_scenarios.json
├── test_chat_agent.py          ← 状态机生命周期测试
├── test_proactive.py           ← v3.1: 主动消息测试 (68 tests)
├── test_output_guard.py        ← 输出守卫测试
├── test_tool_registry.py       ← 工具注册表测试
├── test_tools.py               ← 工具实现测试
├── test_message_actions.py     ← 消息动作测试
├── test_prompt_externalization.py ← 提示词外部化测试
├── test_memory.py              ← 记忆系统测试
├── test_native_tool_calls.py   ← 原生工具调用测试
├── test_llm_gateway.py         ← LLM 网关测试
├── test_chat_security.py       ← 聊天安全测试
├── test_channel_guard.py       ← 渠道守卫测试
├── test_admin_events.py        ← 管理事件测试
├── test_scheduled_store.py     ← 定时任务存储测试
└── test_wechat_bridge.py       ← 微信桥接测试
```

### `docs/` — 文档

```
docs/
├── ARCHITECTURE.md              ← 系统架构图 (Mermaid)
├── MODULE_BOUNDARIES.md         ← 模块边界分析
├── PROJECT_INVENTORY.md         ← 项目文件盘点 (本文件)
├── RUNTIME_FLOW.md              ← 运行入口和主链路分析
├── BUG_TRACKER.md               ← Bug 追踪
├── v0.2-acceptance-report.md    ← v0.2 验收报告
├── v0.2-pytest-results.txt      ← v0.2 测试结果
├── v1.2-acceptance-report.md    ← v1.2 验收报告
├── v1.2-migration-plan.md       ← v1.2 迁移计划
├── v1.3-model-router-plan.md    ← v1.3 模型路由计划
├── v1.4.3-proactive-strict-cc-plan.md ← v3.1 主动消息方案
└── v1.5-implementation-plan.md  ← v1.5 实现计划
```

---

## 新增/删除文件 (v3.1)

| 操作 | 文件 | 说明 |
|------|------|------|
| **新增** | `agent/scheduler.py` | ProactiveScheduler 主动消息调度器 |
| **新增** | `tests/test_proactive.py` | 主动消息测试 (68 tests) |
| **新增** | `docs/v1.4.3-proactive-strict-cc-plan.md` | v3.1 主动消息改动计划 |
| **删除** | `agent/turn_context.py` | 旧版 ContextBuilder, 未被使用 |
| **删除** | `agent/message_envelope.py` | 旧版 MessageEnvelope, 未被使用 |
| **删除** | `tests/test_message_envelope.py` | 对应测试, 随源文件删除 |
| **删除** | `docs/code-review-vs-target-architecture.md` | 过期文档 |
| **删除** | `docs/v0.2-landing-review.md` | 过期文档 |

---

## 根目录文件

| 文件 | 作用 | 是否仍在使用 |
|------|------|:---:|
| `main.py` | 主入口 — FastAPI + proactive endpoints + heartbeat loop | 是 |
| `config.yaml` | 所有配置 (v3.1: +proactive section) | 是 |
| `CLAUDE.md` | AI agent 指令文件 | 是 |
| `requirements.txt` | Python 依赖 | 是 |
| `.env` / `.env.example` | 环境变量 | 是 |
| `.gitignore` | Git 忽略规则 | 是 |
| `Dockerfile` / `docker-compose.yml` | Docker 部署 | 是 |
| `deploy.sh` / `run.bat` | 部署/启动脚本 | 是 |
| `pytest.ini` | pytest 配置 | 是 |

---

## 疑似历史遗留

| 路径 | 状态 | 证据 |
|------|------|------|
| `11/` | 历史快照 | 各版本设计文档, 目录名无语义 |
| `agent/dialogue_state/` | 空目录 | 仅 `__pycache__`, 无 `.py` 文件 |
| `agent/emotion/` | 空目录 | 仅 `__pycache__`, 无 `.py` 文件 |
| `data/codex_server_*.log` | 空文件 | codex_server 未在代码中出现 |
| `data/uvicorn-smoke.*.log` | 空文件 | 手动测试残留 |
| `utils/json_parser.py` | 不确定 | 未发现直接 import |
