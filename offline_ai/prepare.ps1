# Development checkout only. The offline ZIP already includes these files.
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (!(Get-Command uv -ErrorAction SilentlyContinue)) {
    throw 'Source build requires uv (https://docs.astral.sh/uv/). The portable ZIP needs no installation.'
}
if (!(Test-Path '.venv/Scripts/python.exe')) {
    uv venv --python 3.12 .venv
    if ($LASTEXITCODE) { throw 'Python 3.12 setup failed' }
}
uv pip install --python .venv/Scripts/python.exe -r requirements.txt
if ($LASTEXITCODE) { throw 'Python package installation failed' }
& .venv/Scripts/python.exe prepare_assets.py
if ($LASTEXITCODE) { throw 'Speech model download failed' }
$archivePath = Join-Path $PSScriptRoot '.runtime/ollama.zip'
if (!(Test-Path -LiteralPath $archivePath)) {
    curl.exe -fLsS --retry 3 -o "$archivePath.part" https://github.com/ollama/ollama/releases/download/v0.40.16/ollama-windows-amd64.zip
    if ($LASTEXITCODE) { throw 'Ollama download failed' }
    Move-Item -LiteralPath "$archivePath.part" -Destination $archivePath
}
if ((Get-FileHash -LiteralPath $archivePath).Hash -ne '50fcf5ad54277f2fa5d173ebfc93bcb017adbc0c9ef2314daabc96ca9e6aa64e') {
    throw 'Ollama archive checksum mismatch'
}
tar.exe -xf $archivePath -C .runtime
if ($LASTEXITCODE) { throw 'Ollama extraction failed' }
$env:OLLAMA_MODELS = Join-Path $PSScriptRoot '.runtime/models'
$env:OLLAMA_HOST = '127.0.0.1:11439'
$env:OLLAMA_NO_CLOUD = '1'
if (Get-NetTCPConnection -LocalPort 11439 -State Listen -ErrorAction SilentlyContinue) {
    throw 'Port 11439 is busy. Close the running bridge before preparation.'
}
$process = Start-Process -FilePath .runtime/ollama.exe -ArgumentList serve -WindowStyle Hidden -PassThru
try {
    $ready = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        if ($process.HasExited) { throw 'Ollama startup failed' }
        try {
            Invoke-RestMethod 'http://127.0.0.1:11439/api/version' -TimeoutSec 2 | Out-Null
            $ready = $true
            break
        } catch { Start-Sleep -Seconds 1 }
    }
    if (!$ready) { throw 'Ollama startup timed out' }
    & .runtime/ollama.exe pull qwen3:4b-instruct
    if ($LASTEXITCODE) { throw 'Ollama model download failed' }
} finally {
    if (!$process.HasExited) { Stop-Process -Id $process.Id }
}
New-Item .runtime/engine.ready -ItemType File -Force | Out-Null
Write-Output 'Ready. Open START.cmd. To build the offline ZIP, run .venv/Scripts/python.exe build_delivery.py.'
