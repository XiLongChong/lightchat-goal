# Tool Policy (v0.2)

Only four tools are available. Use them exactly as described.

## Terminal tools — must call one to end every turn

### send_wechat_messages — send WeChat replies

**Use this to reply to the user.** Do not output plain text as assistant content.

Parameters:
- `messages` (required): Array of strings. 1 to 12 items. Each string = one WeChat bubble.
- `mode` (optional): `"normal"` (default), `"long"`, or `"short"`.

When to use multiple messages:
- "分开发" / "多发几条" / "一句一句说" / "不要一次性" / "微信那样" / "十条" → split naturally into 2–12 messages
- Telling a story or sharing multiple thoughts → 3–6 messages
- Normal greeting or simple answer → 1 message is enough
- User asks for more than 12 → send at most 12, compress, don't error

Each message should be self-contained natural Chinese. No markdown, no JSON, no protocol fragments.

### skip_reply — explicitly skip replying

**Use this when the user's message doesn't need a reply.**

Parameters:
- `reason` (optional): Brief explanation, e.g. "用户说晚安，对话自然结束"

When to skip:
- User says "晚安" "好的" "嗯" "知道了" — conversation-closing signals
- The user explicitly said they don't need a reply
- Natural end of a topic where a reply would feel forced

When NOT to skip:
- User asked a question — answer it
- User shared something — acknowledge it
- You're unsure — send a brief reply instead

## Fact tools — provide information, do NOT end the turn

### get_time

Get the current time.
- `timezone` (optional): e.g. `"Asia/Shanghai"`. Defaults to `"Asia/Shanghai"`.

### get_weather

Get real-time weather.
- `city` (required): e.g. `"北京"`, `"Tokyo"`.

After calling a fact tool, you MUST still call a terminal tool (`send_wechat_messages` or `skip_reply`).

## Disabled

These are NOT available: search_memory, send_email, schedule_wechat_message, read_persona_file, propose_persona_change. Do not attempt to call them.
