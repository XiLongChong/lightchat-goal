# CLAUDE.md

管理后台前端（纯静态 HTML/CSS/JS，无构建工具）。

## 文件

```
admin.html  — 单页应用：登录、配置、事件仪表板、日志查看器、手动发送
admin.css   — 样式（暗色主题、移动端适配）
admin.js    — 交互逻辑（API 调用、事件轮询、QR 部署流程）
```

## 功能页面

- 登录（密码认证）
- 仪表板（最近的 turn 事件、发送状态、工具调用）
- 人设编辑（persona 文本编辑器）
- 运行设置（微信延迟、天气城市等）
- 微信部署（QR 码扫描 → 轮询状态 → 启动桥接）
- 日志查看器（按级别/关键词过滤）
- 手动发送消息
- 开发者工具（重置上下文、查看 turn 诊断）

## 注意事项

- 通过 `/static` 路径挂载（FastAPI StaticFiles）
- 所有 API 调用需要 admin session cookie
- QR 部署流程：`/wechat/deploy/start` → 轮询 `/wechat/deploy/status` → confirmed 后自动启动 bridge
- 无前端构建步骤，直接修改即生效
