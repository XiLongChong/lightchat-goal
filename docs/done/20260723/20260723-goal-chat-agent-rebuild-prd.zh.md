# Goal Chat Agent 新仓库重建产品需求文档（PRD）（2026-07-23）

> 文档状态：已完成的重建基线 v1.0，可用于新仓库需求拆分、架构设计、测试设计和验收。
>
> 需求来源：`D:\projects\goal` 当前工作树、现有设计/完成记录、管理后台、配置、数据模型、661 个测试函数及 2026-07-21 旧版 PRD。
>
> 目标：在新仓库从零开发一个功能不退化、数据可迁移、边界更清晰的 Goal Chat Agent。本文描述产品行为与验收，不要求复制旧仓库类名、目录或表结构。

---

## 0. 文档使用说明

### 0.1 需求等级

- **P0-Parity**：替代旧系统前必须具备的现有能力。
- **P0-Correction**：现有实现与产品目标明显冲突，重建时必须纠正；不能把旧缺陷固化成兼容行为。
- **P1**：首个稳定版后应完成的重要增强。
- **P2**：明确保留方向，当前不阻塞替代。

### 0.2 规范词

- “必须/不得”：验收硬条件。
- “应该”：默认实现，偏离需留下决策记录。
- “可以”：实现自由。
- “兼容”：对外可观察行为或数据必须保留；不代表复制内部实现。

### 0.3 需求编号

- `PRD-G`：全局产品需求
- `PRD-C`：对话与 Agent
- `PRD-T`：工具
- `PRD-M`：记忆与上下文
- `PRD-S`：调度、提醒与主动消息
- `PRD-W`：微信
- `PRD-A`：AsPocket App
- `PRD-X`：外部 Agent
- `PRD-O`：管理后台与运维
- `PRD-D`：数据
- `NFR`：非功能需求
- `AC`：系统级验收场景

---

## 1. 产品定义

### 1.1 产品定位

Goal Chat Agent 是一个可长期运行的个人聊天型智能 Agent。人格通过md文档设定，通过日常聊天维持连续关系，同时能够查询信息、维护长期记忆、管理待办与提醒，并在受约束的时间窗口主动联系用户。

它不是一次性问答 Bot，也不是通用“多 Agent 平台”。产品的核心是：

1. 同一个人格在多次、跨天、跨重启的互动中保持连续；
2. 模型通过真实工具完成动作，而不是只描述“已经完成”；
3. 用户看到的每条消息都对应一次真实投递；
4. 微信、Android App、HTTP 和外部 Agent 共用稳定契约；
5. 运维者能在本地完成部署、诊断、数据和隐私管理。

### 1.2 核心价值

| 价值 | 产品表现 |
|---|---|
| 连续陪伴 | 记得近期对话、跨天摘要和经用户确认的稳定信息 |
| 自然表达 | 人格可配置，短句、口语化，能判断情绪，不像客服 |
| 真实行动 | 天气、搜索、待办、提醒等由工具产生可验证结果 |
| 主动但不打扰 | 有安静时段、冷却、配额和用户可控频率 |
| 多端一致 | 同一身份在支持的入口获得一致能力与消息语义 |
| 可维护 | 配置、日志、任务、记忆、渠道和依赖状态可管理 |

### 1.3 目标用户与角色

#### U1：主要聊天用户

- 通过微信或 AsPocket 与「栖」交流；
- 希望对话自然、记忆连续、提醒可靠；
- 不需要理解 Agent、tool call、token 或运行时概念；
- 对误记、泄露、重复发送和打扰高度敏感。

#### U2：本地部署者/管理员

- 通常与主要聊天用户为同一人；
- 配置密钥和 provider、扫码绑定微信、查看运行状态；
- 管理人格、记忆、提醒、附件和外部 Agent 会话；
- 需要明确知道操作是否成功、失败原因和恢复方法。

#### U3：AsPocket 客户端

- 通过稳定 REST 契约上传消息、附件和 Phone Context；
- 按 `remoteAgentId` 隔离联系人历史；
- 使用 `clientMessageId` 重试而不产生重复消息。

#### U4：外部 Agent 进程

- 以临时联系人/会话身份注册；
- 通过 callback 或 channel 接收 App 消息并回传回复；
- 自己管理进程与工作目录，Router 不替它启动进程。

### 1.4 主要用户旅程

#### Journey A：首次部署

管理员启动服务 → 检查 provider 与依赖 → 修改默认管理员密码 → 扫微信二维码 → 手机向 Bot 发首条消息 → 系统记录可发送联系人 → 后台显示“接收与发送均就绪”。

#### Journey B：日常聊天

用户连续发送若干短消息 → 微信侧聚合 → 系统创建 turn → 构建历史、时间和记忆上下文 → Agent 查询工具或直接发消息 → 每次真实投递后重新判断 → turn 结束 → 原始事件可重建。

#### Journey C：创建提醒

用户自然语言提出时间和事项 → Agent 调用提醒工具 → 系统验证时间、时区和边界 → 任务持久化 → 到期唤醒 → 通过可发送渠道投递 → 记录明确终态 → 重启不得丢失。

#### Journey D：跨天连续

当天对话积累 → 每日压缩窗口触发 → 生成摘要并审查长期记忆候选 → 文件与数据库事务成功 → 活跃历史替换为摘要 → 次日仍能接续关键事实。

#### Journey E：App 与外部 Agent

外部 Agent 注册并心跳 → App 获得联系人邀请 → 用户选择该联系人发消息 → Router 按 `remoteAgentId` 路由 → 外部 Agent 回复 → App 历史中按原 turn 和顺序展示。

---

## 2. 产品目标、成功指标与非目标

### 2.1 重建目标

- **G1 功能等价**：所有 P0-Parity 场景有自动化验收，外部契约不无意破坏。
- **G2 状态可靠**：对话、记忆、提醒、App 历史和待交付消息在定义的故障场景中可恢复。
- **G3 边界清晰**：渠道、Agent、领域、持久化和第三方 provider 可单独替换。
- **G4 数据不破坏**：schema 升级为增量迁移；不再因版本号变化删除业务表。
- **G5 可测试**：时间、模型、外部 API、投递器均可注入替身；测试不依赖固定历史日期。
- **G6 可观测**：从一次入口请求能关联到 turn、模型调用、工具、投递和任务。

### 2.2 替代成功指标

| 指标 | 目标 |
|---|---|
| P0 需求通过率 | 100% |
| 安全/隐私红线通过率 | 100% |
| Golden 契约场景通过率 | 100% |
| 同一幂等键重复请求产生重复用户消息 | 0 |
| 已确认投递因重试被重复发送 | 0 |
| 跨重启对话/提醒恢复场景 | 100% |
| 非破坏性迁移验证 | 100% |
| 关键链路 trace 关联完整率 | 100% |
| 固定日期导致的时间敏感测试失败 | 0 |

LLM 回复不要求逐字与旧版一致。人格、意图、工具结果、消息条数边界和安全规则必须一致。

### 2.3 首版非目标

- 面向公众的 SaaS、多租户计费、组织与 RBAC；
- 多副本水平扩展和跨机分布式锁；
- Router 启动/停止外部 Agent 进程；
- 通用插件市场；
- 自主修改代码或执行任意系统命令；
- 语音输入、TTS、音频附件；
- 视频、多附件、任意文件协议；
- 未经显式同意收集高敏 Phone Context；
- 为展示“Agent 感”而拆成多个角色 Agent。

这些非目标不影响通过接口与仓储抽象为未来演进留口。

---

## 3. 领域词汇与统一模型

