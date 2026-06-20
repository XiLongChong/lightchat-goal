# Agent Protocol 收敛设计（2026-06-20）

> 状态：设计草案（doing）。本文不修改任何代码，仅产出迁移设计。
> 基线：commit `a16a1c8`。

## 0. 背景与动机

当前 Router 已支持三类 App agent 投递路径：

1. **default / internal** —— 消息直接进入本进程 ChatRuntime（DeepSeek agent），回复同步返回。
2. **attached_session** —— 外部 agent 进程注册一个 session，Router 通过 HTTP POST `callbackUrl` 转发消息并收回调回复。
3. **claude_channel** —— Router 把消息存进内存 pending inbox，由一个 MCP `claude/channel` server 轮询、推送、ack、回投回复。

这三类是**技术投递协议**，但命名开始绑定具体实现（`attached_session`、`claude_channel`）。如果未来 Codex / OpenCode / 其它 self-agent 都各加一个专用 kind，Router 会陷入「每来一个 agent 加一个分支」的膨胀：`main.py` 继续变大、测试与文档重复、`resolve_app_remote_agent` 的 `if kind == ...` 链无限增长。

本设计的目标：把技术协议**收敛为少数通用 kind**（`internal` / `callback` / `channel`），并定义稳定协议标识（`aspocket.callback.v1` / `aspocket.channel.v1`），同时**完全兼容**现有 App 与已注册的 claude_channel/attached_session session。

---

## 1. 当前状态

### 1.1 default / internal path

- `resolve_app_remote_agent` 命中 `"default"` 或配置型 `remoteAgentId`（无注册 session）时返回 `target="default"`。
- `POST /api/messages` 在 `target=="default"` 分支：写 user 消息 → `chat_runtime.handle_chat(...)`（55s 超时）→ 只把 `SENT_CONFIRMED/delivered` 的 `send_text_message` 输出落库为 agent reply（`sequence` 从 1 递增）→ 返回。
- `agentType="pc-agent"`（见 `build_app_contact_invitation` / 配置型 invitation）。

### 1.2 attached_session callback path

- session 通过 `POST /api/app-agent-sessions/{id}/register`（实际为 `api_app_agent_session_register`）注册，`kind` 缺省/未知 → `"attached_session"`。
- `resolve_app_remote_agent` 命中注册 session 时：`kind = session.kind or "attached_session"`，`target = "claude_channel" if kind=="claude_channel" else "attached_session"`。
- `target=="attached_session"` 分支：校验 `status=="online"`、`callbackUrl` 存在（否则 501/503），调用 `_call_attached_session_callback` 向 `callbackUrl` 做 HTTP POST，拿到 replies 后用 `insert_app_turn_messages` 原子写 user + replies（同 `source_turn_id`，reply `sequence` 从 1 起）。
- `agentType="attached-session"`（invitation builder）。
- `callbackUrl` 必须是 `http://` 且 host ∈ {localhost, 127.0.0.1, ::1}（SSRF 收敛，见 `validate_attached_session_callback_url`）。

### 1.3 claude_channel pending inbox path

- 注册时 `kind="claude_channel"`。
- `target=="claude_channel"` 分支：写 user 消息（`sequence=0`）→ `channel_pending_inbox.add(...)` 入队 → 立即返回（`agentReplies=[]`）。
- channel server 走专用端点：`GET .../pending-messages`（poll）、`POST .../pending-messages/{deliveryId}/ack`、`POST .../replies`（写 agent reply，`sequence = max+1`，同 turn）。
- 这些端点用 `(session.get("kind") or "attached_session") != "claude_channel"` 做 kind 守卫，非 channel session 直接 400/404。
- `agentType="claude-code-channel"`（invitation builder）。
- inbox 内存、at-least-once、按 `(sessionId, clientMessageId)` 幂等；重启丢失（App 可用同 `clientMessageId` 重发，DB 层幂等兜底）。

### 1.4 App 视角

App **只感知产品概念**：`remoteAgentId`、`endpoint`、`displayName`、`description`、`agentType`（仅用于 UI 图标/文案）。App **不知道** `attached_session` / `claude_channel` / `target` 这些技术字段——它们只存在于 Router 内部与 register 请求体。这是兼容性能够成立的关键前提。

