# Goal Chat Agent — Agent 入口

V2.1.1 (2026-05-20): 主动消息改为 due-time + intent 模型，LLM 不再决定是否发送。活跃话题状态追踪、提示词边界清理、空消息守卫。V2.1 多 bot 架构保持兼容。

你是 AI agent（或者是代替人类开发者读这份代码的人）。这里告诉你这个项目的骨架在哪、怎么跑起来、改东西从哪里下手。

## 一句话概括

单进程 Python + FastAPI 服务。DeepSeek 驱动一个叫"余"的 AI 角色，通过微信 iLink 跟用户聊天。有长期记忆、主动消息、定时提醒。所有数据存一个 SQLite 文件，零云依赖。

## 跑起来

```
# 最简启动
pip install -r requirements.txt
python main.py

# 或者 Docker
docker-compose up -d
```

服务默认在 `http://0.0.0.0:8899`。

环境变量在 `.env`，必填项：`DEEPSEEK_API_KEY`。参考 `.env.example`。

## 目录骨架

```
goal/
├── main.py                  ← 主入口。FastAPI 所有路由都在这里
├── config.yaml              ← 所有配置（支持 ${ENV_VAR} 替换）
├── prompts/                 ← 提示词文件
│   ├── persona.md           ← V2 旧人设（仍可热生效，default_bot 用 identity 文件）
│   ├── base_protocol.md     ← V2.1 基础协议：输出格式、工具调用规则、安全规则
│   ├── bot_identity_default.md ← V2.1 default_bot 人设（身份、语气、风格）
│   ├── relationship_context.md ← V2.1 关系上下文模板（运行时注入参数）
│   ├── memory_policy.md     ← V2.1 记忆查询策略
│   ├── tool_policy.md       ← V2.1 工具约束（可用工具清单运行时注入）
│   └── trigger_context.md   ← V2.1 触发源模板（user/proactive/morning/night/...）
│
├── agent/                   ← 核心 AI 逻辑
│   ├── bot_profile.py       ← V2.1 Bot 画像管理（BotProfile / BotProfileManager / prompt pack 组装）
│   ├── chat_agent.py        ← 对话主循环：构建请求→调模型→解析action→循环直到 done
│   ├── context_builder.py   ← 拼系统提示词 + 历史压缩（V2.1 支持 prompt pack）
│   ├── memory_worker.py     ← 后台异步提取记忆（V2.1 bot-scoped）
│   ├── output_guard.py      ← V2 统一输出守卫（协议泄露检测/嵌套解包/敏感信息过滤）
│   ├── scheduler.py         ← 主动消息调度（V2.1 三阶段：候选→决策→生成）
│   └── tools.py             ← 工具白名单 + 实现（天气/时间/邮件/记忆/人设）
│
├── bridge/                  ← 微信桥接
│   ├── ilink_client.py      ← iLink API 客户端（从 TypeScript 移植）
│   └── wechat_poller.py     ← 微信消息轮询 + 缓冲去抖
│
├── memory/                  ← 记忆系统
│   ├── store.py             ← SQLite 记忆存储（WAL 模式，V2.1 四层记忆 + bot 隔离）
│   └── dedup.py             ← CJK 二元组 Jaccard 去重
│
├── utils/
│   └── json_parser.py       ← 安全 JSON 解析（多层回退）
│
├── static/                  ← 管理界面（纯 HTML/CSS/JS，无框架）
│   ├── admin.html
│   ├── admin.css
│   └── admin.js
│
├── tests/                   ← pytest (50 passed)
│   ├── test_chat_agent.py
│   ├── test_memory.py
│   ├── test_scheduler.py
│   └── ...
│
└── data/                    ← 运行时数据（gitignore 忽略）
    ├── memory.db            ← SQLite 数据库
    └── agent.log            ← 日志
```

## V2.1 架构概览

### Bot 模型

```
WeChat 消息 → resolve current_bot_id → 加载 bot 画像
→ 加载 bot prompt pack → 加载 bot-scoped 记忆层
→ 对话 → 通过同一微信入口回复
```

- 每个 bot 独立：persona、relationship state、recap、bot-specific memory
- 共享用户事实独立于 bot 之外
- 切换 bot 只改 `current_bot_id`，bot A 的上下文不会泄露给 bot B

