# 压缩重写（2026-06-10）

## 结果

压缩方案从 "注入 tool_result" 重写为 "伪造用户消息 + ContextBuilder 自动合并"。

## 涉及文件（最终实现）

| 文件 | 改动 |
|------|------|
| `agent/compressor.py` | 重写：伪造 user 消息替代文本打平；新增 `_filter_summary_blocks`、`_build_compression_call`、`_extract_summary`、`_in_compress_window`、`maybe_compress_daily`；废弃 `_build_compress_messages`、`_content_repr` |
| `agent/runtime_context_builder.py` | `_is_summary_only_user_message` + `build_for_chat` 末尾合并逻辑 |
| `memory/store.py` | `apply_compression` 重写为"软删全部 + 写入压缩后消息数组"；废弃 `get_all_events_for_compress` |
| `memory/conversation.py` | `replace_with_compressed(summary)` — 只存 `[user([summary])]` |
| `main.py` | `_maybe_compress` 删除，改为调用 `maybe_compress_daily`；手动压缩端点适配新接口 |
| `tests/test_compressor.py` | 32 个测试：过滤、窗口、压缩落盘、内存替换、`_is_summary_only_user_message` |

## 核心流程

```
压缩调用:
  conversation_ctx._messages（原封不动）
    → _filter_summary_blocks（跳过旧 <summary>）
    → + [user(COMPRESSION_PROMPT)]
    → LLM → 提取 <summary>

压缩落盘:
  conversation_ctx.replace_with_compressed(summary)
    → _messages = [user([<summary>])]      ← DB 只存摘要
  store.apply_compression(cid, _messages)   ← 软删旧 + 写新

下一轮 context 构建:
  build_for_chat(prebuilt_messages=[user([<summary>])], user_message="...")
    → _is_summary_only_user_message(messages[-1]) → True
    → 合并: messages[-1].content.append(channel_block)
    → 结果: [user([<summary>, <channel>当前消息</channel>])]
```

## 关键设计决策

- **DB 永远只存摘要**，不提前混入未完成 turn 的用户消息
- **`_is_summary_only_user_message`** 检查语义而非 block 数量：`role=="user"` + `len(content)==1` + `content[0].type=="text"` + `text` 以 `<summary>` 开头
- **每日压缩和手动压缩统一**：都只存 `[user([summary])]`，`build_for_chat` 自动合并
- **`user_message` 始终非空**，ChatAgent 校验不变
- **main.py 无分流逻辑**：不区分每日/手动，不传 channel_msg，无 `actual_user_message`

## 废弃清单

| 函数 | 文件 |
|------|------|
| `_build_compress_messages()` | compressor.py |
| `_content_repr()` | compressor.py |
| `get_all_events_for_compress()` | store.py |
| `_maybe_compress()` | main.py |
| `apply_compression()` 旧逻辑（软删留2条） | store.py |

## 相关 Bug

| Bug | 状态 |
|-----|------|
| 压缩窗口单向检查（3:56-4:25 窗口） | 已修复 |
| 压缩消息格式错误（历史打平 + 提示词位置错误） | 已修复 |
| 压缩后 tool_result 孤儿引用 400 | 已修复 |
| 用户消息未写入 conversation_ctx 内存 | 已修复 |
| 压缩后消息重复（build_for_chat 二次追加） | 已修复 |
