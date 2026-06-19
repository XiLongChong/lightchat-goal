# AsPocket Claude Code Channel Design（2026-06-20）

## 概述

本文档基于对 `@paean-ai/claude-code-wechat` channel server 源码的逆向分析，设计 AsPocket Claude Code channel server。

目标：让用户启动 Claude Code 时，通过 channel 机制连接 AsPocket App（经由 PC Router），在 Claude Code 中接收来自手机的消息并回复。

---

## 一、Channel 机制探源

### 1.1 参考实现：@paean-ai/claude-code-wechat

源码路径：
```
C:\Users\MR\AppData\Roaming\npm\node_modules\@paean-ai\claude-code-wechat\dist\channel.js
C:\Users\MR\AppData\Roaming\npm\node_modules\@paean-ai\claude-code-wechat\dist\cli.js
```

核心架构：

```
WeChat (iOS)
    │
    ▼ HTTP long-poll
ClawBot / ilink API
    │
    ▼
channel.js（MCP stdio server）
    │  notifications/claude/channel
    ▼
Claude Code
    │  wechat_reply tool（CallToolRequest）
    ▼
channel.js
    │  HTTP
    ▼
ilink API → ClawBot → WeChat
```

### 1.2 关键技术栈

| 技术 | 作用 |
|------|------|
| `@modelcontextprotocol/sdk` v1.27.1 | MCP server 框架 |
| `StdioServerTransport` | stdio 传输层（Claude Code ↔ channel） |
| `experimental: { "claude/channel": {} }` | 声明为 channel server |
| `notifications/claude/channel` | 推送入站消息给 Claude Code |
| `<channel>_reply` tool | Claude Code 调用发送回复 |
| `--dangerously-load-development-channels server:<name>` | 加载 channel |
| `claude mcp add --scope user <name> -- <cmd>` | 注册 MCP server |

### 1.3 `--dangerously-load-development-channels server:<name>`

- **语法**：`claude --dangerously-load-development-channels server:<name>`
- **参数**：variadic servers，`server:<name>` 格式
- **行为**：Claude Code 以 `<name>` 为 channel 名称，启动对应的 channel server MCP server，建立 stdio 连接
- **注意**：这是官方 Claude Code 支持的 channel 加载机制

### 1.4 MCP server 注册

```bash
claude mcp add --scope user wechat -- node /path/to/channel.js
```

- `claude mcp get <name>` — 检查是否注册
- `claude mcp remove <name> --scope user` — 取消注册
- channel server 二进制路径由 `claude mcp add` 注册后，Claude Code 可通过 `server:<name>` 找到并启动

### 1.5 完整的 channel 生命周期

```
安装 npm 包
    │
    ▼ npm install -g @paean-ai/claude-code-wechat
channel.js + cli.js 就位
    │
    ▼ claude-wechat setup
登录（获取 token）→ 保存到 ~/.claude/channels/wechat/account.json
    │
    ▼ claude mcp add --scope user wechat -- node <path>
MCP server 注册到 Claude Code
    │
    ▼ claude --dangerously-load-development-channels server:wechat
Claude Code 启动，启动 channel.js 子进程
    │
    ▼ StdioServerTransport
stdio 连接建立，channel.js ↔ Claude Code
    │
    ├─→ channel.js 推送 notifications/claude/channel
    │     Claude Code 接收消息，显示给用户
    │
    ├─→ Claude Code 调用 wechat_reply tool
    │     → CallToolRequest → channel.js
    │     → channel.js 发送 HTTP 到 ilink API
    │
    └─→ Claude Code 退出 → channel.js 子进程退出
```

---

## 二、AsPocket Channel Server 设计

### 2.1 架构对比

| 项目 | WeChat Channel | AsPocket Channel |
|------|---------------|------------------|
| 消息来源 | ilink API（WeChat ClawBot） | PC Router callback |
| 发送方式 | ilink API | Router 的 `POST /api/messages`（由 App 拉取） |
| 传输层 | stdio（Claude Code ↔ channel） | 同 |
| 协议 | MCP + `claude/channel` | 同 |
| 凭证存储 | `~/.claude/channels/wechat/account.json` | `~/.claude/channels/aspocket/account.json` |
| 登录方式 | QR code（ilink） | 预配置 token |
| 依赖 | Node.js, `@modelcontextprotocol/sdk` | 同 |

### 2.2 消息流

```
Android App（emulator 10.0.2.2）
    │  HTTP POST /api/messages remoteAgentId=aspocket
    ▼
PC Router
    │  HTTP GET http://127.0.0.1:<port>/api/messages
    │     (channel 作为 MCP server 也在 PC 上)
    ▼
AspocketChannelServer（channel.js）
    │  notifications/claude/channel
    ▼
Claude Code
    │  aspocket_reply tool
    ▼
AspocketChannelServer
    │  HTTP POST /api/messages remoteAgentId=aspocket
    ▼
PC Router
    │  App 拉取 GET /api/messages
    ▼
Android App
```

