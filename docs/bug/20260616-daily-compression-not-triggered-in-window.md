# 凌晨压缩窗口未触发每日压缩

## 现象

每日压缩窗口为 `Asia/Shanghai` 时间 03:56–04:25（共 29 分钟，见
`agent/compressor.py:128`）。实际运行中，多日观察该窗口内未触发压缩：

- `data/agent.log` 中窗口时间段没有出现 `compress: ` 开头的任何日志（`Starting daily compression …` / `compress: too few messages …` / `compress: no valid summary …` 均未出现）。
- `memory.db` 的 `summaries` 表对应日期没有新记录。
- `raw_events` 表对应日期没有 active=0 的批量软删除事件，提示压缩流程从未进入 commit 阶段。

需要后续再观测一次以确认日志缺失稳定复现。

## 证据

- `agent/compressor.py:285` `compress_conversation()` 与
  `agent/compressor.py:349` `maybe_compress_daily()` 是项目内仅有的两个压缩入口。
- 全仓搜索 `_run_compression` / `maybe_compress_daily` / `compress_conversation`
  的调用点只有两处：
  - `main.py:1525`，位于 `/chat` 路由的用户消息处理流程内（`force=False`）。
  - `main.py:1647`，位于 `/admin/dev/compress` 管理端手动接口（`force=True`）。
- `runtime/task_scheduler.py` 仅处理 `user_reminder` 与 `proactive_speak` 两类
  scheduled_tasks，不包含任何与压缩相关的 job 类型。
- `main.py` 的 `lifespan()`（`main.py:882`）启动时只执行
  `_startup_validate`、long-term memory 诊断、`_bridge.start()`、
  `task_scheduler.start()`，没有注册任何周期性压缩任务。
- `agent/compressor.py:279` `_in_compress_window()` 是窗口守卫而非触发器；它仅在
  上述 `/chat` 调用栈到达 `maybe_compress_daily()` 之后才会被检查。

## 已确认原因

每日压缩没有独立的时间触发器。压缩流程**只能搭车在用户向 `/chat` 发起的请求上**：
当用户在 03:56–04:25 之间没有任何聊天消息抵达，整个流程不会进入。

加重条件：

- 窗口位于深夜 4 点附近，绝大多数用户处于睡眠时段。
- 当前 `data/runtime_settings.json` 将 `proactive.enabled` 置为 `false`，主动消息
  也无法在窗口内反向触发任何 `/chat` 流量（详见同日另一份 bug 文档：
  [20260616-proactive-message-disabled-by-runtime-settings.md](20260616-proactive-message-disabled-by-runtime-settings.md)）。

两条触发路径同时被锁，导致窗口几乎永远等不到流量，压缩长期处于"想触发但没人来按"的状态。

## 影响

- 每日摘要长期缺位，`ConversationContext` 不会被压缩替换，长期记忆评审
  （`<memory_operations>`）也无法被审阅与提交。
- 历史 `raw_events` 持续累积、不被软删除，DB 体量随时间线性增长。
- 一旦后续触发一次压缩，将把跨多天的对话一次性塞入压缩 LLM 调用，可能撞上模型
  上下文上限或返回质量下降。
- 现有日志没有"窗口已到、未触发"的 INFO 行，运维侧无法直观观察到该问题，只能
  通过查 SQLite 反推。

## 期望行为

- 在每天 03:56–04:25 窗口内，至少为存在历史会话的活跃 user_id 触发一次压缩，
  无论用户当下是否发起 `/chat` 请求。
- 触发不依赖主动消息开关（`proactive.enabled`）的状态。
- 压缩入口与现有 `_run_compression(force=False)` 保持等价语义，仍然走
  `_in_compress_window` + `last_compressed_at` 双重幂等。
- 日志中应当能看到窗口被进入、跳过原因（消息不足 / 已压缩过 / LLM 失败）的明确
  记录，便于事后核对。

## 可行方案（仅记录，待决定）

1. 在 `runtime/task_scheduler.py` 中新增独立的 cron job：每天 03:56 起每隔
   N 分钟扫描一次活跃 user_id，调用与 `/chat` 流程相同的 `_run_compression`。
2. 或在 `lifespan()` 启动一个 asyncio task，循环 sleep 到窗口起点，对每个
   `bot_contact_state` 中已绑定的 user_id 执行一次 `_run_compression`。
3. 任意方案都需要复用 `last_compressed_at` 幂等判断，避免与用户在窗口内同时
   发起的 `/chat` 重复压缩。
4. 增加观测日志：窗口入口、user_id 选择来源、跳过原因、最终 status。
5. 增加测试：mock 当前时间到窗口内，断言不依赖 `/chat` 也能触发；mock 到窗口
   外，断言不会触发。

不建议把压缩与主动消息绑定，二者的开关意图不同。

## 状态

已确认，需再观测一次以记录稳定的窗口时段日志样本。严重程度建议为中：不影响
当下对话功能，但持续不压缩会导致 token 与上下文成本随时间累积，并使长期记忆
评审通道失效。
