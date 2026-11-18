$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $projectDir

if (-not (Test-Path -LiteralPath '.venv')) {
    Write-Host 'Creating local Python environment for web...'
    python -m venv .venv
}

& .\.venv\Scripts\python.exe -m pip install -r requirements.txt

if (-not (Test-Path -LiteralPath '.env')) {
    Copy-Item -LiteralPath '.env.example' -Destination '.env'
    Write-Host 'Created web\.env. Telegram fields are optional for demo mode.'
}

$envContent = Get-Content -LiteralPath '.env' -Raw
if ($envContent -match '(?m)^INTERNAL_API_KEY=\s*$') {
    $generatedKey = (& .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_hex(32))").Trim()
    $envContent = $envContent -replace '(?m)^INTERNAL_API_KEY=\s*$', "INTERNAL_API_KEY=$generatedKey"
    Set-Content -LiteralPath '.env' -Value $envContent -Encoding utf8
    Write-Host 'Generated a private internal API key in web\.env.'
}

$botEnv = Join-Path (Split-Path -Parent $projectDir) 'telegram_bot\.env'
if (Test-Path -LiteralPath $botEnv) {
    $botTxt = Get-Content -LiteralPath $botEnv -Raw
    $keyMatch = [regex]::Match($envContent, '(?m)^INTERNAL_API_KEY=(.+)$')
    if ($keyMatch.Success -and ($botTxt -match '(?m)^INTERNAL_API_KEY=\s*$')) {
        $botTxt = $botTxt -replace '(?m)^INTERNAL_API_KEY=\s*$', "INTERNAL_API_KEY=$($keyMatch.Groups[1].Value.Trim())"
        Set-Content -LiteralPath $botEnv -Value $botTxt -Encoding utf8
        Write-Host 'Synced INTERNAL_API_KEY into telegram_bot\.env.'
    }
}

Write-Host ''
Write-Host 'Digital Bridge Web Dashboard is starting at http://127.0.0.1:8000'
Write-Host 'Keep this window open. Press Ctrl+C to stop.'
& .\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
