param(
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"

if (-not $env:MINIWORLD_AGENT_MODEL) {
    Write-Host "MINIWORLD_AGENT_MODEL is not set; MiniWorld will remain fully usable in safe fallback mode."
}
if (-not $env:MINIWORLD_AGENT_API_KEY) {
    Write-Host "MINIWORLD_AGENT_API_KEY is not set; no online model request can occur."
}

Write-Host "Starting MiniWorld V1.6 at http://${HostAddress}:$Port"
Write-Host "Online autonomy remains off until POST /api/runtime/start or the Dashboard start button."
python -B -m uvicorn app:app --host $HostAddress --port $Port --workers 1
