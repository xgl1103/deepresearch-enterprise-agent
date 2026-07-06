param(
    [string]$OutputDirectory = (Join-Path $PSScriptRoot "..\backups")
)

$ErrorActionPreference = "Stop"
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$target = Join-Path $OutputDirectory $timestamp
New-Item -ItemType Directory -Path $target -Force | Out-Null

$pgContainer = "deepresearch-postgres"
$redisContainer = "deepresearch-redis"
$pgTemp = "/tmp/deepresearch-backup.dump"
$redisTemp = "/tmp/deepresearch-backup.rdb"

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)]
        [scriptblock]$Command,
        [Parameter(Mandatory = $true)]
        [string]$Description
    )
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE"
    }
}

Invoke-Checked { docker inspect $pgContainer | Out-Null } "Inspect PostgreSQL container"
Invoke-Checked { docker inspect $redisContainer | Out-Null } "Inspect Redis container"
Invoke-Checked { docker exec $pgContainer pg_dump -U postgres -Fc -f $pgTemp deepresearch } "PostgreSQL dump"
Invoke-Checked { docker cp "${pgContainer}:${pgTemp}" (Join-Path $target "postgres.dump") } "Copy PostgreSQL dump"
Invoke-Checked { docker exec $redisContainer rm -f $redisTemp } "Remove stale Redis dump"
$redisPasswordOutput = & docker exec $redisContainer printenv REDIS_PASSWORD 2>$null
$redisPassword = if ($LASTEXITCODE -eq 0) { ($redisPasswordOutput -join "`n").Trim() } else { "" }
$useRedisAuth = $false
if (-not [string]::IsNullOrWhiteSpace($redisPassword)) {
    $authPing = (& docker exec -e REDISCLI_AUTH=$redisPassword $redisContainer redis-cli ping 2>&1) -join "`n"
    $useRedisAuth = $authPing.Trim() -eq "PONG"
}
if ($useRedisAuth) {
    Invoke-Checked { docker exec -e REDISCLI_AUTH=$redisPassword $redisContainer redis-cli --rdb $redisTemp } "Redis RDB dump"
} else {
    Invoke-Checked { docker exec $redisContainer redis-cli --rdb $redisTemp } "Redis RDB dump"
}
Invoke-Checked { docker exec $redisContainer test -s $redisTemp } "Verify Redis dump"
Invoke-Checked { docker cp "${redisContainer}:${redisTemp}" (Join-Path $target "redis.rdb") } "Copy Redis RDB dump"

$manifest = Get-ChildItem -LiteralPath $target -File | ForEach-Object {
    $hash = Get-FileHash -Algorithm SHA256 -LiteralPath $_.FullName
    [ordered]@{ file = $_.Name; bytes = $_.Length; sha256 = $hash.Hash.ToLower() }
}
$manifest | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $target "manifest.json") -Encoding utf8
Write-Output "Backup completed: $target"
