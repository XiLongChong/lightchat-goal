# Goal Chat Agent 接口文档

> 版本：v1.0
>
> 日期：2026-07-24
>
> 状态：P0 接口基线
>
> 配套 PRD：`20260724-goal-chat-agent-prd.zh.md`

---

## 1. 文档目的

本文定义新项目对外提供的 HTTP 接口、公共字段、错误结构和关键业务规则。

接口名称可以在开发开始前进行一次统一调整，但下列产品能力、请求字段含义、幂等规则和响应结果必须保留。正式开发时应以本文生成 OpenAPI 3.1 文件，并通过契约测试保证文档与服务一致。

---

## 2. 通用约定

### 2.1 基础路径

```text
/api/v1
```

健康检查使用：

```text
/health/live
/health/ready
```

### 2.2 内容类型

- 普通请求与响应：`application/json; charset=utf-8`
- 图片上传：`multipart/form-data`
- 图片下载：附件实际 MIME 类型
- 二维码图片：`image/png`

### 2.3 字段命名

- JSON 字段统一使用 `camelCase`；
- URL 路径使用小写复数名词和连字符；
- 标识均视为不透明字符串，客户端不得解析其内部结构；
- 布尔值使用 `true/false`，不使用 `0/1`；
- 空列表返回 `[]`，不返回 `null`。

### 2.4 时间

- 所有时间使用带时区 ISO 8601；
- 示例：`2026-07-24T14:30:00+08:00`；
- 服务端生成时间优先使用 UTC，也可在展示字段中附带用户时区；
- 只有日期的字段使用 `YYYY-MM-DD`；
- 时区使用 IANA 名称，例如 `Asia/Shanghai`。

### 2.5 请求头

| 请求头 | 是否必需 | 用途 |
|---|---:|---|
| `Authorization: Bearer <token>` | 受保护接口必需 | App、HTTP Chat 或外部 Agent 认证 |
| `Idempotency-Key` | 写操作建议/部分接口必需 | 防止重试产生重复副作用 |
| `X-Request-Id` | 可选 | 客户端请求关联；缺失时服务端生成 |
| `Content-Type` | 有正文时必需 | 声明正文类型 |

响应头应返回 `X-Request-Id`。

### 2.6 分页

列表接口统一采用游标分页：

请求：

```text
?limit=50&cursor=<opaque>
```

响应：

```json
{
  "items": [],
  "page": {
    "limit": 50,
    "nextCursor": null,
    "hasMore": false
  }
}
```

规则：

- `limit` 默认 50；
- 最大 100；
- `cursor` 是不透明字符串；
- 排序规则必须稳定；
- 同一次翻页不得重复或漏掉稳定数据。

### 2.7 成功响应

读取单个资源直接返回资源对象。创建资源返回 `201 Created`，接受异步处理返回 `202 Accepted`，无正文成功使用 `204 No Content`。

### 2.8 错误响应

统一结构：

```json
{
  "error": {
    "code": "REMINDER_TIME_IN_PAST",
    "message": "提醒时间必须晚于当前时间",
    "details": {
      "field": "triggerAt"
    },
    "requestId": "req_01..."
  }
}
```

字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `code` | string | 稳定、可供程序判断的错误码 |
| `message` | string | 面向开发者或管理员的简明中文说明 |
| `details` | object | 可选的字段错误、候选项或恢复提示 |
| `requestId` | string | 用于日志关联 |

### 2.9 HTTP 状态码

| 状态码 | 使用场景 |
|---:|---|
| 200 | 查询成功或幂等重放成功 |
| 201 | 资源创建成功 |
| 202 | 请求已接受，结果稍后完成 |
| 204 | 成功且无响应正文 |
| 400 | JSON、查询参数或业务输入错误 |
| 401 | 未认证或凭据无效 |
| 403 | 已认证但无权限 |
| 404 | 资源不存在或不属于当前身份 |
| 409 | 幂等冲突、状态冲突或资源冲突 |
| 413 | 上传或正文过大 |
| 415 | 不支持的媒体类型 |
| 422 | 字段类型、格式或枚举校验失败 |
| 429 | 请求过于频繁 |
| 502 | 上游返回无效结果 |
| 503 | 必需依赖暂时不可用 |
| 504 | 上游超时且结果未确定 |

### 2.10 幂等规则

下列接口必须幂等：

- 发送 App 消息；
- 上传 Phone Context；
- 外部 Agent 注册；
- 外部 Agent 确认待处理消息；
- 外部 Agent 回传回复；
- 创建提醒；
- 取消提醒；
- 管理员手动发送；
- 高风险数据操作。

相同身份、相同接口、相同幂等键和相同请求内容：

- 返回首次业务结果；
- 响应结构与首次一致；
- 不产生第二次副作用；
- 可以通过 `idempotentReplay: true` 表明为重放。

相同幂等键但请求内容不同：

- 返回 `409 IDEMPOTENCY_KEY_CONFLICT`。

---

## 3. 公共数据模型

### 3.1 Message

```json
{
  "id": "msg_01...",
  "clientMessageId": "client-uuid-or-null",
  "conversationId": "conv_01...",
  "turnId": "turn_01...",
  "remoteAgentId": "default",
  "role": "user",
  "content": "你好",
  "sequence": 0,
  "attachments": [],
  "createdAt": "2026-07-24T14:30:00+08:00"
}
```

