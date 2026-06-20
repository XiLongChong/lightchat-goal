<#
.SYNOPSIS
    Check AsPocket Router and Claude channel readiness.

.DESCRIPTION
    Read-only diagnostics for the AsPocket Claude Code channel workflow.
    The script does not start Router, does not start Claude, does not register
    MCP servers, and never prints CHAT_API_KEY.

.PARAMETER RouterUrl
    Router base URL. Defaults to http://127.0.0.1:8899.

.PARAMETER SessionId
    Expected app agent session id. Defaults to aspocket-claude-1.

.PARAMETER RemoteAgentId
    Expected Router remoteAgentId. Defaults to aspocket-claude-1.

.PARAMETER McpName
    Claude Code MCP server name. Defaults to aspocket.

.EXAMPLE
    .\scripts\check-aspocket-channel.ps1
    .\scripts\check-aspocket-channel.ps1 -RouterUrl http://127.0.0.1:9000
#>
[CmdletBinding()]
param(
    [string]$RouterUrl = 'http://127.0.0.1:8899',
    [string]$SessionId = 'aspocket-claude-1',
    [string]$RemoteAgentId = 'aspocket-claude-1',
    [string]$McpName = 'aspocket'
)

$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptDir
Set-Location $repoRoot

$envFile = Join-Path $repoRoot '.env'
$channelJs = Join-Path $repoRoot 'tools\aspocket_channel\channel.js'

function Read-EnvKey {
    param([string]$Key)
    if (-not (Test-Path -LiteralPath $envFile)) { return $null }
    foreach ($line in Get-Content -LiteralPath $envFile) {
        if ($line -notmatch '^\s*#' -and $line -match '^\s*([^=]+)\s*=\s*(.*)\s*$') {
            if ($matches[1].Trim() -eq $Key) {
                return $matches[2].Trim().Trim('"').Trim("'")
            }
        }
    }
    return $null
}

function Join-Url {
    param(
        [string]$Base,
        [string]$Path
    )
    return ($Base.TrimEnd('/') + '/' + $Path.TrimStart('/'))
}

function Write-Check {
    param(
        [string]$Name,
        [bool]$Ok,
        [string]$Detail = ''
    )
    $status = if ($Ok) { 'OK' } else { 'FAIL' }
    $color = if ($Ok) { 'Green' } else { 'Red' }
    if ($Detail) {
        Write-Host ("[{0}] {1} - {2}" -f $status, $Name, $Detail) -ForegroundColor $color
    } else {
        Write-Host ("[{0}] {1}" -f $status, $Name) -ForegroundColor $color
    }
}

function Invoke-RouterGet {
    param([string]$Path)
    $uri = Join-Url -Base $RouterUrl -Path $Path
    return Invoke-RestMethod -Uri $uri -Headers $script:headers -TimeoutSec 8
}

$failures = 0

Write-Host 'AsPocket channel readiness check'
Write-Host "Repo:        $repoRoot"
Write-Host "Router URL:  $RouterUrl"
Write-Host "Emulator URL: $($RouterUrl -replace '^http://127\.0\.0\.1', 'http://10.0.2.2')"
Write-Host "Session:     $SessionId"
Write-Host "RemoteAgent: $RemoteAgentId"
Write-Host '---'

$chatApiKey = Read-EnvKey -Key 'CHAT_API_KEY'
if ($chatApiKey) {
    $env:CHAT_API_KEY = $chatApiKey
    $script:headers = @{ Authorization = "Bearer $chatApiKey" }
    Write-Check -Name 'CHAT_API_KEY loaded from .env' -Ok $true -Detail 'token hidden'
} else {
    $script:headers = @{}
    Write-Check -Name 'CHAT_API_KEY loaded from .env' -Ok $false -Detail 'missing'
    $failures++
}

if (Test-Path -LiteralPath $channelJs) {
    Write-Check -Name 'channel.js exists' -Ok $true -Detail $channelJs
} else {
    Write-Check -Name 'channel.js exists' -Ok $false -Detail $channelJs
    $failures++
}