| 术语 | 定义 |
|---|---|
| User | 产品用户身份，不等同于渠道帐号或 provider thread |
| Channel | 微信、App、HTTP 等入站/出站媒介 |
| Conversation | `user + agent identity` 下的连续对话 |
| Remote Agent | App 中可选择的目标联系人，使用稳定 `remoteAgentId` |
| Turn | 一次用户输入或后台触发，直到明确终态 |
| Model Call/Step | Turn 内一次 LLM 请求与响应 |
| Tool Call | 模型提出的一次结构化工具调用 |
| Visible Message | 可展示给用户的消息内容 |
| Delivery | 一次向真实渠道发送 Visible Message 的尝试及结果 |
| Raw Event | Turn 中按序发生、可重放的事实事件 |
| Summary | 压缩历史后保留的跨日会话信息 |
| Long-term Memory | 经准入规则持久化的稳定用户事实 |
| Scheduled Task | 提醒、主动检查或压缩的持久化触发任务 |
| App Session | 外部 Agent 注册到 Router 的临时在线会话 |
| Attachment | 消息关联的 image/sticker 等版本化 union |
| Phone Context | App 主动上传、低敏、带时效的设备/应用状态快照 |

### 3.1 身份规则

- `user_id` 是产品身份；渠道原始 ID 必须通过 Identity Mapping 映射。
- 微信用户、App 用户不得因为入口不同而意外共享或隔离历史。
- 新版默认支持一个主要个人用户，但内部模型必须按 user 隔离，不允许全局单例切换当前 user。
- `remoteAgentId` 隔离 App 联系人历史；它不能替代 user_id。
- `conversation_id`、`turn_id`、`message_id`、`delivery_id` 必须是不同实体。

---

## 4. 版本范围与行为纠正

### 4.1 P0-Parity 范围

- 中文人格聊天、原生工具循环、输出清洗；
- 14 个现有工具的产品能力；
- 原始事件、重启重建、每日压缩、长期记忆；
- 提醒、主动消息、压缩调度；
- 微信 iLink 收发与扫码部署；
- App profile、消息、历史、邀请、图片、贴纸、Phone Context；
- external callback 与 channel 会话；
- 管理后台现有功能；
- Docker/本地启动、环境配置和日志。

### 4.2 P0-Correction

以下是重建目标，不以旧实现为兼容标准：

1. **App 内置 Agent 必须有连续上下文**。旧路径用固定 App user 且每次传空历史；新版必须按真实用户和 conversation 重建。
2. **Conversation Context 必须按会话隔离**。不得使用一个全局消息数组在不同用户间切换。
3. **工具暴露必须真正执行策略**。禁用或缺少依赖的工具不得继续全部暴露；`schedule_next_proactive` 仅主动 turn 可见。
4. **提醒只保留一个事实模型**。旧代码同时存在 `reminders` 和 `scheduled_tasks`；新版统一为业务任务及投递记录。
5. **schema 迁移不得重置业务数据**。旧版 schema id 不匹配即删表的行为禁止。
6. **配置优先级必须真实一致**。秘密只从安全来源读取；文档、代码、后台展示不得互相矛盾。
7. **channel pending message 必须可恢复**。旧版纯内存队列的至少一次语义保留，但 P0 新版默认持久化。
8. **管理员会话必须跨进程安全且有过期机制**，不得依赖无界内存 set；默认密码不得作为可接受生产状态。
9. **App 幂等返回 schema 必须一致**。首次与重复请求都必须返回附件和同样字段。
10. **主动频率必须单一来源**。后台显示的限额必须与实际调度配置一致。
11. **健康检查必须区分存活与就绪**。不能只返回 model 名称。
12. **时间敏感代码必须使用可注入 Clock**。测试不得写死已经过期的未来日期。

### 4.3 P1/P2

- P1：FCM/实时推送、通用 persistent channel、多模型路由、记忆冲突/过期管理、管理员操作审计、PostgreSQL 运行模式。
- P2：语音、音频、多附件、高级多模态、面向普通用户的独立 Web 产品界面、分布式多副本。

---

## 5. 全局业务规则

### PRD-G-001：统一入口流程（P0）

所有用户消息必须经过：

鉴权 → schema 校验 → 身份解析 → 幂等/去重 → 入站记录 → 用户/会话串行控制 → 上下文构建 → Agent run/外部路由 → 投递 → 事件与结果提交。

任何入口不得绕过输出清洗、权限或幂等账本直接发送。

### PRD-G-002：同用户串行、跨用户并发（P0）

- 同一 conversation 同时最多一个 active turn。
- 不同 user/conversation 可并发。
- 锁注册表有容量控制和安全淘汰；不得淘汰仍被持有或等待的锁。
- 进程内默认实现可以是 asyncio lock；多副本时替换为分布式协调，不改变 application API。

### PRD-G-003：新消息中断（P0）

- 每个入站 channel/user 维护单调递增 seq。
- turn 记录其输入边界：消息 ID、first seq、latest seq、cutoff time。
- 新 seq 大于 active turn latest seq 时触发取消。
- 尚未发送：终态 `superseded_before_send` 或 `cancelled`。
- 已发送部分：终态 `interrupted_after_partial_delivery`；已发送内容不得撤销或重发。
- 被取消 turn 的普通 assistant text 不得进入后续用户可见历史。

### PRD-G-004：幂等与副作用（P0）

- 用户消息、回调、channel 入队、提醒触发、消息投递分别有幂等键。
- 只读工具可按重试策略重试。
- 高副作用工具在结果不确定时不得自动重放，必须进入 `unknown_after_attempt` 或人工可诊断状态。
- “HTTP 超时”不等于“对端未执行”；投递必须区分失败前、已确认失败、结果未知、已确认成功。

### PRD-G-005：配置优先级（P0-Correction）

1. 命令行临时覆盖；
2. 环境变量和 secret store；
3. 持久化运行时设置；
4. 配置文件；
5. 代码安全默认值。

秘密字段不得写入运行时设置 JSON、管理 API response 或日志。后台修改只允许白名单字段，并在保存前做类型、范围与交叉校验。

---

## 6. 对话与 Agent 需求

### 6.1 Turn 生命周期

### PRD-C-001：Turn 类型（P0）

至少支持：

- `user_chat`：真实用户消息；
- `reminder_delivery`：到期提醒的确定性投递；
- `proactive_chat`：主动对话，由 Agent 生成；
- `daily_compression`：后台压缩，不允许用户工具循环；
- `admin_manual_send`：管理员明确发送，不调用 Agent。

不同类型有独立工具权限和历史写入规则。

### PRD-C-002：Turn 终态（P0）

统一终态至少包含：

| 终态 | 含义 |
|---|---|
| `replied` | 至少一条可见消息确认投递且模型正常结束 |
| `failed_before_delivery` | 未投递任何消息即失败 |
| `failed_after_partial_delivery` | 已投递部分后失败 |
| `cancelled` | 主动取消且未投递 |
| `superseded_before_send` | 被更新输入替代 |
| `interrupted_after_partial_delivery` | 发送序列中被新消息中断 |
| `max_step_limit` | 达到模型步骤上限 |
| `max_message_limit` | 达到消息条数上限 |
| `protocol_error` | 模型未遵守可见消息协议且纠错耗尽 |

终态写入后不可覆盖；补充投递结果通过新事件记录。

### 6.2 可见消息协议

### PRD-C-003：`send_text_message` 是唯一文本出口（P0）

