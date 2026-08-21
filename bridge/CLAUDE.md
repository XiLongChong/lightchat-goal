# CLAUDE.md

微信 iLink 桥接层——负责 Goal Chat Agent 与微信之间的消息收发。

## 文件职责

```
wechat_poller.py  — 长轮询循环：拉消息 → 转发到 /chat → 回传回复；消息缓冲与 debounce；v1.2 新增 per-user inbound seq 和输入边界字段
ilink_client.py   — 原始 iLink HTTP 调用（QR 码、轮询、发送消息等端点）
sender.py         — 消息批量发送器：send_batch（带投递追踪）+ send_admin_batch（管理端手动发送）
qr_utils.py       — QR 码渲染工具：SVG/PNG 生成、payload 标准化（纯函数，无 FastAPI 依赖）
```

## 数据流

```
微信用户 → iLink 服务器 → wechat_poller（长轮询 GET /updates）
  → HTTP POST /chat（Goal 内部 API，携带 input_message_ids / input_first_seq / input_latest_seq / input_cutoff_time）
  → ChatAgent 处理
  → bridge/sender 回调 → iLink POST /send → 微信用户
```

## wechat_poller 关键行为

- 在 FastAPI lifespan 中作为后台任务启动/停止
- 消息缓冲：buffer_debounce_seconds（默认 2s）内累积消息，buffer_max_wait_seconds（默认 8s）强制刷新
- 发送延迟：通过 send_delay 配置模拟真人打字节奏
- **v1.2 新增**：per-user `_inbound_seq` 字典，为每条入站消息分配递增序号
- **v1.2 新增**：刷新时携带完整的输入边界字段（`input_message_ids`, `input_first_seq`, `input_latest_seq`, `input_cutoff_time`），用于中断检测和幂等
- 单进程设计，不支持多副本（有 TODO 注释说明需要分布式锁）

## ilink_client 端点

| 端点 | 用途 |
|------|------|
| `/ilink/bot/get_bot_qrcode` | 创建 QR 码登录 |
| `/ilink/bot/get_qrcode_status` | 轮询 QR 码扫描状态 |
| `/ilink/bot/getupdates` | 长轮询拉取消息（35s 超时） |
| `/ilink/bot/sendmessage` | 发送消息 |
| `/ilink/bot/getconfig` | 获取 bot 配置 |

## sender.py（WeChatSender）

两种发送模式：
- `send_batch` — 主运行时路径（chat turns、proactive、reminders）。追踪每条消息的投递状态，强制执行每用户发送锁和发送间隔。
- `send_admin_batch` — 管理端手动发送路径。调用方已做 guard，不追踪状态。

配置通过 `config_getter` 和 `bridge_getter` 注入，反映 main.py 中的 start/stop/redeploy 周期。

## 注意事项

- token 来自 QR 部署流程，持久化到 runtime_settings.json
- bridge 不直接解析消息内容，只做转发
- 若 chat_auth_enabled=true，bridge 必须持有相同的 CHAT_API_KEY
- QR 码部署流程在管理后台完成：`/wechat/deploy/start` → 轮询 `/wechat/deploy/status` → confirmed 后自动启动 bridge
