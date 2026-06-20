# 富消息协议 — attachments[] union

日期：2026-06-20

`attachments[]` 是富消息 union 协议。每条 message（user 或 agent reply）可携带
0 或 1 个 attachment（Phase 1 限制）。consumer 必须按 `type` 字段解析，
不得将一种 type 的必填字段套用到另一种 type。

## Attachment type: `image`

```
{
  "type": "image",
  "attachmentId": "app_att_xxxxxxxx",
  "mimeType": "image/png",
  "fileName": "photo.png",
  "sizeBytes": 12345,
  "url": "http://127.0.0.1:8899/api/attachments/app_att_xxxxxxxx"
}
```

约束：

- `attachmentId` **必填** — 必须已通过 `POST /api/attachments` 上传。
- `mimeType` 必填 — Phase 1 仅 `image/*`。
- `sizeBytes` 必填 — 文件字节数。
- `url` 必填 — 下载地址。
- 所有字段由 PC Router 从 `app_attachments` 表 canonicalize。
- 客户端的 url 可能被 Router 覆盖。

## Attachment type: `sticker`

```
{
  "type": "sticker",
  "stickerId": "happy_01",
  "alt": "开心"
}
```

约束：

- `stickerId` **必填** — 必须是内置 catalog 中的值。
- `attachmentId` / `mimeType` / `sizeBytes` / `url` **不存在**。
- 不需要上传 — Router 直接校验 stickerId。
- 未知 stickerId → 400（PC Router 拒绝）。

内置 catalog（`runtime/sticker_catalog.py`）：

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

## 全局约束

### Consumer 规则

- 必须按 `type` 做分支解析。
- `type: "image"` → 服务端已保证所有 5 个字段存在。
- `type: "sticker"` → `attachmentId` 等字段不存在，不要默认值。
- 未知 `type` → 目前 PC Router 拒绝（400/502），未来可能新增。

### 每条消息限制

- Phase 1：每条 message 最多 **1 个** attachment。
- image 和 sticker 不共存（满足 `max 1` 约束）。

### 当前支持入口

| 入口 | attachment 方向 |
|------|-----------------|
| `POST /api/messages` | user → Router (image + sticker) |
| `attached_session callback` `agentReplies[].attachments` | agent → Router (sticker) |
| `POST /api/app-agent-sessions/{id}/replies` | channel → Router (sticker) |
| `GET /api/messages` | Router → App (全部 type) |
| `POST /api/attachments` | App → Router (image 上传) |

### 幂等性

`POST /api/messages` 的 `clientMessageId` 幂等检查包含 attachments：
- 同 cid + 同 content + 同 attachments（按 canonical JSON 比对） → 返回既有结果
- 同 cid 但 content 或 attachments 不同 → 409

## 后续预留：audio

**不要复用 image attachmentId 规则。** audio 应定义自己的必填字段：

```
// 预留，未实现
{
  "type": "audio",
  "audioId": "...",        // 必填 — 音频资源 ID
  "durationMs": 5000,      // 必填 — 毫秒时长
  "transcript": "...",     // 可选 — 转写文本
  "mimeType": "audio/aac"  // 可选 — 编码格式
}
```

原则：

- `attachmentId` 不是 global，"每种 type 定义自己的标识符字段名称"。
- 新增 type 时更新本文档 + `runtime/` 下的校验模块。
- App DTO 必须保持 union/type-aware，不假设 attachment 形态。
