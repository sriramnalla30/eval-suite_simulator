# Starts the simulator. Uses the app's virtual environment when it exists (it already has fastapi, uvicorn and httpx).
$app = Join-Path $PSScriptRoot '..\Place_Discover_Negotiating-AGENT\.venv\Scripts\python.exe'
$python = if (Test-Path $app) { $app } else { 'python' }
# The app needs a signed-in account: use APP_EMAIL / APP_PASSWORD, or else the first account in SEED_USERS of the app's .env.
$envFile = Join-Path $PSScriptRoot '..\Place_Discover_Negotiating-AGENT\.env'
if (-not $env:APP_EMAIL -and (Test-Path $envFile)) {
    $line = Get-Content $envFile | Where-Object { $_ -match '^SEED_USERS=' } | Select-Object -First 1
    if ($line) {
        $first = ($line -replace '^SEED_USERS=', '').Split(';')[0]
        $i = $first.IndexOf(':')
        if ($i -gt 0) { $env:APP_EMAIL = $first.Substring(0, $i).Trim(); $env:APP_PASSWORD = $first.Substring($i + 1) }
    }
}
& $python (Join-Path $PSScriptRoot 'server.py')
