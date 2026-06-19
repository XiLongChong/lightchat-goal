# External Attached Agent Sessions 设计（2026-06-19）

## 概述

本文档设计外部 agent 会话（如 Claude Code、Codex）如何通过 attached_session 模式接入手机 App。重点覆盖"用户主动启动的会话"，而非 PC Router 启动进程。

## 1. 产品语义

### 1.1 外部 agent 会话不是常驻联系人

- 外部 agent 会话（Claude Code、Codex）是**临时性、任务导向**的交互入口。
- 与内置 agent（如 Goal Chat Agent）不同，它们不是常驻联系人，不会默认出现在 App 联系人列表中。
- 只有用户**显式启动并挂接**到 bridge/MCP 的会话才会出现在手机 App。

### 1.2 显式挂接原则

- 未挂接的 Claude Code/Codex 进程**不进入手机**。
- 挂接是用户主动行为，不是自动发现。
- 挂接通过 adapter/bridge 在启动时向 Router 注册实现。

### 1.3 会话生命周期

- 会话启动 → adapter 注册 → App 显示为"好友申请"或新联系人
- 用户接受 → 可发送消息 → adapter 转发到外部 agent
- 会话结束 → adapter 调用 offline → 状态变为 offline
- offline 后是否保留联系人由 App/用户后续决策（本阶段不定义）

### 1.4 手机不能启动外部 agent

- 手机 App **不能默认启动**外部 agent 进程。
- 本阶段只支持 **attach**（挂接已存在的会话），不支持从 App 触发启动。
- 启动权限属于 PC 端用户，不属于 Router 或 App。

## 2. 当前已打通链路

以下端点和机制已实现并通过 Echo Session E2E 验证：

### 2.1 注册端点

```
POST /api/app-agent-sessions/register
```

- Body: `sessionId`, `remoteAgentId`, `displayName`, `description`, `callbackUrl`, `sourceLabel`
- Auth: `CHAT_API_KEY` (Bearer / `x-api-key`)
- 409 if `remoteAgentId` 已被其他 session 占用

### 2.2 联系人邀请

```
GET /api/contact-invitations
```

- 返回 App 可见的联系人列表
- 包含已注册的 attached sessions
- Auth: 同上

### 2.3 消息路由

```
POST /api/messages
GET /api/messages?remoteAgentId=...
```

- `remoteAgentId` 决定路由目标
- `default` → 内置 agent
- 非 `default` → attached session callback
- `app_messages` 按 `remoteAgentId` 隔离存储

### 2.4 Callback 协议

```
POST {callbackUrl}/api/app-session/messages
```

请求：
```json
{
  "remoteAgentId": "claude-demo",
  "sessionId": "claude-session-1",
  "clientMessageId": "cm-001",
  "content": "你好",
  "createdAt": "2026-06-19T12:00:00Z"
}
```

响应（v1 contract）：
```json
{
  "agentReplies": [
    {
      "content": "你好，我在。",
      "remoteId": "optional-remote-id",
      "createdAt": "2026-06-19T12:00:02Z"
    }
  ]
}
```

详见 [docs/doing/app-attached-session-callback-v1.md](app-attached-session-callback-v1.md)。

### 2.5 Router 生成的 message id

- Router 为每个 agent reply 生成 `app_rm_agent_<uuid>` 作为本地主键
- Session callback 的 `remoteId` 存入 `source_tool_call_id` 用于追踪
- 避免跨 session id 冲突

### 2.6 Echo Session E2E

- `tools/app_session_echo_server.py` 已验证完整链路
- App → Router → Echo Server → Router → App
- 详见 [docs/doing/app-attached-session-e2e.md](app-attached-session-e2e.md)

## 3. 外部 session adapter 形态

### 3.1 方案 A: HTTP callback adapter

**描述**

- 外部 agent 启动时，同时启动一个本地 HTTP server（如 `127.0.0.1:9010`）
- Adapter 向 Router 注册，`callbackUrl` 指向该 HTTP server
- Router 收到 App 消息后，HTTP POST 到 adapter
- Adapter 将消息送入外部 agent，收集回复后返回 `agentReplies`

**优点**

- 最贴合当前 Echo Session 实现
- 协议简单，调试容易
- 不依赖 MCP 或特定框架
- 适用于任何能接收 HTTP 的 agent

**缺点**

- 需要外部 agent 有 HTTP 接收能力
- 同步 callback 可能不适合长任务

**适用场景**

- 第一版推荐
- Codex、自研 agent、任何 HTTP-capable agent

### 3.2 方案 B: MCP bridge adapter

**描述**

- 外部 agent（Claude Code）启动时连接 MCP server 或 bridge
- Bridge 向 Router 注册 attached session
- App 消息通过 Router → bridge → MCP tool/input → Claude Code
- Claude Code 输出通过 bridge → Router → App

**优点**

