# Proactive Test Console — 人话化 UI / 报告层优化计划

> 生成日期: 2026-05-31
> 范围: 仅前端 JS + 少量后端 report 字段补充, 不改主动消息核心逻辑
> 目标: 不需要理解工程字段也能看懂一次主动消息测试发生了什么

---

## 0. 需要修改的文件

| 文件 | 改动量 | 说明 |
|------|--------|------|
| `agent/proactive_debug.py` | 小 | 补充 `generated_messages`, `guard_summary`, `next_step_suggestions` 字段; 修复 `guard=send_json` bug |
| `static/proactive_test_console.html` | 中 | 新增 suggestion 区域, 调整 panel 结构 |
| `static/proactive_test_console.js` | **大** | 核心修改: 5 层视图, 字段翻译, precheck/execution 分离, 文案展示, 建议生成 |
| `static/proactive_test_console.css` | 小 | 新增少量样式 (card grid, suggestion box, message bubble, fold-toggle) |

不修改: `admin_proactive_test_routes.py`, `agent/scheduler.py`, `agent/chat_agent.py`, `agent/state_machine.py`, `memory/store.py`

---

## 1. 后端修改 (agent/proactive_debug.py)

### 1.1 ProactiveDebugReport 新增字段

```python
# 新增字段
generated_messages: list[str] = field(default_factory=list)
    # 模型实际生成的主动消息文案 (dry-run: 模拟送达的文本, real-send: 确认送达的文本)

next_step_suggestions: list[str] = field(default_factory=list)
    # 人话版下一步建议

guard_summary: dict[str, Any] = field(default_factory=dict)
    # 人话版 guard 摘要 (替代原来 guard=send_json 的 bug)
```

### 1.2 generate_report() 修正

**修复 bug**: `guard=send_json` (line 510) → guard 是覆盖 send_json 的，完全错误。

```python
# 修正后:
guard_summary = {
    "proactive_guard": "pass",       # 从 decision_chain 的 GUARD stage 提取
    "general_guard": "pass",
}
# generated_messages 从 TurnResult.proactive_delivered_texts 提取
generated_messages = list(getattr(turn_result, "proactive_delivered_texts", []))
```

**修复 bug**: summary 中的 `guard 通过` 在 guard blocked 时应该说 `guard 拦截`。

```python
if execution_skip_reason:
    summary = f"已跳过：{execution_skip_reason}"
elif is_dry:
    guard_note = "guard 拦截" if guard_blocked else "guard 通过"
    summary = f"Dry-run 成功：生成 {delivered} 条主动消息，{guard_note}，未发送微信，未写入可见历史。"
elif is_real:
    if t_state and "REPLIED" in t_state:
        summary = f"Real-send 成功：真实发送 {delivered} 条，写入历史 {history_count} 条。"
    elif delivered > 0:
        summary = f"Real-send 部分成功：送达 {delivered} 条，写入历史 {history_count} 条，终端 {t_state}。"
    else:
        summary = f"Real-send 未送达：终端 {t_state}，送达 0 条。"
else:
    summary = "未执行"
```

### 1.3 next_step_suggestions 生成逻辑

在 `generate_report()` 末尾，根据结果生成建议:

