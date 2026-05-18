# Goal Chat Agent — Agent 入口

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
├── prompts/persona.md       ← AI 人设提示词，可直接改，热生效
│
├── agent/                   ← 核心 AI 逻辑
│   ├── chat_agent.py        ← 对话主循环：构建请求→调模型→解析action→循环直到 done
│   ├── context_builder.py   ← 拼系统提示词 + 历史压缩
│   ├── memory_worker.py     ← 后台异步提取记忆（即发即弃，不阻塞回复）
│   ├── scheduler.py         ← 主动消息调度（硬规则层 + LLM 决策层）
│   └── tools.py             ← 工具白名单 + 实现（天气/时间/邮件/记忆/人设）
│
├── bridge/                  ← 微信桥接
│   ├── ilink_client.py      ← iLink API 客户端（从 TypeScript 移植）
│   └── wechat_poller.py     ← 微信消息轮询 + 缓冲去抖
│
├── memory/                  ← 记忆系统
│   ├── store.py             ← SQLite 记忆存储（WAL 模式）
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
├── tests/                   ← pytest
│   ├── test_chat_agent.py
│   ├── test_memory.py
│   ├── test_scheduler.py
│   └── ...
│
└── data/                    ← 运行时数据（gitignore 忽略）
    ├── memory.db            ← SQLite 数据库
    └── agent.log            ← 日志
```

## 核心架构：一次对话怎么走的

```
微信消息 → wechat_poller → /chat 端点 → ChatAgent.chat()
                                            │
                                    context_builder 拼系统提示词
                                    （人设 + 记忆摘要 + 近期历史）
                                            │
                                    调 DeepSeek API → 返回 JSON
                                    {"actions": [...], "done": false}
                                            │
                                    解析 actions：
                                    - type="message" → 通过 wechat_reply 发消息
                                    - type="tool"    → 执行工具，结果注入上下文
                                            │
                                    done=false → 再调一轮（最多 5 轮）
                                    done=true  → 结束，后台提取记忆
```

## 从哪里下手改

| 你想改什么 | 看哪个文件 |
|-----------|-----------|
| AI 的性格/说话方式 | `prompts/persona.md`（改完在管理面板点保存即生效） |
| 对话逻辑/action 解析/安全限制 | `agent/chat_agent.py` |
| 怎么拼提示词/注入多少记忆/历史压缩 | `agent/context_builder.py` |
| 记忆怎么存/怎么搜/怎么衰减 | `memory/store.py` + `memory/dedup.py` |
| 主动消息什么时候发 | `agent/scheduler.py`（硬规则在 `_hard_rules`，LLM 决策在后面） |
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
GET  /memories             # 列出记忆
POST /memories/import      # 导入记忆
DELETE /memories/{id}      # 删记忆
GET  /history/{user_id}    # 查看聊天记录
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
- **硬规则 + LLM 双层调度**：先过硬规则（免打扰时段/频率限制/连续忽略），过了才调 LLM 决定要不要主动发

## 10 项生产安全保障

代码里内建的安全限制，在 `agent/chat_agent.py` 中有详细注释：

1. JSON 解析多层回退 → 最终当纯文本发出去
2. 硬限制 action 数量/消息长度/总字符数
3. done=false 最多循环 5 轮
4. 工具白名单，未注册工具拒绝执行
5. 低重要性记忆不存
6. 调度器先过硬规则再调 LLM
7. 记忆提取异步即发即弃
8. SQLite WAL + 写入锁防并发
9. 超长对话自动压缩摘要
10. 测试套件覆盖核心路径

## 跑测试

```
pytest tests/ -v
```

测试覆盖：JSON 解析回退、记忆去重、消息 action 解析、调度器硬规则、定时消息存储、工具白名单。
