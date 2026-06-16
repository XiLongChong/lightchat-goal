# Microsoft To Do 集成方案（2026-06-16 修订 v4）

**日期**: 2026-06-16（初版），2026-06-16（修订 v2, v3, v4）
**状态**: ✅ 已实施（2026-06-16）。设备码认证状态机有覆盖 bug，已于同日修复，见 [20260616-fix-todo-device-flow-overwrite.md](20260616-fix-todo-device-flow-overwrite.md)
**关联**: [[20260615-web-search-design]], [[20260616-scheduled-reminders-design]]

## 目标

让 Goal Chat Agent 接入用户的 Microsoft To Do，帮用户管理每日计划——查看列表、查看任务、创建任务、完成任务。v1 不提供删除。

## 决策记录

1. **v2 修订**：移除 `todo_delete_task` 工具；移除 `client_secret` 改用纯 Device Code Flow（PublicClientApplication）；使用 `SerializableTokenCache` 持久化 MSAL cache；补全分页、429 重试、401 静默刷新；明确单账号绑定。
2. **v3 修订**：删除 Device Code Flow 需要 `http://localhost` redirect URI 的错误说明（Device Code Flow 不需要 redirect URI）。
3. **v3 修订**：采用 `msal-extensions` 的 `PersistedTokenCache`，使用 Windows DPAPI 加密持久化、内置文件锁和自动重载，**不手写** `threading.Lock` + 原子写入的 `MSTokenCache` 类。
4. **v3 修订**：分页跟随服务返回的完整 `@odata.nextLink`，**禁止**自行构造 `$skip`。
5. **v3 修订**：401 刷新必须校验返回有效 access token 并实际用于重试请求；v4 明确不以 token 字符串是否变化判断刷新成败。
6. **v3 修订**：Retry-After 超过 30 秒时不阻塞用户 turn，返回稳定 `rate_limited` 结果给模型。
7. **v3 修订**：删除无关的 `max_extract_chars` 配置项（那是 Web Search 的配置，不属于 MS To Do）。
8. **v3 修订**：保留 `Asia/Shanghai` 作为时区值；它是 `dateTimeTimeZone` 明确支持的附加 IANA 值，不是 Windows 时区名称。
9. **v2 修订**：`todo_complete_task` 禁止模糊匹配。只有唯一精确匹配才允许完成；多个候选时返回候选列表，让模型向用户澄清。
10. **v2 修订**：`todo_create_task` 只在用户明确要求创建时执行。模型不得"主动帮用户创建任务"。
11. **始终披露**：todo 工具始终在工具列表中。未认证时返回稳定 `not_connected` 结果。
12. **统一工具注册**：工具注册以 `agent/tool_registry.py` 为单一权威来源；执行路由通过按工具名注册的 dispatcher。
13. **v4 修订**：修正 `msal-extensions` API：Windows 使用 `build_encrypted_persistence()`，`PersistedTokenCache` 的第二个参数是 lock file 路径，不是 `CrossPlatLock` 对象。MSAL 的同步 token 获取在异步服务中通过 `asyncio.to_thread()` 执行。
14. **v4 修订**：401 强制刷新只要求返回有效 access token 并实际用于单次重试，不要求 token 字符串必须变化。分页 nextLink 通过统一认证请求入口访问，并校验 HTTPS 与 `graph.microsoft.com` host。
15. **v4 修订**：`todo_complete_task` 增加可选 `task_id`。按标题完成时必须遍历目标列表全部未完成任务，只有唯一精确匹配才写入；多候选返回 task_id，用户澄清后按 task_id 精确完成，避免再次陷入同名歧义。

## 现成方案调研

### 候选 1: `jordanburke/microsoft-todo-mcp-server`

- TypeScript (Node.js)，专做 MS To Do，13 个 MCP tools
- 成熟度：36 commits, npm 发布, CI 通过

### 候选 2: `hvkshetry/office-365-mcp-server`

- JavaScript (Node.js)，全 Office 365，标注"not yet production-ready"

### 结论：不采用外部 MCP server

