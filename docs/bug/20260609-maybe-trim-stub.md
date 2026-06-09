# maybe_trim 未实现（2026-06-09）

## 位置

`memory/conversation.py:71-81`

## 现象

```python
def maybe_trim(self, store: MemoryStore, estimated_tokens: int, max_budget: int) -> bool:
    if estimated_tokens <= max_budget:
        return False
    return True  # Signal that trim is needed (caller handles specifics)
```

文档描述为"drop the oldest non-critical turns, keeping last 20"，实际实现只比较阈值并返回布尔值，不执行任何裁剪操作。注释说"caller handles specifics"，但没有任何调用方。

## 影响

上下文超预算时（默认 1M tokens），无兜底裁剪生效。消息数组持续增长无上限，LLM 请求可能因 token 超限被 provider 拒绝。

## 根因

存储重设计时 `maybe_trim` 作为接口预留，但裁剪逻辑未实现。当前生产数据量小（单用户、短会话），未触发问题。

## 修复方向

实现真正的裁剪：从 `_messages` 头部丢弃最早 turn 的 events，同步更新 DB 的 `trim_marker_turn`。
