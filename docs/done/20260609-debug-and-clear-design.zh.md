# 一键清除调试功能设计（2026-06-09）

## 调研结论

### 两个遗留文件分析

`data/llm_trace_readable_latest.md` + `data/llm_trace_raw_latest.json`：
- **无任何代码引用**它们。全项目 grep 命中为 0
- 最后修改日期 6 月 3 日，是清理前旧脚本（`gray_native_tools.py` 等）的产物
- claude-tap 使用 `.traces/` 目录，与这两个文件无关
- 这两个文件可以安全删除

### claude-tap 与 SQLite traces 的关系

- claude-tap 是网络代理，拦截 HTTP 层流量到 `.traces/` 目录，完全不读 SQLite
- SQLite `llm_traces` 表是 Goal 内部 trace 机制，与 claude-tap 无关
- 清除 SQLite `llm_traces` 不影响 claude-tap 抓包

### 上下文加载机制

- 每次 `/chat` 请求实时从 SQLite 加载历史（非启动时）
- 调用 `get_recent_protocol_history()` → 取最近 20 个 completed turn 的 protocol_messages
- 一键清除后，下一次 `/chat` 请求拿到空历史 → 干净的上下文

## 设计方案（简化版）

### 唯一变更：管理后台新增「一键清除」按钮

在现有管理界面底部添加清除区域，调用已有 API。

```
┌─ 清除数据 ──────────────────────────────────┐
│  ⚠ 清除所有对话历史、LLM 调用记录            │
│  ✓ 微信桥接状态不受影响                      │
│                                              │
│  [一键清除]                                  │
│  <span>已清除 XX 条记录</span>               │
└──────────────────────────────────────────────┘
```

### 后端

**无需新增 API**。已有 `POST /admin/dev/reset-context` 完全满足需求：
- 清除 turns、protocol_messages、outbound_messages、turn_drafts、turn_diagnostics、usage_events、visible_activity、llm_traces
- 保留 bot_contact_state（微信联系人、context_token）
- 保留 runtime_meta（schema 版本号）

### 前端变更

| 文件 | 变更 |
|------|------|
| `static/admin.html` | 新增清除区域（section + 按钮 + 结果提示） |
| `static/admin.js` | 新增清除按钮事件处理（调用 `/admin/dev/reset-context`，展示清除结果） |

### 清理

- 删除 `data/llm_trace_readable_latest.md`
- 删除 `data/llm_trace_raw_latest.json`

## 不变的范围

- 后端 API 零变更
- 数据库 schema 不变
- claude-tap 抓包完全不受影响
- 微信桥接状态不受影响
