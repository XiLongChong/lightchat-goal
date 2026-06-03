# MemorySystem 可执行实现规格

> 日期：2026-06-03
> 状态：可执行实现规格
> 模块：MemorySystem
> 依赖：
> - `docs/companion-architecture-requirements.zh.md` (section 6, 13.4, 13.5, 13.6)
> - `docs/companion-architecture-design-decisions.zh.md` (section 4, 9.5)
> - `docs/companion-runtime-module-contracts.zh.md` (section 13)
> - `docs/companion-runtime-data-contracts.zh.md` (section 16, 18)
> - `docs/companion-runtime-state-machines.zh.md` (section 8, 12)
> - `docs/companion-runtime-gap-analysis.zh.md` (MemorySystem section)
> - `docs/companion-runtime-implementation-plan.zh.md` (Phase 8)
> - `docs/companion-runtime-test-plan.zh.md` (section 7)

---

## 1. 模块目标

MemorySystem 提供两阶段记忆服务：

```text
Phase 1 (before model call):  load_for_turn()   → MemorySelection
Phase 2 (after turn terminal): write_after_turn() → (persisted memories, SentContentRecords)
```

核心原则：

- **记忆在 turn 中绝不写入，只在 turn 结束后写入。**
- 模型可以在 turn 中建议记忆候选（MemoryCandidate），但最终写入决策由 MemorySystem 做出。
- 记忆模拟人类选择性记忆，不是数据库全量记录。
- **情感真实性优先于信息完整性。**

### 1.1 MemorySystem 接口

```python
class MemorySystem:
    """
    Two-phase memory service.

    Phase 1: load_for_turn() — called by ContextBuilder before model invocation.
              Returns MemorySelection with categorized, deduped, visibility-filtered
              memory records ready for context assembly.

    Phase 2: write_after_turn() — called by TurnEngine after terminal state is reached.
              Processes MemoryCandidate list, applies retention policy, checks
              forget directives, writes qualifying memories, and records sent content.
    """

    async def load_for_turn(self, request: MemorySelectionRequest) -> MemorySelection:
        """Select memories for context inclusion. NEVER writes."""
        ...

    async def write_after_turn(self, request: MemoryWriteRequest) -> MemoryWriteResult:
        """Process memory candidates and write after turn terminal. NEVER called mid-turn."""
        ...

    async def apply_forget_directive(self, directive: ForgetDirective) -> None:
        """Execute a forget directive immediately. Marks window, blocks extraction."""
        ...

    async def decay_expired(self) -> DecayResult:
        """Run periodic decay on negative emotions and short-term episodes."""
        ...
```

---

## 2. 完整输入输出对象

### 2.1 MemorySelectionRequest

ContextBuilder 调用 `load_for_turn()` 时传入。

```python
@dataclass(frozen=True)
class MemorySelectionRequest:
    """Input to MemorySystem.load_for_turn()."""

    user_id: str                               # required, 用户标识
    bot_id: str                                # required, bot 标识, 用于 bot-scoped 隔离
    turn_type: TurnType                        # required, turn 类型
    inbound_batch: InboundMessageBatch | None  # optional, 当前输入批次, 用于匹配 open threads
    recent_history: list[ConversationHistoryMessage]  # required, 最近已完成历史, 用于 episode 匹配
    relationship_state: RelationshipState | None      # optional, 当前关系状态
    forget_directives: list[ForgetDirective]          # required, 活跃的遗忘指令
    blocked_topics: list[str]                         # required, 永久屏蔽话题
    max_memories_per_category: int = 5                # optional, 每类最多数量
    max_total_memories: int = 25                      # optional, 总记忆上限
    system_time: datetime | None = None               # optional, 系统时间, 用于衰减计算
```

### 2.2 MemorySelection

`load_for_turn()` 返回。

```python
@dataclass(frozen=True)
class MemorySelection:
    """Output of MemorySystem.load_for_turn()."""

    profile_memory: list[MemoryRecord]         # 用户长期画像
    relationship_memory: list[MemoryRecord]    # 关系状态记忆
    boundary_memory: list[MemoryRecord]        # 用户明确边界 (最高优先级)
    episode_memory: list[MemoryRecord]         # 近期重要事件
    open_thread_memory: list[MemoryRecord]     # 未完成上下文线索
    sent_content_memory: list[SentContentRecord]  # 最近已发送内容 (for dedup, NOT relationship)
    selection_reasons: list[str]               # 为什么选了这些记忆
    total_memory_count: int                    # 总选中记忆数
    token_estimate: int                        # 字符估算, 便于 ContextBuilder 裁剪
```

### 2.3 MemoryWriteRequest

TurnEngine 在 turn 结束后调用 `write_after_turn()` 时传入。

```python
@dataclass(frozen=True)
class MemoryWriteRequest:
    """Input to MemorySystem.write_after_turn()."""

    user_id: str                               # required
    bot_id: str                                # required
    turn_id: str                               # required, 本轮 turn_id
    terminal_state: str                        # required, 本轮终态
    delivered_messages: list[TurnLocalSentMessage]  # required, 本轮已送达消息
    inbound_batch: InboundMessageBatch | None       # optional, 本轮输入
    context_snapshot: ContextSnapshot | None        # optional, 审计快照引用
    model_candidates: list[MemoryCandidate]         # optional, 模型建议的记忆候选
    forget_directives: list[ForgetDirective]        # required, 活跃遗忘指令
    blocked_topics: list[str]                       # required, 永久屏蔽话题
    system_time: datetime | None = None             # optional
```

### 2.4 MemoryWriteResult

```python
@dataclass(frozen=True)
class MemoryWriteResult:
    """Output of MemorySystem.write_after_turn()."""

    memories_written: list[str]          # 已写入的 memory_id 列表
    memories_discarded: int              # 被丢弃候选数
    memories_rejected_by_forget: int     # 被遗忘指令拦截数
    memories_rejected_by_duplicate: int  # 被去重拦截数
    memories_rejected_by_policy: int     # 被策略拦截数 (低重要性/禁写类型)
    sent_content_records: list[str]      # 已写入的 SentContentRecord ID 列表
    decay_applied: int                   # 应用衰减的记忆数
```

### 2.5 MemoryRecord

完整字段定义。这是存储在数据库中的结构化记忆条目。

```python
@dataclass
class MemoryRecord:
    """
    A single memory record in the system.

    Every field is either directly stored or computed at load time.
    Fields marked (persisted) are in the database table.
    Fields marked (computed) are derived at load_for_turn() time.
    """

    # ---- Identity ----
    memory_id: str                              # (persisted) required. Format: "mem_<uuid12>"
                                                #   示例: "mem_a1b2c3d4e5f6"

    # ---- Content ----
    category: MemoryCategory                    # (persisted) required. See MemoryCategory enum.
    content: str                                # (persisted) required. 记忆原文, 最多 2000 chars.
                                                #   示例: "用户喜欢在晚上喝热牛奶, 不喜欢咖啡"
    normalized_content: str                     # (computed) 规范化文本, 用于去重比较.
                                                #   去除多余空白, 全角转半角, 统一标点

    # ---- Source ----
    source_turn_id: str | None                  # (persisted) optional. 来源 turn_id.

    # ---- Timestamps ----
    created_at: datetime                        # (persisted) required. 创建时间.
    updated_at: datetime                        # (persisted) required. 最后更新时间.

    # ---- Retention Policy ----
    retention_policy: RetentionPolicy           # (persisted) required. 保留策略.
        #   long_term   — 永久保留, 除非手动删除或关系重置
        #   short_term  — 12 小时有效, 到期后 archived
        #   decays      — 存在衰减计时器, 到期后降级
        #   hidden      — 用户手动隐藏, 不进入上下文但保留记录

    # ---- Emotional and Relational Signals ----
    emotional_weight: float                     # (persisted) required. 范围 [-1.0, 1.0].
                                                #   -1.0 = 极度负面 (愤怒, 伤心)
                                                #    0.0 = 中性
                                                #   +1.0 = 极度正面 (开心, 温暖)
                                                #   示例: "用户今天很生气" → -0.7
                                                #   示例: "用户收到惊喜礼物" → +0.8

    relationship_importance: float              # (persisted) required. 范围 [0.0, 1.0].
                                                #   对关系维系的重要性.
                                                #   0.0 = 无关紧要的闲聊
                                                #   1.0 = 核心关系节点 (纪念日, 昵称, 长期承诺)
                                                #   示例: "用户外套是蓝色的" → 0.0
                                                #   示例: "用户昵称是宝宝" → 0.95

    # ---- Privacy and Visibility ----
    privacy_level: PrivacyLevel                 # (persisted) required. 隐私等级.
        #   normal          — 正常内容, 内容和 metadata 都可进入上下文
        #   sensitive       — 敏感内容, 只有 metadata 进入上下文, content 隐藏
        #   do_not_surface  — 绝对不进入模型上下文, 仅人工审核可见

    visibility: MemoryVisibility                # (persisted) required. 可见性状态.
        #   active                        — 正常活跃, 可进入上下文
        #   hidden_by_user                — 用户手动隐藏, 不进入上下文
        #   blocked_by_forget_directive   — 被遗忘指令屏蔽, 不进入上下文
        #   archived_by_relationship_reset — 关系重置归档, 不进入上下文

    user_forgettable: bool                      # (persisted) required. 用户是否可要求删除此记忆.
                                                #   True: 用户说"别记这个", 此记忆可被标记 hidden_by_user
                                                #   False: 系统级记忆, 用户不可操作

    # ---- Time-based Decay ----
    decay_at: datetime | None                   # (persisted) optional. 衰减到期时间.
                                                #   当前时间 >= decay_at 时, retention_policy 降级.
                                                #   示例: 负面情绪创建时 decay_at = created_at + 6h

    # ---- Scoping ----
    bot_id: str                                 # (persisted) required. bot 隔离键.
                                                #   "" 表示共享用户事实 (跨 bot 可见)
                                                #   "default_bot" 表示特定 bot 私有

    user_id: str                                # (persisted) required. 用户标识.

    # ---- Metadata ----
    tags: list[str]                             # (persisted) required. 标签列表.
                                                #   示例: ["食物", "偏好", "睡前习惯"]

    metadata: dict[str, Any]                    # (persisted) optional. 扩展 metadata.
                                                #   示例: {"original_expression": "...", "tone": "excited"}
```

### 2.6 MemoryCategory 枚举

```python
class MemoryCategory(str, Enum):
    PROFILE = "profile"              # 用户长期画像 (喜好, 习惯, 作息, 压力来源, 说话习惯)
    RELATIONSHIP = "relationship"    # 关系状态 (怎么哄, 讨厌什么语气, 调侃边界, 亲密度, 互动风格)
    BOUNDARY = "boundary"            # 用户明确边界 ("别烦我", "别这样说话", "不要主动找我")
    EPISODE = "episode"              # 近期重要事件 (今天很累, 刚说要去做某事, 今天心情不好)
    OPEN_THREAD = "open_thread"      # 未完成上下文线索 (说要做任务但未反馈, 晚点再聊, 发图片待追问)
```

### 2.7 SentContentRecord