| 字段 | 类型 | 规则 |
|---|---|---|
| `id` | string | 服务端消息标识 |
| `clientMessageId` | string/null | 客户端用户消息标识 |
| `conversationId` | string | 会话标识 |
| `turnId` | string | 轮次标识 |
| `remoteAgentId` | string | 联系人标识 |
| `role` | enum | `user` 或 `agent` |
| `content` | string | 可以为空，仅当存在附件 |
| `sequence` | integer | 同轮消息顺序，从 0 或 1 开始但全局一致 |
| `attachments` | Attachment[] | 无附件时为 `[]` |
| `createdAt` | datetime | 服务端确认时间 |

### 3.2 Attachment

图片示例：

```json
{
  "id": "att_01...",
  "type": "image",
  "mimeType": "image/png",
  "fileName": "photo.png",
  "sizeBytes": 245120,
  "downloadUrl": "/api/v1/app/attachments/att_01...",
  "createdAt": "2026-07-24T14:29:00+08:00"
}
```

贴纸示例：

```json
{
  "id": "sticker-cat-wave",
  "type": "sticker",
  "stickerId": "cat-wave",
  "assetRef": "stickers/cat-wave",
  "altText": "挥手"
}
```

### 3.3 TurnResult

```json
{
  "turnId": "turn_01...",
  "conversationId": "conv_01...",
  "status": "replied",
  "agentReplies": [],
  "route": "internalAgent",
  "idempotentReplay": false,
  "startedAt": "2026-07-24T14:30:00+08:00",
  "completedAt": "2026-07-24T14:30:04+08:00"
}
```

`status` 枚举：

- `processing`
- `replied`
- `completedWithoutReply`
- `failed`
- `failedAfterPartialReply`
- `cancelled`
- `superseded`
- `interruptedAfterPartialReply`
- `limitReached`
- `resultUnknown`

### 3.4 Contact

```json
{
  "remoteAgentId": "default",
  "displayName": "My Agent",
  "description": "个人聊天 Agent",
  "avatarUrl": null,
  "online": true,
  "kind": "internal",
  "supportedMessageTypes": ["text", "image", "sticker"]
}
```

`kind`：

- `internal`
- `externalCallback`
- `externalQueue`

### 3.5 Reminder

```json
{
  "id": "rem_01...",
  "note": "开会",
  "deliveryText": "该开会啦",
  "triggerAt": "2026-07-25T09:00:00+08:00",
  "timezone": "Asia/Shanghai",
  "status": "pending",
  "createdAt": "2026-07-24T14:30:00+08:00",
  "completedAt": null
}
```

状态：

- `pending`
- `cancelled`
- `delivering`
- `sent`
- `failed`
- `resultUnknown`
- `expired`

---

## 4. 健康接口

### 4.1 `GET /health/live`

用途：判断进程是否存活。

认证：无。

成功响应：

```json
{
  "status": "alive",
  "time": "2026-07-24T14:30:00+08:00"
}
```

只要 HTTP 服务能处理请求即返回 200，不检查第三方依赖。

### 4.2 `GET /health/ready`

用途：判断服务是否具备基础聊天条件。

认证：可以无认证，但不得返回敏感配置。

成功响应：

```json
{
  "status": "ready",
  "checks": {
    "data": "ok",
    "model": "ok",
    "scheduler": "ok"
  },
  "optionalCapabilities": {
    "weather": "notConfigured",
    "web": "ok",
    "microsoftTodo": "notAuthorized",
    "wechat": "disconnected"
  }
}
```

基础条件不足时返回 503：

```json
{
  "error": {
    "code": "SERVICE_NOT_READY",
    "message": "服务尚未具备基础聊天条件",
    "details": {
      "failedChecks": ["model"]
    },
    "requestId": "req_01..."
  }
}
```

---

## 5. HTTP 对话接口

### 5.1 `POST /api/v1/chat/turns`

用途：通过 HTTP 发起一轮内置 Agent 对话。

认证：Bearer Token。

幂等：`Idempotency-Key` 必需。

请求：

```json
{
  "userId": "user_01",
  "conversationId": null,
  "messages": [
    {
      "clientMessageId": "client-msg-001",
      "content": "帮我查一下成都明天的天气"
    }
  ],
  "timezone": "Asia/Shanghai",
  "source": "http"
}
```

校验：

- `userId` 必填；
- `messages` 为 1 至服务限制条；
- 每条 `content` 非空；
- `clientMessageId` 在当前用户下可去重；
- `timezone` 必须为合法 IANA 时区；
- `conversationId` 为空时创建或选择默认会话。

成功响应：

```json
{
  "turnId": "turn_01...",
  "conversationId": "conv_01...",
  "status": "replied",
  "agentReplies": [
    {
      "id": "msg_02...",
      "role": "agent",
      "content": "成都明天预计……",
      "sequence": 1,
      "attachments": [],
      "createdAt": "2026-07-24T14:30:04+08:00"
    }
  ],
  "route": "internalAgent",
  "idempotentReplay": false,
  "startedAt": "2026-07-24T14:30:00+08:00",
  "completedAt": "2026-07-24T14:30:04+08:00"
}
```

