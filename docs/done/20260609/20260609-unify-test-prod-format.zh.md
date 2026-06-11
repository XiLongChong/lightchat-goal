# 统一测试/生产格式路径（2026-06-09）

## 问题

`test_llm_gateway.py` 的 71 个测试全部 mock `AsyncOpenAI.chat.completions.create`，触发 `deepseek.py` 中 `_is_mocked` 检测 → 走 `_build_openai_payload()` 的 **OpenAI 格式路径**。而生产环境走 `_raw_chat_completion()` → **Anthropic 格式路径**。

测试从未验证过生产路径的 Anthropic 格式。

## 修复

### deepseek.py

- 删除 `AsyncOpenAI` 导入、`client` 属性、`aclose` 方法（已无调用方）
- 删除 `_build_openai_payload()` 方法（OpenAI 格式 payload 构建器）
- 删除 `_consume_stream()` 方法（OpenAI 格式流式消费器，已被 Anthropic 的 `_raw_stream_completion` 替代）
- 简化 `complete()`：删除 mock 检测分支，固定调用 `_raw_chat_completion()`

### test_llm_gateway.py

- 全局替换 `adapter.client.chat.completions.create = AsyncMock(...)` → `adapter._raw_chat_completion = AsyncMock(...)`
- 测试现在走与生产完全相同的 `complete()` → `_raw_chat_completion()` 路径

### test_current_chat_protocol.py

- 删除 `test_registry_contains_only_current_tools`（与 `test_tools.py::test_tool_definitions_are_complete` 重复）
- 删除 `test_context_builder_preserves_structured_provider_history`（与 `test_context_builder.py` 中的测试重复）
- 清理不再使用的 import

## 验证

全量 90 测试通过，零回归。

## 影响

- 测试现在覆盖的生产路径与线上行为一致
- 代码精简：删除 ~120 行死代码
- `AsyncOpenAI` 依赖已从适配器移除（仅保留 `openai` 包的异常类型用于错误映射）
