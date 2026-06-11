# CLAUDE.md

测试套件。

## 运行

```bash
# 全部
pytest tests/ -v

# 单个文件
pytest tests/test_chat_security.py -v

# 按名称过滤
pytest tests/ -v -k "test_guard"
```

## 测试文件

```
conftest.py                 — 共享 fixtures：tmp_path 重写（Windows 权限兼容）、MemoryStore 清理
test_chat_security.py       — /chat 端点安全测试（认证、输入校验）
test_output_guard.py        — output_guard 消息过滤测试
test_tools.py               — 工具执行测试（get_time, get_weather）
test_wechat_bridge.py       — 微信桥接测试
test_llm_gateway.py         — LLM 网关测试
test_context_builder.py     — ContextBuilder 组装与校验测试
test_compressor.py           — 每日对话压缩测试（31 个）
test_long_term_memory.py     — 长期记忆文件服务、隔离、回滚与索引诊断
test_admin_memory.py         — 管理端列表、查看、删除和一键清除长期记忆
test_current_chat_protocol.py — 当前聊天协议集成测试
```

## 关键 fixtures

- `conftest.py::tmp_path` — 重写 pytest 内置 fixture，在 Windows 下检测 tmp 目录权限问题并 skip 而非 crash
- `conftest.py::pytest_sessionfinish` — 测试结束后关闭所有 MemoryStore 实例的 SQLite 连接

## 注意事项

- 使用 `pytest-asyncio` 支持异步测试
- Windows 环境下 tmp_path 权限问题是已知环境问题（非代码缺陷），conftest 提供了诊断信息
- MemoryStore 支持 `:memory:` 模式，测试中应使用内存数据库
