# AsPocket Claude Code Channel E2E 验收记录（2026-06-20）

## 背景

AsPocket App 已经具备联系人、`remoteAgentId` 路由、按联系人消息隔离和 PC invitation 拉取能力。PC Router 也已经支持两类外部会话：

- `attached_session` callback：Router 主动 HTTP callback 到外部 session。
- `claude_channel`：Router 写 pending inbox，Claude Code channel server 轮询并通过 `notifications/claude/channel` 注入真实 Claude Code 会话。

本次验收目标是确认真实 Claude Code channel 链路成立，而不只是 mock adapter 或 HTTP echo。

## 已完成链路

### 1. PC Router

相关提交：

- `c8a0f2b feat: add claude channel router inbox`
- `a127f39 chore: add aspocket claude channel server`

Router 已支持：

- `kind=claude_channel` session 注册。
- `GET /api/contact-invitations` 返回 `agentType=claude-code-channel`。
- `POST /api/messages` 对 channel target 写入 user message 和 pending inbox，立即返回 `agentReplies=[]`。
- `GET /api/app-agent-sessions/{sessionId}/pending-messages` 供 channel server 轮询。
- `POST /api/app-agent-sessions/{sessionId}/pending-messages/{deliveryId}/ack` 标记投递。
- `POST /api/app-agent-sessions/{sessionId}/replies` 写入 Claude Code 回复。
- `GET /api/messages?remoteAgentId=...` 读取 channel 历史。

### 2. AsPocket Claude Channel Server

位置：

```text
tools/aspocket_channel/
```

包含：

- `channel.js`：MCP stdio server。
- `helpers.js`：纯 helper。
- `test/helpers.test.js`：Node 单测。
- `README.md`：启动和验证说明。

核心机制：

- 声明 `experimental: { "claude/channel": {} }`。
- 注册到 Router：`kind=claude_channel`。
- 轮询 Router pending inbox。
- 通过 `notifications/claude/channel` 把手机消息推给 Claude Code。
- 暴露 `aspocket_reply` tool。
- Claude Code 调用 `aspocket_reply` 后，channel server 写回 Router reply endpoint。

### 3. 真实 Claude Code 验收

人工交互终端启动：

```powershell
cd D:\projects\goal
$env:CHAT_API_KEY='<same token as Router>'
$env:ASPOCKET_ROUTER_URL='http://127.0.0.1:8899'
$env:ASPOCKET_SESSION_ID='aspocket-claude-1'
$env:ASPOCKET_REMOTE_AGENT_ID='aspocket-claude-1'
$env:ASPOCKET_DISPLAY_NAME='Claude Code - AsPocket'
$env:ASPOCKET_WORKSPACE='D:\projects\goal'

claude --dangerously-load-development-channels server:aspocket
```

已验证：

- Router `GET /api/app-agent-sessions` 显示 `aspocket-claude-1` online。
- Router `GET /api/contact-invitations` 显示 `Claude Code - AsPocket`。
- HTTP 发消息到 `remoteAgentId=aspocket-claude-1` 后，Claude Code 终端收到 channel message。
- Claude Code 调用 `aspocket_reply` tool。
- Router 历史中出现 agent reply。

关键观察：

```text
aspocket: Please reply exactly: AsPocket channel received. Use aspocket_reply.
Thought for 6s, called aspocket
Allowed by PermissionRequest hook
```

这证明真实 Claude Code channel attach 成立。

### 4. Android App UI 验收

测试环境：

- App 提交：`19a372b feat: send remote agent id with app requests`
- PC 提交：`a127f39 chore: add aspocket claude channel server`
- Emulator：`pixel6_api34`
- App 后端地址：`http://10.0.2.2:8899`

已验证：

- App 进入“联系人 -> 新的联系人”，刷新后出现 `Claude Code - AsPocket`。
- 点击“同意”后，联系人列表出现 `Claude Code - AsPocket`。
- 进入聊天页后可发送消息。
- App 用户消息通过 Router 进入 Claude Code channel。
- Claude Code 通过 `aspocket_reply` 写回：

```text
app channel ok
```

- App 聊天页刷新/同步后显示回复气泡。
- `GET /api/messages?remoteAgentId=aspocket-claude-1` 包含本轮 user 和 agent 消息。
- `GET /api/messages?remoteAgentId=default` 不包含本轮消息，历史隔离成立。

结论：App UI -> PC Router -> AsPocket Claude Channel -> Claude Code -> `aspocket_reply` -> Router -> App 气泡链路通过。

## 启动流程

### 1. 启动 Router

```powershell
cd D:\projects\goal
$env:HOST='0.0.0.0'
$env:PORT='8899'
.\.venv\Scripts\python.exe main.py
```

