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
  memory: "LightChat · 记忆",
  settings: "LightChat · 设置",
  logs: "LightChat · 日志",
};

function toast(text) {
  const el = $("toast");
  if (!el) return;
  el.textContent = text;
  el.hidden = false;
  setTimeout(() => { el.hidden = true; }, 2400);
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

function showLogin() {
  $("loginView").hidden = false;
  $("appView").hidden = true;
  $("loginPassword")?.focus();
}

function showApp() {
  $("loginView").hidden = true;
  $("appView").hidden = false;
}

async function checkAuth() {
  try {
    const data = await api("/admin/auth/status");
    if (data.authenticated) {
      showApp();
      await loadAll();
    } else {
      showLogin();
    }
  } catch {
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
  await api("/admin/auth/logout", { method: "POST", body: "{}" });
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
  setValue("personaText", current.persona || "");

  const wechat = current.settings?.wechat || {};
  setValue("messageDelay", wechat.message_delay);
  setValue("maxReplyDelay", wechat.max_reply_delay);
  setValue("bufferDebounceSeconds", wechat.buffer_debounce_seconds);
  setValue("bufferMaxWaitSeconds", wechat.buffer_max_wait_seconds);

  const weather = current.settings?.weather || {};
  setValue("defaultCity", weather.default_city);
  setValue("weatherTimeout", weather.timeout_seconds);
  setValue("weatherCache", weather.cache_ttl_seconds);

  const email = current.settings?.email || {};
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
    : "微信桥接和真实联系人就绪后才能手动发送。";
}

async function loadDashboard() {
  dashboard = await api("/admin/events?limit=40");
  renderOverview();
  renderChat();
  renderLogSummary();
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
  if (el) el.value = value ?? "";
}

function getNumber(id) {
  const value = Number($(id)?.value);
  return Number.isFinite(value) ? value : 0;
}

function getBoolSelect(id) {
  return $(id)?.value === "true";
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
  return new Date(value * 1000).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function shortText(text, max = 120) {
  const value = String(text || "").trim();
  return value.length > max ? `${value.slice(0, max - 1)}…` : value;
}

function clear(el) {
  if (el) el.replaceChildren();
}

function row(label, value, tone = "") {
  const item = document.createElement("div");
  item.className = "row-item";
  const labelEl = document.createElement("span");
  labelEl.textContent = label;
  const valueEl = document.createElement("strong");
  valueEl.textContent = value ?? "--";
  if (tone) valueEl.className = tone;
  item.append(labelEl, valueEl);
  return item;
}

function bubble(role, text) {
  const el = document.createElement("div");
  el.className = `bubble ${role}`;
  el.textContent = text || "";
  return el;
}

function sourceLabel(source) {
  return {
    wechat: "微信",
    user_reply: "用户回复",
    admin: "后台手动",
  }[source] || source || "未知";
}

function renderOverview() {
  const usage = current?.usage || {};
  const runtime = usage.runtime || {};
  $("usageTodayMessages").textContent = formatNumber(usage.messages || 0);
  $("usageTodayTokens").textContent = formatNumber(usage.tokens || 0);
  $("usageTodayFallbacks").textContent = formatNumber(usage.fallbacks || 0);
  $("usageUptime").textContent = formatDuration(runtime.uptime_seconds || 0);
  $("currentBot").textContent = dashboard?.bot?.display_name || "--";

  const bridge = current?.wechat || {};
  $("bridgeBadge").textContent = bridge.running ? "桥接运行中" : (bridge.configured ? "已配置" : "未配置");
  $("bridgeBadge").className = `status ${bridge.running ? "ok" : "warn"}`;

  const feed = $("overviewChat");
  clear(feed);
  const events = dashboard?.events || [];
  if (!events.length) {
    feed.appendChild(bubble("system", "暂无对话"));
  }
  for (const event of events.slice(0, 8)) {
    if (event.user_text) feed.appendChild(bubble("user", event.user_text));
    const replies = event.reply_texts || [];
    if (replies.length) {
      for (const text of replies) feed.appendChild(bubble("assistant", text));
    } else if (event.skip_reply) {
      feed.appendChild(bubble("system", `未回复 · ${event.skip_reason || "no reply"}`));
    }
  }

  const focus = $("focusList");
  clear(focus);
  focus.appendChild(row("联系人", current?.contact?.user_id || "--"));
  focus.appendChild(row("发送状态", current?.send_ready ? "可发送" : "不可发送", current?.send_ready ? "ok" : "warn"));
  focus.appendChild(row("桥接模式", bridge.mode || "--"));
  focus.appendChild(row("token", bridge.token_masked || "--"));
}

function renderChat() {
  const feed = $("chatFeed");
  clear(feed);
  const events = (dashboard?.events || []).filter((event) => {
    if (chatFilter === "issues") return event.skip_reply || event.send_status === "发送失败";
    if (chatFilter === "tools") return (event.tool_calls || []).length > 0;
    return true;
  });
  for (const event of events) {
    if (event.user_text) feed.appendChild(bubble("user", event.user_text));
    for (const reply of event.reply_texts || []) feed.appendChild(bubble("assistant", reply));
    if (event.skip_reply) feed.appendChild(bubble("system", `未回复 · ${event.skip_reason || ""}`));
  }
  if (!events.length) feed.appendChild(bubble("system", "暂无记录"));

  const latest = events[0] || {};
  const detail = $("turnDetail");
  clear(detail);
  detail.appendChild(row("时间", latest.time || "--"));
  detail.appendChild(row("来源", sourceLabel(latest.raw_source)));
  detail.appendChild(row("路由", latest.route || "--"));
  detail.appendChild(row("上下文", latest.context_policy || "--"));
  detail.appendChild(row("耗时", latest.elapsed_ms ? `${latest.elapsed_ms}ms` : "--"));
  detail.appendChild(row("发送", latest.send_status || "--"));
  detail.appendChild(row("工具", (latest.tool_calls || []).map((t) => t.name || "tool").join("、") || "无"));
}

function renderMemory() {
  const list = $("memoryList");
  clear(list);
  for (const mem of memories) {
    list.appendChild(row(mem.category || "memory", shortText(mem.content || "", 140)));
  }
  if (!memories.length) list.appendChild(row("记忆", "暂无"));

  const recapList = $("recapList");
  clear(recapList);
  for (const recap of dashboard?.memory?.recaps || []) {
    recapList.appendChild(row(formatClock(recap.created_at), shortText(recap.recap_text || "", 160)));
  }
  for (const fact of dashboard?.memory?.shared_facts || []) {
    recapList.appendChild(row("共享事实", shortText(fact.content || "", 160)));
  }
  if (!recapList.children.length) recapList.appendChild(row("摘要", "暂无"));
}

function renderBotSelect() {
  const select = $("botSelect");
  if (!select || !bots) return;
  clear(select);
  for (const [botId, bot] of Object.entries(bots.bots || {})) {
    const opt = document.createElement("option");
    opt.value = botId;
    opt.textContent = bot.display_name || botId;
    opt.selected = botId === bots.current_bot_id;
    select.appendChild(opt);
  }
}

function renderLogSummary() {
  const list = $("logSummary");
  if (!list) return;
  clear(list);
  const events = dashboard?.events || [];
  const issueCount = events.filter((e) => e.skip_reply || e.send_status === "发送失败").length;
  const toolCount = events.reduce((sum, e) => sum + (e.tool_calls || []).length, 0);
  list.appendChild(row("最近事件", String(events.length)));
  list.appendChild(row("异常/跳过", String(issueCount), issueCount ? "warn" : "ok"));
  list.appendChild(row("工具调用", String(toolCount)));
}

async function savePersona() {
  await api("/admin/persona", {
    method: "POST",
    body: JSON.stringify({ content: $("personaText").value }),
  });
  toast("已保存人设");
  await loadConfig();
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
    }),
  });
  toast("已保存工具设置");
  await loadConfig();
}

