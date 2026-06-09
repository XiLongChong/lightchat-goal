# CLAUDE.md

开发/运维辅助脚本。

## 脚本

```
reset_runtime_data.py  — 清空 chat/test 数据，保留 WeChat 联系人绑定
```

## reset_runtime_data.py

清空 memory.db 中除 `bot_contact_state` 外的所有表。用法：

```bash
# 预览（dry run）
python scripts/reset_runtime_data.py

# 确认执行
python scripts/reset_runtime_data.py --confirm
```

注意：`bot_contact_state` 包含微信 iLink 的 context_token，丢失后需要重新扫码部署。
