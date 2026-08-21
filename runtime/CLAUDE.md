# CLAUDE.md

Chat runtime 薄封装层——将 ChatAgent 包装为可配置的运行时入口，同时承载 App adapter、任务调度、可观测性等横切关注点。

## 文件

```
chat_runtime.py              — ChatRuntime 数据类 + create_chat_runtime 工厂函数
app_agent_session_registry.py — App Agent 会话注册表（attached_session / claude_channel），Stale-TTL 离线检测
channel_pending_inbox.py     — Channel 内存待处理队列（幂等入队、ack 删除、长轮询消费）
config.py                    — 配置加载与管理（config.yaml + runtime_settings.json 合并、热加载）
proactive_service.py         — 主动检查服务：时间窗口、冷却机制、配额控制
log_reader.py                — 日志读取工具：高效读取大日志文件末尾行，支持过滤
admin_labels.py              — 管理后台标签：状态/类型到中文标签的映射转换
contact.py                   — 联系人发送权限：判断 WeChat 联系人是否可接收当前 bot 消息
user_turns.py                — 用户回合锁注册表：每用户 asyncio.Lock（LRU 淘汰），支持中断检测
protocol_writer.py           — 协议事件写入器：统一写入 turn 事件到 DB 和内存 ConversationContext
turn_hooks.py                — 回合后钩子构建器：dev 日志 + model attempt 日志
daily_compression.py         — 每日压缩运行器：后台触发对话压缩的 orchestration
sticker_catalog.py           — 表情包目录：Phase 1 支持的 8 个 sticker ID 白名单
task_scheduler.py            — 任务调度器：基于 APScheduler 的持久化任务调度（提醒、主动对话、压缩）
__init__.py                  — 导出 ChatRuntime
```

## ChatRuntime

数据类，持有对 chat_agent、context_builder、tool_exposure_policy 的引用。`handle_chat()` 方法委托给 `chat_agent.chat()`，只做参数透传。

### 核心方法

| 方法 | 说明 |
|------|------|
| `handle_chat()` | 主入口。支持 `prebuilt_messages` 参数——从 ConversationContext 传入预构建消息数组，跳过 DB 读取 |
| `build_turn_request()` | 构建 TurnScratchpad（当前未在主流程使用） |
| `get_history_for_context()` | 过滤历史消息，只保留 user/assistant 且非空内容 |

### handle_chat 关键参数

| 参数 | 说明 |
|------|------|
| `prebuilt_messages` | 预构建消息数组，从 ConversationContext 传入 |
| `idle_seconds` | 用户空闲秒数，用于上下文构建 |
| `input_message_ids` / `input_first_seq` / `input_latest_seq` / `input_cutoff_time` | 输入边界信息，用于中断检测 |
| `interrupt_checker` | 中断检测函数（新消息 seq > 当前时取消） |
| `protocol_event_writer` | 协议事件写入回调 |
| `post_turn_hooks` | 回合结束后钩子列表（可观测性） |

## 新增核心模块

### App Agent Session Registry

内存中的外部 Agent 会话注册表：
- 支持 `kind: "claude_channel"` 和 `kind: "attached_session"`
- Stale-TTL：90s 无 heartbeat 自动标记 offline（惰性计算，不影响存储）
- `remoteAgentId` 冲突检测

### Channel Pending Inbox

纯内存队列，用于 Claude Code 等外部 Agent 的异步消息传递：
- 幂等入队：`(sessionId, clientMessageId)` 相同不重复
- Ack 机制：消息保留直到被 ack，ack 后删除
- 至少一次交付语义

### UserTurnRegistry

每 user_id 一个 asyncio.Lock，同用户串行、跨用户并发：
- LRU 淘汰（默认 max_locks=512）
- 支持中断检测：新消息 seq > 当前 turn 的 input_latest_seq 时取消

### TaskScheduler

基于 APScheduler 的持久化任务调度：
- 提醒（`user_reminder`）
- 主动对话（`proactive_speak`）
- 每日压缩（`daily_compression`）

## 与 main.py 的关系

`main.py` 中通过 `create_chat_runtime()` 创建全局单例，所有 `/chat` 请求通过它路由。App adapter 端点（`/api/messages`）则通过 `resolve_app_remote_agent` 分三路：
- `default` → `chat_runtime.handle_chat()`
- `attached_session` → HTTP POST callback
- `claude_channel` → `channel_pending_inbox.add()`

## 设计决策

### 内存状态 vs 持久化

| 组件 | 存储方式 | 原因 |
|------|----------|------|
| AppAgentSessionRegistry | 纯内存 | 会话是临时的，重启后客户端会重新注册 |
| ChannelPendingInbox | 纯内存 | MCP 通道是短连接轮询，消息可重试 |
| UserTurnRegistry | 纯内存 | 锁是进程内的，多进程时需要分布式锁替代 |
| TaskScheduler | SQLite 持久化 | 提醒和主动任务跨重启需要保留 |

### Callable 注入模式

多处使用 Callable 注入而非直接依赖（DailyCompressionRunner、TaskScheduler、ChatRuntime.handle_chat 中的各种回调），避免循环依赖，便于测试，关注点分离。

## 注意事项

- runtime/ 不是业务逻辑层，而是 **"编排层 + 适配层"**
- 将 agent/ memory/ tools 的能力组合成完整流程
- 将内部接口适配到 FastAPI 端点、WeChat 桥接、MCP 通道
- 横切关注点：锁、调度、日志、配置、可观测性
