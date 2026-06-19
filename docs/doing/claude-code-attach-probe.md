# Claude Code Attach Probe（2026-06-19）

## Environment

| Item | Value |
|------|-------|
| Host | Windows, PowerShell 5.1 |
| Working dir | `D:\projects\goal` |
| Claude Code | `2.1.170` (Claude Code) |
| API quota | Exhausted 5h window; reset at 2026-06-20 02:37 CST |
| Existing MCP | `tavily` (HTTP), `wechat` (stdio node) |

## Commands run

All commands were safe — no file modifications, no long-running processes.

### Version / help (no side effects)

| Command | Exit | Key output |
|---------|------|------------|
| `claude --version` | 0 | `2.1.170 (Claude Code)` |
| `claude --help` | 0 | Full option list (see below) |
| `claude mcp --help` | 0 | Subcommands: `add`, `add-json`, `add-from-claude-desktop`, `get`, `list`, `remove`, `serve`, `reset-project-choices` |
| `claude mcp serve --help` | 0 | `claude mcp serve -d --debug` — starts Claude Code as an MCP server |
| `claude project --help` | 0 | Subcommands: `purge` — delete all project state |
| `claude agents --help` | 0 | `--json` to list active sessions as JSON; `--cwd` to filter |
| `claude mcp list` | 0 | Shows 2 configured servers: `tavily` (HTTP, connected), `wechat` (stdio node, connected) |

### Non-interactive print (`-p`) with JSON output

```json
{
  "type": "result",
  "subtype": "success",
  "is_error": true,
  "api_error_status": 429,
  "duration_ms": 171811,
  "num_turns": 1,
  "result": "API Error: Request rejected (429) ...",
  "stop_reason": "stop_sequence",
  "session_id": "a9ceae3c-e0ac-4bde-94b7-103f0f990abc",
  "total_cost_usd": 0,
  "usage": { ... },
  "uuid": "43afccd0-859a-492e-85d7-35d43811bc01"
}
```

### Non-interactive print (`-p`) with text output

```
API Error: Request rejected (429) · You have exceeded the 5-hour usage quota...
```

## Observations

### 1. Output format options

Only `text` and `json` were tested (stream-json not tested). Key schemas:

| Format | Schema | Pros | Cons |
|--------|--------|------|------|
| `text` | Raw stdout string | Simplest parse | No structured metadata; ANSI/control chars possible |
| `json` | `{"type":"result","subtype":"success","result":"...","session_id":"..."}` | Structured; has session_id, usage, cost | Slightly heavier; need `jq` or JSON parser |
| `stream-json` | Realtime chunks | Can stream partial text | More complex to consume |

### 2. Non-interactive mode (-p/--print)

**Confirmed working**. Flags observed on v2.1.170:

| Flag | Purpose | Relevance |
|------|---------|-----------|
| `-p "prompt"` | Send prompt, get response, exit | **Core** — synchronous request-response |
| `--output-format json` | Structured JSON output | **Core** — parse `result` field |
| `--input-format stream-json` | Realtime streaming input from stdin | Medium |
| `--session-id <uuid>` | Specify session ID | **Core** — preserve conversation continuity |
| `-c / --continue` | Continue most recent session in cwd | Alternative |
| `-r / --resume <id>` | Resume by session ID | Alternative |
| `--no-session-persistence` | Don't save to disk | Useful for pure-bridge |
| `--fallback-model` | Fallback on API overload | Resiliency |
| `--max-budget-usd` | Cap spend | Safety |
| `--append-system-prompt` | Inject extra system prompt | **Core** — for shaping agent behaviour |
| `--system-prompt` | Override system prompt | Advanced |
| `--permission-mode` | `bypassPermissions`, `auto`, `plan`, etc | For automation |
| `--bare` | Minimal mode (no hooks/LSP/plugins) | For fast startup |
| `--model` | Choose model | — |
| `--agent` | Choose agent config | — |

### 3. MCP integration

Three distinct MCP pathways available:

#### 3a. `claude mcp serve` — Claude Code AS an MCP server

- `claude mcp serve` starts Claude Code itself as an MCP server (stdio-based)
- Other processes (Router adapter) could connect to it as an MCP client
- No extra flags needed; `--debug` for verbose
- **This is the most promising bridge approach** — see Recommendation

#### 3b. `--mcp-config` — Load external MCP servers INTO Claude Code

- Claude Code can load any stdio/HTTP MCP server at startup
- `--mcp-config` takes JSON files or inline strings
- `claude mcp add` adds permanent config entries
- Router adapter could register as an MCP tool that Claude Code calls when it needs to send a reply

