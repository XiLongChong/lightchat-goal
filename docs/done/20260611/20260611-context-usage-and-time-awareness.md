# 上下文窗口占用率与时间感知注入（2026-06-11）

## 1. 前端显示上下文窗口占用率

### 目标

在管理后台显示最近完成对话轮次的上下文窗口占用情况：

```text
本轮各次模型调用中的峰值 prompt_tokens
÷
模型上下文窗口容量
```

默认上下文窗口容量为 `1,000,000 tokens`，可通过
`MODEL_CONTEXT_WINDOW_TOKENS` 覆盖。

### 实现

- 每次模型调用返回 usage 后，记录本轮最大的 `prompt_tokens`。
- 原有累计 token 统计保持不变，峰值与累计值分开保存。
- 在 `turns` 表增量增加：
  - `prompt_tokens_peak`
  - `context_window_tokens`
  - `model_name`
  - `usage_estimated`
- 数据库迁移使用增量加列，不修改 schema ID，不重建或清空旧数据库。
- 新增管理员接口：

```text
GET /admin/context-usage
```

接口返回最近完成轮次的模型、峰值输入 token、窗口容量、百分比、是否估算及测量时间。

### 前端行为

管理后台新增“上下文窗口”卡片：

```text
128,430 / 1,000,000 tokens · 12.84%
```

- 登录后立即读取。
- 每 10 秒自动刷新。
- 点击现有“刷新”按钮时立即刷新。
- 清空对话后立即显示“暂无上下文数据”。
- 低于 70% 显示绿色。
- 70% 至 90% 显示黄色。
- 90% 及以上显示红色。
- 超过 100% 时保留真实百分比，进度条宽度限制为 100%。
- 没有成功 usage 时不显示虚假的 `0%`。

## 2. 调整时间感知注入

### 原有行为

当前时间每轮都会作为独立文本块追加到顶层 `system`：

```text
Current time: 2026-06-11 20:04:00 周四 (Asia/Shanghai).
```

该行为会无条件占用 system 内容，并且包含固定地点/时区信息。

### 调整后行为

时间信息不再写入顶层 `system`。

系统查询当前会话上一条真实用户 turn 的时间。当距上次对话严格超过
10 分钟时，才在当前 `role:user` 的 content 数组中追加一个独立的
`<system-reminder>` 文本块。

最终请求结构：

```yaml
role: user
content:
  - type: text
    text: |
      <channel source="wechat">
      用户消息
      </channel>
  - type: text
    text: |
      <system-reminder>
      Current time: 2026-06-11 20:04:00 Thursday.
      The previous conversation was 12 minutes ago.
      This is a system reminder. Use this information when relevant; otherwise ignore it.
      </system-reminder>
```

### 注入规则

- 首次对话没有上一条用户消息，不注入。
- 间隔恰好 10 分钟，不注入。
- 间隔超过 10 分钟，注入。
- 阈值由 `conversation.idle_time_reminder_minutes` 配置，默认 `10`。
- 提醒内容全部使用英文。
- 不包含地点、时区名称或中文星期。
- 时间提醒紧跟当前 `<channel>` 块。
- 模型需要时使用该信息，不需要时忽略。

### 数据来源与边界

- 上次对话时间取自当前用户、当前 bot 的上一条真实用户 turn。
- 压缩产生的合成 turn 不参与间隔计算。
- 不改变 `ContextBuilder` 之外的上下文层级语义。
- 存在压缩摘要时，当前用户内容顺序保持：

```text
compressed summary
→ current channel
→ time system-reminder
```

## 验证

- 覆盖峰值 token 与累计 token 分离。
- 覆盖旧数据库无损增量迁移。
- 覆盖联系人隔离、清空后空状态和超过 100% 的百分比。
- 覆盖首次对话、10 分钟边界、超过阈值及压缩摘要顺序。
- 验证顶层 `system` 不再包含 `Current time`。
- 仓库全量测试：`159 passed`。
