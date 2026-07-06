param(
    [Parameter(Mandatory = $true)][string]$BackupFile,
    [switch]$ConfirmRestore
)

$ErrorActionPreference = "Stop"
if (-not $ConfirmRestore) {
    throw "Restore is destructive. Re-run with -ConfirmRestore after verifying the backup."
}
$resolved = Resolve-Path -LiteralPath $BackupFile
docker inspect deepresearch-postgres | Out-Null
docker cp $resolved "deepresearch-postgres:/tmp/deepresearch-restore.dump"
docker exec deepresearch-postgres pg_restore -U postgres --clean --if-exists --no-owner -d deepresearch /tmp/deepresearch-restore.dump
Write-Output "PostgreSQL restore completed from: $resolved"
