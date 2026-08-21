# Ctrl+C 退出时打印大量异常堆栈

## 现象

使用 `run.bat` 启动服务，测试完成后按一次 Ctrl+C。服务虽然退出，但终端连续打印多段 ERROR traceback，主要包括：

```text
KeyboardInterrupt
```

```text
asyncio.exceptions.CancelledError
```

```text
ERROR: Exception in ASGI application
```

从终端表现看，正常的人工停止被显示成了服务异常崩溃。

## 证据

- `run.bat` 直接运行 `.venv\Scripts\python main.py`，退出后的 `pause` 只负责保留窗口，不会产生这些异常。
- `main.py` 使用 `uvicorn.run(app, ...)` 启动服务。
- Ctrl+C 被 Uvicorn 捕获后重新触发信号，最外层出现 `KeyboardInterrupt`。
- 退出时仍有一个 `/wechat/deploy/status` 请求正在执行。
- 该接口正在 `poll_qr_status()` 中等待 iLink 的二维码状态响应。
- 二维码状态轮询使用最长 `35,000 ms` 的 HTTP 长轮询。
- 事件循环关闭时取消了该在途请求，取消沿以下路径传播：

```text
wechat_deploy_status
-> poll_qr_status
-> _api_fetch
-> httpx.AsyncClient.get
-> asyncio.exceptions.CancelledError
```

- `poll_qr_status()` 的 `except Exception` 不会拦截当前 Python 中继承自 `BaseException` 的 `asyncio.CancelledError`，因此取消继续传播到 ASGI 层并被记录为应用错误。

## 已确认原因

这是服务退出阶段的取消和日志处理缺陷，不是二维码请求自身失败，也不是 `run.bat` 启动失败。

Ctrl+C 导致服务关闭属于预期行为；关闭过程中取消 lifespan 任务和在途 HTTP 请求也属于正常行为。当前问题是这些预期取消没有被退出路径整洁地收束，最终以 ERROR traceback 的形式暴露给操作者。

日志中的三段异常属于同一次关闭过程：

1. Uvicorn 处理 Ctrl+C 后产生 `KeyboardInterrupt`。
2. Starlette lifespan 等待任务随事件循环关闭而取消。
3. 正在执行的二维码状态长轮询被取消，并被记录成 `Exception in ASGI application`。

## 影响

- 操作者无法判断服务是正常停止还是发生了真实故障。
- 退出日志被大量无意义堆栈污染，可能掩盖同一时间的真实错误。
- 如果关闭清理将来增加更多资源，当前生命周期结构不利于确认清理是否完整执行。
- 现有证据未显示数据损坏或服务无法退出。

## 期望行为

- 按一次 Ctrl+C 后进入正常关闭流程。
- 在途长轮询可以被取消，但预期的 `CancelledError` 不应记录为应用故障。
- 生命周期清理必须执行，并输出简短、明确的关闭日志。
- 正常退出不应打印 `KeyboardInterrupt`、lifespan `CancelledError` 或 `Exception in ASGI application` 堆栈。
- 非取消类异常仍应保留完整错误日志。

## 可行方案

建议后续修复时同时处理生命周期和在途请求两个边界：

1. 使用 `try/finally` 包住 lifespan 的 `yield`，保证 bridge 停止和数据库关闭在取消场景下仍能执行。
2. 为二维码状态长轮询明确处理 `asyncio.CancelledError`，完成必要清理后继续传播取消，不把它转换成普通的 `wait` 结果。
3. 调整 Uvicorn 的关闭入口或信号处理方式，使单次 Ctrl+C 走优雅关闭流程，不在正常退出后重新打印顶层 `KeyboardInterrupt`。
4. 设置有限的 graceful-shutdown 等待时间，避免 35 秒长轮询阻塞退出，同时允许短请求正常完成。
5. 增加集成测试：启动服务、保持 `/wechat/deploy/status` 请求在途、发送一次终止信号，并断言进程正常退出、清理日志完整且没有 ERROR traceback。

不建议简单吞掉所有 `BaseException` 或关闭全部错误日志，这会同时隐藏真实的强制退出和资源清理问题。

## 修复进展

### 已修复（2026-06-27 确认）

- **方案 1**：`@asynccontextmanager` 装饰器已天然提供 `yield` 前后的 try/finally 语义。`lifespan()` 中 `task_scheduler.stop()`、`_bridge.stop()`、`memory_store.close()` 在取消场景下均能执行。

### 仍待修复

- **方案 2**：`bridge/ilink_client.py::poll_qr_status()` 仍为 `except Exception`，未显式捕获 `asyncio.CancelledError`。退出时 35s 长轮询被取消仍可能记录为 ASGI 错误。不影响功能，仅影响退出日志美观。

## 状态

部分修复。剩余问题严重程度为低：仅影响退出体验，不影响正常运行期间的业务请求或持久化数据。
