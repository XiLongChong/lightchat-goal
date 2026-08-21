// ── DOM refs ──────────────────────────────────────────────────
const loginPanel = document.querySelector("#login-panel");
const loginError = document.querySelector("#login-error");
const sidebar = document.querySelector("#sidebar");
const navLinks = document.querySelectorAll("aside nav a");
const panels = document.querySelectorAll(".panel");
let autoLogPoll = null;
let deployPoll = null;

// ── request helper ────────────────────────────────────────────
async function request(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `请求失败 (${response.status})`);
  return body;
}

// ── navigation ────────────────────────────────────────────────
function showPanel(name) {
  panels.forEach((p) => { p.hidden = p.id !== `panel-${name}`; });
  navLinks.forEach((a) => { a.classList.toggle("active", a.dataset.panel === name); });
  // Trigger refresh for the newly shown panel
  if (name === "overview") refreshOverview();
  if (name === "inbound") refreshInbound();
  if (name === "app") refreshApp();
  if (name === "chat") refreshChat();
  if (name === "memory") refreshMemories();
  if (name === "schedule") refreshSchedule();
  if (name === "tools") refreshTools();
  if (name === "logs") refreshLogs();
}

navLinks.forEach((a) => {
  a.addEventListener("click", (e) => {
    e.preventDefault();
    showPanel(a.dataset.panel);
  });
});

// ── auth ──────────────────────────────────────────────────────
function showConsole(authenticated) {
  loginPanel.hidden = authenticated;
  sidebar.hidden = !authenticated;
  panels.forEach((p) => { p.hidden = !authenticated || !p.classList.contains("panel"); });
  if (authenticated) {
    showPanel("overview");
  }
}

async function checkAuth() {
  const health = await fetch("/health").then((r) => r.json()).catch(() => null);
  document.querySelector("#health").textContent = health ? "服务在线" : "服务不可用";
  const auth = await request("/admin/auth/status");
  showConsole(auth.authenticated);
}

document.querySelector("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  loginError.hidden = true;
  try {
    await request("/admin/auth/login", {
      method: "POST",
      body: JSON.stringify({ password: document.querySelector("#password").value }),
    });
    showConsole(true);
  } catch (error) {
    loginError.textContent = error.message;
    loginError.hidden = false;
  }
});

// ── Overview ──────────────────────────────────────────────────
async function refreshOverview() {
  try {
    const source = await request("/admin/inbound-source");
    const compression = await request("/admin/compression/status");
    document.querySelector("#card-wechat .card-status").textContent =
      source.wechat_running ? "🟢 运行中" : "⚪ 未启动";
    document.querySelector("#card-app .card-status").textContent =
      source.mode === "wechat" ? "🔴 已关闭" : `🟢 ${source.app_sessions_online} 会话`;
    document.querySelector("#card-llm .card-status").textContent = "🟢 正常";
    document.querySelector("#card-compression .card-status").textContent =
      compression.nextRun ? new Date(compression.nextRun).toLocaleString() : "—";
  } catch (e) {
    console.error("overview refresh failed", e);
  }
}

// Quick actions
async function doCompress(btn, resultEl) {
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = "压缩中...";
  resultEl.hidden = true;
  try {
    const r = await request("/admin/dev/compress", { method: "POST", body: "{}" });
    resultEl.textContent = r.status === "compressed" ? `压缩完成 (${r.user_id})` : `跳过: ${r.reason || "无事件"}`;
  } catch (e) {
    resultEl.textContent = "压缩失败: " + e.message;
  }
  resultEl.hidden = false;
  btn.textContent = old;
  btn.disabled = false;
}

async function doClear(btn, resultEl) {
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = "清除中...";
  resultEl.hidden = true;
  try {
    const r = await request("/admin/dev/reset-context", { method: "POST", body: "{}" });
    const lines = [];
    for (const [table, count] of Object.entries(r.cleared || {})) {
      lines.push(`${table}: ${count} 条`);
    }
    lines.push(`长期记忆: ${(r.long_term_memory || {}).files || 0} 个文件`);
    resultEl.textContent = "已清除:\n" + lines.join("\n");
  } catch (e) {
    resultEl.textContent = "清除失败: " + e.message;
  }
  resultEl.hidden = false;
  btn.textContent = old;
  btn.disabled = false;
}

document.querySelector("#qa-compress").addEventListener("click", (e) => doCompress(e.target, document.querySelector("#qa-result")));
document.querySelector("#qa-clear").addEventListener("click", (e) => doClear(e.target, document.querySelector("#qa-result")));