- 普通聊天中，模型 final text、thinking、tool result 均不是用户回复。
- 模型每调用一次 `send_text_message`，系统尝试投递一个聊天气泡。
- 工具返回投递结果后，模型获得新 step，决定继续发、调用其他工具或结束。
- 不允许程序把一段模型文本机械拆成多条并声称是多次模型决策。

### PRD-C-004：协议纠错（P0）

- 若模型没有工具调用且从未成功投递，注入不可见系统纠错提示；
- 默认最多 2 次；
- 仍无可见消息则 `protocol_error`；
- 若已经成功投递，模型无工具调用表示正常结束；
- 纠错提示作为系统事件保存，不伪装为真实用户发言。

### PRD-C-005：硬上限（P0）

- 单 turn 最大模型调用 20；
- 单 turn 最大用户可见消息 10；
- 单条文本清洗后最大 500 字符；
- 单 turn 可见文本总量默认 2000 字符；
- 阈值可配置，但提高上限需同时评估渠道限制与成本。

### 6.3 上下文

### PRD-C-006：上下文组成（P0）

按稳定顺序包含：

1. 核心协议；
2. 当前人格；
3. 当前时间、时区与工作日信息；
4. 当前 turn 类型、来源和渠道能力；
5. 当前允许的工具；
6. 长期记忆索引或按需检索结果；
7. active 历史事件/摘要；
8. 当前输入。

动态时间不得写入可复用静态 snapshot hash。

### PRD-C-007：历史真实性（P0）

- 用户历史只包含真实入站内容；
- assistant 历史只包含已确认投递或明确标记的可见消息；
- tool call/result 保留结构化配对；
- thinking 默认不进入未来对话上下文；
- 发送失败的内容不得伪装为用户已经看见；
- summary 与下一条用户消息可以在 provider 格式中合并，但领域事件仍分开。

### PRD-C-008：空闲时间（P0）

超过默认 10 分钟注入“距离上次用户互动的时间”提示。时间来自 Clock，按用户时区表达。不得把系统任务的时间误当作用户最近互动时间。

### 6.4 人格

### PRD-C-009：可配置人格（P0）

- 默认加载 `persona.md`；
- 后台可编辑、预览、保存和回滚到最近版本；
- 修改只影响新 model call，不篡改历史；
- 人格文件解析失败时保留上一个有效版本；
- 人格与核心安全/工具协议分离，人格不能覆盖系统安全规则。

默认「栖」保留以下产品特征：

- 长期熟悉的聊天伙伴，不是客服、导师或治疗师；
- 优先接住情绪，再决定是否提供建议；
- 能短不长、口语化、允许沉默与轻微情绪；
- 不制造虚假现实经历，不声称已执行未执行的动作；
- 避免过度鸡汤、过度解释、连环反问和机械模板。

### 6.5 输出保护

### PRD-C-010：确定性清洗（P0）

所有渠道投递前必须执行：

- 去除不适合即时聊天的 Markdown 强调、代码和删除线标记；
- 压缩超过 2 个的连续空行；
- 过滤已知密钥、Bearer token、内部 tool call id；
- 处理空内容；
- 应用渠道长度和总量限制。

检测到内部 call id 时整条替换为安全兜底，不把内部标识部分打码后继续发送。

### PRD-C-011：Guard 可观测（P0）

记录规则 ID、处理动作和内容长度变化；默认不得把原始敏感文本写日志。管理员可看到“某条消息因密钥/内部 ID/超长被处理”，但看不到秘密。

### 6.6 Provider

### PRD-C-012：模型可替换（P0）

- 默认 DeepSeek；
- provider 配置包含 model、base URL、timeout、retry、temperature、max tokens、context window；
- 业务层不依赖 provider 原始对象；
- provider 不支持的能力必须在启动或 PoC 时明确报错，不静默丢字段；
- 同一 workflow 尽量使用单一消息/API shape。

### PRD-C-013：流式边界（P0）

内部可以流式读取 thinking/text/tool call 增量；用户可见文本仍以 `send_text_message` 完整工具参数为单位投递。不得把半个 JSON 或未完成的工具参数发送给用户。

---

## 7. 工具系统

### 7.1 通用规则

### PRD-T-001：工具定义（P0）

每个工具必须有：

- 稳定名称与版本；
- 面向模型的描述；
- 强类型输入与 JSON Schema；
- 输出/错误契约；
- 副作用等级：none/read/write/send；
- 超时、重试和幂等策略；
- 依赖健康检查；
- 允许的 turn 类型和渠道；
- 审计脱敏规则。

### PRD-T-002：暴露策略（P0-Correction）

暴露集合由以下因素共同决定：

- turn 类型；
- 配置开关；
- 外部依赖是否可用；
- user/administrator 权限；
- 渠道是否支持动作；
- 副作用是否需确认。

未暴露工具即使模型伪造调用也不得执行。

### 7.2 工具需求矩阵

| 工具 | 输入/前置 | 成功结果 | 主要错误与验收 |
|---|---|---|---|
| `send_text_message` | 非空 text，可选 delay；存在投递器 | `sent` + delivery id/status | guard 阻断、失败前、失败确认、未知；每次调用最多一气泡 |
| `get_time` | IANA timezone，可默认用户时区 | 本地日期、时间、星期、时区 | 非法时区给可理解错误，不猜 |
| `get_weather` | 城市、可选日期 | 位置、实时或预报、更新时间 | QWeather 未配置优雅失败；同名城市需消歧 |
| `memory` | command + path/content | view/create/replace 结果 | 模型无 delete；路径隔离；精确替换唯一匹配 |
| `web_search` | query、结果数、是否 answer | 标题、URL、摘要、来源 | Tavily 未配置；外部内容标记不可信 |
| `web_fetch` | 合法 http(s) URL | 受长度限制的正文/Markdown | 防 SSRF；超时、过大、不可访问可诊断 |
| `todo_list_lists` | Microsoft 授权 | 清单列表 | 未授权返回授权需求，不使 turn 崩溃 |
| `todo_list_tasks` | list id/名称、状态过滤 | 任务列表 | 名称歧义要求消歧 |
| `todo_create_task` | list、title、可选 due/note | 新任务 id 与摘要 | 写动作；结果未知不得自动重复 |
| `todo_complete_task` | task id 或唯一匹配 | 完成确认 | 多匹配不执行，要求澄清 |
| `create_reminder` | trigger、note、delivery text、timezone | task id、本地时间、UTC | 至少提前 10 秒、最多 90 天、文本 ≤500 |
| `list_reminders` | 当前 user | pending/active 列表 | 只返回当前用户 |
| `cancel_reminder` | task id | cancelled/已终态说明 | 已发送任务不可伪装取消成功 |
| `schedule_next_proactive` | delay/建议时间 | candidate accepted | 仅 proactive turn；最终仍受安静时段/配额校验 |

### PRD-T-003：工具结果表达（P0）

- 工具结果面向模型可读，但必须含机器可判断 code/status；
- 不以解析自然语言字符串作为业务控制依据；
- 用户可见说明由 Agent 生成，确定性提醒到期文本除外；
- 外部网页和 memory 内容必须标记为不可信数据，不能覆盖系统指令。

### PRD-T-004：天气（P0）

- QWeather 默认 provider；
- 位置缓存默认 24h、实时天气 10min、预报 30min；
- 缓存 key 包含规范化位置和查询类型；
- 缓存过期后失败可按策略返回“上次数据 + 已过期标记”，不得伪装最新；
- 同省下属区县等启发式消歧必须可测试。

### PRD-T-005：Web（P0）