- 项目是纯 Python，引入 Node.js 子进程增加运维复杂度
- MS Graph To Do API 简单（~5 个 endpoint），不需要 MCP 中间层
- Python 原生实现可完全控制错误处理、格式化、token 管理

## 认证方案：MSAL Device Code Flow + PersistedTokenCache

### 选型

- 使用 `msal.PublicClientApplication`（不依赖 `client_secret`）
- Azure 应用注册需要启用 **"Allow public client flows"**
- 使用 `msal-extensions.PersistedTokenCache`：
  - Windows 上使用 DPAPI 加密持久化
  - 内置文件锁和自动重载
  - **不手写** `threading.Lock` + 原子写入的自定义 cache 类
- 依赖：`msal` + `msal-extensions`

### 认证流程

```
首次配置:
1. 用户在 Azure Portal 注册应用
   - 账户类型: "任何组织目录中的账户和个人 Microsoft 账户"
   - 启用 "Allow public client flows": 是
   - API 权限: Tasks.ReadWrite（委托权限）
   - 注意: Device Code Flow 不需要配置 redirect URI
2. 获取 client_id（不需要 client_secret）
3. 填入 config.yaml
4. 运行: python scripts/auth_microsoft_todo.py
5. 脚本输出: "打开 https://microsoft.com/devicelogin 输入代码 XXXXXXXX"
6. 用户在浏览器输入代码 → 登录 Microsoft 账户 → 授权
7. MSAL 自动获取 access_token + refresh_token → PersistedTokenCache 持久化
8. refresh token 在未过期或未被撤销时由 MSAL 自动使用；失效后返回稳定 `not_connected` 并要求重新认证

日常运行:
- 每次 API 调用前，MSAL acquire_token_silent() 自动检查并刷新
- 401 时静默强制刷新，校验返回有效 access token，使用新结果单次重试
- token cache 由 PersistedTokenCache 自动维护
```

### Token Cache 实现（使用 msal-extensions）

```python
from msal_extensions import (
    PersistedTokenCache,
    build_encrypted_persistence,
)

def build_token_cache(cache_path: str) -> PersistedTokenCache:
    # Windows 下由 DPAPI 加密；PersistedTokenCache 内部使用 CrossPlatLock。
    persistence = build_encrypted_persistence(cache_path)
    return PersistedTokenCache(
        persistence,
        lock_location=cache_path + ".lockfile",
    )
```

`PersistedTokenCache` 会在 cache `search`/`modify` 时自动重载和持久化。不需要手写 `MSTokenCache`、`threading.Lock`、临时文件 rename，也不要直接手动调用 `serialize()` 后覆盖 cache 文件。

## 工具定义

### todo_list_lists

```
name: todo_list_lists
description: >
  Get all Microsoft To Do task lists for the connected account.
category: fact_tool
side_effect: none
terminal: false
args: none
```

### todo_list_tasks

```
name: todo_list_tasks
description: >
  Get tasks from a Microsoft To Do list. Returns incomplete tasks
  by default. Use include_completed=true to also see completed ones.
category: fact_tool
side_effect: none
terminal: false
args:
  list_name (string, optional): Filter by list name. If omitted, uses default list.
  include_completed (boolean, optional, default false): Include completed tasks
  max_results (integer, optional, default 20, max 50): Maximum tasks to return
```

### todo_create_task

```
name: todo_create_task
description: >
  Create a new task in Microsoft To Do. Only call when the user
  explicitly asks to create a task. Do not create tasks proactively.
category: action_tool
side_effect: high
terminal: false
args:
  title (string, required): Task title (max 255 chars)
  list_name (string, optional): Target list. If omitted, uses default list.
  due_date (string, optional): Due date in YYYY-MM-DD format
  reminder_date (string, optional): Reminder datetime in YYYY-MM-DD HH:MM format
  note (string, optional): Additional notes (max 1000 chars)
  importance (string, optional): "low" | "normal" | "high" (default "normal")
```

### todo_complete_task

