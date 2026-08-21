# Goal Chat Agent 重构框架选型报告（2026-07-23）

> 状态：已完成，用于新仓库立项与技术预研的正式选型基线。
>
> 评估对象：当前 `D:\projects\goal` 工作树、现有设计归档、661 个测试函数，以及 2026-07-23 可访问的各框架官方文档。
>
> 本报告回答“哪些通用能力交给框架、哪些产品能力必须自己拥有”。它不等同于 PRD；完整产品需求见同目录 `20260723-goal-chat-agent-rebuild-prd.zh.md`。

---

## 1. 结论

### 1.1 推荐方案

新仓库首版采用以下组合：

1. **Pydantic AI 作为 Agent 内核框架**：负责模型/provider 适配、原生工具调用循环、工具参数 schema 与校验、依赖注入、消息类型、usage 限制、测试模型和 OpenTelemetry 接口。
2. **FastAPI 继续作为 HTTP 与生命周期框架**：承载微信桥接、App API、外部 Agent API、后台 API，不把渠道协议塞进 Agent 框架。
3. **Pydantic v2 作为所有边界契约**：请求、响应、事件、工具参数、配置与领域对象均使用显式类型，替代当前大量手工 `dict` 校验。
4. **SQLAlchemy 2 + Alembic 管理持久化与非破坏性迁移**：SQLite 仍是单机默认数据库；仓储接口不绑定 SQLite，为未来 PostgreSQL 留出口。
5. **APScheduler 保留为单机时间调度器**：业务任务状态先落业务表，APScheduler 只负责唤醒；不能把业务真相仅放在 scheduler job 中。
6. **OpenTelemetry 作为观测标准**：本地结构化日志为默认出口，可选接 Logfire、Langfuse、LangSmith 或其他 OTel 后端；敏感内容默认不外发。

一句话决策：**用 Pydantic AI 消灭通用 Agent 样板，但不让它拥有消息、记忆、任务和渠道的业务真相。**

### 1.2 不建议的首版方案

- 不在首版同时引入 Pydantic AI 与 LangGraph。两者都会拥有 Agent 执行循环和状态概念，叠加会形成双重生命周期、双重消息模型和双重调试面。
- 不以 CrewAI 的多 Agent Crew 作为主架构。当前产品是一个长期陪伴 Agent 加多个确定性适配器，不是角色分工式任务团队。
- 不直接用框架自带 memory 替换现有记忆系统。现有“原始事件—每日摘要—证据约束长期记忆—原子回滚”是产品行为，不是可随意替换的缓存。
- 不把提醒、主动消息和每日压缩实现成 Agent 自己“睡眠等待”。它们必须由持久化调度域驱动。
- 不把新仓库设计成只支持某一家模型。DeepSeek 是当前默认 provider，但 provider 可替换是重构硬约束。

### 1.3 何时改选 LangGraph

只有在 PoC 或后续需求明确出现以下至少一项时，才把 LangGraph 提升为主编排框架：

- 一个用户任务会运行数分钟到数小时，必须在进程崩溃后从中间节点继续；
- 高风险工具必须暂停、等待人工批准，再从原节点恢复；
- 出现多个有状态分支、并行节点或子图，普通 Agent loop 已明显难以理解；
- 需要对任意执行检查点做回放、分叉或 time-travel 调试；
- 要把“计划—执行—复核—补救”定义成稳定工作流，而不是一次聊天中的自由工具调用。

当前普通聊天 turn 最长 20 次模型调用，失败后整 turn 重试即可；这不构成首版引入图运行时的充分理由。

---

## 2. 选型前提：这个项目真正难在哪里

现有仓库不是“一个 prompt 加几个工具”。截至本次审计，它包含：

