# 对话存储重设计 — 验收方案（2026-06-09）

## 验收步骤

### 1. 表结构验证

```bash
sqlite3 data/memory.db ".schema"
```

预期：
- 存在 `conversations`, `turns`, `raw_events`, `context_snapshots`, `tool_versions`
- 存在 `bot_contact_state`, `runtime_meta`
- **不存在** `protocol_messages`, `outbound_messages`, `turn_drafts`, `turn_diagnostics`, `usage_events`, `llm_traces`, `visible_activity`

### 2. 事件全量保留

进行 1 轮对话（用户发消息 → 模型回复），查 raw_events：

```sql
SELECT id, turn_id, seq, role, content_json FROM raw_events ORDER BY id;
```

预期看到：
- seq=0: user 消息（含 `<channel>` 包装）
- seq=N: assistant 消息，content_json 包含 `"type":"thinking"` 块
- seq=N+1: assistant 消息，content_json 包含 `"type":"tool_use"` 块
- seq=N+2: user 消息，content_json 包含 `"type":"tool_result"` 块
- thinking 块没有被过滤

### 3. 消息数组递增

进行 3 轮对话，每轮后查日志（设置 `LOG_LEVEL=DEBUG` 启动）：

```
grep "context messages count" data/agent.log
```

预期：
- 第 1 轮后：1 个 turn 的消息在数组中
- 第 2 轮后：2 个 turn（前缀不变，尾部追加）
- 第 3 轮后：3 个 turn（同上）

验证前缀不变：第 2 轮数组的前 N 条 = 第 1 轮数组的全部

### 4. 重启恢复

进行 5 轮对话 → 重启服务 → 发第 6 条消息，查日志：

预期：第 6 轮上下文包含最近 5 轮的历史（check context messages count ≥ 前 5 轮）

### 5. 缓存命中验证

打开 claude-tap 抓包，连续发 3 条消息：

第 2、3 条消息的请求体中，messages 数组的前缀和上一次请求完全相同。服务日志中 LLM 返回的 `cache_hit_tokens` > 0。

### 6. 微信桥接不受影响

重启 → 一键清除 → 微信仍然能收发消息。`bot_contact_state` 数据不变。

### 7. 一键清除仍可用

管理后台点击"一键清除" → raw_events, turns 清空 → conversations 重置 → 微信桥接正常。

## 验证清单

- [ ] 旧 7 张表已删除
- [ ] thinking 块完整保留在 raw_events
- [ ] 消息数组只增不减（前缀不变）
- [ ] 重启后恢复最近 20 turn
- [ ] 缓存命中 > 0
- [ ] 微信桥接不受影响
- [ ] 一键清除正常
- [ ] 全部单元测试通过