- Claude Code 原生支持 MCP
- 可利用 MCP tool 语义
- 适合 Claude Code 专用场景

**缺点**

- 需要 MCP bridge 实现
- 协议复杂度更高
- 当前未实现

**适用场景**

- Claude Code 专用后续方案
- 本阶段不实现

### 3.3 方案 C: CLI/stdin adapter

**描述**

- Router 或 adapter 子进程通过 stdin/stdout 与 agent 会话通信
- 类似传统 CLI wrapper

**优点**

- 不需要 HTTP server
- 适用于纯 CLI agent

**缺点**

- 进程控制复杂（启动、监控、重启）
- stdin/stdout 协议需要定义
- 不适合本阶段

**适用场景**

- 本阶段不推荐

## 4. 推荐方案

### 4.1 第一版: HTTP callback adapter

- 选择方案 A（HTTP callback adapter）作为第一版
- 原因：
  - 已有 Echo Session 实现，可直接复用
  - 协议简单，易于调试
  - 不依赖 MCP 或进程管理

### 4.2 MCP 作为后续方案

- 方案 B（MCP bridge）作为 Claude Code 专用后续方案
- 不在第一版实现

### 4.3 Router 不管理进程

- Router **不直接管理** Claude Code/Codex 进程
- Router 只负责：
  - 注册/心跳/offline 端点
  - 消息路由和 callback
  - `app_messages` 存储

### 4.4 外部会话自管理

- 外部会话自己负责：
  - 启动 agent 进程
  - 启动 adapter HTTP server
  - 向 Router 注册
  - 定期 heartbeat
  - 结束时调用 offline

## 5. Claude Code 接入草案

### 5.1 启动流程

用户启动 Claude Code 时带一个 bridge/adapter：

```bash
# 假想的启动命令（实际由 adapter 实现）
claude-code-adapter \
  --workspace /path/to/project \
  --router http://127.0.0.1:8899 \
  --port 9011
```

### 5.2 注册 payload

Adapter 向 Router 注册：

```json
{
  "sessionId": "claude-code-a1b2c3",
  "remoteAgentId": "claude-code-a1b2c3",
  "displayName": "Claude Code - goal",
  "description": "Working in D:\\projects\\goal",
  "callbackUrl": "http://127.0.0.1:9011",
  "sourceLabel": "claude-code"
}
```

### 5.3 App 交互

- App 拉取 `GET /api/contact-invitations`，看到 "Claude Code - goal"
- 用户接受后可发送消息
- Adapter 收到 callback，将消息送入当前 Claude Code 会话
- Claude Code 回复/完成任务后，adapter 转成 `agentReplies`

### 5.4 Adapter 转换逻辑

Adapter 负责：
- 将 App 消息转换为 Claude Code 可接受的格式
- 收集 Claude Code 输出，转换为 `agentReplies`
- 过滤 tool_use、thinking 等（按 callback v1 contract）

## 6. Codex 接入草案

### 6.1 与 Claude Code 相同模式

- Codex 作为同类 external attached session
- 使用 HTTP callback adapter
- 注册 payload 类似：

```json
{
  "sessionId": "codex-x1y2z3",
  "remoteAgentId": "codex-x1y2z3",
  "displayName": "Codex - goal",
  "description": "Working in D:\\projects\\goal",
  "callbackUrl": "http://127.0.0.1:9012",
  "sourceLabel": "codex"
}
```

### 6.2 不假设 Codex API 细节

- Codex API 细节由 adapter 实现
- Router 不依赖特定 agent API

### 6.3 不依赖 Router 启动

- Router 不启动 Codex
- 用户自己启动 Codex + adapter

## 7. 同步 vs 异步

### 7.1 当前 callback v1 是同步

- Router POST 到 callbackUrl，等待响应
- 超时约 55s（与内置 agent 相同）
- 适合短任务、即时回复

### 7.2 长任务需要异步 push

- 长任务 agent（如代码重构、长时间思考）可能需要异步推送后续汇报
- 当前 v1 不支持

### 7.3 v2 方向设计

新增端点用于 session 主动推送：

```
POST /api/app-agent-sessions/{sessionId}/messages
或
POST /api/app-agent-replies
```

- Session 在 callback 返回初始回复后，可后续推送更多 reply
- Router 收到后写入 `app_messages`，App 通过 `GET /api/messages` 拉取

### 7.4 本阶段不实现

- v2 异步 push 不在第一版实现
- 先验证同步 callback 链路

## 8. 安全边界

### 8.1 callbackUrl localhost 校验（已实现，必须保持）