### 5.2 `GET /api/v1/chat/turns/{turnId}`

用途：查询异步或超时轮次的最终结果。

认证：Bearer Token。

规则：

- 只能查询当前用户的轮次；
- 返回 `TurnResult`；
- 不返回内部思考或秘密；
- 不存在或无权访问统一返回 404。

---

## 6. App Profile 与联系人

### 6.1 `GET /api/v1/app/profile`

用途：获取内置 Agent 展示资料。

响应：

```json
{
  "remoteAgentId": "default",
  "displayName": "My Agent",
  "description": "个人聊天 Agent",
  "avatarUrl": null,
  "online": true,
  "supportedMessageTypes": ["text", "image", "sticker"]
}
```

### 6.2 `GET /api/v1/app/contact-invitations`

用途：获取可以添加或显示的联系人。

响应：

```json
{
  "items": [
    {
      "remoteAgentId": "default",
      "displayName": "My Agent",
      "description": "个人聊天 Agent",
      "avatarUrl": null,
      "online": true,
      "kind": "internal",
      "supportedMessageTypes": ["text", "image", "sticker"]
    }
  ]
}
```

规则：

- 内置 Agent 始终最多出现一次；
- 外部 Agent 按可展示状态返回；
- 同一 `remoteAgentId` 不重复；
- 离线状态必须准确。

---

## 7. App 消息

### 7.1 `POST /api/v1/app/messages`

用途：向指定联系人发送消息。

认证：Bearer Token。

幂等：`clientMessageId + remoteAgentId`。

请求：

```json
{
  "clientMessageId": "3bb6d13d-...",
  "remoteAgentId": "default",
  "content": "记得半小时后提醒我喝水",
  "attachments": [],
  "createdAt": "2026-07-24T14:30:00+08:00"
}
```

校验：

- `clientMessageId` 必填，最长 128 字符；
- `remoteAgentId` 必填；
- `content` 和 `attachments` 至少一个非空；
- P0 附件最多一个；
- 只接受 `image` 或 `sticker`；
- 图片必须引用已上传且当前身份可访问的附件；
- 贴纸 ID 必须存在。

成功响应：

```json
{
  "userMessage": {
    "id": "msg_user_01",
    "clientMessageId": "3bb6d13d-...",
    "conversationId": "conv_01",
    "turnId": "turn_01",
    "remoteAgentId": "default",
    "role": "user",
    "content": "记得半小时后提醒我喝水",
    "sequence": 0,
    "attachments": [],
    "createdAt": "2026-07-24T14:30:00+08:00"
  },
  "agentReplies": [
    {
      "id": "msg_agent_01",
      "clientMessageId": null,
      "conversationId": "conv_01",
      "turnId": "turn_01",
      "remoteAgentId": "default",
      "role": "agent",
      "content": "好，15:00 提醒你喝水。",
      "sequence": 1,
      "attachments": [],
      "createdAt": "2026-07-24T14:30:03+08:00"
    }
  ],
  "turn": {
    "id": "turn_01",
    "status": "replied",
    "route": "internalAgent"
  },
  "idempotentReplay": false
}
```

重复请求：

- 返回同一个 `userMessage`、`agentReplies` 和 `turn`；
- `idempotentReplay` 为 `true`；
- 附件字段不得缺失。

### 7.2 `GET /api/v1/app/messages`

查询参数：

| 参数 | 必需 | 说明 |
|---|---:|---|
| `remoteAgentId` | 是 | 联系人 |
| `limit` | 否 | 默认 50，最大 100 |
| `cursor` | 否 | 分页游标 |
| `after` | 否 | 可选的增量同步时间 |

响应：

```json
{
  "items": [],
  "page": {
    "limit": 50,
    "nextCursor": null,
    "hasMore": false
  },
  "remoteAgentId": "default"
}
```

排序：

1. `createdAt`；
2. `turnId`；
3. `sequence`；
4. `id`。

---

## 8. App 附件

### 8.1 `POST /api/v1/app/attachments`

认证：Bearer Token，且必须在读取大文件正文前校验。

请求：

```text
Content-Type: multipart/form-data
file=<binary>
```

限制：

- 仅 `image/*`；
- 文件非空；
- 最大 10 MB；
- 单请求一个文件；
- 文件名仅用于展示。

成功：`201 Created`

```json
{
  "attachment": {
    "id": "att_01",
    "type": "image",
    "mimeType": "image/png",
    "fileName": "photo.png",
    "sizeBytes": 245120,
    "downloadUrl": "/api/v1/app/attachments/att_01",
    "createdAt": "2026-07-24T14:29:00+08:00"
  }
}
```

### 8.2 `GET /api/v1/app/attachments/{attachmentId}`

用途：下载当前身份可访问的附件。

响应：

- 正确的 `Content-Type`；
- 安全的 `Content-Disposition`；
- 支持缓存校验；
- 不暴露服务端路径。

错误：

- 不存在或无权访问：404；
- 文件元数据存在但文件不可读：503，并记录后台诊断。

---

## 9. Phone Context

### 9.1 `POST /api/v1/app/phone-context/snapshots`