---

## 2. 问题

1. **分支膨胀**：当前 `kind ∈ {attached_session, claude_channel}`，每加一种外部 agent（Codex、OpenCode、self-agent…）若沿用「加专用 kind」模式，`resolve_app_remote_agent`、register 校验、invitation builder、各 channel 端点的 kind 守卫都要各加一条分支。
2. **命名耦合实现**：`claude_channel` 把「投递机制（channel/inbox 轮询）」和「具体 agent（Claude Code）」焊死。换一个 channel 型 agent 就得复制一整套端点与守卫。
3. **`main.py` 持续膨胀**：session resolving、callback 转发、channel poll/ack/reply、invitation 构建全堆在 `main.py`（已 ~2400 行）。
4. **测试/文档重复**：每个专用 kind 都会带一套几乎相同的 poll/ack/reply 测试与文档。
5. **协议无版本**：callback 与 channel 的请求/响应形状只在代码与零散 e2e 文档里，没有显式 `protocol` 标识，未来改形状无版本兜底。

---

## 3. 目标模型

### 3.1 产品概念（App 可见，不变）

- **AgentSession**：一个外部 agent 进程的一次注册（`sessionId` + `remoteAgentId` + `displayName` + `status`）。
- **Contact**：App 上的联系人，由 invitation 描述（`remoteAgentId` + `endpoint` + `displayName` + `agentType`）。
- **remoteAgentId**：App 与 Router 之间的稳定路由键（`"default"` 或自定义）。

App 永远只操作这些产品概念，不感知技术 kind。

### 3.2 技术 kind（Router 内部，收敛为 3 个）

| 新 kind   | 含义                                       | 协议标识                | 对应当前            |
|-----------|--------------------------------------------|-------------------------|---------------------|
| `internal`| 本进程 agent（ChatRuntime/DeepSeek）       | `aspocket.internal.v1`  | default path        |
| `callback`| Router 主动 HTTP POST 到 `callbackUrl` 收回复 | `aspocket.callback.v1`  | attached_session    |
| `channel` | Router 入队，外部 server 轮询/ack/回投      | `aspocket.channel.v1`   | claude_channel      |

- `internal` 主要用于描述 default path，使其与其余两类对齐（三者同构）。
- `callback` 与 `channel` 是两种「外部 agent 接入协议」。新 agent 只要选其中一种协议即可，**不再新增 kind**。
- `protocol` 字段是面向未来的稳定标识；当前两个外部协议的线上形状就是 `aspocket.callback.v1` / `aspocket.channel.v1`，本次只是**给已有形状命名**，不改形状。

### 3.3 kind ↔ 协议 ↔ 端点的映射

```
internal  → aspocket.internal.v1  → /api/messages (default 分支)
callback  → aspocket.callback.v1  → /api/messages (callback 分支) + register(callbackUrl)
channel   → aspocket.channel.v1   → /api/messages (入队) + pending-messages/ack/replies
```

---

## 4. 兼容策略

核心原则：**外部行为不变，仅内部归一化**。归一化在一个 helper 里集中完成，旧名作为别名永久保留（见 §9）。

### 4.1 kind 归一化

引入 `normalize_session_kind(raw) -> "internal"|"callback"|"channel"`：

| 输入（raw kind）          | 归一化结果 | 说明                                  |
|---------------------------|------------|---------------------------------------|
| 缺省 / `None` / `""`      | `callback` | 现状缺省即 attached_session=callback  |
| `"attached_session"`      | `callback` | 别名                                  |
| `"callback"`              | `callback` | 新名                                  |
| `"claude_channel"`        | `channel`  | 别名                                  |
| `"channel"`               | `channel`  | 新名                                  |
| `"default"` / `"internal"`| `internal` | 新名（描述 default path）             |
| 其它未知值                | `callback` | 与现状「未知→attached_session」一致   |

所有读 `kind` 的点（`resolve_app_remote_agent`、register 校验、invitation builder、channel 端点守卫）改为调用此 helper，**逻辑等价**，不再到处手写 `or "attached_session"` / `if kind == "claude_channel"`。

### 4.2 `target` 派生

