# AsPocket Claude Channel 启动 Runbook（2026-06-20）

把 AsPocket Claude Code channel 的启动流程固化成脚本，避免每次手动记忆一串命令。
配套脚本：

- `scripts/start-aspocket-router.ps1` — 前台启动 PC Router。
- `scripts/start-aspocket-claude-channel.ps1` — 准备 channel 环境并前台启动 Claude Code。

两个脚本都从 `.env` 读取 `CHAT_API_KEY` 注入当前进程环境，**从不打印 token**，只输出 `token loaded: yes/no`。

## 架构链路

```
Phone → App → POST /api/messages (remoteAgentId=aspocket-claude-1)
  → Router 写 user app_message + pending inbox，立即返回 agentReplies=[]
  → Channel server 轮询 GET /api/app-agent-sessions/{sid}/pending-messages
  → Channel server 通过 notifications/claude/channel 推给 Claude Code
  → Claude Code 调用 aspocket_reply tool
  → Channel server POST /api/app-agent-sessions/{sid}/replies
  → Router 写 agent app_message
  → App 轮询/同步后显示回复气泡
```

详见已归档验收记录 `docs/done/20260620/20260620-aspocket-claude-channel-e2e.md`。

## 1. 首次安装（仅一次或路径变更时）

channel server 依赖 `@modelcontextprotocol/sdk`，需先装依赖并跑单测：

```powershell
cd D:\projects\goal\tools\aspocket_channel
npm install
npm test
```

注册 aspocket MCP server 到 Claude Code（user scope，写 `~/.claude.json`）：

```powershell
claude mcp add --scope user aspocket -- node D:\projects\goal\tools\aspocket_channel\channel.js
```

注册后可用 `claude mcp get aspocket` 检查。注意：未注入运行环境变量时健康检查可能显示 `✘ Failed to connect`，这是正常的——真实启动脚本会注入 `CHAT_API_KEY` / `ASPOCKET_*`。

## 2. 每次启动

打开**两个**真实交互终端（不要用无 TTY 后台方式）：

**终端 1 — 启动 Router：**

```powershell
cd D:\projects\goal
.\scripts\start-aspocket-router.ps1
```

脚本会校验 `.venv\Scripts\python.exe` 存在，设置 `HOST=0.0.0.0`、`PORT=8899`，前台运行 `main.py`。输出形如：

```
Router URL:           http://127.0.0.1:8899
Android emulator URL: http://10.0.2.2:8899
token loaded:         yes
```

可选覆盖端口：`.\scripts\start-aspocket-router.ps1 -Port 9000`

**终端 2 — 启动 Claude Code channel：**

```powershell
cd D:\projects\goal
.\scripts\start-aspocket-claude-channel.ps1
```

脚本会：

1. 从 `.env` 注入 `CHAT_API_KEY`（不打印）。
2. 设置 `ASPOCKET_ROUTER_URL`、`ASPOCKET_SESSION_ID=aspocket-claude-1`、
   `ASPOCKET_REMOTE_AGENT_ID=aspocket-claude-1`、`ASPOCKET_DISPLAY_NAME=Claude Code - AsPocket`、
   `ASPOCKET_WORKSPACE=<repo root>`。
3. 校验 `claude` CLI 可用（`claude --version`）。
4. 校验 aspocket MCP server 已注册（`claude mcp get aspocket`）：
   - **未注册** → 打印注册命令并退出，不自动改用户配置。
   - **已注册** → 前台启动 `claude --dangerously-load-development-channels server:aspocket`。

可选参数覆盖：

```powershell
.\scripts\start-aspocket-claude-channel.ps1 -SessionId aspocket-claude-2 -RouterUrl http://127.0.0.1:9000
```

支持的参数：`-RouterUrl`、`-SessionId`、`-RemoteAgentId`、`-DisplayName`、`-Workspace`。

启动后如果 Claude Code 请求 development channel / tool permission，需要用户在终端确认。

