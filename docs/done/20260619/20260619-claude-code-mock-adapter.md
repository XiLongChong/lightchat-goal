# Claude Code Mock Adapter（2026-06-19）

## 概述

Dev-only Claude Code mock attached-session adapter，用于验证外部代码会话（Claude Code 风格）的产品体验。不接真实 Claude Code，不启动外部进程，不执行命令，不改文件。

## 架构

```
Android App (emulator)
    │  HTTP  http://10.0.2.2:8899
    ▼
PC Router (main.py, 127.0.0.1:8899)
    │  HTTP  http://127.0.0.1:9011/api/app-session/messages
    ▼
Claude Code Mock Adapter (127.0.0.1:9011)
```

- App 运行在 Android emulator，通过 `http://10.0.2.2:8899` 访问宿主机上的 PC Router
- Mock adapter 运行在 PC 上，Router 通过 `http://127.0.0.1:9011` 回调
- App 不需要改代码

## 前置条件

- `CHAT_API_KEY` 环境变量已设置（Router 和 mock adapter 共用）
- PC Router 已启动

## 步骤 1：启动 PC Router

```powershell
$env:CHAT_API_KEY = "your-secret-key"
python main.py
```

## 步骤 2：启动 Claude Code Mock Adapter

另开一个终端：

```powershell
$env:CHAT_API_KEY = "your-secret-key"

python tools/claude_code_mock_adapter.py `
    --router http://127.0.0.1:8899 `
    --host 127.0.0.1 `
    --port 9011 `
    --session-id claude-code-mock-1 `
    --remote-agent-id claude-code-mock-1 `
    --workspace D:\projects\goal
```

启动成功会看到：

```
[claude-mock] starting mock adapter on 127.0.0.1:9011 (router=http://127.0.0.1:8899 sessionId=claude-code-mock-1 workspace=D:\projects\goal)
[claude-mock] registered sessionId=claude-code-mock-1 remoteAgentId=claude-code-mock-1 -> Router http://127.0.0.1:8899
```

注册参数：
- `sessionId`: `claude-code-mock-1`
- `remoteAgentId`: `claude-code-mock-1`
- `displayName`: `Claude Code Mock - goal`（从 `--workspace` 推导）
- `description`: `Mock coding session in D:\projects\goal`
- `callbackUrl`: `http://127.0.0.1:9011`
- `sourceLabel`: `claude-code-mock`

## 步骤 3：验证注册

### 查看已注册 sessions

```powershell
curl.exe -H "Authorization: Bearer $env:CHAT_API_KEY" `
  http://127.0.0.1:8899/api/app-agent-sessions
```

应返回包含 `claude-code-mock-1` 的列表，含 `sourceLabel: "claude-code-mock"`。

### 查看 contact invitations

```powershell
curl.exe -H "Authorization: Bearer $env:CHAT_API_KEY" `
  http://127.0.0.1:8899/api/contact-invitations
```

应看到 "Claude Code Mock - goal"。

## 步骤 4：发送消息验证回复逻辑

Mock adapter 根据关键词返回不同风格的回复：

### 询问状态

```powershell
$body = @{
    clientMessageId = "cm-status"
    content = "show status"
    remoteAgentId = "claude-code-mock-1"
} | ConvertTo-Json

curl.exe -X POST `
  -H "Authorization: Bearer $env:CHAT_API_KEY" `
  -H "Content-Type: application/json" `
  -d $body `
  http://127.0.0.1:8899/api/messages
```

回复：mock status 报告，包含 workspace 名称。

### 测试相关

```powershell
$body = @{
    clientMessageId = "cm-test"
    content = "帮我运行测试"
    remoteAgentId = "claude-code-mock-1"
} | ConvertTo-Json
```

回复：测试意图说明，明确标注是 mock adapter，没有真实执行命令。

### 修复相关

```powershell
$body = @{
    clientMessageId = "cm-fix"
    content = "fix the bug"
    remoteAgentId = "claude-code-mock-1"
} | ConvertTo-Json
```

回复：修复意图说明，明确标注是 mock adapter，没有修改文件。

### 默认消息

```powershell
$body = @{
    clientMessageId = "cm-default"
    content = "hello"
    remoteAgentId = "claude-code-mock-1"
} | ConvertTo-Json
```

回复：`Claude Code Mock received: hello`

### 拉取消息历史

```powershell
curl.exe -H "Authorization: Bearer $env:CHAT_API_KEY" `
  "http://127.0.0.1:8899/api/messages?remoteAgentId=claude-code-mock-1"
```

## 回复逻辑

| 关键词 | 回复风格 |
|--------|---------|
| `status` / `状态` / `进度` | Mock status 报告（含 workspace 名称） |
| `test` / `测试` | 测试意图说明（标注 mock，不执行） |
| `fix` / `修` / `bug` | 修复意图说明（标注 mock，不改文件） |
| 默认 | `Claude Code Mock received: <content>` |

`remoteId` 格式：`claude-mock-<clientMessageId>`

## 步骤 5：Android emulator 接入

1. App 配置 Router 地址为 `http://10.0.2.2:8899`
2. App 拉取 contact invitations，应看到 "Claude Code Mock - goal"
3. 选择该联系人发送消息，收到对应风格的 mock 回复

## 步骤 6：退出

按 `Ctrl+C` 退出。Mock adapter 会 best-effort 调用 offline：

```
[claude-mock] offline sent for sessionId=claude-code-mock-1
```

## 参数说明

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--router` | `http://127.0.0.1:8899` | PC Router base URL |
| `--host` | `127.0.0.1` | 监听地址 |
| `--port` | `9011` | 监听端口 |
| `--session-id` | `claude-code-mock-1` | 注册的 sessionId |
| `--remote-agent-id` | `claude-code-mock-1` | 注册的 remoteAgentId |
| `--workspace` | 当前目录 | 工作目录路径（用于 displayName/description） |
| `--display-name` | 从 workspace 推导 | 覆盖 displayName |

## 明确说明

- **这是 mock**，不接真实 Claude Code
- **不执行命令**，不修改文件
- **不启动外部进程**
- **不连接 Anthropic API**
- 仅用于验证 Router 的 attached session callback 链路和产品体验

## 测试

```powershell
python -m pytest tests/test_claude_code_mock_adapter.py -v
```