```python
@dataclass
class SentContentRecord:
    """
    Recently sent content, for dedup only.
    NOT treated as relationship memory.
    """

    sent_content_id: str              # required. Format: "sc_<uuid12>"
    turn_id: str                      # required. 来源 turn_id.
    source: str                       # required.
                                      #   assistant_reply     — 普通回复
                                      #   assistant_proactive — 主动消息
                                      #   reminder_reply      — 提醒回复
    text: str                         # required. 原始发送文本.
    normalized_text: str              # required. 规范化文本 (去除空白, 统一标点, 全角转半角).
    fingerprint: str                  # required. 内容指纹 (CJK bigram set 的排序字符串或 hash).
    sent_at: datetime                 # required. 发送时间.
    bot_id: str                       # required. bot 隔离键.
    user_id: str                      # required. 用户标识.
    expires_at: datetime              # required. 过期时间 (默认 sent_at + 48h).
```

### 2.8 MemoryCandidate

模型或后台提取器提交的记忆建议。

```python
@dataclass
class MemoryCandidate:
    """
    A memory writing suggestion from the model or extractor.

    The model CAN propose, but MemorySystem MAKES the final write decision.
    """

    content: str                                  # required. 建议记忆内容.
    category: MemoryCategory                      # required. 建议分类.
    emotional_weight: float = 0.0                 # optional. 建议情感权重 [-1.0, 1.0].
    relationship_importance: float = 0.0          # optional. 建议关系重要性 [0.0, 1.0].
    privacy_level: PrivacyLevel = PrivacyLevel.NORMAL  # optional.
    retention_policy: RetentionPolicy | None = None    # optional. 建议保留策略.
    user_forgettable: bool = True                 # optional. 是否可由用户删除.
    write_decision: WriteDecision = WriteDecision.MAYBE_WRITE  # optional. extractor 建议.
        #   must_write    — extractor 认为必须写入
        #   should_write  — extractor 认为应该写入
        #   maybe_write   — extractor 不确定
        #   do_not_write  — extractor 明确反对写入
    reason: str = ""                              # optional. 建议理由.
    source_turn_id: str | None = None             # optional. 来源 turn_id.
    suggested_tags: list[str] | None = None       # optional. extractor 建议的标签.
```

### 2.9 WriteDecision 枚举

```python
class WriteDecision(str, Enum):
    MUST_WRITE = "must_write"         # 必须写入 (extractor 高置信度)
    SHOULD_WRITE = "should_write"     # 应该写入 (extractor 中置信度)
    MAYBE_WRITE = "maybe_write"       # 不确定 (extractor 低置信度)
    DO_NOT_WRITE = "do_not_write"     # 明确反对 (extractor 认为不应写入)
```

### 2.10 ForgetDirective

```python
@dataclass(frozen=True)
class ForgetDirective:
    """
    A "don't remember this" command from the user.

    Executed immediately. Blocks memory extraction for ±5 turns around the directive.
    Repeated directives on the same topic → permanent topic block.
    """

    directive_id: str                        # required. Format: "fd_<uuid12>"
    detected_at_turn_id: str                 # required. 被检测到的 turn_id.
    scope: ForgetScope                       # required. 忘记范围.
        #   local_window          — 仅屏蔽 ±5 turns 内记忆
        #   topic                 — 屏蔽整个话题类别的未来记忆
        #   relationship_reset    — 触发关系重置
    window_before: int = 5                   # 指令前屏蔽 turn 数.
    window_after: int = 5                    # 指令后屏蔽 turn 数.
    blocked_topic: str | None = None         # optional. 被屏蔽的话题名.
    permanent_topic_block: bool = False      # 是否进入永久屏蔽列表.
    response_style: str = "gentle"           # 回复风格提示 (gentle / neutral / firm).
    created_at: datetime                     # 创建时间.
    user_id: str                             # 用户标识.
    bot_id: str                              # bot 标识.
```

### 2.11 ForgetScope 枚举

```python
class ForgetScope(str, Enum):
    LOCAL_WINDOW = "local_window"           # ±5 turns
    TOPIC = "topic"                         # 整类话题
    RELATIONSHIP_RESET = "relationship_reset"  # 关系重置
```

### 2.12 MemoryVisibility 枚举

```python
class MemoryVisibility(str, Enum):
    ACTIVE = "active"                                        # 正常活跃
    HIDDEN_BY_USER = "hidden_by_user"                        # 用户手动隐藏
    BLOCKED_BY_FORGET_DIRECTIVE = "blocked_by_forget_directive"  # 被遗忘指令屏蔽
    ARCHIVED_BY_RELATIONSHIP_RESET = "archived_by_relationship_reset"  # 关系重置归档
```

### 2.13 DecayResult

```python
@dataclass
class DecayResult:
    """Result of running decay on memories."""

    negative_emotions_decayed: int   # 负面情绪衰减数
    short_term_expired: int          # 短期事件到期数
    episodes_archived: int           # 归档的 episode 数
    sent_content_expired: int        # SentContentRecord 过期清理数
    total_affected: int              # 总影响记忆数
```

---

## 3. 不可违反的不变量

以下不变量是 hard constraints。任何实现违反其中一条即为 defective。

### INV-1: 记忆写入时机

```
Memory writing happens ONLY after turn terminal state is reached.
Writing during a turn is NEVER allowed.
```

- `write_after_turn()` 只能由 TurnEngine 在 `CHECK_TERMINAL` → `POST_TURN_MEMORY` 阶段调用。
- 在 `CALL_MODEL` / `EXECUTE_TOOL` / `UPDATE_TURN_LOCAL_STATE` 阶段禁止任何写操作。
- 这是为了防止模型本轮输出被不当记入长期记忆，或遗忘指令未被处理就写入。

### INV-2: ForgetDirective 窗口阻塞

```
ForgetDirective blocks memory extraction for ±5 turns around the directive's detected_at_turn_id.
```

- 假设 `directive.detected_at_turn_id` 对应 `turn_number = 100`，则 `[95, 105]` 内的所有 turn 都不允许抽取记忆。
- This includes BOTH turns before the directive (the context the user wants forgotten) AND turns after (to give the system time to adjust).
- 窗口内的 turn 仍然记录 L1 raw messages 和 L2 turns，但 L4 long_term_memory 写入被跳过。
- 这通过检查 `turn_number` 或时间范围实现。

### INV-3: 负面情绪 6 小时衰减

```
Negative emotions (emotional_weight < -0.3) auto-decay after 6 hours.
Decayed negative emotions are NOT proactively surfaced.
```

- 创建时设置 `decay_at = created_at + timedelta(hours=6)`。
- `load_for_turn()` 检查 `decay_at` 是否已过：
  - 已过 `decay_at` 且 `emotional_weight < -0.3`：设置 `visibility = archived`，
    不进入上下文。仅当用户再次提起相同话题时才重新激活。
- 衰减不可逆：一旦衰减，不能通过同一事件再次升高 `emotional_weight`。

### INV-4: 短期 episode 12 小时过期

```
Short-term episodes (retention_policy == short_term) expire after 12 hours.
Expired episodes are NOT surfaced in context.
```

- 创建时设置 `decay_at = created_at + timedelta(hours=12)`。
- `load_for_turn()` 检查 `decay_at` 是否已过：
  - 已过：`visibility = archived`，不进入上下文。

### INV-5: boundary_memory 最高优先级

```
boundary_memory ALWAYS enters context if relevant to the current turn.
No other memory category can override or suppress boundary_memory.
```

- 在 `load_for_turn()` 的排序中，boundary 永远排第一。
- 如果 boundary 和其他记忆冲突，boundary 覆盖其他记忆。
- 示例：profile 说"用户喜欢被开玩笑"，boundary 说"不要再开这种玩笑了"，
  上下文应同时包含两者且 boundary 标注为更高优先级。

### INV-6: sent_content_memory 仅用于去重

```
sent_content_memory is for dedup purposes ONLY.
It is NOT treated as relationship memory, profile memory, or any other category.
It does NOT inform the model about "what the assistant said" in a relational sense.
```

- SentContentRecord 独立存储，不混入任何 MemoryRecord 表。
- `load_for_turn()` 返回的 `sent_content_memory` 放在独立字段中。
- ContextBuilder 使用时单独渲染为一个 "recently sent" block，不混入 "relationship memory" block。

### INV-7: 隐藏/屏蔽记忆绝不进入模型上下文

```
Memories with visibility != active MUST NOT enter model context.
This includes: hidden_by_user, blocked_by_forget_directive, archived_by_relationship_reset.
```

- `load_for_turn()` 必须在加载后过滤掉所有 `visibility != active` 的记录。
- 过滤发生在排序和截断之前，确保不存在 race condition。

### INV-8: do_not_surface 记忆绝不进入模型上下文

```
Memories with privacy_level == do_not_surface MUST NOT enter model context.
Not even metadata.
```

- `load_for_turn()` 必须过滤掉所有 `privacy_level == do_not_surface` 的记录。
- 这些记忆仅存在于数据库中，人类管理员可在管理台查看。

### INV-9: 模型建议, 系统决策

```
The model/external extractor CAN suggest MemoryCandidates.
The MemorySystem makes the FINAL write decision.
```

- `write_after_turn()` 接收 `model_candidates` 列表。
- 每个 candidate 必须经过：forget directive 检查 → 禁写内容检查 → 去重 → 重要性阈值 → 保留策略 → 写入。
- 即使 candidate 标记为 `MUST_WRITE`，系统仍可拒绝。

### INV-10: 主动消息前必须检查 sent content 指纹

```
Sent content fingerprint comparison MUST happen before proactive send.
```

- SchedulerRuntime 在触发主动 turn 之前应通过 MemorySystem 查询最近的 SentContentRecord。
- 如果即将发送的内容与最近的 SentContentRecord 高度相似 (Jaccard >= 0.75)，
  不触发主动 turn。
- 这是硬性的防重复机制，不依赖模型自行判断。

### INV-11: 吵架内容禁入长期记忆

```
Fight words, hurtful words, temporary conflict expressions NEVER enter long-term memory.
```

- `write_after_turn()` 必须检测输入和输出中是否包含吵架/伤害性内容。
- 检测到后，相关 candidate 的 `retention_policy` 强制设为 `short_term` 或直接丢弃。
- 这不是可配置策略，是硬规则。

### INV-12: 亲密对话原文禁入长期记忆

```
Intimate conversation transcripts NEVER enter long-term memory.
```

- `write_after_turn()` 必须检测输入和输出中是否包含亲密对话原文。
- 可以保留高度抽象的 summary (例如 "用户和助手进行了亲密对话")，但原始文本不写入。
- 这与 `privacy_level = sensitive` 不同——敏感记忆包含 content 但不在上下文展示；
  这条规则是完全不写入 content 到 L4 long_term_memory。

### INV-13: Bot 隔离

```
Memories are keyed by bot_id.
Bot A cannot see bot B's memories (unless marked as shared with bot_id='').
```

- `load_for_turn()` 的查询必须带 `bot_id` 过滤。
- 共享用户事实使用 `bot_id = ''`，所有 bot 可见。
- `write_after_turn()` 必须写入正确的 `bot_id`。

### INV-14: privacy_level 控制上下文暴露粒度

```
privacy_level controls whether memory CONTENT or only METADATA enters model context.
```

- `privacy_level == normal`：content + all metadata 可进入上下文。
- `privacy_level == sensitive`：仅 category + normalized_content 摘要 + tags 进入上下文，
  content 本身替换为 "[sensitive memory — content hidden]"。
- `privacy_level == do_not_surface`：完全不进入上下文。

### INV-15: 遗忘指令屏蔽的记忆不可被模型提取