- 默认 Tavily；
- search 默认最多 5 条，缓存 5 分钟、最多 200 项；
- fetch 默认正文上限 12000 字符；
- 只允许 http/https，解析和重定向后再次执行 SSRF 检查；
- Agent 使用网页事实时应在用户消息中给出可访问来源链接。

### PRD-T-006：Microsoft To Do（P0）

- 使用 Microsoft Graph 官方 API；
- Device Code Flow 适合本地无回调环境；
- token cache 加密保存，日志不得输出 access/refresh token；
- 依赖可关闭；关闭时相关工具不暴露；
- 模糊匹配只能用于候选，多个候选时不得猜测执行写操作。

---

## 8. 记忆、事件与压缩

### 8.1 原始事件

### PRD-M-001：事件是对话事实来源（P0）

每个 turn 事件按 seq 有序，至少覆盖：

- turn started/finished；
- inbound user message；
- system reminder；
- model response metadata；
- tool call；
- tool result；
- visible message prepared；
- delivery attempted/outcome；
- interruption/cancellation；
- compression replacement。

事件 payload 版本化。未知新事件在旧读取器中不得导致整个会话无法重建。

### PRD-M-002：重建（P0）

- 服务启动按 conversation 读取 active 事件重建上下文；
- 重建必须验证 tool call/result 配对和顺序；
- 发现损坏事件时隔离并报警，尽可能重建到最后有效边界；
- 正常 turn 不要求每次从 DB 全量读取，但内存 cache 只是派生状态；
- 多 conversation 各自持有 cache，不使用全局当前用户。

### 8.2 每日压缩

### PRD-M-003：触发条件（P0）

- 默认按 Asia/Shanghai 在 03:56–04:25 的容错窗口检查；
- 同一 conversation、同一 summary date 最多成功一次；
- 用户 turn 正在执行时等待该 conversation 锁；
- 没有足够新内容时明确 skip；
- 管理员手动触发与自动触发走同一生产流程。

### PRD-M-004：压缩输出（P0）

压缩模型输出：

- `<summary>`：可供下一日继续对话；
- `<memory_operations>`：最多 3 个主题变更和 1 个索引变更；
- 每个长期记忆操作包含理由与本次对话原文证据。

摘要应涵盖：身份/偏好、情绪变化、主要话题、决定与承诺、已执行动作、未完成事项、关系语境。不得机械保存所有闲聊。

### PRD-M-005：证据与幻觉防护（P0）

- 引用证据必须能在本次压缩输入中精确或规范化匹配；
- 不允许根据模型常识新增用户事实；
- 操作超过数量、路径越界、格式错误、证据缺失时整批拒绝；
- 拒绝记忆操作不必必然拒绝合格摘要，但两者结果需分别记录；
- 旧 memory 与当前用户明确陈述冲突时，以当前陈述优先并产生更新候选。

### PRD-M-006：事务（P0）

1. 构建候选摘要和文件操作；
2. 校验；
3. 暂存文件变更与回滚快照；
4. 在一个 DB 事务中写 summary、新 active 事件、旧事件 inactive、压缩元数据；
5. DB 成功后提交文件批次并替换内存 cache；
6. 任一步失败恢复到压缩前可用状态。

不得出现“DB 已压缩但文件半更新”或“内存替换但持久层失败”。

### 8.3 长期记忆

### PRD-M-007：存储与隔离（P0）

- 默认文件式 Markdown；
- `sha256(user_id)` 派生不可逆目录键；
- 每用户 `MEMORY.md` 为索引，主题正文单独文件；
- 模型路径不能含绝对路径、`..`、隐藏目录或符号链接逃逸；
- 单文件默认 32KB，索引 25KB/200 行，单用户总量 256KB。

### PRD-M-008：准入（P0）

保存：

- 用户明确要求记住的信息；
- 长期稳定的身份、偏好、关系和反复出现的重要目标；
- 对未来互动显著有帮助且有证据的信息。

不保存：

- 普通聊天流水；
- 短期情绪；
- 密钥、token 和高敏身份凭据；
- 提醒和待办的运行状态；
- 模型推断但用户未确认的事实；
- Phone Context 瞬时数据。

### PRD-M-009：权限（P0）

- 模型工具仅 view/create/str_replace；
- 模型不能 delete；
- 用户在聊天中明确要求忘记时，Agent 应说明可执行的产品流程；P1 可加入受确认的忘记动作；
- 管理后台可删除并原子更新索引；
- 一键清除包括长期记忆，但保留微信绑定凭据。

### PRD-M-010：诊断（P0）

后台展示：

- 索引条目；
- 文件大小与更新时间；
- 悬空索引；
- 孤儿文件；
- 语法/路径错误；
- 最近一次压缩/操作结果。

---

## 9. 调度、提醒与主动消息

### 9.1 统一任务状态机

### PRD-S-001：任务实体（P0-Correction）

所有提醒、主动检查和压缩触发使用统一 ScheduledTask 事实模型，不再维护第二套 reminder 表。

必备字段：

- task id、type、user、conversation/agent；
- trigger at UTC、原始 timezone；
- payload version；
- status、generation；
- delivery key；
- attempt/max attempts；
- last error、attempt timestamps、fired timestamp；
- created/updated。

状态至少包含：

`scheduled → firing → sending → fired`

以及 `cancelled`、`failed`、`unknown_after_attempt`。

### PRD-S-002：恢复（P0）

- 启动时恢复未终态任务；
- `scheduled/firing` 可重新调度；
- 崩溃前处于 `sending` 的高副作用任务不得盲目重发，标记 `unknown_after_attempt` 并依据投递账本判断；
- 过期提醒按可配置 grace period 决定立即发或标记 missed；
- 系统时钟变化、夏令时和时区解析必须可测试。

### 9.2 提醒

### PRD-S-003：创建（P0）

- trigger 必须是带时区的明确时间；
- 默认至少未来 10 秒、最多未来 90 天；
- 保存原始用户事项与确定性 delivery text；
- 返回本地时间与 UTC，避免时区误读；
- 创建成功以任务已持久化为准，不以仅加入内存 scheduler 为准。

### PRD-S-004：到期投递（P0）

- 到期提醒默认不再次调用 LLM，直接投递经过 guard 的 delivery text；
- 使用当前可发送渠道和联系人授权；
- 记录 delivery intent/outcome；
- 成功后终态 fired；
- 失败按安全策略重试，结果未知不重复；
- 到期消息作为系统触发和可见 assistant/delivery 事件进入历史，不能伪装成用户发言。

### 9.3 主动对话

### PRD-S-005：资格（P0）

主动 turn 只有同时满足以下条件才可执行：

- 功能已启用；
- 有可发送联系人/渠道；
- 当前时间在允许窗口且不在 quiet hours；
- 冷却已结束；
- 当日配额未耗尽；
- 没有该用户 active user turn；
- generation 仍为最新。

### PRD-S-006：频率（P0）

默认 tier：

| Tier | 每日上限 | 间隔范围 |
|---|---:|---|
| minimal | 5 | 3600–10800 秒 |
| moderate | 10 | 1800–7200 秒 |
| enthusiastic | 20 | 900–3600 秒 |

后台、配置和实际调度必须来自同一配置对象。用户可关闭主动消息。

### PRD-S-007：主动历史（P0）

主动触发 instruction 作为明确 `system_trigger` 事件保存，与响应/工具事件作为一个压缩单元；不得留下孤立触发或孤立回复。

---

## 10. 微信 iLink

### PRD-W-001：扫码部署（P0）

