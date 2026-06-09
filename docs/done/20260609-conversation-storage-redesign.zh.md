# 对话存储重设计 — 递增消息数组与缓存优化（2026-06-09）

## 当前问题

### 滑动窗口导致缓存命中率低

每次取最近 20 个 turn，每轮丢掉最旧、追加最新 → 整个 messages 数组变化 → Anthropic prompt cache 全失效。

### 每次 /chat 都查 DB

消息数组 80 行中 76 行和上一轮完全相同，但每次都全量重读 + json.loads。

### 事件类型混在一张表

`protocol_messages` 混合 user_input、thinking、tool_call、tool_result、delivery，无法高效查询。

## 设计决策（全部确认）

| 决策 | 结论 |
|------|------|
| 事件模型 | 单表 `raw_events`，全量保留，不加工，不过滤 |
| thinking 块 | 保留在消息数组里，给模型看到 |
| 消息数组 | 内存常驻（`ConversationContext`），只增不减，每轮只追加 |
| DB 读取 | `/chat` 不查 DB。重启时才从 DB 重建内存数组 |
| DB 写入 | 每条事件产生即写，WAL 模式，不等 turn 结束 |
| 上下文预算 | 默认 1M tokens，兜底阈值 80%（800K） |
| 兜底裁剪 | 超预算时丢弃最早 turn，保留最近 20 轮完整 |
| 重启恢复 | 注入最近 20 个 completed turn |
| 崩溃 running turn | 丢弃（孤儿 raw_events 无害，恢复时不读） |
| 动静分离 | snapshot hash 不含时间块，时间每轮实时注入 |
| prompt 变更 | 标记不兼容，全量清空 |
| 压缩 | 暂不设计 |
| 旧数据 | 直接删除 |

## 核心运行模型

```
正常运行：
  内存 messages = [t1_turn_start, t1_msg, t1_tool, t1_result, t2_msg, ...]
  每次 /chat → 在内存数组尾部追加本轮事件 → 构建 LLM 请求
  每条事件同时写 DB（WAL append-only）

兜底裁剪：
  token ≥ 800K → 丢弃最早 turn，保留最近 20 轮，更新 trim_marker

重启恢复：
  SELECT 最近 20 completed turn 的 raw_events → 重建内存 messages 数组
```

## 表设计

```sql
── 会话 ──
conversations
    id TEXT PK
    user_id TEXT NOT NULL
    bot_id TEXT NOT NULL
    trim_marker_turn INTEGER DEFAULT 0   ← 裁剪后最早保留的 turn_number
    context_snapshot_id TEXT             ← 当前使用的 snapshot
    created_at REAL NOT NULL

── 回合 ──
turns
    id TEXT PK
    conversation_id TEXT NOT NULL
    turn_number INTEGER NOT NULL         ← 自增，永不回收
    status TEXT NOT NULL                 ← running | completed | failed | cancelled
    token_count INTEGER DEFAULT 0        ← 从 llm_traces 移入
    elapsed_ms INTEGER DEFAULT 0         ← 从 llm_traces 移入
    created_at REAL NOT NULL
    completed_at REAL DEFAULT 0

── 原始事件（唯一消息表）──
raw_events
    id INTEGER PK AUTOINCREMENT
    turn_id TEXT NOT NULL
    seq INTEGER NOT NULL                 ← turn 内顺序
    role TEXT NOT NULL                   ← user | assistant
    content_json TEXT NOT NULL           ← 完整消息（thinking/tool_call/tool_result 全部原样）
    call_id TEXT NOT NULL DEFAULT ''
    created_at REAL NOT NULL
    UNIQUE(turn_id, seq)

── 去重（动静分离）──
context_snapshots
    id TEXT PK                           ← SHA256(protocol + persona + tool_version_id)
    protocol_text TEXT NOT NULL          ← 全局协议（不含时间）
    persona_text TEXT NOT NULL
    tool_version_id TEXT NOT NULL
    created_at REAL NOT NULL

tool_versions
    id TEXT PK                           ← SHA256(tools_json)
    tools_json TEXT NOT NULL
    created_at REAL NOT NULL

── 保留不变 ──
bot_contact_state
runtime_meta
```

### 删除的表

`protocol_messages`、`outbound_messages`、`turn_drafts`、`turn_diagnostics`、`usage_events`、`visible_activity`、`llm_traces` ——全部由 raw_events 替代。token 用量和延迟移到 turns 表字段。

## 写入时机与事务边界

```
turn 生命周期：
  1. BEGIN TRANSACTION
  2. INSERT turns (status=running)
  3. INSERT raw_events (role=user, user_input 完整内容)
  4. COMMIT                           ← 死机点 A：running turn 下次丢弃

  5. LLM call 1
     → INSERT raw_events (role=assistant, thinking 块原样)
     → INSERT raw_events (role=assistant, tool_call 原样)
     → INSERT raw_events (role=user, tool_result 原样)

  6. LLM call 2
     → INSERT raw_events (role=assistant, thinking 块原样)

  7. BEGIN TRANSACTION
  8. UPDATE turns (status=completed)
  9. COMMIT                          ← 死机点 B：完整 turn

每条 INSERT 都是独立的小事务（WAL append-only）。
死机恢复逻辑：只取 status=completed 的 turn。running turn 的孤儿 raw_events 不参与读取。
```

## 内存层

```python
# memory/conversation.py

class ConversationContext:
    """内存常驻的消息数组。每次 /chat 只追加，不查 DB。"""

    def __init__(self, user_id: str, bot_id: str):
        self._messages: list[dict] = []      # 完整的 LLM messages 数组
        self._trim_marker: int = 0
        self._snapshot_id: str = ""

    async def rebuild(self, store: MemoryStore) -> None:
        """重启时从 DB 重建（最近 20 completed turn）."""

    def append_event(self, role: str, content: Any) -> None:
        """追加一条事件到内存数组尾部."""

    def get_messages(self) -> list[dict]:
        """返回当前完整 messages 数组."""

    def maybe_trim(self, estimated_tokens: int, max_budget: int) -> bool:
        """超预算时裁剪最早 turn，返回是否发生了裁剪."""

    @property
    def snapshot_id(self) -> str: ...
    @snapshot_id.setter
    def snapshot_id(self, value: str) -> None: ...
```

## 读取规则

```
正常 /chat：
  messages = conversation_ctx.get_messages()   ← 内存，0 次 DB 查询
  追加 system prompt（静态 snapshot + 实时 time_block）
  → 构建 LLM 请求

重启恢复：
  conversation_ctx.rebuild(memory_store)       ← DB 取最近 20 turn，1 次查询
  → 同正常运行

prompt 变更：
  snapshot_id 变化 → 全量清空内存 + DB → 下次对话从 turn_1 重新开始

兜底裁剪：
  conversation_ctx.maybe_trim(tokens, 800000)  → 更新 trim_marker 到 DB
```

## 代码变更

| 文件 | 变更 |
|------|------|
| `memory/conversation.py` | **新建**：ConversationContext 类 |
| `memory/store.py` | 重写：新 schema，简化为纯持久化（只写 + 重启读） |
| `agent/chat_agent.py` | 适配：从 ConversationContext 拿 messages，不用 history 参数 |
| `agent/runtime_context_builder.py` | 适配：system 动静分离 |
| `agent/llm_gateway.py` | trace 精简：不存完整 request_json |
| `main.py` | 初始化 ConversationContext，传入 ChatAgent 替代 history 查询 |

## 待后续

- **压缩策略**：超出预算时对旧 turn 做摘要
- **中断恢复（部分送达）**：running turn 内已有送达消息的处理
