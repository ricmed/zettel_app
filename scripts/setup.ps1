# Prepara o Zettel num clone novo: dependencias, .env, vault e diagnostico.
#
#   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1          # CUDA se houver nvidia-smi, senao CPU
#   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 -Cpu     # forca torch CPU
#   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 -Cuda    # forca torch CUDA 12.6
#
# Idempotente: rodar de novo nao sobrescreve o .env nem o SESSION_SECRET.
param(
    [switch]$Cpu,
    [switch]$Cuda
)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)

if ($Cpu -and $Cuda) { throw 'Use -Cpu ou -Cuda, nao os dois.' }

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host 'uv nao encontrado. Instale com:' -ForegroundColor Yellow
    Write-Host '  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"'
    Write-Host 'e rode este script de novo (o uv tambem baixa o Python 3.12).'
    exit 1
}

function Invoke-Checked {
    param([string]$Exe, [string[]]$Arguments)
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Exe $($Arguments -join ' ') saiu com $LASTEXITCODE" }
}

# 1. Dependencias. O torch vem pelo Docling; o extra so escolhe o build.
if ($Cuda) { $variant = 'cu126' }
elseif ($Cpu) { $variant = 'cpu' }
else {
    $variant = 'cpu'
    if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
        # PowerShell 5.1 turns a redirected native stderr into an error under Stop.
        try {
            & nvidia-smi *> $null
            if ($LASTEXITCODE -eq 0) { $variant = 'cu126' }
        } catch { }
    }
}
Write-Host "torch: $variant"
Invoke-Checked uv @('sync', '--extra', $variant)

# 2. Segredos. As chaves de API ficam para voce; o segredo da web e gerado.
if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
    Write-Host '.env criado a partir de .env.example'
}
$envLines = @(Get-Content .env -Encoding UTF8)
if (-not ($envLines -match '^SESSION_SECRET=.+')) {
    $secret = (& uv run --no-sync python -c 'import secrets; print(secrets.token_urlsafe(32))').Trim()
    if ($envLines -match '^SESSION_SECRET=') {
        $envLines = $envLines -replace '^SESSION_SECRET=.*', "SESSION_SECRET=$secret"
    } else {
        $envLines += "SESSION_SECRET=$secret"
    }
    # UTF-8 sem BOM: o python-dotenv leria o BOM como parte do primeiro nome.
    [System.IO.File]::WriteAllLines((Join-Path (Get-Location) '.env'), $envLines)
    Write-Host 'SESSION_SECRET gerado no .env'
}

# 3. Vault, bases e diagnostico. --no-sync: um `uv run` simples sincronizaria sem o
# extra e trocaria o torch pelo do PyPI.
Invoke-Checked uv @('run', '--no-sync', 'python', '-m', 'zettel', 'init')
& uv run --no-sync python -m zettel doctor

Write-Host ''
Write-Host 'Pronto. Proximos passos:'
Write-Host '  1. Preencha no .env as chaves que o doctor marcou como FALHA.'
Write-Host '  2. Rode os comandos com:  uv run --no-sync python -m zettel <comando>'
Write-Host '     (ou ative o ambiente: .venv\Scripts\Activate.ps1)'
Write-Host '  3. Coloque um PDF ou Markdown em data\inbox\ e rode: run-all'
