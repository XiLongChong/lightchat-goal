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

### 安全与核心流程

```
conftest.py                    — 共享 fixtures：tmp_path 重写（Windows 权限兼容）、MemoryStore 清理
test_chat_security.py          — /chat 端点安全测试（认证、输入校验）
test_output_guard.py           — output_guard 消息过滤：微信文本清理、工具调用ID阻断、密钥脱敏、长文本截断
test_tools.py                  — 工具执行测试（get_time, get_weather, FakeWeatherProvider）
```

### LLM 与上下文

```
test_llm_gateway.py            — LLMGateway、DeepSeekAdapter、trace 基础设施测试
test_context_builder.py        — ContextBuilder 组装与校验：系统提示词、历史转换、channel 包装
test_compressor.py             — 对话压缩测试（31 个）：摘要块过滤、压缩调用构建、记忆操作提取验证
test_daily_compression_runner.py — DailyCompressionRunner：每日压缩流程、用户锁保护
test_daily_summaries.py        — 每日摘要持久化：日期边界计算（5AM 前→前一天）
test_context_usage.py          — 上下文使用统计：token 用量合并、峰值追踪
```

### 桥接与发送

```
test_wechat_bridge.py          — 微信桥接测试：回复延迟限制、HTTP 错误日志
test_sender.py                 — WeChatSender：批量发送、桥接状态处理、消息状态映射
test_qr_utils.py               — bridge.qr_utils：QR 码 SVG/PNG 生成、payload 标准化
```

### 持久化与记忆

```
test_long_term_memory.py       — 长期记忆服务：文件隔离、回滚、索引诊断、CRUD 操作
test_admin_memory.py           — 管理端长期记忆：列表、查看、删除、一键清除
test_reminders.py              — 提醒工具：ReminderStore、创建、到期查询
test_protocol_writer.py        — ProtocolEventWriter：协议事件持久化、turn 数据写入
```

### App Adapter

```
test_app_adapter.py            — AsPocket app adapter 端点测试：ISO 时间戳转换、会话注册、API 端点（160+ 行）
test_app_session_echo.py       — 开发回显服务器：payload/response 构造助手
test_claude_code_mock_adapter.py — Claude Code 模拟适配器：回复模板构造
```

### 运行时与配置

```
test_current_chat_protocol.py  — 当前聊天协议集成测试
test_runtime_config.py         — 运行时配置：env 变量替换、deep_update、配置文件读写
test_contact.py                — runtime.contact：联系人发送阻断原因、绑定状态检查
test_user_turns.py             — UserTurnRegistry：用户锁、并发控制、中断检测
test_admin_labels.py           — 管理员标签显示：路由、策略、发送状态、来源标签
test_turn_hooks.py             — 可观测性后钩子：开发日志、模型尝试日志、不修改 turn
```

### 外部服务

```
test_weather_provider.py       — QWeather 提供商内部测试：位置信息、格式化、日期标准化
test_web_search.py             — Tavily 网络搜索工具：未配置、成功搜索、空结果场景
test_todo_provider.py          — Microsoft To Do 提供商和工具执行器测试
test_proactive_service.py      — 主动消息推送服务：启用/禁用、时间窗口、间隔控制
test_log_reader.py             — runtime.log_reader：最近日志行读取、截断处理
```

## 关键 fixtures

- `conftest.py::tmp_path` — 重写 pytest 内置 fixture，在 Windows 下检测 tmp 目录权限问题并 skip 而非 crash
- `conftest.py::pytest_sessionfinish` — 测试结束后关闭所有 MemoryStore 实例的 SQLite 连接

## 注意事项

- 使用 `pytest-asyncio` 支持异步测试
- Windows 环境下 tmp_path 权限问题是已知环境问题（非代码缺陷），conftest 提供了诊断信息
- MemoryStore 支持 `:memory:` 模式，测试中应使用内存数据库
