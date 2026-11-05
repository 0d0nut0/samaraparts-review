# Parts.Site: сайт и Telegram-бот.
#   docker-compose (--profile prod): база и Redis — отдельные сервисы, бот — отдельный сервис (RUN_BOT=0 у web).
#   Хостинг ботов (bothost.ru): DATABASE_URL не задан → PostgreSQL, Redis, сайт и бот в одном контейнере,
#   данные в $DATA_DIR (/app/data сохраняется между пересборками). См. deploy/entrypoint.sh.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql redis-server procps \
    && rm -rf /var/lib/apt/lists/*

# playwright нужен только парсеру Мобы (run_parser.py) — в образ сайта браузеры не ставим
COPY requirements.txt .
RUN grep -v '^playwright\|^pyee' requirements.txt > /tmp/req.txt && pip install -r /tmp/req.txt

COPY . .
RUN useradd --create-home --uid 1000 app \
    && mkdir -p media var data && chown -R app:app media var data \
    && sed -i 's/\r$//' deploy/entrypoint.sh && chmod +x deploy/entrypoint.sh

EXPOSE 8000 8888
ENTRYPOINT ["/app/deploy/entrypoint.sh"]
CMD ["web"]