// ── Inbound Source ────────────────────────────────────────────
async function refreshInbound() {
  try {
    const source = await request("/admin/inbound-source");
    const mode = source.mode;
    document.querySelectorAll('input[name="inbound-mode"]').forEach((el) => {
      el.checked = el.value === mode;
    });
    const wechat = await request("/wechat/status");
    document.querySelector("#iw-status").textContent = wechat.running ? "运行中" : "未启动";
    document.querySelector("#iw-token").textContent = wechat.configured ? "已配置" : "未配置";
  } catch (e) {
    console.error("inbound refresh failed", e);
  }
}

document.querySelector("#save-inbound-mode").addEventListener("click", async () => {
  const checked = document.querySelector('input[name="inbound-mode"]:checked');
  if (!checked) return;
  const msg = document.querySelector("#inbound-mode-msg");
  try {
    await request("/admin/inbound-source", { method: "POST", body: JSON.stringify({ mode: checked.value }) });
    msg.textContent = `已切换至 ${checked.value} 模式`;
    msg.hidden = false;
    refreshInbound();
    refreshOverview();
  } catch (e) {
    msg.textContent = "切换失败: " + e.message;
    msg.hidden = false;
  }
});

document.querySelector("#iw-start").addEventListener("click", async () => {
  await request("/wechat/start", { method: "POST", body: "{}" });
  refreshInbound();
});
document.querySelector("#iw-stop").addEventListener("click", async () => {
  await request("/wechat/stop", { method: "POST", body: "{}" });
  refreshInbound();
});
document.querySelector("#iw-deploy").addEventListener("click", async () => {
  const qrPanel = document.querySelector("#iw-qr-panel");
  const qrImage = document.querySelector("#iw-qr-image");
  const qrStatus = document.querySelector("#iw-qr-status");
  qrPanel.hidden = false;
  qrStatus.textContent = "正在生成二维码";
  const startResp = await request("/wechat/deploy/start", { method: "POST", body: "{}" });
  qrImage.src = `/wechat/deploy/qr-image?data=${encodeURIComponent(startResp.qrcode_content)}&t=${Date.now()}`;
  clearInterval(deployPoll);
  deployPoll = setInterval(async () => {
    try {
      const status = await request(`/wechat/deploy/status?qrcode=${encodeURIComponent(startResp.qrcode)}`);
      qrStatus.textContent = status.message || status.status || "等待扫码";
      if (["confirmed", "expired", "failed"].includes(status.status)) {
        clearInterval(deployPoll);
        refreshInbound();
      }
    } catch (error) {
      qrStatus.textContent = error.message;
      clearInterval(deployPoll);
    }
  }, 1800);
});

// ── App Management ────────────────────────────────────────────
async function refreshApp() {
  try {
    // Sessions
    const sessions = await request("/admin/app-sessions");
    const list = document.querySelector("#app-sessions-list");
    if (!sessions.sessions || !sessions.sessions.length) {
      list.textContent = "暂无会话";
    } else {
      list.innerHTML = sessions.sessions.map((s) => `
        <div class="table-row">
          <div class="table-cell"><strong>${s.sessionId}</strong><br><span style="color:#66756d;font-size:12px">${s.remoteAgentId} · ${s.kind}</span></div>
          <div class="table-cell small">${s.status === "online" ? "🟢" : "🔴"} ${s.status}</div>
          <div class="table-cell small"><button class="secondary" onclick="offlineSession('${s.sessionId}')">离线</button></div>
        </div>
      `).join("");
    }

    // Phone context
    const pc = await request("/admin/phone-context");
    const pcEl = document.querySelector("#phone-context-display");
    if (!pc.available) {
      pcEl.textContent = "暂无数据";
    } else {
      pcEl.textContent = JSON.stringify(pc.payload, null, 2);
    }

    // Attachments
    const atts = await request("/admin/attachments");
    const attList = document.querySelector("#app-attachments-list");
    if (!atts.attachments || !atts.attachments.length) {
      attList.textContent = "暂无附件";
    } else {
      attList.innerHTML = atts.attachments.map((a) => `
        <div class="table-row">
          <div class="table-cell">${a.fileName || a.id}<br><span style="color:#66756d;font-size:12px">${a.mimeType}</span></div>
          <div class="table-cell small">${formatBytes(a.sizeBytes)}</div>
          <div class="table-cell small">${a.createdAt}</div>
        </div>
      `).join("");
    }
  } catch (e) {
    console.error("app refresh failed", e);
  }
}

