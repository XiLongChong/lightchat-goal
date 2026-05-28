const $ = (id) => document.getElementById(id);

let current = null;
let dashboard = null;
let memories = [];
let bots = null;
let deployPollTimer = null;
let deployQrCode = "";
let deployPollInFlight = false;
let logPollTimer = null;
let logIssuesOnly = false;
let activeTab = "overview";
let chatFilter = "all";

const pageTitles = {
  overview: "LightChat · 总览",
  chat: "LightChat · 聊天诊断",
  proactive: "LightChat · 主动消息",
  memory: "LightChat · 记忆",
  emotion: "LightChat · 情绪/对话",
  settings: "LightChat · 设置",
  logs: "LightChat · 日志",
};

function toast(text) {
  const el = $("toast");
  el.textContent = text;
  el.hidden = false;
  setTimeout(() => {
    el.hidden = true;
  }, 2400);
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && !path.includes("/admin/auth/")) {
    showLogin();
    throw new Error(data.detail || "请先登录");
  }
  if (!res.ok) throw new Error(data.detail || "请求失败");
  return data;
}

async function apiRaw(path, options = {}) {
  const res = await fetch(path, {
    credentials: "same-origin",
    ...options,
  });
  const text = await res.text();
  let data = {};
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { detail: text };
  }
  if (res.status === 401) {
    showLogin();
    throw new Error(data.detail || "请先登录");
  }
  if (!res.ok) throw new Error(data.detail || "请求失败");
  return data;
}

function showLogin() {
  $("loginView").hidden = false;
  $("appView").hidden = true;
  $("loginPassword").focus();
}

function showApp() {
  $("loginView").hidden = true;
  $("appView").hidden = false;
}

async function checkAuth() {
  const data = await api("/admin/auth/status");
  if (data.authenticated) {
    showApp();
    await loadAll();
  } else {
    showLogin();
  }
}

async function login() {
  const btn = $("loginBtn");
  btn.disabled = true;
  btn.textContent = "登录中";
  try {
    await api("/admin/auth/login", {
      method: "POST",
      body: JSON.stringify({ password: $("loginPassword").value }),
    });
    $("loginPassword").value = "";
    $("loginError").hidden = true;
    showApp();
    await loadAll();
  } catch {
    $("loginError").textContent = "密码不正确";
    $("loginError").hidden = false;
  } finally {
    btn.disabled = false;
    btn.textContent = "进入后台";
  }
}

async function logout() {
  await api("/admin/auth/logout", {
    method: "POST",
    body: JSON.stringify({}),
  });
  showLogin();
}

async function loadAll() {
  await loadConfig();
  await Promise.all([loadDashboard(), loadMemories(), loadBots()]);
  renderMemory();
  renderBotSelect();
  if (activeTab === "logs") await loadLogs();
}

async function loadConfig() {
  current = await api("/admin/config");
  $("personaText").value = current.persona || "";

  const scheduler = current.settings.scheduler || {};
  const proactive = current.settings.proactive || {};
  const timing = proactive.timing || {};
  const generation = proactive.generation || {};
  setValue("schedulerEnabled", scheduler.enabled ? "true" : "false");
  setValue("intervalMinutes", scheduler.interval_minutes);
  setValue("quietHoursEnabled", scheduler.quiet_hours_enabled ? "true" : "false");
  setValue("minProactiveInterval", timing.min_interval_minutes);
  setValue("maxProactiveInterval", timing.max_interval_minutes);
  setValue("maxDailyProactive", scheduler.max_daily_proactive);
  setValue("scheduledCheckSeconds", scheduler.scheduled_check_seconds);
  setValue("sleepStart", scheduler.sleep_start);
  setValue("sleepEnd", scheduler.sleep_end);

  // Clingy settings (independent section)
  const clingy = current.settings.clingy || {};
  setValue("clingyEnabled", current.settings?.proactive?.generation?.clingy_mode_enabled ? "true" : "false");
  setValue("clingyAfterMinutes", current.settings?.proactive?.generation?.clingy_after_minutes || 20);
  setValue("clingyMaxFollowups", clingy.max_followups_per_unanswered || 3);
  setValue("clingyCooldownMinutes", clingy.cooldown_minutes || 20);
  setValue("clingyDailyCap", clingy.daily_cap || 3);
  setValue("clingyRespectQuietHours", clingy.respect_quiet_hours !== false ? "true" : "false");
  setValue("clingyDebugForce", clingy.debug_force_trigger_enabled ? "true" : "false");

  const wechat = current.settings.wechat || {};
  setValue("messageDelay", wechat.message_delay);
  setValue("maxReplyDelay", wechat.max_reply_delay);
  setValue("bufferDebounceSeconds", wechat.buffer_debounce_seconds);
  setValue("bufferMaxWaitSeconds", wechat.buffer_max_wait_seconds);

  const weather = current.settings.weather || {};
  setValue("defaultCity", weather.default_city);
  setValue("weatherTimeout", weather.timeout_seconds);
  setValue("weatherCache", weather.cache_ttl_seconds);

  const email = current.settings.email || {};
  setValue("emailEnabled", email.enabled ? "true" : "false");
  setValue("smtpHost", email.smtp_host);
  setValue("smtpPort", email.smtp_port);
  setValue("emailUsername", email.username);
  setValue("emailFrom", email.from_addr);
  setValue("emailDefaultTo", email.default_to);

  const canSend = Boolean(current.send_ready);
  $("manualSendBtn").disabled = !canSend;
  $("sendHint").textContent = canSend
    ? "会发给最近一次微信联系人。"
    : "微信桥接和真实联系人都就绪后才能主动发送。";
}

async function loadDashboard() {
  try {
    dashboard = await api("/admin/events?limit=40");
  } catch {
    dashboard = await loadDashboardFallback();
  }
  renderOverview();
  renderChat();
  renderProactive();
  renderMemory();
  renderLogSummary();
}

