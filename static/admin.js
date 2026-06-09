const loginPanel = document.querySelector("#login-panel");
const consolePanel = document.querySelector("#console");
const clearPanel = document.querySelector("#clear-panel");
const loginError = document.querySelector("#login-error");
const details = document.querySelector("#details");
const qrPanel = document.querySelector("#qr-panel");
const qrImage = document.querySelector("#qr-image");
const qrStatus = document.querySelector("#qr-status");
const clearResult = document.querySelector("#clear-result");
let deployPoll = null;

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
  if (authenticated) refreshStatus();
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

document.querySelector("#refresh").addEventListener("click", refreshStatus);
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
  await request("/wechat/deploy/start", { method: "POST", body: "{}" });
  qrImage.src = `/wechat/deploy/qr-image?t=${Date.now()}`;
  clearInterval(deployPoll);
  deployPoll = setInterval(async () => {
    try {
      const status = await request("/wechat/deploy/status");
      qrStatus.textContent = status.message || status.status || "等待扫码";
      if (status.status === "connected" || status.status === "failed") {
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
    clearResult.textContent = "已清除:\n" + lines.join("\n");
  } catch (error) {
    clearResult.textContent = "清除失败: " + error.message;
  }
  clearResult.hidden = false;
  button.textContent = "一键清除";
  button.disabled = false;
});

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
