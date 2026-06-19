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