async function loadDashboardFallback() {
  const [
    turnsRes,
    proactiveState,
    proactiveTopic,
    proactiveCandidates,
    recapsRes,
    factsRes,
    outboundRes,
  ] = await Promise.all([
    api("/admin/turns?limit=30").catch(() => ({ turns: [] })),
    api("/admin/proactive/state").catch(() => ({})),
    api("/admin/proactive/topic").catch(() => ({ active_topic: null })),
    api("/admin/proactive/candidates?limit=30").catch(() => ({ candidates: [] })),
    api("/admin/recaps?limit=5").catch(() => ({ recaps: [] })),
    api("/admin/shared-facts?limit=8").catch(() => ({ shared_facts: [] })),
    api("/admin/outbound?limit=30").catch(() => ({ outbound_messages: [] })),
  ]);
  const turns = turnsRes.turns || [];
  const events = [];
  for (const turn of turns) {
    const actions = safeJsonList(turn.assistant_actions);
    const tools = safeJsonList(turn.tool_calls);
    events.push({
      id: turn.id,
      kind: "turn",
      time: formatClock(turn.created_at),
      created_at: turn.created_at,
      title: "收到用户消息",
      user_text: turn.user_messages || "",
      reply_texts: actions.map((item) => item.text).filter(Boolean),
      tool_calls: tools,
      source: sourceLabel(turn.source),
      route: "轻量聊天",
      context_policy: "短上下文 + 轻量记忆",
      elapsed_ms: 0,
      token_count: 0,
      send_status: sendStatusLabel(turn.send_status),
      memory_enqueued: false,
    });
  }
  for (const candidate of proactiveCandidates.candidates || []) {
    events.push({
      id: candidate.id,
      kind: "proactive",
      time: formatClock(candidate.created_at),
      created_at: candidate.created_at,
      title: `主动消息${candidate.sent ? "已发送" : "未发送"}`,
      reason_type: candidate.reason_type,
      reason_detail: candidate.reason_detail,
      gate_detail: candidate.gate_detail,
      generated_message: candidate.generated_message,
      sent: Boolean(candidate.sent),
    });
  }
  events.sort((a, b) => Number(b.created_at || 0) - Number(a.created_at || 0));
  return {
    contact: current?.contact || {},
    bot: { current_bot_id: "default_bot", display_name: "default_bot" },
    events,
    turns,
    outbound_messages: outboundRes.outbound_messages || [],
    proactive: {
      state: proactiveState,
      active_topic: proactiveTopic.active_topic,
      candidates: proactiveCandidates.candidates || [],
    },
    memory: {
      recaps: recapsRes.recaps || [],
      shared_facts: factsRes.shared_facts || [],
    },
  };
}

function safeJsonList(value) {
  if (Array.isArray(value)) return value;
  if (!value) return [];
  try {
    const parsed = JSON.parse(value);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function sourceLabel(source) {
  return {
    wechat: "微信",
    user_reply: "用户回复",
    scheduler: "主动消息",
    proactive: "主动消息",
    admin: "后台手动",
  }[source] || source || "未知";
}

function sendStatusLabel(status) {
  return {
    created: "已生成",
    generated: "已生成",
    sending: "发送中",
    delivered: "已送达",
    failed: "发送失败",
  }[status] || status || "未知";
}

async function loadMemories() {
  try {
    const data = await api("/memories?limit=20");
    memories = data.memories || [];
  } catch {
    memories = [];
  }
}

async function loadBots() {
  try {
    bots = await api("/admin/bots");
  } catch {
    bots = null;
  }
}

function setValue(id, value) {
  const el = $(id);
  if (!el) return;
  el.value = value ?? "";
}

function getNumber(id) {
  const value = Number($(id).value);
  return Number.isFinite(value) ? value : 0;
}

function getBoolSelect(id) {
  return $(id).value === "true";
}

function formatNumber(value) {
  const num = Number(value || 0);
  if (!Number.isFinite(num)) return "0";
  return new Intl.NumberFormat("zh-CN").format(num);
}

function formatDuration(seconds) {
  let remaining = Math.max(0, Math.floor(Number(seconds || 0)));
  const days = Math.floor(remaining / 86400);
  remaining %= 86400;
  const hours = Math.floor(remaining / 3600);
  remaining %= 3600;
  const minutes = Math.floor(remaining / 60);
  if (days > 0) return `${days}天`;
  if (hours > 0) return `${hours}小时${minutes}分`;
  return `${minutes}分钟`;
}

function formatClock(ts) {
  const value = Number(ts || 0);
  if (!value) return "--";
  return new Date(value * 1000).toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
  });
}

function shortText(text, max = 120) {
  const value = String(text || "").trim();
  if (value.length <= max) return value;
  return `${value.slice(0, max)}…`;
}

function labelReason(value) {
  const key = String(value || "").trim();
  const labels = {
    check_in: "关心问候",
    follow_up: "延续话题",
    latest_topic_followup: "话题跟进",
    self_share: "自我分享",
    light_ping: "轻量问候",
    nudge: "轻提醒",
    topic_followup: "话题跟进",
    idle_check: "空闲问候",
    scheduled: "定时触发",
    manual: "手动触发",
    unknown: "未知原因",
  };
  return labels[key] || key || "未知原因";
}

function labelProactiveDetail(value) {
  let text = String(value || "").trim();
  if (!text) return "";
  const replacements = [
    [/intent selection fallback/gi, "没有活跃话题，按默认策略选择"],
    [/eligible topic exists/gi, "当前有可跟进话题"],
    [/topic not eligible, fell back to check_in/gi, "当前话题不适合继续跟进，改为问候"],
    [/no active topic, prefer self share/gi, "没有活跃话题，优先自我分享"],
    [/send failed/gi, "微信送达失败"],
    [/quiet hours/gi, "处于免打扰时段"],
    [/sleep hours/gi, "处于免打扰时段"],
    [/not yet due/gi, "还没到计划时间"],
    [/initial due scheduled/gi, "已安排首次主动时间"],
    [/daily cap/gi, "达到每日上限"],
    [/bridge unavailable/gi, "微信桥接不可用"],
    [/no contact/gi, "还没有可发送联系人"],
    [/proactive disabled/gi, "主动消息已关闭"],
    [/bot not active/gi, "当前 bot 未激活"],
    [/v2\.1\.1 due-time/gi, "到达计划时间"],
    [/due-time/gi, "到达计划时间"],
    [/waiting_user_result/gi, "等待用户反馈"],
    [/active_topic/gi, "当前话题仍在继续"],
    [/min interval/gi, "未满足最小间隔"],
    [/daily limit/gi, "达到每日上限"],
    [/sleep window/gi, "处于免打扰时段"],
    [/no recent context/gi, "缺少最近上下文"],
    [/fallback/gi, "兜底"],
  ];
  for (const [pattern, replacement] of replacements) {
    text = text.replace(pattern, replacement);
  }
  return text;
}

