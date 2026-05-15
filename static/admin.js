const $ = (id) => document.getElementById(id);

let current = null;
let deployPollTimer = null;
let deployQrCode = "";
let deployPollInFlight = false;

function applyTheme(theme) {
  const next = theme || localStorage.getItem("goal-theme") || "auto";
  const resolved = next === "auto"
    ? (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")
    : next;
  document.documentElement.dataset.theme = resolved;
  localStorage.setItem("goal-theme", next === "dark" ? "dark" : next === "light" ? "light" : "auto");
}

function toggleTheme() {
  const currentTheme = localStorage.getItem("goal-theme") || "auto";
  const next = currentTheme === "dark" ? "light" : "dark";
  applyTheme(next);
}

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
    await loadConfig();
  } else {
    showLogin();
  }
}

async function login() {
  const btn = $("loginBtn");
  btn.textContent = "登录中...";
  btn.disabled = true;
  try {
    await api("/admin/auth/login", {
      method: "POST",
      body: JSON.stringify({ password: $("loginPassword").value }),
    });
    $("loginPassword").value = "";
    $("loginError").hidden = true;
    showApp();
    await loadConfig();
  } catch (err) {
    $("loginError").textContent = "密码不正确，请再试一次";
    $("loginError").hidden = false;
    $("loginPassword").value = "";
    $("loginPassword").focus();
  } finally {
    btn.textContent = "登录";
    btn.disabled = false;
  }
}

async function logout() {
  await api("/admin/auth/logout", {
    method: "POST",
    body: JSON.stringify({}),
  });
  showLogin();
}

function describeStatus(data) {
  const inbound = data.wechat?.inbound_seen;
  const outbound = data.wechat?.outbound_available;
  const contact = data.contact?.context_token ? "真实联系人已记录" : "未记录真实联系人";
  if (outbound && data.send_ready) return `内置发送已就绪 · ${contact}`;
  if (outbound) return "内置微信已启动 · 请先从手机发一条消息";
  if (inbound) return "聊天入口已收到消息 · 主动发送未启动";
  return "等待手机发来第一条消息 · 主动发送未启动";
}

function describeDeploy(data) {
  if (data.wechat?.running && data.wechat?.outbound_available) {
    return {
      title: "已部署",
      detail: `已保存 ${data.wechat.token_masked || "token"}，可主动发送`,
    };
  }
  if (data.wechat?.configured) {
    return {
      title: "已保存部署",
      detail: "点击“启动已保存部署”恢复内置微信连接",
    };
  }
  return {
    title: "未部署",
    detail: "点击“开始部署”，扫码后会保存配置",
  };
}

function qrImageSrc(value) {
  if (!value) return "";
  if (value.startsWith("data:")) return value;
  if (value.startsWith("http://") || value.startsWith("https://")) return value;
  return `data:image/png;base64,${value}`;
}

function setValue(id, value) {
  const el = $(id);
  if (!el) return;
  if (el.type === "checkbox") {
    el.checked = value === true || value === "true";
  } else {
    el.value = value ?? "";
  }
}

function getNumber(id) {
  const value = Number($(id).value);
  return Number.isFinite(value) ? value : 0;
}

function getBool(id) {
  return $(id).checked;
}

