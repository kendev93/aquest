param(
    [switch]$Protected,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ComposeArguments
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

$databaseDir = $env:ETFQUEST_MARKET_DATABASE_DIR
if ([string]::IsNullOrWhiteSpace($databaseDir)) {
    $programData = [Environment]::GetFolderPath([Environment+SpecialFolder]::CommonApplicationData)
    if ([string]::IsNullOrWhiteSpace($programData)) {
        $programData = "C:\ProgramData"
    }
    $databaseDir = Join-Path $programData "ETFQuest\database"
}

$databaseFile = Join-Path $databaseDir "market_data.db"
if (-not (Test-Path -LiteralPath $databaseFile -PathType Leaf)) {
    throw "未找到 ETFQuest 行情数据库：$databaseFile。请先安装并初始化最新版 ETFQuest，或设置 ETFQUEST_MARKET_DATABASE_DIR 后重试。"
}

$appDataDir = $env:ETFQUEST_APP_DATA_DIR
if ([string]::IsNullOrWhiteSpace($appDataDir)) {
    $appDataDir = Split-Path -Parent $databaseDir
}
$secretKeyFile = Join-Path $appDataDir ".secret.key"
if (-not (Test-Path -LiteralPath $secretKeyFile -PathType Leaf)) {
    throw "未找到 ETFQuest 授权快照密钥：$secretKeyFile。请确认 ETFQUEST_APP_DATA_DIR 指向包含 .secret.key 的应用数据目录。"
}

$env:ETFQUEST_MARKET_DATABASE_DIR = $databaseDir
$env:ETFQUEST_APP_DATA_DIR = $appDataDir
$env:SELECTOR_MARKET_DATA_SOURCE = "etfquest"
$env:SELECTOR_RELEASE_MODE = "1"
$env:SELECTOR_ETFQUEST_DATABASE_PATH = "/run/etfquest/database/market_data.db"
$env:SELECTOR_ETFQUEST_SECRET_KEY_PATH = "/run/etfquest/.secret.key"
# Docker Desktop cannot map a Windows SID to a Linux UID reliably; root is
# used for the backend bind mount while the container still has no extra caps.
$env:ETFQUEST_MARKET_UID = "0"
$env:ETFQUEST_MARKET_GID = "0"

$composeFiles = @(
    "-f", (Join-Path $repoRoot "docker-compose.yml")
)
if ($Protected) {
    $composeFiles += @("-f", (Join-Path $repoRoot "docker-compose.protected.yml"))
} else {
    $composeFiles += @("-f", (Join-Path $repoRoot "docker-compose.etfquest.yml"))
}

Push-Location $repoRoot
try {
    & docker compose @composeFiles config --quiet
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
    & docker compose @composeFiles up --build -d @ComposeArguments
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
} finally {
    Pop-Location
}