function labelTopicStatus(value) {
  const key = String(value || "").trim();
  const labels = {
    waiting_user_result: "等待用户反馈",
    active: "进行中",
    closed: "已关闭",
    stale: "已过期",
    none: "无",
  };
  return labels[key] || labelProactiveDetail(key) || "无";
}

function clear(el) {
  el.innerHTML = "";
}

function row(title, body = "", extra = "") {
  const div = document.createElement("div");
  div.className = `row ${extra}`;
  const strong = document.createElement("strong");
  strong.textContent = title;
  div.appendChild(strong);
  if (body) {
    const p = document.createElement("p");
    p.textContent = body;
    div.appendChild(p);
  }
  return div;
}

function bubble(kind, text) {
  const div = document.createElement("div");
  div.className = `bubble ${kind}`;
  div.textContent = text;
  return div;
}

function renderOverview() {
  if (!current || !dashboard) return;
  const usage = current.usage || {};
  const today = usage.today || {};
  const runtime = usage.runtime || {};
  const proactive = dashboard.proactive?.state?.proactive_schedule || {};
  const bot = dashboard.bot || {};
  $("usageTodayMessages").textContent = formatNumber(today.messages);
  $("usageTodayTokens").textContent = formatNumber(today.tokens);
  $("usageTodayFallbacks").textContent = formatNumber(today.fallbacks);
  $("usageUptime").textContent = formatDuration(runtime.uptime_seconds);
  $("nextProactiveTime").textContent = proactive.next_due_ts ? formatClock(proactive.next_due_ts) : "--";
  $("currentBot").textContent = bot.display_name || bot.current_bot_id || "--";

  const bridgeOk = Boolean(current.wechat?.running);
  $("overviewTitle").textContent = "运行总览";
  $("bridgeBadge").textContent = bridgeOk ? "在线" : "离线";
  $("bridgeBadge").className = `status ${bridgeOk ? "ok" : "warn"}`;

  const feed = $("overviewChat");
  clear(feed);
  const latestTurn = [...(dashboard.events || [])].find((e) => e.kind === "turn");
  if (!latestTurn) {
    feed.appendChild(bubble("system", "暂无对话记录"));
  } else if (latestTurn.is_proactive) {
    feed.appendChild(bubble("system", `主动消息 · ${labelReason(latestTurn.intent || "proactive")}`));
    for (const msg of latestTurn.reply_texts || []) {
      feed.appendChild(bubble("bot", msg));
    }
    feed.appendChild(bubble("system", `${latestTurn.send_status}`));
  } else {
    feed.appendChild(bubble("user", latestTurn.user_text || "用户消息为空"));
    for (const msg of latestTurn.reply_texts || []) {
      feed.appendChild(bubble("bot", msg));
    }
    feed.appendChild(bubble("system", `${latestTurn.route} · ${latestTurn.context_policy} · ${latestTurn.send_status}`));
  }

  const focus = $("focusList");
  clear(focus);
  const topic = dashboard.proactive?.active_topic;
  focus.appendChild(row("微信状态", current.wechat?.configured ? "已配置微信桥接。" : "还没有完成微信部署。", bridgeOk ? "ok" : "warn"));
  focus.appendChild(row("当前话题", topic?.title || "暂无活跃话题。"));
  const lastCandidate = dashboard.proactive?.candidates?.[0];
  focus.appendChild(row("最近主动消息", lastCandidate?.generated_message || "暂无主动消息候选。", lastCandidate?.sent ? "ok" : "warn"));
}

function renderBotSelect() {
  const select = $("botSelect");
  if (!select) return;
  clear(select);
  const botMap = bots?.bots || {};
  const ids = Object.keys(botMap);
  if (!ids.length) {
    const option = document.createElement("option");
    option.value = dashboard?.bot?.current_bot_id || "default_bot";
    option.textContent = dashboard?.bot?.display_name || "default_bot";
    select.appendChild(option);
    return;
  }
  for (const id of ids) {
    const option = document.createElement("option");
    option.value = id;
    option.textContent = botMap[id].display_name || id;
    option.selected = id === bots.current_bot_id;
    select.appendChild(option);
  }
}

function renderChat() {
  if (!dashboard) return;
  const feed = $("chatFeed");
  clear(feed);
  const events = (dashboard.events || []).filter((event) => {
    if (event.kind !== "turn") return false;
    if (chatFilter === "tools") return (event.tool_calls || []).length > 0;
    if (chatFilter === "issues") return event.send_status !== "已送达" || event.elapsed_ms > 12000;
    return true;
  });

  if (!events.length) {
    feed.appendChild(bubble("system", "没有符合条件的对话"));
  }

  for (const event of events.slice(0, 12).reverse()) {
    feed.appendChild(bubble("system", `${event.time} · ${event.source}`));
    if (event.user_text) feed.appendChild(bubble("user", event.user_text));
    for (const msg of event.reply_texts || []) {
      feed.appendChild(bubble("bot", msg));
    }
    const toolText = (event.tool_calls || []).map((tool) => tool.name || tool.type || "工具").join("、");
    const traceText = [event.raw_source || event.source, event.trigger, event.send_status, event.candidate_id || event.batch_id]
      .filter(Boolean)
      .join(" · ");
    feed.appendChild(bubble("system", `${event.route} · ${event.context_policy} · ${event.elapsed_ms || 0}ms · ${toolText || "0 工具"} · ${traceText}`));
  }

  const detail = $("turnDetail");
  clear(detail);
  const latest = events[0] || (dashboard.events || []).find((e) => e.kind === "turn");
  if (!latest) {
    detail.appendChild(row("暂无详情"));
    return;
  }
  detail.appendChild(row("路由", latest.route));
  detail.appendChild(row("上下文", latest.context_policy));
  detail.appendChild(row("工具调用", latest.tool_calls?.length ? JSON.stringify(latest.tool_calls, null, 2) : "无"));
  detail.appendChild(row("发送状态", latest.send_status));
  if (latest.skip_reply || latest.skip_reason) {
    detail.appendChild(row("跳过回复", latest.skip_reason || "模型选择不回复"));
  }
  detail.appendChild(row("链路追踪", [
    `source=${latest.raw_source || latest.source || ""}`,
    `trigger=${latest.trigger || ""}`,
    `send_status=${latest.send_status || ""}`,
    latest.candidate_id ? `candidate_id=${latest.candidate_id}` : "",
    latest.batch_id ? `batch_id=${latest.batch_id}` : "",
  ].filter(Boolean).join("\n")));
  detail.appendChild(row("记忆写入", latest.memory_enqueued ? "已进入后台提取" : "本轮未写入"));
}

