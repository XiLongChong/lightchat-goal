# Attached Session Callback Protocol v1（2026-06-19）

## 概述

本文档定义 PC Router 与 attached agent session 之间的 callback 协议。

**关键声明：本协议不是 Anthropic Messages API、OpenAI Chat Completions、或 Google A2A 的变体或扩展。** 它是一个独立的、App-visible 的 reply contract，仅关注"用户从手机 App 上看到的聊天气泡"。

## 设计原则

- **单向转发**：PC Router 只将用户消息转发给 session，session 返回 reply 列表。
- **纯文本**：v1 只支持 `content` string。多媒体、附件、图片、语音等暂不支持。
- **非 streaming**：一次请求，一次完整响应。不做 SSE/chunked。
- **非原生 provider 协议**：自研 agent 如内部使用 Anthropic content blocks、tool_use、tool_result、thinking 等，**必须由 session adapter 层转换为本协议的 `agentReplies[]` 再返回给 Router**。Router 不负责解析 provider 原生格式。
- **Router 负责 validation**：Router 在接收 callback response 后校验格式，不符合协议的直接拒绝（未来实现；当前本阶段不做 HTTP forwarding）。

## 请求格式

### PC Router → attached session

```json
{
  "remoteAgentId": "claude-demo",
  "sessionId": "claude-session-1",
  "clientMessageId": "cm-001",
  "content": "你好",
  "createdAt": "2026-06-19T12:00:00Z"
}
```

| 字段 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `remoteAgentId` | string | 是 | 目标 agent 标识 |
| `sessionId` | string | 是 | 注册时的 sessionId |
| `clientMessageId` | string | 是 | 客户端幂等键 |
| `content` | string | 是 | 用户文本 |
| `createdAt` | string | 是 | ISO 8601 UTC Z |

## 响应格式

### attached session → PC Router（成功）

```json
{
  "agentReplies": [
    {
      "content": "你好，我在。",
      "remoteId": "optional-remote-id",
      "createdAt": "2026-06-19T12:00:02Z"
    },
    {
      "content": "有什么可以帮你的？"
    }
  ]
}
```

| 字段 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `agentReplies` | array of reply objects | 是 | 回复列表，可包含多条 |
| `agentReplies[].content` | string | 是 | 回复文本 |
| `agentReplies[].remoteId` | string | 否 | session 侧的消息 ID（用于去重/调试） |
| `agentReplies[].createdAt` | string | 否 | ISO 8601 UTC Z |

**注意：`agentReplies` 可以为空数组 `[]`，表示 session 选择不回复。**

### 响应校验规则（Router 侧）

Router 在接收 callback response 后必须校验：

| 规则 | 违反时行为 |
|------|-----------|
| 顶层必须是 JSON object | 拒绝（400） |
| 顶层不能是数组 | 拒绝（400） |
| `agentReplies` 必须存在 | 拒绝（400） |
| `agentReplies` 必须是 list | 拒绝（400） |
| 每个 reply 必须是 dict | 跳过该项 |
| `content` 必须存在且为 string | 跳过该项 |
| `content` 为空字符串 | 跳过该项 |
| 多余的未知字段 | 忽略（向前兼容） |

## Adapter 转换示例

### 自研 agent 内部 Anthropic-like content blocks

自研 agent 内部可能这样表示 assistant 回复：

```json
{
  "role": "assistant",
  "content": [
    { "type": "text", "text": "你好，我在。" }
  ]
}
```

**adapter 必须在发送 callback response 前将其转换为：**

```json
{
  "agentReplies": [
    { "content": "你好，我在。" }
  ]
}
```

### 带 tool_use 的 Anthropic-like 响应

```json
{
  "role": "assistant",
  "content": [
    { "type": "text", "text": "让我查一下天气。" },
    {
      "type": "tool_use",
      "id": "toolu_01",
      "name": "get_weather",
      "input": { "city": "成都" }
    }
  ]
}
```

**adapter 转换后（tool_use 不出现在 App 气泡）：**

```json
{
  "agentReplies": [
    { "content": "让我查一下天气。" }
  ]
}
```

**注意**：如果 `text` 块在 `tool_use` 之前是空或仅空白，则 `agentReplies` 应为空数组——adapter 负责判断。

### 带 thinking 的 Anthropic-like 响应

thinking 块**永不进入** App 气泡。adapter 必须过滤。

### 多 text 块的 Anthropic-like 响应

```json
{
  "role": "assistant",
  "content": [
    { "type": "text", "text": "第一段。" },
    { "type": "text", "text": "第二段。" }
  ]
}
```

**adapter 转换（合并或分条均可）：**

```json
{
  "agentReplies": [
    { "content": "第一段。" },
    { "content": "第二段。" }
  ]
}
```

或合并为一条：

```json
{
  "agentReplies": [
    { "content": "第一段。\n\n第二段。" }
  ]
}
```

v1 不做硬性规定，adapter 自行选择。

## 明确拒绝 / 暂不支持

| 项 | 说明 |
|----|------|
| 响应顶层直接返回数组 `[...]` | 必须用 object 包裹 |
| `agentReplies[].content` 为 Anthropic content block `{type, text}` | 必须转为纯 string |
| `tool_use` / `tool_result` 作为聊天气泡 | 永不暴露给 App |
| `thinking` 块进入聊天气泡 | 永不暴露给 App |
| 附件 / 图片 / 语音 | v1 不支持，字段预留 |
| streaming / SSE | v1 不支持 |
| Router 吃原生 provider response | Router 不负责解析 Anthropic/OpenAI 格式 |

## 未来扩展预留（v1 不实现）

```json
{
  "agentReplies": [
    {
      "content": "这是一张图片",
      "attachments": [
        {
          "type": "image",
          "uri": "data:image/png;base64,...",
          "mimeType": "image/png"
        }
      ]
    }
  ]
}
```

- `attachments` 字段已预留但 v1 Router 忽略。
- `remoteId` / `createdAt` 字段已预留，v1 Router 可选使用。

## 测试计划

| 测试 | 说明 |
|------|------|
| valid simple text response accepted | `{"agentReplies": [{"content": "hi"}]}` |
| missing agentReplies rejected | `{}` → 拒绝 |
| top-level array rejected | `[{...}]` → 拒绝 |
| agentReplies not list rejected | `{"agentReplies": "hi"}` → 拒绝 |
| reply content not string skipped | `{"agentReplies": [{"content": 123}]}` → 跳过 |
| empty content skipped | `{"agentReplies": [{"content": ""}]}` → 跳过 |
| mixed valid + invalid entries | 保留 valid，跳过 invalid |
| unknown fields ignored | `{"agentReplies": [{"content":"hi","unknown":1}]}` → 接受 |
| Anthropic content block must be converted by adapter | Router validator 不接受 `{type,text}` 作为 content |

当前阶段只实现 validator helper（`validate_callback_response`），不接入生产路径。测试确保验证逻辑正确。
