// Pure helpers for the AsPocket Claude Code channel server.
//
// No MCP SDK imports — safe to import from tests without starting a
// stdio server.  All functions are synchronous and side-effect free.

/**
 * Build the body for `POST /api/app-agent-sessions/register`.
 *
 * claude_channel sessions do not need a callbackUrl — the Router stores
 * messages in an in-memory pending inbox polled by this channel server.
 *
 * @param {object} opts
 * @param {string} opts.sessionId
 * @param {string} opts.remoteAgentId
 * @param {string} opts.displayName
 * @param {string} opts.description
 * @param {string} [opts.sourceLabel="claude-code-channel"]
 * @returns {object}
 */
export function buildRegistrationPayload({
  sessionId,
  remoteAgentId,
  displayName,
  description,
  sourceLabel = "claude-code-channel",
}) {
  return {
    sessionId,
    remoteAgentId,
    displayName,
    description,
    kind: "claude_channel",
    sourceLabel,
  };
}

/**
 * Build the params for a `notifications/claude/channel` MCP notification.
 *
 * Each meta key becomes an attribute on the `<channel>` tag inside
 * Claude Code's context.  Keys must be identifiers (letters, digits,
 * underscores) — hyphens and other characters are silently dropped by
 * Claude Code.
 *
 * @param {object} opts
 * @param {string} opts.content       — message body
 * @param {string} opts.clientMessageId — original App message id (sender_id)
 * @param {string} opts.remoteAgentId   — Router remoteAgentId
 * @param {string} opts.deliveryId      — Router pending delivery id
 * @returns {{content: string, meta: {sender: string, sender_id: string, remote_agent_id: string, delivery_id: string}}}
 */
export function buildChannelNotificationParams({
  content,
  clientMessageId,
  remoteAgentId,
  deliveryId,
}) {
  return {
    content: String(content),
    meta: {
      sender: "AsPocket",
      sender_id: String(clientMessageId),
      remote_agent_id: String(remoteAgentId),
      delivery_id: String(deliveryId),
    },
  };
}

/**
 * Build the body for `POST /api/app-agent-sessions/{sessionId}/replies`.
 *
 * sourceToolCallId is optional — omitted from the payload when not
 * provided so the Router generates its own.
 *
 * @param {object} opts
 * @param {string} opts.clientMessageId
 * @param {string} opts.content
 * @param {string} [opts.sourceToolCallId]
 * @returns {object}
 */
export function buildReplyPayload({ clientMessageId, content, sourceToolCallId }) {
  const payload = {
    clientMessageId: String(clientMessageId),
    content: String(content),
  };
  if (sourceToolCallId) {
    payload.sourceToolCallId = String(sourceToolCallId);
  }
  return payload;
}

/**
 * Join a Router base URL with an absolute path, stripping trailing
 * slashes from the base.
 *
 * @param {string} base  — e.g. "http://127.0.0.1:8899"
 * @param {string} path  — e.g. "/api/app-agent-sessions/register"
 * @returns {string}
 */
export function joinRouterUrl(base, path) {
  return `${base.replace(/\/+$/, "")}${path}`;
}

/**
 * Build the poll URL: `GET /api/app-agent-sessions/{sessionId}/pending-messages`.
 *
 * @param {string} base
 * @param {string} sessionId
 * @param {number} [limit=10]
 * @returns {string}
 */
export function pollPendingUrl(base, sessionId, limit = 10) {
  return joinRouterUrl(
    base,
    `/api/app-agent-sessions/${encodeURIComponent(sessionId)}/pending-messages?limit=${limit}`,
  );
}

/**
 * Build the ack URL: `POST /api/app-agent-sessions/{sessionId}/pending-messages/{deliveryId}/ack`.
 *
 * @param {string} base
 * @param {string} sessionId
 * @param {string} deliveryId
 * @returns {string}
 */
export function ackUrl(base, sessionId, deliveryId) {
  return joinRouterUrl(
    base,
    `/api/app-agent-sessions/${encodeURIComponent(sessionId)}/pending-messages/${encodeURIComponent(deliveryId)}/ack`,
  );
}

/**
 * Build the reply URL: `POST /api/app-agent-sessions/{sessionId}/replies`.
 *
 * @param {string} base
 * @param {string} sessionId
 * @returns {string}
 */
export function replyUrl(base, sessionId) {
  return joinRouterUrl(
    base,
    `/api/app-agent-sessions/${encodeURIComponent(sessionId)}/replies`,
  );
}

/**
 * Build the heartbeat URL: `POST /api/app-agent-sessions/{sessionId}/heartbeat`.
 *
 * The channel server calls this every ~30s while running so the Router's
 * stale-TTL detection keeps the session online.  The endpoint takes no
 * body — the URL alone is the signal.
 *
 * @param {string} base
 * @param {string} sessionId
 * @returns {string}
 */
export function heartbeatUrl(base, sessionId) {
  return joinRouterUrl(
    base,
    `/api/app-agent-sessions/${encodeURIComponent(sessionId)}/heartbeat`,
  );
}

/**
 * Build the offline URL: `POST /api/app-agent-sessions/{sessionId}/offline`.
 *
 * Called best-effort on shutdown.  Not relied upon — the Router's
 * heartbeat TTL is the source of truth for liveness.
 *
 * @param {string} base
 * @param {string} sessionId
 * @returns {string}
 */
export function offlineUrl(base, sessionId) {
  return joinRouterUrl(
    base,
    `/api/app-agent-sessions/${encodeURIComponent(sessionId)}/offline`,
  );
}
