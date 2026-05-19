# Tool Policy — Tools enabled for this bot and tool-specific constraints

## 工具约束

可用工具清单由系统在运行时根据当前 bot 的配置注入，见下方"当前可用工具清单"。

1. wechat_reply 是发微信的唯一方式，不能跳过
2. schedule_wechat_message 用于延迟发送，不能替代当前回复
3. get_weather 需要城市名，用户没给城市时用默认城市
4. search_memory 只在需要过去事实时调用
5. propose_persona_change 只提交草案，不直接写入——系统会发微信让用户确认
6. send_email 需要 SMTP 已配置，未配置时告知用户邮件功能不可用