async function loadConfig() {
  current = await api("/admin/config");
  $("personaText").value = current.persona || "";

  const s = current.settings.scheduler;
  setValue("schedulerEnabled", s.enabled);
  setValue("intervalMinutes", s.interval_minutes);
  setValue("minProactiveInterval", s.min_proactive_interval);
  setValue("minChatGapHours", s.min_chat_gap_hours);
  setValue("maxDailyProactive", s.max_daily_proactive);
  setValue("scheduledCheckSeconds", s.scheduled_check_seconds);
  setValue("sleepStart", s.sleep_start);
  setValue("sleepEnd", s.sleep_end);

  const w = current.settings.wechat;
  setValue("messageDelay", w.message_delay);

  const weather = current.settings.weather;
  setValue("weatherTimeout", weather.timeout_seconds);
  setValue("weatherCache", weather.cache_ttl_seconds);

  const email = current.settings.email;
  setValue("emailEnabled", email.enabled);
  setValue("smtpHost", email.smtp_host);
  setValue("smtpPort", email.smtp_port);
  setValue("emailUsername", email.username);
  setValue("emailFrom", email.from_addr);
  setValue("emailDefaultTo", email.default_to);

  $("statusText").textContent = describeStatus(current);
  const deploy = describeDeploy(current);
  $("deployStatus").textContent = deploy.title;
  $("deployDetail").textContent = deploy.detail;
  $("deployBadge").textContent = current.wechat?.configured ? "已部署" : "未部署";
  $("deployBadge").classList.toggle("ok", Boolean(current.wechat?.configured));

  const canSend = Boolean(current.send_ready);
  $("manualSendBtn").disabled = !canSend;
  $("sendHint").textContent = canSend
    ? "会通过内置 iLink 发送给最近一次微信联系人"
    : "部署成功后，请先从手机给这个内置 bot 发一条消息，记录真实微信会话后才可主动发送";
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
        enabled: getBool("schedulerEnabled"),
        interval_minutes: getNumber("intervalMinutes"),
        min_proactive_interval: getNumber("minProactiveInterval"),
        min_chat_gap_hours: getNumber("minChatGapHours"),
        max_daily_proactive: getNumber("maxDailyProactive"),
        scheduled_check_seconds: getNumber("scheduledCheckSeconds"),
        sleep_start: getNumber("sleepStart"),
        sleep_end: getNumber("sleepEnd"),
      },
    }),
  });
  toast("主动设置已保存");
}

async function saveTools() {
  await api("/admin/settings", {
    method: "POST",
    body: JSON.stringify({
      wechat: {
        message_delay: getNumber("messageDelay"),
      },
      weather: {
        timeout_seconds: getNumber("weatherTimeout"),
        cache_ttl_seconds: getNumber("weatherCache"),
      },
      email: {
        enabled: getBool("emailEnabled"),
        smtp_host: $("smtpHost").value,
        smtp_port: getNumber("smtpPort"),
        username: $("emailUsername").value,
        from_addr: $("emailFrom").value,
        default_to: $("emailDefaultTo").value,
      },
    }),
  });
  toast("工具设置已保存");
}

async function triggerScheduler() {
  const data = await api("/scheduler/trigger", {
    method: "POST",
    body: JSON.stringify({}),
  });
  toast(data.result?.sent ? "已发送主动消息" : data.result?.reason || "未发送");
}

async function startDeploy() {
  const data = await api("/wechat/deploy/start", {
    method: "POST",
    body: JSON.stringify({}),
  });
  deployQrCode = data.qrcode || "";
  if (!deployQrCode || (!data.qrcode_svg && !data.qrcode_img && !data.qrcode_url)) {
    $("deployStatus").textContent = "二维码生成失败";
    $("deployDetail").textContent = `返回字段：${Object.keys(data).join(", ") || "空"}`;
    toast("二维码生成失败");
    return;
  }
  $("qrSvg").innerHTML = data.qrcode_svg || "";
  $("qrSvg").hidden = !data.qrcode_svg;
  $("qrImage").hidden = Boolean(data.qrcode_svg);
  if (!data.qrcode_svg && data.qrcode_img) {
    $("qrImage").src = qrImageSrc(data.qrcode_img);
  }
  const qrUrl = data.qrcode_url || data.qrcode_img || "";
  $("qrLink").hidden = !qrUrl;
  $("qrLink").href = qrUrl || "#";
  $("qrWrap").hidden = false;
  $("deployStatus").textContent = "等待扫码";
  $("deployDetail").textContent = data.qrcode_svg
    ? "请用微信扫码确认"
    : "后端缺少 qrcode 依赖，先点击下方链接或安装 qrcode 后重启";
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
        $("deployStatus").textContent = "部署成功";
        $("deployDetail").textContent = "已保存配置并启动内置微信连接";
        $("qrWrap").hidden = true;
        toast("部署成功");
        await loadConfig();
      } else if (status === "expired") {
        clearInterval(deployPollTimer);
        deployPollTimer = null;
        $("deployStatus").textContent = "二维码已过期";
        $("deployDetail").textContent = "请重新点击开始部署";
      } else if (status === "scanned" || status === "scaned") {
        $("deployStatus").textContent = "已扫码";
        $("deployDetail").textContent = "请在手机上确认";
      } else {
        $("deployStatus").textContent = "等待扫码";
      }
    } catch (err) {
      $("deployDetail").textContent = err.message;
    } finally {
      deployPollInFlight = false;
    }
  }, 1800);
}

