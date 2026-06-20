# AsPocket PC Router current capabilities and upgrade map (2026-06-21)

This is the Router-side closeout note before the planned cross-repo code review and UI phase.
It records the current App adapter, external-agent routing, rich-message protocol, Phone Context
tooling, and temporary solutions that must be revisited.

Related App note:
`D:\projects\app-phone\docs\done\2026-06-21\current-capabilities-and-upgrade-map.md`

## Current Router Capabilities

### App Adapter Endpoints

The Router exposes the App-facing REST surface:

- `GET /api/profile`
- `POST /api/messages`
- `GET /api/messages`
- `POST /api/attachments`
- `GET /api/attachments/{id}`
- `GET /api/contact-invitations`
- `POST /api/phone-context/snapshots`
- `GET /api/phone-context/latest`
- App agent session registration/heartbeat/offline/list endpoints
- Claude channel pending/poll/ack/reply endpoints

Authentication is shared with existing chat auth. Do not print tokens in tests or logs.

### Agent Routing

Current message targets:

- `default`: built-in/internal PC agent path.
- `attached_session`: external HTTP callback sessions.
- `claude_channel`: Claude Code channel path through MCP stdio + pending inbox.

The App sees stable contact/session metadata through `remoteAgentId`, not through these internal
implementation details.

### Rich Messages

Current attachment union:

- `image`
  - uploaded through `POST /api/attachments`
  - stored in `app_attachments`
  - referenced from `app_messages.attachments_json`
- `sticker`
  - validated against the built-in sticker catalog
  - stored directly in message attachments JSON
  - does not use `attachmentId`

Phase 1 limit: one attachment per message.

### Phone Context

Router stores latest App-uploaded snapshots and exposes them through:

- `GET /api/phone-context/latest`
- Claude channel tool `aspocket_get_phone_context`

Current context fields are cached, not live:

- `device_status`: battery, charging, power source, network type, online state, local time, time
  zone.
- `app_status`: app foreground/background, current screen, active contact identity, notification
  enabled state.

The tool description tells agents to check `status`, `ageSeconds`, and `missing` before using
the response, and to avoid guessing when unavailable.

Privacy boundary: Router must not ask the App for messages, typed text, contact lists, location,
SSID, IP address, clipboard contents, media library contents, or health data as part of Phone
Context.

## Not Implemented

- No Router-managed external-agent process lifecycle.
- No generic persistent agent-session queue.
- No per-contact token/auth.
- No realtime push from Router to App.
- No FCM.
- No audio attachment protocol.
- No speech generation endpoint.
- No user voice ingestion.
- No multi-attachment protocol.
- No production admin UI for managing attached sessions.

## Transitional Solutions That Must Be Upgraded Later

### `claude_channel` Naming

Current: `claude_channel` is an implementation-specific target name.

Upgrade later:
- Normalize internally toward generic `channel`.
- Keep backwards-compatible aliases during migration.
- Do not introduce one Router target name per external agent.

### In-Memory Channel Pending Inbox

Current: channel messages wait in an in-memory pending inbox until the Claude channel polls and
acks them.

Risk:
- Pending messages are lost if Router restarts before Claude Code receives them.

Upgrade later:
- Persist channel pending messages in SQLite if this path becomes production-critical.
- Define delivery semantics before adding retries.

### Attached Session Callback

Current: attached sessions use synchronous callback and localhost callback URL validation.

Upgrade later:
- Add async callback/push only if real external agents need it.
- Keep callback URL SSRF restrictions.

### Phone Context Cache

Current: Router stores raw JSON snapshots and returns latest matching snapshot. Validation is
permissive for optional App fields.

Upgrade later:
- Add stricter versioned schema only if multiple App versions diverge.
- Keep old clients compatible.
- Do not add high-sensitivity fields without an explicit privacy review.

### Main Router File Size

Current: App adapter and session endpoints still have significant wiring in `main.py`.

Upgrade later:
- Extract App adapter routing into a focused module only after the current review.
- Keep tests around auth-first behavior, idempotency, and response shapes before moving code.

## Code Review Focus

For the upcoming PC review, prioritize:

1. App adapter auth-first behavior and error consistency.
2. Message idempotency with attachments.
3. Attachment canonicalization and path traversal defenses.
4. Session heartbeat/stale TTL behavior.
5. In-memory channel inbox loss modes.
6. Phone Context snapshot validation/storage/tool description.
7. Places where `main.py` should be split after tests pin behavior.

This document is the current Router truth for AsPocket. Older App adapter notes that only mention
`/api/profile` and `/api/messages` are historical.
