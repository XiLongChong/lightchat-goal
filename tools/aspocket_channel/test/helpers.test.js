import { test } from "node:test";
import assert from "node:assert/strict";
import {
  buildRegistrationPayload,
  buildChannelNotificationParams,
  buildReplyPayload,
  joinRouterUrl,
  pollPendingUrl,
  ackUrl,
  replyUrl,
  heartbeatUrl,
  offlineUrl,
} from "../helpers.js";

// ── buildRegistrationPayload ──────────────────────────────────────

test("buildRegistrationPayload sets kind=claude_channel and default sourceLabel", () => {
  const payload = buildRegistrationPayload({
    sessionId: "s1",
    remoteAgentId: "r1",
    displayName: "D",
    description: "desc",
  });
  assert.equal(payload.sessionId, "s1");
  assert.equal(payload.remoteAgentId, "r1");
  assert.equal(payload.displayName, "D");
  assert.equal(payload.description, "desc");
  assert.equal(payload.kind, "claude_channel");
  assert.equal(payload.sourceLabel, "claude-code-channel");
  assert.equal(payload.callbackUrl, undefined);
});

test("buildRegistrationPayload allows custom sourceLabel", () => {
  const payload = buildRegistrationPayload({
    sessionId: "s1",
    remoteAgentId: "r1",
    displayName: "D",
    description: "desc",
    sourceLabel: "custom",
  });
  assert.equal(payload.sourceLabel, "custom");
});

// ── buildChannelNotificationParams ────────────────────────────────

test("buildChannelNotificationParams builds correct structure", () => {
  const params = buildChannelNotificationParams({
    content: "hello",
    clientMessageId: "cm1",
    remoteAgentId: "ra1",
    deliveryId: "d1",
  });
  assert.equal(params.content, "hello");
  assert.equal(params.meta.sender, "AsPocket");
  assert.equal(params.meta.sender_id, "cm1");
  assert.equal(params.meta.remote_agent_id, "ra1");
  assert.equal(params.meta.delivery_id, "d1");
});

test("buildChannelNotificationParams coerces values to string", () => {
  const params = buildChannelNotificationParams({
    content: 123,
    clientMessageId: 456,
    remoteAgentId: 789,
    deliveryId: 0,
  });
  assert.equal(params.content, "123");
  assert.equal(params.meta.sender_id, "456");
  assert.equal(params.meta.delivery_id, "0");
});

test("buildChannelNotificationParams meta keys are valid identifiers", () => {
  // Claude Code silently drops meta keys with hyphens or other chars.
  // All our keys must be letters/digits/underscores.
  const params = buildChannelNotificationParams({
    content: "x",
    clientMessageId: "c",
    remoteAgentId: "r",
    deliveryId: "d",
  });
  for (const key of Object.keys(params.meta)) {
    assert.match(key, /^[a-zA-Z_][a-zA-Z0-9_]*$/, `meta key "${key}" is not a valid identifier`);
  }
});

// ── buildReplyPayload ─────────────────────────────────────────────

test("buildReplyPayload includes required fields only", () => {
  const payload = buildReplyPayload({
    clientMessageId: "cm1",
    content: "reply",
  });
  assert.equal(payload.clientMessageId, "cm1");
  assert.equal(payload.content, "reply");
  assert.equal(payload.sourceToolCallId, undefined);
});

test("buildReplyPayload includes sourceToolCallId when provided", () => {
  const payload = buildReplyPayload({
    clientMessageId: "cm1",
    content: "reply",
    sourceToolCallId: "stc1",
  });
  assert.equal(payload.sourceToolCallId, "stc1");
});

test("buildReplyPayload omits sourceToolCallId when empty", () => {
  const payload = buildReplyPayload({
    clientMessageId: "cm1",
    content: "reply",
    sourceToolCallId: "",
  });
  assert.equal(payload.sourceToolCallId, undefined);
});

// ── URL helpers ───────────────────────────────────────────────────

test("joinRouterUrl strips trailing slashes from base", () => {
  assert.equal(
    joinRouterUrl("http://127.0.0.1:8899/", "/api/x"),
    "http://127.0.0.1:8899/api/x",
  );
  assert.equal(
    joinRouterUrl("http://127.0.0.1:8899", "/api/x"),
    "http://127.0.0.1:8899/api/x",
  );
  assert.equal(
    joinRouterUrl("http://127.0.0.1:8899///", "/api/x"),
    "http://127.0.0.1:8899/api/x",
  );
});

test("pollPendingUrl builds correct URL with limit", () => {
  const url = pollPendingUrl("http://127.0.0.1:8899", "sess-1", 5);
  assert.equal(
    url,
    "http://127.0.0.1:8899/api/app-agent-sessions/sess-1/pending-messages?limit=5",
  );
});

test("pollPendingUrl uses default limit 10", () => {
  const url = pollPendingUrl("http://127.0.0.1:8899", "sess-1");
  assert.equal(
    url,
    "http://127.0.0.1:8899/api/app-agent-sessions/sess-1/pending-messages?limit=10",
  );
});

test("ackUrl builds correct URL", () => {
  const url = ackUrl("http://127.0.0.1:8899", "sess-1", "del-1");
  assert.equal(
    url,
    "http://127.0.0.1:8899/api/app-agent-sessions/sess-1/pending-messages/del-1/ack",
  );
});

test("replyUrl builds correct URL", () => {
  const url = replyUrl("http://127.0.0.1:8899", "sess-1");
  assert.equal(
    url,
    "http://127.0.0.1:8899/api/app-agent-sessions/sess-1/replies",
  );
});

test("URLs encode special characters in path segments", () => {
  const url = pollPendingUrl("http://127.0.0.1:8899", "sess/1");
  assert.equal(
    url,
    "http://127.0.0.1:8899/api/app-agent-sessions/sess%2F1/pending-messages?limit=10",
  );
});

// ── heartbeat / offline URL helpers ───────────────────────────────

test("heartbeatUrl builds correct URL with no query or body path", () => {
  const url = heartbeatUrl("http://127.0.0.1:8899", "sess-1");
  assert.equal(
    url,
    "http://127.0.0.1:8899/api/app-agent-sessions/sess-1/heartbeat",
  );
});

test("heartbeatUrl strips trailing slash from base", () => {
  const url = heartbeatUrl("http://127.0.0.1:8899/", "sess-1");
  assert.equal(
    url,
    "http://127.0.0.1:8899/api/app-agent-sessions/sess-1/heartbeat",
  );
});

test("heartbeatUrl encodes special characters in sessionId", () => {
  const url = heartbeatUrl("http://127.0.0.1:8899", "sess/1");
  assert.equal(
    url,
    "http://127.0.0.1:8899/api/app-agent-sessions/sess%2F1/heartbeat",
  );
});

test("offlineUrl builds correct URL", () => {
  const url = offlineUrl("http://127.0.0.1:8899", "sess-1");
  assert.equal(
    url,
    "http://127.0.0.1:8899/api/app-agent-sessions/sess-1/offline",
  );
});