```
name: todo_complete_task
description: >
  Mark a task as completed. Prefer task_id when it was returned by a
  previous candidate list. Without task_id, this only works when there
  is exactly one exact title match in the target list. Multiple matches
  return candidate task IDs and do not complete anything.
category: action_tool
side_effect: high
terminal: false
args:
  task_title (string, optional): Exact task title to complete. Required
    when task_id is omitted.
  task_id (string, optional): Exact Graph task ID selected from a prior
    list/candidate result. When provided, no title matching is performed.
  list_name (string, optional): List to search in. If omitted, uses default list.
```

后端要求 `task_title` 与 `task_id` 至少提供一个；两者同时提供时以 `task_id` 为准，并验证该 task 属于目标 list 且尚未完成。

## 实现架构

```
agent/todo_provider.py         ← 新文件：MSTodoProvider
  ├── __init__(client_id, tenant_id, cache_path, ...)
  ├── authenticate()           ← Device Code Flow（首次/重认证）
  ├── acquire_token()          ← acquire_token_silent() + 401 刷新（校验新 token）+ 单次重试
  ├── get_lists()              ← GET /me/todo/lists
  ├── get_default_list()       ← 通过 wellknownListName=defaultList 查找
  ├── get_tasks(list_id, ...)  ← GET /me/todo/lists/{id}/tasks（跟随 @odata.nextLink 分页）
  ├── create_task(...)         ← POST /me/todo/lists/{id}/tasks
  ├── complete_task(...)       ← PATCH /me/todo/lists/{id}/tasks/{id}
  ├── _find_list_by_name(name) ← 列表名模糊匹配（候选 > 1 返回候选）
  ├── _find_task_by_title(...) ← 遍历全部未完成任务后精确匹配（候选 > 1 返回 task_id）
  └── _handle_response()       ← 统一错误处理 + 429 Retry-After

agent/tool_registry.py         ← 注册 4 个 todo_* ToolDef
agent/tool_exposure_policy.py  ← 从 TOOL_REGISTRY.iter_defs() 派生
agent/tools.py                 ← 添加 4 个 todo executor 到 dispatcher dict
config.yaml                    ← 新增 microsoft_todo 配置段
scripts/auth_microsoft_todo.py ← 新建：首次认证脚本（使用 PersistedTokenCache）
requirements.txt               ← 新增 msal, msal-extensions
```

## MSTodoProvider 关键实现细节

### 默认列表查找

```python
async def get_default_list(self) -> dict:
    """获取全部列表后按 wellknownListName 查找默认任务列表。"""
    lists = await self.get_lists()
    matches = [
        item for item in lists
        if item.get("wellknownListName") == "defaultList"
    ]
    if len(matches) == 1:
        return matches[0]
    # 不回退到任意第一个列表，避免把任务写入非预期列表。
    raise TodoError("default_list_not_found", "Default task list was not found")
```

### 日期格式

```python
# dueDateTime 和 reminderDateTime 必须使用 Graph dateTimeTimeZone 格式
# Asia/Shanghai 是 dateTimeTimeZone 明确支持的附加 IANA 时区值，
# 不是 Windows 时区名称。
{
    "dueDateTime": {
        "dateTime": "2026-06-20T00:00:00",
        "timeZone": "Asia/Shanghai"
    },
    "reminderDateTime": {
        "dateTime": "2026-06-20T09:00:00",
        "timeZone": "Asia/Shanghai"
    },
    "isReminderOn": True  # 设置 reminder 时同步设置为 true
}
```

### 分页（跟随 @odata.nextLink）

`get_lists()` 和 `get_tasks()` 都通过同一个 `_paginate_graph_collection()` helper 跟随 `@odata.nextLink`；默认列表查找不能只检查第一页。

