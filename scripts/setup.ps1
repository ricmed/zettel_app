# Prepara o Zettel num clone novo: uv, dependencias, .env, vault e diagnostico.
#
#   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1          # CUDA se houver nvidia-smi, senao CPU
#   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 -Cpu     # forca torch CPU
#   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 -Cuda    # forca torch CUDA 12.6
#
# Idempotente: rodar de novo nao sobrescreve o .env nem o SESSION_SECRET.
# Se o uv nao estiver no PATH, o script instala o binario oficial em ~\.local\bin.
# Um hash mismatch de torch/torchvision atualiza o lock desses pacotes, grava o
# hash da roda baixada quando o indice nao publica um, e tenta o sync outra vez.
param(
    [switch]$Cpu,
    [switch]$Cuda
)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)

if ($Cpu -and $Cuda) { throw 'Use -Cpu ou -Cuda, nao os dois.' }

function Add-UvToPath {
    $bin = Join-Path $env:USERPROFILE '.local\bin'
    $uv = Join-Path $bin 'uv.exe'
    if ((Test-Path $uv) -and -not (Get-Command uv -ErrorAction SilentlyContinue)) {
        $env:PATH = "$bin;$env:PATH"
    }
}

Add-UvToPath
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host 'uv nao encontrado. Instalando...'
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    Add-UvToPath
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw 'A instalacao do uv nao o deixou disponivel nesta sessao.'
}

function Invoke-Checked {
    param([string]$Exe, [string[]]$Arguments)
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Exe $($Arguments -join ' ') saiu com $LASTEXITCODE" }
}

# O indice CUDA publica a roda Windows sem sha256 do arquivo. O uv entao recusa o
# download. Gravamos o hash que ele acabou de calcular, na roda deste Python.
function Repair-TorchWheelHash {
    param([string]$SyncLog)
    $failed = [regex]::Match($SyncLog, 'Failed to download `([^`]+)==([^`]+)`')
    $computed = [regex]::Match($SyncLog, 'Computed:\s+sha256:([0-9a-fA-F]{64})')
    if (-not $failed.Success -or -not $computed.Success) { return }
    $name = $failed.Groups[1].Value
    if ($name -ne 'torch' -and $name -ne 'torchvision') { return }
    $py = (Get-Content -Raw .python-version).Trim()
    if ($py -notmatch '^(\d+)\.(\d+)') { return }
    $tag = "cp$($Matches[1])$($Matches[2])"
    $version = $failed.Groups[2].Value.Replace('+', '%2B')
    $wheel = "$name-$version-$tag-$tag-win_amd64.whl"
    $hash = $computed.Groups[1].Value.ToLower()
    $lockPath = Join-Path (Get-Location) 'uv.lock'
    $raw = [System.IO.File]::ReadAllText($lockPath)
    $newline = if ($raw.Contains("`r`n")) { "`r`n" } else { "`n" }
    $lines = $raw -split "`r`n|`n", -1
    $found = $false
    for ($i = 0; $i -lt $lines.Length; $i++) {
        if ($lines[$i] -notlike "*$wheel*") { continue }
        if ($lines[$i] -match 'hash = "sha256:[0-9a-fA-F]{64}"') {
            $lines[$i] = [regex]::Replace($lines[$i], 'hash = "sha256:[0-9a-fA-F]{64}"', "hash = `"sha256:$hash`"")
        } else {
            $lines[$i] = [regex]::Replace($lines[$i], 'url = "([^"]+)"', 'url = "$1", hash = "sha256:' + $hash + '"')
        }
        $found = $true
        break
    }
    if (-not $found) {
        Write-Host "Nao achei $wheel no uv.lock; o novo sync segue com o lock atual."
        return
    }
    $utf8 = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($lockPath, ($lines -join $newline), $utf8)
    Write-Host "Hash gravado para $wheel."
}

function Invoke-UvSync {
    param([string]$Variant)
    $syncArgs = @('sync', '--extra', $Variant)
    $log = Join-Path ([System.IO.Path]::GetTempPath()) ("zettel-uv-sync-{0}.log" -f [guid]::NewGuid().ToString('n'))
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & uv @syncArgs 2>&1 | ForEach-Object {
        $line = $_.ToString()
        [System.IO.File]::AppendAllText($log, $line + "`n")
        Write-Host $line
    }
    $code = $LASTEXITCODE
    $ErrorActionPreference = $prev
    if ($code -eq 0) {
        Remove-Item $log -Force -ErrorAction SilentlyContinue
        return
    }
    $text = ''
    if (Test-Path $log) { $text = [System.IO.File]::ReadAllText($log) }
    $torchMismatch = $text -match 'Hash mismatch' -and $text -match 'torch'
    if (-not $torchMismatch) {
        Remove-Item $log -Force -ErrorAction SilentlyContinue
        throw "uv $($syncArgs -join ' ') saiu com $code"
    }
    Write-Host 'Hash do torch/torchvision desatualizado. Atualizando o lock e tentando de novo.'
    & uv lock --refresh-package torch --refresh-package torchvision
    if ($LASTEXITCODE -ne 0) {
        Remove-Item $log -Force -ErrorAction SilentlyContinue
        throw "uv lock --refresh-package torch --refresh-package torchvision saiu com $LASTEXITCODE"
    }
    Repair-TorchWheelHash $text
    Remove-Item $log -Force -ErrorAction SilentlyContinue
    Invoke-Checked uv $syncArgs
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
Invoke-UvSync $variant

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