function renderProactive() {
  if (!dashboard || !current) return;
  const state = dashboard.proactive?.state || {};
  const schedule = state.proactive_schedule || {};
  const scheduler = state.scheduler_state || {};
  const timing = state.timing_config || {};
  const clingy = state.clingy_state || {};
  const topic = dashboard.proactive?.active_topic;
  $("proactiveBadge").textContent = schedule.is_due ? "已到时间" : "等待";
  $("proactiveBadge").className = `status ${schedule.is_due ? "ok" : "warn"}`;
  $("maxDailyProactiveText").textContent = current.settings?.scheduler?.max_daily_proactive ?? "--";
  $("messagesTodayText").textContent = scheduler.messages_today ?? "--";
  $("minIntervalText").textContent = `${timing.min_interval_minutes ?? "--"}m`;
  $("maxIntervalText").textContent = `${timing.max_interval_minutes ?? "--"}m`;

  // Clingy state display
  const clingyPanel = $("clingyPanel");
  clear(clingyPanel);
  clingyPanel.appendChild(row("粘人模式", clingy.clingy_mode_enabled ? "已启用" : "已关闭", clingy.clingy_mode_enabled ? "ok" : "warn"));
  clingyPanel.appendChild(row("等待用户回复", clingy.awaiting_user_reply ? "是" : "否", clingy.awaiting_user_reply ? "ok" : ""));
  clingyPanel.appendChild(row("上轮意图", clingy.last_proactive_intent || "无"));
  clingyPanel.appendChild(row("下次粘人检查", clingy.next_clingy_due_at ? formatClock(clingy.next_clingy_due_at) : "暂无"));
  clingyPanel.appendChild(row("已追问次数", `${clingy.clingy_followup_count || 0} / ${clingy.max_followups || 3}`));
  clingyPanel.appendChild(row("粘人等待", `${clingy.clingy_after_minutes || 20} 分钟`));
  if (clingy.next_due_in_seconds != null && clingy.next_due_in_seconds > 0) {
    clingyPanel.appendChild(row("距下次检查", `${Math.ceil(clingy.next_due_in_seconds / 60)} 分钟`));
  }

  const list = $("proactiveList");
  clear(list);
  const candidates = dashboard.proactive?.candidates || [];
  if (!candidates.length) {
    list.appendChild(row("暂无主动消息候选"));
  }
  for (const item of candidates.slice(0, 12)) {
    const sendLabel = (s) => {
      if (s === "delivered" || s === "已送达") return "已送达";
      if (s === "sending" || s === "发送中") return "发送中";
      if (s === "failed" || s === "失败") return "失败";
      if (s === "skipped" || s === "suppressed" || s === "已跳过") return "已跳过";
      return "";
    };
    const statusLabel = sendLabel(item.send_status) || (item.sent ? "已送达" : "未送达");
    const title = `${formatClock(item.created_at)} · ${statusLabel} · ${labelReason(item.reason_type)}`;
    const body = [
      item.generated_message ? `内容：${item.generated_message}` : "",
      item.reason_detail ? `原因：${labelProactiveDetail(item.reason_detail)}` : "",
      item.gate_detail ? `发送门槛：${labelProactiveDetail(item.gate_detail)}` : "",
      `链路：source=${item.source || "proactive"} · trigger=${item.trigger || "proactive_reason"} · send_status=${item.send_status || (item.sent ? "delivered" : "")}`,
      item.batch_id || item.candidate_id ? `ID：candidate_id=${item.candidate_id || item.id || ""} · batch_id=${item.batch_id || ""}` : "",
    ].filter(Boolean).join("\n");
    list.appendChild(row(title, body, item.sent ? "ok" : "warn"));
  }

  const topicPanel = $("topicPanel");
  clear(topicPanel);
  topicPanel.appendChild(row("下次主动", schedule.next_due_ts ? formatClock(schedule.next_due_ts) : "暂无计划"));
  topicPanel.appendChild(row("当前话题", topic?.title || "暂无活跃话题"));
  topicPanel.appendChild(row("话题状态", labelTopicStatus(topic?.status || "无")));
}

function renderMemory() {
  const list = $("memoryList");
  clear(list);
  if (!memories.length) {
    list.appendChild(row("暂无长期记忆"));
  }
  for (const item of memories.slice(0, 12)) {
    const itemRow = row(item.category || "记忆", shortText(item.content, 220));
    if (item.id) {
      const actions = document.createElement("div");
      actions.className = "row-actions";
      const del = document.createElement("button");
      del.className = "danger";
      del.textContent = "删除";
      del.addEventListener("click", () => deleteMemory(item.id));
      actions.appendChild(del);
      itemRow.appendChild(actions);
    }
    list.appendChild(itemRow);
  }

  const recapList = $("recapList");
  clear(recapList);
  const recaps = dashboard?.memory?.recaps || [];
  const facts = dashboard?.memory?.shared_facts || [];
  if (!recaps.length && !facts.length) {
    recapList.appendChild(row("暂无摘要或共享事实"));
  }
  for (const recap of recaps) {
    recapList.appendChild(row(`摘要 · ${formatClock(recap.created_at)}`, shortText(recap.recap_text, 220)));
  }
  for (const fact of facts) {
    recapList.appendChild(row(`共享事实 · ${fact.category || ""}`, shortText(fact.content, 180)));
  }
}

function renderLogSummary() {
  const box = $("logSummary");
  clear(box);
  const items = (dashboard?.events || []).slice(0, 10);
  if (!items.length) {
    box.appendChild(row("暂无事件"));
    return;
  }
  for (const item of items) {
    if (item.kind === "turn") {
      box.appendChild(row(`${item.time} 收到用户消息`, `${shortText(item.user_text, 90)}\n回复 ${item.reply_texts?.length || 0} 条 · ${item.send_status}`));
    } else {
      box.appendChild(row(`${item.time} ${item.title}`, item.generated_message || item.gate_detail || item.reason_detail, item.sent ? "ok" : "warn"));
    }
  }
}

