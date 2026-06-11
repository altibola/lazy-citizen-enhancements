# install_translation.ps1 — Instalador da tradução para jogadores.
#
# Baixa o global.ini melhorado direto do GitHub (branch main) e instala na
# pasta do Star Citizen, ajustando o user.cfg (g_language).
#
# Detecção da pasta do jogo, nesta ordem:
#   1. Pasta atual: se você rodar de dentro de StarCitizen\LIVE (ou PTU etc.),
#      o canal correspondente é o alvo.
#   2. Logs/Settings do RSI Launcher → alvo é o LIVE.
#   3. Caminho padrão (C:\Program Files\Roberts Space Industries\StarCitizen)
#      → alvo é o LIVE.
#   4. Pergunta o caminho.
#
# Uso (PowerShell):
#   irm https://raw.githubusercontent.com/altibola/lazy-citizen-enhancements/main/install_translation.ps1 | iex
#
# Compatível com Windows PowerShell 5.1 e PowerShell 7+.

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$RepoRaw = 'https://raw.githubusercontent.com/altibola/lazy-citizen-enhancements/main'

# Opções de idioma: pasta no repositório -> id de idioma que o jogo aceita.
# 'variant' aponta a pasta com os melhoramentos TRADUZIDOS (quando existir).
$Languages = @(
    @{ Label = 'English (enhancements in English)';            Repo = 'english';                          Variant = $null;                                 GameId = 'english' }
    @{ Label = 'Francais (France) - fonte Dymerz';             Repo = 'french_(france)';                  Variant = 'french_(france)_all';                 GameId = 'french_(france)' }
    @{ Label = 'Espanol (Espana) - fonte Dymerz';              Repo = 'spanish_(spain)';                  Variant = 'spanish_(spain)_all';                 GameId = 'spanish_(spain)' }
    @{ Label = 'Portugues (Brasil) - fonte Dymerz';            Repo = 'portuguese_(brazil)_dymerz';       Variant = 'portuguese_(brazil)_dymerz_all';      GameId = 'portuguese_(brazil)' }
    @{ Label = 'Portugues (Brasil) - fonte danielgmota';       Repo = 'portuguese_(brazil)_danielgmota';  Variant = 'portuguese_(brazil)_danielgmota_all'; GameId = 'portuguese_(brazil)' }
)

function Find-ChannelDirFromCwd {
    # Sobe a partir da pasta atual procurando um diretório com Data.p4k
    # (a pasta de canal: LIVE, PTU, EPTU, HOTFIX, TECHPREVIEW).
    $dir = Get-Location | Select-Object -ExpandProperty Path
    while ($dir) {
        if (Test-Path (Join-Path $dir 'Data.p4k')) { return $dir }
        $parent = Split-Path $dir -Parent
        if ($parent -eq $dir) { break }
        $dir = $parent
    }
    return $null
}

function Find-SCRootFromLauncher {
    # Procura o caminho da biblioteca nos logs/settings do RSI Launcher.
    $candidates = @(
        (Join-Path $env:APPDATA 'rsilauncher\log.log'),
        (Join-Path $env:APPDATA 'rsilauncher\logs\log.log'),
        (Join-Path $env:APPDATA 'rsilauncher\settings.json')
    )
    foreach ($file in $candidates) {
        if (-not (Test-Path $file)) { continue }
        try {
            $text = Get-Content $file -Raw -ErrorAction Stop
            # Caminhos aparecem com \\ (JSON) ou \ — captura ...\StarCitizen
            $m = [regex]::Matches($text, '([A-Za-z]:[\\/](?:[^"''\r\n]*?[\\/])?StarCitizen)(?=[\\/"''])')
            if ($m.Count -gt 0) {
                $path = $m[$m.Count - 1].Groups[1].Value -replace '\\\\', '\'
                if (Test-Path $path) { return $path }
            }
        } catch { }
    }
    return $null
}