```
Memories that fall within a forget directive's window MUST NOT be surfaced.
Memory selection for context is handled entirely by MemorySystem.load_for_turn().
There is NO search_memory tool exposed to the model in the current version.
```
- 当前版本不暴露 `search_memory` 工具。模型无法主动查询记忆。
- 记忆选择和过滤完全由 `MemorySystem.load_for_turn()` 在 turn 开始前完成。
- `load_for_turn()` 必须先过滤掉 `visibility == blocked_by_forget_directive` 的记录。
- 所有通过 load_for_turn() 返回的记忆已经过隐私、遗忘指令、衰减、可见性过滤。
- 未来版本如需暴露 `search_memory`，必须通过 ToolExposurePolicy 定义完整的暴露条件、隐私过滤、forget directive 拦截规则，并纳入测试矩阵。

### INV-16: 每条 sent content 有唯一指纹

```
Every SentContentRecord MUST have a fingerprint computed at write time.
Fingerprint is used for dedup comparison before proactive sends.
```

- 指纹算法：CJK bigram set → 排序 → SHA256 前 16 hex → `"fp_" + hex16`。
- 或直接存储 CJK bigram set 的 sorted string 用于 Jaccard 快速比较。

---

## 4. MemoryRecord 完整字段

(已在 section 2.5 中完整定义, 此处不再重复)

附加说明：

### 4.1 字段类型约束

| 字段 | 类型 | 约束 |
|------|------|------|
| memory_id | str | `^mem_[a-f0-9]{12}$` |
| category | MemoryCategory | 必须是枚举成员 |
| content | str | 1–2000 chars |
| normalized_content | str | 由 `content` 派生 |
| source_turn_id | str\|None | nullable |
| created_at | datetime | 自动设置, 不可修改 |
| updated_at | datetime | 自动更新, 不可回退 |
| retention_policy | RetentionPolicy | 默认 `short_term` |
| emotional_weight | float | [-1.0, 1.0], 精度 0.01 |
| relationship_importance | float | [0.0, 1.0], 精度 0.01 |
| privacy_level | PrivacyLevel | 默认 `normal` |
| visibility | MemoryVisibility | 默认 `active` |
| user_forgettable | bool | 默认 `True` |
| decay_at | datetime\|None | nullable |
| bot_id | str | NOT NULL, default `''` |
| user_id | str | NOT NULL |
| tags | list[str] | 至少 1 个, 最多 10 个 |
| metadata | dict[str, Any] | nullable |

### 4.2 字段示例

```python
# Example: 用户昵称
MemoryRecord(
    memory_id="mem_a1b2c3d4e5f6",
    category=MemoryCategory.PROFILE,
    content="用户喜欢被叫'宝宝', 但不喜欢在别人面前被这样叫",
    normalized_content="用户喜欢被叫宝宝 但不喜欢在别人面前被这样叫",
    source_turn_id="turn_abc123",
    created_at=datetime(2026, 6, 3, 10, 30, 0),
    updated_at=datetime(2026, 6, 3, 10, 30, 0),
    retention_policy=RetentionPolicy.LONG_TERM,
    emotional_weight=0.3,
    relationship_importance=0.85,
    privacy_level=PrivacyLevel.NORMAL,
    visibility=MemoryVisibility.ACTIVE,
    user_forgettable=True,
    decay_at=None,
    bot_id="default_bot",
    user_id="user_001",
    tags=["昵称", "偏好", "社交边界"],
    metadata={},
)

# Example: 今天负面情绪
MemoryRecord(
    memory_id="mem_z9y8x7w6v5u4",
    category=MemoryCategory.EPISODE,
    content="用户今天被老板批评了, 心情非常不好, 想早点休息",
    normalized_content="用户今天被老板批评了 心情非常不好 想早点休息",
    source_turn_id="turn_def456",
    created_at=datetime(2026, 6, 3, 14, 0, 0),
    updated_at=datetime(2026, 6, 3, 14, 0, 0),
    retention_policy=RetentionPolicy.DECAYS,
    emotional_weight=-0.7,
    relationship_importance=0.4,
    privacy_level=PrivacyLevel.NORMAL,
    visibility=MemoryVisibility.ACTIVE,
    user_forgettable=True,
    decay_at=datetime(2026, 6, 3, 20, 0, 0),  # 6h later
    bot_id="default_bot",
    user_id="user_001",
    tags=["负面情绪", "工作", "压力"],
    metadata={"original_expression": "我今天真的烦死了", "tone": "frustrated"},
)
```

---

## 5. MemoryCandidate 结构

(已在 section 2.8 中定义)

### 5.1 Decision 流程

```text
model/extractor produces MemoryCandidate
  → candidate.write_decision is SUGGESTION only
  → MemorySystem.write_after_turn() evaluates:
      1. Is turn within forget directive window? → REJECT
      2. Is content in blocked_topics list? → REJECT
      3. Is content a fight word / hurtful word? → retain as short_term ONLY, NORMALIZE content
      4. Is content intimate transcript? → REJECT (do not write at all)
      5. Is content duplicate of existing? → REJECT
      6. Is relationship_importance below threshold (0.2)? → REJECT
      7. Apply retention policy based on category + emotional_weight
      8. Compute normalized_content + tags
      9. Set decay_at if applicable
      10. WRITE
```

### 5.2 模型如何生成 candidate

模型通过 context 中的 `memory_policy.md` 获知记忆规则。
模型可以在 `finish_turn` 调用中附带 `memory_candidates` 参数（未来扩展）。

当前实现中，MemoryWorker 在 turn 结束后异步运行独立的 extraction LLM call 生成 candidates。

---

## 6. 记忆选择伪代码: load_for_turn()

```python
async def load_for_turn(self, request: MemorySelectionRequest) -> MemorySelection:
    """
    Phase 1: Select memories for context assembly.

    Steps:
      1. Load boundary_memory (ALWAYS, highest priority)
      2. Load relationship_memory
      3. Load profile_memory
      4. Load episode_memory (filter expired: decay_at > now)
      5. Load open_thread_memory
      6. Load sent_content_memory (expires_at > now)
      7. Apply visibility filter (only active)
      8. Apply privacy_level filter (do_not_surface removed)
      9. Apply forget directive blocking
      10. Dedup within and across categories
      11. Sort: boundary > relationship > profile > episode > open_thread
      12. Truncate per category and total limits
      13. Compute token_estimate
      14. Return MemorySelection
    """

    # ---- Step 1: Load boundary ----
    boundary = await self.store.query_memories(
        user_id=request.user_id,
        bot_id=request.bot_id,
        category=MemoryCategory.BOUNDARY,
        visibility=MemoryVisibility.ACTIVE,
    )

    # ---- Step 2: Load relationship ----
    relationship = await self.store.query_memories(
        user_id=request.user_id,
        bot_id=request.bot_id,
        category=MemoryCategory.RELATIONSHIP,
        visibility=MemoryVisibility.ACTIVE,
    )

    # ---- Step 3: Load profile ----
    profile = await self.store.query_memories(
        user_id=request.user_id,
        bot_id=request.bot_id,
        category=MemoryCategory.PROFILE,
        visibility=MemoryVisibility.ACTIVE,
    )

    # ---- Step 4: Load episodes, filter expired ----
    now = request.system_time or datetime.utcnow()
    episodes_raw = await self.store.query_memories(
        user_id=request.user_id,
        bot_id=request.bot_id,
        category=MemoryCategory.EPISODE,
        visibility=MemoryVisibility.ACTIVE,
    )
    episodes = []
    for ep in episodes_raw:
        # Filter out expired short-term episodes
        if ep.retention_policy == RetentionPolicy.SHORT_TERM and ep.decay_at and ep.decay_at <= now:
            continue  # Expired, do not surface
        # Check decay timers for "decays" retention policy
        if ep.retention_policy == RetentionPolicy.DECAYS and ep.decay_at and ep.decay_at <= now:
            # Negative emotion decay logic
            if ep.emotional_weight < -0.3:
                # Auto-archive: update visibility in DB
                await self.store.update_memory_visibility(
                    ep.memory_id, MemoryVisibility.ARCHIVED_BY_RELATIONSHIP_RESET
                )
                continue  # Do not surface
            else:
                # Non-negative decays: still include but mark as fading
                ep.metadata["decay_status"] = "fading"

        # Filter out negative emotions past their decay_at
        if ep.emotional_weight < -0.3 and ep.decay_at and ep.decay_at <= now:
            continue  # Do not surface decayed negative emotions

        episodes.append(ep)

    # ---- Step 5: Load open threads ----
    open_threads = await self.store.query_memories(
        user_id=request.user_id,
        bot_id=request.bot_id,
        category=MemoryCategory.OPEN_THREAD,
        visibility=MemoryVisibility.ACTIVE,
    )

    # ---- Step 6: Load sent content ----
    sent_content = await self.store.query_sent_content(
        user_id=request.user_id,
        bot_id=request.bot_id,
        expires_after=now,  # Only non-expired
        limit=20,
    )

    # ---- Step 7: Apply forget directive blocking ----
    # Any memory whose source_turn_id falls within ANY forget directive's
    # [turn_number - window_before, turn_number + window_after] window
    # and the directive is still active → blocked, not selected.
    blocked_turn_numbers = self._compute_blocked_turns(
        directives=request.forget_directives,
        user_id=request.user_id,
        bot_id=request.bot_id,
    )

    def _is_blocked(mem: MemoryRecord) -> bool:
        if mem.visibility != MemoryVisibility.ACTIVE:
            return True  # Already non-active
        if mem.privacy_level == PrivacyLevel.DO_NOT_SURFACE:
            return True  # INV-8
        if mem.tags and any(tag in request.blocked_topics for tag in mem.tags):
            return True  # Permanently blocked topic
        # Check if source_turn_id is in blocked window
        if mem.source_turn_id and mem.source_turn_id in blocked_turn_numbers:
            return True
        return False

    boundary = [m for m in boundary if not _is_blocked(m)]
    relationship = [m for m in relationship if not _is_blocked(m)]
    profile = [m for m in profile if not _is_blocked(m)]
    episodes = [m for m in episodes if not _is_blocked(m)]
    open_threads = [m for m in open_threads if not _is_blocked(m)]

    # ---- Step 8: Dedup within each category ----
    boundary = self._dedup_memories(boundary)
    relationship = self._dedup_memories(relationship)
    profile = self._dedup_memories(profile)
    episodes = self._dedup_memories(episodes)
    open_threads = self._dedup_memories(open_threads)

    # ---- Step 9: Sort ----
    # boundary: keep all (highest priority)
    # relationship: sort by relationship_importance desc
    # profile: sort by relationship_importance desc
    # episodes: sort by relationship_importance desc, then created_at desc
    # open_threads: sort by created_at desc

    relationship.sort(key=lambda m: m.relationship_importance, reverse=True)
    profile.sort(key=lambda m: m.relationship_importance, reverse=True)
    episodes.sort(key=lambda m: (m.relationship_importance, m.created_at), reverse=True)
    open_threads.sort(key=lambda m: m.created_at, reverse=True)

    # ---- Step 10: Truncate ----
    max_per = request.max_memories_per_category
    boundary = boundary[:max_per]       # Keep all boundary (cap at max_per as safety)
    relationship = relationship[:max_per]
    profile = profile[:max_per]
    episodes = episodes[:max_per]
    open_threads = open_threads[:max_per]

    # Total cap
    total = len(boundary) + len(relationship) + len(profile) + len(episodes) + len(open_threads)
    if total > request.max_total_memories:
        # Trim from lowest priority first: open_threads → episodes → profile → relationship → boundary
        overflow = total - request.max_total_memories
        for lst in [open_threads, episodes, profile, relationship]:
            while overflow > 0 and lst:
                lst.pop()  # Remove lowest priority
                overflow -= 1

    # ---- Step 11: Compute token estimate ----
    token_estimate = self._estimate_tokens(
        boundary + relationship + profile + episodes + open_threads
    )

    # ---- Step 12: Build selection reasons ----
    reasons = []
    if boundary:
        reasons.append(f"Loaded {len(boundary)} boundary memories (highest priority)")
    if relationship:
        reasons.append(f"Loaded {len(relationship)} relationship memories")
    if profile:
        reasons.append(f"Loaded {len(profile)} profile memories")
    if episodes:
        reasons.append(f"Loaded {len(episodes)} episode memories (filtered expired)")
    if open_threads:
        reasons.append(f"Loaded {len(open_threads)} open thread memories")

    blocked_count = len(blocked_turn_numbers)
    if blocked_count > 0:
        reasons.append(f"Blocked {blocked_count} turn windows due to forget directives")

    return MemorySelection(
        profile_memory=profile,
        relationship_memory=relationship,
        boundary_memory=boundary,
        episode_memory=episodes,
        open_thread_memory=open_threads,
        sent_content_memory=sent_content,
        selection_reasons=reasons,
        total_memory_count=total,
        token_estimate=token_estimate,
    )
```

