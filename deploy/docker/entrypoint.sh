#!/bin/sh
set -eu

thirdparty_seed=/opt/legadohub/plugins/thirdparty
thirdparty_target=/app/plugins/sources/thirdparty
official_seed=/opt/legadohub/plugins/official
official_target=/app/plugins/sources/official
runtime_directories="/app/backend/data /app/backend/config /app/backend/generated /app/backend/runtime"
# 体量小的挂载：启动时 chown -R（可接受）
small_trees="/app/backend/config /app/backend/generated /app/backend/runtime ${thirdparty_target} ${official_target}"

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
# 设为 1 时对 data 做 chown -R（迁移/修权限；日常保持 0，避免大书库启动过慢）
CHOWN_DATA_RECURSIVE="${LEGADOHUB_CHOWN_DATA:-0}"

validate_runtime_id() {
    name=$1
    value=$2
    case "$value" in
        ''|*[!0-9]*)
            echo "LegadoHub ${name} must be a positive integer." >&2
            exit 1
            ;;
    esac
    if [ "$value" -eq 0 ]; then
        echo "LegadoHub ${name}=0 is not allowed; the application must not run as root." >&2
        exit 1
    fi
}

is_root() {
    [ "$(id -u)" -eq 0 ]
}

ensure_dir() {
    directory=$1
    if ! mkdir -p "$directory"; then
        echo "LegadoHub runtime directory could not be created: $directory" >&2
        exit 1
    fi
}

chown_path() {
    # $1=path  $2=recursive(0|1)
    directory=$1
    recursive=${2:-0}
    ensure_dir "$directory"
    if [ "$recursive" = "1" ]; then
        if ! chown -R "${PUID}:${PGID}" "$directory" 2>/dev/null; then
            echo "LegadoHub could not recursively adjust ownership: $directory (want ${PUID}:${PGID})" >&2
            exit 1
        fi
        return 0
    fi
    if ! chown "${PUID}:${PGID}" "$directory" 2>/dev/null; then
        echo "LegadoHub could not adjust directory ownership: $directory (want ${PUID}:${PGID})" >&2
        exit 1
    fi
}

apply_runtime_ownership() {
    # data：默认只修挂载根；大体量书库避免每次启动全树 chown
    if [ "$CHOWN_DATA_RECURSIVE" = "1" ]; then
        chown_path /app/backend/data 1
    else
        chown_path /app/backend/data 0
        for database_file in \
            /app/backend/data/app.db \
            /app/backend/data/app.db-wal \
            /app/backend/data/app.db-shm \
            /app/backend/data/app.db-journal
        do
            if [ -e "$database_file" ] && ! chown "${PUID}:${PGID}" "$database_file" 2>/dev/null; then
                echo "LegadoHub could not adjust database ownership: $database_file (want ${PUID}:${PGID})" >&2
                exit 1
            fi
        done
    fi

    for directory in $small_trees; do
        chown_path "$directory" 1
    done

    # 浏览器 profile 落在 data 下
    chown_path /app/backend/data/browser_profiles 0

    # 家目录缓存（Playwright 等）
    chown_path /home/legadohub 1
}

seed_plugin_directory() {
    seed=$1
    target=$2
    other_target=$3
    ensure_dir "$target"
    for source in "$seed"/*; do
        [ -d "$source" ] || continue
        name=$(basename "$source")
        [ -e "$target/$name" ] && continue
        # 同名插件目录已存在于另一来源（official/thirdparty 互为备份时宿主
        # 挂载可能只放其一）：跳过，避免加载器撞重复插件 ID 崩溃。
        [ -e "$other_target/$name" ] && continue
        cp -R "$source" "$target/$name"
        if is_root; then
            chown -R "${PUID}:${PGID}" "$target/$name" 2>/dev/null || true
        fi
    done
}

seed_plugin_sources() {
    seed_plugin_directory "$thirdparty_seed" "$thirdparty_target" "$official_target"
    seed_plugin_directory "$official_seed" "$official_target" "$thirdparty_target"
}

as_app_user() {
    if is_root; then
        gosu "${PUID}:${PGID}" "$@"
        return
    fi
    "$@"
}

verify_writable() {
    for directory in $runtime_directories; do
        ensure_dir "$directory"
        if ! as_app_user test -w "$directory" || ! as_app_user test -x "$directory"; then
            echo "LegadoHub runtime directory is not writable by ${PUID}:${PGID}: $directory (set PUID/PGID to match host or recreate once with LEGADOHUB_CHOWN_DATA=1)" >&2
            exit 1
        fi
    done
    for database_file in \
        /app/backend/data/app.db \
        /app/backend/data/app.db-wal \
        /app/backend/data/app.db-shm \
        /app/backend/data/app.db-journal
    do
        if [ -e "$database_file" ] && ! as_app_user test -w "$database_file"; then
            echo "LegadoHub database is not writable by ${PUID}:${PGID}: $database_file (recreate once with LEGADOHUB_CHOWN_DATA=1)" >&2
            exit 1
        fi
    done
    for data_entry in /app/backend/data/*; do
        [ -e "$data_entry" ] || continue
        if [ -d "$data_entry" ]; then
            if ! as_app_user test -w "$data_entry" || ! as_app_user test -x "$data_entry"; then
                echo "LegadoHub data directory is not writable by ${PUID}:${PGID}: $data_entry (recreate once with LEGADOHUB_CHOWN_DATA=1)" >&2
                exit 1
            fi
        elif ! as_app_user test -w "$data_entry"; then
            echo "LegadoHub data file is not writable by ${PUID}:${PGID}: $data_entry (recreate once with LEGADOHUB_CHOWN_DATA=1)" >&2
            exit 1
        fi
    done
}

run_as_app_user() {
    export HOME=/home/legadohub
    if is_root; then
        exec gosu "${PUID}:${PGID}" "$@"
    fi
    exec "$@"
}

validate_runtime_id PUID "$PUID"
validate_runtime_id PGID "$PGID"

# 兼容：仅修权后退出
if [ "${1:-}" = "--initialize-runtime" ]; then
    if ! is_root; then
        echo "LegadoHub runtime initialization must run as root." >&2
        exit 1
    fi
    shift
    apply_runtime_ownership
    for directory in "$@"; do
        chown_path "$directory" 0
    done
    seed_plugin_sources
    exit 0
fi

if is_root; then
    apply_runtime_ownership
    seed_plugin_sources
    verify_writable
    run_as_app_user "$@"
fi

# 非 root 启动（本地调试）：不改权，仅检查可写
seed_plugin_sources
verify_writable
run_as_app_user "$@"
