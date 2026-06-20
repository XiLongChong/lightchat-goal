# Phone Context Phase 2C — Claude Channel Tool Usability

## Summary

Improved the `aspocket_get_phone_context` MCP tool so Claude agents are more
likely to call it when needed and less likely to misinterpret the response.

No App changes. No new collection fields. No Router endpoint changes.

## Changes

### 1. Tool description (`channel.js`)

The tool description now explicitly:

- Lists **when to call**: user asks about screen, chat, battery, network, notifications
- Warns about **staleness**: check `status`, `ageSeconds`, `missing` before relying
- States **privacy boundaries**: what the tool NEVER returns (messages, text, location, etc.)
- Documents **parameter guidance**: `maxAgeSeconds` trade-off, `types` selection

### 2. Error messages (`channel.js` handler)

Error responses now include actionable guidance:
- HTTP errors → "tell the user you cannot check their phone state right now"
- Fetch failures → "tell the user the phone snapshot is currently unreachable"

Previously errors returned bare `HTTP 500: ...` strings.

### 3. Response summariser (`helpers.js` → `summarizePhoneContext`)

New pure function that produces a human-readable summary from a parsed
`GET /api/phone-context/latest` JSON response:

- **Unavailable** → explains reason, missing types, tells agent not to guess
- **Available** → lists age (flagging stale if >120s), device fields (battery, network, timezone), app fields (state, screen, contact, agent, notifications)
- **Partial** → reports which types are missing, tells agent not to fabricate
- **Null/empty input** → safe fallback

### 4. Tests (`test/helpers.test.js`)

Added 8 tests:

| Test | What it verifies |
|------|-----------------|
| phoneContextUrl with empty array | URL builder handles edge case |
| phoneContextUrl preserves custom maxAgeSeconds | Non-default value passes through |
| phoneContextUrl clamps negative maxAgeSeconds | Floor at 0 |
| summarizePhoneContext unavailable | Reason + missing types + "do not guess" |
| summarizePhoneContext ok with full data | All device/app fields in summary |
| summarizePhoneContext stale | Age > 120s flagged as stale |
| summarizePhoneContext missing types | Missing reported + "do not fabricate" |
| summarizePhoneContext null/empty | Graceful fallback |

Existing 3 phoneContextUrl tests continue to pass.

## When the agent should call

- "我现在在哪个页面？" → call with `types=["app_status"]`
- "手机电量多少？" → call with `types=["device_status"]`
- "有在跟谁聊天吗？" → call with both (default)
- "通知开着吗？" → call with `types=["app_status"]`

## When NOT to call

- Asking about message content → this tool never returns messages
- Asking about location → this tool never returns location
- Asking "right now" with sub-second precision → this is a cached snapshot, not real-time

## Privacy guarantees (documented in tool description)

The tool NEVER returns:
- Messages, typed text, input field content
- Location, SSID, IP address
- Clipboard data, media library contents
- Contact lists, health data

## Manual acceptance check

In a real Claude Code session connected to the AsPocket channel, ask:

1. "我现在手机在哪个页面？" → agent should call `aspocket_get_phone_context` with `types=["app_status"]`, check `screen` field
2. "手机电量多少？" → agent should call with `types=["device_status"]`, check `batteryPercent`
3. Ask when no snapshot exists → agent should report unavailable, not guess
