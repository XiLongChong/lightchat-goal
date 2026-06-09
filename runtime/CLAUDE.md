# CLAUDE.md

Chat runtime 薄封装层——将 ChatAgent 包装为可配置的运行时入口。

## 文件

```
chat_runtime.py  — ChatRuntime 数据类 + create_chat_runtime 工厂函数
__init__.py      — 导出 ChatRuntime, create_chat_runtime
```

## ChatRuntime

数据类，持有对 chat_agent、context_builder、tool_exposure_policy 的引用。`handle_chat()` 方法直接委托给 `chat_agent.chat()`，只做参数透传。支持 `prebuilt_messages` 参数——从 ConversationContext 传入预构建消息数组，跳过 DB 读取。

## 与 main.py 的关系

`main.py` 中通过 `create_chat_runtime()` 创建全局单例，所有 `/chat` 请求通过它路由：

```python
chat_runtime = create_chat_runtime(user_id="", bot_id=BOT_ID, ...)
result = await chat_runtime.handle_chat(user_message=..., ...)
```

## 注意事项

- 这个层的存在主要是为了未来可能的多 runtime 实例（不同 bot 配置），当前只有一个全局实例
- `build_turn_request()` 和 `get_history_for_context()` 是辅助方法，当前未在主流程中使用