async function saveEmail() {
  await api("/admin/settings", {
    method: "POST",
    body: JSON.stringify({
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
  toast("已保存邮箱设置");
  await loadConfig();
}

async function manualSend() {
  const text = $("manualText").value.trim();
  if (!text) {
    toast("请输入消息");
    return;
  }
  await api("/admin/send", {
    method: "POST",
    body: JSON.stringify({ text }),
  });
  $("manualText").value = "";
  toast("已发送");
  await loadDashboard();
}

async function generateRecap() {
  try {
    await api("/admin/recaps/generate", { method: "POST", body: "{}" });
    toast("已触发摘要");
    await loadDashboard();
  } catch (err) {
    toast(err.message);
  }
}

async function exportMemory() {
  window.location.href = "/memories/export";
}

async function importMemory() {
  const file = $("memoryFile").files?.[0];
  if (!file) {
    toast("请选择文件");
    return;
  }
  const form = new FormData();
  form.append("file", file);
  form.append("clear_before", $("clearBeforeImport").checked ? "true" : "false");
  const res = await fetch("/memories/import", {
    method: "POST",
    credentials: "same-origin",
    body: form,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || "导入失败");
  $("memoryImportHint").textContent = `已导入 ${data.imported || 0} 条`;
  await loadMemories();
  renderMemory();
}

async function forgetAll() {
  if (!confirm("确认清空记忆和历史？")) return;
  await api("/admin/dev/forget", { method: "POST", body: "{}" });
  toast("已清空");
  await loadAll();
}

async function startDeploy() {
  const data = await api("/wechat/deploy/start", { method: "POST", body: "{}" });
  deployQrCode = data.qrcode || "";
  renderQr(data);
  startDeployPolling();
}

function renderQr(data) {
  const wrap = $("qrWrap");
  wrap.hidden = false;
  $("qrSvg").innerHTML = data.qrcode_svg || "";
  $("qrImage").hidden = !data.qrcode_img;
  if (data.qrcode_img) $("qrImage").src = data.qrcode_img;
  $("qrLink").hidden = !data.qrcode_url;
  if (data.qrcode_url) $("qrLink").href = data.qrcode_url;
}

function startDeployPolling() {
  clearInterval(deployPollTimer);
  deployPollTimer = setInterval(async () => {
    if (!deployQrCode || deployPollInFlight) return;
    deployPollInFlight = true;
    try {
      const data = await api(`/wechat/deploy/status?qrcode=${encodeURIComponent(deployQrCode)}`);
      if (data.status === "deployed") {
        clearInterval(deployPollTimer);
        toast("微信部署已完成");
        await loadAll();
      }
    } finally {
      deployPollInFlight = false;
    }
  }, 2000);
}

async function startBridge() {
  await api("/wechat/start", { method: "POST", body: "{}" });
  toast("桥接已启动");
  await loadConfig();
}

async function stopBridge() {
  await api("/wechat/stop", { method: "POST", body: "{}" });
  toast("桥接已停止");
  await loadConfig();
}

async function switchBot() {
  const botId = $("botSelect").value;
  await api("/admin/bots/switch", {
    method: "POST",
    body: JSON.stringify({ bot_id: botId }),
  });
  toast("已切换 bot");
  await loadAll();
}

async function loadLogs() {
  const contains = logIssuesOnly ? "&contains=error" : "";
  const data = await api(`/admin/logs?limit=200${contains}`);
  $("logMeta").textContent = data.exists ? `${data.path}${data.truncated ? " · 已截断" : ""}` : "日志不存在";
  $("logConsole").textContent = (data.lines || []).join("\n") || "暂无日志";
}

function switchTab(tab) {
  activeTab = tab;
  document.querySelectorAll(".panel").forEach((panel) => {
    panel.classList.toggle("active", panel.id === tab);
  });
  document.querySelectorAll("[data-tab]").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.tab === tab);
  });
  $("pageTitle").textContent = pageTitles[tab] || "LightChat";
  if (tab === "logs") loadLogs();
}

function switchSettingsView(view) {
  document.querySelectorAll(".settings-view").forEach((el) => {
    el.classList.toggle("active", el.id === view);
  });
}

function bindEvents() {
  $("loginBtn")?.addEventListener("click", login);
  $("loginPassword")?.addEventListener("keydown", (event) => {
    if (event.key === "Enter") login();
  });
  $("logoutBtn")?.addEventListener("click", logout);
  $("refreshBtn")?.addEventListener("click", loadAll);
  $("overviewRefreshBtn")?.addEventListener("click", loadAll);

  document.querySelectorAll("[data-tab]").forEach((btn) => {
    btn.addEventListener("click", () => switchTab(btn.dataset.tab));
  });
  document.querySelectorAll("[data-chat-filter]").forEach((btn) => {
    btn.addEventListener("click", () => {
      chatFilter = btn.dataset.chatFilter;
      document.querySelectorAll("[data-chat-filter]").forEach((b) => b.classList.toggle("active", b === btn));
      renderChat();
    });
  });
  document.querySelectorAll("[data-settings-view]").forEach((btn) => {
    btn.addEventListener("click", () => switchSettingsView(btn.dataset.settingsView));
  });
  document.querySelectorAll(".back-settings").forEach((btn) => {
    btn.addEventListener("click", () => switchSettingsView("settingsHome"));
  });

  $("savePersonaBtn")?.addEventListener("click", savePersona);
  $("saveToolsBtn")?.addEventListener("click", saveTools);
  $("saveEmailBtn")?.addEventListener("click", saveEmail);
  $("manualSendBtn")?.addEventListener("click", manualSend);
  $("generateRecapBtn")?.addEventListener("click", generateRecap);
  $("exportMemoryBtn")?.addEventListener("click", exportMemory);
  $("importMemoryBtn")?.addEventListener("click", () => importMemory().catch((err) => toast(err.message)));
  $("forgetAllBtn")?.addEventListener("click", forgetAll);
  $("startDeployBtn")?.addEventListener("click", startDeploy);
  $("startBridgeBtn")?.addEventListener("click", startBridge);
  $("stopBridgeBtn")?.addEventListener("click", stopBridge);
  $("botSelect")?.addEventListener("change", switchBot);
  $("refreshLogsBtn")?.addEventListener("click", loadLogs);
  $("logIssuesOnly")?.addEventListener("click", () => {
    logIssuesOnly = !logIssuesOnly;
    $("logIssuesOnly").classList.toggle("active", logIssuesOnly);
    loadLogs();
  });
  $("logAutoRefresh")?.addEventListener("click", () => {
    const btn = $("logAutoRefresh");
    if (logPollTimer) {
      clearInterval(logPollTimer);
      logPollTimer = null;
      btn.classList.remove("active");
    } else {
      logPollTimer = setInterval(() => { if (activeTab === "logs") loadLogs(); }, 5000);
      btn.classList.add("active");
    }
  });
}

bindEvents();
checkAuth();