---

## 7. 记忆写入伪代码: write_after_turn()

```python
async def write_after_turn(self, request: MemoryWriteRequest) -> MemoryWriteResult:
    """
    Phase 2: Process memory candidates and write after turn terminal.

    Steps:
      1. Check if current turn is within any forget directive window → skip all candidates
      2. Extract candidates from turn content (model_candidates + internal extraction)
      3. For each candidate:
         a. Check against blocked_topics
         b. Check content type: fight words? intimate transcript?
         c. Classify retention_policy
         d. Check against forbidden types for long-term
         e. Apply emotional decay timer if negative
         f. Compute relationship_importance
         g. Dedup against existing
         h. Write or discard
      4. Generate SentContentRecords for delivered messages
      5. Update decay timers on existing memories
      6. Return result
    """

    # ---- Step 1: Check forget directive window ----
    now = request.system_time or datetime.utcnow()

    # Determine if current turn is blocked
    current_turn_number = await self.store.get_turn_number(request.turn_id)
    is_blocked = self._is_turn_blocked_by_forget_directive(
        turn_number=current_turn_number,
        directives=request.forget_directives,
    )

    result = MemoryWriteResult(
        memories_written=[],
        memories_discarded=0,
        memories_rejected_by_forget=0,
        memories_rejected_by_duplicate=0,
        memories_rejected_by_policy=0,
        sent_content_records=[],
        decay_applied=0,
    )

    if is_blocked:
        # All candidates rejected
        # Still write sent content records (dedup is separate from memory writing)
        sc_ids = await self._write_sent_content_records(
            delivered_messages=request.delivered_messages,
            turn_id=request.turn_id,
            user_id=request.user_id,
            bot_id=request.bot_id,
            now=now,
        )
        result.sent_content_records = sc_ids
        result.memories_rejected_by_forget = len(request.model_candidates)
        return result

    # ---- Step 2: Gather candidates ----
    all_candidates = list(request.model_candidates or [])

    # Internal extraction may add more candidates based on turn content
    # (future: internal extraction LLM call in background)

    # ---- Step 3: Process each candidate ----
    for candidate in all_candidates:
        # Level 1: ignore?
        if candidate.write_decision == WriteDecision.DO_NOT_WRITE:
            result.memories_rejected_by_policy += 1
            continue

        # Level 2: blocked topic?
        if candidate.suggested_tags:
            for tag in candidate.suggested_tags:
                if tag in request.blocked_topics:
                    result.memories_rejected_by_forget += 1
                    continue  # Skip to next candidate

        # Level 3: content type check
        write_as = self._classify_content_type(candidate.content)
        # write_as can be: "long_term", "short_term", "decays", "reject"
        if write_as == "reject":
            result.memories_discarded += 1
            continue

        # Level 4: fight words / hurtful words?
        if self._contains_fight_or_hurtful_content(candidate.content):
            # Downgrade to short_term ONLY if content is valuable for context
            # Otherwise reject entirely
            if candidate.write_decision == WriteDecision.MUST_WRITE:
                write_as = "short_term"
            else:
                result.memories_discarded += 1
                continue

        # Level 5: intimate transcript?
        if self._contains_intimate_content(candidate.content):
            # Reject entirely per INV-12
            result.memories_discarded += 1
            continue

        # Level 6: Determine retention_policy
        retention = candidate.retention_policy
        if retention is None:
            retention = self._default_retention_for_category(
                category=candidate.category,
                emotional_weight=candidate.emotional_weight,
                relationship_importance=candidate.relationship_importance,
                content_type=write_as,
            )

        # Force override: write_as from content type check
        if write_as == "short_term":
            retention = RetentionPolicy.SHORT_TERM
        elif write_as == "decays":
            retention = RetentionPolicy.DECAYS

        # Level 7: Long-term memory gate — strict filter
        if retention == RetentionPolicy.LONG_TERM:
            if not self._qualifies_for_long_term(
                category=candidate.category,
                relationship_importance=candidate.relationship_importance,
                emotional_weight=candidate.emotional_weight,
                content=candidate.content,
            ):
                # Downgrade to short-term
                retention = RetentionPolicy.SHORT_TERM

        # Level 8: Importance threshold
        if candidate.relationship_importance < 0.2 and candidate.write_decision != WriteDecision.MUST_WRITE:
            result.memories_rejected_by_policy += 1
            continue

        # Level 9: Normalize content
        normalized = self._normalize_text(candidate.content)

        # Level 10: Dedup against existing
        existing_memories = await self.store.query_memories_by_content_bigrams(
            user_id=request.user_id,
            bot_id=request.bot_id,
            normalized_content=normalized,
            threshold=0.75,
        )
        if existing_memories:
            # Update existing instead of creating new
            existing = existing_memories[0]
            await self.store.update_memory(
                memory_id=existing.memory_id,
                relationship_importance=max(
                    existing.relationship_importance,
                    candidate.relationship_importance,
                ),
                emotional_weight=(
                    existing.emotional_weight * 0.7
                    + candidate.emotional_weight * 0.3
                ),
                tags=self._merge_tags(existing.tags, candidate.suggested_tags or []),
                updated_at=now,
            )
            result.memories_rejected_by_duplicate += 1
            continue

        # Level 11: Compute decay_at
        decay_at = self._compute_decay_at(
            retention=retention,
            emotional_weight=candidate.emotional_weight,
            now=now,
        )

        # Level 12: Compute tags
        tags = candidate.suggested_tags or self._auto_tag(
            content=candidate.content,
            category=candidate.category,
        )

        # Level 13: WRITE
        try:
            mem_id = await self.store.insert_memory_record(
                category=candidate.category,
                content=candidate.content,
                normalized_content=normalized,
                source_turn_id=request.turn_id,
                retention_policy=retention,
                emotional_weight=candidate.emotional_weight,
                relationship_importance=candidate.relationship_importance,
                privacy_level=candidate.privacy_level,
                visibility=MemoryVisibility.ACTIVE,
                user_forgettable=candidate.user_forgettable,
                decay_at=decay_at,
                bot_id=request.bot_id,
                user_id=request.user_id,
                tags=tags,
                metadata={
                    "source": "memory_system",
                    "write_decision_suggested": candidate.write_decision.value,
                },
            )
            result.memories_written.append(mem_id)
        except Exception:
            result.memories_discarded += 1

    # ---- Step 4: Write SentContentRecords ----
    sc_ids = await self._write_sent_content_records(
        delivered_messages=request.delivered_messages,
        turn_id=request.turn_id,
        user_id=request.user_id,
        bot_id=request.bot_id,
        now=now,
    )
    result.sent_content_records = sc_ids

    # ---- Step 5: Update decay timers ----
    decayed = await self.decay_expired()
    result.decay_applied = decayed.total_affected

    return result


def _qualifies_for_long_term(
    self,
    category: MemoryCategory,
    relationship_importance: float,
    emotional_weight: float,
    content: str,
) -> bool:
    """
    Strict gate for long-term memory.

    Only allows:
    - 专属昵称与爱称
    - 双方特殊称呼方式
    - 确定关系的日期 / 重要纪念日
    - 第一次聊天/语音/视频等核心情感节点
    - 核心喜好 (> 0.6 importance)
    - 核心雷区 (> 0.6 importance)
    - 长期约定与承诺
    - 重要人生事件 (生日, 考试, 升职, 家人重要事情)

    Rejects:
    - 吵架气话
    - 伤人的话
    - 临时冲突表达 ("我不想理你了")
    - 过于私密的身体细节
    - 亲密对话原文
    - 负面情绪和脆弱时刻 (除非用户明确希望记住)
    - 临时小矛盾
    - 临时误会
    """

    # Hard floor: relationship_importance must be >= 0.6
    if relationship_importance < 0.6:
        return False

    # Emotional guard: very negative emotions are short-term only
    if emotional_weight < -0.5:
        return False

    # Content pattern checks
    fight_patterns = [
        "分手", "不理你了", "不想和你说话", "离我远点",
        "烦死了", "别烦我", "走开", "滚",
    ]
    for p in fight_patterns:
        if p in content:
            return False

    return True
```

---

## 8. ForgetDirective 处理

### 8.1 完整伪代码

```python
async def apply_forget_directive(self, directive: ForgetDirective) -> None:
    """
    Execute a forget directive.

    Steps:
      1. Persist the directive.
      2. Compute the blocked turn window.
      3. Mark all memories within the window as blocked_by_forget_directive.
      4. If scope == TOPIC: add topic to permanent blocked topics list.
      5. If scope == RELATIONSHIP_RESET: archive all relationship memories.
      6. Log the action for audit.
    """

    # 1. Persist
    await self.store.insert_forget_directive(directive)

    # 2. Compute blocked turns
    directive_turn_number = await self.store.get_turn_number(
        directive.detected_at_turn_id
    )
    if directive_turn_number is not None:
        blocked_start = directive_turn_number - directive.window_before
        blocked_end = directive_turn_number + directive.window_after

    # 3. Mark memories
    if directive.scope == ForgetScope.LOCAL_WINDOW:
        # Mark memories whose source_turn_id falls within [blocked_start, blocked_end]
        blocked_turn_ids = await self.store.get_turn_ids_in_range(
            user_id=directive.user_id,
            bot_id=directive.bot_id,
            turn_number_start=blocked_start,
            turn_number_end=blocked_end,
        )
        for turn_id in blocked_turn_ids:
            await self.store.update_memory_visibility_by_source_turn(
                source_turn_id=turn_id,
                new_visibility=MemoryVisibility.BLOCKED_BY_FORGET_DIRECTIVE,
            )

    elif directive.scope == ForgetScope.TOPIC:
        # 4. Add to permanent blocked topics
        if directive.blocked_topic:
            await self.store.add_blocked_topic(
                user_id=directive.user_id,
                bot_id=directive.bot_id,
                topic=directive.blocked_topic,
            )
        # Also block ±5 window
        blocked_turn_ids = await self.store.get_turn_ids_in_range(
            user_id=directive.user_id,
            bot_id=directive.bot_id,
            turn_number_start=blocked_start,
            turn_number_end=blocked_end,
        )
        for turn_id in blocked_turn_ids:
            await self.store.update_memory_visibility_by_source_turn(
                source_turn_id=turn_id,
                new_visibility=MemoryVisibility.BLOCKED_BY_FORGET_DIRECTIVE,
            )

    elif directive.scope == ForgetScope.RELATIONSHIP_RESET:
        # 5. Archive all relationship + profile memories
        # Mark them as archived_by_relationship_reset, NOT deleted
        await self.store.archive_memories_by_relationship_reset(
            user_id=directive.user_id,
            bot_id=directive.bot_id,
        )
        # Reset relationship state to initial values
        await self.store.reset_relationship_state(
            user_id=directive.user_id,
            bot_id=directive.bot_id,
        )

    # 6. Log
    logger.info(
        f"ForgetDirective executed: {directive.directive_id}, "
        f"scope={directive.scope.value}, "
        f"user={directive.user_id}, bot={directive.bot_id}"
    )


async def _detect_repeated_forget_directive(
    self,
    directive: ForgetDirective,
    recent_directives: list[ForgetDirective],
) -> bool:
    """
    Check if the same topic has been mentioned in recent forget directives.

    If user has asked "don't mention X" more than twice within a short period
    (e.g., 7 days), escalate: add X to permanent blocked topics.

    Returns True if this is a repeated directive that triggers permanent block.
    """
    if not directive.blocked_topic:
        return False

    now = datetime.utcnow()
    recent_same_topic = [
        d for d in recent_directives
        if d.blocked_topic == directive.blocked_topic
        and (now - d.created_at).total_seconds() < 7 * 24 * 3600
    ]

    if len(recent_same_topic) >= 2:  # This is the 3rd+ time
        # Trigger permanent block
        await self.store.add_blocked_topic(
            user_id=directive.user_id,
            bot_id=directive.bot_id,
            topic=directive.blocked_topic,
        )
        directive.permanent_topic_block = True
        return True

    return False
```

