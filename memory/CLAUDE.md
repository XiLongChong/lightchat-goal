# CLAUDE.md

持久化层——SQLite 事件落盘 + ConversationContext 内存常驻消息数组。DB 只做写入和重启重建，日常 /chat 不查 DB。

## 文件

```
store.py           — MemoryStore：纯持久化（事件落盘、DB 迁移、联系人状态、App adapter 表）
conversation.py    — ConversationContext：内存常驻消息数组（追加、重建、complete 重置）
long_term.py       — 长期记忆 Markdown 文件服务（隔离、校验、原子批次、诊断、管理端删除）
reminder_store.py  — 提醒持久化：独立 SQLite 表，CRUD + 索引，与主 Store 共享数据库路径
```

## 数据表

### 核心聊天表

| 表名 | 用途 |
|------|------|
| `bot_contact_state` | 微信联系人绑定（user_id, context_token, bot_id），单行 |
| `conversations` | 会话元数据（trim_marker, context_snapshot_id, last_compressed_at） |
| `turns` | turn 记录（turn_number 自增永回收，status, token_count, elapsed_ms, model_name, prompt_tokens_peak, context_window_tokens） |
| `raw_events` | **唯一消息表**（role, content_json 完整未加工，含 thinking/tool_call/tool_result）。`active=0` 为压缩后软删除，重建时不加载 |
| `context_snapshots` | 静态 system prompt 去重（protocol + persona + tool_version_id） |
| `tool_versions` | 工具定义去重（tools_json hash） |
| `runtime_meta` | schema 版本号（`v3_daily_compression`） |
| `daily_summaries` | 每日摘要（conversation_id, summary_date, summary_text，有唯一索引） |

### 定时任务表

| 表名 | 用途 |
|------|------|
| `scheduled_tasks` | 定时任务（user_reminder / proactive_speak），含 trigger_at_utc, status, generation |
| `proactive_quota` | 主动说话每日配额（user_id, date, used_count, frequency_tier） |

### App Adapter 表

| 表名 | 用途 |
|------|------|
| `app_messages` | App 消息历史（client_message_id, remote_agent_id, role, sequence, attachments_json），复合排序键分页 |
| `app_attachments` | 附件元数据（type, mime_type, storage_path） |
| `phone_context_snapshots` | Phone Context 快照（保留最近 50 条，自动修剪） |

### 提醒表

| 表名 | 用途 |
|------|------|
| `reminders` | 提醒（id, user_id, message, remind_at, timezone, status），独立管理 |

## 设计原则

- **raw_events 是唯一事实来源**：所有事件写入此表，不做加工，不过滤
- **内存常驻**：ConversationContext 持完整 messages 数组，/chat 不查 DB
- **DB 只做两件事**：事件落盘（每条即写）+ 重启重建（一次 SELECT）
- **动静分离**：snapshot hash 不含时间块（时间每轮实时注入）
- **每日压缩**：3:56-4:25 窗口内触发。从 ConversationContext 内存取消息数组，追加伪造 user 消息调用 LLM 生成摘要。摘要单独写入 raw_events（active=1），旧 event 全部软删除（active=0）。DB 永远只存摘要，不提前混入未完成 turn 的用户消息。下一次 chat 时 ContextBuilder 检测到纯 summary user 消息，自动与当前用户消息合并为一条。
- **迁移**：schema 版本不匹配时删除除 `bot_contact_state` 和 `runtime_meta` 外的所有表重建

## 清除

`clear_all()` DELETE FROM conversations/turns/raw_events/context_snapshots/tool_versions/daily_summaries/app_messages/app_attachments/phone_context_snapshots/scheduled_tasks/proactive_quota/reminders，保留 bot_contact_state 和 runtime_meta。ConversationContext.complete() 封装 DB 清除 + 内存重置，管理后台一键清除通过它调用。

## 压缩

`apply_compression()` 接受压缩后的消息数组（通常是一条 `[user([summary])]`），软删除全部旧 event，写入新消息数组（active=1）。压缩源来自 `ConversationContext._messages`（内存），不从 DB 读。旧摘要通过 `_filter_summary_blocks()` 过滤 `<summary>` 开头的 text block。详见 `agent/compressor.py`。

新流程使用 `commit_compression()` 在一个 SQLite 事务中写压缩事件、`last_compressed_at` 和每日摘要。长期记忆文件批次先执行并保留回滚快照；DB 提交失败时回滚文件，成功后才替换内存。

长期记忆按 user_id 哈希隔离。`MEMORY.md` 是扁平索引，主题正文必须有 Markdown 标题和 `更新时间：YYYY-MM-DD`。模型不能删除；管理端删除会同步移除索引项。

## 提醒（reminder_store.py）

独立 `ReminderStore` 类，管理 `reminders` 表：
- `create()` — 创建提醒
- `get_due()` — 获取到期的提醒
- `mark_sent()` — 标记为已发送
- `list_by_user()` — 按用户列出
- `cancel()` — 取消提醒

两张索引：`idx_reminders_user_status` 和 `idx_reminders_remind_at`。