幂等：`snapshotId`。

请求：

```json
{
  "snapshotId": "snap_01",
  "type": "appStatus",
  "reason": "activeContactResolved",
  "observedAt": "2026-07-24T14:30:00+08:00",
  "payload": {
    "appInForeground": true,
    "screen": "chat",
    "activeRemoteAgentId": "default"
  }
}
```

允许类型：

- `deviceStatus`
- `appStatus`

允许原因：

- `foregroundStart`
- `foregroundTick`
- `activeChatChanged`
- `activeContactResolved`
- `screenChanged`
- `backgroundSync`
- `manualDebug`

成功：

```json
{
  "accepted": true,
  "snapshotId": "snap_01",
  "receivedAt": "2026-07-24T14:30:01+08:00",
  "idempotentReplay": false
}
```

### 9.2 `GET /api/v1/app/phone-context/latest`

查询参数：

| 参数 | 必需 | 说明 |
|---|---:|---|
| `type` | 否 | 限定快照类型 |
| `maxAgeSeconds` | 否 | 最大有效年龄 |

有数据：

```json
{
  "available": true,
  "stale": false,
  "ageSeconds": 8,
  "snapshot": {
    "snapshotId": "snap_01",
    "type": "appStatus",
    "reason": "activeContactResolved",
    "observedAt": "2026-07-24T14:30:00+08:00",
    "payload": {
      "appInForeground": true,
      "screen": "chat",
      "activeRemoteAgentId": "default"
    }
  }
}
```

无数据：

```json
{
  "available": false,
  "stale": false,
  "ageSeconds": null,
  "snapshot": null
}
```

---

## 10. 外部 Agent 会话

### 10.1 `POST /api/v1/external-agent-sessions`

用途：注册外部 Agent。

幂等：`registrationId`。

请求：

```json
{
  "registrationId": "reg-client-01",
  "remoteAgentId": "coding-agent",
  "displayName": "Coding Agent",
  "description": "处理编程任务",
  "kind": "queue",
  "callbackUrl": null,
  "capabilities": {
    "messageTypes": ["text", "image"],
    "replyTypes": ["text", "sticker"]
  }
}
```

`kind`：

- `callback`
- `queue`

规则：

- `callback` 必须提供允许的 `callbackUrl`；
- `queue` 不应提供回调地址；
- `remoteAgentId` 必须稳定；
- 同一注册请求重放返回同一会话；
- 同一联系人存在在线会话时按冲突规则处理。

成功：`201 Created`

```json
{
  "sessionId": "session_01",
  "remoteAgentId": "coding-agent",
  "status": "online",
  "heartbeatIntervalSeconds": 20,
  "expiresAfterSeconds": 60,
  "registeredAt": "2026-07-24T14:30:00+08:00"
}
```

### 10.2 `POST /api/v1/external-agent-sessions/{sessionId}/heartbeat`

请求：

```json
{
  "sentAt": "2026-07-24T14:30:20+08:00"
}
```

响应：

```json
{
  "sessionId": "session_01",
  "status": "online",
  "lastSeenAt": "2026-07-24T14:30:20+08:00",
  "expiresAt": "2026-07-24T14:31:20+08:00"
}
```

### 10.3 `POST /api/v1/external-agent-sessions/{sessionId}/offline`

成功：

```json
{
  "sessionId": "session_01",
  "status": "offline",
  "offlineAt": "2026-07-24T14:35:00+08:00"
}
```

重复下线返回同一终态。

### 10.4 `GET /api/v1/external-agent-sessions`

用途：当前外部 Agent 查询自己的会话，管理员使用单独接口查询全部会话。

支持状态和联系人筛选。

---

## 11. 外部 Agent 队列

### 11.1 `GET /api/v1/external-agent-sessions/{sessionId}/pending-messages`

查询参数：

- `limit`：默认 10，最大 50；
- `waitSeconds`：可选长轮询时长；
- `cursor`：可选游标。

响应：

```json
{
  "items": [
    {
      "deliveryId": "delivery_01",
      "clientMessageId": "client-msg-01",
      "remoteAgentId": "coding-agent",
      "turnId": "turn_01",
      "content": "帮我解释这张图",
      "attachments": [
        {
          "id": "att_01",
          "type": "image",
          "mimeType": "image/png",
          "fileName": "diagram.png",
          "sizeBytes": 245120,
          "downloadUrl": "/api/v1/app/attachments/att_01"
        }
      ],
      "createdAt": "2026-07-24T14:30:00+08:00"
    }
  ],
  "page": {
    "limit": 10,
    "nextCursor": null,
    "hasMore": false
  }
}
```

### 11.2 `POST /api/v1/external-agent-sessions/{sessionId}/pending-messages/{deliveryId}/ack`

用途：确认消息已被外部 Agent 接收。

幂等：`deliveryId`。

请求：

```json
{
  "acceptedAt": "2026-07-24T14:30:01+08:00"
}
```

响应：

```json
{
  "deliveryId": "delivery_01",
  "status": "acknowledged",
  "acknowledgedAt": "2026-07-24T14:30:01+08:00",
  "idempotentReplay": false
}
```

### 11.3 `POST /api/v1/external-agent-sessions/{sessionId}/replies`