#### 3c. Existing MCP configuration

```
tavily: https://mcp.tavily.com/mcp/ (HTTP) - Connected
wechat: cmd /c node ...\claude-code-wechat\dist\channel.js (stdio) - Connected
```

The project already uses MCP — the `wechat` server bridges Claude Code ↔ WeChat.

### 4. Input modes

| Mode | Syntax | Suitable for |
|------|--------|-------------|
| Direct prompt | `claude -p "..."` | Simplest synchronous |
| stdin pipe | `echo "..." \| claude -p` | Scriptable |
| stream-json stdin | `--input-format stream-json` | Realtime multi-turn |
| Interactive session | `claude` (no flags) | Manual use, not for adapter |

### 5. Session continuity

- `--session-id` allows explicit session ID assignment
- `-c` continues last session in cwd
- `-r <id>` resumes a specific session by ID
- `--no-session-persistence` prevents disk writes
- Sessions persist by default in `~/.claude/sessions/`

### 6. Token cost / quota

- 5-hour rolling quota from Anthropic
- `--max-budget-usd` can cap cost
- `--fallback-model` handles overload
- **Important**: Each `-p` call counts against quota, even short prompts

### 7. Windows / PTY considerations

- Claude Code appears to work natively on Windows (confirmed by `--version`)
- Stdio-based MCP servers work (the `wechat` server uses `cmd /c node ...`)
- No ANSI escape issues observed with `--output-format json`
- `--output-format text` may have control chars in normal interactive mode but `-p` seems clean

## Candidate approaches

| # | Approach | Description | Pros | Cons | Feasibility |
|---|----------|-------------|------|------|-------------|
| **A** | `claude -p` per-message | Adapter calls `claude -p "msg" --output-format json` for each incoming App message | Simplest; no long-lived process; clean JSON output; session-id preserves continuity | Each call = fresh LLM call + full context reload; quota cost per message; 429 risk without fallback; no streaming feedback | ⭐⭐⭐ Feasible but costly |
| **B** | Long-lived subprocess | Adapter spawns `claude` interactive session, pipes stdin, reads stdout | Single session; context preserved in memory; lower latency per turn | Stdout parsing fragile; Windows PTY issues; need heartbeats; process crashes need recovery | ⭐⭐ Risky on Windows |
| **C** | MCP bridge (adapter as MCP client to claude mcp serve) | Adapter connects to `claude mcp serve` via stdio as MCP client; sends tools/resources, reads results | Clean protocol; no fragile stdout parsing; bidirectional | Higher complexity; need MCP client library; `claude mcp serve` exposes limited toolset | ⭐⭐⭐⭐ Best long-term |
| **D** | MCP bridge (adapter as MCP server loaded by claude) | Adapter runs as MCP server; Claude Code loads it via `--mcp-config`; App message arrives as MCP tool call | Native MCP loading; Claude Code controls flow | Adapter can't push messages to Claude Code; only responds when Claude Code calls the tool; reverse direction tricky | ⭐⭐ Partial |
| **E** | HTTP callback / external (current mock) | Already working Echo + Claude Code mock; Claude Code not involved | Zero risk; works today | Not real Claude Code; no actual coding capability | ⭐⭐⭐ Done |

## Recommendation

### Recommended: Approach C — MCP bridge (adapter as MCP client to `claude mcp serve`)

**Rationale**:

1. `claude mcp serve` exists and is documented — Claude Code can be an MCP server
2. The project already uses MCP (the `wechat` bridge is an MCP server), so the pattern is proven
3. MCP provides a clean, structured protocol — no fragile stdout parsing
4. The adapter can send messages as MCP tools/resources and receive structured results
5. Bidirectional communication is possible through the MCP protocol

**Architecture sketch**:

```
App → Router HTTP → MCP Bridge Adapter → stdio MCP → `claude mcp serve`
                                                ← JSON reply ←
                                         → Agent reply JSON → Router → App
```

**Prerequisites**:
- MCP client library in Python (there are several, e.g. `mcp` PyPI package)
- Understanding of what tools `claude mcp serve` exposes (needs probe with real API quota)

### Fallback: Approach A — `claude -p` per message

If MCP bridge proves too complex or `claude mcp serve` doesn't expose the needed capabilities, fall back to `claude -p --output-format json`. This is simpler but costs more (each message is a separate API call).

### Keep Approach E (mock) for development

The existing mock adapter (Echo + Claude Code Mock) should remain the primary development and E2E testing tool. Real Claude Code integration is a separate deployment path.