function classifyLogLine(line) {
  const upper = String(line || "").toUpperCase();
  if (upper.includes("[ERROR]") || upper.includes("FAILED") || upper.includes("EXCEPTION")) return "error";
  if (upper.includes("[WARNING]") || upper.includes("[WARN]") || upper.includes("SKIP") || upper.includes("FALLBACK")) return "warn";
  return "info";
}

function isIssueLogLine(line) {
  const lower = String(line || "").toLowerCase();
  return ["[error]", "[warning]", "failed", "fallback", "skip", "empty", "send_error", "llm_error", "scheduler:"]
    .some((token) => lower.includes(token));
}

function renderLogs(data = {}) {
  const box = $("logConsole");
  clear(box);
  const allLines = data.lines || [];
  const lines = logIssuesOnly ? allLines.filter(isIssueLogLine) : allLines;
  $("logMeta").textContent = data.exists
    ? `${lines.length} 行 · ${data.truncated ? "已截取尾部" : "完整尾部"}`
    : "日志文件不存在";
  if (!lines.length) {
    const empty = document.createElement("div");
    empty.className = "log-empty";
    empty.textContent = logIssuesOnly ? "最近没有问题日志" : "暂无日志";
    box.appendChild(empty);
    return;
  }
  for (const line of lines) {
    const div = document.createElement("div");
    div.className = `log-line ${classifyLogLine(line)}`;
    div.textContent = line;
    box.appendChild(div);
  }
  box.scrollTop = box.scrollHeight;
}

async function loadLogs() {
  try {
    const data = await api("/admin/logs?limit=240");
    renderLogs(data);
  } catch (err) {
    $("logConsole").innerHTML = `<div class="log-empty">日志加载失败：${err.message}</div>`;
  }
}

function startLogPolling() {
  if (logPollTimer) clearInterval(logPollTimer);
  if (!$("logAutoRefresh").classList.contains("active")) return;
  logPollTimer = setInterval(() => {
    if (activeTab === "logs") {
      loadLogs();
      loadDashboard().catch(() => {});
    }
  }, 4000);
}

async function savePersona() {
  await api("/admin/persona", {
    method: "POST",
    body: JSON.stringify({ content: $("personaText").value }),
  });
  toast("人设已保存");
}

async function saveScheduler() {
  await api("/admin/settings", {
    method: "POST",
    body: JSON.stringify({
      scheduler: {
        enabled: getBoolSelect("schedulerEnabled"),
        interval_minutes: getNumber("intervalMinutes"),
        quiet_hours_enabled: getBoolSelect("quietHoursEnabled"),
        max_daily_proactive: getNumber("maxDailyProactive"),
        scheduled_check_seconds: getNumber("scheduledCheckSeconds"),
        sleep_start: getNumber("sleepStart"),
        sleep_end: getNumber("sleepEnd"),
      },
      proactive: {
        timing: {
          min_interval_minutes: getNumber("minProactiveInterval"),
          max_interval_minutes: getNumber("maxProactiveInterval"),
        },
        generation: {
          enabled_types: ["check_in", "latest_topic_followup", "clingy_followup", "self_share", "light_ping"],
        },
      },
    }),
  });
  toast("主动设置已保存");
  await loadAll();
}

async function saveClingy() {
  const clingyEnabledEl = $("clingyEnabled");
  if (!clingyEnabledEl) { toast("粘人设置表单未就绪"); return; }
  try {
    await api("/admin/settings", {
      method: "POST",
      body: JSON.stringify({
        proactive: {
          generation: {
            clingy_mode_enabled: getBoolSelect("clingyEnabled"),
            clingy_after_minutes: getNumber("clingyAfterMinutes"),
          },
          clingy: {
            max_followups_per_unanswered: getNumber("clingyMaxFollowups"),
            cooldown_minutes: getNumber("clingyCooldownMinutes"),
            daily_cap: getNumber("clingyDailyCap"),
            respect_quiet_hours: getBoolSelect("clingyRespectQuietHours"),
            debug_force_trigger_enabled: getBoolSelect("clingyDebugForce"),
          },
        },
      }),
    });
    toast("粘人设置已保存");
    await loadAll();
  } catch (err) {
    toast(`保存失败: ${err.message}`);
  }
}

async function simulateClingy() {
  const resultEl = $("clingySimulateResult");
  if (!resultEl) return;
  resultEl.textContent = "检查中...";
  try {
    const data = await api("/admin/clingy/simulate", { method: "POST", body: "{}" });
    const lines = [];
    lines.push(`是否满足发送条件: ${data.would_send ? "是" : "否"} (stage=${data.stage})`);
    lines.push(`原因: ${data.reason}`);
    if (data.blocked_reasons && data.blocked_reasons.length) {
      lines.push("阻塞原因:");
      for (const r of data.blocked_reasons) lines.push(`  - ${r}`);
    }
    const cs = data.clingy_state || {};
    lines.push("");
    lines.push("--- 粘人状态 ---");
    lines.push(`模式: ${cs.clingy_mode_enabled ? "启用" : "关闭"}`);
    lines.push(`等待回复: ${cs.awaiting_user_reply ? "是" : "否"}`);
    lines.push(`上轮意图: ${cs.last_proactive_intent || "无"} (user_reply=聊天回复, proactive=主动消息)`);
    lines.push(`已追问: ${cs.clingy_followup_count || 0} / ${cs.max_followups || "?"}`);
    lines.push(`等待分钟: ${cs.clingy_after_minutes || "?"}`);
    if (cs.next_due_in_seconds != null && cs.next_due_in_seconds > 0) {
      lines.push(`距下次检查: ${Math.ceil(cs.next_due_in_seconds / 60)} 分钟`);
    }
    if (data.generated_message) {
      lines.push(`将发送: ${data.generated_message}`);
    }
    resultEl.textContent = lines.join("\n");
  } catch (err) {
    resultEl.textContent = `错误: ${err.message}`;
  }
}

