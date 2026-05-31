# Architecture — 系统架构图

> 生成日期: 2026-05-31
> 基于实际代码 (v1.5 strict CC causal chain)

---

## 1. 系统模块图

```mermaid
graph TD
    WX[微信用户] -->|iLink API| IL[bridge/ilink_client.py<br/>7 HTTP 端点<br/>get_updates · send_message · get_qr_code]
    IL <--> WP[bridge/wechat_poller.py<br/>WeChatBridge<br/>长轮询 · 缓冲去抖 · seq 管理]
    WP -->|POST /chat| MAIN

    subgraph MAIN["main.py — FastAPI (30+ 路由)"]
        direction TB
        CHAT["/chat 端点<br/>认证 · 锁管理 · 中断检查 · 历史管理"]
        ADMIN["/admin/*<br/>面板 · 设置 · 记忆 · 诊断"]
        WECHAT["/wechat/*<br/>QR 登录 · 桥接控制"]
        ORCH["编排层<br/>wechat_sender 回调 · 中断检查器工厂<br/>post-terminal 回调构造 · 对话历史管理<br/>记忆导入/导出 · 管理面板数据聚合"]
        CHAT --> ORCH
    end

    MAIN --> AGENT
    MAIN --> MEMORY
    MAIN --> PROMPTS["prompts/<br/>persona.md<br/>base_protocol.md<br/>tool_policy.md ..."]
    MAIN --> CONFIG["config.yaml<br/>+.env+runtime_settings"]

    subgraph AGENT["agent/ — 核心 AI 逻辑"]
        direction TB
        CA["chat_agent.py<br/>状态机主循环 (23+ state handler)<br/>模型路由 (v1.3) · post-terminal pipeline"]
        SM["state_machine.py<br/>TurnState · TurnScratchpad<br/>TurnResult · VisibleMessage · DeliveryStatus"]
        TC["tool_classifier.py<br/>FACT_TOOL · ACTION_TOOL<br/>TERMINAL_TOOL · STATE_TOOL"]
        TR["tool_registry.py<br/>ToolDef · ActionDef · TOOL_REGISTRY"]
        TL["tools.py<br/>get_time · get_weather · execute_tool"]
        OG["output_guard.py<br/>协议泄露检测 · 敏感信息过滤 · batch guard"]
        CB["context_builder.py<br/>compact_history · 系统提示词构建 · token 估算"]

        CA --> SM
        CA --> TC
        TC --> TR
        TR --> TL
        CA --> OG

        subgraph LLM["llm_gateway.py — Provider-neutral LLM 网关"]
            GW["LLMGateway"]
            BASE["providers/base<br/>ProviderConfig · Protocol"]
            DS["providers/deepseek.py<br/>DeepSeekAdapter<br/>AsyncOpenAI → DeepSeek API"]
            GW --> BASE
            DS -.->|implements| BASE
        end
        CA --> GW

        MW["memory_worker.py<br/>异步记忆提取 · fire-and-forget"]
        MH["memory_hook.py<br/>load_context · update"]
        MW --> GW
    end

    subgraph MEMORY["memory/ — 持久化层"]
        MS["store.py<br/>MemoryStore · SQLite WAL · 15+ 表"]
        DD["dedup.py<br/>Jaccard 相似度 · CJK 二元组 · 去重/重要度"]
        MS --> DD
    end

    MW --> MS
    MH --> MS

    subgraph DATA["data/ — 运行时数据 (gitignore)"]
        DB["memory.db · agent.log<br/>runtime_settings.json · backups/"]
    end
    MS --> DB
```

---

## 2. 主链路流程图

一次微信消息从接收到回复的完整链路:

