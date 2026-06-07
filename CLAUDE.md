# CLAUDE.md

## 角色定位

你是这个仓库的开发执行 agent，不是架构决策 agent。

本文件只说明如何在本仓库里工作。具体系统架构、模块边界、运行时状态机、工具暴露规则、上下文构建规则、记忆系统规则，可能会随设计推进而变化，不以本文件为准。

## 重要协议决策：DeepSeek 使用 Anthropic Messages 格式

普通聊天的 DeepSeek 请求按 Anthropic 兼容协议构建，不按 OpenAI Chat Completions 协议构建。

- `base_url` 固定目标形态为 `https://api.deepseek.com/anthropic`。
- 实际请求地址必须是 `https://api.deepseek.com/anthropic/v1/messages`。
- 不要把 Anthropic base URL 拼接成 `/chat/completions`。
- 使用 claude-tap 反向代理抓包时，tap target 也必须是 `https://api.deepseek.com/anthropic`；否则代理会把 `/v1/messages` 转发到 OpenAI 根路径下，导致 404。
- 顶层请求字段按 Claude Code 抓包便于阅读的顺序组织：`model`, `messages`, `system`, `tools`, `max_tokens`, `thinking`, `output_config`, `stream`。
- 工具定义必须使用 Anthropic 格式：
  - `name`
  - `description`
  - `input_schema`
- 不要在 Anthropic payload 中发送 OpenAI `{"type":"function","function":{...}}` 工具定义。
- assistant 工具回灌必须使用 Anthropic content block：`thinking` 后接一个或多个 `tool_use`。
- thinking 模式下，只要 assistant 返回 tool use，就必须在后续请求中回传该 assistant 的 thinking 内容。
- 工具结果必须是单条 `role="user"` 消息内的 `tool_result` content block。
- 如果一次 assistant 返回多个 `tool_use`，必须把全部对应结果放进同一条 user 消息中；每个 `tool_use.id` 必须有对应的 `tool_result.tool_use_id`。

## 当前开发入口

开始任何运行时重构或功能实现前，先读：

1. `docs/companion-runtime-implementation-plan.zh.md`
2. `docs/runtime-specs/01-context-builder-spec.zh.md`
3. `docs/runtime-specs/02-tool-exposure-policy-spec.zh.md`
4. `docs/runtime-specs/03-turn-engine-spec.zh.md`
5. `docs/runtime-specs/04-memory-system-spec.zh.md`
6. `docs/runtime-specs/05-scheduler-runtime-spec.zh.md`
7. `docs/runtime-specs/10-test-matrix.zh.md`

如果实施计划和 runtime-specs 冲突，停止开发并报告冲突。不要自行选择一边实现。

如果代码和文档冲突，默认认为文档描述的是目标状态、代码描述的是当前状态。实现时按当前 phase 的任务范围推进，不要一次性重写整个系统。

## 工作原则

- 只做当前任务或当前 phase。
- 只改任务允许修改的文件。
- 不顺手重构无关模块。
- 不把旧架构说明当作事实来源。
- 不在没有规格的情况下补业务判断。
- 不为了让测试通过而降低协议约束。
- 不保留长期兼容层，除非实施计划明确要求迁移期 adapter。
- 发现规格缺口、规格冲突、测试矩阵无法覆盖时，先报告，不要猜。

## 开发流程

1. 阅读当前任务指定文档。
2. 对照实施计划确认当前 phase 的目标、允许修改范围、禁止修改范围、验收测试。
3. 阅读相关现有代码，确认当前实现和目标规格的差异。
4. 做最小必要修改。
5. 新增或更新对应测试。
6. 运行当前 phase 要求的测试。
7. 汇报修改内容、测试命令、测试结果、未覆盖风险。

## 运行与测试

优先使用仓库本地虚拟环境：

```powershell
D:\projects\goal\.venv\Scripts\python.exe -m pytest
```

如果只改某个模块，优先跑相关测试，再按任务要求决定是否跑全量测试。

常用命令：

```powershell
D:\projects\goal\.venv\Scripts\python.exe -m pytest tests\test_xxx.py -v
D:\projects\goal\.venv\Scripts\python.exe -m py_compile main.py
```

不要用“没有运行测试”冒充完成。如果测试无法运行，必须说明原因和阻塞点。

## 代码阅读优先级

不同任务优先看不同入口：

| 任务类型 | 优先阅读 |
| --- | --- |
| FastAPI 路由、依赖注入、启动流程 | `main.py` |
| 对话主流程、turn 生命周期 | `agent/chat_agent.py` |
| 运行时状态对象 | `agent/state_machine.py` |
| 上下文构建 | `agent/context_builder.py` 或新规格指定的目标模块 |
| 工具注册和工具 schema | `agent/tool_registry.py` |
| 工具分类和调用合法性 | `agent/tool_classifier.py` |
| 工具实现 | `agent/tools.py` |
| 调度和主动消息 | `agent/scheduler.py` |
| 记忆存取 | `memory/store.py` |
| 输出保护 | `agent/output_guard.py` |
| 配置项 | `config.yaml` |
| 管理界面 | `static/` |

这些文件只是当前代码入口，不代表最终架构边界。最终边界以 runtime-specs 和实施计划为准。

## 文档使用规则

实施类任务的事实优先级：

1. 用户当前任务说明
2. `docs/companion-runtime-implementation-plan.zh.md`
3. `docs/runtime-specs/*.zh.md`
4. 当前代码
5. 其他历史文档
6. 本文件

历史设计文档可以帮助理解背景，但不能覆盖 runtime-specs。

## 重要约束

- 模型工具名、终态顺序、上下文 block 顺序、记忆写入规则、主动消息规则，必须按 runtime-specs 实现。
- 当前版本是否暴露某个工具，要查 `02-tool-exposure-policy-spec.zh.md` 和 `10-test-matrix.zh.md`。
- 上下文打包内容，要查 `01-context-builder-spec.zh.md`。
- turn 如何进入终态，要查 `03-turn-engine-spec.zh.md`。
- 记忆如何存取、过滤、写入，要查 `04-memory-system-spec.zh.md`。
- 调度和主动触发，要查 `05-scheduler-runtime-spec.zh.md`。
- 测试是否足够，要查 `10-test-matrix.zh.md`。

## Git 与现有改动

工作区可能已经有用户或其他 agent 的改动。

- 不要回滚自己没有做的改动。
- 修改前先看 `git status --short`。
- 如果必须编辑已有改动的文件，先读清楚当前内容，基于现状继续改。
- 不使用 `git reset --hard`、`git checkout -- <file>` 等破坏性命令，除非用户明确要求。

## 完成汇报格式

完成任务时，汇报：

```text
修改内容：
- ...

测试：
- <命令> -> <结果>

未覆盖/风险：
- ...
```

如果发现不能继续：

```text
阻塞原因：
- ...

涉及文件：
- ...

建议下一步：
- ...
```