window.offlineSession = async function(sessionId) {
  try {
    await request(`/admin/app-sessions/${encodeURIComponent(sessionId)}/offline`, { method: "POST", body: "{}" });
    refreshApp();
  } catch (e) {
    alert("离线失败: " + e.message);
  }
};

document.querySelector("#refresh-sessions").addEventListener("click", refreshApp);

// ── Chat Management ───────────────────────────────────────────
async function refreshChat() {
  try {
    const usage = await request("/admin/context-usage");
    const empty = document.querySelector("#chat-context-empty");
    const content = document.querySelector("#chat-context-content");
    if (!usage.available) {
      empty.hidden = false;
      content.hidden = true;
      empty.textContent = "暂无上下文数据";
      return;
    }
    empty.hidden = true;
    content.hidden = false;
    const peak = Number(usage.prompt_tokens_peak);
    const windowSize = Number(usage.context_window_tokens);
    const percent = Number(usage.usage_percent);
    const level = percent >= 90 ? "danger" : percent >= 70 ? "warning" : "normal";
    document.querySelector("#chat-context-value").textContent =
      `${peak.toLocaleString()} / ${windowSize.toLocaleString()} tokens · ${percent.toFixed(2)}%`;
    document.querySelector("#chat-context-meta").textContent =
      [usage.model, usage.measured_at ? new Date(usage.measured_at * 1000).toLocaleString() : ""].filter(Boolean).join(" · ") + (usage.estimated ? " · 估算值" : "");
    const track = document.querySelector("#chat-context-track");
    track.className = `context-usage-track ${level}`;
    track.setAttribute("aria-valuenow", String(percent));
    document.querySelector("#chat-context-bar").style.width = `${Math.min(Math.max(percent, 0), 100)}%`;
  } catch (e) {
    console.error("chat refresh failed", e);
  }
}

document.querySelector("#send-btn").addEventListener("click", async () => {
  const text = document.querySelector("#send-text").value.trim();
  if (!text) return;
  const result = document.querySelector("#send-result");
  try {
    await request("/admin/send", { method: "POST", body: JSON.stringify({ text }) });
    result.textContent = "发送成功";
    result.hidden = false;
    document.querySelector("#send-text").value = "";
  } catch (e) {
    result.textContent = "发送失败: " + e.message;
    result.hidden = false;
  }
});

document.querySelector("#chat-compress").addEventListener("click", (e) =>
  doCompress(e.target, document.querySelector("#chat-op-result"))
);
document.querySelector("#chat-clear").addEventListener("click", (e) =>
  doClear(e.target, document.querySelector("#chat-op-result"))
);

// ── Memory ────────────────────────────────────────────────────
async function refreshMemories() {
  const list = document.querySelector("#memory-list");
  const warnings = document.querySelector("#memory-warnings");
  const content = document.querySelector("#memory-content");
  list.textContent = "正在读取";
  content.hidden = true;
  try {
    const result = await request("/admin/memories");
    list.textContent = "";
    const entries = result.entries || [];
    if (!entries.length) {
      list.textContent = "暂无长期记忆";
    }
    for (const entry of entries) {
      const row = document.createElement("div");
      row.className = "memory-row";
      const info = document.createElement("div");
      const title = document.createElement("strong");
      title.textContent = entry.title;
      const summary = document.createElement("p");
      summary.textContent = entry.summary;
      const meta = document.createElement("small");
      meta.textContent = `${entry.filename} · ${entry.bytes} bytes`;
      info.append(title, summary, meta);

      const actions = document.createElement("div");
      actions.className = "memory-actions";
      const view = document.createElement("button");
      view.type = "button";
      view.className = "secondary";
      view.textContent = "查看";
      view.addEventListener("click", async () => {
        const detail = await request(`/admin/memories/${encodeURIComponent(entry.filename)}`);
        content.textContent = detail.message || "";
        content.hidden = false;
      });
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "danger";
      remove.textContent = "删除";
      remove.addEventListener("click", async () => {
        if (!window.confirm(`确定删除"${entry.title}"吗？`)) return;
        await request(`/admin/memories/${encodeURIComponent(entry.filename)}`, { method: "DELETE" });
        await refreshMemories();
      });
      actions.append(view, remove);
      row.append(info, actions);
      list.append(row);
    }
    const ws = result.warnings || [];
    warnings.hidden = !ws.length;
    warnings.textContent = ws.map((item) => item.message).join("\n");
  } catch (error) {
    list.textContent = error.message;
  }
}

document.querySelector("#refresh-memories").addEventListener("click", refreshMemories);

