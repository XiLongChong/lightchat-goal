#!/usr/bin/env node
// AsPocket Claude Code channel server (dev-only).
//
// This is an MCP stdio server that bridges AsPocket mobile app messages
// to a running Claude Code session via the experimental `claude/channel`
// capability.
//
// Flow:
//   1. Connect to Claude Code over stdio (MCP transport).
//   2. Register with the PC Router as a `claude_channel` session.
//   3. Poll the Router for pending user messages.
//   4. Push each message to Claude Code via `notifications/claude/channel`.
//   5. Ack delivery so the Router doesn't re-send.
//   6. Expose `aspocket_reply` tool so Claude Code can reply back; the
//      reply is POSTed to the Router and persisted as an agent message.
//
// Start with:
//   claude --dangerously-load-development-channels server:aspocket
//
// All logs go to stderr — stdout is reserved for JSON-RPC.

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  ListToolsRequestSchema,
  CallToolRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import {
  buildRegistrationPayload,
  buildChannelNotificationParams,
  buildReplyPayload,
  joinRouterUrl,
  pollPendingUrl,
  ackUrl,
  replyUrl,
} from "./helpers.js";

// ── Config (env) ──────────────────────────────────────────────────

const ROUTER_URL = process.env.ASPOCKET_ROUTER_URL || "http://127.0.0.1:8899";
const SESSION_ID = process.env.ASPOCKET_SESSION_ID || "aspocket-claude-1";
const REMOTE_AGENT_ID =
  process.env.ASPOCKET_REMOTE_AGENT_ID || "aspocket-claude-1";
const DISPLAY_NAME =
  process.env.ASPOCKET_DISPLAY_NAME || "Claude Code - AsPocket";
const WORKSPACE = process.env.ASPOCKET_WORKSPACE || "";
const API_KEY = process.env.CHAT_API_KEY || "";
const POLL_INTERVAL_MS = parseInt(
  process.env.ASPOCKET_POLL_INTERVAL_MS || "1500",
  10,
);

if (!API_KEY) {
  console.error("[aspocket] CHAT_API_KEY is required");
  process.exit(1);
}

const DESCRIPTION = WORKSPACE
  ? `Claude Code channel session (workspace: ${WORKSPACE})`
  : "Claude Code channel session";

const INSTRUCTIONS = [
  "Messages come from the AsPocket mobile app.",
  "Reply using the aspocket_reply tool.",
  "Pass clientMessageId from sender_id.",
  "Keep replies concise.",
  "Do not expose hidden reasoning.",
].join(" ");

// ── MCP server ────────────────────────────────────────────────────

const mcp = new Server(
  { name: "aspocket", version: "0.1.0" },
  {
    capabilities: {
      experimental: { "claude/channel": {} },
      tools: {},
    },
    instructions: INSTRUCTIONS,
  },
);

// ── Router HTTP helpers ───────────────────────────────────────────

function authHeaders() {
  return {
    Authorization: `Bearer ${API_KEY}`,
    "Content-Type": "application/json",
  };
}

async function registerToRouter() {
  const payload = buildRegistrationPayload({
    sessionId: SESSION_ID,
    remoteAgentId: REMOTE_AGENT_ID,
    displayName: DISPLAY_NAME,
    description: DESCRIPTION,
  });
  const url = joinRouterUrl(ROUTER_URL, "/api/app-agent-sessions/register");
  try {
    const resp = await fetch(url, {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify(payload),
    });
    if (!resp.ok) {
      const body = await resp.text().catch(() => "");
      throw new Error(
        `HTTP ${resp.status} ${resp.statusText} body=${body.slice(0, 200)}`,
      );
    }
    await resp.json();
    console.error(
      `[aspocket] registered sessionId=${SESSION_ID} remoteAgentId=${REMOTE_AGENT_ID}`,
    );
  } catch (err) {
    throw new Error(`registration to ${url} failed: ${err.message}`);
  }
}

async function pollPending() {
  const url = pollPendingUrl(ROUTER_URL, SESSION_ID, 10);
  const resp = await fetch(url, { headers: authHeaders() });
  if (!resp.ok) {
    const body = await resp.text().catch(() => "");
    throw new Error(
      `HTTP ${resp.status} ${resp.statusText} body=${body.slice(0, 200)}`,
    );
  }
  const data = await resp.json();
  return data.messages || [];
}