```python
async def get_tasks(self, list_id: str, max_results: int = 20):
    """分页获取任务，跟随服务返回的 @odata.nextLink。
    禁止自行构造 $skip 参数。"""
    all_tasks = []
    url = f"/me/todo/lists/{list_id}/tasks"
    params = {"$top": min(max_results, 50), "$orderby": "createdDateTime desc"}
    seen_urls = set()

    while url is not None and len(all_tasks) < max_results:
        if url in seen_urls:
            raise TodoError("provider_error", "Repeated @odata.nextLink")
        seen_urls.add(url)

        # 统一入口负责 Authorization、401/429、超时和错误映射。
        # 对完整 nextLink 校验 scheme=https 且 host=graph.microsoft.com。
        data = await self._request_json("GET", url, params=params)
        tasks = data.get("value", [])
        all_tasks.extend(tasks)
        url = data.get("@odata.nextLink")
        params = None  # nextLink 自带所有参数

    return all_tasks[:max_results]
```

`todo_list_tasks` 遵守用户给出的 `max_results`。但 `todo_complete_task` 的内部标题匹配不能复用这个展示上限，必须分页遍历全部未完成任务：
- 标题比较采用 Unicode NFC + 首尾空白清理后的大小写敏感精确匹配
- 设置防御性 `max_pages`/`max_scanned_tasks` 上限；达到上限时返回 `scan_limit_exceeded`，禁止执行写操作
- 多个精确同名候选返回 `task_id`、标题、dueDateTime 和简短列表信息
- 后续按 `task_id` 完成时，先 GET/校验目标 task，不能信任模型传入的任意 ID

### 401 静默刷新 + 校验 + 单次重试

```python
async def _request(self, method: str, path: str, **kwargs):
    try:
        return await self._do_request(method, path, **kwargs)
    except TodoError as e:
        if e.code == "unauthorized":
            # 静默刷新 token
            result = await asyncio.to_thread(
                self._app.acquire_token_silent,
                scopes=["Tasks.ReadWrite"],
                account=self._account,
                force_refresh=True,
            )
            # 同一 token 字符串并不能证明刷新失败；只校验结果和 access_token。
            if not result or "access_token" not in result:
                raise TodoError("not_connected",
                    "Authentication expired. Re-authenticate.")
            self._current_token = result["access_token"]
            # 单次重试
            return await self._do_request(
                method,
                path,
                access_token=self._current_token,
                **kwargs,
            )
        raise
```

### 429 Retry-After（30 秒上限）

```python
async def _do_request(self, method, path, **kwargs):
    resp = await self._client.request(method, url, **kwargs)
    if resp.status_code == 429:
        retry_after = int(resp.headers.get("Retry-After", 5))
        if retry_after > 30:
            # 不阻塞用户 turn，返回稳定错误
            logger.warning("MS Graph 429 with Retry-After=%ds, returning rate_limited", retry_after)
            raise TodoError("rate_limited",
                f"Microsoft To Do is busy. Try again in {retry_after}s.")
        logger.warning("MS Graph 429, retry after %ds", retry_after)
        await asyncio.sleep(retry_after)
        resp = await self._client.request(method, url, **kwargs)
    return resp.json()
```

## 错误处理

| 错误场景 | 返回给模型 | 日志 |
|----------|-----------|------|
| 未认证 | `not_connected: Run auth_microsoft_todo.py first` | WARNING |
| Token 过期且刷新失败 | `not_connected: Authentication expired. Re-authenticate.` | ERROR |
| Token 刷新没有返回 access_token | `not_connected: Authentication expired. Re-authenticate.` | ERROR |
| 列表不存在 | `list_not_found: No list matching "xxx"` | INFO |
| 完成任务有多个候选 | `ambiguous: Found N matching tasks. Ask user to clarify.` + 候选列表 | INFO |
| 完成任务无匹配 | `not_found: No task matching "xxx"` | INFO |
| 创建任务成功 | `created: "xxx" (id: xxx)` | INFO |
| 完成任务成功 | `completed: "xxx"` | INFO |
| API 429（≤30s） | 内部自动重试 | WARNING + Retry-After |
| API 429（>30s） | `rate_limited: Microsoft To Do is busy. Try again in Xs.` | WARNING |
| API 401 | 内部静默刷新 + 校验 + 单次重试 | WARNING |
| API 500 | `provider_error: Microsoft To Do is temporarily unavailable` | ERROR + 截断、脱敏后的错误摘要 |
| 网络超时 | `provider_error: request timed out` | ERROR |

