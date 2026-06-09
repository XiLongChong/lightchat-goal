# CLAUDE.md

持久化层——纯 SQLite 存储（只写不读，重启时重建）+ 内存常驻消息数组。

## 文件

```
store.py         — MemoryStore：纯持久化（事件落盘、DB 迁移、联系人状态）
conversation.py  — ConversationContext：内存常驻消息数组（追加、裁剪、重建）
```

## 数据表

| 表名 | 用途 |
|------|------|
| `bot_contact_state` | 微信联系人绑定（user_id, context_token, bot_id），单行 |
| `conversations` | 会话元数据（trim_marker, context_snapshot_id, last_compressed_at） |
| `turns` | turn 记录（turn_number 自增永回收，status, token_count, elapsed_ms） |
| `raw_events` | **唯一消息表**（role, content_json 完整未加工，含 thinking/tool_call/tool_result）。`active=0` 为压缩后软删除，重建时不加载 |
| `context_snapshots` | 静态 system prompt 去重（protocol + persona + tool_version_id） |
| `tool_versions` | 工具定义去重（tools_json hash） |
| `runtime_meta` | schema 版本号（`v3_daily_compression`） |

## 设计原则

- **raw_events 是唯一事实来源**：所有事件写入此表，不做加工，不过滤
- **内存常驻**：ConversationContext 持完整 messages 数组，/chat 不查 DB
- **DB 只做两件事**：事件落盘（每条即写）+ 重启重建（一次 SELECT）
- **动静分离**：snapshot hash 不含时间块（时间每轮实时注入）
- **每日压缩**：凌晨 4:00 后首条消息触发。全部 raw_events 软删除（active=0），摘要注入最后一条含 tool_result 的 user 消息，保留紧随的 assistant。`get_recent_events()` 只查 `active=1`。
- **迁移**：schema 版本不匹配时删除除 `bot_contact_state` 和 `runtime_meta` 外的所有表重建

## 清除

`clear_all()` 删除 conversations/turns/raw_events/context_snapshots/tool_versions，保留 bot_contact_state。管理后台一键清除调用此方法。

## 压缩

`get_all_events_for_compress()` 读全部 raw_events 用于压缩模型输入。`apply_compression()` 执行软删除 + 摘要注入。压缩来源始终读全部历史，旧摘要通过过滤 `<analysis>` 开头的 text block 排除。详见 `agent/compressor.py`。
