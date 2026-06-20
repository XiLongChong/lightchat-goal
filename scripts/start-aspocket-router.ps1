<#
.SYNOPSIS
    启动 AsPocket PC Router（前台进程）。

.DESCRIPTION
    定位仓库根目录，从 .env 读取 CHAT_API_KEY 到当前进程环境（不打印），
    设置 HOST/PORT，校验 .venv 解释器存在，然后前台启动 main.py。
    前台运行便于 Ctrl+C 停止；不做后台化。

.PARAMETER Port
    覆盖监听端口，默认 8899。

.PARAMETER Host_
    覆盖监听地址，默认 0.0.0.0。参数名为 Host_ 以避免遮蔽 PowerShell 的 $Host 自动变量。

.EXAMPLE
    .\scripts\start-aspocket-router.ps1
    .\scripts\start-aspocket-router.ps1 -Port 9000

.NOTES
    不打印 CHAT_API_KEY。token 仅以 "loaded: yes/no" 形式体现。
#>
[CmdletBinding()]
param(
    [string]$Host_ = '0.0.0.0',
    [int]$Port = 8899
)

$ErrorActionPreference = 'Stop'

# 定位仓库根目录：脚本位于 <root>/scripts/ 下。
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptDir
Set-Location $repoRoot

$envFile = Join-Path $repoRoot '.env'
$venvPython = Join-Path $repoRoot '.venv\Scripts\python.exe'

function Read-EnvKey {
    # 从 .env 读取单个 key 的值。仅解析 KEY=VALUE 行，忽略注释与空行。
    # 不打印任何内容。找不到则返回 $null。
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

# 设置监听参数（环境变量覆盖 .env 默认值）。
$env:HOST = $Host_
$env:PORT = $Port.ToString()

# 校验 venv 解释器存在。
if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host "错误：找不到 venv 解释器：$venvPython" -ForegroundColor Red
    Write-Host "请先创建虚拟环境并安装依赖（见仓库 README）。" -ForegroundColor Red
    exit 1
}

Write-Host "Router URL:           http://127.0.0.1:$Port"
Write-Host "Android emulator URL: http://10.0.2.2:$Port"
Write-Host "token loaded:         $(if ($tokenLoaded) { 'yes' } else { 'no' })"
if (-not $tokenLoaded) {
    Write-Host "提示：未在 .env 找到 CHAT_API_KEY，Router 可能拒绝 App 请求。" -ForegroundColor Yellow
}
Write-Host "前台运行中，按 Ctrl+C 停止。" -ForegroundColor DarkGray
Write-Host '---'

# 前台启动；保持进程附着终端，便于 Ctrl+C。
& $venvPython 'main.py'