### 记忆四层 (L1-L4)

```
L1 raw_messages  → 每条原始消息（审计、恢复）
L2 turns         → 语义化对话回合（user batch + bot reply batch + 工具调用 + 元数据）
L3 recap         → 会话阶段摘要（每 N turns 后台 LLM 生成，用于恢复上下文）
L4 long_term_memory → 稳定事实、偏好、项目状态（现有 memories 表，bot-scoped）
```

### 主动消息 V2.1.1 due-time + intent 管道

```
Stage 1: hard_precheck      → sleep hours / bridge / contact / daily cap
Stage 2: due_time_check     → next_due_ts 已到？（随机间隔内保证一次尝试）
Stage 3: load_latest_topic  → 仅加载活跃话题（不含 recap / broad memory）
Stage 4: select_intent      → check_in / latest_topic_followup / self_share / light_ping
Stage 5: generate_message   → LLM 生成消息文本（不询问是否发送）
Stage 6: send_and_record    → 发送 + 记录候选
Stage 7: schedule_next_due  → 随机下一次到期时间
```

LLM 不决定是否发送。due-time 调度 + 意图选择。支持仅跟进最新活跃话题。
followup_max_count 次后或话题 close/stale/answered 后停止跟进。

### 活跃话题状态 (V2.1.1)

```
active_topics 表：每个 (user_id, bot_id) 一行
status: open / waiting_user_result / answered / closed / stale
新话题 → 旧话题变 stale。用户拒绝 → close。天气回答 → answered。
仅 followup-worthy 事件才创建话题（meal/plan/project/question 等）。
```

### V2.1 主动消息三阶段管道（已替换为 V2.1.1）

```
Stage 1: 候选原因生成 → special_date / unfinished_topic / project_push / long_silence / user_reminder
Stage 2: 决策门        → bot active + proactive enabled + bridge/contact + 频率/间隔/忽略检查
Stage 3: 消息生成      → 仅决策通过后才调 LLM 生成文本
```

### 出站消息语义

每条出站消息携带：`source`（user_reply/fixed_morning/scheduled_reminder/proactive/admin）、`author`（assistant/system）、`trigger`、`visible_to_user`、`send_status`（created→sending→delivered/failed）。

## 核心架构：一次对话怎么走的

```
微信消息 → wechat_poller → /chat 端点 → ChatAgent.chat()
                                            │
                                    context_builder 拼系统提示词
                                    （V2.1: prompt pack 组件）
                                            │
                                    调 DeepSeek API → 返回 JSON
                                    {"actions": [...], "done": false}
                                            │
                                    解析 actions：
                                    - type="message" → 通过 wechat_reply 发消息
                                    - type="tool"    → 执行工具（checked against bot.enabled_tools）
                                            │
                                    done=false → 再调一轮（最多 5 轮）
                                    done=true  → 结束，写入 turn (L2) + outbound 记录
                                               → 后台提取记忆（bot-scoped）
                                               → 触发 recap 检查
```

## 从哪里下手改

| 你想改什么 | 看哪个文件 |
|-----------|-----------|
| AI 的性格/说话方式 | `prompts/persona.md`（V2.1.1: 默认 bot 人设源） |
| Bot 画像/切 bot/提示词分包 | `agent/bot_profile.py` |
| 对话逻辑/action 解析/安全限制 | `agent/chat_agent.py` |
| 怎么拼提示词/注入多少记忆/历史压缩 | `agent/context_builder.py` |
| 记忆怎么存/怎么搜/怎么衰减 | `memory/store.py` + `memory/dedup.py` |
| 主动消息 V2.1.1 due-time + intent 管道 | `agent/scheduler.py`（`run_proactive_pipeline` 是入口） |
| 活跃话题状态 | `memory/store.py`（active_topics 表）+ `main.py`（`_analyze_and_update_topic`） |
| 工具怎么加 | `agent/tools.py`（加到 `TOOL_WHITELIST` + 实现函数） |
| API 路由 | `main.py`（所有 `@app.get/post/delete` 都在这里） |
| 微信接入细节 | `bridge/ilink_client.py` + `bridge/wechat_poller.py` |
| 管理面板 UI | `static/admin.html` + `admin.css` + `admin.js` |
| 配置项含义 | `config.yaml`（中文注释很全） |