幂等：`replyBatchId`。

请求：

```json
{
  "replyBatchId": "batch_01",
  "clientMessageId": "client-msg-01",
  "replies": [
    {
      "clientReplyId": "reply-01",
      "content": "这张图展示的是……",
      "attachments": []
    },
    {
      "clientReplyId": "reply-02",
      "content": "如果你愿意，我可以继续逐块解释。",
      "attachments": []
    }
  ]
}
```

成功：

```json
{
  "turnId": "turn_01",
  "acceptedReplies": [
    {
      "clientReplyId": "reply-01",
      "messageId": "msg_agent_01",
      "sequence": 1
    },
    {
      "clientReplyId": "reply-02",
      "messageId": "msg_agent_02",
      "sequence": 2
    }
  ],
  "idempotentReplay": false
}
```

规则：

- `clientMessageId` 必须属于该会话联系人；
- `replies` 最大数量受配置限制；
- P0 回复附件只支持贴纸；
- 重复 `clientReplyId` 不重复保存；
- 未知用户消息返回 404；
- 会话离线时是否允许完成已领取消息必须在配置中固定，默认允许短暂宽限。

---

## 12. 外部 Agent 回调契约

回调请求由 Goal Chat Agent 发往注册地址。

### 12.1 请求

```json
{
  "eventId": "event_01",
  "eventType": "message.created",
  "sessionId": "session_01",
  "remoteAgentId": "coding-agent",
  "deliveryId": "delivery_01",
  "message": {
    "id": "msg_user_01",
    "clientMessageId": "client-msg-01",
    "turnId": "turn_01",
    "role": "user",
    "content": "你好",
    "attachments": [],
    "createdAt": "2026-07-24T14:30:00+08:00"
  },
  "sentAt": "2026-07-24T14:30:00+08:00"
}
```

请求应包含签名或会话认证信息。回调接收方必须按 `eventId` 幂等处理。

### 12.2 同步响应

```json
{
  "accepted": true,
  "replies": [
    {
      "clientReplyId": "reply-01",
      "content": "你好呀",
      "attachments": []
    }
  ]
}
```

规则：

- `accepted: true` 且空回复表示已处理但无需回复；
- 多条回复保持数组顺序；
- 非 JSON、缺少 `accepted` 或字段非法视为上游无效响应；
- HTTP 超时视为结果未知，是否重试必须遵守回调幂等规则。

---

## 13. 管理员认证

管理员接口使用 `/api/v1/admin`。

### 13.1 `GET /api/v1/admin/auth/status`

响应：

```json
{
  "authenticated": false,
  "defaultPassword": true,
  "sessionExpiresAt": null
}
```

### 13.2 `POST /api/v1/admin/auth/login`

请求：

```json
{
  "password": "..."
}
```

成功：

```json
{
  "authenticated": true,
  "defaultPassword": false,
  "sessionExpiresAt": "2026-07-31T14:30:00+08:00"
}
```

认证会话通过安全 Cookie 或等价安全机制返回，不在 JSON 中回显会话秘密。

### 13.3 `POST /api/v1/admin/auth/logout`

成功返回 `204 No Content` 并使当前会话失效。

---

## 14. 管理概览与配置

### 14.1 `GET /api/v1/admin/overview`

响应：

```json
{
  "service": {
    "status": "ready",
    "startedAt": "2026-07-24T10:00:00+08:00"
  },
  "model": {
    "status": "ok",
    "name": "configured-model"
  },
  "wechat": {
    "configured": true,
    "running": true,
    "sendReady": true,
    "lastReceivedAt": "2026-07-24T14:20:00+08:00",
    "lastSentAt": "2026-07-24T14:20:03+08:00"
  },
  "app": {
    "available": true,
    "onlineExternalAgents": 1
  },
  "schedule": {
    "pendingReminders": 2,
    "nextSummaryAt": "2026-07-25T03:56:00+08:00",
    "proactiveTodayUsed": 1,
    "proactiveDailyLimit": 3
  },
  "recentError": null
}
```

### 14.2 `GET /api/v1/admin/config`

用途：获取可公开的运行设置、人格信息和依赖状态。

不得返回秘密原文。

### 14.3 `PATCH /api/v1/admin/settings`

请求仅允许白名单设置，例如：

```json
{
  "timezone": "Asia/Shanghai",
  "inboundMode": "both",
  "weather": {
    "defaultLocation": "成都",
    "enabled": true
  },
  "proactive": {
    "enabled": true,
    "frequency": "moderate",
    "quietHours": {
      "start": "23:00",
      "end": "08:00",
      "timezone": "Asia/Shanghai"
    }
  }
}
```

响应：

```json
{
  "settings": {},
  "effective": "nextTurn",
  "restartRequired": false
}
```

### 14.4 `GET /api/v1/admin/context-usage`

响应：

```json
{
  "available": true,
  "turnId": "turn_01",
  "model": "configured-model",
  "promptTokensPeak": 12000,
  "contextWindowTokens": 64000,
  "usageRatio": 0.1875,
  "estimated": false,
  "measuredAt": "2026-07-24T14:30:04+08:00"
}
```

---

## 15. 管理人格

### 15.1 `GET /api/v1/admin/persona`

