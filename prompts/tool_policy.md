# Tool Policy — Tools enabled for this bot and tool-specific constraints

## 可用工具

- wechat_reply(text, delay) — 回复微信，每次调用发一条消息
- schedule_wechat_message(delay_minutes, messages) — 安排未来发送微信消息。messages 是数组，每项包含 text 和 delay
- get_weather(city) — 查天气
- get_time(timezone) — 查时间
- search_memory(query, top_k) — 按需搜索长期记忆
- read_persona_file() — 读取当前人设文件
- propose_persona_change(content, reason) — 提出人设修改草案，等待用户微信确认
- send_email(to, subject, body) — 发送邮件

## 工具约束

1. wechat_reply 是发微信的唯一方式，不能跳过
2. schedule_wechat_message 用于延迟发送，不能替代当前回复
3. get_weather 需要城市名，用户没给城市时用默认城市
4. search_memory 只在需要过去事实时调用
5. propose_persona_change 只提交草案，不直接写入——系统会发微信让用户确认
6. send_email 需要 SMTP 已配置，未配置时告知用户邮件功能不可用