所有错误码保持稳定，不向模型返回原始 Graph 错误全文。

## 配置

### config.yaml

```yaml
microsoft_todo:
  enabled: false
  client_id: ""            # Azure 应用 ID
  tenant_id: "common"      # 同时支持个人和组织账号；只允许个人账号时改为 consumers
  token_cache_path: "data/ms_todo_cache.bin"
  timeout_seconds: 15
```

不需要 `client_secret`。不需要 `max_extract_chars`（那是 Web Search 的配置）。

### .env

无需新增。`client_id` 直接在 config.yaml 中配置。

### requirements.txt

```
msal>=1.31,<2
msal-extensions>=1.2,<2
```

## 用户接入步骤

1. Azure Portal 注册应用（见 `docs/demo/azure-app-registration-guide.md`）
   - 不需要配置 redirect URI（Device Code Flow 不使用回调）
2. 填入 `client_id` 到 config.yaml
3. 运行 `python scripts/auth_microsoft_todo.py`
4. 浏览器输入设备代码完成授权
5. 在 admin UI 或 runtime_settings 设置 `microsoft_todo.enabled: true`
6. 热加载配置（或重启服务）

## admin 状态展示

在 admin UI 的 settings 面板中展示：
- 连接状态：已连接 / 未认证
- 已连接账号（从 token cache 中提取）
- 最后认证时间
- 默认列表名称

## 涉及改动清单

| 文件 | 改动 |
|------|------|
| `agent/todo_provider.py` | **新建** — MSTodoProvider（PersistedTokenCache + @odata.nextLink 分页 + 429/401 处理） |
| `agent/tool_registry.py` | 注册 `todo_list_lists` + `todo_list_tasks` + `todo_create_task` + `todo_complete_task` |
| `agent/tool_exposure_policy.py` | 从 TOOL_REGISTRY.iter_defs() 派生（自动包含 todo 工具） |
| `agent/tools.py` | 添加 4 个 todo executor 到 dispatcher dict |
| `config.yaml` | 新增 `microsoft_todo` 配置段（不含 max_extract_chars） |
| `scripts/auth_microsoft_todo.py` | **新建** — Device Code Flow 认证脚本（使用 PersistedTokenCache） |
| `main.py` | `configure_tools()` 初始化 todo provider；`public_settings`/`apply_runtime_settings` 加 todo 段；admin 状态接口 |
| `docs/demo/azure-app-registration-guide.md` | **新建** — Azure 注册图文指南（不含 redirect URI 错误说明） |
| `requirements.txt` | 新增 `msal`, `msal-extensions` |
| `static/admin.html` 或 `static/admin.js` | admin 中加入 todo 状态展示和配置 |
| `tests/` | 新增 `test_todo_provider.py` |

## 测试矩阵