```python
def _build_suggestions(report: ProactiveDebugReport) -> list[str]:
    suggestions = []
    
    # 从 eligibility checks 判断
    if not report.precheck_passed:
        checks = (report.eligibility or {}).get("checks", [])
        for c in checks:
            if c.get("name") == "features.proactive" and not c.get("passed"):
                suggestions.append("主动消息功能未开启：请将 config.yaml 中 features.proactive 设为 true")
            if c.get("name") == "user_silent_window" and not c.get("passed"):
                suggestions.append("用户静默窗口未达标：可填写 simulate_user_silent_seconds=28800 模拟长时间静默")
            if c.get("name") == "sleep_hours" and not c.get("passed"):
                suggestions.append("当前处于睡眠时段：可设置 debug_now 为白天时间再试")
            if c.get("name") == "bridge_online" and not c.get("passed"):
                suggestions.append("微信 bridge 离线：请先部署/启动微信桥接")
            if c.get("name") == "daily_limit" and not c.get("passed"):
                suggestions.append("今日发送已达上限：可修改 daily_cap 或等待明天")
    
    # 从 impulse 判断
    if report.precheck_eligibility and report.precheck_eligibility.get("eligible"):
        if not report.selected_impulse:
            suggestions.append("没有选中主动理由：试试 forced_impulse_type=PRESENCE 并填写 simulate_user_silent_seconds，或在微信中发几条带情绪的消息制造 ECHO")
        if report.candidates and not report.selected_impulse:
            suggestions.append("候选被过滤：展开「主动理由过滤」raw 查看 dedup/expire/score/threshold 详情")
    
    # 从 guard 判断
    if report.decision_chain:
        guard_stages = [s for s in report.decision_chain if s.get("stage") == "guard"]
        if guard_stages and guard_stages[0].get("status") == "block":
            suggestions.append("文案被 guard 拦截：可能包含编造观察/系统味/通知味，展开 raw 查看具体拦截原因")
    
    # Dry-run success
    if report.mode == "dry_run" and report.simulated_delivered_count > 0:
        suggestions.append("Dry-run 成功：检查上方生成的文案，确认像真人聊天后可执行 real-send")
    
    # Real-send
    if report.mode == "real_send":
        if report.real_delivered_count > 0:
            suggestions.append("Real-send 成功：请去微信确认收到消息，并检查 history_written_count 是否与 delivered_count 一致")
        elif report.history_written_count == 0:
            suggestions.append("未写入历史：可能发送失败或 guard 拦截，检查 terminal_state 和 send 详情")
    
    return suggestions
```

### 1.4 to_dict() 同步新增字段

```
"generated_messages": self.generated_messages,
"next_step_suggestions": self.next_step_suggestions,
"guard_summary": self.guard_summary,
```

---

## 2. 前端 JS 修改 (static/proactive_test_console.js)

### 2.1 字段翻译表

```javascript
var STAGE_LABELS = {
  bootstrap: '启动',
  eligibility: '资格检查',
  impulse_generation: '主动理由生成',
  impulse_filtering: '主动理由过滤',
  impulse_selection: '主动理由选择',
  proactive_context_build: '上下文构建',
  llm_step: '模型调用',
  model_tool_call: '工具调用',
  guard: '文案安全检查',
  send: '发送检查',
  history_write: '历史写入检查',
  terminal: '本轮结束状态'
};

var STAGE_STATUS_LABELS = {
  pass: '通过', ok: '通过', done: '完成', written: '已写入',
  fail: '未通过', block: '已拦截', empty: '无结果',
  simulated: '已模拟', skip: '已跳过', not_written: '未写入', skipped: '已跳过',
  end: '结束'
};

var REASON_LABELS = {
  'no_impulse_selected': '没有选中主动理由',
  'proactive feature is disabled': '主动消息功能未开启',
  'eligibility_failed': '资格检查未通过',
  'dry_run_never_writes_visible_history': 'Dry-run 模式不写入可见历史',
  'active_proactive_turn_running': '另一个主动回合正在执行',
  'no candidate passed filters': '所有候选被过滤（去重/过期/评分不达标）',
  'no candidate passed score threshold': '候选评分未达阈值',
  'no candidates generated': '未生成任何候选',
  'user_silent_window_not_met': '用户静默窗口未达标',
  'sleep_hours': '当前处于睡眠时段',
  'daily_limit_reached': '今日发送已达上限',
  'min_interval_not_met': '距上次主动消息太近'
};
```

### 2.2 5 层视图渲染

```
renderReport(report):
  1. 一句话结论 (summary bar, 已存在, 修改 class 逻辑)
  2. 核心结果卡片 (新增, card grid 6 项)
  3. 生成的主动消息文案 (新增, message bubbles)
  4. 人话版原因解释 (新增, precheck + execution 分离)
  5. 工程详情折叠区 (保留现有 chain + raw, 默认折叠)
  6. 下一步建议 (新增, suggestion box)
```

### 2.3 核心结果卡片 (第 2 层)

