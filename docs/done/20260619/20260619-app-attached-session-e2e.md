# App Attached Session E2E 验证（2026-06-19）

## 概述

使用 dev-only 的 Echo Session Server 端到端验证手机 App 接入外部 agent 会话的完整链路：
App → PC Router → attached session callback → Router 落库 → App 拉取消息。

不接 Claude Code，不接真实外部 agent。Echo server 只做本地回显。

## 架构

```
Android App (emulator)
    │  HTTP  http://10.0.2.2:8899
    ▼
PC Router (main.py, 127.0.0.1:8899)
    │  HTTP  http://127.0.0.1:9010/api/app-session/messages
    ▼
Echo Session Server (tools/app_session_echo_server.py, 127.0.0.1:9010)
```

- App 运行在 Android emulator，通过 `http://10.0.2.2:8899` 访问宿主机上的 PC Router。
- Echo server 运行在 PC 上，Router 通过 `http://127.0.0.1:9010` 访问它。
- `callbackUrl` 是 Router → echo server 方向，写 `http://127.0.0.1:9010` 是对的。
- App 不需要改代码。

## 前置条件

- `CHAT_API_KEY` 环境变量已设置（Router 和 echo server 共用）。
- Python 依赖已安装（`pip install -r requirements.txt`）。

## 步骤 1：启动 PC Router

```powershell
# 设置认证 key（与 App 配置一致）
$env:CHAT_API_KEY = "your-secret-key"

# 启动 Router（默认监听 0.0.0.0:8899）
python main.py
```

确认日志出现 `Starting server on 0.0.0.0:8899`。

## 步骤 2：启动 Echo Session Server

另开一个终端：

```powershell
# 同一个 key
$env:CHAT_API_KEY = "your-secret-key"

# 启动 echo server，自动向 Router 注册
python tools/app_session_echo_server.py `
    --router http://127.0.0.1:8899 `
    --host 127.0.0.1 `
    --port 9010 `
    --session-id echo-session-1 `
    --remote-agent-id echo-session-1 `
    --display-name "Echo Session"
```

启动成功会看到：

```
[echo] starting echo server on 127.0.0.1:9010 (router=http://127.0.0.1:8899 sessionId=echo-session-1)
[echo] registered sessionId=echo-session-1 remoteAgentId=echo-session-1 -> Router http://127.0.0.1:8899
```

如果注册失败会输出清楚错误并退出，例如 Router 未启动：

```
[echo] registration failed: Cannot connect to PC Router at http://127.0.0.1:8899/api/app-agent-sessions/register. Is the Router running? (...)
```

## 步骤 3：验证注册结果

### 查看已注册的 sessions

```powershell
curl.exe -H "Authorization: Bearer $env:CHAT_API_KEY" `
  http://127.0.0.1:8899/api/app-agent-sessions
```

应返回包含 `echo-session-1` 的列表。

### 查看 contact invitations（App 拉取联系人列表用）

```powershell
curl.exe -H "Authorization: Bearer $env:CHAT_API_KEY" `
  http://127.0.0.1:8899/api/contact-invitations
```

## 步骤 4：发送消息验证 callback 链路

### POST /api/messages（模拟 App 发消息）

```powershell
$body = @{
    clientMessageId = "cm-test-001"
    content = "你好"
    remoteAgentId = "echo-session-1"
} | ConvertTo-Json

curl.exe -X POST `
  -H "Authorization: Bearer $env:CHAT_API_KEY" `
  -H "Content-Type: application/json" `
  -d $body `
  http://127.0.0.1:8899/api/messages
```

预期响应（Router 生成 user + agent reply，remoteId 为 `app_rm_*`）：

```json
{
  "userMessage": {
    "remoteId": "app_rm_user_...",
    "clientMessageId": "cm-test-001",
    "status": "sent",
    "createdAt": "2026-06-19T...Z"
  },
  "agentReplies": [
    {
      "remoteId": "app_rm_agent_...",
      "content": "Echo from attached session: 你好",
      "createdAt": "2026-06-19T...Z",
      "sourceToolCallId": "echo-cm-test-001"
    }
  ]
}
```

注意：
- 返回给 App 的 `remoteId` 是 Router 生成的本地 id（`app_rm_agent_*`），不是 echo server 的 `echo-cm-test-001`。
- echo server 的 session remoteId 保存在 `sourceToolCallId` 字段，用于调试追踪。

### GET /api/messages（拉取历史）

```powershell
curl.exe -H "Authorization: Bearer $env:CHAT_API_KEY" `
  "http://127.0.0.1:8899/api/messages?remoteAgentId=echo-session-1"
```

应返回刚才的 user 消息和 agent reply。

## 步骤 5：Android emulator 接入

1. App 配置 Router 地址为 `http://10.0.2.2:8899`（emulator 中 `10.0.2.2` 指向宿主机）。
2. App 使用与 Router 相同的 `CHAT_API_KEY`。
3. App 拉取 contact invitations，应看到 "Echo Session"。
4. 选择 "Echo Session" 发送消息，应收到 "Echo from attached session: ..." 回复。

App 不需要改代码。

## 步骤 6：退出 Echo Server

按 `Ctrl+C` 退出。Echo server 会 best-effort 调用 Router 的 offline endpoint：

```
[echo] offline sent for sessionId=echo-session-1
```

如果 offline 调用失败（如 Router 已关闭），会输出非致命错误但不阻塞退出。

## 参数说明

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--router` | `http://127.0.0.1:8899` | PC Router base URL |
| `--host` | `127.0.0.1` | echo server 监听地址 |
| `--port` | `9010` | echo server 监听端口 |
| `--session-id` | `echo-session-1` | 注册的 sessionId |
| `--remote-agent-id` | `echo-session-1` | 注册的 remoteAgentId |
| `--display-name` | `Echo Session` | App 中显示的名称 |
| `--description` | `Dev attached session for App callback testing` | 描述 |

## 安全

- `CHAT_API_KEY` 从环境变量读取，**不会打印**。
- 启动失败/注册失败输出清楚错误信息。
- Echo server 仅用于 dev，不要部署到生产环境。

## 测试

Echo server 的纯函数 helper 有轻量单测（不启动真实 server，不占用端口）：

```powershell
python -m pytest tests/test_app_session_echo.py -v
```

不把真实 server 集成进 pytest，避免端口占用导致 flaky。