- 管理员发起部署；
- 获取二维码 payload 并服务端安全渲染 PNG；
- 前端轮询状态；
- confirmed 后校验 token，持久化 credential，启动 bridge；
- token 日志与 API 只显示尾部掩码；
- 重新部署先安全停止旧 bridge，再切换 credential；
- 失败保留旧的可用 credential，除非管理员明确解绑。

### PRD-W-002：绑定保护（P0）

微信联系人绑定含 `user_id/context_token/bot_id/updated_at`。任何普通清空、schema 迁移、测试清理都不得删除。只有显式“解除微信绑定”操作可删，且需二次确认并说明必须重新扫码。

### PRD-W-003：长轮询（P0）

- 默认上游 poll timeout 35 秒；
- 超时属于正常循环，不记 error；
- 网络失败指数退避并有上限；
- 停止服务可取消 pending poll，无 Ctrl-C traceback；
- 不把完整 token 或消息敏感正文写入 error 日志。

### PRD-W-004：消息聚合（P0）

- 默认 debounce 2 秒，max wait 8 秒（以当前配置为准）；
- 同用户聚合，不跨用户；
- 保留原 message ids、seq 和 cutoff；
- max wait 到期必须刷新，避免用户持续输入导致永不响应；
- 空/不支持消息有确定处理策略。

### PRD-W-005：发送（P0）

- 每用户发送锁；
- 保序；
- 支持拟真 delay，测试模式可归零；
- 单条结果映射为统一 DeliveryStatus；
- bridge 未启动、token 缺失、联系人不匹配时不得假装成功；
- `/chat` 返回空 `messages`，避免 poller 二次发送；通过 delivered count 记录统计。

---

## 11. AsPocket App

### 11.1 通用规则

### PRD-A-001：鉴权优先（P0）

所有 `/api/*` 在解析大 body、写文件、验证 remoteAgentId 或 callback URL 前先鉴权。支持 Bearer 或 `x-api-key`。未配置 key 且启用鉴权时返回 readiness/config error，不降级为匿名。

### PRD-A-002：入站模式（P0）

支持 `wechat`、`app`、`both`：

- `wechat`：App 消息读写入口返回明确 503；
- `app`：停止微信 bridge；
- `both`：两者可用；
- 切换模式不删除历史和 session；
- 状态持久化并在重启后恢复。

### 11.2 Profile、邀请与路由

### PRD-A-003：Profile（P0）

`GET /api/profile` 返回至少：

- `agentName`
- `connectedAt`
- 可选 `capabilities`/protocol version（P1）

connectedAt 定义为当前 Router 连接/响应时间还是服务启动时间必须固定；建议改名避免歧义。

### PRD-A-004：联系人邀请（P0）

- 始终包含内置 `default` Agent；
- 包含在线外部 session；
- 稳定字段至少：invitation id、remoteAgentId、displayName、description、agentType、status；
- offline session 不作为可接受的新邀请；
- 配置与 session remoteAgentId 冲突时有确定优先级；
- App 只依赖通用 agentType/protocol，不依赖 `claude_channel` 这种品牌名。

### PRD-A-005：三路路由（P0）

- `internal`：内置 Agent；
- `callback`：同步 localhost HTTP callback；
- `channel`：持久化 pending inbox；
- 保留旧 `attached_session/claude_channel` 输入 alias，输出逐步迁移为通用术语；
- 未知 remoteAgentId 返回 400，不回退到 default。

### 11.3 消息发送与历史

### PRD-A-006：POST `/api/messages`（P0）

请求：

- `clientMessageId`：必填、非空；
- `remoteAgentId`：缺省为 default；
- `content`：可为空，但此时必须有附件；
- `attachments[]`：Phase 1 最多一个。

响应：

```json
{
  "userMessage": {
    "remoteId": "string",
    "clientMessageId": "string",
    "status": "sent",
    "createdAt": "RFC3339 UTC",
    "attachments": []
  },
  "agentReplies": [
    {
      "remoteId": "string",
      "content": "string",
      "createdAt": "RFC3339 UTC",
      "sourceToolCallId": null,
      "attachments": []
    }
  ]
}
```

首次与幂等重试字段必须完全一致。

### PRD-A-007：幂等（P0）

键为 `(remoteAgentId, clientMessageId)`。

- content 和规范化 attachments 相同：返回已存结果，不再次运行 Agent/callback/入队；
- content 不同：409；
- attachments 不同：409；
- 相同 clientMessageId 可在不同 remoteAgentId 独立使用；
- user message 与同步 replies 原子写入；
- callback 失败时不得留下假成功 user message；
- channel 接受成功后 user message 与 pending delivery 原子提交。

### PRD-A-008：Internal 连续性（P0-Correction）

- App internal 路径使用与微信相同的对话内核和事件协议；
- 按 App user/conversation 载入历史，不传空历史；
- 可见回复仍只来自已确认 `send_text_message`；
- App request 默认等待最多 55 秒；
- timeout 返回结构化 `agent_timeout`，客户端可用同幂等键重试；
- 图片无文字时向模型提供规范化多模态输入或明确的附件描述，不伪造为用户原话。

### PRD-A-009：GET `/api/messages`（P0）

- 按 remoteAgentId 隔离；
- 稳定按 `(created_at, sequence/id)` 排序；
- cursor 不跨 Agent；来自其他 Agent 的 cursor 按无效处理或 400，行为固定；
- `limit` 有最小/最大值；
- 每条消息始终包含 `attachments` 数组；
- 返回 `nextCursor/hasMore`；
- created_at 相同不得丢失或重复。

### 11.4 附件

### PRD-A-010：图片上传（P0）

- `POST /api/attachments` multipart 字段 `file`；
- 仅 `image/*`；
- 单文件最大 10MB；
- 同时检查 Content-Length 和实际 body；
- 空文件拒绝；
- 文件名消毒，存储名由服务端生成；
- metadata 与文件写入失败需补偿，不留孤儿；
- 下载验证 attachment id 格式、metadata 和解析后路径仍在 root；
- 返回 `attachmentId/type/mimeType/fileName/sizeBytes/url/createdAt`。

P1 应使用内容嗅探而非只信 MIME，并增加孤儿文件清理。

### PRD-A-011：Sticker（P0）

- union 形式 `{type:"sticker", stickerId, alt}`；
- stickerId 必须在版本化白名单；
- alt 由服务端 catalog 规范化，不能信任客户端；
- 不需要上传二进制；
- internal、callback、channel 和历史返回一致；
- Phase 1 支持 8 个现有 ID，catalog 迁移不得改变旧消息显示。

### 11.5 Phone Context

### PRD-A-012：上传（P0）

请求必须包含：

- `snapshotId`
- `collectedAtMs`
- `source="android-app"`
- 合法 reason
- 非空 contexts

合法 context type：

- `device_status`
- `app_status`

合法 reason 至少保留：

`foreground_start`、`foreground_tick`、`active_chat_changed`、`active_contact_resolved`、`screen_changed`、`background_sync`、`manual_debug`。

### PRD-A-013：读取与时效（P0）

- 默认请求两类、最大年龄 300 秒；
- maxAgeSeconds 限制 0–86400；
- 返回 schemaVersion、status、snapshotId、collectedAtMs、ageSeconds、contexts、missing；
- 无快照或过期返回 `unavailable`，不得猜测；
- 部分缺失时 status 应明确 `partial`，不与完整 `ok` 混淆；
- 保留最近 50 条，修剪事务化。

### PRD-A-014：隐私红线（P0）

未经新的显式设计评审和用户同意，不得采集：

- 消息内容、输入草稿；
- 联系人列表；
- 精确/粗略位置；
- SSID、IP；
- 剪贴板；
- 媒体库；
- 健康数据。

