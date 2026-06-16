# 主动消息未触发：runtime_settings 覆盖关闭了 proactive

## 现象

机器人长期没有发出任何主动消息（proactive check-in）：

- `data/agent.log` 中没有 `ProactiveService started …` 之外的主动消息相关日志，
  也没有 `proactive trigger completed user=…` 这种由 `_handle_proactive_trigger`
  产生的回执行为。
- SQLite `scheduled_tasks` 表中没有 `task_type='proactive_speak'` 的活跃记录。
- 用户向 `/chat` 发完消息、turn 终态为 `END_REPLIED` 之后，未观察到 N 分钟后
  机器人主动开口的行为。

## 证据

- `config.yaml:144` 中 `proactive.enabled` 默认为 `true`：

  ```yaml
  proactive:
    enabled: true
    interval_minutes: 30
    ...
  ```

- 但 `data/runtime_settings.json` 中显式覆盖了该字段：

  ```json
  "proactive": {
    "enabled": false,
    ...
  }
  ```

- `main.py:140` 在启动时执行
  `_deep_update(cfg, runtime_settings)`，按照项目优先级
  （CLAUDE.md：`命令行/环境变量 > runtime_settings.json > config.yaml 默认值`），
  `runtime_settings.json` 的 `enabled: false` 会覆盖 `config.yaml` 的 `true`。
- `runtime/task_scheduler.py:207` `schedule_initial_proactive()` 起手即检查：

  ```python
  config = self._proactive_config()
  if not bool(config.get("enabled", True)):
      await self.cancel_user_proactive_tasks(user_id, reason="proactive_disabled")
      return None
  ```

  在用户 turn 完成后被 `_hook_proactive_schedule`（`main.py:1410`）调用，
  立即被短路；不仅不创建新的 `proactive_speak` 任务，还会顺手把同 user 已有的
  `proactive_speak` 任务全部取消。
- `runtime/task_scheduler.py:296` `complete_proactive_turn()` 同样有
  `if not bool(config.get("enabled", True)): return {"status": "skipped", "reason": "proactive_disabled"}`，
  即便有遗留任务跑起来，也会在收尾阶段被丢弃。
- 触发回调 `_handle_proactive_trigger` 在 `main.py:1209` 通过
  `task_scheduler.set_proactive_callback(_handle_proactive_trigger)` 注入，
  代码路径完整；并不是缺回调，而是**根本没有 task 被排程**。

附带一个无效配置坑（不影响本 bug，但容易误导排查）：

- `data/runtime_settings.json` 中还存在一段 `scheduler` 节
  （`enabled / interval_minutes / sleep_start / sleep_end / quiet_hours_enabled` 等）。
- 全仓搜索 `cfg["scheduler"]` / `cfg.get("scheduler"` 未命中，主动消息相关
  代码只读取 `cfg.get("proactive", {})`（`runtime/task_scheduler.py:154`、
  `runtime/proactive_service.py:43`）。
- 这段 `scheduler` 配置实际上是历史遗留，**完全不会影响行为**，但维护者很容易
  误以为"我已经把 scheduler 关了 / 开了"。

## 已确认原因

`data/runtime_settings.json` 中残留的 `proactive.enabled: false` 在启动时被深度
合并到运行配置里，将默认 `true` 覆盖为 `false`。`TaskScheduler` 在两处提前
短路，导致：

1. 用户消息处理完后不再排程下一次主动 check-in；
2. 已经存在的 `proactive_speak` 任务在第一次 hook 调用时即被全部取消；
3. 调度链不再自我延续，主动消息长期沉默。

## 影响

- 用户长时间不回消息时，机器人不会再主动联系，明显偏离 `proactive` 模块设计
  目标（关系维护、空闲检查）。
- `task_scheduler` 表面在跑（`TaskScheduler started` 仍出现在日志里），但实际
  对 proactive 通道是空转；运维只看启动日志会误判为正常。
- 该 bug 与
  [20260616-daily-compression-not-triggered-in-window.md](20260616-daily-compression-not-triggered-in-window.md)
  叠加：主动消息无法在凌晨窗口内反向产生 `/chat` 流量，进一步降低了每日压缩
  被触发的概率。

## 期望行为

- `data/runtime_settings.json` 中 `proactive.enabled` 不再以 `false` 长期常驻；
  默认启用与 `config.yaml` 保持一致。
- 用户 turn 终态为 `END_REPLIED` 且至少送达一条消息时，
  `schedule_initial_proactive` 能正常创建 `proactive_speak` 任务。
- `proactive_speak` 任务在到点后能走完
  `_fire_task` → `_handle_proactive_trigger` → `complete_proactive_turn` 的完整
  链路，并按 `frequencies[tier].interval_range_seconds` 排程下一次。
- 管理后台的 proactive 开关切换需明确写回 `runtime_settings.json`，避免出现
  "默认 true、运行时 false、长期没人发现"的静默状态。

## 可行方案（仅记录，待决定）

1. 临时恢复：通过 `/admin` 把 proactive 重新打开，或直接编辑
   `data/runtime_settings.json` 将 `proactive.enabled` 改回 `true`，重启进程。
2. 清理无效字段：移除 `runtime_settings.json` 中已无任何代码读取的 `scheduler`
   节，避免维护者误读。
3. 增加启动期校验日志：在 `_startup_validate()`（`main.py:480`）打印当前生效
   的 `cfg["proactive"]["enabled"]` 与默认值是否一致，让"运行时被覆盖"成为
   可观测事件。
4. 增加额外的人类可读告警：当 `proactive.enabled=false` 时，每次
   `_hook_proactive_schedule` 被短路后以 INFO 频率写一条
   `proactive disabled by runtime settings` 而非完全沉默，便于发现。
5. 增加配额观测：即便启用，也需要对 `proactive_quotas` 表当天 `used` 与
   `frequencies[tier].daily_limit` 的关系做一次每日巡检，避免再次被"配额已耗尽"
   静默挡住。

## 状态

已确认，原因明确。修复路径取决于产品决策：是恢复默认开启，还是显式保持关闭
但补充更显眼的日志/管理 UI 提示。严重程度建议为中：不影响被动响应链路，但
属于功能性沉默缺失，长期会被使用者注意到。