$claudeCmd = Get-Command claude -ErrorAction SilentlyContinue
if ($claudeCmd) {
    try {
        $version = (& claude --version 2>$null | Select-Object -First 1)
        Write-Check -Name 'claude CLI' -Ok $true -Detail $version
    } catch {
        Write-Check -Name 'claude CLI' -Ok $false -Detail $_.Exception.Message
        $failures++
    }
} else {
    Write-Check -Name 'claude CLI' -Ok $false -Detail 'not found in PATH'
    $failures++
}

if ($claudeCmd) {
    $mcpOutput = & claude mcp get $McpName 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Check -Name "MCP server '$McpName' registered" -Ok $true -Detail 'registered'
    } else {
        Write-Check -Name "MCP server '$McpName' registered" -Ok $false -Detail "run: claude mcp add --scope user $McpName -- node `"$channelJs`""
        $failures++
    }
}

try {
    $profile = Invoke-RouterGet -Path 'api/profile'
    Write-Check -Name 'Router /api/profile' -Ok $true -Detail ("agentName={0}" -f $profile.agentName)
} catch {
    Write-Check -Name 'Router /api/profile' -Ok $false -Detail $_.Exception.Message
    $failures++
}

try {
    $sessionsResp = Invoke-RouterGet -Path 'api/app-agent-sessions'
    $sessions = @($sessionsResp.sessions)
    $session = $sessions | Where-Object { $_.sessionId -eq $SessionId -or $_.remoteAgentId -eq $RemoteAgentId } | Select-Object -First 1
    if ($session) {
        $detail = "sessionId=$($session.sessionId), remoteAgentId=$($session.remoteAgentId), status=$($session.status), kind=$($session.kind)"
        Write-Check -Name 'Router session' -Ok $true -Detail $detail
    } else {
        Write-Check -Name 'Router session' -Ok $false -Detail 'not found; start Claude channel in an interactive terminal'
        $failures++
    }
} catch {
    Write-Check -Name 'Router session' -Ok $false -Detail $_.Exception.Message
    $failures++
}

try {
    $invitationsResp = Invoke-RouterGet -Path 'api/contact-invitations'
    $invitations = @($invitationsResp.invitations)
    $invitation = $invitations | Where-Object { $_.remoteAgentId -eq $RemoteAgentId } | Select-Object -First 1
    if ($invitation) {
        $detail = "displayName=$($invitation.displayName), agentType=$($invitation.agentType), endpoint=$($invitation.endpoint)"
        Write-Check -Name 'Contact invitation' -Ok $true -Detail $detail
    } else {
        Write-Check -Name 'Contact invitation' -Ok $false -Detail 'not found; session may be offline'
        $failures++
    }
} catch {
    Write-Check -Name 'Contact invitation' -Ok $false -Detail $_.Exception.Message
    $failures++
}

try {
    $historyPath = 'api/messages?remoteAgentId=' + [uri]::EscapeDataString($RemoteAgentId) + '&limit=5'
    $history = Invoke-RouterGet -Path $historyPath
    $count = @($history.messages).Count
    Write-Check -Name 'Channel history readable' -Ok $true -Detail ("messages={0}" -f $count)
} catch {
    Write-Check -Name 'Channel history readable' -Ok $false -Detail $_.Exception.Message
    $failures++
}

Write-Host '---'
if ($failures -eq 0) {
    Write-Host 'Ready: Router, MCP registration, session, invitation, and history are available.' -ForegroundColor Green
    exit 0
}

Write-Host ("Not ready: {0} check(s) failed." -f $failures) -ForegroundColor Red
Write-Host 'Typical startup order:'
Write-Host '  Terminal 1: .\scripts\start-aspocket-router.ps1'
Write-Host '  Terminal 2: .\scripts\start-aspocket-claude-channel.ps1'
exit 1