| # | 测试场景 | 预期 |
|---|---------|------|
| 1 | 未认证时调用 todo_list_lists | 返回 `not_connected` |
| 2 | Device Code Flow 认证成功 | PersistedTokenCache 文件创建 |
| 3 | acquire_token_silent 正常获取 | 返回有效 token |
| 4 | todo_list_lists 返回列表 | JSON 含 displayName + id |
| 5 | todo_list_tasks 默认列表 | 返回未完成任务 |
| 6 | todo_list_tasks include_completed=true | 含已完成任务 |
| 7 | todo_create_task 基本创建 | 返回 created 确认 |
| 8 | todo_create_task 含 due_date + reminder | Graph dateTimeTimeZone 格式正确（Asia/Shanghai） |
| 9 | todo_complete_task 唯一精确匹配 | 标记完成 |
| 10 | todo_complete_task 多个同名任务 | 返回 ambiguous + 候选列表，不执行写操作 |
| 11 | todo_complete_task 无匹配 | 返回 not_found |
| 12 | 401 静默刷新 → 新 token 有效 → 重试成功 | 第二次请求成功 |
| 13 | 401 刷新返回相同 token 但 token 有效 | 使用该 token 单次重试，不误判失败 |
| 14 | 429 Retry-After ≤ 30s | 延迟后重试成功 |
| 15 | 429 Retry-After > 30s | 返回 `rate_limited`，不阻塞 |
| 16 | 分页跟随 @odata.nextLink | 不自行构造 $skip，跟随服务返回的 nextLink |
| 17 | PersistedTokenCache 自动持久化 | 重启后 token 可用 |
| 18 | 列表名模糊匹配多个候选 | 返回候选列表，不随意选择 |
| 19 | 网络超时 | 返回 `provider_error` |
| 20 | PersistedTokenCache 文件锁 | 多进程不损坏 cache |
| 21 | PersistedTokenCache 构造参数 | 第二参数为 lock file 路径，不传 CrossPlatLock 实例 |
| 22 | nextLink host 校验 | 非 HTTPS 或非 graph.microsoft.com 的完整 URL 被拒绝 |
| 23 | 异步服务获取 token | MSAL 同步调用通过 asyncio.to_thread，不阻塞事件循环 |
| 24 | 默认列表无法识别 | 返回 default_list_not_found，不回退到任意第一个列表 |
| 25 | common tenant 账号类型 | 个人账号和组织账号均可完成 Device Code Flow |
| 26 | 同名候选澄清后按 task_id 完成 | 只完成用户选中的一项 |
| 27 | 同名任务位于第 2 页以后 | 完整分页发现歧义，不误完成第 1 页任务 |
| 28 | 标题扫描达到安全上限 | 返回 scan_limit_exceeded，不执行 PATCH |
| 29 | task_id 不属于目标列表 | 返回 not_found/invalid_target，不执行 PATCH |

## 功能范围

- v1：单账号绑定，不做多用户 Microsoft 账号体系
- v1：不实现 `todo_delete_task`
- v1：不实现 checklist item（子任务）管理
- v1：不实现任务列表的创建/删除/更新

## 依赖顺序

本方案是 **依赖序列的第 3 步**（在统一工具注册 + web search 之后）。

## 已否决方案

1. **自建 SQLite Todo** — 用户明确要求接入 Microsoft To Do
2. **外部 Node.js MCP server** — 增加运维复杂度
3. **Client Credentials Flow** — 个人账号不支持
4. **Office 365 MCP Server（全功能）** — 过度引入不需要的功能
5. **client_secret 认证** — v2 改为纯 Device Code Flow（PublicClientApplication）
6. **自维护 access_token/refresh_token JSON** — v2 改用 MSAL token cache；v3 改用 msal-extensions PersistedTokenCache
7. **手写 threading.Lock + 原子写入 cache** — v3 改用 msal-extensions 内置文件锁和 DPAPI
8. **按标题模糊匹配直接完成** — v2 禁止，必须唯一精确匹配
9. **模型可主动创建任务** — v2 限制仅在用户明确要求时创建
10. **自行构造 $skip 分页** — v3 改为跟随 @odata.nextLink
11. **Device Code Flow 需要 redirect URI** — v3 删除此错误说明
12. **Retry-After 任意时长都阻塞** — v3 超过 30s 返回 rate_limited
13. **配置含 max_extract_chars** — v3 删除（属于 Web Search 方案）

## 参考

- Microsoft Graph To Do API: https://learn.microsoft.com/en-us/graph/todo-concept-overview
- MSAL Python: https://learn.microsoft.com/en-us/entra/msal/python
- msal-extensions: https://github.com/AzureAD/microsoft-authentication-extensions-for-python
- Microsoft Graph paging: https://learn.microsoft.com/en-us/graph/paging
- dateTimeTimeZone: https://learn.microsoft.com/en-us/graph/api/resources/datetimetimezone
- Device Code Flow: https://github.com/Azure-Samples/ms-identity-python-devicecodeflow
- `jordanburke/microsoft-todo-mcp-server`: 参考 tool 设计