- 53 个 FastAPI 端点；
- 7 类 Agent 产品能力：消息表达、时间、天气、联网资料、长期记忆、Microsoft To Do、提醒；代码层调用项可以按框架需要拆分或合并；
- 14 张 SQLite 表；
- 微信 iLink、Android App、HTTP、同步 callback、异步 channel 五类交互路径；
- 每用户串行、跨用户并发和新消息中断旧 turn；
- `send_text_message` 作为唯一用户可见输出的特殊协议；
- 原始事件、每日压缩、文件式长期记忆和事务回滚；
- 定时提醒、主动对话、每日压缩三类后台任务；
- App 消息幂等、附件、Phone Context、外部 Agent 会话和至少一次投递；
- 管理后台、运行时配置和日志诊断。

因此框架适配度不能只看“会不会 tool calling”。本项目的核心约束如下。

| 权重 | 评估维度 | 本项目要求 |
|---:|---|---|
| 20% | 自定义回合与投递语义 | 一次 `send_text_message` 等于一次真实投递；发送后模型重新决策；普通 final text 不可直接展示 |
| 15% | Provider 与协议可替换 | 当前 DeepSeek，未来可切换；不可被单一厂商 API 锁定 |
| 15% | 类型、工具与测试 | 工具参数强校验、依赖注入、错误可回馈模型、可离线测试 |
| 15% | 历史与状态可控 | 能导入/导出消息，不强迫采用框架自己的 memory 真相 |
| 10% | 异步与 FastAPI 集成 | 支持 async、取消、超时、并发，不另起不可控服务 |
| 10% | 可恢复能力 | 至少支持外部持久层重建；更强断点恢复是加分项 |
| 5% | 可观测与评测 | 能输出稳定事件/OTel，支持模型与工具测试 |
| 10% | 简洁度与锁定风险 | 首版可在单机 Windows/Linux 部署，依赖和概念不过重 |

---

## 3. 候选框架总览

评分使用 1–5 分，乘以上述权重后换算为百分制。分数是针对本项目的工程适配度，不是框架的通用排名。

| 框架 | 回合/投递 | Provider | 类型/工具 | 状态可控 | Async | 可恢复 | 观测/评测 | 简洁/锁定 | 加权分 | 结论 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| **Pydantic AI** | 4 | 5 | 5 | 4 | 5 | 3 | 5 | 5 | **90** | 首选 |
| **LangGraph** | 5 | 4 | 4 | 5 | 5 | 5 | 5 | 3 | **89** | 强备选 |
| Microsoft Agent Framework | 4 | 4 | 4 | 4 | 4 | 4 | 4 | 3 | 78 | 观察/企业备选 |
| Google ADK | 4 | 4 | 4 | 4 | 4 | 3 | 4 | 3 | 76 | 生态备选 |
| OpenAI Agents SDK | 4 | 3 | 4 | 4 | 5 | 3 | 5 | 3 | 75 | OpenAI 主栈时再选 |
| Agno | 3 | 4 | 4 | 2 | 4 | 3 | 4 | 3 | 67 | 开箱快但边界冲突 |
| CrewAI | 2 | 4 | 3 | 3 | 3 | 3 | 4 | 2 | 57 | 不符合产品形态 |

---

## 4. 首选：Pydantic AI

### 4.1 官方能力与项目匹配

Pydantic AI 官方将自身定位为 production-grade Python Agent 框架，提供 Agent loop、工具、依赖注入、结构化输出、模型/provider 抽象、MCP、可观测、评测和图支持。官方 provider 列表明确包含 DeepSeek，并允许自定义 model。参见：