async function startSavedBridge() {
  try {
    await api("/wechat/start", {
      method: "POST",
      body: JSON.stringify({}),
    });
    toast("已启动");
    await loadConfig();
  } catch (err) {
    toast(err.message);
  }
}

async function manualSend() {
  try {
    await api("/admin/send", {
      method: "POST",
      body: JSON.stringify({ text: $("manualText").value }),
    });
    $("manualText").value = "";
    toast("已发送");
  } catch (err) {
    toast(err.message);
  }
}

async function loadTasks() {
  const data = await api("/scheduler/messages?limit=20");
  const list = $("taskList");
  list.innerHTML = "";
  for (const task of data.tasks || []) {
    const item = document.createElement("div");
    item.className = "task";
    const due = new Date(task.due_at * 1000).toLocaleString();
    const first = task.messages?.[0]?.text || "";
    item.innerHTML = `<strong>${task.status} · ${due}</strong><div>${first}</div>`;
    list.appendChild(item);
  }
}

async function importMemory() {
  const file = $("memoryFile").files?.[0];
  if (!file) {
    toast("先选择一个记忆文件");
    return;
  }
  const clear = $("clearBeforeImport").checked;
  try {
    const data = await apiRaw(`/memories/import?clear_existing=${clear ? "true" : "false"}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/octet-stream",
        "X-Filename": encodeURIComponent(file.name),
      },
      body: await file.arrayBuffer(),
    });
    $("memoryImportHint").textContent =
      `解析 ${data.parsed} 条，导入 ${data.imported} 条，跳过 ${data.skipped} 条，清空 ${data.cleared} 条`;
    toast("记忆已导入");
  } catch (err) {
    $("memoryImportHint").textContent = err.message;
    toast(err.message);
  }
}

function exportMemory() {
  window.location.href = "/memories/export";
}

document.querySelectorAll(".nav-item").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".nav-item").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    $(btn.dataset.tab).classList.add("active");
  });
});

$("loginBtn").addEventListener("click", login);
$("loginPassword").addEventListener("keydown", (event) => {
  if (event.key === "Enter") login();
});
$("logoutBtn").addEventListener("click", logout);
$("themeBtn").addEventListener("click", toggleTheme);
$("refreshBtn").addEventListener("click", () => loadConfig().then(() => toast("已刷新")));
$("savePersonaBtn").addEventListener("click", savePersona);
$("saveSchedulerBtn").addEventListener("click", saveScheduler);
$("saveToolsBtn").addEventListener("click", saveTools);
$("triggerSchedulerBtn").addEventListener("click", triggerScheduler);
$("manualSendBtn").addEventListener("click", manualSend);
$("loadTasksBtn").addEventListener("click", loadTasks);
$("importMemoryBtn").addEventListener("click", importMemory);
$("exportMemoryBtn").addEventListener("click", exportMemory);
$("startDeployBtn").addEventListener("click", () => startDeploy().catch((err) => toast(err.message)));
$("startBridgeBtn").addEventListener("click", startSavedBridge);

applyTheme();
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  if ((localStorage.getItem("goal-theme") || "auto") === "auto") applyTheme("auto");
});
checkAuth().catch((err) => {
  toast(err.message);
  showLogin();
});
