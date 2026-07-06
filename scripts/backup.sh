#!/usr/bin/env sh
set -eu

root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
output=${1:-"$root/backups"}
timestamp=$(date +%Y%m%d-%H%M%S)
target="$output/$timestamp"
mkdir -p "$target"

docker exec deepresearch-postgres pg_dump -U postgres -Fc -f /tmp/deepresearch-backup.dump deepresearch
docker cp deepresearch-postgres:/tmp/deepresearch-backup.dump "$target/postgres.dump"
docker exec deepresearch-redis sh -c 'rm -f /tmp/deepresearch-backup.rdb; auth_ping=""; if [ -n "${REDIS_PASSWORD:-}" ]; then auth_ping=$(REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli ping 2>&1 || true); fi; if [ "$auth_ping" = "PONG" ]; then REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli --rdb /tmp/deepresearch-backup.rdb; else redis-cli --rdb /tmp/deepresearch-backup.rdb; fi; test -s /tmp/deepresearch-backup.rdb'
docker cp deepresearch-redis:/tmp/deepresearch-backup.rdb "$target/redis.rdb"

(
  cd "$target"
  sha256sum postgres.dump redis.rdb > manifest.sha256
)
printf 'Backup completed: %s\n' "$target"
