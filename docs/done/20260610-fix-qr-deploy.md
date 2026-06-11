# 微信部署二维码加载失败 — 已修复（2026-06-11）

## 原始 Bug

`docs/bug/20260610-wechat-qr-deploy-failure.md`（已删除）

## 根因

`static/admin.js:198` — `POST /wechat/deploy/start` 响应被丢弃，后续 `GET /wechat/deploy/qr-image` 缺 `data` 参数（400），`GET /wechat/deploy/status` 缺 `qrcode` 参数（422）。同时轮询终态判断为 `connected`，但后端实际返回 `confirmed`。

## 修复内容

`static/admin.js` 三处改动：

1. 保存 start 响应：`const startResp = await request(...)`
2. 传递参数：`qr-image?data=${encodeURIComponent(startResp.qrcode_content)}`，`status?qrcode=${encodeURIComponent(startResp.qrcode)}`
3. 修正终态：`connected` → `confirmed`，增加 `expired`