```html
<div class="result-cards">
  <div class="rcard ${elig_ok ? 'rcard-pass' : 'rcard-fail'}">
    <div class="rcard-label">资格检查</div>
    <div class="rcard-value">${elig_ok ? '通过' : '失败'}</div>
  </div>
  <div class="rcard">
    <div class="rcard-label">主动理由</div>
    <div class="rcard-value">${actual_impulse_type || '无'}</div>
  </div>
  <div class="rcard ${msg_count > 0 ? 'rcard-pass' : 'rcard-gray'}">
    <div class="rcard-label">生成消息</div>
    <div class="rcard-value">${msg_count} 条</div>
  </div>
  <div class="rcard ${guard_ok ? 'rcard-pass' : 'rcard-fail'}">
    <div class="rcard-label">Guard</div>
    <div class="rcard-value">${guard_label}</div>
  </div>
  <div class="rcard ${send_ok ? 'rcard-pass' : 'rcard-yellow'}">
    <div class="rcard-label">发送</div>
    <div class="rcard-value">${send_label}</div>
  </div>
  <div class="rcard ${hw_ok ? 'rcard-pass' : 'rcard-yellow'}">
    <div class="rcard-label">历史写入</div>
    <div class="rcard-value">${hw_label}</div>
  </div>
</div>
```

卡片取值逻辑:

```javascript
var eligOk = report.precheck_passed;
var impulseLabel = report.actual_impulse_type || report.selected_impulse?.impulse_type || '无';
var msgCount = report.generated_messages?.length || report.simulated_delivered_count || report.real_delivered_count || 0;
var guardOk = true;  // 从 decision_chain guard stage 判断
var guardLabel = report.mode === 'dry_run' ? 'dry-run 已模拟' : (guardOk ? '通过' : '拦截');
var sendOk = report.real_send_attempted ? (report.real_delivered_count > 0) : false;
var sendLabel;
if (report.mode === 'dry_run') {
  sendLabel = 'dry-run 未真实发送';
} else if (report.real_send_attempted) {
  sendLabel = report.real_delivered_count > 0 ? '真实发送成功 (' + report.real_delivered_count + ' 条)' : '发送失败';
} else {
  sendLabel = '未执行';
}
var hwLabel;
if (report.mode === 'dry_run') {
  hwLabel = '未写入（dry-run）';
} else if (report.history_written_count > 0) {
  hwLabel = '已写入 ' + report.history_written_count + ' 条';
} else {
  hwLabel = '未写入（' + (report.history_write?.reason || '原因未知') + '）';
}
```

### 2.4 生成消息文案展示 (第 3 层)

```html
<div class="messages-section">
  <h3>本次生成的主动消息（模型实际想发给用户的文案）</h3>
  ${report.generated_messages.length > 0
    ? report.generated_messages.map(function(m) {
        return '<div class="msg-bubble">' + esc(m) + '</div>';
      }).join('')
    : '<p class="dim">未生成任何消息（模型调用了 skip_proactive）</p>'
  }
</div>
```

### 2.5 人话版原因解释 (第 4 层) — Precheck / Execution 分离

```html
<div class="explain-section">
  <h3>预检查结果</h3>
  <div class="explain-precheck">
    ${renderPrecheck(report)}
  </div>

  <h3>正式执行结果</h3>
  <div class="explain-execution">
    ${renderExecution(report)}
  </div>
</div>
```

Precheck 渲染:
```javascript
function renderPrecheck(report) {
  var html = '';
  if (report.precheck_eligibility) {
    var eligChecks = (report.precheck_eligibility.checks || []);
    for (var i = 0; i < eligChecks.length; i++) {
      var c = eligChecks[i];
      html += '<div class="check-row"><span class="check-icon ' + (c.passed ? 'c-pass' : 'c-fail') + '">' + (c.passed ? '✓' : '✗') + '</span>';
      html += '<span>' + esc(c.name) + '</span>';
      if (!c.passed && c.reason) html += '<span class="check-reason"> — ' + esc(c.reason) + '</span>';
      html += '</div>';
    }
    // Show impulse result
    if (report.precheck_selected_impulse) {
      html += '<div class="check-row"><span class="check-icon c-pass">✓</span><span>主动理由: ' + esc(report.precheck_selected_impulse.impulse_type || '') + '</span></div>';
    } else if (report.precheck_eligibility.eligible) {
      html += '<div class="check-row"><span class="check-icon c-fail">✗</span><span>主动理由: 无候选被选中</span></div>';
      if (report.candidates && report.candidates.length > 0) {
        html += '<div class="check-detail">候选列表: ' + report.candidates.map(function(c){return c.type}).join(', ') + '</div>';
      }
    }
  }
  return html;
}
```

