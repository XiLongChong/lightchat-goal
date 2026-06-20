# Phone Context Phase 1 — Router 缓存型上下文工具

日期：2026-06-20

## 设计理念

Phase 1 是 **cached snapshot**，不是 realtime request。App 在关键生命周期事件
（切前台、轮询 tick、联系人或聊天切换、后台同步、手动 debug）上传最近一次手机上下文
快照到 Router。Agent 通过统一 tool 读取 Router 缓存的最新快照。

## API

### POST /api/phone-context/snapshots

上传手机上下文快照。

**Request:**
```json
{
  "schemaVersion": 1,
  "snapshotId": "uuid",
  "collectedAtMs": 1781950000000,
  "source": "android-app",
  "reason": "foreground_start",
  "contexts": {
    "device_status": {
      "platform": "android",
      "localTime": "2026-06-20T21:30:00+08:00",
      "timezoneId": "Asia/Shanghai",
      "batteryPercent": 82,
      "isCharging": true,
      "powerSource": "usb",
      "networkType": "wifi",
      "isOnline": true
    },
    "app_status": {
      "appState": "foreground",
      "activeContactId": "contact-id-or-null",
      "notificationsEnabled": true
    }
  }
}
```

**Validation:**
- `schemaVersion` 必须为 1
- `snapshotId` 非空字符串
- `collectedAtMs` 正整数
- `source` 必须是 `"android-app"`
- `reason`: `foreground_start` | `foreground_tick` | `active_chat_changed` | `background_sync` | `manual_debug`
- `contexts` 只能包含 `device_status` / `app_status`
- auth-first（401 先于 body 解析）

**Response ok:**
```json
{
  "ok": true,
  "snapshotId": "...",
  "acceptedTypes": ["device_status"],
  "storedAtMs": 1781950000100
}
```

### GET /api/phone-context/latest

读取最近一次 phone context 快照。

**Query params:**
- `types` — comma-separated context types（默认 `device_status,app_status`）
- `maxAgeSeconds` — 最大允许年龄秒数（0–86400，默认 300）

**Response ok:**
```json
{
  "schemaVersion": 1,
  "status": "ok",
  "snapshotId": "...",
  "collectedAtMs": 1781950000000,
  "ageSeconds": 12,
  "contexts": {
    "device_status": { "status": "ok", "data": { ... } },
    "app_status":    { "status": "ok", "data": { ... } }
  },
  "missing": []
}
```

**Response unavailable（快照太旧或无快照）:**
```json
{
  "schemaVersion": 1,
  "status": "unavailable",
  "reason": "no recent phone context snapshot",
  "contexts": {},
  "missing": ["device_status", "app_status"]
}
```

## 存储

- 表：`phone_context_snapshots`
  - `snapshot_id TEXT PRIMARY KEY`
  - `collected_at_ms INTEGER NOT NULL`
  - `received_at_ms INTEGER NOT NULL`
  - `payload_json TEXT NOT NULL` — 完整原始请求
  - `stored_at REAL NOT NULL`
- 只读最新一条（`ORDER BY received_at_ms DESC LIMIT 1`）
- 写入时自动 prune 超过 50 条

## MCP Tool

`aspocket_get_phone_context`（Claude Code channel）：

```json
{
  "name": "aspocket_get_phone_context",
  "inputSchema": {
    "properties": {
      "types": { "type": "array", "items": { "enum": ["device_status", "app_status"] } },
      "maxAgeSeconds": { "type": "number" }
    }
  }
}
```

- 调用 Router `GET /api/phone-context/latest`
- 返回 Raw JSON 字符串
- 不需要 channel 直接采集手机数据

## 不做（Phase 2+）

- 实时请求队列
- 定位（GPS）
- 日历
- 通知正文
- 媒体播放状态
- 剪贴板
- 每个 agent 单独的接口

## 涉及文件

| 文件 | 改动 |
|------|------|
| `main.py` | POST/GET phone context endpoints + validation |
| `memory/store.py` | `phone_context_snapshots` table + insert/get 方法 |
| `tools/aspocket_channel/channel.js` | `aspocket_get_phone_context` tool + handler |
| `tools/aspocket_channel/helpers.js` | `phoneContextUrl` helper |
| `tools/aspocket_channel/test/helpers.test.js` | +3 JS tests |
| `tests/test_app_adapter.py` | +10 Python tests |