`target` 仍由 kind 派生，但归一化后只剩三值映射：

```
internal → default 分支
callback → attached_session 分支（行为不变）
channel  → claude_channel 分支（行为不变）
```

> 注：`target` 是内部实现细节，可保留旧字符串值（`"default"`/`"attached_session"`/`"claude_channel"`）以最小化 diff，也可以同步改成新名。**建议 Phase 1 保留旧 target 字符串**，避免一次性改动过大；归一化只发生在 `kind` 层。

### 4.3 API response 的 `agentType` —— 选项

`agentType` 是 App 唯一可见的技术字段（用于 UI）。当前值：`pc-agent` / `attached-session` / `claude-code-channel`。提供两个选项：

- **选项 A（推荐，Phase 1–2）：保留 `agentType` 现值不变。** `agentType` 描述的是「这是哪种 agent 产品」（Claude Code / 普通附加 / PC agent），本就**不该**等同于技术 kind。新 agent 自带自己的 `agentType`（如 `codex`）。`agentType` 与 `kind` 正交，无需改 App。
- **选项 B（Phase 3，可选）：新增独立 `protocol` 字段**到 register 响应 / session 列表 / invitation（如 `"protocol":"aspocket.channel.v1"`），`agentType` 保持不变。`protocol` 面向运维/调试，App 可忽略。**不替换 `agentType`**，避免触发 App UI 变更。

> 决策：采用 **A + B**。`agentType` 永远是产品语义、永不收敛为技术 kind；`protocol` 是新增的、可选的技术标识。两者正交。**不把 `agentType` 改成 `callback`/`channel`**——那会让 App UI 失去「这是 Claude Code」的信息，且属于破坏性变更。

### 4.4 为何不破坏 App

App 只用 `remoteAgentId`（路由）、`endpoint`（投递地址）、`displayName`/`description`/`agentType`（UI）。这三者本次设计**一个都不改**。`kind`/`target`/`protocol` 全是 Router 内部或 register 请求体字段，App 不读。因此归一化对已发布 App 零影响。

---

## 5. Router 内部重构建议

> 重构是**手段**，不是本次必须。见 §9：在真正加第二个 channel/callback 型 agent 之前可不做。

目标：把 session resolving/routing 从 `main.py` 拆出，`main.py` 只剩 FastAPI endpoint wiring（认证、请求解析、调用、响应）。

### 5.1 拆出 `runtime/app_agent_router.py`

承载：
- `normalize_session_kind(raw)`
- `resolve_app_remote_agent(remote_agent_id) -> ResolvedRoute`（含 `kind`/`protocol`/`target`/`sessionId`/`status`/`callbackUrl`）
- `get_known_app_remote_agent_ids()`
- kind → 协议映射表

`ResolvedRoute` 用 `runtime_contracts` 里的数据类表达（与现有契约层一致），取代当前裸 dict。

### 5.2 分离三类 handler

- `callback_handler.py`：`_call_attached_session_callback` + callbackUrl 校验（`validate_attached_session_callback_url`）+ callback 路径的 user/reply 落库。
- `channel_handler.py`：inbox 入队 + poll/ack/reply 端点逻辑 + channel 路径落库。
- `internal_handler`：default path 调 ChatRuntime + 落库（可暂留 main.py，量小）。

### 5.3 分离 invitation 构建

`build_app_contact_invitations` 移到 `runtime/app_contact_invitations.py`（或 router 模块内），`main.py` 的 `/api/contact-invitations` 只调用它。`agentType` 计算逻辑集中此处。

### 5.4 main.py 只保留 wiring

每个 endpoint：`_require_chat_auth` → 解析 body → `await router/handler.xxx()` → 组装响应。**认证永远在 body 校验之前**（见 §8.4），这条不变。

---

## 6. 数据 / 状态边界