```mermaid
flowchart TD
    A[微信用户发送消息] --> B["WeChatBridge._poll_loop()<br/>bridge/wechat_poller.py"]
    B --> B1["iLink get_updates()"]
    B1 --> B2[缓冲去抖]
    B2 --> B3["flush → POST /chat<br/>{messages, user_id, input_latest_seq}"]

    B3 --> C["POST /chat<br/>main.py:2030"]
    C --> C1["_require_chat_auth() — API key 验证"]
    C1 --> C2["_normalize_chat_messages() — 标准化"]
    C2 --> C3["_validate_chat_user_id()"]
    C3 --> C4["_record_user_message_arrival(seq) — 中断基础"]
    C4 --> C5["_get_user_lock(user_id) — per-user 锁"]

    C5 --> D{"handle_pending_<br/>persona_confirmation()?"}
    D -->|有 pending| D1[快速路径返回]
    D -->|无 pending| E

    E["预处理 (锁内)"] --> E1["_select_route()"]
    E1 -->|空消息| E2["SystemFastPath"]
    E1 -->|≤30字| E3["ChatLite"]
    E1 -->|其他| E4["ChatFull"]

    E --> E5["history = get_history_async()"]
    E5 --> E6["system_prompt = build_enriched_system_prompt()"]
    E6 --> E7["interrupt_checker = _make_interrupt_checker()"]
    E7 --> E8["_wmh_cb + _hook_cbs 构造"]

    E8 --> F["chat_agent.chat()<br/>agent/chat_agent.py:146"]
    F --> F1["turn = TurnScratchpad(...)"]
    F1 --> F2["state = RECEIVE_EVENT"]

    F2 --> SM

    subgraph SM["状态机主循环"]
        direction TB
        S1["RECEIVE_EVENT"] --> S2["CREATE_TURN"]
        S2 --> S3{"INTERRUPT_CHECK"}
        S3 -->|被中断| S3C["END_CANCELLED"]
        S3 -->|通过| S4["BUILD_CONTEXT"]
        S4 --> S5{"CLASSIFY_CONTEXT_ERROR"}
        S5 -->|FATAL| S5F["HARD_FALLBACK"]
        S5 -->|OK| S6["OPTIONAL_PREPROCESS<br/>(预取时间)"]
        S6 --> S7["CALL_LLM"]
        S7 --> S8["HANDLE_MODEL_OUTPUT"]
        S8 --> S9["CLASSIFY_ONE_TOOL_CALL"]

        S9 -->|FACT_TOOL| FT["EXEC_FACT_TOOLS<br/>(get_time/get_weather)"]
        FT --> FT1["结果回灌 LLM"]
        FT1 --> S7

        S9 -->|ACTION_TOOL| AT["VALIDATE_REPLY_ARGS"]
        AT --> AT1["GUARD_OUTPUT"]
        AT1 --> AT2{"PRE_SEND_<br/>INTERRUPT_CHECK"}
        AT2 -->|被中断| AT2C["END_SUPERSEDED"]
        AT2 -->|通过| AT3["EXEC_SEND_ONE<br/>(wechat_sender)"]
        AT3 --> AT4["APPEND_SEND_RESULT"]
        AT4 --> AT5["POST_SEND_THINKING_DELAY"]
        AT5 --> AT6{"POST_SEND_<br/>INTERRUPT_CHECK"}
        AT6 -->|被中断| AT6C["END_INTERRUPTED"]
        AT6 -->|通过| AT7{"LIMIT_CHECK<br/>≥ MAX_REPLY?"}
        AT7 -->|是| AT7L["END_MAX_LIMIT"]
        AT7 -->|否| S7

        S9 -->|TERMINAL_TOOL| TT["VALIDATE_DONE_ARGS"]
        TT --> TT1["END_REPLIED<br/>END_NO_REPLY"]

        S5F --> HF1["INTERRUPT_CHECK"]
        HF1 --> HF2["guard('我刚刚卡了一下 重新说')"]
        HF2 --> HF3["EXEC_SEND_ONE"]
        HF3 --> HF4["END_FALLBACK_SENT / END_FAILED"]
    end

    SM -->|terminal state 确定| PT

    subgraph PT["Post-terminal Pipeline"]
        direction TB
        PT1["1. WRITE_MODEL_HISTORY<br/>写 user 消息 → conversation_history<br/>按终态矩阵写 assistant 消息<br/>source: assistant / assistant:fallback"]
        PT2["2. POST_TURN_HOOKS (串行, 单失败不阻塞)<br/>_hook_memory → 异步记忆提取<br/>_hook_dev_logs → turn_diagnostics + usage"]
        PT1 --> PT2
    end

    PT --> G["TurnResult 组装返回 main.py"]
    G --> G1["记录 L2 turn → create_turn()"]
    G1 --> G2["记录 outbound → record_outbound_messages()"]
    G2 --> G3["返回 JSONResponse"]
    G3 --> H[微信用户收到回复<br/>多条消息逐条发送]
```

---

## 3. Tool Call 循环图

v1.5 严格 CC 因果链: 每次 LLM 响应只允许一个 tool call, `wechat_reply` 是 action (非终态), `done` 是唯一终态工具。

