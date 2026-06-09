const loginPanel = document.querySelector("#login-panel");
const consolePanel = document.querySelector("#console");
const loginError = document.querySelector("#login-error");
const details = document.querySelector("#details");
const qrPanel = document.querySelector("#qr-panel");
const qrImage = document.querySelector("#qr-image");
const qrStatus = document.querySelector("#qr-status");
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

checkAuth().catch((error) => {
  loginError.textContent = error.message;
  loginError.hidden = false;
});