### 8.2 回复模板

系统回复用户 "别记这个" 时的文本由 persona 控制，但必须满足：

- 温柔、不追问。
- 不固定文案。
- 自动转移话题。

推荐风格示例：

```text
"好呀宝宝，我听你的，刚才那些我不记了。"
"嗯嗯，已经忘掉啦。"
"好的，我不记这些了。"
```

---

## 9. 情绪衰减逻辑

### 9.1 负面情绪衰减函数

```python
def compute_emotional_decay(
    emotional_weight: float,
    created_at: datetime,
    now: datetime | None = None,
) -> tuple[float, bool]:
    """
    Compute decayed emotional weight.

    Rules:
    - Negative emotions (weight < -0.3): decay with 6-hour half-life.
    - After decay_at (created_at + 6h), weight approaches 0.
    - Once weight >= -0.15, the emotion is considered "neutralized" and the
      memory should be archived.

    Returns:
      (decayed_weight, should_surface)
    """
    if emotional_weight >= -0.3:
        # Non-negative: no decay
        return emotional_weight, True

    now = now or datetime.utcnow()
    elapsed_hours = (now - created_at).total_seconds() / 3600.0

    if elapsed_hours < 6.0:
        # Still in the decay window
        # Linear interpolation: from original weight to 0 over 6 hours
        # After 3h: weight ~= original_weight * 0.5
        progress = elapsed_hours / 6.0  # 0.0 → 1.0
        decayed = emotional_weight * (1.0 - progress)
        # If weight is nearly neutral, still surface (don't drop abruptly)
        should_surface = decayed < -0.15  # Surface if still clearly negative
        return round(decayed, 2), should_surface
    else:
        # Past 6 hours: fully decayed
        return 0.0, False  # Do NOT surface
```

### 9.2 短期事件过期函数

```python
def is_episode_expired(
    created_at: datetime,
    retention_policy: RetentionPolicy,
    decay_at: datetime | None,
    now: datetime | None = None,
) -> bool:
    """
    Check if a short-term episode should be expired.

    Rules:
    - SHORT_TERM: expires after 12 hours from created_at.
    - DECAYS: expires when decay_at is reached.
    - LONG_TERM: never expires.
    """
    now = now or datetime.utcnow()

    if retention_policy == RetentionPolicy.LONG_TERM:
        return False

    if retention_policy == RetentionPolicy.SHORT_TERM:
        # 12-hour expiry
        elapsed_hours = (now - created_at).total_seconds() / 3600.0
        return elapsed_hours >= 12.0

    if retention_policy == RetentionPolicy.DECAYS:
        if decay_at is None:
            return False
        return now >= decay_at

    return False
```

### 9.3 关系重要性衰减曲线

```python
def compute_importance_decay(
    relationship_importance: float,
    days_since_update: float,
) -> float:
    """
    Decay relationship_importance over time.

    Rules:
    - Days 0-7:    no decay
    - Days 7-30:   multiply by 0.95 every 7 days
    - Days 30+:    multiply by 0.90 every 30 days
    - Floor:       0.05

    This ensures that long-term memories gradually fade if not reinforced,
    but never go to exactly zero (so they can be recovered if user mentions
    the topic again).
    """
    if days_since_update <= 7:
        return relationship_importance

    decayed = relationship_importance

    if days_since_update > 7:
        weeks = (days_since_update - 7) / 7
        decayed *= 0.95 ** weeks

    if days_since_update > 30:
        months = (days_since_update - 30) / 30
        decayed *= 0.90 ** months

    return max(0.05, round(decayed, 4))
```

### 9.4 周期性衰减任务

```python
async def decay_expired(self) -> DecayResult:
    """
    Run periodic decay on all active memories.

    Called by:
    - SchedulerRuntime on heartbeat (e.g., every 5 minutes)
    - write_after_turn() as part of post-turn cleanup
    - Admin API trigger

    Operations:
      1. Scan negative emotion memories (emotional_weight < -0.3):
         if decay_at <= now → archive (visibility → archived_by_relationship_reset)
      2. Scan short-term episodes (retention_policy == short_term):
         if created_at + 12h <= now → archive
      3. Scan "decays" retention_policy memories:
         if decay_at <= now → archive
      4. Clean expired SentContentRecords (expires_at <= now → delete)
      5. Apply importance decay to all active long-term memories
    """

    now = datetime.utcnow()
    result = DecayResult(0, 0, 0, 0, 0)

    # 1. Negative emotions decay
    negative_decayed = await self.store.archive_expired_negative_emotions(
        cutoff=now,
        bot_id=None,  # All bots
    )
    result.negative_emotions_decayed = negative_decayed

    # 2. Short-term episodes expiry
    short_term_expired = await self.store.archive_expired_short_term(
        cutoff=now - timedelta(hours=12),
    )
    result.short_term_expired = short_term_expired

    # 3. General decays
    general_decayed = await self.store.archive_expired_decays(cutoff=now)
    result.episodes_archived = general_decayed

    # 4. Sent content cleanup
    sent_cleaned = await self.store.delete_expired_sent_content(cutoff=now)
    result.sent_content_expired = sent_cleaned

    # 5. Importance decay (update relationship_importance)
    importance_decayed = await self.store.apply_importance_decay(cutoff=now)
    result.decay_applied += importance_decayed

    result.total_affected = (
        result.negative_emotions_decayed
        + result.short_term_expired
        + result.episodes_archived
        + result.sent_content_expired
        + result.decay_applied
    )

    return result
```

---

## 10. 去重策略

### 10.1 CJK Bigram Jaccard 去重 (复用现有)

```python
def dedup_memories(
    memories: list[MemoryRecord],
    threshold: float = 0.75,
) -> list[MemoryRecord]:
    """
    Deduplicate a list of MemoryRecords using CJK bigram Jaccard similarity.

    Algorithm (from memory/dedup.py):
    1. For each pair of memories, compute Jaccard similarity on normalized_content.
    2. If similarity >= threshold, keep the one with higher relationship_importance.
    3. If relationship_importance is equal, keep the newer one (higher updated_at).

    This is a within-category dedup. Cross-category dedup is not applied
    because the same fact can legitimately exist in multiple categories
    (e.g., a boundary is also a profile fact).
    """
    if len(memories) <= 1:
        return list(memories)

    # Sort: higher importance first, then newer first
    sorted_mems = sorted(
        memories,
        key=lambda m: (m.relationship_importance, m.updated_at),
        reverse=True,
    )

    kept = [sorted_mems[0]]
    for candidate in sorted_mems[1:]:
        is_dup = False
        for existing in kept:
            sim = jaccard_similarity(
                candidate.normalized_content,
                existing.normalized_content,
            )
            if sim >= threshold:
                is_dup = True
                break
        if not is_dup:
            kept.append(candidate)

    return kept
```

### 10.2 Sent Content 指纹比较

```python
def compute_sent_content_fingerprint(text: str) -> str:
    """
    Compute a fingerprint for sent content.

    Algorithm:
    1. Normalize: strip whitespace, fullwidth→halfwidth, lowercase.
    2. Extract CJK bigrams.
    3. Sort bigrams alphabetically.
    4. Join into a single string.
    5. SHA256 hash, take first 16 hex chars.

    Returns: "fp_<hex16>"
    """
    import hashlib

    normalized = normalize_text(text)
    bigrams = sorted(_extract_bigrams(normalized))
    bigram_str = "|".join(bigrams)

    if not bigram_str:
        # Text too short for bigrams
        bigram_str = normalized

    h = hashlib.sha256(bigram_str.encode("utf-8")).hexdigest()[:16]
    return f"fp_{h}"


async def is_sent_content_duplicate(
    text: str,
    recent_sent_content: list[SentContentRecord],
    threshold: float = 0.75,
) -> bool:
    """
    Check if text is too similar to recently sent content.

    Used by proactive send gate. Compares incoming text against
    recent SentContentRecords using both fingerprint and Jaccard.

    Returns True if duplicate found.
    """
    normalized = normalize_text(text)

    for sc in recent_sent_content:
        # Fast path: exact fingerprint match
        new_fp = compute_sent_content_fingerprint(text)
        if new_fp == sc.fingerprint:
            return True

        # Slow path: Jaccard comparison
        sim = jaccard_similarity(normalized, sc.normalized_text)
        if sim >= threshold:
            return True

    return False
```

### 10.3 规范化文本函数

```python
def normalize_text(text: str) -> str:
    """
    Normalize text for comparison purposes.

    Operations:
    - Strip leading/trailing whitespace.
    - Collapse multiple spaces/newlines to single space.
    - Convert fullwidth ASCII to halfwidth.
    - Convert fullwidth punctuation to halfwidth.
    - Lowercase ASCII letters.
    - Remove emoji and variation selectors.
    - Trim to 500 chars max.
    """
    import re

    text = text.strip()
    text = re.sub(r'\s+', ' ', text)

    # Fullwidth to halfwidth (U+FF01–U+FF5E → U+0021–U+007E)
    result = []
    for ch in text:
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:
            result.append(chr(code - 0xFEE0))
        elif 0x3000 == code:
            result.append(' ')
        else:
            result.append(ch)
    text = ''.join(result)

    # Lowercase ASCII
    # (CJK has no case, this only affects embedded English)
    text = text.lower()

    # Remove emoji
    text = re.sub(r'[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF'
                  r'\U0001F680-\U0001F6FF\U0001F1E0-\U0001F1FF'
                  r'\U00002702-\U000027B0\U000024C2-\U0001F251'
                  r'\U0000FE00-\U0000FE0F\U0000200D]+', '', text)

    text = text.strip()

    if len(text) > 500:
        text = text[:500]

    return text
```

---

## 11. 决策表

### 11.1 Write Decision by Content Category and Emotional Weight