响应：

```json
{
  "file": "prompts/persona.md",
  "content": "# 人格\n...",
  "valid": true,
  "lastLoadedAt": "2026-07-24T10:00:00+08:00",
  "effectiveFrom": "current"
}
```

### 15.2 `PUT /api/v1/admin/persona`

请求：

```json
{
  "file": "prompts/persona.md",
  "content": "# 人格\n..."
}
```

规则：

- `content` 非空；
- UTF-8 Markdown；
- 文件必须位于允许的人格目录；
- 保存前校验；
- 保存失败继续使用最近有效版本；
- 不接受密钥字段；
- 生效范围必须明确。

响应：

```json
{
  "saved": true,
  "valid": true,
  "file": "prompts/persona.md",
  "effectiveFrom": "nextTurn"
}
```

---

## 16. 管理手动消息与数据

### 16.1 `POST /api/v1/admin/messages`

请求：

```json
{
  "userId": "user_01",
  "channel": "wechat",
  "messages": [
    {
      "text": "第一条",
      "delayMilliseconds": 0
    },
    {
      "text": "第二条",
      "delayMilliseconds": 500
    }
  ]
}
```

响应：

```json
{
  "deliveries": [
    {
      "messageId": "msg_01",
      "status": "sent"
    },
    {
      "messageId": "msg_02",
      "status": "sent"
    }
  ]
}
```

### 16.2 `POST /api/v1/admin/data-reset-jobs`

用途：创建明确范围的数据清理任务。

请求：

```json
{
  "userId": "user_01",
  "scopes": ["conversationHistory", "summaries", "longTermMemory"],
  "keep": ["wechatBinding", "persona", "settings"],
  "confirmation": "RESET"
}
```

响应：`201 Created`

```json
{
  "jobId": "reset_01",
  "status": "completed",
  "cleared": {
    "messages": 120,
    "summaries": 5,
    "memoryItems": 3
  },
  "kept": ["wechatBinding", "persona", "settings"]
}
```

---

## 17. 管理记忆

### 17.1 `GET /api/v1/admin/memories`

查询参数：

- `userId` 必需；
- `limit`；
- `cursor`。

响应项：

```json
{
  "name": "preferences.md",
  "title": "交流偏好",
  "sizeBytes": 1250,
  "updatedAt": "2026-07-20T10:00:00+08:00",
  "warnings": []
}
```

### 17.2 `GET /api/v1/admin/memories/{name}`

查询参数：`userId`。

响应：

```json
{
  "name": "preferences.md",
  "content": "# 交流偏好\n...",
  "sizeBytes": 1250,
  "updatedAt": "2026-07-20T10:00:00+08:00",
  "warnings": []
}
```

### 17.3 `DELETE /api/v1/admin/memories/{name}`

查询参数：`userId`。

请求头：`Idempotency-Key`。

响应：

```json
{
  "deleted": true,
  "name": "preferences.md",
  "bytesRemoved": 1250,
  "idempotentReplay": false
}
```

路径不合法、索引文件保护或不属于目标用户时返回 400/404。

---

## 18. 管理提醒、摘要和主动对话

### 18.1 `GET /api/v1/admin/reminders`

查询参数：

- `userId`；
- `status`；
- `limit`；
- `cursor`。

返回分页 `Reminder`。

### 18.2 `POST /api/v1/admin/reminders`

请求：

```json
{
  "userId": "user_01",
  "note": "开会",
  "deliveryText": "该开会啦",
  "triggerAt": "2026-07-25T09:00:00+08:00",
  "timezone": "Asia/Shanghai"
}
```

成功返回 `201 Created` 和 `Reminder`。

### 18.3 `POST /api/v1/admin/reminders/{reminderId}/cancel`

响应返回更新后的 `Reminder`。

### 18.4 `GET /api/v1/admin/compression/status`

响应：

```json
{
  "enabled": true,
  "nextRunAt": "2026-07-25T03:56:00+08:00",
  "lastRun": {
    "status": "succeeded",
    "completedAt": "2026-07-24T03:56:12+08:00"
  }
}
```

### 18.5 `POST /api/v1/admin/compression-runs`

请求：

```json
{
  "userId": "user_01",
  "date": "2026-07-24"
}
```

返回：

- 同步完成可返回 201；
- 异步执行返回 202 和运行编号；
- 同日已有有效摘要时返回已有结果或 409，规则必须固定。

### 18.6 `GET /api/v1/admin/compression-runs`

返回日期、状态、内容预览、触发来源、开始时间、完成时间和错误摘要。

### 18.7 `GET /api/v1/admin/proactive-policy`

响应：

```json
{
  "enabled": true,
  "frequency": "moderate",
  "dailyLimit": 3,
  "todayUsed": 1,
  "quietHours": {
    "start": "23:00",
    "end": "08:00",
    "timezone": "Asia/Shanghai"
  },
  "nextEligibleAt": "2026-07-24T18:00:00+08:00"
}
```

### 18.8 `PUT /api/v1/admin/proactive-policy`

允许修改开关、频率和安静时段。保存后返回完整生效策略。

---

## 19. 管理微信

### 19.1 `POST /api/v1/admin/wechat/deployments`