Execution 渲染:
```javascript
function renderExecution(report) {
  var html = '';
  if (report.execution_skip_reason) {
    html += '<p class="exec-skip">执行被跳过：' + esc(report.execution_skip_reason) + '</p>';
  }
  if (report.terminal_state) {
    html += '<p>终端状态: ' + esc(report.terminal_state) + '</p>';
  }
  // Show each tool call result
  if (report.tool_calls && report.tool_calls.length > 0) {
    for (var i = 0; i < report.tool_calls.length; i++) {
      var tc = report.tool_calls[i];
      html += '<p>工具调用: ' + esc(tc.name || '') + ' → ' + esc(JSON.stringify(tc.result || '')) + '</p>';
    }
  }
  return html;
}
```

### 2.6 下一步建议 (第 6 层)

```html
${report.next_step_suggestions && report.next_step_suggestions.length > 0
  ? '<div class="suggestion-box"><h3>下一步建议</h3><ul>' +
    report.next_step_suggestions.map(function(s) { return '<li>' + esc(s) + '</li>'; }).join('') +
    '</ul></div>'
  : ''
}
```

### 2.7 工程详情折叠区 (第 5 层)

保留现有效果, 但默认全部折叠 (`<details>` 标签默认 closed):
- Decision chain: `<details><summary>决策链 (工程详情)</summary>...`
- Raw JSON: `<details><summary>完整 Raw JSON</summary><pre>...`
- 主动理由候选列表: `<details><summary>主动理由候选详情</summary>...`

### 2.8 区分 "主动理由候选" 和 "生成消息"

- Impulse 区域: 标题明确写 "系统想主动发的原因 (Impulse Candidates)"
- 消息区域: 标题明确写 "模型实际生成的微信消息 (Generated Messages)"
- 两者的数据来源不同: impulse 来自 `report.candidates`, messages 来自 `report.generated_messages`

---

## 3. 前端 HTML 修改

### 3.1 新增 suggestion box 区域

在 `labSummary` 下方添加:
```html
<div id="suggestionsBox" class="suggestion-box" style="display:none"></div>
```

### 3.2 调整 panel 结构

将原来的 `eligPanel` + `impulsePanel` + `chainPanel` + `historyPanel` 合并为更清晰的结构:

```html
<!-- 核心结果卡片 (新增) -->
<section class="panel" id="resultCardsPanel" style="display:none">
  <h2>核心结果</h2>
  <div id="resultCards"></div>
</section>

<!-- 生成消息文案 (新增) -->
<section class="panel" id="messagesPanel" style="display:none">
  <h2>生成的主动消息</h2>
  <div id="messagesResult"></div>
</section>

<!-- 原因解释 (重构) -->
<section class="panel" id="explainPanel" style="display:none">
  <h2>原因解释</h2>
  <div id="explainResult"></div>
</section>

<!-- 工程详情 (重构, 默认折叠) -->
<section class="panel" id="detailsPanel" style="display:none">
  <h2>工程详情</h2>
  <div id="detailsResult"></div>
</section>
```

---

## 4. 前端 CSS 新增样式