- [Pydantic AI Overview](https://pydantic.dev/docs/ai/overview/)
- [Models and Providers](https://pydantic.dev/docs/ai/models/overview/)
- [Function Tools](https://pydantic.dev/docs/ai/tools-toolsets/tools/)
- [Messages and chat history](https://pydantic.dev/docs/ai/core-concepts/message-history/)
- [Testing](https://pydantic.dev/docs/ai/guides/testing/)

它最适合本项目的原因不是“功能最多”，而是边界恰当：

- 类型注解直接生成工具 schema，减少当前 `ToolDef` 手写 schema 与实现漂移；
- `RunContext` 可注入当前用户、渠道、仓储、投递器、时钟和配置，不需要工具模块读取全局变量；
- 消息历史由调用方提供，允许产品事件存储继续做事实来源；
- 可以自定义历史处理、工具准备、hook 和 output validation；
- Pydantic 与 FastAPI 的类型体系一致，新仓库可共享请求/响应模型；
- TestModel/FunctionModel 适合把大量测试从脆弱 mock 改成确定性 Agent 行为测试；
- OpenTelemetry 能接入现有或未来观测系统，而不强制绑定某一 SaaS。

### 4.2 框架负责什么

| 能力 | 交给 Pydantic AI | 说明 |
|---|---|---|
| Provider/model 适配 | 是 | 优先验证 DeepSeek 原生路径；必要时实现薄 Model adapter |
| 模型请求/响应类型 | 是 | 不再自行维护第二套通用 LLM DTO |
| 工具 schema 与参数校验 | 是 | 用 Pydantic 模型和函数签名生成 |
| 工具循环 | 是 | 框架运行，产品 hook 记录事件与投递结果 |
| 结构化错误反馈 | 是 | 参数错误反馈模型，允许有限纠错 |
| token/请求限额 | 是 + 产品约束 | 框架统计，产品定义 20 call/10 bubble/2 protocol retry |
| 测试模型与 eval | 是 | 确定性单测 + Golden 行为集 |
| OTel instrumentation | 是 | 产品再补渠道、任务、DB span |

### 4.3 产品必须自己负责什么

| 能力 | 原因 |
|---|---|
| `send_text_message` 的投递与结束协议 | 这是用户可见语义，不是普通 function tool |
| 渠道适配与消息路由 | 微信、App、callback、channel 都是产品契约 |
| 用户身份、会话与 remote agent 映射 | 不能由 provider thread/session id 决定 |
| 事件溯源与消息投递账本 | 需要审计、压缩、重建、幂等和跨框架迁移 |
| 每日压缩和长期记忆 | 有证据校验、文件权限、原子回滚等独有规则 |
| 提醒、主动消息和任务恢复 | 是业务状态机，不能只存在于 Agent run |
| 输出清洗、隐私与授权 | 必须在模型外强制执行 |
| App API、附件、Phone Context | 与 Agent loop 无关 |

### 4.4 最大风险

Pydantic AI 的默认成功条件是得到最终输出，而本产品的成功条件是“至少一次用户可见消息已确认投递，随后模型主动结束”。两者相近但不等价。

必须通过 PoC 证明：

1. `send_text_message` 工具成功后 Agent 会继续获得工具结果并重新决策；
2. 普通 final output 能被产品拦截，不会绕过投递器直接显示；
3. 在从未成功投递时，output validator 能触发最多 2 次协议重试；
4. 已投递一条后出现异常时，结果能标为 partial failure，而不是整体回滚或重复发送；
5. asyncio cancellation 能终止正在运行的 Agent，且不会吞掉已落盘的投递事件；
6. DeepSeek 的 thinking、tool-call 流式增量、usage 和错误信息不会在框架层丢失。

PoC 未通过以上任一红线时，不直接进入全面重写。

### 4.5 Durable Execution 的判断

Pydantic AI 官方提供 Temporal、DBOS、Prefect、Restate 等 durable execution 集成，可跨进程错误或重启恢复长任务：

- [Pydantic AI Durable Execution](https://pydantic.dev/docs/ai/capabilities/durable_execution/overview/)

首版不引入这些运行时，原因是：

- 普通聊天 turn 是短事务，失败后按幂等规则重试即可；
- 提醒和主动任务已经是独立持久化状态机，不应套进 Agent durable run；
- Temporal/DBOS 等会显著增加部署与调试成本；
- 当前最需要的是业务数据非破坏性迁移，而不是单个 LLM step 断点续跑。

如果未来增加需要人工审批的长任务，再单独为该工作流引入 durable capability，不要让普通聊天也被迫进入重型运行时。

---

## 5. 强备选：LangGraph

LangGraph 是低层、长期运行、有状态 Agent 的编排运行时，核心能力是 durable execution、checkpoint、interrupt、memory 和 human-in-the-loop：

- [LangGraph Overview](https://docs.langchain.com/oss/python/langgraph/overview)
- [LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
- [LangGraph Functional API](https://docs.langchain.com/oss/python/langgraph/functional-api)

### 5.1 优势

- 能把“模型调用—工具执行—消息投递—是否继续—终态”画成显式状态图；
- checkpoint 天然适合失败恢复、暂停批准和执行回放；
- 对自定义终态和分支的控制强于高层 Agent runner；
- 可以不使用完整 LangChain，只使用 LangGraph 和自有模型/工具节点；
- 若未来要做长任务或高风险工具审批，演进路径清晰。

### 5.2 对本项目的代价

- 普通聊天循环本身并不复杂，图会增加节点、state reducer、checkpointer 和序列化概念；
- checkpoint state 与产品 `raw_events` 容易形成两个事实来源；
- 图的“恢复”不能替代提醒/主动任务的业务恢复；
- 框架不会自动解决微信投递 exactly-once、App 幂等、长期记忆证据校验；
- 若同时使用 LangChain model/tool abstraction，会扩大依赖和迁移面。

### 5.3 若选择 LangGraph，边界必须这样定

- LangGraph checkpoint 只保存**执行状态**；
- 产品事件表保存**业务事实和用户可见事实**；
- 发送消息前创建 delivery intent，发送后记录 delivery outcome；
- checkpoint 重放不得再次执行已确认成功的高副作用工具；
- 所有外部调用节点必须幂等或带幂等键；
- 不把长期记忆文件内容复制进每个 checkpoint。

---

## 6. 其他候选

### 6.1 Microsoft Agent Framework

Microsoft Agent Framework 是 AutoGen 与 Semantic Kernel 团队的后继统一框架，提供 Agent、session、middleware、provider、MCP、类型化 graph workflow、checkpoint 和 HITL，并明确提供 AutoGen/Semantic Kernel 迁移路线：

- [Microsoft Agent Framework Overview](https://learn.microsoft.com/en-us/agent-framework/overview/)
- [Migration from AutoGen](https://learn.microsoft.com/en-us/agent-framework/migration-guide/from-autogen/)

优点：

- 工作流和单 Agent 能力兼具；
- 与 Microsoft Graph/M365 生态方向一致；
- middleware、telemetry、A2A 等企业能力完整；
- Python 与 .NET 均可用。

不作为首选的原因：

- 框架仍处于快速演进阶段，当前文档也说明分布式执行仍是后续方向；
- 本项目只用 Microsoft To Do，不足以让整个 Agent 内核绑定微软生态；
- 对 DeepSeek 自定义协议和特殊投递循环仍需 PoC；
- 重构首版更需要稳定、窄边界的内核，而不是企业平台面。

适用条件：未来产品明确以 Microsoft 365、Azure、A2A 和企业工作流为核心时重新评估。

### 6.2 Google ADK

Google ADK 2.0 提供多语言 Agent、graph workflow、Session/State/Memory 服务、工具确认、MCP、A2A、评测和部署能力：

- [Google ADK](https://adk.dev/)
- [Sessions, State and Memory](https://adk.dev/sessions/)
- [MemoryService](https://adk.dev/sessions/memory/)

优点：

- Session、Event、State、Memory 的概念与本项目相似；
- A2A 与外部 Agent 接入方向契合；
- graph 与 Dev UI 完整；
- 多语言生态适合未来跨端团队。

不作为首选的原因：

- 默认路径明显围绕 Gemini/Google Cloud，虽开放但会引入不需要的平台概念；
- ADK MemoryService 是可搜索知识库，不等价于本项目的证据约束长期记忆；
- 框架 session/event 与产品事件账本也有双真相风险；
- 当前 Python 单机项目用不到多语言和云端伸缩优势。

### 6.3 OpenAI Agents SDK

OpenAI Agents SDK 提供 Agent、tools、sessions、handoffs、guardrails 和 tracing，也支持自定义 provider 或 OpenAI-compatible Chat Completions：

- [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/)
- [Models and non-OpenAI providers](https://openai.github.io/openai-agents-python/models/)
- [Running agents](https://openai.github.io/openai-agents-python/running_agents/)
- [Tracing](https://openai.github.io/openai-agents-python/tracing/)

优点：

- API 小而直接，学习成本低；
- guardrails、sessions、tracing 和 run hooks 完整；
- 异步、流式和工具调用成熟；
- 若未来主模型改用 OpenAI，兼容性最佳。

不作为首选的原因：

- 默认行为、最新特性和 tracing 都围绕 OpenAI Responses API；
- 非 OpenAI provider 常走 OpenAI-compatible Chat Completions 或 beta adapter，功能差异需要逐项验证；
- DeepSeek 当前使用 Anthropic-compatible 路径，迁移会多一层协议适配；
- 默认 tracing 可能向 OpenAI 发送数据，需显式关闭或换 processor，不符合本地隐私默认值。

### 6.4 Agno

Agno 面向开箱即用的 Agent、team、workflow、session、memory、knowledge 和 AgentOS。它能很快搭出“带记忆聊天助手”，但本项目已经有明确的消息、记忆与路由域。

不作为首选的原因：

- 使用其内建 session/memory 才能获得主要便利，但这会与本项目的事件真相和长期记忆规则冲突；
- 绕过其 memory 后，优势主要剩下 Agent loop，边界不如 Pydantic AI 清晰；
- AgentOS 会扩大部署与框架锁定面。

适用条件：做全新、无需兼容现有记忆语义的快速原型。

### 6.5 CrewAI

CrewAI 官方核心模型是 Flow 管状态与控制、Crew 组织多个角色 Agent 协作：

- [CrewAI Introduction](https://docs.crewai.com/en/introduction)
- [CrewAI Flows](https://docs.crewai.com/en/concepts/flows)

它适合研究、内容生产、企业自动化等任务型多 Agent 流程，不适合当前主产品：

- 每个运行实例只需要一个对用户负责的 Agent，其人格由可替换的 Markdown 文件提供；
- 天气、提醒、记忆、渠道是工具或服务，不应拟人化成多个 Agent；
- 多角色对话会增加 token、延迟和不可预测性；
- “Crew”不能解决消息投递、跨重启、幂等和隐私问题。

未来若增加独立的深度研究功能，可以在该功能内部评估 CrewAI，但不作为主运行时。

### 6.6 LlamaIndex

LlamaIndex 的 Agent 与 Workflow 能力成熟，但其最强优势仍是文档、索引、检索和 RAG。当前产品没有大型知识库或复杂检索管线，联网搜索也不是 RAG。

适用条件：未来出现“个人文档库、邮件库、笔记库统一检索”这一独立子系统时，在该子系统中引入，不替换主 Agent 内核。

---

## 7. 建议的新仓库技术边界

```text
clients/
  wechat / app / http / external-agent
                 |
                 v
interfaces/  FastAPI routers + typed contracts
                 |
                 v
application/ use cases
  handle_user_turn / deliver_message / schedule_task
  compress_conversation / manage_memory / route_external_agent
                 |
        +--------+--------+
        |                 |
        v                 v
agent_core/           domain/
Pydantic AI           conversation/message/delivery
model + tools         reminder/proactive/memory/session
        |                 |
        +--------+--------+
                 v
infrastructure/
provider adapters / SQLAlchemy repositories / APScheduler
iLink client / Tavily / QWeather / Graph / filesystem / OTel
```

边界规则：

1. `domain` 不 import Pydantic AI、FastAPI、SQLAlchemy 或 APScheduler。
2. Pydantic AI tool 函数只调用 application use case，不直接写数据库或全局变量。
3. HTTP response 不直接暴露框架 message/event 对象。
4. provider session/thread id 不能作为产品 conversation id。
5. 所有高副作用动作都有产品幂等键和投递账本。
6. Agent 框架升级不允许触发业务表重建。

---

## 8. 开工前 PoC

PoC 是选型门，不是示例代码。建议在新仓库 `spikes/pydantic-ai-core/` 完成，所有断言自动化。

### 8.1 必测场景

| ID | 场景 | 通过条件 |
|---|---|---|
| POC-01 | DeepSeek 单轮普通回复 | 请求成功；usage、thinking、原始错误可取 |
| POC-02 | `get_time` 后发送 | 模型调用读取工具，再调用 `send_text_message` |
| POC-03 | 连续两条可见消息 | 每次发送后有新模型 step，不是程序拆分 |
| POC-04 | 无可见消息直接 final | 最多纠错 2 次；不得把 final text 直接显示 |
| POC-05 | 工具参数非法 | Pydantic 校验错误可反馈模型，且不会执行工具 |
| POC-06 | 取消运行 | 新 seq 到达后 run 被取消；已确认发送不重复 |
| POC-07 | 20 call/10 bubble 上限 | 精确进入对应终态 |
| POC-08 | 历史导入/导出 | 原生 tool call/result 可完整序列化并重建 |
| POC-09 | provider 失败重试 | 只重试安全调用；高副作用工具不重复 |
| POC-10 | OTel 隐私 | 默认 span 不含完整用户文本、token、密钥 |
| POC-11 | FastAPI 并发 | 同用户串行、不同用户并发 |
| POC-12 | TestModel | 不访问真实网络即可验证工具序列和终态 |

### 8.2 Go / No-Go

以下任一项失败即暂缓 Pydantic AI 全面落地，转做 LangGraph 或自定义薄 loop 的比较 PoC：

- 无法稳定接入 DeepSeek 原生 tool calls；
- 无法阻止普通 final output 绕过 `send_text_message`；
- 无法安全取消；
- 无法导出完整工具事件供事件存储重建；
- 框架自动重试可能重复执行已发送消息或高副作用工具。

---

## 9. 最终决策记录

| 决策 | 结论 | 理由 |
|---|---|---|
| 首版 Agent 框架 | Pydantic AI | 最少锁定、类型强、DeepSeek 明确支持、与 FastAPI 统一、可保留产品状态主权 |
| 首版是否使用 LangGraph | 否 | 当前没有必须 checkpoint 的长工作流，避免双状态与过度设计 |
| 是否框架化长期记忆 | 否 | 记忆的准入、证据、权限和事务是产品需求 |
| 是否框架化调度 | 否 | 任务真相属于业务表，调度器只负责唤醒 |
| 是否保留 SQLite | 是 | 单机自用足够；通过仓储接口和 Alembic 保留升级路径 |
| 是否继续手写 LLM SSE parser | 原则上否 | 优先交给框架/provider adapter；只有 PoC 证明能力缺失才保留极薄适配 |
| 是否引入多 Agent | 首版否 | 当前业务是一个人格 Agent，不应为框架而拆角色 |
| 观测标准 | OpenTelemetry | 后端可替换，隐私策略可控 |
| 重写方式 | 新仓库、契约优先、分阶段双跑 | 避免在旧入口继续堆叠，先冻结行为再替换 |

---

## 10. 版本与复核要求

框架能力会快速变化。本报告的能力判断以 2026-07-23 官方文档为准。正式开发时必须：

1. 在 lockfile 中固定框架版本；
2. 保存 PoC 使用的真实 provider 请求/响应样本（脱敏后）；
3. 每次大版本升级重跑 POC-01 至 POC-12；
4. 不因官方新增 memory/session 功能就自动替换产品域实现；
5. 若产品需求转向长任务或 HITL，重新执行 LangGraph 决策门，不在现有代码中偷偷叠加。