async function ackDelivery(deliveryId) {
  const url = ackUrl(ROUTER_URL, SESSION_ID, deliveryId);
  const resp = await fetch(url, { method: "POST", headers: authHeaders() });
  if (!resp.ok) {
    const body = await resp.text().catch(() => "");
    throw new Error(
      `HTTP ${resp.status} ${resp.statusText} body=${body.slice(0, 200)}`,
    );
  }
  await resp.json();
}

async function postReply(clientMessageId, text, sourceToolCallId) {
  const payload = buildReplyPayload({
    clientMessageId,
    content: text,
    sourceToolCallId,
  });
  const url = replyUrl(ROUTER_URL, SESSION_ID);
  const resp = await fetch(url, {
    method: "POST",
    headers: authHeaders(),
    body: JSON.stringify(payload),
  });
  if (!resp.ok) {
    const body = await resp.text().catch(() => "");
    throw new Error(
      `HTTP ${resp.status} ${resp.statusText} body=${body.slice(0, 200)}`,
    );
  }
  return await resp.json();
}

// ── Polling loop ──────────────────────────────────────────────────

let pollTimer = null;

async function pollOnce() {
  let messages;
  try {
    messages = await pollPending();
  } catch (err) {
    console.error(`[aspocket] poll failed: ${err.message}`);
    return;
  }
  for (const msg of messages) {
    try {
      const params = buildChannelNotificationParams({
        content: msg.content,
        clientMessageId: msg.clientMessageId,
        remoteAgentId: msg.remoteAgentId,
        deliveryId: msg.deliveryId,
      });
      await mcp.notification({
        method: "notifications/claude/channel",
        params,
      });
      // Notification written to transport — ack so the Router doesn't
      // re-deliver on the next poll.  If ack fails, the message stays
      // pending and will be retried next cycle (at-least-once).
      await ackDelivery(msg.deliveryId);
      console.error(
        `[aspocket] delivered deliveryId=${msg.deliveryId} clientMessageId=${msg.clientMessageId}`,
      );
    } catch (err) {
      console.error(
        `[aspocket] deliver failed for deliveryId=${msg.deliveryId}: ${err.message}`,
      );
      // Leave unacked — next poll will retry.
    }
  }
}

function startPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(pollOnce, POLL_INTERVAL_MS);
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

// ── Tool: aspocket_reply ──────────────────────────────────────────

mcp.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: "aspocket_reply",
      description: "Reply to an AsPocket mobile app message",
      inputSchema: {
        type: "object",
        properties: {
          clientMessageId: {
            type: "string",
            description: "The sender_id from the channel message",
          },
          text: {
            type: "string",
            description: "The reply text to send back",
          },
        },
        required: ["clientMessageId", "text"],
      },
    },
  ],
}));

mcp.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;
  if (name !== "aspocket_reply") {
    return {
      content: [{ type: "text", text: `unknown tool: ${name}` }],
      isError: true,
    };
  }
  const clientMessageId = String(args?.clientMessageId || "").trim();
  const text = String(args?.text || "").trim();
  if (!clientMessageId || !text) {
    return {
      content: [
        {
          type: "text",
          text: "send failed: clientMessageId and text are required",
        },
      ],
      isError: true,
    };
  }
  try {
    await postReply(clientMessageId, text, undefined);
    return {
      content: [{ type: "text", text: "sent" }],
    };
  } catch (err) {
    return {
      content: [{ type: "text", text: `send failed: ${err.message}` }],
      isError: true,
    };
  }
});

// ── Startup ───────────────────────────────────────────────────────

const transport = new StdioServerTransport();
await mcp.connect(transport);

try {
  await registerToRouter();
} catch (err) {
  console.error(`[aspocket] ${err.message}`);
  console.error("[aspocket] is the Router running? exiting.");
  stopPolling();
  process.exit(1);
}

startPolling();
console.error(
  `[aspocket] polling every ${POLL_INTERVAL_MS}ms session=${SESSION_ID}`,
);

// ── Graceful shutdown ─────────────────────────────────────────────

function shutdown() {
  stopPolling();
  process.exit(0);
}

process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
