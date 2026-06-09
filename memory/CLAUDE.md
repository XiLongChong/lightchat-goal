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
| `conversations` | 会话元数据（trim_marker, context_snapshot_id） |
| `turns` | turn 记录（turn_number 自增永回收，status, token_count, elapsed_ms） |
| `raw_events` | **唯一消息表**（role, content_json 完整未加工，含 thinking/tool_call/tool_result） |
| `context_snapshots` | 静态 system prompt 去重（protocol + persona + tool_version_id） |
| `tool_versions` | 工具定义去重（tools_json hash） |
| `runtime_meta` | schema 版本号（`v2_incremental_array`） |

## 设计原则

- **raw_events 是唯一事实来源**：所有事件写入此表，不做加工，不过滤
- **内存常驻**：ConversationContext 持完整 messages 数组，/chat 不查 DB
- **DB 只做两件事**：事件落盘（每条即写）+ 重启重建（一次 SELECT）
- **动静分离**：snapshot hash 不含时间块（时间每轮实时注入）
- **迁移**：schema 版本不匹配时删除除 `bot_contact_state` 和 `runtime_meta` 外的所有表重建

## 清除

`clear_all()` 删除 conversations/turns/raw_events/context_snapshots/tool_versions，保留 bot_contact_state。管理后台一键清除调用此方法。
