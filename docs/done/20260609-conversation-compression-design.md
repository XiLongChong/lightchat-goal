# 对话压缩策略 — 每日定时压缩（2026-06-09）

## 核心思路

每天凌晨 4:00 后第一条消息触发一次同步压缩。压缩源读全部 raw_events（过滤历史摘要），生成新摘要后全部软删除（active=0），摘要附着在最后一条含 tool_result 的 user 消息上。当天后续消息不受影响。

## 注入方式

找到**最后一条含 tool_result 的 user 消息**，在其 content JSON 数组末尾追加 text block。该消息和紧随的 assistant 消息设为 active=1，其余 active=0。

```
压缩后 active=1:
  seq=51  user      [tool_result: sent, text: "摘要"]  ← 改写，active=1
  seq=52  assistant 嗯 收到了                          ← 保留，active=1
  seq=53  user      <channel>今天第一轮消息</channel>     ← 正常写入
  seq=54  assistant ...                                 ← 正常写入
```

不新增 raw_event，不插入虚假对话。模型把摘要当历史消息的附加背景，不以为是用户说的。

## raw_events 表变更

新增一列：

| 列名 | 类型 | 用途 |
|------|------|------|
| `active` | INTEGER DEFAULT 1 | 1=活跃，0=软删除 |

不需要 event_type 列——摘要不占独立行。

## 流程

```
当前 turn 开始前:
  now = 北京时间
  today_4am = 今天 04:00:00

  if now >= today_4am AND last_compressed_at < today_4am:
    1. 读全部 raw_events，解析 content JSON
       过滤：移除 text block 中以 <analysis> 开头的内容（旧摘要）
       → 得到纯对话历史

    2. 调用 pro 模型生成摘要

    3. UPDATE raw_events SET active = 0              ← 全部软删

    4. 找到最后一条含 tool_result 的 user 消息:
       UPDATE content_json 追加 {"type": "text", "text": "摘要"}
       SET active = 1

    5. 保留步骤4后面那条 assistant 消息: SET active = 1
       （如果后面没有 assistant，从历史中找最近一条 assistant 设为 active=1）

    6. UPDATE conversations.last_compressed_at = now

    7. conversation_ctx.rebuild()                   ← SELECT WHERE active=1

    8. 继续正常对话

  else:
    正常对话
```

## 旧摘要防泄漏

压缩源读全部 raw_events 时，对每条 user 消息解析 content JSON，过滤掉以 `<analysis>` 开头的 text block（即旧摘要）。模型只看到真实对话，不看到历史摘要文本。

## 最后一条 user 消息没有 tool_result

（如用户发"嗯"，模型判断话题结束不回复）

向前查找最近一条含 tool_result 的 user 消息作为注入目标。找到后同样保留它 + 紧跟的 assistant → active=1。

极端情况：整个对话没有任何 tool_result（未发过消息）→ 跳过压缩。

## 数据库持久化

`conversations` 表新增一列：

| 列名 | 用途 |
|------|------|
| `last_compressed_at` | 上次压缩时间（ISO 8601） |

## 重启恢复

```sql
SELECT * FROM raw_events WHERE active=1 ORDER BY seq
```

一行 SQL。active=1 只有当天摘要 + 当天对话，天然可控。

## 压缩模型

和对话同款 pro 模型。同步调用，阻塞当前 turn（每天一次，第一条消息多等几秒）。

## 压缩提示词

```
You are compressing a WeChat chat history between a user and an AI assistant into a context summary for the next session. You will be given the full conversation. Your output will replace the raw chat history — the assistant will ONLY see your summary, not the original messages.

CRITICAL: Do NOT call any tools. Respond with TEXT ONLY.
Your entire response must be plain text: an <analysis> block followed by a <summary> block.

<analysis>
Organize your thoughts here. Go through the conversation chronologically, identify what matters and what doesn't. This block will be discarded — it is only for you to think through the compression.
</analysis>

<summary>
Output the structured summary below. The assistant will ONLY see this block — be precise and thorough.
</summary>

## What to preserve in the summary

1. User Identity & Preferences
   - Name, nickname, gender, age, occupation, location — anything the user revealed about themselves
   - Likes, dislikes, hobbies, habits, dietary preferences
   - Communication style: how they talk, sentence length, tone, emoji usage

2. Emotional State
   - Current mood and recent emotional shifts
   - Things bothering them or making them happy
   - Relationship tone with the assistant (trusting, skeptical, playful, etc.)

3. Topics Discussed
   - For each topic or subject: what was discussed, key observations, conclusions reached
   - Knowledge or expertise the user demonstrated
   - Opinions the user expressed on specific subjects

4. Decisions & Commitments
   - What the user decided to do or said they would do
   - What the assistant promised to follow up on
   - Pending tasks or "let's talk about this later" items

5. Tool Actions
   - Describe tool calls and their outcomes in plain language. Do NOT reproduce raw JSON.

6. Verbatim Quotes (IMPORTANT)
   - Preserve VERBATIM the user's messages that contain: strong emotions, personal revelations, important decisions, or unique phrasing that reveals character
   - List each as: [context] "exact message"

## What to ignore

- Pure greetings and farewells with no substance
- Completed one-off topics with no follow-up or lasting impact
- Repeated or near-identical content
- System reminders, command output, local-command blocks — these are tool artifacts, not conversation

## Format

<analysis>
[Your working notes — will be discarded]
</analysis>

<summary>
1. User Identity & Preferences:
   ...

2. Emotional State:
   ...

3. Topics Discussed:
   ...

4. Decisions & Commitments:
   ...

5. Tool Actions:
   ...

6. Verbatim Quotes:
   "[message]"
   "[message]"
   ...

7. Pending:
   ...
</summary>
```

## 设计决策总结

- 注入方式：改写已有 raw_event 的 content JSON，不新建行
- 摘要载体：最后一条含 tool_result 的 user 消息
- 无 tool_result 时：向前查找
- 旧摘要防泄漏：读压缩源时过滤 `<analysis>` 开头的 text block
- 软删除：active=0，旧数据保留
- 重建：`SELECT * FROM raw_events WHERE active=1 ORDER BY seq`
- 触发：每天凌晨 4:00 后首条消息
- 模型：pro 模型，同步调用
- 后续优化：压缩时间可配置