### 2. 注册 channel server

只需首次或路径变化时执行：

```powershell
claude mcp add --scope user aspocket -- node D:\projects\goal\tools\aspocket_channel\channel.js
```

检查：

```powershell
claude mcp get aspocket
```

注意：如果未注入 `CHAT_API_KEY` / `ASPOCKET_*` 环境变量，健康检查可能显示 failed。真实启动 Claude Code 时会继承当前终端环境。

### 3. 启动 Claude Code

必须在真实交互 TTY 中启动，不能用无 TTY 后台方式启动：

```powershell
cd D:\projects\goal
$env:CHAT_API_KEY='<same token as Router>'
$env:ASPOCKET_ROUTER_URL='http://127.0.0.1:8899'
$env:ASPOCKET_SESSION_ID='aspocket-claude-1'
$env:ASPOCKET_REMOTE_AGENT_ID='aspocket-claude-1'
$env:ASPOCKET_DISPLAY_NAME='Claude Code - AsPocket'
$env:ASPOCKET_WORKSPACE='D:\projects\goal'

claude --dangerously-load-development-channels server:aspocket
```

如果 Claude Code 请求允许 development channel / tool permission，需要用户确认。

### 4. App 侧

- 后端地址：`http://10.0.2.2:8899`
- 刷新联系人申请。
- 接受 `Claude Code - AsPocket`。
- 进入聊天发送消息。

## 当前限制

### P0

无。真实 Claude Code channel 和 App UI E2E 已通过。

### P1

无。

### P2 / 后续优化

- `claude_channel` 是当前实现名，长期应泛化为 `channel`，避免 Router 为每个 agent 增加专用分支。
- `attached_session` 是产品概念，技术路由层应逐步收敛为 `callback`。
- App 对 channel 异步回复依赖手动刷新/重新同步；后续需要轮询、通知或后台同步。
- Router pending inbox 当前是内存态，Router 重启会丢 pending 消息。
- `claude mcp get aspocket` 在未注入运行环境变量时可能显示 failed，容易误导。
- Claude Code 必须用交互 TTY 启动；后台无 TTY 会进入 print/非交互模式，无法验证真实会话。
- Android adb shell 对中文输入不稳定，E2E 自动化使用 ASCII 触发词更可靠。

## 架构决策

### 当前先做通

用户已确认：先把真实 Claude Code channel 链路做出来，协议命名和边界收敛后续再做。

### 后续需要收敛

不要为每个 agent 在 Router 内新增一种专用协议。长期目标：

```text
internal
callback
channel
```

建议映射：

- Echo / Claude mock / HTTP-capable self-agent -> `callback`
- Claude Code experimental channel / future long-running pull clients -> `channel`
- PC Router 内置 agent -> `internal`

保留对现有 `claude_channel` 的兼容，但内部命名和文档逐步改为通用 `channel`。

## 下一步计划

### 1. 文档与启动体验

- 把真实 Claude Code channel 启动步骤补充到开发手册或 README。
- 明确必须用交互 TTY。
- 标注 `CHAT_API_KEY` 需要同时给 Router 和 Claude Code channel 子进程。

### 2. App 异步刷新体验

当前 channel reply 是异步写回 Router，App 需要刷新/同步后才能看到。下一阶段建议做：

- 聊天页定时轻量刷新当前联系人历史。
- 或进入聊天页时保持短周期 polling。
- 或后续接系统通知/后台同步。

默认建议：先做聊天页前台 polling，范围小、效果直接。

### 3. Router 协议收敛

完成产品链路后，再把 Router 的技术协议抽象收敛：

- `attached_session` callback 路径命名为 `callback`。
- `claude_channel` 泛化为 `channel`。
- 保持旧 kind 兼容。
- 更新测试和 docs。

### 4. Channel 稳定性

- heartbeat / offline 与 Claude Code 进程生命周期联动。
- pending inbox 持久化或可恢复策略。
- 更明确的 delivery retry / ack 语义。
- `aspocket_reply` 多回复、空回复、错误回复的 UX 策略。

## 需求决策待确认

以下不是阻塞项，但进入产品化前需要用户拍板：

- App 对异步回复采用前台 polling、手动刷新、系统通知，还是三者组合？
- Claude Code 会话结束后，App 联系人是保留为离线联系人，还是作为临时联系人自动隐藏？
- 用户是否需要在 App 中看到 `Claude Code - workspace` 的在线/离线状态？
- 是否允许手机向 offline Claude channel 发送消息并排队，还是直接失败？
- Channel pending inbox 是否需要持久化，还是开发期内存态可接受？

