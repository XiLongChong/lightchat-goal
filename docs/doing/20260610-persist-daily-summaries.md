# 历史摘要持久化（2026-06-10）

## 问题

当前每日压缩时，`_filter_summary_blocks()` 会丢弃前一天的 `<summary>` 内容。
旧摘要不参与新摘要生成（这是对的，避免套娃），但旧摘要内容彻底丢失。

后果：只有最近一天的摘要可见，更早的信息除非对话中重提，否则永久消失。

## 目标

摘要产生时直接存入数据库，保留历史快照。压缩逻辑不变。

## 方案

新增 `daily_summaries` 表，压缩产生摘要后立即写入。

### 日期判定规则

- 凌晨 5:00 之前产生 → 归为前一天
- 凌晨 5:00 及之后产生 → 归为当天

当前压缩窗口 3:56-4:25，都在 5:00 之前，所以压缩产出的摘要统一归为"前一天"。

同一天多份摘要 → 后者覆盖（`INSERT OR REPLACE`）。
当天没说话 → `compress_conversation` 因 `len(messages) < 4` 跳过 → 不触发压缩 → 不存。

### 新增表

```sql
CREATE TABLE IF NOT EXISTS daily_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    summary_date TEXT NOT NULL,     -- YYYY-MM-DD，按日期判定规则
    summary_text TEXT NOT NULL,     -- 摘要原文，保留 <summary>...</summary> 标签
    char_count INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_daily_summaries_cid_date
    ON daily_summaries(conversation_id, summary_date);
```

- `summary_text` 保留 `<summary>` 标签，取出即用，无需再加工
- 同一 conversation 同一天只保留一份

### 改动文件

| 文件 | 改动 |
|------|------|
| `memory/store.py` | 新增 `daily_summaries` 表 + `save_daily_summary()` + `get_daily_summaries()` |
| `main.py` | 压缩成功后调用 `save_daily_summary()` 保存新摘要 |

### 数据流（改动后）

```
/chat → maybe_compress_daily()
  → compress_conversation(messages)   ← 不变
  → 新摘要产生
  → replace_with_compressed(summary)  ← 不变
  → apply_compression(...)            ← 不变
  → save_daily_summary(summary, date) ← 新增：保存新摘要
```

### 实现步骤

1. **`memory/store.py`**
   - `_create_tables()` 加 `daily_summaries` 建表
   - `save_daily_summary(conversation_id, summary_date, summary_text)` — `INSERT OR REPLACE`，同时写 `char_count`
   - `get_daily_summaries(conversation_id, limit=30)` — 按 `summary_date DESC` 返回

2. **`main.py`**
   - 两处压缩后（自动压缩 ~L1260、手动压缩 ~L1380）加一行：
     ```python
     summary_date = _compute_summary_date()  # 按规则算日期
     await memory_store.save_daily_summary(cid, summary_date, f"<summary>\n{summary}\n</summary>")
     ```

3. **日期判定工具函数**
   - 放在 `main.py` 或 `compressor.py`，逻辑：
     ```python
     def _compute_summary_date() -> str:
         now = datetime.now(ZoneInfo("Asia/Shanghai"))
         if now.hour < 5:
             now -= timedelta(days=1)
         return now.strftime("%Y-%m-%d")
     ```

4. **测试**
   - 日期判定：5 点前算前一天、5 点后算当天
   - 保存和读取摘要
   - 同一天覆盖
   - 压缩逻辑不受影响

### 不变的部分

- `_filter_summary_blocks()` 不变 — 仍然过滤旧摘要，不套娃
- `compress_conversation()` 不变
- `apply_compression()` 不变
- `ConversationContext.replace_with_compressed()` 不变
- 压缩 prompt 不变
- schema version 不变（`IF NOT EXISTS` 兼容现有 schema）

### 后续利用（本次不做）

- 记忆系统读取历史摘要作为长期记忆来源
- Admin UI 查看历史摘要
- 按日期搜索"那天聊了什么"
