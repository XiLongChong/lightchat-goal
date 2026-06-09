# CLAUDE.md

SQLite 持久化层——存储联系人状态、对话历史、turn 诊断、用量统计。

## 文件

```
store.py  — MemoryStore 类：aiosqlite 封装，包含全部建表迁移和 CRUD 操作
```

## 数据表

| 表名 | 用途 |
|------|------|
| `bot_contact_state` | 微信联系人绑定（user_id, context_token, bot_id），单行 |
| `turns` | turn 记录（用户消息、assistant actions、tool calls） |
| `protocol_messages` | 模型协议消息（provider transcript，按 turn 组织） |
| `outbound_messages` | 外发消息及其发送状态 |
| `turn_diagnostics` | turn 诊断（耗时、token 数、fallback 类型、terminal_state） |
| `turn_drafts` | turn 草稿（生成但未发送的消息） |
| `usage_events` | 用量事件（chat_turn, manual_send, empty_message_batch 等） |
| `visible_activity` | 最后可见消息时间（用于上线检测） |
| `llm_traces` | LLM 请求/响应完整记录（通过 trace 回调写入） |
| `runtime_meta` | schema 版本号（`chat_only_1`），迁移时比较 |

## 迁移策略

- schema 版本存储在 `runtime_meta` 表
- 版本不匹配时，删除除 `bot_contact_state` 外的所有表重建
- `bot_contact_state` 在重置和迁移中始终保留（保持微信连接不中断）

## 关键设计

- 使用 `aiosqlite.row_factory = aiosqlite.Row` 返回字典式行
- WAL 模式（非 :memory: 时）
- `_write_lock` 保护写操作串行化
- `close_all()` 类方法用于测试清理（见 conftest.py）
- `_instances` 类属性追踪所有实例

## 注意事项

- `db_path` 默认 `./data/memory.db`，在 `main.py` 启动时配置
- LLM trace 回调是异步的，写入失败会被静默忽略
