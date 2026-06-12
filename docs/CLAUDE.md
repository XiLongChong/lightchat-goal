# CLAUDE.md

## 目录职责

```
docs/
├── demo/   — 设计讨论、草案、思路、未完善的计划。不确定要不要做的东西先放这里。
├── doing/  — 当前正在做的，即正式决定执行的方案。同一时间不应有太多活跃文件。
├── done/   — 执行后完善的文档，已完成的设计归档。废弃的草案也从 demo/ 移入此处。
│           按日期分子目录存放：done/YYYYMMDD/（如 done/20260611/）。
└── bug/    — 当前已知的 Bug 集。修复后移除（将修复记录归档到 done/）。
```

## 工作流

1. 有新想法或初步设计 → 放入 `demo/`
2. 正式决定执行 → 从 `demo/` 移入 `doing/`
3. 实施完成 → 从 `doing/` 移入 `done/YYYYMMDD/`，补充结果摘要
4. 废弃的草案 → 从 `demo/` 移入 `done/YYYYMMDD/`，标注废弃原因
5. 发现 Bug → 记录到 `bug/`，修复后移除 Bug 文件，修复记录归档到 `done/YYYYMMDD/`

## 文件命名规则

统一格式：`{日期}-{标题}.md`

- 日期：`YYYYMMDD`，如 `20260609`
- 标题：英文小写 + 连字符，简洁描述内容
- 完整示例：`20260609-model-router-design.md`
- 中文内容可在标题后加 `.zh`：`20260609-model-router-design.zh.md`

每个文件首行应为 `# 标题（日期）` 格式，如 `# 模型路由设计（2026-06-09）`。

## done/ 目录规则

`done/` 下的文件**必须**按日期分子目录存放，不在 `done/` 根目录放任何文件：

```
done/
├── 20260609/
│   └── 20260609-model-router-design.md
├── 20260611/
│   ├── 20260611-migrate-weather-provider.md
│   └── 20260611-weather-tool-current-only.md
└── 20260612/
    └── 20260612-migrate-weather-provider.md
```

子目录名即 `YYYYMMDD`，与文件名中的日期前缀一致。
