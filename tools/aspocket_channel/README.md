# AsPocket Claude Code Channel (dev-only)

Dev-only MCP stdio server that bridges AsPocket mobile app messages into
a running Claude Code session via the experimental `claude/channel`
capability.

This is a minimal prototype — no setup CLI, no QR, no npm package. It
registers with the PC Router, polls pending user messages, pushes them
to Claude Code as channel notifications, and exposes an `aspocket_reply`
tool so Claude Code can reply back.

## Requirements

- Node.js >= 18 (uses built-in `fetch` and `node --test`)
- `@modelcontextprotocol/sdk` (installed via `npm install`)
- Claude Code v2.1.80+ (channels are in research preview)
- PC Router running on `http://127.0.0.1:8899` (default)
- `CHAT_API_KEY` env var set to the Router's API key

## Install

```bash
cd tools/aspocket_channel
npm install
```

## Register with Claude Code

Add the server to your Claude Code MCP config (user-level
`~/.claude.json` or project-level `.mcp.json`):

```json
{
  "mcpServers": {
    "aspocket": {
      "command": "node",
      "args": ["D:\\projects\\goal\\tools\\aspocket_channel\\channel.js"]
    }
  }
}
```

Or via CLI:

```bash
claude mcp add --scope user aspocket -- node D:\projects\goal\tools\aspocket_channel\channel.js
```

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `ASPOCKET_ROUTER_URL` | `http://127.0.0.1:8899` | PC Router base URL |
| `ASPOCKET_SESSION_ID` | `aspocket-claude-1` | Session id to register |
| `ASPOCKET_REMOTE_AGENT_ID` | `aspocket-claude-1` | Router remoteAgentId |
| `ASPOCKET_DISPLAY_NAME` | `Claude Code - AsPocket` | Display name in App contacts |
| `ASPOCKET_WORKSPACE` | _(empty)_ | Optional workspace label for description |
| `CHAT_API_KEY` | _(required)_ | Router API key (never printed) |
| `ASPOCKET_POLL_INTERVAL_MS` | `1500` | Poll interval in milliseconds |

## Run

1. **Start the PC Router** (from the project root):

   ```bash
   python main.py
   ```

2. **Start Claude Code** with the development channels flag:

   ```bash
   claude --dangerously-load-development-channels server:aspocket
   ```

   The channel server starts automatically as a subprocess, registers
   with the Router, and begins polling.

3. **App side**: refresh contact invitations in AsPocket, accept the
   "Claude Code - AsPocket" contact.

4. **Send a message** from the phone. Within `ASPOCKET_POLL_INTERVAL_MS`
   the message appears in the Claude Code terminal as a
   `<channel source="aspocket" ...>` event.

5. **Claude Code replies** by calling the `aspocket_reply` tool with the
   `clientMessageId` (from `sender_id`) and `text`. The reply is POSTed
   to the Router and persisted as an agent message.

6. **App side**: refresh chat history to see Claude Code's reply.

## How it works

```
Phone → App → POST /api/messages (remoteAgentId=aspocket-claude-1)
  → Router writes user app_message + pending inbox entry
  → Channel server polls GET /api/app-agent-sessions/{sid}/pending-messages
  → Channel server sends notifications/claude/channel to Claude Code
  → Channel server acks the delivery
  → Claude Code calls aspocket_reply tool
  → Channel server POSTs /api/app-agent-sessions/{sid}/replies
  → Router writes agent app_message
  → App GET /api/messages sees the reply
```

## Test

```bash
cd tools/aspocket_channel
npm test
```

Runs pure-helper unit tests with `node --test` (no MCP or Router
needed).

## Notes

- All logs go to stderr; stdout is reserved for JSON-RPC.
- `CHAT_API_KEY` is never printed or logged.
- Pending inbox is in-memory on the Router side — if the Router
  restarts, pending messages are lost. The App can re-send with the
  same `clientMessageId` (idempotent at the DB layer).
- Delivery is at-least-once: if the channel server crashes before
  acking, the next poll re-delivers the same message.
