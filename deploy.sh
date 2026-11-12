#!/usr/bin/env bash
# Run on Proxmox VE 6.3 as root. Never recreates containers.
# Keep maintenance active: docker start can resume processes built into the image.
set +x  # Credentials must never be traced, including when invoked with bash -x.
set -Eeuo pipefail
umask 077
archive=/root/lexradar-task-opportunity-1.0.0.tar.gz
expected=0bb4c257baf2688354a5046cefa952017736aab16eec5ee3f9b4caf9804c9a61
run=$(date -u +%Y%m%dT%H%M%SZ)-$$
safe=/root/lexradar-deploy-$run
pkg=/tmp/lexradar-package-$run/espocrm-task-opportunity-extension
crm() { pct exec 102 -- docker exec -u www-data -w /var/www/html espocrm "$@"; }
# docker cp writes a tar stream; redirection is on Proxmox, never in LXC.
copy_backup() {
    pct exec 102 -- docker cp "espocrm:$1" - > "$safe/$2.tar.partial"
    tar -tf "$safe/$2.tar.partial" > /dev/null
    mv "$safe/$2.tar.partial" "$safe/$2.tar"
    pct exec 102 -- docker exec espocrm rm -r -- "$1"
}
dump_db() {
    # Global read lock also covers non-InnoDB tables; maintenance stays active.
    pct exec 102 -- docker exec -i espocrm-db sh -eu -s > "$safe/mysql-before.sql.partial" 2> "$safe/mysql-dump.stderr.private" <<'DUMP_SCRIPT'
        set +x
        test -n "$MYSQL_ROOT_PASSWORD"
        test -n "$MYSQL_DATABASE"
        MYSQL_PWD="$MYSQL_ROOT_PASSWORD"; export MYSQL_PWD
        exec mysqldump --user=root --lock-all-tables --quick \
            --routines --events --triggers --hex-blob --databases "$MYSQL_DATABASE"
DUMP_SCRIPT
    test -s "$safe/mysql-before.sql.partial"
    grep -q '^CREATE TABLE ' "$safe/mysql-before.sql.partial"
    tail -n 5 "$safe/mysql-before.sql.partial" | grep -q '^-- Dump completed on '
    mv "$safe/mysql-before.sql.partial" "$safe/mysql-before.sql"
    (cd "$safe" && sha256sum mysql-before.sql > mysql-before.sql.sha256 && sha256sum -c mysql-before.sql.sha256)
}
fail() { echo "STOP: $*" >&2; exit 1; }
trap 'echo "Остановка на строке $LINENO. Резервные копии: $safe. Доступ/писателей не возобновлять. Отключение: bash /root/deploy.sh --rollback" >&2' ERR
[[ $EUID == 0 ]] || fail 'Нужен root Proxmox'
exec 9>/run/lock/lexradar-extension.lock
flock -n 9 || fail 'Уже выполняется установка'
pct status 102 | grep -qx 'status: running'
[[ $(pct exec 102 -- docker inspect -f '{{.State.Running}}' espocrm) == true ]] || fail 'espocrm не запущен'
[[ $(pct exec 102 -- docker inspect -f '{{.State.Running}}' espocrm-db) == true ]] || fail 'espocrm-db не запущен'
[[ $(pct exec 102 -- docker inspect -f '{{.Config.Image}}' espocrm) == espocrm/espocrm:latest ]] || fail 'Неожиданный образ espocrm; нужна сверка deployment'
[[ $(pct exec 102 -- docker inspect -f '{{len .Mounts}}' espocrm) == 0 ]] || fail 'Неожиданные mounts espocrm; нужна сверка deployment'
[[ $# == 0 || ( $# == 1 && $1 == --rollback ) ]] || fail 'Использование: deploy.sh [--rollback]'
# README requires operator-established maintenance; no guessed proxy/worker commands.
read -r -p 'Доступ закрыт, внешние писатели/cron/daemon остановлены, клон принят? Введите MAINTENANCE: ' answer
[[ $answer == MAINTENANCE ]] || fail 'Требуется окно обслуживания по README'
if [[ ${1:-} == --rollback ]]; then
    # Disable retains schema and source protection even after first use.
    pkg=$(cat /root/lexradar-extension-package)
    [[ $pkg == /tmp/lexradar-package-*/espocrm-task-opportunity-extension ]] || fail 'Некорректный путь пакета'
    mkdir -m 700 "$safe"
    dump_db
    crm php "$pkg/install.php" --root=/var/www/html --disable
    crm php "$pkg/install.php" --root=/var/www/html --disable --apply --backup="/tmp/lexradar-disable-$run"
    crm php rebuild.php
    copy_backup "/tmp/lexradar-disable-$run" disable-backup
    echo "Кнопка и маршруты отключены; схема и данные сохранены. Backup Proxmox: $safe"
    exit
fi
[[ $# == 0 ]] || fail 'Использование: deploy.sh [--rollback]'
echo "$expected  $archive" | sha256sum -c -
mkdir -m 700 "$safe"
pct config 102 > "$safe/lxc-config.private"
# Refuse mount configurations outside the previously agreed backup scope.
if grep -Eq '^(mp[0-9]+|lxc.mount.entry):' "$safe/lxc-config.private"; then
    fail 'Дополнительные mounts: сначала отдельно подтвердить согласованную резервную копию по README'
fi
pct exec 102 -- docker inspect espocrm-db > "$safe/db-inspect.private.json"
# Full vzdump of LXC 102 has already completed; do not repeat it here.
pct exec 102 -- docker inspect espocrm > "$safe/espocrm-inspect.private.json"
dump_db
pct exec 102 -- docker stop espocrm
pct exec 102 -- docker cp espocrm:/var/www/html - > "$safe/html-before.tar.partial"
tar -tf "$safe/html-before.tar.partial" > /dev/null
mv "$safe/html-before.tar.partial" "$safe/html-before.tar"
# Export the writable layer directly, without creating a large image in LXC.
pct exec 102 -- docker export espocrm > "$safe/rootfs-before.tar.partial"
tar -tf "$safe/rootfs-before.tar.partial" > /dev/null
mv "$safe/rootfs-before.tar.partial" "$safe/rootfs-before.tar"
pct exec 102 -- docker start espocrm
lxc_archive=/tmp/lexradar-package-$run.tar.gz
pct push 102 "$archive" "$lxc_archive"
pct exec 102 -- sh -c 'echo "$1  $2" | sha256sum -c -' sh "$expected" "$lxc_archive"
pct exec 102 -- mkdir -m 755 "${pkg%/*}"
pct exec 102 -- tar -xzf "$lxc_archive" -C "${pkg%/*}"
pct exec 102 -- sh -c 'cd "$1" && sha256sum -c FILES.sha256' sh "$pkg"
pct exec 102 -- docker exec espocrm mkdir -p "${pkg%/*}"
pct exec 102 -- docker cp "$pkg" "espocrm:$pkg"
pct exec 102 -- docker exec -w "$pkg" espocrm sha256sum -c FILES.sha256
crm php "$pkg/install.php" --root=/var/www/html
printf '%s\n' "$pkg" > /root/lexradar-extension-package
crm php "$pkg/install.php" --root=/var/www/html --apply --backup="/tmp/lexradar-installer-$run"
copy_backup "/tmp/lexradar-installer-$run" installer-backup
crm php rebuild.php
crm php "$pkg/verify-schema.php" /var/www/html
# A second plan checks installed files without creating any records.
crm php "$pkg/install.php" --root=/var/www/html
pct exec 102 -- docker stop espocrm
pct exec 102 -- docker cp espocrm:/var/www/html - > "$safe/html-after.tar.partial"
tar -tf "$safe/html-after.tar.partial" > /dev/null
mv "$safe/html-after.tar.partial" "$safe/html-after.tar"
# Export the writable layer directly, without creating a large image in LXC.
pct exec 102 -- docker export espocrm > "$safe/rootfs-after.tar.partial"
tar -tf "$safe/rootfs-after.tar.partial" > /dev/null
mv "$safe/rootfs-after.tar.partial" "$safe/rootfs-after.tar"
pct exec 102 -- docker start espocrm
printf 'Установка проверена. Backup Proxmox: %s\nНе пересоздавать контейнер. Доступ/писателей возобновлять после ручной приёмки.\n' "$safe"
