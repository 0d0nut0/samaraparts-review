import os
from dotenv import load_dotenv
from pydantic_settings import BaseSettings

load_dotenv()

class Settings(BaseSettings):
    DATABASE_URL: str
    REDIS_URL: str
    SECRET_KEY: str
    ENV: str = "dev"
    HTTPS_ONLY: bool = False
    MAINTENANCE_FILE: str = 'var/catalog-rebuild.flag'

    # Обмен с 1С по протоколу CommerceML («Обмен с сайтами» в УТ 11). Выключен, пока не настроен узел в 1С.
    ONEC_EXCHANGE_ENABLED: bool = False
    ONEC_EXCHANGE_LOGIN: str = ""         # логин/пароль, которые указываются в узле обмена в 1С
    ONEC_EXCHANGE_PASSWORD: str = ""
    ONEC_PRICE_TYPE_BASE: str = "Розничная"  # наименование вида цены 1С → our_base
    ONEC_PRICE_TYPE_OPT: str = "Оптовая"     # → our_opt
    ONEC_PRICE_TYPE_PURCHASE: str = "Закупочная"
    ONEC_DEFAULT_CITY_ID: int | None = None  # склад сайта для остатков без разбивки по складам 1С
    ONEC_UPLOAD_DIR: str = "var/1c_exchange"
    ONEC_FILE_LIMIT: int = 50 * 1024 * 1024  # максимальный размер части файла от 1С
    MOBA_SNAPSHOT_PATH: str = "var/prices/latest.json"
    MOBA_HTTP_PROXY: str = ""
    ONEC_CATALOG_MAP: str = "var/onec_catalog_map.json"
    ONEC_IMPORT_STOCKS: bool = True
    ONEC_SOURCE_ONLY: bool = False
    ONEC_ODATA_URL: str = ""
    ONEC_ODATA_LOGIN: str = ""
    ONEC_ODATA_PASSWORD: str = ""

    # Публичный адрес сайта (ссылки в Telegram, sitemap, canonical): https://parts.example.ru
    SITE_URL: str = "http://localhost:8000"

    # Письма подтверждения регистрации. 587 — STARTTLS, 465 — SMTP_SSL.
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = ""
    SMTP_SSL: bool = False
    SMTP_STARTTLS: bool = True
    SMTP_TIMEOUT: int = 15
    REGISTRATION_EMAIL_ENABLED: bool = True

    SAMARA_PHONE: str = ""
    KAZAN_PHONE: str = ""

    # Telegram-бот работает на той же базе, что и сайт (python -m app.bot)
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_BOT_USERNAME: str = ""      # без @ — для ссылок t.me/<бот>?start=…
    TELEGRAM_API_URL: str = ""           # только для тестов: свой сервер вместо api.telegram.org

    class Config:
        env_file = ".env"

settings = Settings()

# Backward compatible constants
DATABASE_URL = settings.DATABASE_URL
REDIS_URL = settings.REDIS_URL
SECRET_KEY = settings.SECRET_KEY
ENV = settings.ENV
HTTPS_ONLY = settings.HTTPS_ONLY


