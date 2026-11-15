#!/bin/sh
# Nightly database backup. Runs in the `backup` container (see docker-compose.yml).
#
# Writes /backups/dashboard_YYYY-MM-DD.dump (pg_dump custom format), checks
# that the file is readable, and deletes dumps older than BACKUP_KEEP_DAYS.
# A dump is taken at start-up if none exists for today, then once a day
# after BACKUP_HOUR_UTC.
#
# Restore:  docker compose exec -T db pg_restore -U dashboard -d dashboard \
#               --clean --if-exists < backups/dashboard_YYYY-MM-DD.dump
set -u

KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
HOUR="${BACKUP_HOUR_UTC:-8}"
DIR=/backups

backup() {
    file="$DIR/dashboard_$(date -u +%Y-%m-%d).dump"
    if pg_dump -h db -U dashboard -d dashboard -Fc -f "$file.tmp" && pg_restore --list "$file.tmp" > /dev/null; then
        mv "$file.tmp" "$file"
        find "$DIR" -name 'dashboard_*.dump' -mtime "+$KEEP_DAYS" -delete
        echo "$(date -u +%FT%TZ) backup ok: $file ($(du -h "$file" | cut -f1))"
    else
        rm -f "$file.tmp"
        echo "$(date -u +%FT%TZ) BACKUP FAILED" >&2
        return 1
    fi
}

if [ "${1:-}" = "once" ]; then
    backup
    exit $?
fi

[ -e "$DIR/dashboard_$(date -u +%Y-%m-%d).dump" ] || backup
while true; do
    sleep 600
    today="$DIR/dashboard_$(date -u +%Y-%m-%d).dump"
    if [ ! -e "$today" ] && [ "$(date -u +%H)" -ge "$HOUR" ]; then
        backup
    fi
done