Phone Context 不能进入长期记忆，也不得用作未声明的用户画像。

---

## 12. 外部 Agent

### PRD-X-001：注册（P0）

`POST /api/app-agent-sessions/register` 支持：

- sessionId 必填；
- remoteAgentId 默认 sessionId；
- displayName、description、sourceLabel；
- kind/protocol：callback 或 channel；
- callback URL（仅 callback）。

同一 sessionId 重复注册更新 metadata 和 last seen；不同 session 争用同一 remoteAgentId 返回 409。

### PRD-X-002：URL 安全（P0）

callback v1 只允许：

- `http`
- `127.0.0.1`
- `localhost`
- IPv6 loopback

拒绝 public host、LAN private address、`0.0.0.0`、HTTPS（当前契约）、credentials、file/javascript scheme、无 hostname。鉴权必须先于 URL 解析。

### PRD-X-003：生命周期（P0）

- register 后 online；
- heartbeat 刷新 lastSeen；
- 默认 90 秒无 heartbeat 判定 stale/offline；
- offline 可显式设置；
- stale 判定不得意外修改历史；
- offline 仍可读取历史，不能接收新消息/回复；
- Router 不启动外部进程。

### PRD-X-004：Callback（P0）

- 同步 POST `{clientMessageId, remoteAgentId, content, attachments}` 到 `/message`；
- timeout 返回 504；
- network/non-2xx/invalid JSON/schema 返回 502；
- response 必须为对象且含 `agentReplies[]`；
- 非字符串/空 content reply 跳过；
- 服务端为回复生成本地 remoteId；
- callback remoteId 保留为 source correlation，不作为全局主键；
- user + replies 原子写入。

### PRD-X-005：Channel（P0-Correction）

- accepted user message 写入持久化 pending inbox；
- `(sessionId, clientMessageId)` 幂等；
- poll 默认 limit 10；
- 未 ack 前重复 poll 可再次看到，语义至少一次；
- ack 只确认交付给 channel，不等于回复；
- ack 后不再 poll；
- reply 通过原 clientMessageId 关联到原 turn；
- unknown clientMessageId 返回 404；
- pending delivery 在 Router 重启后仍存在；
- consumer 必须按 deliveryId 幂等，避免至少一次造成重复执行。

---

## 13. 管理后台

### PRD-O-001：认证（P0-Correction）

- 密码使用强哈希保存，不保存明文；
- 默认密码仅允许首次本地初始化，必须显著警告或强制更改；
- session 存储有签名、过期、注销和容量控制；
- cookie `HttpOnly`、`SameSite=Lax/Strict`，HTTPS 部署时 `Secure`；
- 所有 mutation 做 CSRF 防护或同源 token；
- 登录失败有速率限制；
- API 401/403 一致。

### PRD-O-002：信息架构（P0）

后台至少包含：

1. 系统概览；
2. 接入源；
3. App/外部 Agent；
4. 对话与上下文；
5. 长期记忆；
6. 调度；
7. 工具与依赖；
8. 日志；
9. 人格与运行设置；
10. 数据维护。

### PRD-O-003：概览（P0）

显示：

- liveness/readiness；
- provider/model；
- 微信 running/configured/send-ready；
- App 模式与在线 session；
- DB/文件目录；
- scheduler 状态、下一次压缩；
- 最近错误；
- 不显示秘密。

### PRD-O-004：接入源（P0）

- 查看/切换 wechat/app/both；
- 微信扫码、启动、停止、重新部署；
- 展示 token masked、bridge status、最近入站；
- 切换的影响在确认前说明。

### PRD-O-005：App 与 Session（P0）

- session 列表含 kind/protocol、remoteAgentId、online/stale、lastSeen、callback masked；
- 可强制 offline；
- 查看 pending count，不显示敏感正文为默认；
- 查看 Phone Context 最新快照与 age；
- 查看最近 100 附件 metadata。

### PRD-O-006：对话（P0）

- 当前 conversation、最近 turn、终态；
- context window token 峰值、比例、是否估算；
- 手动发送到明确目标，发送前显示渠道；
- 立即压缩；
- 清除上下文；
- 不把“生成成功”显示成“发送成功”。

### PRD-O-007：记忆（P0）

- 列表、查看、诊断、删除；
- 删除二次确认；
- 删除同时更新索引；
- 显示操作是否影响当前内存 context；
- 不允许通过 filename 路径穿越。

### PRD-O-008：调度（P0）

- 压缩 schedule、下次执行、最近 30 次结果；
- pending/unknown/failed 提醒；
- 主动 tier、实际 daily limit、今日 used、quiet hours；
- 可取消 pending reminder；
- unknown_after_attempt 需醒目标注，不提供“一键重发”默认动作。

### PRD-O-009：工具（P0）

展示工具：

- 是否注册；
- 当前是否暴露及原因；
- category/side effect；
- 依赖健康；
- 最近错误；
- 不显示 API key。

### PRD-O-010：日志（P0）

- tail 读取，默认 200、最大 1000；
- level/关键词过滤；
- 手动/自动刷新；
- 大日志读取不全量载入内存；
- 用户消息正文、memory、请求/响应默认脱敏；
- 支持复制 correlation id。

### PRD-O-011：数据维护（P0）

“清除所有上下文与记忆”必须：

- 先展示将清除的实体；
- 二次确认；
- 清除 conversations/turns/events/summaries/long-term memory/App 可选数据/任务按选项；
- 默认保留微信绑定与 schema metadata；
- 返回每类数量；
- 文件与 DB 有回滚/补偿；
- 不提供模糊的“forget”与“reset”两个等价按钮。

---

## 14. API 目录与兼容要求

### 14.1 公共/聊天

| Method | Path | 需求 |
|---|---|---|
| GET | `/health/live` | 进程存活，不访问慢依赖 |
| GET | `/health/ready` | DB、配置、核心组件就绪 |
| GET | `/` | 重定向 `/admin` |
| POST | `/chat` | 微信/HTTP 普通聊天入口 |

旧 `GET /health` 可作为兼容 alias，不能继续混淆 live/ready。

### 14.2 App

| Method | Path |
|---|---|
| GET | `/api/profile` |
| POST | `/api/messages` |
| GET | `/api/messages` |
| POST | `/api/attachments` |
| GET | `/api/attachments/{attachmentId}` |
| GET | `/api/contact-invitations` |
| POST | `/api/phone-context/snapshots` |
| GET | `/api/phone-context/latest` |
| POST | `/api/app-agent-sessions/register` |
| POST | `/api/app-agent-sessions/{sessionId}/heartbeat` |
| POST | `/api/app-agent-sessions/{sessionId}/offline` |
| GET | `/api/app-agent-sessions` |
| GET | `/api/app-agent-sessions/{sessionId}/pending-messages` |
| POST | `/api/app-agent-sessions/{sessionId}/pending-messages/{deliveryId}/ack` |
| POST | `/api/app-agent-sessions/{sessionId}/replies` |

### 14.3 Admin/微信

保留现有功能等价端点：认证、config、context usage、persona、settings、send、memory、compression/reset、QR/deploy/start/stop/status、logs、inbound source、sessions、Phone Context、attachments、reminders、compression status/history、proactive config、tools status。

新仓库可以重组 URL，但替代旧客户端前必须提供兼容层或同步升级调用方。

### PRD-G-006：统一错误 envelope（P0-Correction）

不得再把 JSON 字符串塞进 FastAPI `detail`。统一：

