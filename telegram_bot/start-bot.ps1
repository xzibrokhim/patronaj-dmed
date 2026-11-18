$ErrorActionPreference = 'Stop'
$botDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $botDir

if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    Write-Host 'Creating local Python environment for telegram_bot...'
    python -m venv .venv
    & .\.venv\Scripts\python.exe -m pip install -r requirements.txt
}

if (-not (Test-Path -LiteralPath '.env')) {
    $siblingEnv = Join-Path (Split-Path -Parent $botDir) 'web\.env'
    if (Test-Path -LiteralPath $siblingEnv) {
        Copy-Item -LiteralPath $siblingEnv -Destination '.env'
        Write-Host 'Copied settings from ..\web\.env to telegram_bot\.env.'
    } else {
        Copy-Item -LiteralPath '.env.example' -Destination '.env'
        Write-Host 'Created telegram_bot\.env from .env.example.'
    }
}

$envContent = Get-Content -LiteralPath '.env' -Raw
if ($envContent -match '(?m)^INTERNAL_API_KEY=\s*$') {
    $generatedKey = (& .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_hex(32))").Trim()
    $envContent = $envContent -replace '(?m)^INTERNAL_API_KEY=\s*$', "INTERNAL_API_KEY=$generatedKey"
    Set-Content -LiteralPath '.env' -Value $envContent -Encoding utf8
}

Write-Host 'Starting district-doctor Telegram bot...'
& .\.venv\Scripts\python.exe -m app.telegram_bot
