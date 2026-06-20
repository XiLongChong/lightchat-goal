# Sticker Attachments Phase 1 — PC Router 支持 agent 发送表情

日期：2026-06-20

## 目标

支持 agent（Claude Code / mock adapter / callback session）发送结构化表情，
通过扩展 `attachments[]` 协议在 `type: "image"` 旁增加 `type: "sticker"`。
用户暂时不能从 App 发表情。

## 实现

### 1. Sticker catalog — `runtime/sticker_catalog.py`

内置 8 个 sticker：

| stickerId | alt |
|-----------|-----|
| happy_01 | 开心 |
| laugh_01 | 大笑 |
| sad_01 | 委屈 |
| angry_01 | 生气 |
| shy_01 | 害羞 |
| think_01 | 思考 |
| ok_01 | 好的 |
| heart_01 | 喜欢 |

API：
- `is_known_sticker(stickerId) -> bool`
- `canonicalize_sticker_attachment(att) -> dict` — 校验并标准化，未知 sticker 抛 `ValueError`

### 2. Attachment canonicalization 扩展 — `main.py`

`POST /api/messages` 的 `attachments[]` 现在按 type 分路径校验：

- `type: "image"` — 仍走 `app_attachments` 表校验，不变
- `type: "sticker"` — 走 `canonicalize_sticker_attachment()`，不查 `app_attachments`，不要求上传
- `type` 未知 → 400

### 3. Agent reply 支持 sticker

- **`validate_callback_response`**：从 `agentReplies[].attachments` 提取 sticker 并校验
- **`api_channel_post_reply`**：body 新增可选 `attachments` 字段，支持 sticker
- **`insert_app_turn_messages`**：agent reply 现在持久化 `attachments_json`
- **`GET /api/messages`**：agent reply 的 `attachments` 字段始终存在（已有代码自动处理）

### 4. Claude channel tool — `tools/aspocket_channel/`

- `aspocket_reply` tool 新增可选 `stickerId` 参数
- tool description 列出可用 stickerId
- `buildReplyPayload` 支持 `attachments` 参数
- `postReply` 传递 attachments 到 Router

### 5. Mock adapter — `tools/claude_code_mock_adapter.py`

关键词触发（`sticker` / `表情` / `happy`）返回带 `attachments: [{type: "sticker", stickerId: "happy_01"}]` 的 agent reply。

## 协议说明

`attachments[]` 扩展为 union：

```jsonc
// image（已有）
{"type": "image", "attachmentId": "...", "mimeType": "image/png", ...}
// sticker（新增）
{"type": "sticker", "stickerId": "happy_01", "alt": "开心"}
```

规则：
- 每条消息最多 1 个 attachment（Phase 1）
- image 和 sticker 不共存
- sticker 不需要上传，Router 不查 `app_attachments` 表
- `app_messages.attachments_json` 继续存 JSON，无需新表

## 兼容性

- 图片附件 (`type: "image"`) 全部现有测试仍通过
- 旧客户端不发送 sticker → 行为不变
- `GET /api/messages` 的 `attachments` 字段始终存在（`[]` 或 populated）

## 测试

### Python (12 新增)
- `test_canonicalize_sticker_valid` — 已知 sticker canonicalized
- `test_canonicalize_sticker_unknown_raises` — 未知 sticker 抛 ValueError
- `test_canonicalize_sticker_empty_id_raises` — stickerId 缺失抛错
- `test_is_known_sticker` — 正确识别已知/未知
- `test_post_messages_sticker_success` — POST sticker attachment 成功存储
- `test_post_messages_unknown_sticker_rejected` — 未知 stickerId → 400
- `test_callback_sticker_persists_to_store` — callback sticker 持久化到 app_messages
- `test_get_messages_sticker_attachments` — GET 返回 sticker 结构 + DB 验证
- `test_channel_reply_with_sticker` — channel reply endpoint 接受 sticker
- `test_channel_reply_unknown_sticker_rejected` — channel reply 拒绝未知 sticker

### Mock adapter (3 新增)
- `test_sticker_keyword_triggers_sticker_attachment` — sticker/表情/happy 触发词
- `test_sticker_reply_has_text_content` — sticker reply 仍含文字
- `test_status_prompt_does_not_return_sticker` — 非 sticker 词不误触发

### JS helpers (3 新增)
- `buildReplyPayload includes attachments when provided`
- `buildReplyPayload omits attachments when empty array`
- `buildReplyPayload omits attachments when not provided`

## 涉及文件

| 文件 | 改动 |
|------|------|
| `runtime/sticker_catalog.py` | **新建** — sticker catalog + validation |
| `main.py` | 扩展 attachment canonicalization, validate_callback_response, channel reply |
| `memory/store.py` | `insert_app_turn_messages` agent reply 支持 attachments |
| `tools/aspocket_channel/channel.js` | `aspocket_reply` +stickerId param |
| `tools/aspocket_channel/helpers.js` | `buildReplyPayload` +attachments |
| `tools/aspocket_channel/test/helpers.test.js` | +3 sticker tests |
| `tools/claude_code_mock_adapter.py` | sticker 关键词触发 |
| `tests/test_app_adapter.py` | +10 sticker tests |
| `tests/test_claude_code_mock_adapter.py` | +3 sticker mock tests |