```mermaid
flowchart TD
    LLM["CALL_LLM<br/>_do_call_llm()"] -->|LLM 响应| HMO["HANDLE_MODEL_OUTPUT<br/>_do_handle_model_output()"]

    HMO -->|"有 content, 无 tool_calls"| PR["PROTOCOL_RETRY"]
    HMO -->|空响应| PR
    HMO -->|"有 tool_calls"| COC["CLASSIFY_ONE_TOOL_CALL<br/>classify_single_tool_call()"]

    COC -->|"FACT_TOOL<br/>get_time / get_weather"| FACT
    COC -->|"ACTION_TOOL<br/>wechat_reply"| ACTION
    COC -->|"TERMINAL_TOOL<br/>done"| TERM
    COC -->|未知/废弃工具| PR

    subgraph FACT["FACT_TOOL 路径"]
        direction TB
        F1["EXEC_FACT_TOOLS<br/>asyncio.gather 并行执行"]
        F2["execute_tool()<br/>· _get_time()<br/>· _get_weather()"]
        F3["结果写入 turn.tool_results"]
        F1 --> F2 --> F3
    end
    F3 -->|结果回灌 LLM| LLM

    subgraph ACTION["ACTION_TOOL 路径 (wechat_reply)"]
        direction TB
        A1["VALIDATE_REPLY_ARGS<br/>检查 text 非空"]
        A2["GUARD_OUTPUT<br/>guard_single_message()<br/>· 协议泄露检测<br/>· 敏感信息过滤<br/>· 长度截断"]
        A3{"PRE_SEND_INTERRUPT_CHECK"}
        A3 -->|"被中断"| A3X["END_SUPERSEDED_BEFORE_SEND"]
        A3 -->|通过| A4["EXEC_SEND_ONE<br/>wechat_sender()<br/>→ bridge.send_text()<br/>→ ilink API"]
        A5["APPEND_SEND_RESULT<br/>记录送达状态:<br/>SENT_CONFIRMED / FAILED_CONFIRMED / RATE_LIMITED"]
        A6["POST_SEND_THINKING_DELAY<br/>可中断延迟 (模拟打字时间)"]
        A7{"POST_SEND_INTERRUPT_CHECK"}
        A7 -->|"被中断"| A7X["END_INTERRUPTED_DURING_REPLY_SEQUENCE"]
        A7 -->|通过| A8{"LIMIT_CHECK<br/>已发 ≥ MAX_REPLY?"}
        A8 -->|是| A8X["END_MAX_LIMIT"]
        A8 -->|否| A9["继续 LLM 循环"]

        A1 --> A2 --> A3
        A4 --> A5 --> A6 --> A7
    end
    A9 --> LLM

    subgraph TERM["TERMINAL_TOOL 路径 (done)"]
        direction TB
        T1["VALIDATE_DONE_ARGS<br/>检查 status"]
        T1 --> T2["END_REPLIED"]
        T1 --> T3["END_NO_REPLY"]
    end

    PR -->|带错误提示, max 1 次| LLM

    subgraph FALLBACK["回退路径"]
        direction TB
        HF["HARD_FALLBACK<br/>触发: LLM 超时 / API 错误 /<br/>上下文 FATAL / 连续协议违规"]
        HF --> HF1["INTERRUPT_CHECK"]
        HF1 --> HF2["guard('我刚刚卡了一下 重新说')"]
        HF2 --> HF3["EXEC_SEND_ONE"]
        HF3 --> HF4["END_FALLBACK_SENT / END_FAILED"]

        FP["FLASH_TO_PRO_UPGRADE<br/>触发: flash 模型失败"]
        FP --> FP1["CALL_LLM (用 pro 模型)"]
    end
```

### Tool Call 循环的关键约束

| 约束 | 值 |
|------|-----|
| 每次 LLM 响应最多 tool calls | 1 (严格单工具) |
| MAX_LLM_STEPS_PER_TURN | 5 |
| MAX_PROTOCOL_RETRIES | 1 |
| MAX_REPLY_MESSAGES_PER_TURN | 8 |
| MAX_SEND_FAILURES_PER_TURN | 3 |
| turn timeout | 可配置 |
| FACT_TOOL 结果 | 回灌 LLM |
| ACTION_TOOL 结果 | 继续 LLM 循环 |
| TERMINAL_TOOL (done) | 结束 turn |
| PROTOCOL_RETRY 最多 | 1 次 |
| HARD_FALLBACK | 使用固定文案 |
| send_wechat_messages | 已废弃 (v1.5) |
| skip_reply | 已废弃 (v1.5) |