## Open questions

1. **What tools does `claude mcp serve` actually expose?** Need a working API key to probe `claude mcp serve` and list its tools/resources.
2. **Session continuity via MCP**: Does `claude mcp serve` maintain a single session or create a new one per connection?
3. **MCP tool for sending a reply**: Can we define a custom tool like `respond_to_phone(content: str)` that Claude Code calls when it has a reply for the phone?
4. **Concurrent sessions**: Can multiple Router attached sessions map to multiple `claude mcp serve` instances?
5. **Windows stdio MCP stability**: Does `claude mcp serve` on Windows handle long-lived stdio connections reliably?
6. **Permission model**: Will `claude mcp serve` prompt for permissions (file access, command execution) or can it run in bypass mode?
7. **Cost control**: How to prevent Claude Code from spending unlimited quota during attached sessions?
8. **Offline detection**: If `claude mcp serve` exits, how does the adapter detect it and call Router offline?

## Next implementation step

### Phase 1: MCP bridge adapter (probe with quota)

1. **After quota reset** (2026-06-20 02:37 CST), run `claude mcp serve` and observe:
   - What stdio protocol (JSON-RPC?) does it speak?
   - What tools/resources are listed?
   - Can an external MCP client send a `messages/create` or similar?
   - Does it handle multiple turns?
2. **Choose Python MCP client library**:
   - Check `mcp` PyPI package for client capabilities
   - Or implement minimal JSON-RPC over stdio
3. **Build a thin bridge adapter**:
   - Adapter starts `claude mcp serve` as subprocess
   - Adapter connects as MCP client
   - Adapter registers with Router (existing `/api/app-agent-sessions/register`)
   - Adapter relays App messages as MCP tools/resources
   - Adapter collects responses and relays back to Router
4. **Test with `claude_code_mock_adapter.py` patterns**:
   - Keyword-based reply verification
   - Session continuity
   - Offline handling

### Phase 2: Production hardening (if phase 1 viable)

- Permission mode (`--dangerously-skip-permissions` or `--permission-mode auto` depending on safety)
- Heartbeat between adapter and `claude mcp serve`
- Multi-session isolation (one `claude mcp serve` per attached session?)
- Budget caps via `--max-budget-usd`
- CallbackUrl localhost validation (already done)

## Appendix: Full `claude --help` key options

All options from `claude --help` (abridged to integration-relevant):

| Category | Key options |
|----------|------------|
| Non-interactive | `-p/--print`, `--output-format text\|json\|stream-json` |
| Input | `--input-format text\|stream-json` |
| Session | `--session-id <uuid>`, `-c/--continue`, `-r/--resume` |
| Persistence | `--no-session-persistence` |
| MCP | `--mcp-config`, `--strict-mcp-config`, `mcp serve` |
| Prompt | `--system-prompt`, `--append-system-prompt` |
| Safety | `--dangerously-skip-permissions`, `--permission-mode` |
| Budget | `--max-budget-usd`, `--fallback-model` |
| Model | `--model`, `--agent` |
| Minimal | `--bare` (skip hooks/LSP/plugins) |

---

## MCP Serve Capability Probe（2026-06-19 新增）

### Probe script

`tools/claude_mcp_probe.py` — 同步 stdio JSON-RPC MCP 客户端，Windows 兼容。

启动 `claude mcp serve` 子进程，发送 MCP handshake + `tools/list` / `resources/list` / `prompts/list`，收集结果后退出。关键设计：过滤非 JSON 行（启动 banner、ANSI escape），避免被 CLI 噪音干扰。

### Transport

- **Protocol**: MCP JSON-RPC 2.0 over stdio
- **Transport**: stdin/stdout pipes（Windows .exe → subprocess.PIPE）
- **Handshake**: `initialize` (protocolVersion=`2024-11-05`) → `notifications/initialized`

### Server info

| Field | Value |
|-------|-------|
| serverInfo.name | `claude/tengu` |
| serverInfo.version | `2.1.170` |
| protocolVersion | `2024-11-05` |

### Tools found（32 个）

**全部是标准的 Claude Code 内置工具**：