### 2.3 为什么不用 attached-session callback 路径？

attached-session 方案（已实现）：
- App → Router → `POST callbackUrl/api/app-session/messages` → External agent
- **方向**：Router 主动推送消息到外部 agent

channel 方案：
- App → Router → Channel Server（MCP）→ Claude Code
- **方向**：Claude Code 作为 MCP client，channel server 推送消息给它
- **关键区别**：Claude Code 是"主动方"，channel 把手机消息推入 Claude Code 的会话上下文

对于 Claude Code attached session，channel 方案更自然：
- Claude Code 保持完整对话上下文
- 手机消息作为 channel notification 进入同一个 Claude Code 会话
- Claude Code 调用 `aspocket_reply` tool 发送回复

### 2.4 channel.js MCP server 核心结构

```javascript
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { ListToolsRequestSchema, CallToolRequestSchema } from "@modelcontextprotocol/sdk/types.js";

const CHANNEL_NAME = "aspocket";
const CHANNEL_VERSION = "0.1.0";

// MCP server 声明 claude/channel 实验能力
const mcp = new Server(
  { name: CHANNEL_NAME, version: CHANNEL_VERSION },
  {
    capabilities: {
      experimental: { "claude/channel": {} },  // 关键！
      tools: {}
    },
    instructions: [
      `Messages from AsPocket App arrive as <channel source="aspocket" sender="..." sender_id="...">.`,
      "Reply using the aspocket_reply tool. You MUST pass the sender_id from the inbound tag.",
      "You are a coding assistant. Keep replies concise.",
    ].join("\n")
  }
);

// 暴露 reply tool
mcp.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [{
    name: "aspocket_reply",
    description: "Send a text reply back to the AsPocket App user",
    inputSchema: {
      type: "object",
      properties: {
        sender_id: { type: "string", description: "The sender_id from the inbound <channel> tag" },
        text: { type: "string", description: "The plain-text message to send" }
      },
      required: ["sender_id", "text"]
    }
  }]
}));

// reply tool 实现：调用 Router 发送消息
mcp.setRequestHandler(CallToolRequestSchema, async (req) => {
  if (req.params.name === "aspocket_reply") {
    const { sender_id, text } = req.params.arguments;
    // 调用 Router POST /api/messages
    await routerPostMessage(text, sender_id);
    return { content: [{ type: "text", text: "sent" }] };
  }
  throw new Error(`unknown tool: ${req.params.name}`);
});

// 启动：建立与 Claude Code 的 stdio 连接
await mcp.connect(new StdioServerTransport());

// 启动后：开始轮询 PC Router 的消息
await startPolling();
```

### 2.5 消息轮询逻辑

```javascript
// 从 PC Router 获取新消息
async function pollRouterMessages() {
  // GET http://127.0.0.1:8899/api/app-agent-sessions/aspocket/messages
  // 或 GET /api/messages?remoteAgentId=aspocket&since=<cursor>
  const resp = await fetch(`${ROUTER_BASE}/api/messages?remoteAgentId=aspocket`, {
    headers: { Authorization: `Bearer ${account.token}` }
  });
  const data = await resp.json();

  for (const msg of data.messages ?? []) {
    // 推送消息到 Claude Code
    await mcp.notification({
      method: "notifications/claude/channel",
      params: {
        content: msg.content,
        meta: {
          sender: msg.senderId ?? "app",
          sender_id: msg.senderId ?? "app"
        }
      }
    });
  }
}
```

### 2.6 credentials 存储

```json
// ~/.claude/channels/aspocket/account.json
{
  "token": "<CHAT_API_KEY>",
  "routerBaseUrl": "http://127.0.0.1:8899",
  "remoteAgentId": "aspocket",
  "savedAt": "2026-06-20T00:00:00Z"
}
```

---

## 三、CLI 工具设计（cli.js）

参考 `claude-wechat` CLI，提供类似接口：

| 命令 | 行为 |
|------|------|
| `claude-aspocket setup` | 配置 token，保存到 account.json，注册 MCP server |
| `claude-aspocket start` | `claude --dangerously-load-development-channels server:aspocket` |
| `claude-aspocket status` | 显示登录状态和 MCP 注册状态 |
| `claude-aspocket logout` | 删除 credentials 和 MCP 注册 |

### setup 命令

