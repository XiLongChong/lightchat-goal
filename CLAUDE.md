# CLAUDE.md

Goal Chat Agent — 基于 DeepSeek LLM 的微信聊天机器人，单进程 FastAPI + SQLite。

## 项目规则

- **不要删除 `bot_contact_state`** — 包含微信 iLink 的 context_token，丢失后需重新扫码部署
- **不要新建文档文件（*.md）** 除非明确要求。根目录只保留此文件和 README.md
- **不要引入新的依赖** 除非不可避免。当前依赖见 requirements.txt
- **配置优先级**：命令行/环境变量 > runtime_settings.json > config.yaml 默认值
- **日志**：`./data/agent.log`，可通过 admin UI 查看（`/admin/logs`）
- **Windows 环境注意**：PowerShell 5.1，无 `&&`/`||`，无 tail/head，需用对应 PowerShell 语法

## 常用命令

```bash
python main.py                       # 启动服务
pytest tests/ -v                     # 全部测试
pytest tests/ -v -k "test_name"      # 按名称过滤
python scripts/reset_runtime_data.py # 预览清除
python scripts/reset_runtime_data.py --confirm  # 确认清除
```

## 目录索引

| 目录 | 职责 | 详情 |
|------|------|------|
| `agent/` | LLM 交互层（核心对话循环、工具执行、输出 guard） | [agent/CLAUDE.md](agent/CLAUDE.md) |
| `bridge/` | 微信 iLink 桥接（长轮询、消息收发） | [bridge/CLAUDE.md](bridge/CLAUDE.md) |
| `memory/` | SQLite 持久化（联系人、历史、诊断） | [memory/CLAUDE.md](memory/CLAUDE.md) |
| `runtime/` | Chat runtime 薄封装 | [runtime/CLAUDE.md](runtime/CLAUDE.md) |
| `runtime_contracts/` | 共享数据契约类型 | [runtime_contracts/CLAUDE.md](runtime_contracts/CLAUDE.md) |
| `tests/` | 测试套件 | [tests/CLAUDE.md](tests/CLAUDE.md) |
| `scripts/` | 开发辅助脚本 | [scripts/CLAUDE.md](scripts/CLAUDE.md) |
| `static/` | 管理后台前端 | [static/CLAUDE.md](static/CLAUDE.md) |
| `prompts/` | LLM 提示词模板 | [prompts/CLAUDE.md](prompts/CLAUDE.md) |
| `docs/` | 项目文档（demo/doing/done/bug） | [docs/CLAUDE.md](docs/CLAUDE.md) |

## 数据流（一次用户消息的完整路径）

```
微信 → iLink 长轮询 → bridge/wechat_poller
  → HTTP POST /chat → main.py（认证 → 校验 → 获取锁 → 构建上下文）
  → runtime/chat_runtime → agent/chat_agent.chat()
    → agent/runtime_context_builder（构建系统 prompt）
    → agent/llm_gateway → agent/providers/deepseek（LLM 调用）
    → agent/tools（工具执行：send_text_message / get_time / get_weather）
    → agent/output_guard（消息清洗）
    → bridge wechat_sender 回调 → iLink 发送 → 微信
  → memory/store（持久化 protocol transcript + 诊断）
  → 释放锁 → 返回响应
```

## 关键机制

- **用户级锁**：每 user_id 一个 asyncio.Lock，同用户串行、跨用户并发
- **中断检测**：新消息 seq > 当前 turn 的 input_latest_seq 时取消当前 turn
- **配置**：config.yaml（`${VAR:-default}` 替换）+ .env（密钥）+ data/runtime_settings.json（热加载覆盖）
- **Schema 迁移**：`runtime_meta` 表存版本号 `chat_only_1`，不匹配时重置非 contact 表