async function forceClingy() {
  const resultEl = $("clingyForceResult");
  if (!resultEl) return;
  resultEl.textContent = "发送中...";
  try {
    const data = await api("/admin/clingy/force", { method: "POST", body: "{}" });
    if (data.status === "blocked") {
      const lines = [`被阻止: ${data.reason}`];
      if (data.diagnostics) {
        const d = data.diagnostics;
        lines.push("");
        lines.push("--- 详细诊断 ---");
        lines.push(`clingly enabled: ${d.clingy_mode_enabled}`);
        lines.push(`awaiting: ${d.awaiting_user_reply}`);
        lines.push(`last_proactive_intent: ${d.last_proactive_intent || "(无)"}`);
        lines.push(`followup count: ${d.clingy_followup_count}`);
        lines.push(`clingy_after: ${d.clingy_after_minutes}min`);
        if (d.last_bot_turn_at) lines.push(`上次bot消息: ${formatClock(d.last_bot_turn_at)} (${d.last_bot_turn_source})`);
        else lines.push("上次bot消息: 无记录");
        if (d.last_user_turn_at) lines.push(`上次用户消息: ${formatClock(d.last_user_turn_at)} — ${d.last_user_turn_text || ""}`);
        else lines.push("上次用户消息: 无记录");
        lines.push("");
        lines.push("→ 如果 awaiting=false 但 bot 刚回复过，请检查 bot 是否 skip 了");
        lines.push("→ 可点「设等待」手动进入未回复状态再测试");
      }
      resultEl.textContent = lines.join("\n");
    } else {
      const r = data.result || {};
      resultEl.textContent = r.sent
        ? `已发送: ${r.message || "(空)"} · intent=${r.intent}`
        : `未发送: ${r.reason || "未知"} (stage=${r.stage})`;
    }
    await loadAll();
  } catch (err) {
    resultEl.textContent = `错误: ${err.message}`;
  }
}

async function primeClingy() {
  const resultEl = $("clingyForceResult");
  if (!resultEl) return;
  try {
    const data = await api("/admin/clingy/prime", { method: "POST", body: "{}" });
    resultEl.textContent = data.message || "已设置";
    await loadAll();
  } catch (err) {
    resultEl.textContent = `错误: ${err.message}`;
  }
}

async function saveTools() {
  await api("/admin/settings", {
    method: "POST",
    body: JSON.stringify({
      wechat: {
        message_delay: getNumber("messageDelay"),
        max_reply_delay: getNumber("maxReplyDelay"),
        buffer_debounce_seconds: getNumber("bufferDebounceSeconds"),
        buffer_max_wait_seconds: getNumber("bufferMaxWaitSeconds"),
      },
      weather: {
        default_city: $("defaultCity").value,
        timeout_seconds: getNumber("weatherTimeout"),
        cache_ttl_seconds: getNumber("weatherCache"),
      },
      email: {
        enabled: getBoolSelect("emailEnabled"),
        smtp_host: $("smtpHost").value,
        smtp_port: getNumber("smtpPort"),
        username: $("emailUsername").value,
        from_addr: $("emailFrom").value,
        default_to: $("emailDefaultTo").value,
      },
    }),
  });
  toast("工具和邮箱设置已保存");
  await loadConfig();
}

async function switchBot() {
  const botId = $("botSelect").value;
  if (!botId) return;
  await api("/admin/bots/switch", {
    method: "POST",
    body: JSON.stringify({ bot_id: botId }),
  });
  toast("bot 已切换");
  await loadAll();
}

async function stopBridge() {
  await api("/wechat/stop", {
    method: "POST",
    body: JSON.stringify({}),
  });
  toast("桥接已停止");
  await loadAll();
}

async function deleteMemory(id) {
  if (!id) return;
  await api(`/memories/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
  toast("记忆已删除");
  await loadAll();
}

async function forgetAllDeveloperState() {
  const ok = window.confirm("确定要彻底遗忘所有对话历史、记忆、摘要、关系状态、主动话题和未发送任务吗？提示词和配置会保留。");
  if (!ok) return;
  const data = await api("/admin/dev/forget", {
    method: "POST",
    body: JSON.stringify({}),
  });
  const total = Object.values(data.cleared || {}).reduce((sum, value) => sum + Number(value || 0), 0);
  toast(`已彻底遗忘 ${total} 条状态记录`);
  await loadAll();
}

async function triggerScheduler() {
  const data = await api("/admin/proactive/trigger", {
    method: "POST",
    body: JSON.stringify({}),
  });
  toast(data.message || "调度已触发，稍后刷新查看结果");
  // Give scheduler a moment, then refresh
  setTimeout(() => loadDashboard(), 2000);
}

async function closeTopic() {
  await api("/admin/proactive/topic/close", {
    method: "POST",
    body: JSON.stringify({ reason: "admin manual close" }),
  });
  toast("当前话题已关闭");
  await loadDashboard();
}

async function generateRecap() {
  const data = await api("/admin/recaps/generate", {
    method: "POST",
    body: JSON.stringify({}),
  });
  toast(data.status === "generated" ? "摘要已生成" : data.reason || "本次未生成");
  await loadDashboard();
}

async function manualSend() {
  await api("/admin/send", {
    method: "POST",
    body: JSON.stringify({ text: $("manualText").value }),
  });
  $("manualText").value = "";
  toast("已发送");
  await loadDashboard();
}

async function loadTasks() {
  const data = await api("/scheduler/messages?limit=20");
  const list = $("taskList");
  clear(list);
  const tasks = data.tasks || [];
  if (!tasks.length) {
    list.appendChild(row("暂无定时任务"));
    return;
  }
  for (const task of tasks) {
    const first = task.messages?.[0]?.text || "";
    list.appendChild(row(`${task.status} · ${formatClock(task.due_at)}`, first));
  }
}

async function importMemory() {
  const file = $("memoryFile").files?.[0];
  if (!file) {
    toast("先选择记忆文件");
    return;
  }
  const clearBefore = $("clearBeforeImport").checked;
  const data = await apiRaw(`/memories/import?clear_existing=${clearBefore ? "true" : "false"}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/octet-stream",
      "X-Filename": encodeURIComponent(file.name),
    },
    body: await file.arrayBuffer(),
  });
  $("memoryImportHint").textContent = `解析 ${data.parsed} 条，导入 ${data.imported} 条，跳过 ${data.skipped} 条。`;
  toast("记忆已导入");
  await loadAll();
}

function exportMemory() {
  window.location.href = "/memories/export";
}

function qrImageSrc(value) {
  if (!value) return "";
  if (value.startsWith("data:")) return value;
  if (value.startsWith("http://") || value.startsWith("https://")) return value;
  return `data:image/png;base64,${value}`;
}