## 3. App 侧使用

- 后端地址：`http://10.0.2.2:8899`（Android 模拟器访问宿主 Loopback 的特殊地址）。
- 进入「联系人 → 新的联系人」，刷新联系人申请。
- 接受 `Claude Code - AsPocket`。
- 进入聊天页发送消息。Claude Code 收到 channel 消息后会调用 `aspocket_reply` 写回，App 轮询/同步后显示回复气泡。

## 4. 验证命令

Router 启动后，用 `CHAT_API_KEY` 做 Bearer / `x-api-key` 认证访问：

```powershell
# 读取后端 profile（确认 Router 在线、认证有效）
curl.exe -s http://127.0.0.1:8899/api/profile -H "x-api-key: <CHAT_API_KEY>"

# 列出外部 agent session，应看到 aspocket-claude-1 online
curl.exe -s http://127.0.0.1:8899/api/app-agent-sessions -H "x-api-key: <CHAT_API_KEY>"

# 列出联系人邀请，应看到 Claude Code - AsPocket (agentType=claude-code-channel)
curl.exe -s http://127.0.0.1:8899/api/contact-invitations -H "x-api-key: <CHAT_API_KEY>"

# 读取 channel 历史
curl.exe -s "http://127.0.0.1:8899/api/messages?remoteAgentId=aspocket-claude-1" -H "x-api-key: <CHAT_API_KEY>"
```

注意：上面命令里的 `<CHAT_API_KEY>` 需手动替换为 `.env` 中的值，脚本不会代填（脚本从不打印 token）。可在当前终端用 `$env:CHAT_API_KEY`（由脚本注入的会话）拼到 header。

## 5. 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| `chat_auth_missing` | 当前终端没有加载 token。确保通过脚本启动（脚本从 `.env` 注入），或手动 `$env:CHAT_API_KEY=...`。Router 和 Claude Code channel 子进程必须拿到同一个 token。 |
| `claude mcp get aspocket` failed / `No MCP server found` | 未注册。执行首次安装里的 `claude mcp add --scope user aspocket -- node ...channel.js`。若显示 `✘ Failed to connect` 但已注册，属正常——健康检查未注入运行 env，真实启动脚本会注入。 |
| 后台启动 Claude 失败 / 进入非交互模式 | Claude Code 必须在真实交互 TTY 启动。不要用无 TTY 后台、`Start-Process -NoNewWindow` 或重定向 stdin 的方式启动脚本。 |
| App 收不到回复 | 依次检查：Router 是否在线；`GET /api/app-agent-sessions` 是否有 aspocket-claude-1；`GET /api/messages?remoteAgentId=aspocket-claude-1` 的 pending 历史；Claude Code 终端是否真的调用了 `aspocket_reply` tool。 |
| Router 重启后 pending 消息丢失 | Router pending inbox 当前是内存态，重启会丢。App 可用相同 `clientMessageId` 重发（DB 层幂等）。 |
| 中文消息在 adb shell 不稳定 | E2E 自动化建议用 ASCII 触发词。人工交互无此问题。 |

## 6. 安全 / 卫生

- **不要提交 `pc-agent.err.log` / `pc-agent.out.log`** 等运行日志。
- 脚本从不打印 `CHAT_API_KEY`，也不自动修改 `~/.claude.json`——MCP server 注册需用户手动执行注册命令。
- `.env` 已在 `.gitignore` 中，不会被提交。

## 7. 脚本行为约定

- 两个脚本都通过脚本自身位置定位仓库根目录（`<root>/scripts/`），无需手动 `cd`（脚本内部会 `Set-Location`）。
- 均前台运行，便于 Ctrl+C 停止；不做后台化。
- `.env` 读取逻辑在两个脚本里各自重复一份（少量代码，避免过度抽象）。
- 失败时给出清晰错误并 `exit 1`，不静默继续。
