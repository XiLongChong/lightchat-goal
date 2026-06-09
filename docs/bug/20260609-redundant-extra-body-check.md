# _build_payload 中冗余的 extra_body 检查（2026-06-09）

## 位置

`agent/providers/deepseek.py:58-63`

```python
request_extra = dict(request.extra)
request_extra.pop("extra_body", None)

for key, value in request_extra.items():
    if key != "extra_body":       # ← 冗余
        payload[key] = value
```

## 现象

第 59 行已从副本中 `pop("extra_body")`，循环中第 62 行再检查 `key != "extra_body"` 永远不会匹配到该键——它已不存在。

## 影响

无功能影响，纯粹冗余代码。`pop` 后保留空检查遗留自重构前。
