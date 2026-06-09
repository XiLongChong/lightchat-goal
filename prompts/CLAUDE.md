# CLAUDE.md

LLM 提示词模板。

## 文件

```
persona.md               — 当前激活的 bot 人设（「栖」——微信聊天伴侣）
bot_identity_default.md  — 兼容性占位文件，指向 persona.md
```

## persona.md

定义 bot 的角色身份、语气、接话规则、情绪处理方式。主要内容：
- 基本设定：角色叫「栖」，用户是微信里长期聊天的人
- 核心关系感：熟悉感、陪伴感、偏心感、真实感
- 接话规则：优先接情绪而非解释，能短不长，像真人微信聊天
- 禁止风格：客服腔、导师腔、鸡汤、过度贫嘴、过度解释

文件通过 `main.py` 的 `read_persona_file()` 加载，内容拼接到 system prompt 中。

## 注意事项

- 编辑 persona 可通过 admin UI（`/admin/persona`）或直接编辑文件
- `bot_identity_default.md` 仅作向后兼容保留，实际不再使用
- persona 内容不经过 env var 替换，直接作为原始文本
