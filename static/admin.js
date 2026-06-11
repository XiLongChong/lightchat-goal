const loginPanel = document.querySelector("#login-panel");
const consolePanel = document.querySelector("#console");
const clearPanel = document.querySelector("#clear-panel");
const loginError = document.querySelector("#login-error");
const details = document.querySelector("#details");
const qrPanel = document.querySelector("#qr-panel");
const qrImage = document.querySelector("#qr-image");
const qrStatus = document.querySelector("#qr-status");
const clearResult = document.querySelector("#clear-result");
const contextUsagePanel = document.querySelector("#context-usage-panel");
const contextUsageEmpty = document.querySelector("#context-usage-empty");
const contextUsageContent = document.querySelector("#context-usage-content");
const contextUsageValue = document.querySelector("#context-usage-value");
const contextUsageMeta = document.querySelector("#context-usage-meta");
const contextUsageTrack = document.querySelector("#context-usage-track");
const contextUsageBar = document.querySelector("#context-usage-bar");
const memoryPanel = document.querySelector("#memory-panel");
const memoryList = document.querySelector("#memory-list");
const memoryContent = document.querySelector("#memory-content");
const memoryWarnings = document.querySelector("#memory-warnings");
let deployPoll = null;
let contextUsagePoll = null;

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

function showConsole(authenticated) {
  loginPanel.hidden = authenticated;
  consolePanel.hidden = !authenticated;
  clearPanel.hidden = !authenticated;
  contextUsagePanel.hidden = !authenticated;
  memoryPanel.hidden = !authenticated;
  clearInterval(contextUsagePoll);
  contextUsagePoll = null;
  if (authenticated) {
    refreshStatus();
    refreshContextUsage();
    refreshMemories();
    contextUsagePoll = setInterval(refreshContextUsage, 10000);
  }
}

function contextUsageLevel(percent) {
  if (percent >= 90) return "danger";
  if (percent >= 70) return "warning";
  return "normal";
}

async function refreshContextUsage() {
  try {
    const usage = await request("/admin/context-usage");
    if (!usage.available) {
      contextUsageEmpty.textContent = "暂无上下文数据";
      contextUsageEmpty.hidden = false;
      contextUsageContent.hidden = true;
      contextUsageMeta.textContent = "最近完成轮次的峰值输入占用";
      return;
    }

    const peak = Number(usage.prompt_tokens_peak);
    const windowSize = Number(usage.context_window_tokens);
    const percent = Number(usage.usage_percent);
    const level = contextUsageLevel(percent);
    const measuredAt = usage.measured_at
      ? new Date(usage.measured_at * 1000).toLocaleString()
      : "";
    const estimateLabel = usage.estimated ? " · 估算值" : "";

    contextUsageEmpty.hidden = true;
    contextUsageContent.hidden = false;
    contextUsageValue.textContent =
      `${peak.toLocaleString()} / ${windowSize.toLocaleString()} tokens · ${percent.toFixed(2)}%`;
    contextUsageMeta.textContent =
      [usage.model, measuredAt].filter(Boolean).join(" · ") + estimateLabel;
    contextUsageTrack.className = `context-usage-track ${level}`;
    contextUsageTrack.setAttribute("aria-valuenow", String(percent));
    contextUsageTrack.setAttribute("aria-valuemax", "100");
    contextUsageBar.style.width = `${Math.min(Math.max(percent, 0), 100)}%`;
  } catch (error) {
    contextUsageEmpty.hidden = false;
    contextUsageContent.hidden = true;
    contextUsageEmpty.textContent = `读取失败: ${error.message}`;
  }
}

async function refreshMemories() {
  memoryList.textContent = "正在读取";
  memoryContent.hidden = true;
  try {
    const result = await request("/admin/memories");
    memoryList.textContent = "";
    const entries = result.entries || [];
    if (!entries.length) {
      memoryList.textContent = "暂无长期记忆";
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
        memoryContent.textContent = detail.message || "";
        memoryContent.hidden = false;
      });
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "danger";
      remove.textContent = "删除";
      remove.addEventListener("click", async () => {
        if (!window.confirm(`确定删除“${entry.title}”吗？`)) return;
        await request(`/admin/memories/${encodeURIComponent(entry.filename)}`, {
          method: "DELETE",
        });
        await refreshMemories();
      });
      actions.append(view, remove);
      row.append(info, actions);
      memoryList.append(row);
    }
    const warnings = result.warnings || [];
    memoryWarnings.hidden = !warnings.length;
    memoryWarnings.textContent = warnings.map((item) => item.message).join("\n");
  } catch (error) {
    memoryList.textContent = error.message;
  }
}