async function startDeploy() {
  const data = await api("/wechat/deploy/start", {
    method: "POST",
    body: JSON.stringify({}),
  });
  deployQrCode = data.qrcode || "";
  if (!deployQrCode || (!data.qrcode_svg && !data.qrcode_img && !data.qrcode_url)) {
    toast("二维码生成失败");
    return;
  }
  $("qrSvg").innerHTML = data.qrcode_svg || "";
  $("qrSvg").hidden = !data.qrcode_svg;
  $("qrImage").hidden = Boolean(data.qrcode_svg);
  if (!data.qrcode_svg && data.qrcode_img) $("qrImage").src = qrImageSrc(data.qrcode_img);
  const qrUrl = data.qrcode_url || data.qrcode_img || "";
  $("qrLink").hidden = !qrUrl;
  $("qrLink").href = qrUrl || "#";
  $("qrWrap").hidden = false;
  startDeployPolling();
}

function startDeployPolling() {
  if (deployPollTimer) clearInterval(deployPollTimer);
  deployPollTimer = setInterval(async () => {
    if (!deployQrCode || deployPollInFlight) return;
    deployPollInFlight = true;
    try {
      const data = await api(`/wechat/deploy/status?qrcode=${encodeURIComponent(deployQrCode)}`);
      const status = data.status || "wait";
      if (status === "confirmed") {
        clearInterval(deployPollTimer);
        deployPollTimer = null;
        $("qrWrap").hidden = true;
        toast("微信部署成功");
        await loadAll();
      } else if (status === "expired") {
        clearInterval(deployPollTimer);
        deployPollTimer = null;
        toast("二维码已过期");
      }
    } catch (err) {
      $("qrHint").textContent = err.message;
    } finally {
      deployPollInFlight = false;
    }
  }, 1800);
}

async function startSavedBridge() {
  await api("/wechat/start", {
    method: "POST",
    body: JSON.stringify({}),
  });
  toast("桥接已启动");
  await loadAll();
}

function showSettingsView(viewId = "settingsHome") {
  document.querySelectorAll(".settings-view").forEach((el) => {
    el.classList.toggle("active", el.id === viewId);
  });
}

// ── Emotion / Dialogue state ──────────────────────────────────────

async function loadEmotionState() {
  try {
    const [emoResp, dsResp] = await Promise.all([
      api("/admin/emotion/state"),
      api("/admin/dialogue/state"),
    ]);
    renderEmotionState(emoResp);
    renderDialogueState(dsResp);
    renderEmotionEvents(emoResp.recent_events || []);
    renderDialogueEvents(dsResp.recent_events || []);
  } catch (e) {
    toast("加载情绪状态失败: " + e.message);
  }
}

function renderEmotionState(data) {
  const s = data.emotion_state || {};
  const moodMap = { calm: "平静", cold: "冷淡", warm: "温暖", playful: "调皮", jealous: "吃醋", lonely: "孤独", hurt: "受伤", irritated: "烦躁", angry: "生气", softened: "软化" };
  const stageMap = { stranger: "陌生人", familiar: "熟人", close: "亲密", attached: "依恋" };
  $("emotionStateView").innerHTML =
    `<p>心情: <strong>${moodMap[s.bot_mood] || s.bot_mood}</strong> (强度 ${s.mood_intensity}/100)</p>` +
    `<p>关系: <strong>${stageMap[s.relationship_stage] || s.relationship_stage}</strong></p>` +
    `<p>好感: ${s.affection} | 信任: ${s.trust} | 激怒: ${s.irritation} | 依恋: ${s.attachment} | 吃醋: ${s.jealousy}</p>` +
    `<p class="hint">最后原因: ${s.last_emotion_reason || "--"}</p>`;
  // Pre-fill form
  $("emoMoodSelect").value = s.bot_mood || "calm";
  $("emoAffection").value = s.affection || 10;
  $("emoTrust").value = s.trust || 10;
  $("emoIrritation").value = s.irritation || 0;
}

function renderDialogueState(data) {
  const s = data.dialogue_state || {};
  const waitMap = { none: "正常", assistant_waiting_user: "等待Bot回复", user_waiting_assistant: "等待用户回复", topic_closed: "话题结束", paused_by_user: "用户暂停", bot_silent: "Bot沉默中" };
  const isSilent = s.wait_state === "bot_silent";
  const proactiveAllowed = !["bot_silent", "user_waiting_assistant", "paused_by_user"].includes(s.wait_state);
  const clingyAllowed = s.wait_state === "assistant_waiting_user";
  $("dialogueStateView").innerHTML =
    `<p>状态: <strong>${waitMap[s.wait_state] || s.wait_state}</strong></p>` +
    `<p>Bot沉默: <strong>${isSilent ? "是" : "否"}</strong> ${isSilent ? "(原因: " + (s.silent_reason || "--") + ")" : ""}</p>` +
    `<p>沉默消息数: ${s.user_messages_since_silent} / ${s.silent_must_reply_after_count}</p>` +
    `<p>主动消息允许: <strong>${proactiveAllowed ? "是" : "否"}</strong> | 粘人允许: <strong>${clingyAllowed ? "是" : "否"}</strong></p>` +
    `<p>上次用户消息: ${s.last_user_message_excerpt || "--"}</p>` +
    `<p class="hint">原因: ${s.last_state_reason || "--"}</p>`;
  $("dsWaitSelect").value = s.wait_state || "none";
}

function renderEmotionEvents(events) {
  if (!events || !events.length) { $("emotionEventsView").innerHTML = "<p>暂无事件</p>"; return; }
  $("emotionEventsView").innerHTML = events.slice(0, 20).map(e =>
    `<div class="event-row"><span class="tag">${e.event_type || ""}</span> ${(e.reason || "").slice(0, 80)} <span class="hint">conf=${e.confidence?.toFixed(2) || "?"}</span></div>`
  ).join("");
}

function renderDialogueEvents(events) {
  if (!events || !events.length) { $("dialogueEventsView").innerHTML = "<p>暂无事件</p>"; return; }
  $("dialogueEventsView").innerHTML = events.slice(0, 20).map(e =>
    `<div class="event-row">${(e.reason || "").slice(0, 100)}</div>`
  ).join("");
}

// ── Emotion manual override handlers ──

$("emoUpdateBtn").addEventListener("click", async () => {
  try {
    await api("/admin/emotion/update", {
      method: "POST",
      body: JSON.stringify({
        bot_mood: $("emoMoodSelect").value,
        affection: parseInt($("emoAffection").value) || 10,
        trust: parseInt($("emoTrust").value) || 10,
        irritation: parseInt($("emoIrritation").value) || 0,
      }),
    });
    toast("情绪已更新");
    loadEmotionState();
  } catch (e) { toast("更新失败: " + e.message); }
});

