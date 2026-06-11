# CLAUDE.md

微信 iLink 桥接层——负责 Goal Chat Agent 与微信之间的消息收发。

## 文件职责

```
wechat_poller.py  — 长轮询循环：拉消息 → 转发到 /chat → 回传回复；消息缓冲与 debounce
ilink_client.py   — 原始 iLink HTTP 调用（QR 码、轮询、发送消息等 7 个端点）
```

## 数据流

```
微信用户 → iLink 服务器 → wechat_poller（长轮询 GET /updates）
  → HTTP POST /chat（Goal 内部 API）
  → ChatAgent 处理
  → wechat_sender 回调 → iLink POST /send → 微信用户
```

## wechat_poller 关键行为

- 在 FastAPI lifespan 中作为后台任务启动/停止
- 消息缓冲：buffer_debounce_seconds（默认 2s）内累积消息，buffer_max_wait_seconds（默认 8s）强制刷新
- 发送延迟：通过 send_delay 配置模拟真人打字节奏
- 单进程设计，不支持多副本（有 TODO 注释说明需要分布式锁）

## ilink_client 端点

| 端点 | 用途 |
|------|------|
| `/qrcode/create` | 创建 QR 码登录 |
| `/qrcode/status` | 轮询 QR 码扫描状态 |
| `/updates` | 长轮询拉取消息 |
| `/send` | 发送消息 |
| `/config/get` | 获取 bot 配置 |
| `/callback/register` | 注册回调 |

## 注意事项

- token 来自 QR 部署流程，持久化到 runtime_settings.json
- bridge 不直接解析消息内容，只做转发
- 若 chat_auth_enabled=true，bridge 必须持有相同的 CHAT_API_KEY