async function refreshStatus() {
  try {
    const status = await request("/wechat/status");
    document.querySelector("#wechat-summary").textContent =
      status.running ? "微信连接运行中" : "微信连接未启动";
    details.textContent = JSON.stringify(status, null, 2);
  } catch (error) {
    details.textContent = error.message;
  }
}

async function checkAuth() {
  const health = await fetch("/health").then((response) => response.json()).catch(() => null);
  document.querySelector("#health").textContent = health ? "服务在线" : "服务不可用";
  const auth = await request("/admin/auth/status");
  showConsole(auth.authenticated);
}

document.querySelector("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
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

document.querySelector("#refresh").addEventListener("click", async () => {
  await Promise.all([refreshStatus(), refreshContextUsage()]);
});
document.querySelector("#start").addEventListener("click", async () => {
  await request("/wechat/start", { method: "POST", body: "{}" });
  await refreshStatus();
});
document.querySelector("#stop").addEventListener("click", async () => {
  await request("/wechat/stop", { method: "POST", body: "{}" });
  await refreshStatus();
});

document.querySelector("#deploy").addEventListener("click", async () => {
  qrPanel.hidden = false;
  qrStatus.textContent = "正在生成二维码";
  const startResp = await request("/wechat/deploy/start", { method: "POST", body: "{}" });
  qrImage.src = `/wechat/deploy/qr-image?data=${encodeURIComponent(startResp.qrcode_content)}&t=${Date.now()}`;
  clearInterval(deployPoll);
  deployPoll = setInterval(async () => {
    try {
      const status = await request(`/wechat/deploy/status?qrcode=${encodeURIComponent(startResp.qrcode)}`);
      qrStatus.textContent = status.message || status.status || "等待扫码";
      if (status.status === "confirmed" || status.status === "expired" || status.status === "failed") {
        clearInterval(deployPoll);
        await refreshStatus();
      }
    } catch (error) {
      qrStatus.textContent = error.message;
      clearInterval(deployPoll);
    }
  }, 1800);
});

document.querySelector("#clear-all").addEventListener("click", async () => {
  const button = document.querySelector("#clear-all");
  button.disabled = true;
  button.textContent = "清除中...";
  clearResult.hidden = true;
  try {
    const result = await request("/admin/dev/reset-context", { method: "POST", body: "{}" });
    const lines = [];
    for (const [table, count] of Object.entries(result.cleared || {})) {
      lines.push(`${table}: ${count} 条`);
    }
    lines.push(`长期记忆: ${(result.long_term_memory || {}).files || 0} 个文件`);
    clearResult.textContent = "已清除:\n" + lines.join("\n");
    await refreshContextUsage();
    await refreshMemories();
  } catch (error) {
    clearResult.textContent = "清除失败: " + error.message;
  }
  clearResult.hidden = false;
  button.textContent = "一键清除";
  button.disabled = false;
});

document.querySelector("#refresh-memories").addEventListener("click", refreshMemories);

document.querySelector("#compress-now").addEventListener("click", async () => {
  const button = document.querySelector("#compress-now");
  button.disabled = true;
  button.textContent = "压缩中...";
  clearResult.hidden = true;
  try {
    const result = await request("/admin/dev/compress", { method: "POST", body: "{}" });
    if (result.status === "compressed") {
      clearResult.textContent = "压缩完成 (" + result.user_id + ")";
    } else {
      clearResult.textContent = "跳过: " + (result.reason || "无事件");
    }
  } catch (error) {
    clearResult.textContent = "压缩失败: " + error.message;
  }
  clearResult.hidden = false;
  button.textContent = "立即压缩";
  button.disabled = false;
});

checkAuth().catch((error) => {
  loginError.textContent = error.message;
  loginError.hidden = false;
});