```css
/* Result cards grid */
.result-cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 10px; margin-bottom: 12px; }
.rcard { background: #f9f9f9; border-radius: 6px; padding: 12px; text-align: center; }
.rcard-label { font-size: 11px; color: #888; margin-bottom: 4px; }
.rcard-value { font-size: 16px; font-weight: 600; }
.rcard-pass { border-left: 3px solid #2e7d32; }
.rcard-fail { border-left: 3px solid #c62828; }
.rcard-yellow { border-left: 3px solid #f57f17; }
.rcard-gray { border-left: 3px solid #999; }

/* Message bubbles */
.msg-bubble { background: #e8f5e9; border-radius: 8px; padding: 10px 14px; margin: 6px 0; font-size: 14px; max-width: 100%; }

/* Check rows */
.check-row { padding: 4px 0; font-size: 13px; display: flex; align-items: center; gap: 6px; }
.check-icon { width: 18px; text-align: center; font-weight: 600; }
.c-pass { color: #2e7d32; }
.c-fail { color: #c62828; }
.check-reason { color: #c62828; font-size: 12px; }
.check-detail { font-size: 12px; color: #888; padding-left: 24px; }

/* Execution */
.exec-skip { color: #f57f17; font-weight: 500; }

/* Suggestion box */
.suggestion-box { background: #e3f2fd; border-radius: 6px; padding: 12px 16px; margin-top: 12px; }
.suggestion-box h3 { font-size: 13px; margin-bottom: 6px; color: #1565c0; }
.suggestion-box li { font-size: 13px; margin: 4px 0; }

/* Explain section */
.explain-precheck, .explain-execution { margin-bottom: 12px; }

/* Details always start collapsed */
.details-fold > summary { cursor: pointer; font-size: 13px; color: #888; }
.details-fold > summary:hover { color: #333; }
```

---

## 5. "send_simulated" 歧义修正

当前 `send_simulated: YES` 出现在表格中容易误导 (让人以为"发送被模拟了, 没真发", 但其实 dry-run 里确实没真发所以是对的, 只是命名混淆)。

修正: 展示时用更清晰的标签:

| 原字段 | 人话标签 | dry-run 显示 | real-send 显示 |
|--------|---------|-------------|---------------|
| `mode` | 执行模式 | dry-run (未真实发送) | real-send (已真实发送) |
| `send_simulated` | 是否模拟发送 | 是 (模拟 delivered) | 否 |
| `real_send_attempted` | 是否调用真实微信 | 否 | 是 |
| `real_delivered_count` | 真实送达条数 | — (dry-run 不适用) | N 条 |
| `simulated_delivered_count` | 模拟送达条数 | N 条 | — (real-send 不适用) |

前端 JS 中统一替换展示逻辑, 不修改后端字段名。

---

## 6. Bug 修复清单

| # | Bug | 修复 |
|---|-----|------|
| 1 | `guard=send_json` (line 510) — guard 字段被 send 数据覆盖 | 后端: `generate_report()` 中 guard 从 decision_chain 提取 |
| 2 | summary 在 guard blocked 时仍说 "guard 通过" | 后端: 判断 guard_blocked 后改 summary |
| 3 | Precheck eligibility 和 Execution 混在同一个决策链中 | 前端: 拆分 renderPrecheck / renderExecution |
| 4 | 生成消息只显示 "N 条" 无正文 | 后端: 补充 `generated_messages` 字段; 前端: 展示气泡 |
| 5 | "send_simulated: YES" 展示歧义 | 前端: 改为人话标签 (见 §5) |
| 6 | Raw JSON 默认展开, 淹没主要信息 | 前端: 默认折叠 |
| 7 | sent_message_count 和目标文案没有对应展示, 无法判断 "模型想说的话" 和 "实际 WX 对话对应" | 后续测试场景: delivery_status列表 与目标文案的文案要对应 |

---

## 7. 实施顺序

1. **后端补字段** (`agent/proactive_debug.py`): 新增 `generated_messages`, `next_step_suggestions`, `guard_summary`; 修复 `guard=send_json` bug; 改进 summary 文本和 `_build_suggestions()`
2. **前端 JS 重写渲染**: 5 层视图 + 翻译表 + precheck/execution 分离 + 建议 + 默认折叠
3. **前端 HTML/CSS**: 新增 card/suggestion/bubble 结构和样式
4. **验证**: 跑 test_proactive.py 确保后端改动不破坏现有逻辑; 手工检查前端

不写新测试 (只改展示层)。

---

## 8. 本期明确不做

- 不引入任何前端框架 (React/Vue/jQuery)
- 不美化视觉 (颜色/字体/动画/主题)
- 不修改 ChatAgent / ProactiveScheduler / guard / sender 核心逻辑
- 不修改后端 API 路由签名
- 不做国际化 (i18n)
- 不添加图表
- 不修改 `proactive_debug_runs` 表结构 (新增字段通过 `to_dict()` 携带, 不要求 DB migration)