```json
{
  "error": {
    "code": "agent_session_offline",
    "message": "Agent session is offline",
    "requestId": "req_xxx",
    "details": {}
  }
}
```

同一错误在 internal/callback/channel 使用相同 code。message 可本地化，客户端只依赖 code。

---

## 15. 数据需求

### 15.1 逻辑实体

| 实体 | 关键字段/关系 | 保留策略 |
|---|---|---|
| User | id、timezone、preferences | 长期 |
| ChannelIdentity | user、channel、external id | 显式解绑前 |
| BotContactBinding | user/context token/bot | 受保护 |
| Conversation | user、agent、status、compressedAt | 长期 |
| Turn | type/source/status/usage/time | 可配置 |
| Event | turn/seq/type/payload/version/active | 审计与重建 |
| VisibleMessage | turn/index/content/guard | 与 turn |
| Delivery | message/channel/status/idempotency | 至少覆盖审计期 |
| DailySummary | conversation/date/text | 长期 |
| MemoryTopic | user/path/title/update | 长期 |
| ScheduledTask | type/status/trigger/generation | 终态后按策略 |
| ProactiveQuota | user/date/tier/used | 按日 |
| AppMessage | user/agent/role/content/sequence | App 历史 |
| Attachment | metadata/storage/checksum | 可配置清理 |
| PhoneContextSnapshot | type/payload/time | 最近 50/短期 |
| ExternalAgentSession | session/agent/protocol/lastSeen | 临时 metadata |
| PendingDelivery | session/message/ack/status | 终态前必须保留 |
| ConfigRevision | whitelist settings/version | 最近 N 版 |
| AdminSession/Audit | session/action/result | 安全策略 |

### 15.2 数据一致性

### PRD-D-001（P0）

- 所有时间在 DB 保存 UTC，另存原时区；
- App API 输出 RFC3339 UTC；
- user-visible 时间按用户时区；
- 枚举有 DB/应用双重约束；
- message 与 reply 的 sequence 稳定；
- idempotency unique index 与应用判断一致；
- 外键或显式完整性检查不能长期产生孤儿。

### PRD-D-002：非破坏性迁移（P0-Correction）

- Alembic/等价工具逐版本 forward migration；
- migration 前备份并检查磁盘空间；
- 禁止遇到未知 schema 自动 drop；
- 失败回滚且旧版本仍可启动，或明确阻止启动并提供恢复命令；
- 大迁移提供 dry-run；
- `bot_contact_state` 永久保护；
- migration 自动化覆盖旧数据库 fixture。

### PRD-D-003：旧数据导入（P0）

替代当前实例时至少迁移：

- 微信绑定；
- active 对话事件与 turn；
- daily summaries；
- 长期记忆目录；
- pending reminders/scheduled tasks；
- proactive quota（可按切换日重置但需记录）；
- App messages/attachments；
- Phone Context 仅迁移最新有效或直接舍弃，需明确；
- runtime settings 中的非秘密白名单。

导入工具支持：

- dry-run 数量与警告；
- backup；
- checksum/行数核对；
- 可重复执行且不重复；
- 敏感字段不打印；
- 成功后生成迁移报告。

### PRD-D-004：数据清理（P1）

附件、过期 Phone Context、旧 inactive events、过期 admin session 和日志有独立 retention policy。清理任务不得影响 active context 或消息引用。

---

## 16. 安全与隐私

### NFR-SEC-001：秘密

- `.env`/secret store 提供 API key；
- 配置 response、trace、日志、异常不得包含；
- token cache 加密；
- 管理后台只显示 configured/masked；
- source control 扫描秘密。

### NFR-SEC-002：输入

- Pydantic schema 验证类型、长度、枚举；
- user_id/attachment/session/path 使用 allowlist；
- multipart 有总 body 和 file 双限制；
- URL 在解析、DNS/重定向后检查 SSRF；
- HTML/日志输出转义，防 XSS；
- 管理 mutation 防 CSRF。

### NFR-SEC-003：模型不可信

- 模型不能绕过工具权限；
- 网页、memory、Phone Context、callback 数据均作为不可信内容；
- 高副作用工具参数由应用再次验证；
- 模型说“已发送/已创建”不构成成功，必须有工具结果；
- prompt injection 不得改变系统/隐私规则。

### NFR-PRIV-001：最小化

- 默认本地存储；
- OTel/第三方 trace 默认不含完整 prompt、response、memory 和附件；
- 如管理员开启完整 trace，必须显式说明数据去向和保留期；
- 用户可查看并删除长期记忆；
- Phone Context 遵守第 11.5 红线。

---

## 17. 可靠性、性能与可观测

### NFR-REL-001：启动与关闭

- 启动顺序：配置校验 → DB/migration → repository → scheduler recovery → Agent/provider → bridge；
- readiness 只在核心组件就绪后成功；
- 关闭停止接收新 turn，等待/取消活动任务，flush 日志，关闭 HTTP/DB/scheduler/bridge；
- Ctrl-C 不输出非预期 traceback；
- 后台 task 异常必须被收集记录。

### NFR-REL-002：故障恢复

必须自动测试：

- model call 前后崩溃；
- tool 执行前后崩溃；
- message sending 状态未知；
- DB commit 失败；
- 长期记忆文件批次失败；
- scheduler 在 sending 状态重启；
- channel 消息未 ack 重启；
- attachment 文件写入与 metadata 不一致。

### NFR-PERF-001

- 单机个人使用目标，不为虚假高并发过度设计；
- `/health/live` p95 < 100ms（本地）；
- 不含 provider 的普通 API/DB p95 < 300ms；
- App history 50 条 p95 < 300ms；
- 后台日志 tail 不随文件总大小线性读取；
- idle RSS 和负载峰值记录基线，新版不应无理由显著劣化；
- 每 turn framework overhead 应能通过外部代理与端到端计时估算。

LLM 延迟目标以同模型基线对比，不规定绝对值。

### NFR-OBS-001：关联 ID

每次链路至少有：

- request id；
- conversation id；
- turn id；
- model call id；
- tool call id；
- message/delivery id；
- scheduled task id（如适用）。

日志和 span 可通过这些 ID 关联。

### NFR-OBS-002：事件与指标

记录：

- turn count/terminal/latency；
- model provider/model/latency/retry/token/cache；
- tool count/name/status/latency；
- delivery status；
- scheduler due/late/retry/unknown；
- compression outcome/size/memory ops；
- bridge poll/reconnect；
- App idempotent hit/conflict；
- pending queue depth；
- dependency health。

### NFR-OBS-003：原始模型数据

本地调试可以通过显式开关捕获完整 request/response；默认关闭或脱敏。不得把该数据写业务 SQLite。生产观测使用 OTel processor，支持替换后端。

---

## 18. 测试与验收

### 18.1 测试层

1. 领域单测：无网络、无真实时钟；
2. Repository contract：SQLite 与未来 PostgreSQL 一致；
3. Agent contract：TestModel/FunctionModel 验证工具序列；
4. Provider adapter：脱敏真实 fixture；
5. FastAPI API contract；
6. 微信/App/external adapter；
7. scheduler 恢复；
8. migration fixture；
9. Golden 端到端；
10. 安全/隐私测试。

### 18.2 系统级验收场景

