# maybe_trim 功能移除归档（2026-06-27）

## 原 Bug

`docs/bug/20260609-maybe-trim-stub.md` 记录 `memory/conversation.py:71-81` 中的 `maybe_trim` 函数为 stub 实现，上下文超预算时无兜底裁剪。

## 当前状态

`maybe_trim` 函数已从代码库中移除（conversation storage redesign 时删除）。当前项目无自动上下文裁剪机制。

## 影响评估

- 当前生产数据量小（单用户、短会话），未触发问题
- 每日压缩机制（3:56-4:25）定期清理历史，减轻内存增长
- 如未来用户量或会话长度显著增长，需重新设计上下文预算管理策略

## 决策

暂不重新实现 `maybe_trim`。如未来需要上下文裁剪，应在设计文档中明确策略（保留最近 N 轮 vs 按 token 预算 vs 语义压缩），而非简单恢复旧 stub。
