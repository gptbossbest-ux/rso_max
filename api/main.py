"""
api/main.py — FastAPI-приложение РСО Портал.

Запуск:
    uvicorn api.main:app --host 127.0.0.1 --port 8000 --reload

Systemd-сервис (production):
    ExecStart=/home/rsobot/rso_bot/venv/bin/uvicorn api.main:app \
              --host 127.0.0.1 --port 8000 --workers 1

Порты:
    5000 — Flask-портал (web.py)
    8000 — FastAPI (этот файл)

Оба процесса обращаются к одному database.sqlite.
WAL-режим включён в get_conn() — параллельный доступ безопасен.

Роутеры Горизонта 1:
    /api/v1/appeals          — этот файл (Этап 2)

Роутеры следующих этапов (добавить здесь при реализации):
    /api/v1/scripts          — Этап 9
    /api/v1/house-chats      — Этап 7
    /api/v1/broadcast        — Этап 8
"""
from __future__ import annotations

# ── Сертификаты Минцифры (platform-api2.max.ru) ──────────────────────────────
# Должно выполниться до api.routers.appeals → api.notifier → httpx (импорт
# роутера ниже по файлу, но порядок инъекции важен, а не порядок импорта
# httpx как такового — переключаем ssl глобально для процесса как можно раньше).
import truststore
truststore.inject_into_ssl()

import logging
import os
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import (
    API_HOST,
    API_PORT,
    ENABLE_1C_INTEGRATION,
    INTEGRATION_1C_SYNC_RETRY_HOURS,
    LOG_BACKUP_COUNT,
    LOG_FILE,
    LOG_LEVEL,
    LOG_MAX_BYTES,
)
from database import init_db
from sync_1c import sync_1c_job

# ── Логгер ────────────────────────────────────────────────────────────────────

def _setup_logging() -> None:
    """Настройка корневого логгера FastAPI-процесса."""
    log_dir = os.path.dirname(LOG_FILE)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(getattr(logging, LOG_LEVEL.upper(), logging.INFO))

    if not root.handlers:
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        root.addHandler(ch)

    log_name = LOG_FILE.replace(".log", "_api.log")
    try:
        fh = RotatingFileHandler(
            log_name,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError as exc:
        logging.warning("Не удалось открыть файл лога %s: %s", log_name, exc)


_setup_logging()
log = logging.getLogger("rso.api")


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Инициализация при старте, очистка при завершении."""
    log.info("FastAPI запускается на %s:%s", API_HOST, API_PORT)
    init_db()
    log.info("БД инициализирована")
    scheduler = None
    if ENABLE_1C_INTEGRATION:
        scheduler = BackgroundScheduler(timezone="Europe/Moscow")
        scheduler.add_job(
            sync_1c_job,
            trigger="interval",
            hours=INTEGRATION_1C_SYNC_RETRY_HOURS,
            id="sync_1c",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=1800,
        )
        scheduler.start()
        log.info("Планировщик синхронизации 1С запущен")
    yield
    if scheduler is not None:
        scheduler.shutdown(wait=False)
    log.info("FastAPI завершает работу")


# ── Приложение ────────────────────────────────────────────────────────────────

app = FastAPI(
    title="РСО Портал API",
    description=(
        "REST API платформы автоматизации коммуникаций РСО/УК/ТСЖ. "
        "Горизонт 1 — MVP."
    ),
    version="1.0.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)

# CORS — только localhost в production (Nginx проксирует с одного домена)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5000", "http://127.0.0.1:5000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Роутеры ───────────────────────────────────────────────────────────────────

from api.routers.appeals import router as appeals_router  # noqa: E402
from api.routers.integration_1c import router as integration_1c_router  # noqa: E402
from api.routers.scripts import router as scripts_router  # noqa: E402

app.include_router(appeals_router, prefix="/api/v1")
app.include_router(integration_1c_router, prefix="/api/v1")
app.include_router(scripts_router, prefix="/api/v1")

# TODO Этап 7:  from api.routers.house_chats  import router as house_chats_router
#               app.include_router(house_chats_router, prefix="/api/v1")
# TODO Этап 8:  from api.routers.broadcast    import router as broadcast_router
#               app.include_router(broadcast_router, prefix="/api/v1")


# ── Health-check ──────────────────────────────────────────────────────────────

@app.get("/healthz", tags=["system"], summary="Health-check")
async def healthz() -> dict:
    return {"status": "ok"}


# ── Точка входа (прямой запуск: python api/main.py) ──────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "api.main:app",
        host=API_HOST,
        port=API_PORT,
        reload=False,
    )