function Update-UserCfg([string]$channelDir, [string]$gameId) {
    $cfg = Join-Path $channelDir 'user.cfg'
    $lines = @()
    if (Test-Path $cfg) {
        $lines = @(Get-Content $cfg | Where-Object { $_ -notmatch '^\s*g_language\s*=' })
    }
    $lines += "g_language = $gameId"
    # UTF-8 sem BOM (Out-File no PS 5.1 escreveria BOM)
    [IO.File]::WriteAllLines($cfg, $lines, (New-Object Text.UTF8Encoding($false)))
    Write-Host "user.cfg atualizado: g_language = $gameId" -ForegroundColor Green
}

Write-Host ''
Write-Host '=== Star Citizen - instalador de traducao (lazy-citizen-enhancements) ===' -ForegroundColor Cyan
Write-Host ''

# ── 1. Localizar a pasta do canal ────────────────────────────────────────────
$channelDir = Find-ChannelDirFromCwd
if ($channelDir) {
    Write-Host "Pasta do jogo detectada (pasta atual): $channelDir"
} else {
    $scRoot = Find-SCRootFromLauncher
    if (-not $scRoot) {
        $default = 'C:\Program Files\Roberts Space Industries\StarCitizen'
        if (Test-Path $default) { $scRoot = $default }
    }
    if ($scRoot) {
        # Fora da pasta do jogo: o alvo e o LIVE.
        $live = Join-Path $scRoot 'LIVE'
        if (Test-Path (Join-Path $live 'Data.p4k')) {
            $channelDir = $live
            Write-Host "Instalacao detectada via launcher: $channelDir"
        }
    }
}
while (-not $channelDir) {
    $answer = Read-Host 'Informe a pasta do canal do jogo (ex: C:\...\StarCitizen\LIVE)'
    if ($answer -and (Test-Path (Join-Path $answer 'Data.p4k'))) {
        $channelDir = $answer
    } else {
        Write-Host 'Pasta invalida (Data.p4k nao encontrado nela). Tente novamente.' -ForegroundColor Yellow
    }
}

# ── 2. Escolher o idioma ─────────────────────────────────────────────────────
Write-Host ''
Write-Host 'Idiomas disponiveis:'
for ($i = 0; $i -lt $Languages.Count; $i++) {
    Write-Host ("  [{0}] {1}" -f ($i + 1), $Languages[$i].Label)
}
$lang = $null
while (-not $lang) {
    $choice = Read-Host ("Escolha o idioma [1-{0}]" -f $Languages.Count)
    $n = 0
    if ([int]::TryParse($choice, [ref]$n) -and $n -ge 1 -and $n -le $Languages.Count) {
        $lang = $Languages[$n - 1]
    }
}

# ── 3. Melhoramentos traduzidos? ─────────────────────────────────────────────
$repoFolder = $lang.Repo
if ($lang.Variant) {
    $resp = Read-Host 'Deseja a versao com os melhoramentos (stats) tambem traduzidos? [S/n]'
    if ($resp -eq '' -or $resp -match '^[SsYy]') { $repoFolder = $lang.Variant }
}

# ── 4. Baixar e instalar ─────────────────────────────────────────────────────
$encoded = [uri]::EscapeDataString($repoFolder)
$url = "$RepoRaw/data/Localization/$encoded/global.ini"
$destDir = Join-Path $channelDir ("data\Localization\" + $lang.GameId)
$dest = Join-Path $destDir 'global.ini'

Write-Host ''
Write-Host "Baixando: $url"
New-Item -ItemType Directory -Force -Path $destDir | Out-Null
try {
    Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing
} catch {
    Write-Host "Falha no download: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host 'Verifique se essa variante ja foi publicada no branch main (tabela Downloads do README).' -ForegroundColor Yellow
    exit 1
}
$size = (Get-Item $dest).Length
Write-Host ("Instalado: {0} ({1:N1} MB)" -f $dest, ($size / 1MB)) -ForegroundColor Green

# ── 5. user.cfg ──────────────────────────────────────────────────────────────
Update-UserCfg -channelDir $channelDir -gameId $lang.GameId

Write-Host ''
Write-Host 'Pronto! Abra o jogo e o idioma ja estara ativo.' -ForegroundColor Cyan