用途：发起扫码连接。

响应：`201 Created`

```json
{
  "deploymentId": "wxdep_01",
  "status": "waiting",
  "qrContent": "...",
  "qrImageUrl": "/api/v1/admin/wechat/deployments/wxdep_01/qr-image",
  "expiresAt": "2026-07-24T14:35:00+08:00"
}
```

### 19.2 `GET /api/v1/admin/wechat/deployments/{deploymentId}`

状态：

- `waiting`
- `scanned`
- `confirmed`
- `expired`
- `failed`

确认成功后返回：

```json
{
  "deploymentId": "wxdep_01",
  "status": "confirmed",
  "configured": true,
  "running": true,
  "credential": {
    "configured": true,
    "masked": "...abc123"
  }
}
```

### 19.3 `GET /api/v1/admin/wechat/deployments/{deploymentId}/qr-image`

返回 `image/png`。

### 19.4 `POST /api/v1/admin/wechat/start`

成功：

```json
{
  "running": true,
  "startedAt": "2026-07-24T14:30:00+08:00"
}
```

### 19.5 `POST /api/v1/admin/wechat/stop`

成功：

```json
{
  "running": false,
  "stoppedAt": "2026-07-24T14:35:00+08:00"
}
```

### 19.6 `GET /api/v1/admin/wechat/status`

响应：

```json
{
  "configured": true,
  "running": true,
  "inboundSeen": true,
  "sendReady": true,
  "lastReceivedAt": "2026-07-24T14:20:00+08:00",
  "lastSentAt": "2026-07-24T14:20:03+08:00",
  "lastError": null
}
```

---

## 20. 管理外部 Agent 与 App

### 20.1 `GET /api/v1/admin/external-agent-sessions`

响应项：

```json
{
  "sessionId": "session_01",
  "remoteAgentId": "coding-agent",
  "displayName": "Coding Agent",
  "kind": "queue",
  "status": "online",
  "lastSeenAt": "2026-07-24T14:30:20+08:00",
  "expiresAt": "2026-07-24T14:31:20+08:00",
  "callbackUrl": null,
  "pendingMessages": 0
}
```

### 20.2 `POST /api/v1/admin/external-agent-sessions/{sessionId}/offline`

强制下线。重复请求返回同一离线结果。

### 20.3 `GET /api/v1/admin/phone-context/latest`

返回最新 Phone Context、年龄和过期状态。敏感扩展字段默认不返回。

### 20.4 `GET /api/v1/admin/attachments`

支持类型、时间和引用状态筛选。

响应只包含附件元数据，不包含服务端真实路径。

---

## 21. 管理能力与日志

### 21.1 `GET /api/v1/admin/capabilities`

响应：

```json
{
  "items": [
    {
      "id": "weather",
      "name": "天气查询",
      "purpose": "查询实时天气和预报",
      "enabled": true,
      "status": "available",
      "dependency": {
        "configured": true,
        "authorized": true
      },
      "sideEffect": "read",
      "lastCheckedAt": "2026-07-24T14:30:00+08:00",
      "message": null
    }
  ]
}
```

能力 ID 建议：

- `messageDelivery`
- `time`
- `weather`
- `webResearch`
- `longTermMemory`
- `microsoftTodo`
- `reminders`
- `proactiveConversation`

### 21.2 `GET /api/v1/admin/logs`

查询参数：

| 参数 | 说明 |
|---|---|
| `level` | 日志级别 |
| `contains` | 关键字 |
| `requestId` | 请求编号 |
| `turnId` | 轮次编号 |
| `limit` | 默认 200，最大 1000 |
| `cursor` | 分页游标 |

响应项：

```json
{
  "time": "2026-07-24T14:30:04+08:00",
  "level": "ERROR",
  "event": "capability.failed",
  "message": "天气服务暂时不可用",
  "requestId": "req_01",
  "turnId": "turn_01"
}
```

日志接口必须执行脱敏。

---

## 22. 错误码目录

### 22.1 通用

| 错误码 | HTTP | 含义 |
|---|---:|---|
| `INVALID_JSON` | 400 | JSON 无法解析 |
| `VALIDATION_FAILED` | 422 | 字段校验失败 |
| `UNAUTHENTICATED` | 401 | 未认证 |
| `FORBIDDEN` | 403 | 无权限 |
| `RESOURCE_NOT_FOUND` | 404 | 资源不存在 |
| `STATE_CONFLICT` | 409 | 当前状态不允许操作 |
| `IDEMPOTENCY_KEY_CONFLICT` | 409 | 相同幂等键对应不同请求 |
| `RATE_LIMITED` | 429 | 请求过多 |
| `DEPENDENCY_UNAVAILABLE` | 503 | 依赖不可用 |
| `UPSTREAM_TIMEOUT` | 504 | 上游超时 |
| `INTERNAL_ERROR` | 500 | 未预期错误 |

### 22.2 对话与消息