| Category | Emotional Weight | write_decision 策略 | 默认 retention_policy |
|----------|------------------|---------------------|-----------------------|
| profile | >= 0.3 | SHOULD_WRITE, 高重要性 → MUST_WRITE | long_term |
| profile | [-0.3, 0.3) | MAYBE_WRITE, 依赖 importance | short_term |
| profile | < -0.3 | 降级为 episode, DO_NOT_WRITE 长期 | short_term |
| relationship | >= 0.5 | MUST_WRITE (核心关系节点) | long_term |
| relationship | [0.2, 0.5) | SHOULD_WRITE | long_term 或 short_term |
| relationship | < 0.2 | MAYBE_WRITE 或 DO_NOT_WRITE | short_term |
| boundary | any | ALWAYS WRITE (系统最高优先级) | long_term |
| episode | >= 0.5 | SHOULD_WRITE (标记为长期候选) | short_term (可升级) |
| episode | [0.2, 0.5) | MAYBE_WRITE | short_term |
| episode | < 0.2 或 < -0.3 | DO_NOT_WRITE 或 decays | decays (6h) |
| open_thread | any | MAYBE_WRITE, 自动过期 | short_term |

### 11.2 Retention Policy by Content Type

| Content Type | Default retention | 可否升级到 long_term | 条件 |
|-------------|-------------------|----------------------|------|
| 专属昵称 | long_term | — | 无条件 |
| 纪念日/日期 | long_term | — | 无条件 |
| 核心喜好 | long_term | — | relationship_importance >= 0.6 |
| 核心雷区 | long_term | — | relationship_importance >= 0.6 |
| 长期承诺 | long_term | — | 无条件 |
| 重要人生事件 | long_term | — | relationship_importance >= 0.5 |
| 日常闲聊 | short_term | 否 | 12h 过期 |
| 临时情绪 (正面) | short_term | 可 | relationship_importance >= 0.6 |
| 临时情绪 (负面) | decays (6h) | 否 | emotional_weight < -0.3 |
| 吵架气话 | decays (6h) 或 reject | 否 | 不变写入 long_term |
| 伤人的话 | reject | 否 | 完全不写入 |
| 亲密对话原文 | reject | 否 | 完全不写入 |
| 临时矛盾/误会 | short_term | 否 | 12h 过期 |
| 未完成任务 | open_thread | 否 | 完成任务后转为 episode |
| 用户边界 | long_term | — | 无条件 |

### 11.3 Context Inclusion by Memory Category and Privacy Level

| Category | Privacy Level | 进入上下文? | 如何展示 |
|----------|---------------|-----------|---------|
| boundary | normal | YES, 始终优先 | 完整 content |
| boundary | sensitive | YES, metadata only | "用户设置了边界: [category]" |
| boundary | do_not_surface | NO | — |
| relationship | normal | YES | 完整 content |
| relationship | sensitive | YES, metadata only | "关系状态: [summary]" |
| relationship | do_not_surface | NO | — |
| profile | normal | YES | 完整 content |
| profile | sensitive | YES, metadata only | "用户信息: [tags]" |
| profile | do_not_surface | NO | — |
| episode | normal | YES, 未过期 | 完整 content + 时间标注 |
| episode | sensitive | YES, metadata only | "近期事件: [summary]" |
| episode | do_not_surface | NO | — |
| open_thread | normal | YES | 完整 content |
| open_thread | sensitive | YES, metadata only | "待处理: [summary]" |
| open_thread | do_not_surface | NO | — |

### 11.4 Decay Schedule by Emotional Valence

| Emotional Weight 范围 | 衰减策略 | 衰减时间 | 衰减后行为 |
|----------------------|---------|---------|-----------|
| [-1.0, -0.7) | 6h half-life, 线性衰减 | 6 小时 | 到期后 archived, 不主动提及 |
| [-0.7, -0.3) | 6h half-life, 线性衰减 | 6 小时 | 到期后 archived, 不主动提及 |
| [-0.3, 0.0) | 无特殊衰减 | 跟随 retention_policy | 正常 short_term 12h 过期 |
| [0.0, 1.0] | 无特殊衰减 | 跟随 retention_policy | 正常处理 |

### 11.5 Forget Directive Scope and Effect

| Scope | 效果 | 窗口 | 是否永久 | 回复风格 |
|-------|------|------|---------|---------|
| local_window | ±5 turns 内记忆被封存 | 10 turns | 否 | gentle |
| local_window (重复 3 次同一话题) | ±5 turns + 话题永久屏蔽 | 10 turns | 是 | gentle → firm |
| topic | 该话题永久屏蔽 + ±5 turns | 10 turns + permanent | 是 | gentle |
| relationship_reset | 所有 relationship + profile 记忆归档 | 全部 | 可逆 (管理员) | neutral |

---

## 12. 禁止行为

以下行为在任何实现中都不可出现：

### FORBID-1: write_after_turn() 在 turn 中调用

```
MemorySystem.write_after_turn() MUST NOT be called before TurnEngine reaches terminal state.
Violation results in: memories written before model output is complete,
possibly capturing content that should not be retained.
```

### FORBID-2: 忽略 ForgetDirective

```
ForgetDirective MUST be checked before ANY memory write.
A memory whose source_turn_id falls within a directive's window
MUST NOT be extracted or written, regardless of its importance.
```

### FORBID-3: 吵架内容作为 long_term 写入

```
Fight words, hurtful words, temporary conflict expressions
MUST NOT be written with retention_policy == long_term.
The system may retain them as short_term (12h) for immediate context,
but they must never persist as permanent memories.
```

### FORBID-4: 在上下文中展示隐藏/屏蔽记忆

```
Memories with visibility != active MUST NOT enter model context.
This includes checking visibility AFTER loading and BEFORE passing
to ContextBuilder.
```

### FORBID-5: 将 sent_content_memory 当作关系记忆

```
SentContentRecords MUST NOT be stored in the memories table.
They MUST NOT influence relationship state computation.
They are for dedup only.
```

### FORBID-6: 写入亲密对话原文

```
Intimate conversation transcripts MUST NOT be written to any
memory table. Not as long-term, not as short-term, not as episode.
Only a highly abstract metadata record is allowed (e.g.,
"亲密对话发生于 turn_xxx").
```

### FORBID-7: 在上下文中展示 do_not_surface 记忆

```
Memories with privacy_level == do_not_surface MUST NOT appear
in any context block visible to the model. Not even as metadata.
The model must have zero awareness of these memories' existence.
```

### FORBID-8: 绕过去重直接写入

```
Every memory write MUST pass through dedup check before insertion.
No write path may bypass the dedup logic (except admin import tools
with an explicit "skip_dedup" flag).
```

### FORBID-9: 无 bot_id 写入记忆

```
Every memory write MUST include a valid bot_id.
Empty string '' is reserved for shared user facts.
Missing bot_id → REJECT the write.
```

### FORBID-10: 写负面情绪记忆时不设 decay_at

```
Any memory with emotional_weight < -0.3 MUST have a decay_at
set to created_at + 6 hours (or the configured duration).
Writing a negative emotion memory without decay_at is a bug.
```

### FORBID-11: 主动提及已衰减的负面情绪

```
In load_for_turn(), decayed negative emotions (decay_at <= now)
MUST be filtered out. The model MUST NOT see them in context.
The only exception is when the user explicitly brings up the
same topic again in the current turn — then the memory can
be temporarily re-activated for this turn only.
```

### FORBID-12: 将模型自然语言当作遗忘指令

```
"Forgetting" must be triggered by an explicit tool call
(apply_forget_directive) or detected user intent with high confidence.
The system MUST NOT interpret casual phrases like "算了不说了"
or "当我没说过" as forget directives unless the model explicitly
calls the forget directive tool.
```

### FORBID-13: 在 memory write 期间阻塞 turn 完成

```
write_after_turn() must be non-blocking for the turn's terminal flow.
If heavy processing is needed (multiple LLM calls for extraction),
the actual writes should be enqueued and processed asynchronously.
The turn result MUST be returned to the caller before all memory
processing is complete.
```

---

## 13. 具体输入输出例子

### Example 1: Normal chat — load_for_turn with rich profile

**输入：**

```python
request = MemorySelectionRequest(
    user_id="user_001",
    bot_id="default_bot",
    turn_type=TurnType.USER_CHAT,
    inbound_batch=InboundMessageBatch(
        batch_id="batch_001",
        user_id="user_001",
        bot_id="default_bot",
        items=[InboundMessageItem(kind="text", text="我今天好累啊")],
    ),
    recent_history=[
        ConversationHistoryMessage(role="user", content="今天工作好多"),
        ConversationHistoryMessage(role="assistant", content="辛苦了宝宝"),
    ],
    relationship_state=RelationshipState(intimacy_level=7),
    forget_directives=[],
    blocked_topics=[],
)
```

**预期输出：**

```python
MemorySelection(
    profile_memory=[
        MemoryRecord(content="用户喜欢喝热牛奶放松", category=PROFILE, ...),
        MemoryRecord(content="用户工作日通常22点睡觉", category=PROFILE, ...),
    ],
    relationship_memory=[
        MemoryRecord(content="用户累的时候喜欢被轻轻关心，不要追问", category=RELATIONSHIP, ...),
    ],
    boundary_memory=[
        MemoryRecord(content="用户明确说过不要在23点后主动发消息", category=BOUNDARY, ...),
    ],
    episode_memory=[
        MemoryRecord(content="用户今天说工作很多，心情一般", category=EPISODE, emotional_weight=-0.3, ...),
    ],
    open_thread_memory=[],
    sent_content_memory=[
        SentContentRecord(text="辛苦了宝宝", sent_at=today, ...),
    ],
    selection_reasons=[
        "Loaded 1 boundary memories (highest priority)",
        "Loaded 1 relationship memories",
        "Loaded 2 profile memories",
        "Loaded 1 episode memories (filtered expired)",
    ],
    total_memory_count=5,
)
```

### Example 2: Cold start — no memories

**输入：**

```python
request = MemorySelectionRequest(
    user_id="new_user_99",
    bot_id="default_bot",
    turn_type=TurnType.USER_CHAT,
    inbound_batch=...,
    recent_history=[],
    forget_directives=[],
    blocked_topics=[],
)
```

**预期输出：**

```python
MemorySelection(
    profile_memory=[],
    relationship_memory=[],
    boundary_memory=[],
    episode_memory=[],
    open_thread_memory=[],
    sent_content_memory=[],
    selection_reasons=["No memories found for user new_user_99 / bot default_bot"],
    total_memory_count=0,
    token_estimate=0,
)
```

### Example 3: Forget directive detected

**场景：** 用户在第 95 轮说 "别记这个"。系统在第 96 轮尝试写记忆。

**输入 (write_after_turn)：**

```python
request = MemoryWriteRequest(
    user_id="user_001",
    bot_id="default_bot",
    turn_id="turn_096",
    terminal_state="finished",
    delivered_messages=[...],
    model_candidates=[
        MemoryCandidate(content="用户喜欢喝热牛奶", category=PROFILE, ...),
    ],
    forget_directives=[
        ForgetDirective(
            directive_id="fd_001",
            detected_at_turn_id="turn_095",
            scope=ForgetScope.LOCAL_WINDOW,
            window_before=5,
            window_after=5,
            created_at=...,
        ),
    ],
    blocked_topics=[],
)
```

**预期输出：**

```python
MemoryWriteResult(
    memories_written=[],
    memories_rejected_by_forget=1,  # Turn 96 is within [90, 100] window
    memories_rejected_by_duplicate=0,
    memories_rejected_by_policy=0,
    sent_content_records=["sc_xxx"],  # Sent content still written (dedup)
    decay_applied=0,
)
```

### Example 4: Negative emotion decay