| Tool | 功能 | 能否注入手机消息 |
|------|------|-----------------|
| `Agent` | 启动子 agent | ❌ 启动新会话，不能注入 |
| `TaskOutput` | 读取后台任务输出 | ❌ |
| `Bash` | 执行 shell 命令 | ❌ |
| `Glob` / `Grep` / `Read` / `Edit` / `Write` | 文件操作 | ❌ |
| `WebFetch` / `WebSearch` | 网络访问 | ❌ |
| `AskUserQuestion` | 向用户提问 | ❌ |
| `Skill` | 执行 skill | ❌ |
| `EnterPlanMode` | 进入计划模式 | ❌ |
| `DesignSync` | Design 系统同步 | ❌ |
| `TaskCreate` / `TaskGet` / `TaskUpdate` / `TaskList` | 任务管理 | ❌ |
| `EnterWorktree` / `ExitWorktree` | Git worktree | ❌ |
| `Workflow` | 多 agent 工作流 | ❌ |
| `CronCreate` / `CronDelete` / `CronList` | 定时任务 | ❌ |
| `ScheduleWakeup` | 定时唤醒 | ❌ |
| `Monitor` | 后台监控 | ❌ |
| `PushNotification` | 推送通知 | ❌ |
| `PowerShell` | 执行 PowerShell | ❌ |
| `ToolSearch` | 搜索工具 | ❌ |

**结论：没有 `SendMessage`、`CreateTurn`、`AppendUserMessage` 或任何可以将外部消息注入会话的工具。**

### Resources / Prompts

- `resources`: 0 个
- `prompts`: 0 个

没有可用的 resources 或 prompts。

### Can it drive a chat turn?

**❌ 不能直接驱动聊天。**

`claude mcp serve` 将 Claude Code 暴露为 MCP server，但它暴露的是 **Claude Code 自身的工具集**，不是会话管理接口。它没有：

- 发送消息到当前会话的 API
- 注入用户消息的工具
- 读取当前会话 transcript 的 resource
- 创建或恢复会话的 prompt

### Verdict: Option B — MCP serve 不能直接驱动聊天

**Option B confirmed: MCP serve 是工具执行器，不是消息注入器。**

- 它适合让外部系统（Router adapter）调用 Claude Code 工具（执行命令、读写文件等）
- 但它不适合作为"手机 → Claude Code 会话"的消息桥
- 如果 Router adapter 需要在 Claude Code 执行时调用工具（而不是注入消息），MCP serve 可以辅助

---

## Updated Recommendation

### Option B: `claude -p` per-message（推荐）

探针确认 MCP serve 无法直接驱动聊天。回归 Option A（`claude -p`）：

```bash
claude -p "手机用户发来: <content>" \
  --output-format json \
  --session-id <uuid> \
  --append-system-prompt "你是手机用户的代码助手..."
```

**优点**：
- 每次消息 = 一次 LLM 调用，上下文通过 `--session-id` 保留
- JSON 输出可直接解析 `result` 字段
- 不需要长期子进程
- 不依赖 MCP serve

**缺点**：
- 每次 App 消息 = 一次 API 调用，配额消耗快
- 429 超额需要 `--fallback-model` 或重试退避
- 上下文在会话级别保留，不是 turn 级别

**架构**：
```
App → Router → claude-p Adapter → claude -p (per message) → JSON result → Router → App
```

### Option C: MCP serve 作为工具辅助

MCP serve 可用于 **辅助场景**：
- Router adapter 收到 App 消息，需要 Claude Code 执行特定任务（读写文件、运行测试）
- adapter 调用 `claude -p` 并在 prompt 中指示 Claude Code 执行任务
- 但这不是 MCP serve 的设计意图

### Option D: Hybrid

`claude -p` 驱动会话 + MCP serve 提供工具能力（如果有外部工具需要）。

---

## Conclusion

**Answer: B — MCP serve 不能直接驱动聊天，但 `claude -p` 可以。**

| 结论 | 原因 |
|------|------|
| MCP serve 是**工具执行器**，不是**消息注入器** | 32 个 tools 全部是 Claude Code 内置工具；无会话管理 API |
| Option C 不适用于 attached session 桥接 | 无法注入手机消息；只能作为工具辅助 |
| Option A（`claude -p`）是当前**唯一可行路径** | 有 `session-id` 保留上下文，有 JSON 输出，有 `--append-system-prompt` 塑形 |
| MCP serve 可作为工具层辅助 | 当手机用户需要 Claude Code 执行特定工具时可用，但不是主要消息路径 |

### Next step

基于 `claude -p` 构建 claude-code-attached-adapter：
1. Adapter 接收 Router callback
2. 调用 `claude -p <手机消息> --session-id <session> --output-format json`
3. 解析 JSON `result` 字段，转为 `agentReplies`
4. 保留 `--session-id` 跨请求维持上下文
5. 加入 `--permission-mode bypassPermissions`（需要评估安全风险）
6. 处理 429/500 等错误，返回合适的 HTTP 状态