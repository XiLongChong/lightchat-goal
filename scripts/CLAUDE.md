# CLAUDE.md

开发/运维辅助脚本。

## 脚本

```
reset_runtime_data.py                  — 清空 chat/test 数据，保留 WeChat 联系人绑定
finalize_orphaned_tap_sessions.py      — 清理异常退出后残留的 claude-tap active 会话
start-aspocket-claude-channel.ps1      — 启动 Claude Code development channel
start-aspocket-router.ps1              — 启动 AsPocket PC Router（main.py 前台运行）
check-aspocket-channel.ps1             — AsPocket 通道就绪状态诊断（只读检查，不打印 token）
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

## AsPocket 运维脚本

### start-aspocket-router.ps1
启动 AsPocket PC Router（main.py 前台运行）：
- 设置 HOST/PORT
- 校验 venv
- 读取 CHAT_API_KEY

### start-aspocket-claude-channel.ps1
启动 Claude Code development channel：
- 读取 .env
- 设置 ASPOCKET_* 环境变量
- 校验 claude CLI 和 MCP 注册

### check-aspocket-channel.ps1
AsPocket 通道就绪状态诊断：
- 只读检查，不打印 token
- 检查 Router 在线、会话注册、MCP 配置

## 注意事项

- PowerShell 脚本使用 `;` 分隔命令（无 `&&`/`||`）
- 所有脚本都避免在输出中打印敏感 token