| ID | 场景 | 验收 |
|---|---|---|
| AC-001 | 普通聊天 | 只展示 send_text_message，turn=replied |
| AC-002 | 两条回复 | 两次模型决策、两次 delivery，不是文本拆分 |
| AC-003 | 模型直接 final | 最多 2 次纠错，final 不展示 |
| AC-004 | 新消息中断 | 旧 turn 进入正确终态，最新输入被处理 |
| AC-005 | 跨用户并发 | 历史和锁不串用户 |
| AC-006 | 跨重启历史 | active events 重建后能接续 |
| AC-007 | 压缩 | 摘要、事件、文件和内存原子一致 |
| AC-008 | 伪造记忆 | 无证据 operation 被拒绝 |
| AC-009 | 删除记忆 | 索引同步，无路径越界 |
| AC-010 | 提醒重启 | 创建后重启，到期仍只投递一次 |
| AC-011 | 发送未知 | 不自动重复，后台显示 unknown |
| AC-012 | 主动 quiet hours | 延迟到允许时间，不占错误配额 |
| AC-013 | 微信聚合 | debounce/max wait、边界 seq 正确 |
| AC-014 | 清空数据 | 对话/记忆清除，微信绑定保留 |
| AC-015 | App 幂等 | 同 payload 返回相同结果，不二次运行 |
| AC-016 | App 幂等冲突 | content/attachment 改变返回 409 |
| AC-017 | App 连续聊天 | 第二轮能使用第一轮历史 |
| AC-018 | App pagination | 同时间戳无丢失/重复 |
| AC-019 | 图片安全 | 非图片/过大/路径穿越拒绝 |
| AC-020 | Sticker | 四条链路 canonical schema 一致 |
| AC-021 | Phone Context 过期 | unavailable/partial/ok 正确 |
| AC-022 | 隐私字段 | 禁止字段被 schema/测试阻断 |
| AC-023 | Callback 原子性 | 失败不写 user；成功 user+replies 一次提交 |
| AC-024 | Callback SSRF | 非 loopback 全拒绝，鉴权优先 |
| AC-025 | Channel 重启 | pending 未 ack 消息仍可 poll |
| AC-026 | Channel ack | ack 后不再出现；reply 正确关联 |
| AC-027 | 配置依赖 | 未配置工具不暴露，后台原因一致 |
| AC-028 | migration | 旧 DB 导入后数量/关键数据一致 |
| AC-029 | schema upgrade | 升级不 drop 业务表 |
| AC-030 | 时间测试 | 任意 Clock 日期均通过，不依赖编写日 |

### 18.3 替代门槛

新版进入替代阶段前：

- AC-001 至 AC-030 全通过；
- 所有 P0 安全和隐私测试全通过；
- 旧数据 dry-run 与正式迁移报告无未解释丢失；
- 新旧 Golden 剧本在工具结果、消息语义和终态上无阻断差异；
- 在同 provider/model 下延迟、token 和内存无不可接受劣化；
- 回滚步骤在隔离环境演练成功。

---

## 19. 新仓库交付阶段

### Phase 0：契约冻结

- 将本文需求转为可执行验收清单；
- 冻结 App API schema、事件 schema、工具 schema；
- 构建旧系统 Golden fixture；
- 完成 Pydantic AI PoC。

退出条件：框架红线全部通过，未决产品问题有明确 owner。

### Phase 1：领域与持久化

- identity/conversation/turn/event/message/delivery；
- SQLAlchemy/Alembic；
- Clock、ID、repository 接口；
- 旧数据读取器与 migration dry-run。

退出条件：无 LLM 也能通过事件、幂等、迁移和 scheduler 状态测试。

### Phase 2：Agent 内核与工具

- Pydantic AI provider；
- visible message 协议；
- context、guard；
- 14 个工具逐个迁移；
- deterministic Agent tests。

退出条件：AC-001 至 AC-012。

### Phase 3：渠道

- `/chat`、微信；
- App internal/messages/history/attachments/Phone Context；
- callback/channel；
- session lifecycle。

退出条件：AC-013 至 AC-026。

### Phase 4：管理与观测

- 后台全部页面；
- health/live/ready；
- OTel/log/audit；
- 配置与数据维护。

退出条件：AC-027、运维和安全场景。

### Phase 5：迁移与双跑

- 冻结旧库写入窗口或增量同步方案；
- 备份、dry-run、正式导入；
- 新旧黑盒对照；
- 小流量/本地切换；
- 保留旧实例只读回滚窗口。

退出条件：AC-028 至 AC-030 与替代门槛全部满足。

---

## 20. 待产品确认但不阻塞架构的决策

以下不应由开发者默默猜测。默认值已给出，可在开工前冻结：

1. **是否迁移全部 App 历史与附件**：默认“全部迁移”；若数据量过大再给日期范围。
2. **微信与 App 是否共享同一 conversation**：默认“同一 user + default agent 共享”，渠道只作为事件 metadata。
3. **App 图片是否立即进入模型视觉输入**：默认 P0 保留附件与文字描述；真实多模态理解为 P1，除非当前 provider PoC 稳定。
4. **用户聊天中“忘记 X”是否可直接删除长期记忆**：默认 P0 只引导管理员确认，P1 增加受确认流程。
5. **channel pending 是否在 P0 持久化**：本文默认“是”，因为新仓库不应继续已知重启丢失。
6. **完整 LLM trace 是否默认保留**：默认“不保留正文，只保留 metadata”；本地调试显式开启。
7. **对外开放范围**：默认只监听 `127.0.0.1`；如开放局域网/公网，必须增加 HTTPS、强鉴权与安全评审。

---

## 附录 A：当前默认参数

| 参数 | 当前默认/目标默认 |
|---|---|
| Server | `127.0.0.1:8899` |
| Chat auth | enabled |
| 每请求消息数 | 8 |
| 单用户消息长度 | 1000 |
| 请求总字符 | 4000 |
| 模型步骤 | 20 |
| 可见消息条数 | 10 |
| 可见单条字符 | 500 |
| 可见总字符 | 2000 |
| App Agent timeout | 55s |
| 微信 poll timeout | 35s |
| 微信 debounce/max wait | 2s/8s |
| 图片大小 | 10MB |
| Phone Context 保留 | 50 |
| Phone Context 默认时效 | 300s |
| 外部 session stale | 90s |
| 提醒最短/最长 | 10s/90d |
| 默认时区 | Asia/Shanghai |
| 主动 quiet hours | 23:00–08:00 |
| 主动首次检查 | 120s |
| 搜索结果/缓存 TTL | 5/300s |
| fetch 最大字符 | 12000 |
| 天气位置/实时/预报缓存 | 24h/10m/30m |
| 压缩窗口 | 03:56–04:25 |

参数是初始配置，不是写死常量；改变时必须经过 schema 校验并在后台准确展示。

---

## 附录 B：现状审计结论

本 PRD 相比 2026-07-21 旧稿新增或深化了：

- 用户旅程、成功指标、版本范围和非目标；
- Turn/Message/Delivery 三层事实模型与完整终态；
- 首次/重试一致的 App schema；
- 每个工具的前置、结果、错误和幂等要求；
- external callback/channel 的原子性与恢复语义；
- 统一调度状态机和 exactly-once 不确定性；
- 数据实体、保留、旧数据导入与非破坏性迁移；
- 安全、隐私、SSRF、CSRF、秘密和模型不可信边界；
- 30 个系统级验收场景；
- 新仓库分阶段退出条件；
- 将旧代码中的已知矛盾列为 P0-Correction，而不是兼容约束。

当前测试基线为 **654 passed、4 failed、13 skipped**。4 个失败均来自提醒测试使用已经过去的固定日期，证明新仓库必须注入 Clock；它们不是本次 PRD 对提醒能力的否定。由于当前工作树含未提交修改，正式 Phase 0 应保存 commit/worktree 快照及脱敏 Golden 数据，作为唯一可重复的旧版基线。