| 状态          | 位置                                   | 生命周期       | 归一化影响                                   |
|---------------|----------------------------------------|----------------|----------------------------------------------|
| session registry | `AppAgentSessionRegistry`（内存）   | 重启丢失       | `kind` 字段存归一化后的值；旧名读入时归一化   |
| pending inbox | `ChannelPendingInbox`（内存）          | 重启丢失       | 仅 `channel` kind 使用，无变化                |
| app_messages  | SQLite `app_messages` 表                | 持久           | 不依赖 kind；排序已确定性化（commit `a16a1c8`）|
| callbackUrl   | 注册时校验（localhost http only）       | 随 session     | 仅 `callback` kind 要求；`channel` 忽略       |
| channel poll/ack/reply | 专用端点 + kind 守卫           | 随 session     | 守卫改用 `normalize_session_kind(...)=="channel"` |

边界约束（重构须保持）：
- `app_messages` 是唯一持久事实来源，三类 handler 都通过 `insert_app_message` / `insert_app_turn_messages` 写入，`sequence` 语义不变（user=0，reply≥1，同 turn）。
- inbox 与 registry **都是内存态**，不进 DB；重启后 session 需重新注册（现状如此，不变）。
- callbackUrl 的 SSRF 收敛（仅 loopback http）必须在归一化后依然只对 `callback` kind 生效。

---

## 7. 分阶段迁移计划

### Phase 1 —— 内部归一化，外部行为零变化（最小、可单独合入）
- 新增 `normalize_session_kind` helper + 单测。
- 把 `resolve_app_remote_agent`、register 校验、invitation builder、channel 端点守卫改为调用 helper。
- `target` 仍用旧字符串；`agentType` 不变。
- **验收**：现有 `tests/test_app_adapter.py` 全绿（含 `test_get_history_returns_user_and_reply`、channel 系列、attached_session 系列），无任何 response shape 变化。

### Phase 2 —— 文档/测试 rename 到 callback/channel
- 设计文档、e2e 文档、测试注释统一用 `callback`/`channel` 术语；保留「`attached_session` 是 `callback` 的别名」「`claude_channel` 是 `channel` 的别名」说明。
- 测试**断言**仍用旧名入参（验证别名兼容），新增用新名入参的等价测试。
- **验收**：新旧名入参都走同一归一化路径，行为一致。

### Phase 3 —— 可选：response 增 `protocol` 字段
- register 响应 / session 列表 / invitation 增 `"protocol"`（`aspocket.callback.v1` / `aspocket.channel.v1` / `aspocket.internal.v1`）。
- `agentType` 不变。新增字段为可选追加，App 可忽略。
- **验收**：新增字段不影响现有 App 解析；新增 protocol 断言测试。

### Phase 4 —— 移除旧名（仅当 App/clients 全部迁移后）
- 评估是否真有必要：若旧名仅作别名、零维护成本，可**永不移除**（见 §9）。
- 若移除：需确认所有 register 调用方都已用新名，并保留至少一个版本的兼容窗口 + 日志告警。
- **验收**：移除后旧名入参被拒（400）并有清晰错误信息；e2e 文档同步更新。

> 每个阶段都应**独立可合入、独立可回滚**。Phase 1 不依赖后续任何阶段。

---

## 8. 风险

1. **破坏现有 Claude channel 测试**：channel 系列（poll/ack/reply、invitation `agentType=claude-code-channel`）测试较多且贴近线上。缓解：Phase 1 严格保持外部行为，归一化只在内部 helper；用旧名入参的测试一条不改，作为别名兼容的回归网。
2. **改 invitation `agentType` 影响 App UI**：`agentType` 是 App 图标/文案依据。缓解：**绝不改 `agentType`**（§4.3 选项 A）；`agentType` 与 kind 正交。
3. **过度重构 `main.py`**：一次性大拆易引入回归。缓解：拆分按 §5 分模块、分 PR；Phase 1 只加 helper 不拆文件；真正拆分推迟到 §9 触发条件满足。
4. **破坏 auth-before-validation**：当前所有 `/api/*` 端点都是 `_require_chat_auth` 在 `await request.json()` 之前。重构后 endpoint wiring 必须保持此顺序——否则未认证请求能触发 body 解析/DB 写入。缓解：在重构 PR 的 review checklist 里显式列入「认证先于校验」，并加一条针对未认证 401 的测试。
5. **`target` 旧字符串与新 kind 不一致造成认知负担**：Phase 1 保留旧 target 字符串是权衡（减小 diff）但会有一段「kind 已新、target 仍旧」的过渡期。缓解：在 helper 注释里写明映射；Phase 2/3 视情况统一。
6. **协议命名固化过早**：`aspocket.callback.v1` / `aspocket.channel.v1` 一旦写入 response（Phase 3）就成了契约。缓解：Phase 3 是可选的；只有确认需要版本化兜底时才暴露给外部，否则 protocol 只在内部日志/文档用。

