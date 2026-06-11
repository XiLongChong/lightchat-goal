# 移除 OpenAI SDK 依赖 + SSE 流式解析器测试覆盖（2026-06-10）

## 问题

`agent/providers/deepseek.py` 中 HTTP 层已用 `httpx` 直接调用 DeepSeek Anthropic 兼容端点，但 `_map_error` 错误分类仍使用 `openai` 包的异常类型做 `isinstance` 检查——这两个类型体系不兼容，`isinstance` 检查永远为 False。所有 httpx 异常都落入 `kind="unknown"` 分支，导致 **timeout、连接失败等本应重试的错误被标记为不可重试**。

同时 `_raw_stream_completion`（SSE 流式解析器，~80 行）完全没有测试覆盖。生产环境 `stream=True` 即走此路径。

## 改动

### 1. `agent/providers/deepseek.py`

- 删除 `from openai import ...`（6 个异常类）
- `_map_error` 改为按 `httpx` 异常分类：
  - `httpx.TimeoutException` → `timeout`（可重试）
  - `httpx.ConnectError` / `httpx.RemoteProtocolError` → `network_error`（可重试）
  - `httpx.HTTPStatusError` → 按 `response.status_code` 细分：429=rate_limited，503=overloaded，50x=server_error，401/403=auth_error，40x=bad_request
- `_provider_code` 从 `exc.body`（OpenAI 属性）改为 `exc.response.json()`（httpx 属性）

### 2. `requirements.txt`

- 删除 `openai>=1.30.0`

### 3. `tests/test_llm_gateway.py` — 错误映射测试

- 用 `httpx.HTTPStatusError` / `httpx.ReadTimeout` / `httpx.ConnectError` / `httpx.RemoteProtocolError` 替代 OpenAI 异常类型构造
- 新增工厂函数 `_make_http_status()`、`_make_timeout()` 等
- 覆盖 11 种错误分类场景（原 9 种）

### 4. `tests/test_llm_gateway.py` — SSE 流式解析器测试（新增 6 个）

| 测试 | 覆盖场景 |
|------|----------|
| `test_simple_text_response` | message_start → content_block_start(text) → text_delta → message_delta |
| `test_tool_use_response` | content_block_start(tool_use) → input_json_delta → 工具调用提取 |
| `test_thinking_block` | thinking content_block + delta，多 content block 混合 |
| `test_error_status_raises_llm_error` | HTTP 400 → LLMError 抛出 |
| `test_json_decode_error_skipped` | 非法 JSON / 空 data 行 → parser 跳过，不崩溃 |
| `test_tool_use_input_json_delta_no_start` | 孤儿 delta（无 content_block_start）→ parser 跳过 |

SSE mock 通过 `_make_sse_response()` + `_run_sse_stream()` 实现，构造假的 `httpx` stream 响应，直接验证 `_raw_stream_completion` 的输出。

## 结果

- 消除一个依赖（`openai` 包）
- 修复一个 bug（httpx 异常不会被正确重试）
- 覆盖盲区（SSE parser 从 0 覆盖到 6 个测试）
- 117 tests pass（原 110）