| 错误码 | HTTP | 含义 |
|---|---:|---|
| `EMPTY_MESSAGE` | 422 | 文字和附件均为空 |
| `MESSAGE_TOO_LONG` | 413 | 消息过长 |
| `UNKNOWN_REMOTE_AGENT` | 404 | 联系人不存在 |
| `REMOTE_AGENT_OFFLINE` | 409 | 外部 Agent 离线 |
| `TURN_LIMIT_REACHED` | 409 | 轮次达到安全上限 |
| `DELIVERY_FAILED` | 502 | 渠道确认发送失败 |
| `DELIVERY_RESULT_UNKNOWN` | 504 | 渠道结果未知 |

### 22.3 附件

| 错误码 | HTTP | 含义 |
|---|---:|---|
| `ATTACHMENT_REQUIRED` | 422 | 缺少文件 |
| `ATTACHMENT_EMPTY` | 422 | 空文件 |
| `ATTACHMENT_TOO_LARGE` | 413 | 超过 10 MB |
| `UNSUPPORTED_ATTACHMENT_TYPE` | 415 | 不支持的类型 |
| `ATTACHMENT_NOT_FOUND` | 404 | 附件不存在 |
| `UNKNOWN_STICKER` | 422 | 贴纸 ID 不存在 |

### 22.4 Phone Context

| 错误码 | HTTP | 含义 |
|---|---:|---|
| `INVALID_SNAPSHOT_TYPE` | 422 | 快照类型无效 |
| `INVALID_SNAPSHOT_REASON` | 422 | 上传原因无效 |
| `SNAPSHOT_TOO_OLD` | 409 | 快照过旧 |
| `SNAPSHOT_FROM_FUTURE` | 422 | 时间明显超前 |

### 22.5 提醒

| 错误码 | HTTP | 含义 |
|---|---:|---|
| `REMINDER_TIME_IN_PAST` | 422 | 时间不在未来 |
| `REMINDER_TIME_TOO_CLOSE` | 422 | 时间过近 |
| `REMINDER_TIME_TOO_FAR` | 422 | 超过允许范围 |
| `REMINDER_NOT_FOUND` | 404 | 提醒不存在 |
| `REMINDER_ALREADY_SENT` | 409 | 已发送，不能取消 |

### 22.6 人格与记忆

| 错误码 | HTTP | 含义 |
|---|---:|---|
| `PERSONA_EMPTY` | 422 | 人格内容为空 |
| `PERSONA_FILE_NOT_ALLOWED` | 403 | 文件不在允许目录 |
| `PERSONA_SAVE_FAILED` | 503 | 保存失败，继续使用有效版本 |
| `MEMORY_NOT_FOUND` | 404 | 记忆不存在 |
| `MEMORY_PATH_INVALID` | 422 | 名称或路径无效 |
| `MEMORY_PROTECTED` | 409 | 受保护内容不能直接删除 |

### 22.7 外部 Agent

| 错误码 | HTTP | 含义 |
|---|---:|---|
| `SESSION_NOT_FOUND` | 404 | 会话不存在 |
| `SESSION_EXPIRED` | 409 | 会话已过期 |
| `REMOTE_AGENT_ALREADY_ONLINE` | 409 | 联系人已有冲突会话 |
| `CALLBACK_URL_NOT_ALLOWED` | 422 | 回调地址不允许 |
| `CALLBACK_RESPONSE_INVALID` | 502 | 回调响应无效 |
| `PENDING_MESSAGE_NOT_FOUND` | 404 | 待处理消息不存在 |
| `REPLY_TARGET_NOT_FOUND` | 404 | 原用户消息不存在 |

---

## 23. 契约验收清单

### 23.1 通用

- 所有接口在 OpenAPI 中有请求、响应和错误定义；
- 字段命名、时间和错误结构一致；
- 未认证请求在解析大正文前返回 401；
- 写操作具备幂等测试；
- 相同幂等键、不同正文返回 409；
- 不存在与无权访问均不泄露资源存在性；
- 日志和错误中无秘密。

### 23.2 App

- 首次发送和重复发送响应结构一致；
- `agentReplies[]` 一项对应一条真实回复；
- 联系人历史严格隔离；
- 附件字段在所有消息响应中存在；
- 图片大小、类型和访问权限均有测试；
- Phone Context 成功响应包含 `accepted: true`；
- 过期快照明确标记。

### 23.3 外部 Agent

- 注册、心跳、过期和下线状态完整；
- 队列消息在重启后可恢复；
- ACK 和回复提交幂等；
- 回复不能写入其他联系人；
- 回调地址安全规则有测试；
- 多条回复顺序稳定。

### 23.4 管理后台

- 默认密码告警可见；
- 配置接口不返回秘密；
- 人格保存失败不影响有效版本；
- 清理接口明确列出保留项；
- 微信、模型和能力状态可诊断；
- 日志筛选与脱敏有效。

---

## 24. OpenAPI 交付要求

开发开始时应建立：

```text
contracts/openapi/goal-chat-agent.v1.yaml
```

OpenAPI 文件必须：

- 使用 OpenAPI 3.1；
- 覆盖本文全部 P0 接口；
- 定义 Bearer 与管理员会话认证；
- 为写接口标注幂等要求；
- 为所有枚举建立 schema；
- 提供关键请求和响应示例；
- 定义统一错误结构；
- 通过 lint；
- 在 CI 中验证服务响应与契约；
- 由客户端生成或校验 DTO；
- 任何破坏性变更必须升级接口版本或经过明确发布决策。
