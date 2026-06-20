<#
.SYNOPSIS
    准备 Claude Code channel 环境并启动 Claude（前台交互终端）。

.DESCRIPTION
    定位仓库根目录，从 .env 读取 CHAT_API_KEY（不打印），设置 ASPOCKET_* 环境变量，
    校验 claude CLI 可用、aspocket MCP server 已注册，然后前台启动 Claude Code
    development channel。

    - aspocket 未注册时，打印注册命令并退出，不自动改用户配置。
    - 不打印 token。
    - 必须在真实交互终端运行；不要用无 TTY 后台方式启动本脚本。

.PARAMETER RouterUrl
    Router 基址，默认 http://127.0.0.1:8899。

.PARAMETER SessionId
    Channel 注册的 session id，默认 aspocket-claude-1。

.PARAMETER RemoteAgentId
    Router remoteAgentId，默认 aspocket-claude-1。

.PARAMETER DisplayName
    App 联系人显示名，默认 "Claude Code - AsPocket"。

.PARAMETER Workspace
    工作区标签（默认仓库根目录），写入 channel description。

.EXAMPLE
    .\scripts\start-aspocket-claude-channel.ps1
    .\scripts\start-aspocket-claude-channel.ps1 -SessionId aspocket-claude-2

.NOTES
    不自动注册 MCP server，不自动修改 ~/.claude.json。
#>
[CmdletBinding()]
param(
    [string]$RouterUrl = 'http://127.0.0.1:8899',
    [string]$SessionId = 'aspocket-claude-1',
    [string]$RemoteAgentId = 'aspocket-claude-1',
    [string]$DisplayName = 'Claude Code - AsPocket',
    [string]$Workspace
)

$ErrorActionPreference = 'Stop'

# 定位仓库根目录：脚本位于 <root>/scripts/ 下。
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptDir
Set-Location $repoRoot

if (-not $Workspace) { $Workspace = $repoRoot }

$envFile = Join-Path $repoRoot '.env'
$channelJs = Join-Path $repoRoot 'tools\aspocket_channel\channel.js'

function Read-EnvKey {
    # 从 .env 读取单个 key 的值。不打印。
    param([string]$Key)
    if (-not (Test-Path $envFile)) { return $null }
    foreach ($line in Get-Content -LiteralPath $envFile) {
        if ($line -notmatch '^\s*#' -and $line -match '^\s*([^=]+)\s*=\s*(.*)\s*$') {
            if ($matches[1].Trim() -eq $Key) {
                return $matches[2].Trim()
            }
        }
    }
    return $null
}

# 从 .env 读取 CHAT_API_KEY 到当前进程环境，不打印。
$chatApiKey = Read-EnvKey -Key 'CHAT_API_KEY'
$tokenLoaded = [bool]$chatApiKey
if ($chatApiKey) {
    $env:CHAT_API_KEY = $chatApiKey
}

# 注入 channel 环境变量（参数可覆盖）。
$env:ASPOCKET_ROUTER_URL = $RouterUrl
$env:ASPOCKET_SESSION_ID = $SessionId
$env:ASPOCKET_REMOTE_AGENT_ID = $RemoteAgentId
$env:ASPOCKET_DISPLAY_NAME = $DisplayName
$env:ASPOCKET_WORKSPACE = $Workspace

Write-Host "Router URL:    $RouterUrl"
Write-Host "Session id:    $SessionId"
Write-Host "Remote agent:  $RemoteAgentId"
Write-Host "Display name:  $DisplayName"
Write-Host "Workspace:     $Workspace"
Write-Host "token loaded:  $(if ($tokenLoaded) { 'yes' } else { 'no' })"
if (-not $tokenLoaded) {
    Write-Host "提示：未在 .env 找到 CHAT_API_KEY，channel 注册可能被 Router 拒绝。" -ForegroundColor Yellow
}
Write-Host '---'

# 校验 claude CLI 可用。
$claudeCmd = Get-Command claude -ErrorAction SilentlyContinue
if (-not $claudeCmd) {
    Write-Host "错误：找不到 claude CLI。请先安装 Claude Code 并加入 PATH。" -ForegroundColor Red
    exit 1
}
& claude --version | Out-Host

# 校验 aspocket MCP server 已注册。
# `claude mcp get aspocket`：已注册退出码 0（即使连接 failed 也算已注册）；
# 未注册退出码 1（输出 "No MCP server found"）。连接状态不影响启动——
# 真实启动时会继承本终端注入的环境变量。
$mcpGetOutput = & claude mcp get aspocket 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "aspocket MCP server 尚未注册。" -ForegroundColor Yellow
    Write-Host "请先执行（仅首次或路径变更时）："
    Write-Host "  claude mcp add --scope user aspocket -- node `"$channelJs`""
    Write-Host ""
    Write-Host "注册后重新运行本脚本。" -ForegroundColor Yellow
    exit 1
}
Write-Host "aspocket MCP server 已注册。" -ForegroundColor Green
Write-Host "（如显示 'Failed to connect' 属正常：健康检查未注入运行环境变量，真实启动时会注入。）" -ForegroundColor DarkGray
Write-Host '---'

# 提醒：必须交互 TTY。
Write-Host "提醒：" -ForegroundColor Cyan
Write-Host "  - 必须在真实交互终端运行本脚本；不要用无 TTY 后台方式启动。"
Write-Host "  - Claude Code 请求 development channel / tool permission 时需要用户确认。"
Write-Host "  - 启动后保持终端前台，按 Ctrl+C 停止。"
Write-Host '---'

# 前台启动 Claude Code development channel。
& claude --dangerously-load-development-channels server:aspocket