$("emoResetBtn").addEventListener("click", async () => {
  try {
    await api("/admin/emotion/reset", { method: "POST" });
    toast("情绪已重置");
    loadEmotionState();
  } catch (e) { toast("重置失败: " + e.message); }
});

$("emotionRefreshBtn").addEventListener("click", loadEmotionState);

// ── Dialogue manual override handlers ──

$("dsUpdateBtn").addEventListener("click", async () => {
  try {
    await api("/admin/dialogue/update", {
      method: "POST",
      body: JSON.stringify({ wait_state: $("dsWaitSelect").value }),
    });
    toast("对话状态已更新");
    loadEmotionState();
  } catch (e) { toast("更新失败: " + e.message); }
});

$("dsResetBtn").addEventListener("click", async () => {
  try {
    await api("/admin/dialogue/reset", { method: "POST" });
    toast("对话状态已重置");
    loadEmotionState();
  } catch (e) { toast("重置失败: " + e.message); }
});

$("dsForceReplyBtn").addEventListener("click", async () => {
  try {
    await api("/admin/dialogue/update", {
      method: "POST",
      body: JSON.stringify({ wait_state: "assistant_waiting_user", silent_started_at: 0, user_messages_since_silent: 0 }),
    });
    toast("已强制进入等待Bot回复状态");
    loadEmotionState();
  } catch (e) { toast("失败: " + e.message); }
});

$("dsBreakSilenceBtn").addEventListener("click", async () => {
  try {
    await api("/admin/dialogue/update", {
      method: "POST",
      body: JSON.stringify({ wait_state: "assistant_waiting_user", silent_started_at: 0, silent_reason: "admin manual break", user_messages_since_silent: 0 }),
    });
    toast("已解除沉默");
    loadEmotionState();
  } catch (e) { toast("失败: " + e.message); }
});

$("dsPauseProactiveBtn").addEventListener("click", async () => {
  try {
    const until = Date.now() / 1000 + 3600;
    await api("/admin/dialogue/update", {
      method: "POST",
      body: JSON.stringify({ wait_state: "paused_by_user", proactive_paused_until: until }),
    });
    toast("已暂停主动消息1小时");
    loadEmotionState();
  } catch (e) { toast("失败: " + e.message); }
});

// ── End emotion/dialogue ──────────────────────────────────────────

function switchTab(tab) {
  activeTab = tab;
  $("pageTitle").textContent = pageTitles[tab] || "LightChat";
  document.querySelectorAll(".panel").forEach((el) => el.classList.remove("active"));
  document.querySelectorAll(".nav-item, .mobile-item").forEach((el) => el.classList.toggle("active", el.dataset.tab === tab));
  $(tab).classList.add("active");
  if (tab === "emotion") {
    loadEmotionState();
  }
  if (tab === "logs") {
    loadLogs();
    startLogPolling();
  } else if (logPollTimer) {
    clearInterval(logPollTimer);
    logPollTimer = null;
  }
  if (tab === "settings") showSettingsView("settingsHome");
}

document.querySelectorAll(".nav-item, .mobile-item").forEach((btn) => {
  btn.addEventListener("click", () => switchTab(btn.dataset.tab));
});

document.querySelectorAll("[data-chat-filter]").forEach((btn) => {
  btn.addEventListener("click", () => {
    chatFilter = btn.dataset.chatFilter;
    document.querySelectorAll("[data-chat-filter]").forEach((el) => el.classList.toggle("active", el === btn));
    renderChat();
  });
});

document.querySelectorAll("[data-settings-view]").forEach((btn) => {
  btn.addEventListener("click", () => showSettingsView(btn.dataset.settingsView));
});

document.querySelectorAll(".back-settings").forEach((btn) => {
  btn.addEventListener("click", () => showSettingsView("settingsHome"));
});

$("loginBtn").addEventListener("click", login);
$("loginPassword").addEventListener("keydown", (event) => {
  if (event.key === "Enter") login();
});
$("logoutBtn").addEventListener("click", logout);
$("refreshBtn").addEventListener("click", () => loadAll().then(() => toast("已刷新")));
$("overviewRefreshBtn").addEventListener("click", () => loadAll().then(() => toast("已刷新")));
$("savePersonaBtn").addEventListener("click", savePersona);
$("saveSchedulerBtn").addEventListener("click", saveScheduler);
$("saveClingyBtn").addEventListener("click", saveClingy);
$("saveToolsBtn").addEventListener("click", saveTools);
$("saveEmailBtn").addEventListener("click", saveTools);
$("primeClingyBtn").addEventListener("click", primeClingy);
$("simulateClingyBtn").addEventListener("click", simulateClingy);
$("forceClingyBtn").addEventListener("click", forceClingy);
$("botSelect").addEventListener("change", () => switchBot().catch((err) => toast(err.message)));
$("triggerSchedulerBtn").addEventListener("click", triggerScheduler);
$("closeTopicBtn").addEventListener("click", closeTopic);
$("generateRecapBtn").addEventListener("click", generateRecap);
$("manualSendBtn").addEventListener("click", () => manualSend().catch((err) => toast(err.message)));
$("loadTasksBtn").addEventListener("click", loadTasks);
$("importMemoryBtn").addEventListener("click", () => importMemory().catch((err) => toast(err.message)));
$("exportMemoryBtn").addEventListener("click", exportMemory);
$("forgetAllBtn").addEventListener("click", () => forgetAllDeveloperState().catch((err) => toast(err.message)));
$("startDeployBtn").addEventListener("click", () => startDeploy().catch((err) => toast(err.message)));
$("startBridgeBtn").addEventListener("click", () => startSavedBridge().catch((err) => toast(err.message)));
$("stopBridgeBtn").addEventListener("click", () => stopBridge().catch((err) => toast(err.message)));
$("refreshLogsBtn").addEventListener("click", loadLogs);
$("logIssuesOnly").addEventListener("click", () => {
  logIssuesOnly = !logIssuesOnly;
  $("logIssuesOnly").classList.toggle("active", logIssuesOnly);
  loadLogs();
});
$("logAutoRefresh").addEventListener("click", () => {
  $("logAutoRefresh").classList.toggle("active");
  startLogPolling();
});

checkAuth().catch((err) => {
  toast(err.message);
  showLogin();
});