// ── Schedule ──────────────────────────────────────────────────
async function refreshSchedule() {
  try {
    const status = await request("/admin/compression/status");
    document.querySelector("#sch-next-compression").textContent =
      status.nextRun ? new Date(status.nextRun).toLocaleString() : "—";

    const history = await request("/admin/compression/history");
    const histEl = document.querySelector("#compression-history");
    if (!history.summaries || !history.summaries.length) {
      histEl.textContent = "暂无历史";
    } else {
      histEl.innerHTML = history.summaries.map((s) => `
        <div class="table-row">
          <div class="table-cell"><strong>${s.date}</strong></div>
          <div class="table-cell">${s.text}...</div>
        </div>
      `).join("");
    }

    const reminders = await request("/admin/reminders");
    const remEl = document.querySelector("#reminders-list");
    if (!reminders.reminders || !reminders.reminders.length) {
      remEl.textContent = "暂无提醒";
    } else {
      remEl.innerHTML = reminders.reminders.map((r) => `
        <div class="table-row">
          <div class="table-cell"><strong>${r.note || "提醒"}</strong><br><span style="color:#66756d;font-size:12px">${r.deliveryText}</span></div>
          <div class="table-cell small">${r.triggerAt}</div>
          <div class="table-cell small">${r.status}</div>
        </div>
      `).join("");
    }

    const pc = await request("/admin/proactive/config");
    document.querySelector("#pc-enabled").textContent = pc.enabled ? "✅ 启用" : "❌ 禁用";
    document.querySelector("#pc-tier").textContent = pc.frequencyTier;
    document.querySelector("#pc-limit").textContent = pc.dailyLimit;
    document.querySelector("#pc-used").textContent = pc.todayUsed;
    document.querySelector("#pc-quiet").textContent =
      `${pc.quietHours.start} - ${pc.quietHours.end} (${pc.quietHours.timezone})`;
  } catch (e) {
    console.error("schedule refresh failed", e);
  }
}

document.querySelector("#sch-compress-now").addEventListener("click", (e) =>
  doCompress(e.target, document.querySelector("#sch-compress-result"))
);
document.querySelector("#refresh-reminders").addEventListener("click", refreshSchedule);

// ── Tools ─────────────────────────────────────────────────────
async function refreshTools() {
  try {
    const status = await request("/admin/tools/status");
    const deps = document.querySelector("#tool-deps");
    deps.innerHTML = Object.entries(status.dependencies || {}).map(([k, v]) => `
      <div class="table-row">
        <div class="table-cell">${k}</div>
        <div class="table-cell small">${v ? "✅ 已配置" : "❌ 未配置"}</div>
      </div>
    `).join("");

    const list = document.querySelector("#tool-list");
    list.innerHTML = (status.tools || []).map((t) => `
      <div class="table-row">
        <div class="table-cell"><strong>${t.name}</strong></div>
        <div class="table-cell small">${t.category}</div>
        <div class="table-cell small">${t.sideEffect}</div>
      </div>
    `).join("");
  } catch (e) {
    console.error("tools refresh failed", e);
  }
}

// ── Logs ──────────────────────────────────────────────────────
async function refreshLogs() {
  const level = document.querySelector("#log-level").value;
  const contains = document.querySelector("#log-contains").value;
  const limit = document.querySelector("#log-limit").value;
  const content = document.querySelector("#log-content");
  try {
    const params = new URLSearchParams({ limit });
    if (level) params.set("level", level);
    if (contains) params.set("contains", contains);
    const result = await request(`/admin/logs?${params}`);
    content.textContent = (result.lines || []).join("\n") || "(无日志)";
  } catch (e) {
    content.textContent = "读取失败: " + e.message;
  }
}

document.querySelector("#refresh-logs").addEventListener("click", refreshLogs);
document.querySelector("#auto-logs").addEventListener("click", (e) => {
  const btn = e.target;
  if (autoLogPoll) {
    clearInterval(autoLogPoll);
    autoLogPoll = null;
    btn.textContent = "自动刷新";
    btn.classList.add("secondary");
  } else {
    refreshLogs();
    autoLogPoll = setInterval(refreshLogs, 3000);
    btn.textContent = "停止刷新";
    btn.classList.remove("secondary");
  }
});

// ── Utilities ─────────────────────────────────────────────────
function formatBytes(bytes) {
  if (!bytes) return "0 B";
  const k = 1024;
  const sizes = ["B", "KB", "MB", "GB"];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + " " + sizes[i];
}

// ── Init ──────────────────────────────────────────────────────
checkAuth().catch((error) => {
  loginError.textContent = error.message;
  loginError.hidden = false;
});
