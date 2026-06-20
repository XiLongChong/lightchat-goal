# Image Attachments Phase 1 — PC Router 支持

日期：2026-06-20

## 目标

支持 App 上传图片附件、发送带图片的消息、history 返回附件 metadata，
持久化附件文件和 metadata 到 PC Router。不做图片理解 / OCR / 视觉模型。

## 已实现

### A. 附件上传 — `POST /api/attachments`

- `multipart/form-data`，字段 `file`（必填）+ 可选 `clientAttachmentId`
- 鉴权复用 app adapter (`_require_chat_auth`)，**401 先于文件解析**
- Phase 1 只接受 `image/*`，非图片 → 400
- 单文件最大 10 MB，超限 → 413
- 空文件 → 400
- 文件名从 `os.path.basename` 清洗，存储为 `data/app_attachments/<attachmentId>.<ext>`
- `attachmentId` = `app_att_<uuid4 hex>`
- 响应返回 `attachmentId`, `type`, `mimeType`, `fileName`, `sizeBytes`, `url`, `createdAt`
- **没有引入新的 Python 依赖**（手动 multipart 解析代替 `python-multipart`）

### B. 附件下载 — `GET /api/attachments/{attachmentId}`

- app auth 必要
- `attachmentId` 正则 `^app_att_[a-f0-9]+$` 校验，不匹配 → 404（防路径穿越）
- `FileResponse`，media_type = 存储的 mimeType
- 二次防御：storage path 解析后必须在 `data/app_attachments/` 子树内

### C. 消息协议扩展 — `POST /api/messages`

新增可选 `attachments` 数组（每项含 `attachmentId`, `type`, `mimeType`, `fileName`, `sizeBytes`, `url`）：

- 兼容旧客户端：无 attachments 时行为不变
- **content 为空 + attachments 非空**：允许发送（content 存空字符串）
- **content 为空 + attachments 为空** → 400
- 每条消息最多 1 个 attachment（Phase 1），超限 → 400
- attachmentId 必须已上传存在，否则 → 400
- **幂等扩展**：同 `clientMessageId` + 同 content + 同 attachments → 返回既有结果；同 cid 但 content 或 attachments 不同 → 409

### D. `GET /api/messages` 返回

每条 message 增加 `attachments: [...]`（无附件时 = `[]`）。旧字段 `remoteId/clientMessageId/role/content/createdAt` 不变。

### E. 存储

- `app_messages` 新增 `attachments_json TEXT NOT NULL DEFAULT '[]'`（增量 migration：新安装 CREATE TABLE 包含，已有 DB 自动 ALTER TABLE ADD COLUMN）
- 新增 `app_attachments` 表：`attachment_id TEXT PK`, `type`, `mime_type`, `file_name`, `size_bytes`, `storage_path`, `created_at`
- 附件文件存储到 `data/app_attachments/<id>.<ext>`

### F. Agent 路径行为

- **default internal agent**：空 content 时传 `"用户发送了一张图片。"` 给 agent
- **attached_session callback**：payload 包含 `attachments` 字段
- **claude_channel pending inbox**：pending message 含 `attachments` 字段；channel server 不会崩
- `aspocket_reply` 仍只支持 text reply

## 测试

新增 19 个测试（tests/test_app_adapter.py）：

1. 上传 image 成功，返回 metadata/url，文件落盘
2. 上传非 image → 400
3. 上传超过 10 MB → 413
4. 未授权上传 → 401，且不落文件
5. GET attachment 成功返回 FileResponse 且 media_type 正确
6. GET missing attachment → 404
6b. 路径穿越 → 404
7. POST content empty + image attachment 成功
8. POST no content + no attachments → 400
9. POST unknown attachmentId → 400
10. >1 attachment → 400
11. GET /api/messages 返回 attachments metadata
12. 幂等同 cid + 同 attachment → 返回既有结果
13. 幂等同 cid + 不同 attachment → 409
14. claude_channel pending inbox 保存 attachments，不崩
15. callback payload 包含 attachments

Store 层测试：app_message attachments 存储/回读，app_attachment insert/get/get_multiple。

## 未做（Phase 2+）

- OCR / vision / 视觉模型
- 图片回复生成（agent 只能发文字）
- 多图（>1 attachment）
- 文件附件（非 image/*）
- relay / FCM
- App 端代码
- PC UI

## 依赖决策

**不引入 `python-multipart`**：manually parse multipart/form-data from raw body。
理由：CLAUDE.md 明确 `不要引入新的依赖 除非不可避免`。手动解析可满足
Phase 1 需求（单文件 + 可选 text 字段），避免新增依赖。后续多文件场景可
重新评估。

## 涉及文件

| 文件 | 改动 |
|------|------|
| `main.py` | +POST/GET `/api/attachments`，扩展 POST/GET `/api/messages`，multipart parser，attachment helpers |
| `memory/store.py` | +`app_attachments` table，+`attachments_json` column，+incremental migration，+insert/get methods |
| `runtime/channel_pending_inbox.py` | +`attachments` param on `add()`，included in poll response |
| `tests/test_app_adapter.py` | +19 tests for Phase 1 scenarios |