## API 路由速查

```
GET  /health               # 健康检查
POST /chat                 # 主聊天端点（微信消息最终走到这里）
GET  /admin                # 管理面板页面
GET  /admin/config         # 查看配置
POST /admin/persona        # 保存人设
POST /admin/settings       # 运行时设置
POST /admin/send           # 手动发消息
GET  /admin/bots           # V2.1 列出所有 bot
POST /admin/bots/switch    # V2.1 切换 active bot
GET  /admin/recaps         # V2.1 查看 recap 列表
POST /admin/recaps/generate # V2.1 手动触发 recap 生成
GET  /admin/turns          # V2.1 查看 L2 turns
GET  /admin/outbound       # V2.1 查看出站消息追踪
GET  /admin/proactive/candidates # V2.1 查看主动消息候选记录
GET  /admin/proactive/state    # V2.1.1 查看主动调度状态（due time, last send）
GET  /admin/proactive/topic    # V2.1.1 查看当前活跃话题
POST /admin/proactive/trigger  # V2.1.1 手动触发主动消息（忽略 due time）
POST /admin/proactive/topic/close # V2.1.1 手动关闭活跃话题
GET  /admin/shared-facts   # V2.1 查看共享用户事实
GET  /admin/diagnostics    # V2 查看 turn 诊断
GET  /admin/drafts         # V2 查看 turn drafts
GET  /memories             # 列出记忆
POST /memories/import      # 导入记忆
DELETE /memories/{id}      # 删记忆
GET  /history/{user_id}    # 查看聊天记录（V2.1 bot-scoped）
DELETE /history/{user_id}  # V2.1 清除当前 bot 历史
GET  /wechat/qr            # 获取微信登录二维码
POST /wechat/deploy/start  # 开始部署微信
POST /wechat/start         # 启动微信桥接
POST /wechat/stop          # 停止微信桥接
POST /scheduler/trigger    # 手动触发一轮调度
GET  /scheduler/state      # 调度器状态
GET  /relationship         # 查看关系数据
```

## 关键技术选型

- **JSON action 格式而非 OpenAI function calling**：DeepSeek 对 JSON 输出更稳定
- **CJK 二元组 Jaccard 相似度**：中文记忆去重和检索，不需要嵌入模型
- **SQLite + WAL**：单文件，写入快，不需要外部数据库
- **即发即弃记忆提取**：回复用户后才异步提取记忆，不阻塞消息返回
- **V2.1 三阶段主动管道**：候选原因 → 决策门（纯逻辑，免 LLM）→ 消息生成（仅通过后调 LLM）
- **V2.1 四层记忆**：raw → turn → recap → long-term，bot 隔离
- **V2.1 提示词分包**：6 个独立 .md 文件，bot 画像可替换 identity 文件

## 10 项生产安全保障

代码里内建的安全限制，在 `agent/chat_agent.py` 中有详细注释：

1. JSON 解析多层回退 → 最终当纯文本发出去
2. 硬限制 action 数量/消息长度/总字符数
3. done=false 最多循环 5 轮
4. 工具白名单，未注册工具拒绝执行（V2.1 增加 bot-level enabled_tools 拦截）
5. 低重要性记忆不存
6. 调度器先过硬规则再调 LLM（V2.1 前置部署前提检查）
7. 记忆提取异步即发即弃
8. SQLite WAL + 写入锁防并发
9. 超长对话自动压缩摘要
10. 测试套件覆盖核心路径

### V2.1 新增安全边界

- 系统触发任务不进 user role（trigger != "user" → system role）
- bot 间记忆/关系/历史/摘要完全隔离（bot_id 做 key）
- 决策门纯逻辑拦截（不浪费 LLM 调用）
- enabled_tools 在 prompt 和执行层双重拦截
- 出站消息生命周期追踪（created→sending→delivered/failed）

## 跑测试

```
pytest tests/ -v
```

测试覆盖：JSON 解析回退、记忆去重、消息 action 解析、输出守卫、调度器硬规则、定时消息存储、工具白名单。当前 50 passed，5 个 Windows tmp_path 权限错误（环境问题，非代码缺陷）。
