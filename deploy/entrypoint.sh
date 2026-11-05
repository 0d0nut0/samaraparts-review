#!/bin/sh
# Запуск Parts.Site в контейнере.
#
#   web (по умолчанию) — сайт; если DATABASE_URL не задан, в этом же контейнере поднимаются PostgreSQL и Redis
#                        с данными в $DATA_DIR (режим «всё в одном» для хостинга ботов вроде bothost.ru),
#                        а при заданном токене бота — и Telegram-бот (RUN_BOT=0 — не запускать).
#   любая другая команда — выполняется как есть (docker-compose: сервис bot → python -m app.bot).
set -e

DATA_DIR="${DATA_DIR:-/app/data}"
export DATA_DIR

# bothost хранит токен бота в BOT_TOKEN, порт веб-приложения — в PORT
export TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-$BOT_TOKEN}"
PORT="${PORT:-8000}"

if [ "$1" != "web" ]; then
    exec "$@"
fi

as_app() { setpriv --reuid=app --regid=app --init-groups -- "$@"; }

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA_DIR" /app/var
    chown app:app "$DATA_DIR" /app/var /app/media 2>/dev/null || true
fi

# SECRET_KEY: если не задан — создаём один раз и храним в $DATA_DIR (сессии переживают перезапуск)
if [ -z "$SECRET_KEY" ]; then
    if [ ! -s "$DATA_DIR/secret_key" ]; then
        python -c "import secrets; print(secrets.token_urlsafe(48))" > "$DATA_DIR/secret_key"
        chmod 600 "$DATA_DIR/secret_key"; chown app:app "$DATA_DIR/secret_key" 2>/dev/null || true
    fi
    SECRET_KEY="$(cat "$DATA_DIR/secret_key")"
    export SECRET_KEY
fi

case "$SITE_URL" in https://*) export HTTPS_ONLY="${HTTPS_ONLY:-true}" ;; esac

PGBIN=""
if [ -z "$DATABASE_URL" ]; then
    # ── Встроенные PostgreSQL и Redis (данные в $DATA_DIR, наружу не слушают) ──
    PGBIN="$(ls -d /usr/lib/postgresql/*/bin | sort -V | tail -1)"
    PGDATA="$DATA_DIR/pg"
    if [ ! -s "$PGDATA/PG_VERSION" ]; then
        echo "Создаём базу PostgreSQL в $PGDATA"
        mkdir -p "$PGDATA" && chown app:app "$PGDATA" && chmod 700 "$PGDATA"
        as_app "$PGBIN/initdb" -D "$PGDATA" -U partsbot --auth=trust --encoding=UTF8 --locale=C.UTF-8 >/dev/null
        cat >> "$PGDATA/postgresql.conf" <<'EOF'
listen_addresses = '127.0.0.1'
unix_socket_directories = '/tmp'
shared_buffers = 32MB
work_mem = 4MB
max_connections = 30
EOF
    fi
    rm -f "$PGDATA/postmaster.pid"  # контейнер мог быть остановлен без корректного выключения базы
    as_app "$PGBIN/pg_ctl" -D "$PGDATA" -l "$DATA_DIR/postgres.log" -w start >/dev/null
    as_app "$PGBIN/psql" -h 127.0.0.1 -U partsbot -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='partsbot'" | grep -q 1 \
        || as_app "$PGBIN/createdb" -h 127.0.0.1 -U partsbot partsbot

    mkdir -p "$DATA_DIR/redis" && chown app:app "$DATA_DIR/redis"
    as_app redis-server --bind 127.0.0.1 --port 6379 --dir "$DATA_DIR/redis" --appendonly yes \
        --maxmemory 48mb --maxmemory-policy allkeys-lru --daemonize yes --logfile "$DATA_DIR/redis.log"

    export DATABASE_URL="postgresql://partsbot@127.0.0.1:5432/partsbot"
    export REDIS_URL="redis://127.0.0.1:6379/0"

    # Пустая база — загружаем каталог (товары, цены, разделы, города; без покупателей и заказов)
    if ! as_app "$PGBIN/psql" -h 127.0.0.1 -U partsbot -d partsbot -tAc "SELECT to_regclass('public.alembic_version')" | grep -q alembic; then
        echo "Загружаем каталог из deploy/seed/catalog.sql.gz"
        gunzip -c /app/deploy/seed/catalog.sql.gz | as_app "$PGBIN/psql" -q -h 127.0.0.1 -U partsbot -d partsbot -v ON_ERROR_STOP=1 >/dev/null
    fi
fi

as_app alembic upgrade head
as_app python -m scripts.first_run  # администратор из ADMIN_ID, покупатели старого бота из $DATA_DIR/spare_parts.db

BG_PIDS=""
if [ -n "$TELEGRAM_BOT_TOKEN" ] && [ "${RUN_BOT:-1}" = "1" ]; then
    # Бот в фоне; упал — перезапускаем через 10 с (сеть, Telegram недоступен)
    ( while true; do as_app python -m app.bot || true; echo "Бот остановился, перезапуск через 10 с"; sleep 10; done ) &
    BG_PIDS="$BG_PIDS $!"
fi

if [ -n "$PGBIN" ]; then
    # Встроенная база: копия раз в сутки в $DATA_DIR/backups, хранятся 7 последних
    mkdir -p "$DATA_DIR/backups" && chown app:app "$DATA_DIR/backups"
    ( while true; do
        sleep 3600
        f="$DATA_DIR/backups/partsbot_$(date +%F).sql.gz"
        [ -s "$f" ] || as_app sh -c "'$PGBIN/pg_dump' -h 127.0.0.1 -U partsbot partsbot | gzip > '$f'"
        ls -1t "$DATA_DIR"/backups/partsbot_*.sql.gz 2>/dev/null | tail -n +8 | xargs -r rm -f
      done ) &
    BG_PIDS="$BG_PIDS $!"
fi

# Сайт — не через exec: при остановке контейнера корректно выключаем базу и Redis (иначе — восстановление после сбоя)
setpriv --reuid=app --regid=app --init-groups --     uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --no-server-header --proxy-headers \
    --forwarded-allow-ips="${FORWARDED_ALLOW_IPS:-127.0.0.1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16}" &
WEB_PID=$!

shutdown() {
    echo "Остановка…"
    kill -TERM "$WEB_PID" $BG_PIDS 2>/dev/null || true
    pkill -TERM -f "python -m app.bot" 2>/dev/null || true
    wait "$WEB_PID" 2>/dev/null || true
    if [ -n "$PGBIN" ]; then
        as_app "$PGBIN/pg_ctl" -D "$PGDATA" -m fast -w stop >/dev/null 2>&1 || true
        as_app redis-cli -h 127.0.0.1 shutdown >/dev/null 2>&1 || true
    fi
    exit 0
}
trap shutdown TERM INT

wait "$WEB_PID"
shutdown
