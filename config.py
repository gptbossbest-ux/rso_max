"""
config.py — централизованные константы РСО Портал.
Все секреты читаются из .env через python-dotenv.
Не импортировать TOKEN/SECRET_KEY напрямую в бизнес-логику —
только через этот модуль.
"""
import os
import logging
from dotenv import load_dotenv

load_dotenv()

# ── База данных ────────────────────────────────────────────────────────────────
DB_PATH: str = os.getenv("DB_PATH", "database.sqlite")

# ── Часовой пояс (UTC+N) ──────────────────────────────────────────────────────
TIMEZONE_OFFSET: int = int(os.getenv("TIMEZONE_OFFSET", "3"))

# ── Логирование ───────────────────────────────────────────────────────────────
LOG_FILE: str = os.getenv("LOG_FILE", "logs/rso_portal.log")
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")          # DEBUG | INFO | WARNING | ERROR
LOG_MAX_BYTES: int = 10 * 1024 * 1024                     # 10 МБ на один файл
LOG_BACKUP_COUNT: int = 5                                  # хранить 5 ротированных файлов

# ── MAX Bot ────────────────────────────────────────────────────────────────────
TOKEN: str = os.getenv("TOKEN", "")
API: str = os.getenv("MAX_API_URL", "https://platform-api2.max.ru")  # см. dev.max.ru/docs-api — домен сменился с platform-api

# ── Flask-портал ──────────────────────────────────────────────────────────────
SECRET_KEY: str = os.getenv("SECRET_KEY", "change-me-in-production")

# ── FastAPI ───────────────────────────────────────────────────────────────────
API_HOST: str = os.getenv("API_HOST", "127.0.0.1")
API_PORT: int = int(os.getenv("API_PORT", "8000"))
# URL внутреннего FastAPI — используется bot.py и client_api.py
FASTAPI_BASE_URL: str = os.getenv(
    "FASTAPI_BASE_URL", f"http://{os.getenv('API_HOST', '127.0.0.1')}:{os.getenv('API_PORT', '8000')}"
)
# Bearer-токен для внутренних вызовов (бот → FastAPI)
INTERNAL_API_TOKEN: str = os.getenv("INTERNAL_API_TOKEN", "")

# ── Авторизация в боте ────────────────────────────────────────────────────────
MAX_AUTH_ATTEMPTS: int = int(os.getenv("MAX_AUTH_ATTEMPTS", "5"))
AUTH_BLOCK_MINUTES: int = int(os.getenv("AUTH_BLOCK_MINUTES", "30"))
SESSION_TTL_MINUTES: int = int(os.getenv("SESSION_TTL_MINUTES", "10"))

# ── Обращения ─────────────────────────────────────────────────────────────────
# Через сколько часов pending_confirmation автоматически → resolved (APScheduler, Этап 6)
APPEAL_PENDING_AUTO_CLOSE_HOURS: int = int(os.getenv("APPEAL_PENDING_AUTO_CLOSE_HOURS", "24"))
# Префикс номера тикета: RSO-20260702-0001
TICKET_PREFIX: str = os.getenv("TICKET_PREFIX", "RSO")

# ── Синхронизация с 1С ────────────────────────────────────────────────────────
CACHE_CLEANUP_DAYS: int = int(os.getenv("CACHE_CLEANUP_DAYS", "90"))

ENABLE_1C_INTEGRATION: bool = os.getenv("ENABLE_1C_INTEGRATION", "false").lower() == "true"
INTEGRATION_1C_MOCK: bool = os.getenv("INTEGRATION_1C_MOCK", "true").lower() == "true"
INTEGRATION_1C_BASE_URL: str = os.getenv("INTEGRATION_1C_BASE_URL", "").rstrip("/")
INTEGRATION_1C_AUTH_TOKEN: str = os.getenv("INTEGRATION_1C_AUTH_TOKEN", "")
INTEGRATION_1C_AUTH_TIMEOUT_SECONDS: int = int(
    os.getenv("INTEGRATION_1C_AUTH_TIMEOUT_SECONDS", "5")
)
INTEGRATION_1C_SYNC_TIMEOUT_SECONDS: int = int(
    os.getenv("INTEGRATION_1C_SYNC_TIMEOUT_SECONDS", "60")
)
INTEGRATION_1C_SYNC_PERIOD_HOURS: int = int(
    os.getenv("INTEGRATION_1C_SYNC_PERIOD_HOURS", "24")
)
INTEGRATION_1C_SYNC_RETRY_HOURS: int = int(
    os.getenv("INTEGRATION_1C_SYNC_RETRY_HOURS", "1")
)
INTEGRATION_1C_BATCH_SIZE: int = int(os.getenv("INTEGRATION_1C_BATCH_SIZE", "500"))
INTEGRATION_1C_CODE_TTL_MINUTES: int = int(
    os.getenv("INTEGRATION_1C_CODE_TTL_MINUTES", "10")
)
INTEGRATION_1C_CODE_MAX_ATTEMPTS: int = int(
    os.getenv("INTEGRATION_1C_CODE_MAX_ATTEMPTS", "5")
)
# Только для локальной имитации. В production код генерирует и хранит 1С.
INTEGRATION_1C_MOCK_CODE: str = os.getenv("INTEGRATION_1C_MOCK_CODE", "000000")

# ── Маркерные слова для автоматического повышения приоритета ──────────────────
# Используются в FastAPI (Этап 2), здесь только хранятся
EMERGENCY_KEYWORDS: tuple[str, ...] = (
    "авария", "прорыв", "затопление",
    "нет воды", "нет света", "нет тепла",
)

# ── Валидация при старте ───────────────────────────────────────────────────────
def _validate() -> None:
    logger = logging.getLogger(__name__)
    if not TOKEN:
        logger.warning("TOKEN не задан — MAX-бот не запустится")
    if SECRET_KEY == "change-me-in-production":
        logger.warning("SECRET_KEY не изменён — небезопасно для production")
    if not INTERNAL_API_TOKEN:
        logger.warning("INTERNAL_API_TOKEN не задан — внутренние вызовы бот→FastAPI не защищены")
    if ENABLE_1C_INTEGRATION and not INTEGRATION_1C_MOCK:
        if not INTEGRATION_1C_BASE_URL:
            logger.warning("INTEGRATION_1C_BASE_URL не задан — реальная интеграция с 1С не запустится")
        if not INTEGRATION_1C_AUTH_TOKEN:
            logger.warning("INTEGRATION_1C_AUTH_TOKEN не задан — 1С отклонит запросы")

_validate()