```javascript
async function setupCommand(opts) {
  const token = await promptForToken();  // 或从环境变量 / stdin 读取
  const account = {
    token,
    routerBaseUrl: "http://127.0.0.1:8899",
    remoteAgentId: "aspocket",
    savedAt: new Date().toISOString()
  };
  saveCredentials(account);

  // 注册 MCP server
  const serverPath = getServerBinPath();
  execFileSync("claude", [
    "mcp", "add", "--scope", "user", "aspocket", "--",
    "node", serverPath
  ], { stdio: "inherit" });
}
```

---

## 四、实现路径

### 阶段 1：最小可运行 channel server

1. 创建 `tools/aspocket_channel/` 目录（TypeScript 或 JavaScript）
2. 实现 `channel.js`：
   - MCP server with `claude/channel` experimental capability
   - `aspocket_reply` tool
   - `notifications/claude/channel` 推送（从 Router 拉取消息）
3. 实现 `cli.js`：
   - `setup` / `start` / `status` / `logout` 命令
4. 构建：`tsup` → `dist/channel.js`
5. 测试：手动运行 `claude --dangerously-load-development-channels server:aspocket`

### 阶段 2：Router 端适配

1. 新增 `GET /api/app-agent-sessions/{remoteAgentId}/messages` 端点（channel 拉取消息用）
2. 或利用现有的 `GET /api/messages?remoteAgentId=aspocket`
3. Channel server 使用 `GET /api/messages?remoteAgentId=aspocket&since=<cursor>` 轮询

### 阶段 3：reply 路径

1. Channel server 的 `aspocket_reply` tool 调用 `POST /api/messages`
2. Router 存储到 `app_messages`，App 拉取

### 阶段 4：集成 attached session 注册

- Channel 启动时，向 Router 注册 attached session
- `POST /api/app-agent-sessions/register` with `sourceLabel: "claude-code-channel"`
- 消息同时走 channel 和 Router storage（双写）

---

## 五、与 attached-session callback 的关系

| 维度 | Attached Session callback | Claude Code channel |
|------|--------------------------|---------------------|
| **协议** | HTTP POST/GET | MCP stdio |
| **消息方向** | Router → External agent | Channel → Claude Code |
| **会话管理** | External agent 维护 | Claude Code 维护 |
| **上下文保留** | External agent 决定 | Claude Code 原生 |
| **适用场景** | 外部 agent（Codex 等） | Claude Code 自身 |
| **App 可见性** | 同 | 同 |

Channel 和 attached session 是**互补**的：
- 外部 agent → attached session callback（HTTP）
- Claude Code → Claude Code channel（MCP）
- 两者都可以通过 Router 的 `/api/messages` 路由

---

## 六、关键问题与开放点

1. **Router message cursor**：Channel 轮询 Router 时，如何知道从哪条消息开始（避免重复推送）？
   - 方案：在 channel server 内存中维护 `lastMessageId`
   - 或让 Router 返回 `messages?since=<cursor>`

2. **Session 隔离**：多个 App 用户连接同一个 Claude Code channel，如何区分？
   - 方案：每个 App 用户对应不同的 `sender_id`，Claude Code 通过 tool 调用时携带

3. **Channel 退出时**：Claude Code 退出后，channel 的 Router 注册状态如何清理？
   - 方案：在 channel.js 的 `process.on('exit')` 中调用 `POST /api/app-agent-sessions/<id>/offline`

4. **权限模式**：`--dangerously-skip-permissions` vs `--permission-mode`？
   - Claude Code channel 在attached session 场景下可能需要跳过确认

5. **Credential 文件权限**：Node.js `fs.chmod(credentials_file, 0o600)` 在 Windows 上可能无效
   - WeChat channel 已忽略此错误，Windows 上无安全凭证文件保护

---

## 七、总结

### Channel 机制核心要素

1. **MCP server**：`experimental: { "claude/channel": {} }` 声明 channel 身份
2. **消息推送**：`notifications/claude/channel` 方法推入站消息
3. **回复工具**：`<channel>_reply` tool 供 Claude Code 调用发送回复
4. **stdio 传输**：`StdioServerTransport` 连接 Claude Code 和 channel
5. **生命周期绑定**：Claude Code 退出 → channel 退出
6. **凭证存储**：`~/.claude/channels/<name>/account.json`

### AsPocket Channel 优势

- Claude Code 保持完整对话上下文
- 手机消息自然融入 Claude Code 会话
- 不需要 `claude -p` per-message 的配额开销
- 支持多轮对话和工具调用

### 下一步

1. 实现 `tools/aspocket_channel/dist/channel.js`（TypeScript/JS MCP server）
2. 实现 `tools/aspocket_channel/dist/cli.js`（setup/start/status/logout）
3. Router 端新增 `GET /api/app-agent-sessions/<id>/messages` 轮询端点
4. 本地 E2E 测试：`claude --dangerously-load-development-channels server:aspocket` → App 发消息 → Claude Code 回复