- `callbackUrl` 只允许 `localhost` / `127.0.0.1` / `::1`
- 避免 SSRF（Server-Side Request Forgery）
- **已实现**：`validate_attached_session_callback_url` 在 `POST /api/app-agent-sessions/register` 阶段校验，失败返回 400
- 只允许 `http` scheme（本地 dev callback 不需要 https）
- 拒绝 `0.0.0.0`、`192.168.*`、`10.*`、`172.16-31.*`、公网 hostname、`file:` / `javascript:`、带 credentials 的 URL、无 hostname 的 URL
- 校验在鉴权之后执行：未授权请求返回 401，不暴露 callbackUrl 校验结果
- **callback server 可以 listen `127.0.0.1`**：Router 和 adapter 在同一台 PC 上通信，callbackUrl 是 Router → adapter 方向，必须是 localhost
- **注意 Android emulator**：emulator 访问 Router 用 `10.0.2.2`（emulator 中 `10.0.2.2` 指向宿主机），但 callbackUrl 仍是 Router 访问 adapter，所以必须是 localhost

### 8.2 注册鉴权

- 注册端点复用 `CHAT_API_KEY`
- Bearer token 或 `x-api-key`
- 与其他 `/api/*` 端点一致

### 8.3 不打印 token

- Adapter 和 Router 都不打印 `CHAT_API_KEY`
- 日志中不暴露认证信息

### 8.4 手机不控制进程

- App 不能启动、停止、重启外部 agent
- App 只能发送消息
- 进程控制权限属于 PC 端用户

### 8.5 工作目录/命令执行权限

- 工作目录、命令执行权限属于外部 agent 自己
- Router 不负责：
  - 指定工作目录
  - 执行命令
  - 文件系统访问
- Router 只负责消息路由

## 9. 失败和状态

### 9.1 Heartbeat

```
POST /api/app-agent-sessions/{sessionId}/heartbeat
```

- 更新 `lastSeenAt`
- 设置 `status` = `online`
- Adapter 应定期调用（如每 30s）

### 9.2 Offline

```
POST /api/app-agent-sessions/{sessionId}/offline
```

- 设置 `status` = `offline`
- Adapter 结束时调用

### 9.3 Callback timeout

- Router callback 超时约 55s
- 超时后 Router 返回 504 给 App
- App 显示发送失败

### 9.4 Callback 502/504

- Adapter 返回 502/504 时 Router 同样返回错误给 App
- Adapter 应处理内部错误并返回合适的 HTTP 状态

### 9.5 Session crash 后联系人表现

- Session crash 后未调用 offline
- Router 通过 heartbeat 超时检测（未来实现）
- App 目前只能通过发送失败/刷新看到状态
- 后续可补在线状态显示

### 9.6 App 状态可见性

- 当前 App 只能通过：
  - 发送失败（callback 错误）
  - 刷新联系人列表
- 后续可补：
  - `status` 字段在 `contact-invitations` 中返回
  - App 显示在线/offline 状态

## 10. 明确不做

### 10.1 不做 Router 启动 Claude Code

- Router 不启动外部 agent 进程
- 启动权限属于用户

### 10.2 不做多进程管理器

- Router 不是进程管理器
- 不负责：
  - 进程启动
  - 进程监控
  - 进程重启
  - 进程崩溃恢复

### 10.3 不做真实 MCP 实现

- MCP bridge 不在第一版实现
- 仅作为后续方案设计

### 10.4 不做异步 push reply

- v2 异步推送不在第一版实现
- 先验证同步 callback

### 10.5 不做 App UI 改动

- App 不需要改代码
- App 使用现有 `/api/messages` 和 `/api/contact-invitations`

## 11. 下一步实现建议

### 11.1 callbackUrl localhost 校验（已完成）

- ✅ 已在 `POST /api/app-agent-sessions/register` 中校验 `callbackUrl`
- ✅ 只允许 `localhost` / `127.0.0.1` / `::1`（http scheme）
- ✅ 拒绝外部 URL、`0.0.0.0`、私有网段、非 http scheme、credentials，防止 SSRF
- ✅ 鉴权先于校验，未授权返回 401

### 11.2 Claude Code mock adapter

- 先做一个 Claude Code mock adapter（不接真实 Claude Code）
- 类似 Echo Session，但：
  - 模拟 Claude Code 风格的回复
  - 模拟长任务场景（返回 "working..." 后延迟推送）
- 用于验证完整链路和 UI

### 11.3 真实 Claude Code MCP/bridge 接入

- 最后研究 Claude Code MCP 接入
- 可能需要：
  - Claude Code 启动时加载 MCP server
  - MCP server 作为 bridge 连接 Router
  - 或 Claude Code 直接支持 HTTP callback

### 11.4 实现顺序

1. callbackUrl 校验（安全）
2. Mock adapter（验证）
3. 真实 Claude Code 接入（生产）

---

## 附录：相关文档

- [Attached Session Callback Protocol v1](app-attached-session-callback-v1.md)
- [App Attached Session E2E](app-attached-session-e2e.md)
- [Echo Server](../../tools/app_session_echo_server.py)