---

## 9. Recommendation

- **现在不要重构。** 当前只有两个外部 kind、一套 channel 端点，分支膨胀尚未真实发生。提前拆 `main.py` 是为想象中的第三个 agent 付利息，且引入 §8 的回归风险。**触发条件**：当真要接入第二个 channel 型或 callback 型 agent（如 Codex channel、OpenCode callback）时再做。
- **当重构时，先做 Phase 1**：只加 `normalize_session_kind` helper + 单测，让所有读 kind 的点走它。这是无外部行为变化、可独立合入、可回滚的最小第一步，也是后续一切的前提。
- **旧名别名永久保留**：`attached_session`→`callback`、`claude_channel`→`channel` 作为别名**无限期保留**，用于 dev/本地脚本兼容。移除旧名（Phase 4）收益极小、风险不小，默认不做；除非有明确清理理由并完成迁移窗口。
- **`agentType` 与 `kind` 严格正交**：`agentType` 是产品语义（Claude Code / Codex / pc-agent），`kind`/`protocol` 是技术投递协议。永远不要用技术 kind 去覆盖 `agentType`。
- **保持 auth-before-validation**：任何 endpoint 重构都必须 `_require_chat_auth` 先于 body 解析，并以此作为 review 的硬性 checklist 项。

---

## 10. 已否决 / 暂缓方案

> 依项目规则记录被否决路径及当时约束，便于未来判断前提是否变化。

- **否决：为每个新 agent 加专用 kind（如 `codex_channel`）。** 约束：每加一个 agent 就要在 resolve/register/invitation/端点守卫四处加分支，`main.py` 与测试线性膨胀。前提不变则继续否决。
- **否决：把 `agentType` 收敛成 `callback`/`channel`。** 约束：`agentType` 承载 App UI 语义，改它属破坏性变更，且会让 App 失去「这是哪种 agent」的信息。`agentType` 与 kind 正交才是正确模型。
- **暂缓：立即拆分 `main.py` 到 `app_agent_router.py` / handler 模块。** 约束：当前仅两外部 kind，分支膨胀未真实发生，提前拆分付利息且引入回归风险。触发条件见 §9。
- **暂缓：response 暴露 `protocol` 字段（Phase 3）。** 约束：一旦暴露即成契约。仅当确需版本化兜底时再做；否则 protocol 只在内部/文档使用。
- **否决：把 `target` 在 Phase 1 一起改成新名。** 约束：Phase 1 目标是零外部行为变化的最小归一化，target 改名扩大 diff、增加回归面。Phase 1 保留旧 target 字符串，统一留到后续阶段。

---

## 11. 变更点速查（实施时用）

Phase 1 涉及代码点（仅供参考，本文不改代码）：

- `main.py:resolve_app_remote_agent` —— `kind = session.get("kind") or "attached_session"` → `normalize_session_kind(session.get("kind"))`；`target` 派生改用归一化 kind。
- `main.py:api_app_agent_session_register` —— `kind = raw_kind if raw_kind in (...) else "attached_session"` → `normalize_session_kind(raw_kind)`。
- `main.py:build_app_contact_invitations` —— `kind = sess.get("kind") or "attached_session"` → 归一化；`agentType` 计算逻辑**不变**（仍 `claude-code-channel` / `attached-session`）。
- `main.py` channel 端点守卫（poll/ack/reply）—— `(session.get("kind") or "attached_session") != "claude_channel"` → `normalize_session_kind(session.get("kind")) != "channel"`。
- `runtime/app_agent_session_registry.py:register` —— `if kind != "claude_channel": kind = "attached_session"` → `normalize_session_kind(kind)`。
- 新增 `runtime/app_agent_router.py`（或在 main.py 内先放 helper）—— `normalize_session_kind` + 单测。