**场景：** 7 小时前用户很生气 (emotional_weight = -0.8)。现在 `load_for_turn()` 被调用。

**`load_for_turn()` 中 episode 处理：**

```text
episode: {
    content: "用户今天被老板骂了，非常生气",
    emotional_weight: -0.8,
    decay_at: created_at + 6h,  # 7 hours ago → expired
    retention_policy: "decays",
}

→ 检查 decay_at <= now: True
→ emotional_weight < -0.3: True
→ 设置 visibility = archived_by_relationship_reset
→ 不进入 MemorySelection
```

**预期 MemorySelection.episode_memory 不包含此记录。**

### Example 5: Sent content dedup

**场景：** 系统想要发送主动消息 "今天累吗，早点休息"。最近已发送过 "今天辛苦了，早点休息吧"。

```python
recent = [
    SentContentRecord(
        text="今天辛苦了，早点休息吧",
        normalized_text="今天辛苦了 早点休息吧",
        fingerprint="fp_a1b2c3d4e5f6a7b8",
    ),
]

is_dup = await is_sent_content_duplicate("今天累吗，早点休息", recent)
# → True (Jaccard similarity ≈ 0.80 > 0.75 threshold)
```

**结果：** 主动消息被阻断，不发送。

### Example 6: Boundary memory priority

**场景：** profile 说 "用户喜欢被开玩笑"，boundary 说 "不要再开体重玩笑了"。

**`load_for_turn()` 返回：**

```python
MemorySelection(
    boundary_memory=[
        MemoryRecord(content="不要再开体重玩笑了", category=BOUNDARY, ...),
    ],
    profile_memory=[
        MemoryRecord(content="用户喜欢被开玩笑，但要注意分寸", category=PROFILE, ...),
    ],
    selection_reasons=[
        "Loaded 1 boundary memories (highest priority)",
        "Loaded 1 profile memories",
    ],
)
```

**ContextBuilder 渲染时，boundary 在 profile 之前，且标注为 OVERRIDE。**

### Example 7: write_after_turn — fight words filtered

**输入 (model_candidates)：**

```python
model_candidates = [
    MemoryCandidate(
        content="用户说你从来不懂他",
        category=RELATIONSHIP,
        emotional_weight=-0.6,
        relationship_importance=0.3,
        write_decision=WriteDecision.SHOULD_WRITE,
    ),
    MemoryCandidate(
        content="用户喜欢你叫他宝宝",
        category=PROFILE,
        emotional_weight=0.5,
        relationship_importance=0.8,
        write_decision=WriteDecision.MUST_WRITE,
    ),
]
```

**处理：**

```text
Candidate 1:
→ _contains_fight_or_hurtful_content("用户说你从来不懂他"): True
  (matches pattern "不懂")
→ write_decision != MUST_WRITE: discard
→ memories_discarded += 1

Candidate 2:
→ _contains_fight_or_hurtful_content: False
→ _contains_intimate_content: False
→ retention = long_term
→ _qualifies_for_long_term: True (importance 0.8, positive emotion)
→ dedup: no existing match
→ WRITTEN
→ memories_written += 1
```

**结果：**

```python
MemoryWriteResult(
    memories_written=["mem_abc123..."],
    memories_discarded=1,
    memories_rejected_by_forget=0,
    memories_rejected_by_duplicate=0,
    memories_rejected_by_policy=0,
)
```

### Example 8: Repeated "don't mention X" → permanent topic block

**场景：**

```text
Day 1: 用户说 "别再说我工作了"
  → directive_1: scope=LOCAL_WINDOW, blocked_topic="工作"

Day 3: 用户又说 "我说了别提我工作的事"
  → directive_2: scope=LOCAL_WINDOW, blocked_topic="工作"

Day 5: 用户再次说 "不要再问我工作怎么样了"
  → directive_3: 系统检测到是第 3 次 "工作" 话题
  → _detect_repeated_forget_directive(): True
  → blocked_topics 永久加入 "工作"
  → permanent_topic_block = True
```

**此后：** 所有 tags 包含 "工作" 的 MemoryCandidate 在 write_after_turn() 中直接被拒绝。

### Example 9: Hidden memory NOT in context

**场景：** 管理员在管理台隐藏了一条 profile memory "用户住在上海"。

```text
Before hide:
  visibility = active
  load_for_turn() → profile_memory includes this record

After hide:
  visibility = hidden_by_user
  load_for_turn() → profile_memory does NOT include this record
  selection_reasons includes: "Filtered 1 non-active memory"
```

### Example 10: Relationship reset — memories archived, NOT deleted

**场景：** 管理台触发关系重置 (scope = relationship_reset)。

```python
directive = ForgetDirective(
    scope=ForgetScope.RELATIONSHIP_RESET,
    user_id="user_001",
    bot_id="default_bot",
    ...
)

await memory_system.apply_forget_directive(directive)
```

**结果：**

```text
memories 表中:
- category=RELATIONSHIP 且 visibility=active → visibility=archived_by_relationship_reset
- category=PROFILE 且 visibility=active → visibility=archived_by_relationship_reset (部分, 仅关系相关的)
- category=BOUNDARY → 保留 active (边界不受关系重置影响)
- category=EPISODE → 保留 active (短期事件不受关系重置影响)

relationship_state 表:
- familiarity → 5 (初始值)
- intimacy_level → 重置
- chat_boundary → ""
- interaction_style → "casual"
- 其他字段 → 默认值
```

**NOT deleted:** 所有记忆仍然保留在数据库中，`visibility` 变为 `archived_by_relationship_reset`。管理员可手动恢复。

### Example 11: Bot isolation

**场景：** bot_A 和 bot_B 各有独立记忆。

```python
# bot_A 的记忆
await memory_system.write_after_turn(MemoryWriteRequest(
    bot_id="bot_A",
    model_candidates=[MemoryCandidate(content="用户喜欢蓝色", category=PROFILE, ...)],
))
# → 写入 memories 表, bot_id="bot_A"

# bot_B 的 load_for_turn()
result = await memory_system.load_for_turn(MemorySelectionRequest(
    bot_id="bot_B",
))
# → profile_memory 不包含 "用户喜欢蓝色"
# → bot_B 看不到 bot_A 的记忆
```

**共享记忆 (bot_id='')：**

```python
# 所有 bot 都可以看到
await memory_system.write_after_turn(MemoryWriteRequest(
    bot_id="",  # Shared
    model_candidates=[MemoryCandidate(content="用户生日是 1 月 15 日", category=PROFILE, ...)],
))
# → load_for_turn(bot_id="bot_A") 和 load_for_turn(bot_id="bot_B") 都能看到
```

### Example 12: Short-term episode expiry

**场景：** 13 小时前的 episode "用户说今天想吃火锅"。

```python
episode = MemoryRecord(
    content="用户说今天想吃火锅",
    category=EPISODE,
    retention_policy=SHORT_TERM,
    created_at=datetime(2026, 6, 2, 20, 0, 0),  # 13 hours ago
    decay_at=datetime(2026, 6, 3, 8, 0, 0),     # expired
    visibility=ACTIVE,
)

# load_for_turn() at 2026-06-03 09:00:
# → decay_at <= now: True
# → is_episode_expired: True
# → visibility = archived (updated in DB)
# → NOT in MemorySelection.episode_memory
```

---

## 14. 当前代码迁移点

### 14.1 现有文件与新模块映射

| 现有文件 | 行数 | 新模块中的角色 | 迁移策略 |
|---------|------|---------------|---------|
| `memory/store.py` | ~136KB | 底层持久化 (MemoryStore) | 保留作为 `MemorySystem` 内部依赖。新增 facade 层 `memory/system.py` 封装语义调用。MemoryStore 的 `memories` 表新增列：`normalized_content`, `emotional_weight`, `relationship_importance`, `privacy_level`, `visibility`, `user_forgettable`, `decay_at`, `tags`, `metadata`。 |
| `memory/dedup.py` | ~150 行 | 去重工具 | 完整保留。`is_duplicate()` 和 `jaccard_similarity()` 继续使用。新增 `normalize_text()` 和 `compute_sent_content_fingerprint()`。 |
| `agent/memory_worker.py` | ~170 行 | MemoryCandidate 提取器 | 作为 `MemorySystem` 的私有 worker。`extract()` 方法改为返回 `list[MemoryCandidate]` 而不是直接写入。写入决策移交给 `write_after_turn()`。 |

### 14.2 需要新增的文件

```text
memory/
  system.py                  ← MemorySystem facade (NEW, ~400 lines)
  types.py                   ← MemoryRecord, MemoryCandidate, enums (NEW, ~200 lines)
  forget.py                  ← ForgetDirective 处理 (NEW, ~200 lines)
  decay.py                   ← 衰减逻辑 (NEW, ~150 lines)
  sent_content.py            ← SentContentRecord 管理 (NEW, ~150 lines)
  serializers.py             ← DB row ↔ MemoryRecord 序列化 (NEW, ~100 lines)
  store.py                   ← 保留, 增加 Migration 方法
  dedup.py                   ← 保留, 增加 normalize_text + fingerprint
```

### 14.3 store.py 需要新增的表/列

#### 新增表

```sql
-- Memory records (升级版 memories 表, 向后兼容)
-- 方案: 新增列到现有 memories 表

-- Forget directives
CREATE TABLE IF NOT EXISTS forget_directives (
    id TEXT PRIMARY KEY,                        -- "fd_<uuid12>"
    user_id TEXT NOT NULL,
    bot_id TEXT NOT NULL DEFAULT '',
    detected_at_turn_id TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'local_window',  -- local_window | topic | relationship_reset
    window_before INTEGER NOT NULL DEFAULT 5,
    window_after INTEGER NOT NULL DEFAULT 5,
    blocked_topic TEXT,
    permanent_topic_block INTEGER NOT NULL DEFAULT 0,
    response_style TEXT NOT NULL DEFAULT 'gentle',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_forget_directives_user_bot
    ON forget_directives(user_id, bot_id, created_at DESC);

-- Sent content records
CREATE TABLE IF NOT EXISTS sent_content_records (
    id TEXT PRIMARY KEY,                        -- "sc_<uuid12>"
    turn_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    bot_id TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'assistant_reply',
    text TEXT NOT NULL,
    normalized_text TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    sent_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sent_content_fingerprint
    ON sent_content_records(fingerprint);
CREATE INDEX IF NOT EXISTS idx_sent_content_user_bot_expires
    ON sent_content_records(user_id, bot_id, expires_at DESC);

-- Blocked topics
CREATE TABLE IF NOT EXISTS blocked_topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    bot_id TEXT NOT NULL DEFAULT '',
    topic TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(user_id, bot_id, topic)
);

-- Memory decay log (audit)
CREATE TABLE IF NOT EXISTS memory_decay_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id TEXT NOT NULL,
    action TEXT NOT NULL,                       -- archived | visibility_changed | importance_decayed
    from_value TEXT,
    to_value TEXT,
    reason TEXT,
    applied_at REAL NOT NULL
);
```

#### 新增列到 memories 表

```sql
-- 现有 memories 表已有: id, content, category, importance, keywords,
--                        source_turn_id, created_at, updated_at, bot_id (added V2.1)
-- 需要新增:
ALTER TABLE memories ADD COLUMN normalized_content TEXT NOT NULL DEFAULT '';
ALTER TABLE memories ADD COLUMN emotional_weight REAL NOT NULL DEFAULT 0.0;
ALTER TABLE memories ADD COLUMN relationship_importance REAL NOT NULL DEFAULT 0.0;
ALTER TABLE memories ADD COLUMN privacy_level TEXT NOT NULL DEFAULT 'normal';
ALTER TABLE memories ADD COLUMN visibility TEXT NOT NULL DEFAULT 'active';
ALTER TABLE memories ADD COLUMN user_forgettable INTEGER NOT NULL DEFAULT 1;
ALTER TABLE memories ADD COLUMN decay_at REAL;
ALTER TABLE memories ADD COLUMN user_id TEXT NOT NULL DEFAULT '';
ALTER TABLE memories ADD COLUMN tags TEXT NOT NULL DEFAULT '[]';
ALTER TABLE memories ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}';
```

