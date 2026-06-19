# 主动消息与每日压缩触发修复（2026-06-19）

关联 bug：

- `docs/bug/20260616-proactive-message-disabled-by-runtime-settings.md`
- `docs/bug/20260616-daily-compression-not-triggered-in-window.md`

## 问题

两个运行问题叠加导致长期后台维护链路失效：

1. `data/runtime_settings.json` 中残留 `proactive.enabled=false`，启动时覆盖
   `config.yaml` 的默认开启状态，导致用户 turn 结束后不会排程 `proactive_speak`。
2. 每日压缩没有独立时间触发器，只能搭车 `/chat` 请求。凌晨压缩窗口内没有用户消息时，
   `maybe_compress_daily()` 根本不会被调用。

## 修复

- `runtime/task_scheduler.py`
  - 新增每日压缩回调槽位。
  - `TaskScheduler.start()` 时注册两个 Asia/Shanghai cron job：
    - `daily_compression_0356`：03:56。
    - `daily_compression_0401_0421`：04:01、04:06、04:11、04:16、04:21。
  - 回调执行结果写入日志，异常保留完整 traceback。
- `main.py`
  - 接入 `_run_daily_compression_from_scheduler()`，从 `bot_contact_state` 取得当前联系人。
  - 后台触发时复用用户锁；必要时从 DB rebuild 当前 `ConversationContext`。
  - 继续调用同一个 `_run_compression(force=False)`，保留窗口判断与
    `last_compressed_at` 幂等语义。
  - 启动校验日志记录 proactive 的默认值、最终生效值和 runtime override 是否存在。
  - 当默认开启但运行时关闭时输出明确 warning。
- `data/runtime_settings.json`
  - 将 `proactive.enabled` 恢复为 `true`。
  - 删除已无代码读取的历史 `scheduler` 节，避免误导排查。

## 验证

```text
python -m pytest tests/test_reminders.py tests/test_compressor.py
55 passed

python -m py_compile main.py runtime/task_scheduler.py
data/runtime_settings.json passed json.tool validation
```

新增测试覆盖 `TaskScheduler.start()` 会注册每日压缩 cron job，避免压缩触发器再次只存在于文档中。
