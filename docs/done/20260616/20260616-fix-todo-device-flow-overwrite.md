# Microsoft To Do 设备码认证状态机修复（2026-06-16）

## 现象

用户在 Microsoft 页面完成设备码授权（页面显示"大功告成，已登录到 goal-todo-agent"）后，
再次调用 `todo_create_task` / `todo_list_lists` 等工具时，后端不使用已授权的 flow 换取 token，
而是生成一个全新的设备码，导致用户永远无法完成认证。

加剧问题：一次创建两个任务时，会并发生成两个不同的设备码，第二个覆盖第一个。

## 根因

### Bug 1：`acquire_token() → None` 后无条件调用 `initiate_device_flow()` 覆盖 pending flow

旧代码中每个 todo 工具执行器都遵循同一模式：

```python
token = await provider.acquire_token()   # authorization_pending → 返回 None
if token is None:
    return provider.initiate_device_flow()  # 无条件创建新 flow，覆盖旧的
```

用户在 Microsoft 页面输入设备码后，下次工具调用时 `acquire_token()` 返回
`authorization_pending`（MSAL 单次轮询结果），被映射为 `None`。
随后 `initiate_device_flow()` 创建全新 flow，**用户刚完成的授权对应的 device_code
被永久丢失**。

### Bug 2：无并发保护

多个 todo 工具并发调用时，每个都独立走 `acquire_token() → initiate_device_flow()`，
产生多个不同 device code，后者覆盖前者。

### Bug 3：`_device_flow_data` 丢失 `user_code`

旧代码只存储 `{device_code, interval, expires_at}`，丢弃了 MSAL flow dict 中的
`user_code` 和 `verification_uri`。即使想复用 pending flow，也无法重新展示设备码。

### 加深理解：MSAL 内部轮询循环

MSAL v1.36.0 的 `acquire_token_by_device_flow` 内部有一个阻塞轮询循环，
exit condition 为 `flow.get("expires_at", 0) < time.time()`。
旧代码传了截断的 dict（无 `expires_at`），意外使得 `0 < now()` → `True`，
MSAL 只做单次 HTTP 请求就返回——行为正确但属于巧合。
新代码**显式**在 flow 副本上设 `expires_at = 0` 来实现同样的单次轮询语义。

## 修复方案

### `agent/todo_provider.py` — 重写认证状态机

1. **存储完整 MSAL flow dict**（含 `user_code`、`verification_uri`），新增 `_flow_expires_at` 单独跟踪过期
2. **新增 `asyncio.Lock`** — 序列化所有认证操作，防止并发覆盖
3. **`initiate_device_flow()` 检查 pending flow** — 未过期时返回同一消息，不生成新码
4. **新增 `ensure_auth()` 方法** — 单一入口，三步认证：
   - 静默获取 token（`acquire_token_silent`）
   - 单次轮询 pending device flow（`acquire_token_by_device_flow` + `expires_at=0`）
   - 无 pending flow 时才创建新的
5. **`acquire_token()` 降级为纯静默** — 不再尝试 device flow 轮询，消除
   `acquire_token() → None → initiate_device_flow()` 的覆盖路径
6. **新增 `_format_device_flow_message()`** — 从完整 flow dict 格式化设备码消息
7. **新增 `auth_pending` 属性** — 检查是否有待完成的设备码流程

### `agent/tools.py` — 统一使用 `ensure_auth()`

1. 新增 `_ensure_todo_auth(provider)` helper，调用 `provider.ensure_auth()`
2. 全部 4 个 todo 工具执行器替换旧的 `acquire_token() + initiate_device_flow()` 模式

### 认证状态机正确行为

```
首次调用 todo_list_lists：
  → ensure_auth() → 无 token、无 pending flow
  → initiate_device_flow() → 生成设备码 ABCD-1234
  → 返回设备码消息给用户

用户在 Microsoft 页面输入 ABCD-1234 → 页面显示"大功告成"

再次调用 todo_list_lists：
  → ensure_auth() → 无 token、有 pending flow
  → acquire_token_by_device_flow(单次轮询) → 返回 access_token
  → 清除 pending flow，设置 token
  → 正常调用 API

并发调用 todo_create_task × 2：
  → asyncio.Lock 序列化
  → 第一个：ensure_auth() → 创建/复用 flow
  → 第二个：ensure_auth() → 发现已有 pending flow 或已有 token，复用
  → 不会生成第二个设备码
```

## 测试

新增 `TestDeviceFlowStateMachine` 类（9 个测试）：

| 测试 | 验证 |
|------|------|
| `test_initiate_device_flow_reuses_pending_flow` | 已有 pending flow 时不生成新码 |
| `test_initiate_device_flow_creates_new_when_expired` | 过期后才创建新码 |
| `test_ensure_auth_silent_token` | 有缓存 token 时直接返回 |
| `test_ensure_auth_pending_flow_returns_same_message` | 用户未完成授权时返回同一设备码 |
| `test_ensure_auth_completes_flow` | 用户完成授权后获取 token |
| `test_ensure_auth_creates_new_flow_when_none` | 无 token 无 flow 时创建新 flow |
| `test_ensure_auth_expired_flow_cleared` | 过期 flow 被清除后创建新的 |
| `test_ensure_auth_not_configured` | 未配置时返回 `(None, None)` |
| `test_concurrent_ensure_auth_same_flow` | 并发调用只生成 1 个 device code |

更新 `TestTodoToolExecutors` 中 2 个旧测试适配 `ensure_auth`，新增 2 个测试。

全量测试结果：**249 passed, 0 failed**。

## 涉及文件

| 文件 | 改动 |
|------|------|
| `agent/todo_provider.py` | 重写认证状态机（ensure_auth + lock + 复用 flow） |
| `agent/tools.py` | 4 个 todo executor 改用 `_ensure_todo_auth()` |
| `tests/test_todo_provider.py` | 新增 9 + 更新 2 个测试 |