### 14.4 MemoryStore 新增方法

```python
# MemoryStore 新增的语义方法 (被 MemorySystem facade 调用):

# 查询
async def query_memories(
    self, user_id: str, bot_id: str,
    category: str | None = None,
    visibility: str | None = None,
    privacy_level: str | None = None,
    retention_policy: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Query memories with filters."""

async def query_memories_by_content_bigrams(
    self, user_id: str, bot_id: str,
    normalized_content: str,
    threshold: float = 0.75,
) -> list[dict]:
    """Check for duplicates using bigram Jaccard on stored normalized_content."""

async def insert_memory_record(self, **fields) -> str:
    """Insert a full MemoryRecord. Returns memory_id."""

async def update_memory_visibility(
    self, memory_id: str, new_visibility: str,
) -> bool:
    """Update visibility only."""

async def update_memory_visibility_by_source_turn(
    self, source_turn_id: str, new_visibility: str,
) -> int:
    """Batch update visibility by source turn."""

async def archive_memories_by_relationship_reset(
    self, user_id: str, bot_id: str,
) -> int:
    """Archive relationship + profile memories for a reset."""

async def archive_expired_negative_emotions(self, cutoff: datetime) -> int:
    """Archive memories with emotional_weight < -0.3 and decay_at <= cutoff."""

async def archive_expired_short_term(self, cutoff: datetime) -> int:
    """Archive short_term memories created before cutoff."""

async def archive_expired_decays(self, cutoff: datetime) -> int:
    """Archive memories with retention_policy=decays and decay_at <= cutoff."""

async def apply_importance_decay(self, cutoff: datetime) -> int:
    """Apply time-based decay to relationship_importance."""

# Forget directives
async def insert_forget_directive(self, directive: ForgetDirective) -> str: ...
async def get_active_forget_directives(self, user_id: str, bot_id: str) -> list[ForgetDirective]: ...

# Sent content
async def insert_sent_content(self, record: SentContentRecord) -> str: ...
async def query_sent_content(self, user_id: str, bot_id: str, expires_after: datetime, limit: int = 20) -> list[SentContentRecord]: ...
async def delete_expired_sent_content(self, cutoff: datetime) -> int: ...

# Blocked topics
async def add_blocked_topic(self, user_id: str, bot_id: str, topic: str) -> None: ...
async def get_blocked_topics(self, user_id: str, bot_id: str) -> list[str]: ...

# Turn info
async def get_turn_number(self, turn_id: str) -> int | None: ...
async def get_turn_ids_in_range(self, user_id: str, bot_id: str, turn_number_start: int, turn_number_end: int) -> list[str]: ...
```

### 14.5 对 agent/memory_worker.py 的修改

```python
# 修改前: MemoryWorker.extract() 直接写入
# async def extract(...):
#     ...
#     mem_id = await self.store.save_memory_bot(...)

# 修改后: MemoryWorker.extract() 返回 candidates
# async def extract(...) -> list[MemoryCandidate]:
#     ...
#     return [MemoryCandidate(content=..., category=..., ...), ...]

# 调用方 (TurnEngine / POST_TURN_MEMORY):
# candidates = await memory_worker.extract(...)
# result = await memory_system.write_after_turn(
#     MemoryWriteRequest(model_candidates=candidates, ...)
# )
```

---

## 15. 必须测试的用例列表

### 15.1 结构测试

| # | 测试名 | 描述 |
|---|--------|------|
| 1 | `test_memory_record_required_fields` | MemoryRecord 必填字段缺失时抛出异常 |
| 2 | `test_memory_record_emotional_weight_range` | emotional_weight 必须在 [-1.0, 1.0] |
| 3 | `test_memory_record_relationship_importance_range` | relationship_importance 必须在 [0.0, 1.0] |
| 4 | `test_sent_content_fingerprint_unique` | 相同文本产生相同指纹，不同文本产生不同指纹 |
| 5 | `test_memory_candidate_write_decision_enum` | WriteDecision 枚举值正确 |

### 15.2 记忆加载测试

| # | 测试名 | 描述 |
|---|--------|------|
| 6 | `test_load_boundary_always_first` | boundary memory 始终出现在 MemorySelection 首位 |
| 7 | `test_load_filters_expired_episodes` | 过期 12h 的 episode 不出现在结果中 |
| 8 | `test_load_filters_negative_emotions_after_decay` | 6h 后负面情绪不出现在结果中 |
| 9 | `test_load_filters_hidden_memories` | visibility=hidden_by_user 的记忆不出现在结果中 |
| 10 | `test_load_filters_do_not_surface` | privacy_level=do_not_surface 的记忆不出现在结果中 |
| 11 | `test_load_filters_blocked_by_forget_directive` | 被 forget directive 屏蔽的记忆不出现在结果中 |
| 12 | `test_load_applies_blocked_topics_filter` | blocked_topics 列表中的话题被过滤 |
| 13 | `test_load_respects_max_per_category` | 每类记忆不超过 max_memories_per_category |
| 14 | `test_load_respects_max_total` | 总记忆不超过 max_total_memories |

### 15.3 记忆写入测试

| # | 测试名 | 描述 |
|---|--------|------|
| 15 | `test_write_after_turn_blocks_forget_directive_window` | ±5 turns 内不写入任何记忆 |
| 16 | `test_write_filters_fight_words` | 吵架内容被拒绝或降级为 short_term |
| 17 | `test_write_filters_intimate_transcripts` | 亲密对话原文被完全拒绝 |
| 18 | `test_write_downgrades_low_importance_long_term` | 低 importance 的 long_term 被降级 |
| 19 | `test_write_sets_decay_at_for_negative_emotions` | 负面情绪记忆有 decay_at = created_at + 6h |
| 20 | `test_write_sets_decay_at_for_short_term` | short_term 记忆有 decay_at = created_at + 12h |
| 21 | `test_write_writes_sent_content_separately` | sent_content 写入 sent_content_records 表而非 memories 表 |

### 15.4 去重测试

| # | 测试名 | 描述 |
|---|--------|------|
| 22 | `test_dedup_cjk_bigram_jaccard` | 相似中文文本被识别为重复 (threshold >= 0.75) |
| 23 | `test_dedup_normalized_text_comparison` | 空白/标点差异不影响去重判断 |
| 24 | `test_sent_content_dedup_blocks_proactive` | 与最近发送内容相似度 >= 0.75 时阻断主动消息 |

### 15.5 ForgetDirective 测试

| # | 测试名 | 描述 |
|---|--------|------|
| 25 | `test_forget_directive_blocks_local_window` | scope=local_window 屏蔽 ±5 turns |
| 26 | `test_forget_directive_topic_adds_to_blocked_list` | scope=topic 添加永久屏蔽话题 |
| 27 | `test_forget_directive_relationship_reset_archives` | scope=relationship_reset 归档但不删除 |
| 28 | `test_repeated_forget_directive_triggers_permanent_block` | 3 次同一话题触发永久屏蔽 |

### 15.6 衰减测试

| # | 测试名 | 描述 |
|---|--------|------|
| 29 | `test_negative_emotion_decays_after_6_hours` | emotional_weight -0.8 在 6h 50m 后为 0.0 且不出现在上下文 |
| 30 | `test_negative_emotion_partial_decay_at_3_hours` | emotional_weight -0.8 在 3h 后约为 -0.4 |
| 31 | `test_short_term_episode_expires_after_12_hours` | retention=short_term 在 12h 后不出现在上下文 |
| 32 | `test_importance_decay_after_30_days` | relationship_importance 在 30 天后下降 |

### 15.7 Bot 隔离测试

| # | 测试名 | 描述 |
|---|--------|------|
| 33 | `test_bot_isolation_load` | bot_A 的 load_for_turn() 看不到 bot_B 的记忆 |
| 34 | `test_shared_facts_visible_across_bots` | bot_id='' 的记忆对所有 bot 可见 |
| 35 | `test_write_assigns_correct_bot_id` | write_after_turn() 写入正确的 bot_id |

### 15.8 集成测试

| # | 测试名 | 描述 |
|---|--------|------|
| 36 | `test_full_cycle_load_write_decay` | 完整: 加载 → 写入 → 衰减 → 再加载，验证一致性 |
| 37 | `test_concurrent_write_and_load` | 并发 write_after_turn 和 load_for_turn 不产生 race condition |

---

## 附录 A: 验收检查清单

实现完成时必须通过以下检查：

- [ ] MemorySystem 接口完整: `load_for_turn()`, `write_after_turn()`, `apply_forget_directive()`, `decay_expired()`
- [ ] 所有 16 个不变量在代码中有对应的检查或测试
- [ ] 所有 12+ 个禁止行为有对应的防护代码或测试
- [ ] 所有 13 个例子可以通过集成测试验证
- [ ] 数据库 migration 正确新增表/列, 不破坏现有数据
- [ ] `memory/store.py` 新增语义方法, 不破坏现有 CRUD
- [ ] `memory/dedup.py` 新增 `normalize_text` 和 `compute_sent_content_fingerprint`
- [ ] `agent/memory_worker.py` 改为返回 `list[MemoryCandidate]` 而非直接写入
- [ ] 现有 `tests/test_memory.py` 和 `tests/test_temporal_context.py` 仍然通过
- [ ] 新测试文件 `tests/test_memory_system_contract.py` 包含至少 25 个测试
- [ ] 管理台可查询 MemoryRecord, SentContentRecord, ForgetDirective, blocked_topics

---

## 附录 B: 与其他模块的通信契约

### B.1 ContextBuilder 调用 MemorySystem

```
ContextBuilder.build()
  → memory_system.load_for_turn(MemorySelectionRequest)
  → receives MemorySelection
  → renders 6 memory blocks in fixed order:
      1. boundary_memory (OVERRIDE priority marker)
      2. relationship_memory
      3. profile_memory
      4. episode_memory
      5. open_thread_memory
      6. sent_content_memory (DEDUP ONLY marker)
```

### B.2 TurnEngine 调用 MemorySystem

```
TurnEngine → CHECK_TERMINAL → POST_TURN_MEMORY
  → memory_system.write_after_turn(MemoryWriteRequest)
  → receives MemoryWriteResult
  → result does NOT block turn result assembly (FORBID-13)
```

### B.3 SchedulerRuntime 调用 MemorySystem

```
SchedulerRuntime → proactive gate check
  → memory_system.load_for_turn() → sent_content_memory
  → check is_sent_content_duplicate() against recent sent content
  → if duplicate: block proactive trigger
```

### B.4 Admin API 调用 MemorySystem

```
Admin API:
  GET  /admin/memories          → query MemoryRecord list
  POST /admin/memories/hide     → update visibility → hidden_by_user
  POST /admin/memories/unhide   → update visibility → active
  POST /admin/relationship/reset → apply_forget_directive(RELATIONSHIP_RESET)
  GET  /admin/blocked-topics     → list blocked_topics
  DELETE /admin/blocked-topics/{id} → remove blocked topic
  POST /admin/decay/trigger      → decay_expired()
```
