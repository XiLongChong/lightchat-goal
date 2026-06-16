# 工具与调度运行修复记录（2026-06-16）

## 背景

今天按 `docs/doing` 中 4 个方案的实施结果做复核后，发现虽然工具定义和测试已经新增，但运行链路仍有多处没有真正接通：

- 新工具可见性和执行器注册不一致；
- `web_search` 只暴露搜索，缺少 `web_fetch`，且 Tavily 配置未明确；
- reminder 只创建数据，没有可靠投递闭环；
- proactive 仍有旧的伪 `/chat` 自调用/内存定时器思路残留；
- `proactive_speak` 与 `ChatAgent` source 校验不一致；
- provider 运行时 `import main` 会在 `python main.py` 模式下创建第二份全局对象，覆盖已启动 scheduler。

## 已完成改动

### 1. 工具注册和暴露

- `agent/tool_registry.py`
  - 新增 `web_fetch`。
  - 新增 `schedule_next_proactive`。
  - `is_action()`、`is_external_tool()` 改为基于 registry 判断。
- `agent/tools.py`
  - 统一执行器注册表。
  - 新增 `ToolExecutionContext`，把 `user_id`、`source`、当前 `TurnScratchpad` 传入工具。
  - 接入 `web_search`、`web_fetch`、Microsoft To Do、reminder、proactive scheduling 工具。
- `agent/chat_agent.py`、`runtime/chat_runtime.py`、`agent/tool_exposure_policy.py`
  - 移除旧的硬编码工具白名单。
  - 允许 registry 中的工具统一暴露。

### 2. 网页搜索

- `agent/search_provider.py`
  - 使用 Tavily REST API 实现 `web_search` 和 `web_fetch`。
  - 增加搜索缓存、超时、错误码、URL 校验和外部内容不可信提示。
  - 改为从 `agent.tools.TOOL_RUNTIME_CONFIG` 读取配置，避免运行时 `import main`。
- `config.yaml`
  - 新增 `tavily` 配置段。
- `.env.example`
  - 标注 `TAVILY_API_KEY`。

运行规则：

```env
TAVILY_API_KEY=your_tavily_api_key_here
```

当前不是 Apify provider；设置 `APIFY_API_KEY` 不会让 `web_search` 生效。

### 3. Microsoft To Do

- `agent/todo_provider.py`
  - 使用 MSAL device code flow 和 Graph API。
  - 改为从 `TOOL_RUNTIME_CONFIG` 读取配置，避免重复导入 `main`。
- `requirements.txt`
  - 增加 `msal`、`msal-extensions`。

### 4. 定时提醒

- 新增 `runtime/task_scheduler.py`
  - 使用 APScheduler `AsyncIOScheduler` + SQLite `scheduled_tasks`。
  - 支持 `user_reminder` 创建、恢复、触发、发送、失败/未知状态记录。
- `memory/store.py`
  - 新增 `scheduled_tasks` 表和相关 CRUD/CAS 方法。
  - reminder 发送成功后以 `reminder_due` turn 写回历史。
- `agent/tools.py`
  - `create_reminder` 优先走 `TaskScheduler`。
  - 回显时间改为显示原始本地时间，并附带 UTC，避免把 `Z` 时间误认为本地凌晨。

示例：

```text
Time: 2026-06-16T10:34:28+08:00 (2026-06-16T02:34:28Z UTC)
```

### 5. 主动消息 / 冷场检查

- `runtime/task_scheduler.py`
  - 支持 `proactive_speak` 持久化任务。
  - 支持首次 proactive 任务、取消用户旧 proactive 任务、配额统计、下一任务创建。
  - `schedule_next_proactive` 只记录候选间隔；至少一条 proactive 消息 confirmed delivered 后，后端才提交下一任务。
  - 首次 proactive 使用 `first_check_delay_seconds`，不再被频率档最小值裁剪。
- `memory/store.py`
  - 新增 `proactive_quota` 表。
  - 新增 `commit_proactive_success()`，在一个事务中完成当前任务 fired、配额 +1、下一任务插入。
- `main.py`
  - proactive 背景触发改为进程内 callback，不再伪造 HTTP `/chat` 请求。
  - 停用旧 `ProactiveService` 启动路径，统一由持久化 `TaskScheduler` 负责。
  - 普通用户消息成功后取消旧 proactive 任务并创建首次 proactive 任务。
- `agent/chat_agent.py`
  - 接受 `source="proactive_speak"`，修复 proactive 到期后被入口拒绝的问题。

## 关键运行问题定位

### 网页搜索 `not_configured`

日志：

```text
not_configured: TAVILY_API_KEY is not set
```

结论：这是配置问题，不是模型调用失败。后续 `model_attempt result=success` 只表示模型请求成功，不代表工具成功。

### 冷场检查 2 分钟未触发

定位到两个问题：

1. `proactive_speak` source 不被 `ChatAgent` 接受；
2. `first_check_delay_seconds=120` 被频率档 `[1800, 7200]` 裁剪为 30 分钟。

两者均已修复。

### 提醒创建后未触发

本地 DB 中看到提醒仍为 `scheduled`，但触发时间已过。日志同时出现：

```text
Adding job tentatively -- it will be properly scheduled when the scheduler starts
```

结论：创建提醒时用到了第二份未启动的 scheduler。根因是 provider 在运行时 `import main`，导致 `python main.py` 模式下主模块被重复加载。已移除业务模块中的 `import main`。

## 验证结果

全量测试：

```text
250 passed
```

静态验证：

- `agent/`、`runtime/`、`memory/`、`bridge/` 下无业务模块 `import main`。
- `proactive_speak` 已接入 `ChatAgent`。
- `first_check_delay_seconds` 已用于首次 proactive 任务。
- `git diff --check` 无空白错误，仅 Windows 行尾提示。

## 注意事项

- 这些修复需要重启服务后生效。
- 现有数据库里已经过期但仍是 `scheduled` 的 reminder，重启后会被恢复并立即触发；不想收到迟到提醒时，应先取消对应 reminder。
- `web_search` 当前 provider 是 Tavily，不是 Apify。
