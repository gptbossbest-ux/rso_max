"""
database.py — уровень доступа к данным, РСО Портал, Горизонт 1.

Изменения относительно предыдущей версии (Этап 1):
  - get_conn(): добавлены PRAGMA foreign_keys=ON и journal_mode=WAL
  - init_db(): идемпотентная миграция appeals → appeals_legacy;
    создание новых таблиц по ТЗ разделы 4.1–4.5 + chat_exclusions (7.4)
  - Удалены функции старого флоу обращений:
      add_appeal, get_appeals, get_appeal,
      update_appeal_status, append_to_appeal_text
    (заменяются FastAPI-слоем в Этапе 2)
  - get_counts() обновлён под новую схему appeals
  - Добавлены CRUD-функции для нового флоу:
      create_appeal, get_appeal_by_ticket, list_appeals,
      add_appeal_response
  - Добавлены cleanup-функции для APScheduler (Этап 4):
      cleanup_old_cache
  - Логирование через RotatingFileHandler вместо print()
"""

import json
import logging
import os
import re
import sqlite3
import unicodedata
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from zoneinfo import ZoneInfo

from werkzeug.security import check_password_hash, generate_password_hash

from config import (
    BOOTSTRAP_ADMIN_PASSWORD,
    DB_PATH,
    LOG_BACKUP_COUNT,
    LOG_FILE,
    LOG_LEVEL,
    LOG_MAX_BYTES,
    TICKET_PREFIX,
    TIMEZONE_OFFSET,
)

# ── Логгер модуля ─────────────────────────────────────────────────────────────

def _setup_logger() -> logging.Logger:
    log_dir = os.path.dirname(LOG_FILE)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    logger = logging.getLogger("rso.database")
    if logger.handlers:          # уже настроен (повторный импорт)
        return logger

    logger.setLevel(getattr(logging, LOG_LEVEL.upper(), logging.INFO))

    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Консоль
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # Файл с ротацией
    try:
        fh = RotatingFileHandler(
            LOG_FILE, maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT, encoding="utf-8",
        )
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError as exc:
        logger.warning("Не удалось открыть файл лога %s: %s", LOG_FILE, exc)

    return logger


log = _setup_logger()


# ── Утилиты времени ───────────────────────────────────────────────────────────

def msk_now() -> str:
    """Текущее время в настроенном часовом поясе (строка 'YYYY-MM-DD HH:MM')."""
    tz = timezone(timedelta(hours=TIMEZONE_OFFSET))
    return datetime.now(tz).strftime("%Y-%m-%d %H:%M")


# ── Соединение с БД ───────────────────────────────────────────────────────────

def get_conn() -> sqlite3.Connection:
    """
    Открывает соединение с SQLite.
    PRAGMA foreign_keys=ON  — проверка FK-констрейнтов.
    PRAGMA journal_mode=WAL — параллельный доступ без блокировок
                              (бот + FastAPI + Flask на одном файле).
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


# ── Инициализация и миграции ──────────────────────────────────────────────────

def _migrate_appeals_legacy(c: sqlite3.Cursor) -> None:
    """
    Идемпотентная миграция: если таблица appeals существует,
    но не содержит ticket_no (старая схема) — переименовываем в appeals_legacy.
    При повторном запуске init_db() уже не срабатывает.
    """
    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='appeals'")
    if not c.fetchone():
        return  # таблицы нет — нечего мигрировать

    c.execute("PRAGMA table_info(appeals)")
    cols = {row[1] for row in c.fetchall()}
    if "ticket_no" in cols:
        return  # уже новая схема

    log.info("Миграция: appeals → appeals_legacy (старая схема без ticket_no)")
    c.execute("ALTER TABLE appeals RENAME TO appeals_legacy")


def _ensure_column(c: sqlite3.Cursor, table: str, column: str, definition: str) -> None:
    """Идемпотентно добавляет колонку в существующую SQLite-таблицу."""
    columns = {row[1] for row in c.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        log.info("Миграция: добавлена колонка %s.%s", table, column)


def init_db() -> None:
    """
    Создаёт/обновляет схему БД. Идемпотентна — безопасно вызывать при каждом старте.

    Порядок:
      1. Миграция appeals → appeals_legacy (если нужна)
      2. Существующие таблицы (не меняются)
      3. Новые таблицы Горизонта 1
      4. Индексы
      5. Seed: admin-пользователь
    """
    bootstrap_password = BOOTSTRAP_ADMIN_PASSWORD.strip()
    if bootstrap_password and len(bootstrap_password) < 12:
        raise RuntimeError("BOOTSTRAP_ADMIN_PASSWORD должен содержать не менее 12 символов")

    conn = get_conn()
    c = conn.cursor()

    # 1. Миграция ──────────────────────────────────────────────────────────────
    _migrate_appeals_legacy(c)

    # 2. Существующие таблицы (схема не меняется) ──────────────────────────────

    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            name     TEXT NOT NULL,
            role     TEXT DEFAULT 'operator',
            session_version INTEGER NOT NULL DEFAULT 1,
            must_change_password INTEGER NOT NULL DEFAULT 0
        )
    """)
    _ensure_column(c, "users", "session_version", "INTEGER NOT NULL DEFAULT 1")
    _ensure_column(c, "users", "must_change_password", "INTEGER NOT NULL DEFAULT 0")

    c.execute("""
        CREATE TABLE IF NOT EXISTS licschet (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            number  TEXT UNIQUE NOT NULL,
            fio     TEXT,
            address TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS schetchiki (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ls            TEXT NOT NULL,
            resource_type TEXT,
            meter_number  TEXT,
            meter_type    TEXT,
            initial1      TEXT DEFAULT '0',
            initial2      TEXT DEFAULT '0'
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS bot_users (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id   INTEGER UNIQUE NOT NULL,
            ls        TEXT,
            fio       TEXT,
            last_seen TEXT,
            authorized_1c INTEGER NOT NULL DEFAULT 0
        )
    """)
    _ensure_column(c, "bot_users", "authorized_1c", "INTEGER NOT NULL DEFAULT 0")

    c.execute("""
        CREATE TABLE IF NOT EXISTS pokazaniya (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id       INTEGER,
            ls            TEXT,
            resource_type TEXT,
            meter_number  TEXT,
            value1        TEXT,
            value2        TEXT,
            created_at    TEXT,
            sent_to_1c    INTEGER NOT NULL DEFAULT 0,
            status_1c     TEXT DEFAULT NULL,
            batch_id      TEXT DEFAULT NULL
        )
    """)

    _ensure_column(c, "pokazaniya", "sent_to_1c", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(c, "pokazaniya", "status_1c", "TEXT DEFAULT NULL")
    _ensure_column(c, "pokazaniya", "batch_id", "TEXT DEFAULT NULL")
    # Excel imports use this exact signature for starting meter values, not
    # customer submissions. Keep genuine historical readings in the queue.
    c.execute("""
        UPDATE pokazaniya SET sent_to_1c=1
        WHERE chat_id=0 AND created_at='2000-01-01 00:00' AND sent_to_1c=0
          AND batch_id IS NULL
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS sync_1c_state (
            id               INTEGER PRIMARY KEY CHECK (id = 1),
            last_success_at  TEXT DEFAULT NULL,
            last_attempt_at  TEXT DEFAULT NULL,
            pending_batch_id TEXT DEFAULT NULL
        )
    """)
    c.execute("INSERT OR IGNORE INTO sync_1c_state (id) VALUES (1)")

    # Таблица comments остаётся как архивная (appeal_id → appeals_legacy).
    # Новые ответы операторов идут в appeal_responses.
    c.execute("""
        CREATE TABLE IF NOT EXISTS comments (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            appeal_id INTEGER NOT NULL,
            author    TEXT,
            text      TEXT,
            created_at TEXT
        )
    """)

    # 3. Новые таблицы Горизонта 1 ─────────────────────────────────────────────

    # 4.1 Обращения (новая схема)
    c.execute("""
        CREATE TABLE IF NOT EXISTS appeals (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_no         TEXT UNIQUE NOT NULL,
            ls                TEXT,
            channel           TEXT NOT NULL
                                CHECK(channel IN ('max','telegram','lk','widget')),
            category          TEXT NOT NULL
                                CHECK(category IN ('заявка','авария','качество','прочее')),
            priority          TEXT DEFAULT 'normal'
                                CHECK(priority IN ('normal','high')),
            status            TEXT DEFAULT 'new'
                                CHECK(status IN (
                                    'new','in_work',
                                    'pending_confirmation',
                                    'resolved','closed'
                                )),
            body              TEXT,
            file_path         TEXT,
            chat_id           INTEGER,
            created_at        TEXT,
            updated_at        TEXT,
            first_response_at TEXT,
            closed_at         TEXT,
            reopen_reason     TEXT
        )
    """)
    _ensure_column(c, "appeals", "reopen_reason", "TEXT")

    # 4.2 Ответы операторов
    c.execute("""
        CREATE TABLE IF NOT EXISTS appeal_responses (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            appeal_id   INTEGER NOT NULL REFERENCES appeals(id),
            operator_id INTEGER NOT NULL REFERENCES users(id),
            body        TEXT NOT NULL,
            sent_at     TEXT
        )
    """)

    # 4.3 Скрипты (табличный редактор)
    c.execute("""
        CREATE TABLE IF NOT EXISTS scripts (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            title      TEXT NOT NULL,
            is_active  INTEGER DEFAULT 1,
            sort_order INTEGER DEFAULT 0
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS script_nodes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            script_id   INTEGER NOT NULL REFERENCES scripts(id),
            title       TEXT NOT NULL,
            image_path  TEXT,
            is_terminal INTEGER DEFAULT 0
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS script_edges (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            script_id    INTEGER NOT NULL REFERENCES scripts(id),
            from_node_id INTEGER NOT NULL REFERENCES script_nodes(id),
            label        TEXT NOT NULL,
            to_node_id   INTEGER NOT NULL REFERENCES script_nodes(id)
        )
    """)

    # Настройки ИИ-помощника. API-ключа здесь нет: он читается
    # только из окружения процесса.
    c.execute("""
        CREATE TABLE IF NOT EXISTS ai_settings (
            id                INTEGER PRIMARY KEY CHECK (id = 1),
            enabled           INTEGER NOT NULL DEFAULT 0,
            model             TEXT NOT NULL DEFAULT 'yandexgpt/latest',
            system_prompt     TEXT NOT NULL,
            daily_limit       INTEGER NOT NULL DEFAULT 5 CHECK (daily_limit BETWEEN 1 AND 100),
            temperature       REAL NOT NULL DEFAULT 0.3 CHECK (temperature BETWEEN 0 AND 1),
            max_output_tokens INTEGER NOT NULL DEFAULT 800 CHECK (max_output_tokens BETWEEN 1 AND 8000)
        )
    """)
    default_ai_prompt = (
        "Ты — ИИ-помощник РСО по вопросам ЖКХ. Отвечай только по теме ЖКХ, "
        "кратко и понятно. Не выдумывай тарифы, нормы, адреса или факты. "
        "Если не уверен в ответе или нужны данные клиента, прямо скажи об этом и "
        "предложи оформить обращение. Не запрашивай персональные данные."
    )
    c.execute(
        "INSERT OR IGNORE INTO ai_settings (id, system_prompt) VALUES (1, ?)",
        (default_ai_prompt,),
    )

    # Это не журнал: ровно одна обезличенная текущая сессия на MAX ID.
    # При первом доступе в новую дату старая строка заменяется.
    c.execute("""
        CREATE TABLE IF NOT EXISTS ai_daily_sessions (
            chat_id        INTEGER PRIMARY KEY,
            session_date   TEXT NOT NULL,
            question_count INTEGER NOT NULL DEFAULT 0,
            history_json   TEXT NOT NULL DEFAULT '[]',
            faq_context    TEXT,
            updated_at     TEXT NOT NULL
        )
    """)
    # TODO Этап 10 (табличный редактор скриптов): при сохранении рёбер
    # добавить валидацию на отсутствие циклов в графе (DFS/топологическая сортировка).
    # Цикл в скрипте приведёт к бесконечному навигационному циклу в боте.
    # Реализовать в api/routers/scripts.py (POST /api/v1/scripts/{id}/edges)
    # до коммита транзакции, возвращать HTTP 422 при обнаружении цикла.

    # 4.4 Домовые чаты
    c.execute("""
        CREATE TABLE IF NOT EXISTS house_chats (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            address         TEXT NOT NULL,
            messenger       TEXT NOT NULL,
            chat_id         TEXT NOT NULL,
            is_active       INTEGER DEFAULT 1,
            connected_at    TEXT,
            disconnected_at TEXT
        )
    """)

    # 4.5 Сценарии мониторинга
    c.execute("""
        CREATE TABLE IF NOT EXISTS chat_scenarios (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            title          TEXT NOT NULL,
            keywords       TEXT NOT NULL,   -- JSON-массив строк
            response_text  TEXT NOT NULL,
            suggest_appeal INTEGER DEFAULT 0,
            is_active      INTEGER DEFAULT 1
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS chat_scenario_links (
            scenario_id   INTEGER NOT NULL REFERENCES chat_scenarios(id),
            house_chat_id INTEGER NOT NULL REFERENCES house_chats(id),
            PRIMARY KEY (scenario_id, house_chat_id)
        )
    """)

    # 7.4 Пользователи-исключения для домовых чатов
    # messenger + user_id — мессенджеро-независимая идентификация пользователя
    # (MAX: messenger='max', user_id=str(max_user_id);
    #  Telegram: messenger='telegram', user_id=str(tg_user_id))
    c.execute("""
        CREATE TABLE IF NOT EXISTS chat_exclusions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            house_chat_id INTEGER NOT NULL REFERENCES house_chats(id),
            messenger     TEXT    NOT NULL,
            user_id       TEXT    NOT NULL,
            reason        TEXT,
            created_at    TEXT,
            UNIQUE(house_chat_id, messenger, user_id)
        )
    """)

    # ── Модуль записи на приём (Этап B) ──────────────────────────────────────

    # Филиалы
    c.execute("""
        CREATE TABLE IF NOT EXISTS branches (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT    NOT NULL,
            address      TEXT    NOT NULL,
            is_active    INTEGER DEFAULT 1
        )
    """)

    # Расписание филиала — повторяющееся недельное
    # weekday: 0=Пн … 6=Вс
    # slot_duration_min: продолжительность слота в минутах
    # capacity: вместимость одного слота (кол-во клиентов)
    # booking_horizon_days: на сколько дней вперёд открыта запись
    c.execute("""
        CREATE TABLE IF NOT EXISTS branch_schedules (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            branch_id            INTEGER NOT NULL REFERENCES branches(id),
            weekday              INTEGER NOT NULL CHECK(weekday BETWEEN 0 AND 6),
            time_from            TEXT    NOT NULL,  -- 'HH:MM'
            time_to              TEXT    NOT NULL,  -- 'HH:MM'
            slot_duration_min    INTEGER NOT NULL DEFAULT 30 CHECK(slot_duration_min > 0),
            capacity             INTEGER NOT NULL DEFAULT 1 CHECK(capacity > 0),
            booking_horizon_days INTEGER NOT NULL DEFAULT 14,
            is_active            INTEGER DEFAULT 1
        )
    """)

    # Исключения из расписания — конкретные даты когда филиал не работает
    c.execute("""
        CREATE TABLE IF NOT EXISTS schedule_exceptions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            branch_id   INTEGER NOT NULL REFERENCES branches(id),
            date        TEXT    NOT NULL,  -- 'YYYY-MM-DD'
            reason      TEXT,
            UNIQUE(branch_id, date)
        )
    """)

    # Записи на приём
    # status: active | cancelled | visited | no_show
    c.execute("""
        CREATE TABLE IF NOT EXISTS appointments (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            ls             TEXT    NOT NULL,
            branch_id      INTEGER NOT NULL REFERENCES branches(id),
            slot_date      TEXT    NOT NULL,  -- 'YYYY-MM-DD'
            slot_time      TEXT    NOT NULL,  -- 'HH:MM'
            theme          TEXT,
            status         TEXT    NOT NULL DEFAULT 'active'
                               CHECK(status IN ('active','cancelled','visited','no_show')),
            channel        TEXT    NOT NULL,  -- 'max' | 'telegram'
            chat_id        INTEGER NOT NULL,
            cancel_reason  TEXT,
            reminded_24h   INTEGER DEFAULT 0,  -- 1 = напоминание за 24ч отправлено
            reminded_day   INTEGER DEFAULT 0,  -- 1 = напоминание в день приёма отправлено
            created_at     TEXT,
            cancelled_at   TEXT,
            cancelled_by   TEXT    -- 'client' | 'operator' | 'system'
        )
    """)

    # Индексы для модуля записи
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_appointments_ls
        ON appointments(ls)
    """)
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_appointments_slot
        ON appointments(branch_id, slot_date, slot_time)
    """)
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_appointments_status
        ON appointments(status)
    """)
    c.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_appointments_active_ls
        ON appointments(ls) WHERE status = 'active'
    """)
    # Контроль вместимости слота реализуется на уровне приложения (SELECT COUNT + транзакция),
    # а не индексом — вместимость > 1 не позволяет использовать UNIQUE.

    # Один активный чат на адрес (NFR: один дом = не более одного активного chat_id)
    c.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_house_chats_active_address
        ON house_chats(address) WHERE is_active = 1
    """)

    # Ускорение типовых выборок
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_appeals_status
        ON appeals(status)
    """)
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_appeals_ls
        ON appeals(ls)
    """)
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_appeals_created_at
        ON appeals(created_at)
    """)
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_appeal_responses_appeal
        ON appeal_responses(appeal_id)
    """)
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_pokazaniya_ls_meter
        ON pokazaniya(ls, meter_number)
    """)
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_pokazaniya_1c_queue
        ON pokazaniya(sent_to_1c, batch_id, created_at)
    """)
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_schetchiki_ls_meter
        ON schetchiki(ls, meter_number)
    """)

    # 5. Seed ──────────────────────────────────────────────────────────────────
    c.execute("SELECT COUNT(*) FROM users")
    users_empty = c.fetchone()[0] == 0
    if users_empty and bootstrap_password:
        c.execute(
            "INSERT INTO users "
            "(username, password, name, role, must_change_password) VALUES (?, ?, ?, ?, 1)",
            ("admin", generate_password_hash(bootstrap_password), "Администратор", "admin"),
        )
        log.warning("Создан bootstrap-администратор admin; требуется смена пароля")
    elif users_empty:
        log.warning("Пользователи отсутствуют; задайте BOOTSTRAP_ADMIN_PASSWORD для bootstrap")

    # Обезвреживаем инсталляции старых версий с публично известным admin123.
    legacy_admin = c.execute(
        "SELECT id, password FROM users WHERE username='admin'"
    ).fetchone()
    if legacy_admin and check_password_hash(legacy_admin["password"], "admin123"):
        if not bootstrap_password:
            conn.close()
            raise RuntimeError(
                "Обнаружен admin с устаревшим паролем admin123. "
                "Задайте BOOTSTRAP_ADMIN_PASSWORD (не менее 12 символов) и перезапустите."
            )
        c.execute(
            "UPDATE users SET password=?, must_change_password=1, "
            "session_version=session_version+1 WHERE id=?",
            (generate_password_hash(bootstrap_password), legacy_admin["id"]),
        )
        log.warning("Небезопасный старый пароль admin заменён bootstrap-секретом")

    conn.commit()
    conn.close()
    log.info("init_db() завершён успешно")


# ── Утилита генерации номера тикета ───────────────────────────────────────────

def _next_ticket_no(c: sqlite3.Cursor) -> str:
    """
    Генерирует уникальный ticket_no вида RSO-20260702-0001.
    Счётчик сбрасывается каждый день.
    """
    today = datetime.now(timezone(timedelta(hours=TIMEZONE_OFFSET))).strftime("%Y%m%d")
    prefix = f"{TICKET_PREFIX}-{today}-"
    c.execute(
        "SELECT ticket_no FROM appeals WHERE ticket_no LIKE ? ORDER BY id DESC LIMIT 1",
        (prefix + "%",),
    )
    row = c.fetchone()
    if row:
        last_seq = int(row[0].split("-")[-1])
        seq = last_seq + 1
    else:
        seq = 1
    return f"{prefix}{seq:04d}"


# ── CRUD: обращения (новая схема) ─────────────────────────────────────────────

def create_appeal(
    ls: str | None,
    channel: str,
    category: str,
    body: str,
    chat_id: int | None = None,
    file_path: str | None = None,
    priority: str = "normal",
) -> str:
    """
    Создаёт обращение, возвращает ticket_no.
    Логика маркерных слов (авария → priority=high) — в FastAPI-слое (Этап 2),
    здесь принимаем уже разрешённые значения.
    """
    conn = get_conn()
    c = conn.cursor()
    ticket_no = _next_ticket_no(c)
    c.execute(
        """
        INSERT INTO appeals
            (ticket_no, ls, channel, category, priority, body, chat_id, file_path, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (ticket_no, ls, channel, category, priority, body, chat_id, file_path, msk_now()),
    )
    conn.commit()
    conn.close()
    log.info("Создано обращение %s  канал=%s  категория=%s", ticket_no, channel, category)
    return ticket_no


def get_appeal_by_ticket(ticket_no: str) -> sqlite3.Row | None:
    """Возвращает обращение по ticket_no (для GET /api/v1/appeals/{ticket_no})."""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM appeals WHERE ticket_no = ?", (ticket_no,)
    ).fetchone()
    conn.close()
    return row


def get_appeal_by_id(appeal_id: int) -> sqlite3.Row | None:
    conn = get_conn()
    row = conn.execute("SELECT * FROM appeals WHERE id = ?", (appeal_id,)).fetchone()
    conn.close()
    return row


def list_appeals(
    status: str | None = None,
    category: str | None = None,
    priority: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    ls: str | None = None,
) -> list[sqlite3.Row]:
    """
    Список обращений с фильтрами для операторского портала.
    Все параметры опциональны.
    """
    query = "SELECT * FROM appeals WHERE 1=1"
    params: list = []
    if status:
        query += " AND status = ?"
        params.append(status)
    if category:
        query += " AND category = ?"
        params.append(category)
    if priority:
        query += " AND priority = ?"
        params.append(priority)
    if date_from:
        query += " AND created_at >= ?"
        params.append(date_from)
    if date_to:
        query += " AND created_at <= ?"
        params.append(date_to)
    if ls:
        query += " AND ls = ?"
        params.append(ls)
    query += " ORDER BY created_at DESC"

    conn = get_conn()
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return rows


def update_appeal_status(
    appeal_id: int,
    status: str,
    operator_id: int | None = None,
    reason: str | None = None,
) -> None:
    """
    Меняет статус обращения.
    При status='closed' или 'resolved' проставляет closed_at.
    operator_id используется для аудита (Этап 2/5).
    """
    conn = get_conn()
    now = msk_now()
    if status == "in_work" and reason:
        conn.execute(
            "UPDATE appeals SET status=?, updated_at=?, closed_at=NULL, reopen_reason=? WHERE id=?",
            (status, now, reason.strip(), appeal_id),
        )
    elif status in ("closed", "resolved"):
        conn.execute(
            "UPDATE appeals SET status=?, updated_at=?, closed_at=? WHERE id=?",
            (status, now, now, appeal_id),
        )
    else:
        conn.execute(
            "UPDATE appeals SET status=?, updated_at=? WHERE id=?",
            (status, now, appeal_id),
        )
    conn.commit()
    conn.close()
    log.info("Статус обращения id=%s изменён на %s (оператор id=%s)", appeal_id, status, operator_id)


def auto_resolve_pending(hours: int | None = None) -> int:
    """
    Переводит обращения из pending_confirmation → resolved,
    если прошло более `hours` часов с момента последнего изменения статуса
    (поле updated_at; если NULL — используется created_at).

    Вызывается APScheduler-задачей (Этап 6).
    Возвращает количество закрытых обращений.

    hours: переопределяет APPEAL_PENDING_AUTO_CLOSE_HOURS из config.
    """
    from config import APPEAL_PENDING_AUTO_CLOSE_HOURS  # noqa: PLC0415 — отложенный импорт константы
    if hours is None:
        hours = APPEAL_PENDING_AUTO_CLOSE_HOURS

    tz = timezone(timedelta(hours=TIMEZONE_OFFSET))
    cutoff = (datetime.now(tz) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M")

    conn = get_conn()
    # Находим кандидатов: pending_confirmation дольше `hours` часов
    rows = conn.execute(
        """
        SELECT id FROM appeals
        WHERE status = 'pending_confirmation'
          AND COALESCE(updated_at, created_at) <= ?
        """,
        (cutoff,),
    ).fetchall()

    count = 0
    now = msk_now()
    for row in rows:
        cursor = conn.execute(
            "UPDATE appeals SET status='resolved', closed_at=?, updated_at=? "
            "WHERE id=? AND status='pending_confirmation' "
            "AND COALESCE(updated_at, created_at) <= ?",
            (now, now, row["id"], cutoff),
        )
        count += cursor.rowcount

    conn.commit()
    conn.close()

    if count:
        log.info(
            "auto_resolve_pending: %d обращений переведено в resolved "
            "(порог %d ч, cutoff=%s)",
            count, hours, cutoff,
        )
    return count


def add_appeal_response(appeal_id: int, operator_id: int, body: str) -> int:
    """
    Фиксирует ответ оператора.
    Если это первый ответ — проставляет first_response_at в appeals (SLA).
    Возвращает id записи.
    """
    conn = get_conn()
    now = msk_now()

    row_id = conn.execute(
        "INSERT INTO appeal_responses (appeal_id, operator_id, body, sent_at) VALUES (?, ?, ?, ?)",
        (appeal_id, operator_id, body, now),
    ).lastrowid

    # first_response_at — только если ещё не проставлен
    conn.execute(
        "UPDATE appeals SET first_response_at=? WHERE id=? AND first_response_at IS NULL",
        (now, appeal_id),
    )

    conn.commit()
    conn.close()
    log.info("Ответ оператора id=%s добавлен к обращению id=%s", operator_id, appeal_id)
    return row_id


def get_last_appeal_response(appeal_id: int) -> sqlite3.Row | None:
    """Последний ответ оператора по обращению."""
    conn = get_conn()
    row = conn.execute(
        "SELECT ar.*, u.name AS operator_name "
        "FROM appeal_responses ar "
        "LEFT JOIN users u ON u.id = ar.operator_id "
        "WHERE ar.appeal_id = ? ORDER BY ar.sent_at DESC, ar.id DESC LIMIT 1",
        (appeal_id,),
    ).fetchone()
    conn.close()
    return row


def get_all_appeal_responses(appeal_id: int) -> list[sqlite3.Row]:
    """Все ответы операторов по обращению (для карточки в портале)."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT ar.*, u.name AS operator_name "
        "FROM appeal_responses ar "
        "LEFT JOIN users u ON u.id = ar.operator_id "
        "WHERE ar.appeal_id = ? ORDER BY ar.sent_at ASC",
        (appeal_id,),
    ).fetchall()
    conn.close()
    return rows


# ── CRUD: скрипты ─────────────────────────────────────────────────────────────

def get_active_scripts() -> list[sqlite3.Row]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, title, sort_order FROM scripts WHERE is_active=1 ORDER BY sort_order, id"
    ).fetchall()
    conn.close()
    return rows


def get_script_tree(script_id: int) -> dict | None:
    """
    Возвращает полный граф скрипта: заголовок + узлы + переходы.
    Формат совпадает с ответом GET /api/v1/scripts/{id}/tree.
    """
    conn = get_conn()
    script = conn.execute(
        "SELECT id, title FROM scripts WHERE id=? AND is_active=1", (script_id,)
    ).fetchone()
    if not script:
        conn.close()
        return None

    nodes = conn.execute(
        "SELECT id, title, is_terminal, image_path FROM script_nodes WHERE script_id=?",
        (script_id,),
    ).fetchall()

    edges = conn.execute(
        "SELECT from_node_id, label, to_node_id FROM script_edges WHERE script_id=?",
        (script_id,),
    ).fetchall()

    conn.close()
    return {
        "id": script["id"],
        "title": script["title"],
        "nodes": [dict(n) for n in nodes],
        "edges": [dict(e) for e in edges],
    }


# ── CRUD: домовые чаты ────────────────────────────────────────────────────────

def get_house_chats(active_only: bool = True) -> list[sqlite3.Row]:
    conn = get_conn()
    if active_only:
        rows = conn.execute(
            "SELECT * FROM house_chats WHERE is_active=1 ORDER BY address"
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM house_chats ORDER BY address").fetchall()
    conn.close()
    return rows


def get_house_chat_by_chat_id(chat_id: str) -> sqlite3.Row | None:
    """Поиск активного домового чата по chat_id мессенджера."""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM house_chats WHERE chat_id=? AND is_active=1", (chat_id,)
    ).fetchone()
    conn.close()
    return row


def add_house_chat(address: str, messenger: str, chat_id: str) -> int:
    conn = get_conn()
    try:
        row_id = conn.execute(
            "INSERT INTO house_chats (address, messenger, chat_id, is_active, connected_at) "
            "VALUES (?, ?, ?, 1, ?)",
            (address, messenger, chat_id, msk_now()),
        ).lastrowid
        conn.commit()
        log.info("Домовой чат подключён: адрес=%s  мессенджер=%s  chat_id=%s", address, messenger, chat_id)
        return row_id
    finally:
        conn.close()


def deactivate_house_chat(house_chat_id: int) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE house_chats SET is_active=0, disconnected_at=? WHERE id=?",
            (msk_now(), house_chat_id),
        )
        conn.commit()
        log.info("Домовой чат id=%s отвязан (is_active=0)", house_chat_id)
    finally:
        conn.close()


def get_scenarios_for_chat(house_chat_id: int) -> list[sqlite3.Row]:
    """Активные сценарии, привязанные к домовому чату."""
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT cs.*
        FROM chat_scenarios cs
        JOIN chat_scenario_links csl ON csl.scenario_id = cs.id
        WHERE csl.house_chat_id = ? AND cs.is_active = 1
        ORDER BY cs.id ASC
        """,
        (house_chat_id,),
    ).fetchall()
    conn.close()
    return rows


def is_user_excluded(house_chat_id: int, messenger: str, user_id: str) -> bool:
    """
    Проверяет, находится ли пользователь в списке исключений для данного чата.
    Вызывается в polling до обработки ключевых слов (раздел 7.4 ТЗ).

    messenger: 'max' | 'telegram'
    user_id:   строковое представление ID пользователя в мессенджере
    """
    conn = get_conn()
    row = conn.execute(
        "SELECT 1 FROM chat_exclusions "
        "WHERE house_chat_id=? AND messenger=? AND user_id=? LIMIT 1",
        (house_chat_id, messenger, user_id),
    ).fetchone()
    conn.close()
    return row is not None


def add_chat_exclusion(
    house_chat_id: int,
    messenger: str,
    user_id: str,
    reason: str | None = None,
) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO chat_exclusions "
            "(house_chat_id, messenger, user_id, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (house_chat_id, messenger, user_id, reason, msk_now()),
        )
        conn.commit()
        log.info(
            "Исключение добавлено: house_chat_id=%s  messenger=%s  user_id=%s",
            house_chat_id, messenger, user_id,
        )
    finally:
        conn.close()


def remove_chat_exclusion(house_chat_id: int, messenger: str, user_id: str) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "DELETE FROM chat_exclusions WHERE house_chat_id=? AND messenger=? AND user_id=?",
            (house_chat_id, messenger, user_id),
        )
        conn.commit()
        log.info(
            "Исключение удалено: house_chat_id=%s  messenger=%s  user_id=%s",
            house_chat_id, messenger, user_id,
        )
    finally:
        conn.close()


def list_chat_exclusions(house_chat_id: int) -> list[sqlite3.Row]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM chat_exclusions WHERE house_chat_id=? ORDER BY created_at DESC",
        (house_chat_id,),
    ).fetchall()
    conn.close()
    return rows


def remove_chat_exclusion_by_id(exclusion_id: int) -> None:
    """Удаляет исключение по его PK (для портала — проще чем по составному ключу)."""
    conn = get_conn()
    conn.execute("DELETE FROM chat_exclusions WHERE id=?", (exclusion_id,))
    conn.commit()
    conn.close()


def get_house_chat(house_chat_id: int) -> sqlite3.Row | None:
    """Поиск домового чата по PK (для карточки в портале, в отличие от поиска по chat_id мессенджера)."""
    conn = get_conn()
    row = conn.execute("SELECT * FROM house_chats WHERE id=?", (house_chat_id,)).fetchone()
    conn.close()
    return row


# ── Показания ─────────────────────────────────────────────────────────────────

def add_pokazaniya(
    chat_id: int,
    ls: str,
    resource_type: str,
    meter_number: str,
    value1: str,
    value2: str | None = None,
) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO pokazaniya "
            "(chat_id, ls, resource_type, meter_number, value1, value2, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (chat_id, ls, resource_type, meter_number, value1, value2, msk_now()),
        )
        conn.commit()
    finally:
        conn.close()


def get_pokazaniya() -> list[sqlite3.Row]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM pokazaniya ORDER BY id DESC").fetchall()
    conn.close()
    return rows


def get_last_pokazaniya(ls: str, meter_number: str) -> sqlite3.Row | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM pokazaniya WHERE ls=? AND meter_number=? ORDER BY id DESC LIMIT 1",
        (ls, meter_number),
    ).fetchone()
    conn.close()
    return row


def get_pokazaniya_with_prev() -> list[dict]:
    """
    Показания с предыдущими значениями и разницей (для таблицы в портале).
    Использует оконные функции LAG — один запрос вместо N+1.
    """
    conn = get_conn()
    rows = conn.execute("""
        SELECT *,
               LAG(value1) OVER (PARTITION BY ls, meter_number ORDER BY id) AS prev_value1,
               LAG(value2) OVER (PARTITION BY ls, meter_number ORDER BY id) AS prev_value2
        FROM pokazaniya
        ORDER BY id DESC
    """).fetchall()
    conn.close()

    result = []
    for r in rows:
        r = dict(r)
        try:
            r["diff1"] = round(float(r["value1"]) - float(r["prev_value1"]), 2) \
                if r["prev_value1"] is not None else None
        except (TypeError, ValueError):
            r["diff1"] = None
        try:
            r["diff2"] = round(float(r["value2"]) - float(r["prev_value2"]), 2) \
                if r["value2"] is not None and r["prev_value2"] is not None else None
        except (TypeError, ValueError):
            r["diff2"] = None
        result.append(r)
    return result


_CUSTOMER_READING_SQL = "NOT (chat_id IS 0 AND created_at IS '2000-01-01 00:00')"


def claim_1c_sync_batch(
    now: datetime,
    new_batch_id: str,
    limit: int,
    retry_hours: int,
    period_hours: int,
) -> dict | None:
    """Claim due work atomically, retaining orphan batch IDs from old versions.

    BEGIN IMMEDIATE serializes scheduler processes before they read the state.
    The attempt timestamp is committed together with membership, so another
    scheduler cannot send a different payload for the same pending batch.
    """
    def parsed(value: str | None) -> datetime | None:
        if not value:
            return None
        result = datetime.fromisoformat(value)
        return result if result.tzinfo else result.replace(
            tzinfo=timezone(timedelta(hours=TIMEZONE_OFFSET))
        )

    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT OR IGNORE INTO sync_1c_state (id) VALUES (1)")
        state = dict(conn.execute("SELECT * FROM sync_1c_state WHERE id=1").fetchone())
        batch_id = state["pending_batch_id"]
        recovered = False
        if batch_id is None:
            orphan = conn.execute(
                "SELECT batch_id FROM pokazaniya WHERE sent_to_1c=0 "
                "AND batch_id IS NOT NULL ORDER BY id LIMIT 1"
            ).fetchone()
            if orphan is not None:
                batch_id = orphan["batch_id"]
                recovered = True

        first_attempt = batch_id is None
        last_attempt = parsed(state["last_attempt_at"])
        last_success = parsed(state["last_success_at"])
        if not recovered:
            if batch_id is not None and last_attempt and now - last_attempt < timedelta(hours=retry_hours):
                return None
            if batch_id is None and last_success and now - last_success < timedelta(hours=period_hours):
                return None

        if first_attempt:
            batch_id = new_batch_id
            rows = conn.execute(
                "SELECT id FROM pokazaniya WHERE sent_to_1c=0 AND batch_id IS NULL "
                f"AND {_CUSTOMER_READING_SQL} AND created_at <= ? ORDER BY id LIMIT ?",
                (now.strftime("%Y-%m-%d %H:%M"), limit),
            ).fetchall()
            conn.executemany(
                "UPDATE pokazaniya SET batch_id=? WHERE id=?",
                [(batch_id, row["id"]) for row in rows],
            )
        conn.execute(
            "UPDATE sync_1c_state SET pending_batch_id=?, last_attempt_at=? WHERE id=1",
            (batch_id, now.isoformat(timespec="minutes")),
        )
        rows = conn.execute(
            # Assigned legacy baselines may already have reached 1C. Preserve
            # the complete original payload under that idempotency key.
            "SELECT * FROM pokazaniya WHERE batch_id=? ORDER BY id",
            (batch_id,),
        ).fetchall()
        conn.commit()
        return {
            "batch_id": batch_id, "rows": rows, "first_attempt": first_attempt,
            "attempt_at": now.isoformat(timespec="minutes"),
        }
    finally:
        conn.close()


def complete_1c_sync_batch(batch_id: str, attempt_at: str) -> bool:
    """Finish only the claimed attempt; a stale worker cannot clear newer work."""
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        state = conn.execute("SELECT * FROM sync_1c_state WHERE id=1").fetchone()
        if state["pending_batch_id"] != batch_id or state["last_attempt_at"] != attempt_at:
            return False
        more = conn.execute(
            "SELECT 1 FROM pokazaniya WHERE sent_to_1c=0 "
            f"AND (batch_id IS NOT NULL OR {_CUSTOMER_READING_SQL}) LIMIT 1"
        ).fetchone()
        conn.execute(
            "UPDATE sync_1c_state SET pending_batch_id=NULL, last_success_at=? WHERE id=1",
            (state["last_success_at"] if more else attempt_at,),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def get_unsent_1c_readings(
    limit: int,
    created_before: str | None = None,
) -> list[sqlite3.Row]:
    """Возвращает ещё не закреплённые за пакетом показания."""
    conn = get_conn()
    try:
        sql = (
            "SELECT * FROM pokazaniya "
            "WHERE sent_to_1c=0 AND batch_id IS NULL "
            f"AND {_CUSTOMER_READING_SQL} "
        )
        params: list = []
        if created_before is not None:
            sql += "AND created_at <= ? "
            params.append(created_before)
        sql += "ORDER BY id LIMIT ?"
        params.append(limit)
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def has_unsent_1c_readings() -> bool:
    conn = get_conn()
    try:
        row = conn.execute(
            # Orphan batches must also prevent the daily timer from starting.
            "SELECT 1 FROM pokazaniya WHERE sent_to_1c=0 "
            f"AND (batch_id IS NOT NULL OR {_CUSTOMER_READING_SQL}) LIMIT 1"
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def assign_readings_to_1c_batch(reading_ids: list[int], batch_id: str) -> None:
    """Atomically assign membership and its pending state (legacy callers)."""
    if not reading_ids:
        return
    placeholders = ",".join("?" for _ in reading_ids)
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT OR IGNORE INTO sync_1c_state (id) VALUES (1)")
        pending = conn.execute("SELECT pending_batch_id FROM sync_1c_state WHERE id=1").fetchone()[0]
        if pending is not None:
            raise ValueError("A 1C batch is already pending")
        conn.execute(
            f"UPDATE pokazaniya SET batch_id=? "
            f"WHERE id IN ({placeholders}) AND sent_to_1c=0 AND batch_id IS NULL "
            f"AND {_CUSTOMER_READING_SQL}",
            [batch_id, *reading_ids],
        )
        conn.execute("UPDATE sync_1c_state SET pending_batch_id=? WHERE id=1", (batch_id,))
        conn.commit()
    finally:
        conn.close()


def get_1c_readings_by_batch(batch_id: str) -> list[sqlite3.Row]:
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT * FROM pokazaniya WHERE batch_id=? ORDER BY id", (batch_id,)
        ).fetchall()
    finally:
        conn.close()


def _apply_1c_reading_statuses_conn(
    conn: sqlite3.Connection,
    batch_id: str,
    statuses: list[dict],
) -> list[dict]:
    updated: list[dict] = []
    for item in statuses:
        status = item.get("status")
        if status not in {"accepted", "rejected"}:
            raise ValueError(f"Неизвестный статус показания: {status!r}")
        row = conn.execute(
            """
            SELECT * FROM pokazaniya
            WHERE batch_id=? AND ls=? AND meter_number=? AND value1=?
              AND ((value2 IS NULL AND ? IS NULL) OR value2=?)
              AND created_at=? AND sent_to_1c=0
            ORDER BY id LIMIT 1
            """,
            (
                batch_id,
                item.get("ls"),
                item.get("meter_number"),
                str(item.get("value1")),
                item.get("value2"),
                None if item.get("value2") is None else str(item.get("value2")),
                item.get("submitted_at"),
            ),
        ).fetchone()
        if row is None:
            already_final = conn.execute(
                """
                SELECT 1 FROM pokazaniya
                WHERE batch_id=? AND ls=? AND meter_number=? AND value1=?
                  AND ((value2 IS NULL AND ? IS NULL) OR value2=?)
                  AND created_at=? AND sent_to_1c=1
                LIMIT 1
                """,
                (
                    batch_id,
                    item.get("ls"),
                    item.get("meter_number"),
                    str(item.get("value1")),
                    item.get("value2"),
                    None if item.get("value2") is None else str(item.get("value2")),
                    item.get("submitted_at"),
                ),
            ).fetchone()
            if already_final is not None:
                continue
            raise ValueError(
                f"Ответ 1С содержит неизвестное показание: batch_id={batch_id}"
            )
        conn.execute(
            "UPDATE pokazaniya SET sent_to_1c=1, status_1c=? WHERE id=?",
            (status, row["id"]),
        )
        result = dict(row)
        result["status_1c"] = status
        result["sent_to_1c"] = 1
        updated.append(result)
    return updated


def apply_1c_reading_statuses(batch_id: str, statuses: list[dict]) -> list[dict]:
    """Финализирует строки пакета и возвращает данные для уведомлений MAX."""
    conn = get_conn()
    try:
        updated = _apply_1c_reading_statuses_conn(conn, batch_id, statuses)
        conn.commit()
        return updated
    finally:
        conn.close()


def get_1c_sync_state() -> dict:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM sync_1c_state WHERE id=1").fetchone()
        if row is None:
            conn.execute("INSERT INTO sync_1c_state (id) VALUES (1)")
            conn.commit()
            row = conn.execute("SELECT * FROM sync_1c_state WHERE id=1").fetchone()
        return dict(row)
    finally:
        conn.close()


def update_1c_sync_state(**fields: str | None) -> None:
    allowed = {"last_success_at", "last_attempt_at", "pending_batch_id"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"Неизвестные поля sync_1c_state: {sorted(unknown)}")
    if not fields:
        return
    assignments = ", ".join(f"{name}=?" for name in fields)
    conn = get_conn()
    try:
        conn.execute("INSERT OR IGNORE INTO sync_1c_state (id) VALUES (1)")
        conn.execute(
            f"UPDATE sync_1c_state SET {assignments} WHERE id=1",
            list(fields.values()),
        )
        conn.commit()
    finally:
        conn.close()


# ── Лицевые счета и счётчики ──────────────────────────────────────────────────

_LS_NUMBER_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z", re.ASCII)
_LS_SEARCH_MAX_LENGTH = 100


def normalize_lschet_number(number: str) -> str:
    """Normalize and validate a user-facing account number."""
    normalized = number.strip()
    if not normalized:
        raise ValueError("Введите номер лицевого счёта")
    if _LS_NUMBER_RE.fullmatch(normalized) is None:
        raise ValueError(
            "Номер лицевого счёта должен содержать от 1 до 64 символов: "
            "латинские буквы, цифры, дефис или подчёркивание"
        )
    return normalized


def _normalize_optional_lschet_field(
    value: str | None,
    *,
    label: str,
    max_length: int,
) -> str | None:
    normalized = unicodedata.normalize("NFC", value.strip()) if value else ""
    if not normalized:
        return None
    if len(normalized) > max_length:
        raise ValueError(f"Поле «{label}» не должно превышать {max_length} символов")
    if any(unicodedata.category(char).startswith("C") for char in normalized):
        raise ValueError(f"Поле «{label}» содержит недопустимые символы")
    return normalized


def normalize_lschet_search(query: str | None) -> str:
    """Normalize an account-directory query and cap resource usage."""
    return (query or "").strip()[:_LS_SEARCH_MAX_LENGTH]


def _licschet_search(query: str | None) -> tuple[str, ...]:
    normalized = normalize_lschet_search(query)
    if not normalized:
        return ()
    escaped = (
        normalized.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    pattern = f"%{escaped}%"
    return (pattern, pattern, pattern)


def count_lschet(query: str | None = None) -> int:
    """Count accounts matching a literal administrative search query."""
    params = _licschet_search(query)
    conn = get_conn()
    try:
        if not params:
            row = conn.execute("SELECT COUNT(*) FROM licschet").fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) FROM licschet WHERE "
                "number LIKE ? ESCAPE '\\' OR fio LIKE ? ESCAPE '\\' "
                "OR address LIKE ? ESCAPE '\\'",
                params,
            ).fetchone()
        return int(row[0])
    finally:
        conn.close()


def list_lschet(
    query: str | None = None,
    *,
    limit: int = 50,
    offset: int = 0,
) -> list[sqlite3.Row]:
    """Return one bounded page of accounts for the administrative directory."""
    if limit < 1 or limit > 100:
        raise ValueError("Размер страницы должен быть от 1 до 100")
    if offset < 0:
        raise ValueError("Смещение не может быть отрицательным")
    params = _licschet_search(query)
    conn = get_conn()
    try:
        if not params:
            return conn.execute(
                "SELECT id, number, fio, address FROM licschet "
                "ORDER BY number LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return conn.execute(
            "SELECT id, number, fio, address FROM licschet WHERE "
            "number LIKE ? ESCAPE '\\' OR fio LIKE ? ESCAPE '\\' "
            "OR address LIKE ? ESCAPE '\\' ORDER BY number LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
    finally:
        conn.close()


def create_lschet(
    number: str,
    fio: str | None = None,
    address: str | None = None,
) -> bool:
    """Insert one account without overwriting an existing account."""
    normalized = normalize_lschet_number(number)
    normalized_fio = _normalize_optional_lschet_field(
        fio,
        label="ФИО",
        max_length=256,
    )
    normalized_address = _normalize_optional_lschet_field(
        address,
        label="Адрес",
        max_length=512,
    )
    conn = get_conn()
    try:
        conn.execute("BEGIN")
        conn.execute(
            "INSERT INTO licschet (number, fio, address) VALUES (?, ?, ?)",
            (normalized, normalized_fio, normalized_address),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        if (
            getattr(exc, "sqlite_errorname", None) == "SQLITE_CONSTRAINT_UNIQUE"
            and "licschet.number" in str(exc)
        ):
            return False
        raise
    finally:
        conn.close()

def get_ls(number: str) -> sqlite3.Row | None:
    normalized = normalize_lschet_number(number)
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT * FROM licschet WHERE number=?", (normalized,)
        ).fetchone()
    finally:
        conn.close()


def get_schetchiki(ls: str) -> list[sqlite3.Row]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM schetchiki WHERE ls=?", (ls,)).fetchall()
    conn.close()
    return rows


def _validate_1c_meter(meter: dict) -> None:
    if not meter.get("meter_number"):
        raise ValueError("В ответе 1С отсутствует meter_number")
    if meter.get("meter_type") not in {"Однотарифный", "Двухтарифный"}:
        raise ValueError(f"Неподдерживаемый meter_type: {meter.get('meter_type')!r}")


def _upsert_1c_meter_conn(conn: sqlite3.Connection, ls: str, meter: dict) -> None:
    """Обновляет существующий счётчик без удаления возможных старых дублей."""
    existing = conn.execute(
        "SELECT id FROM schetchiki WHERE ls=? AND meter_number=? ORDER BY id LIMIT 1",
        (ls, meter["meter_number"]),
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE schetchiki SET resource_type=?, meter_type=? WHERE id=?",
            (meter.get("resource_type"), meter["meter_type"], existing["id"]),
        )
    else:
        conn.execute(
            "INSERT INTO schetchiki "
            "(ls, resource_type, meter_number, meter_type, initial1, initial2) "
            "VALUES (?, ?, ?, ?, '0', '0')",
            (ls, meter.get("resource_type"), meter["meter_number"], meter["meter_type"]),
        )


def upsert_1c_meters(ls: str, meters: list[dict]) -> None:
    conn = get_conn()
    try:
        for meter in meters:
            _validate_1c_meter(meter)
            _upsert_1c_meter_conn(conn, ls, meter)
        conn.commit()
    finally:
        conn.close()


def _apply_1c_meter_changes_conn(
    conn: sqlite3.Connection,
    changes: list[dict],
) -> None:
    for change in changes:
        action = change.get("action")
        ls = change.get("ls")
        meter_number = change.get("meter_number")
        if not ls or not meter_number:
            raise ValueError("Изменение счётчика без ls или meter_number")
        if action == "removed":
            conn.execute(
                "DELETE FROM schetchiki WHERE ls=? AND meter_number=?",
                (ls, meter_number),
            )
        elif action == "changed":
            _validate_1c_meter(change)
            conn.execute(
                "UPDATE schetchiki SET resource_type=?, meter_type=? "
                "WHERE ls=? AND meter_number=?",
                (
                    change.get("resource_type"),
                    change.get("meter_type"),
                    ls,
                    meter_number,
                ),
            )
        elif action == "added":
            _validate_1c_meter(change)
            _upsert_1c_meter_conn(conn, ls, change)
        else:
            raise ValueError(f"Неизвестное действие счётчика: {action!r}")


def apply_1c_meter_changes(changes: list[dict]) -> None:
    conn = get_conn()
    try:
        _apply_1c_meter_changes_conn(conn, changes)
        conn.commit()
    finally:
        conn.close()


def apply_1c_sync_result(
    batch_id: str,
    attempt_at: str,
    statuses: list[dict],
    changes: list[dict],
) -> list[dict] | None:
    """Atomically apply a validated 1C result and finish its claimed batch."""
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        state = conn.execute("SELECT * FROM sync_1c_state WHERE id=1").fetchone()
        if (
            state is None
            or state["pending_batch_id"] != batch_id
            or state["last_attempt_at"] != attempt_at
        ):
            conn.rollback()
            return None

        updated = _apply_1c_reading_statuses_conn(conn, batch_id, statuses)
        _apply_1c_meter_changes_conn(conn, changes)
        more = conn.execute(
            "SELECT 1 FROM pokazaniya WHERE sent_to_1c=0 "
            f"AND (batch_id IS NOT NULL OR {_CUSTOMER_READING_SQL}) LIMIT 1"
        ).fetchone()
        conn.execute(
            "UPDATE sync_1c_state SET pending_batch_id=NULL, last_success_at=? WHERE id=1",
            (state["last_success_at"] if more else attempt_at,),
        )
        conn.commit()
        return updated
    finally:
        conn.close()


def import_from_excel(filepath: str = "Данные_по_ЛС.xlsx") -> None:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        message = "Для импорта Excel не установлен openpyxl"
        log.error(message)
        raise RuntimeError(message) from exc

    try:
        wb = load_workbook(filepath, read_only=True)
    except FileNotFoundError as exc:
        message = f"Файл не найден: {filepath}"
        log.error(message)
        raise FileNotFoundError(message) from exc

    accounts: list[tuple[str, str | None, str | None]] = []
    meters: list[tuple[str, str, str, str, str, str]] = []
    has_accounts_sheet = "ЛС и ФИО" in wb.sheetnames
    has_meters_sheet = "Счетчики" in wb.sheetnames
    try:
        if has_accounts_sheet:
            for row_number, row in enumerate(
                wb["ЛС и ФИО"].iter_rows(values_only=True), start=1
            ):
                if row_number == 1 or not row[0]:
                    continue
                try:
                    number = normalize_lschet_number(str(row[0]))
                except ValueError as exc:
                    raise ValueError(
                        "Некорректный лицевой счёт на листе «ЛС и ФИО», "
                        f"строка {row_number}: {exc}"
                    ) from exc
                fio = _normalize_optional_lschet_field(
                    str(row[1]) if row[1] else None,
                    label="ФИО",
                    max_length=256,
                )
                address = _normalize_optional_lschet_field(
                    str(row[2]) if row[2] else None,
                    label="Адрес",
                    max_length=512,
                )
                accounts.append((number, fio, address))

        if has_meters_sheet:
            for row_number, row in enumerate(
                wb["Счетчики"].iter_rows(values_only=True), start=1
            ):
                if row_number == 1 or not row[0]:
                    continue
                try:
                    ls = normalize_lschet_number(str(row[0]))
                except ValueError as exc:
                    raise ValueError(
                        "Некорректный лицевой счёт на листе «Счетчики», "
                        f"строка {row_number}: {exc}"
                    ) from exc
                meters.append(
                    (
                        ls,
                        str(row[1]).strip() if row[1] else "",
                        str(row[2]).strip() if row[2] else "",
                        str(row[3]).strip() if row[3] else "Однотарифный",
                        str(row[4]).strip() if row[4] else "0",
                        str(row[5]).strip() if row[5] else "0",
                    )
                )
    finally:
        close_workbook = getattr(wb, "close", None)
        if callable(close_workbook):
            close_workbook()

    conn = get_conn()
    initial_count = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        if has_accounts_sheet:
            conn.executemany(
                "INSERT OR REPLACE INTO licschet (number, fio, address) VALUES (?, ?, ?)",
                accounts,
            )

        if has_meters_sheet:
            conn.execute("DELETE FROM schetchiki")
            for ls, resource_type, meter_number, meter_type, initial1, initial2 in meters:
                conn.execute(
                    "INSERT INTO schetchiki "
                    "(ls, resource_type, meter_number, meter_type, initial1, initial2) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (ls, resource_type, meter_number, meter_type, initial1, initial2),
                )
                existing = conn.execute(
                    "SELECT id FROM pokazaniya WHERE ls=? AND meter_number=? LIMIT 1",
                    (ls, meter_number),
                ).fetchone()
                if not existing:
                    value2 = initial2 if meter_type == "Двухтарифный" and initial2 else None
                    conn.execute(
                        "INSERT INTO pokazaniya "
                        "(chat_id, ls, resource_type, meter_number, value1, value2, created_at, sent_to_1c) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                        (0, ls, resource_type, meter_number, initial1, value2,
                         "2000-01-01 00:00"),
                    )
                    initial_count += 1
        conn.commit()
    except Exception:  # noqa: BLE001 - transaction boundary must rollback all errors
        conn.rollback()
        raise
    finally:
        conn.close()

    if has_accounts_sheet:
        log.info("Импортировано лицевых счетов: %d", len(accounts))
    if has_meters_sheet:
        log.info("Импортировано счётчиков: %d", len(meters))
        log.info("Записано начальных показаний: %d", initial_count)


# ── Пользователи портала ──────────────────────────────────────────────────────

def get_user(username: str) -> sqlite3.Row | None:
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    conn.close()
    return row


def get_user_by_id(user_id: int) -> sqlite3.Row | None:
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    conn.close()
    return row


def get_all_users() -> list[sqlite3.Row]:
    conn = get_conn()
    rows = conn.execute("SELECT id, username, name, role FROM users ORDER BY id").fetchall()
    conn.close()
    return rows


def create_user(username: str, password: str, name: str, role: str) -> tuple[bool, str]:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO users (username, password, name, role) VALUES (?, ?, ?, ?)",
            (username, generate_password_hash(password), name, role),
        )
        conn.commit()
        log.info("Создан пользователь: %s  роль=%s", username, role)
        return True, "Пользователь создан"
    except Exception as exc:
        log.warning("Ошибка создания пользователя %s: %s", username, exc)
        return False, f"Ошибка: {exc}"
    finally:
        conn.close()


def delete_user(user_id: int) -> None:
    conn = get_conn()
    conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    conn.commit()
    conn.close()
    log.info("Пользователь id=%s удалён", user_id)


def change_password(user_id: int, new_password: str) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE users SET password=?, session_version=session_version+1, "
            "must_change_password=0 WHERE id=?",
            (generate_password_hash(new_password), user_id),
        )
        conn.commit()
        log.info("Пароль пользователя id=%s изменён", user_id)
    finally:
        conn.close()


def change_role(user_id: int, role: str) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE users SET role=?, session_version=session_version+1 WHERE id=?",
            (role, user_id),
        )
        conn.commit()
        log.info("Роль пользователя id=%s изменена на %s", user_id, role)
    finally:
        conn.close()


# ── Пользователи бота ─────────────────────────────────────────────────────────

def get_bot_user(chat_id: int) -> sqlite3.Row | None:
    """Возвращает запись bot_users по chat_id. O(1) vs get_all_bot_users()."""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM bot_users WHERE chat_id = ? LIMIT 1", (chat_id,)
    ).fetchone()
    conn.close()
    return row


def upsert_bot_user(
    chat_id: int,
    ls: str | None,
    fio: str | None,
    authorized_1c: bool | None = None,
    *,
    clear_fio: bool = False,
) -> None:
    conn = get_conn()
    try:
        auth_value = int(bool(authorized_1c)) if authorized_1c is not None else 0
        fio_value = None if clear_fio else fio
        conn.execute(
            "INSERT INTO bot_users (chat_id, ls, fio, last_seen, authorized_1c) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET "
            "ls=excluded.ls, "
            "fio=CASE WHEN ? THEN NULL "
            "WHEN excluded.fio IS NOT NULL AND excluded.fio<>'' "
            "THEN excluded.fio "
            "WHEN bot_users.ls IS NOT excluded.ls THEN NULL "
            "ELSE bot_users.fio END, "
            "last_seen=excluded.last_seen, "
            "authorized_1c=CASE WHEN ? IS NULL THEN bot_users.authorized_1c "
            "ELSE excluded.authorized_1c END",
            (
                chat_id,
                ls,
                fio_value,
                msk_now(),
                auth_value,
                clear_fio,
                authorized_1c,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_all_bot_users() -> list[sqlite3.Row]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM bot_users ORDER BY last_seen DESC").fetchall()
    conn.close()
    return rows


# ── Комментарии (архивный флоу → appeals_legacy) ─────────────────────────────

def add_comment(appeal_id: int, author: str, text: str) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO comments (appeal_id, author, text, created_at) VALUES (?, ?, ?, ?)",
            (appeal_id, author, text, msk_now()),
        )
        conn.commit()
    finally:
        conn.close()


def get_comments(appeal_id: int) -> list[sqlite3.Row]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM comments WHERE appeal_id=? ORDER BY created_at ASC", (appeal_id,)
    ).fetchall()
    conn.close()
    return rows


def get_last_comment(appeal_id: int) -> sqlite3.Row | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM comments WHERE appeal_id=? ORDER BY created_at DESC LIMIT 1",
        (appeal_id,),
    ).fetchone()
    conn.close()
    return row


# ── Счётчики для дашборда ─────────────────────────────────────────────────────

def get_counts() -> dict:
    """
    Счётчики для дашборда портала.
    appeals — новая схема (ticket_no); appeals_legacy — для совместимости
    пока Flask-портал не переведён на новую схему (Этап 5).
    """
    conn = get_conn()

    def _count(table: str, where: str, *params) -> int:
        try:
            return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()[0]
        except Exception:
            return 0

    result = {
        # Новая схема
        "new":                  _count("appeals", "status='new'"),
        "in_work":              _count("appeals", "status='in_work'"),
        "pending_confirmation": _count("appeals", "status='pending_confirmation'"),
        "resolved":             _count("appeals", "status='resolved'"),
        "closed":               _count("appeals", "status='closed'"),
        "pokazaniya":           _count("pokazaniya", "1=1"),
    }
    conn.close()
    return result


# ── Cleanup-функции для APScheduler (Этап 4) ──────────────────────────────────
#
# cleanup_user_states() — НЕ здесь.
# Функция оперирует dict user_states, объявленным в bot.py в том же процессе.
# Сигнатура: cleanup_user_states(user_states: dict, ttl_minutes: int = SESSION_TTL_MINUTES)
# Место: bot.py, рядом с объявлением user_states.


def cleanup_old_cache(days: int = 90) -> int:
    """
    Удаляет показания старше `days` дней.
    В режиме очереди удаляет только уже финализированные в 1С строки.
    Возвращает количество удалённых строк.
    """
    conn = get_conn()
    tz = timezone(timedelta(hours=TIMEZONE_OFFSET))
    cutoff = (datetime.now(tz) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
    cursor = conn.execute(
        "DELETE FROM pokazaniya WHERE created_at < ? "
        "AND created_at != '2000-01-01 00:00' AND sent_to_1c=1",
        (cutoff,),
    )
    deleted = cursor.rowcount
    conn.commit()
    conn.close()
    if deleted:
        log.info("cleanup_old_cache: удалено %d записей показаний старше %d дней", deleted, days)
    return deleted


# ── Модуль записи на приём ────────────────────────────────────────────────────

# -- Филиалы ------------------------------------------------------------------

def get_branches(active_only: bool = True) -> list[sqlite3.Row]:
    conn = get_conn()
    if active_only:
        rows = conn.execute(
            "SELECT * FROM branches WHERE is_active=1 ORDER BY name"
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM branches ORDER BY name").fetchall()
    conn.close()
    return rows


def get_branch(branch_id: int) -> sqlite3.Row | None:
    conn = get_conn()
    row = conn.execute("SELECT * FROM branches WHERE id=?", (branch_id,)).fetchone()
    conn.close()
    return row


def create_branch(name: str, address: str) -> int:
    conn = get_conn()
    row_id = conn.execute(
        "INSERT INTO branches (name, address) VALUES (?, ?)", (name, address)
    ).lastrowid
    conn.commit()
    conn.close()
    return row_id


def update_branch(branch_id: int, name: str, address: str, is_active: int) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE branches SET name=?, address=?, is_active=? WHERE id=?",
        (name, address, is_active, branch_id)
    )
    conn.commit()
    conn.close()


# -- Расписание ---------------------------------------------------------------

def get_branch_schedules(branch_id: int) -> list[sqlite3.Row]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM branch_schedules WHERE branch_id=? AND is_active=1 ORDER BY weekday, time_from",
        (branch_id,)
    ).fetchall()
    conn.close()
    return rows


def create_schedule(
    branch_id: int,
    weekday: int,
    time_from: str,
    time_to: str,
    slot_duration_min: int,
    capacity: int,
    booking_horizon_days: int,
) -> int:
    if not 0 <= weekday <= 6:
        raise ValueError("weekday должен быть от 0 до 6")
    if slot_duration_min <= 0:
        raise ValueError("Длительность слота должна быть больше нуля")
    if capacity <= 0:
        raise ValueError("Вместимость должна быть больше нуля")
    if booking_horizon_days < 0:
        raise ValueError("Горизонт записи не может быть отрицательным")
    start = datetime.strptime(time_from, "%H:%M")
    end = datetime.strptime(time_to, "%H:%M")
    if start >= end:
        raise ValueError("Время окончания должно быть позже времени начала")
    conn = get_conn()
    row_id = conn.execute(
        """INSERT INTO branch_schedules
           (branch_id, weekday, time_from, time_to, slot_duration_min, capacity, booking_horizon_days)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (branch_id, weekday, time_from, time_to, slot_duration_min, capacity, booking_horizon_days)
    ).lastrowid
    conn.commit()
    conn.close()
    return row_id


def delete_schedule(schedule_id: int) -> None:
    conn = get_conn()
    conn.execute("UPDATE branch_schedules SET is_active=0 WHERE id=?", (schedule_id,))
    conn.commit()
    conn.close()


# -- Исключения из расписания -------------------------------------------------

def get_exceptions(branch_id: int) -> list[sqlite3.Row]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM schedule_exceptions WHERE branch_id=? ORDER BY date",
        (branch_id,)
    ).fetchall()
    conn.close()
    return rows


def add_exception(branch_id: int, date: str, reason: str | None = None) -> None:
    """Добавляет день-исключение (INSERT OR IGNORE — дубли игнорируются)."""
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO schedule_exceptions (branch_id, date, reason) VALUES (?, ?, ?)",
        (branch_id, date, reason)
    )
    conn.commit()
    conn.close()


def remove_exception(branch_id: int, date: str) -> None:
    conn = get_conn()
    conn.execute(
        "DELETE FROM schedule_exceptions WHERE branch_id=? AND date=?",
        (branch_id, date)
    )
    conn.commit()
    conn.close()


def is_exception_day(branch_id: int, date: str) -> bool:
    """True если дата является исключением (филиал не работает)."""
    conn = get_conn()
    row = conn.execute(
        "SELECT 1 FROM schedule_exceptions WHERE branch_id=? AND date=? LIMIT 1",
        (branch_id, date)
    ).fetchone()
    conn.close()
    return row is not None


# -- Слоты --------------------------------------------------------------------

def _matching_schedule(schedules, slot_time: str):
    """Детерминированно выбирает расписание для слота при пересечениях.

    Приоритет имеет интервал с более поздним началом, затем более новая запись.
    То же правило используется и при показе, и при подтверждении слота.
    """
    requested = datetime.strptime(slot_time, "%H:%M")
    for schedule in sorted(
        schedules,
        key=lambda item: (item["time_from"], item["id"]),
        reverse=True,
    ):
        duration = schedule["slot_duration_min"]
        capacity = schedule["capacity"]
        if duration <= 0 or capacity <= 0:
            continue
        start = datetime.strptime(schedule["time_from"], "%H:%M")
        end = datetime.strptime(schedule["time_to"], "%H:%M")
        offset_minutes = int((requested - start).total_seconds() // 60)
        if (
            requested >= start
            and requested + timedelta(minutes=duration) <= end
            and offset_minutes % duration == 0
        ):
            return schedule
    return None

def get_available_slots(branch_id: int, date: str) -> list[str]:
    """
    Возвращает список доступных слотов ('HH:MM') для филиала на дату.
    Слот доступен если:
      - дата не является исключением
      - weekday совпадает с расписанием
      - время слота ещё не наступило (REQ-АВТ-07-04)
      - количество активных записей на слот < capacity

    Генерация слотов: от time_from до time_to с шагом slot_duration_min.
    Неполный хвостовой слот не создаётся (REQ-АВТ-07-02).
    """
    if is_exception_day(branch_id, date):
        return []

    parsed_date = datetime.strptime(date, "%Y-%m-%d").date()
    weekday = parsed_date.weekday()  # 0=Пн … 6=Вс

    schedules = get_branch_schedules(branch_id)
    day_schedules = [s for s in schedules if s["weekday"] == weekday]
    if not day_schedules:
        return []

    now = datetime.now()
    candidate_slots: set[str] = set()

    for sched in day_schedules:
        if sched["slot_duration_min"] <= 0 or sched["capacity"] <= 0:
            log.error("Некорректное расписание id=%s пропущено", sched["id"])
            continue
        t_from = datetime.strptime(f"{date} {sched['time_from']}", "%Y-%m-%d %H:%M")
        t_to   = datetime.strptime(f"{date} {sched['time_to']}",   "%Y-%m-%d %H:%M")
        step   = timedelta(minutes=sched["slot_duration_min"])
        current = t_from
        while current + step <= t_to:
            # Не предлагаем прошедшие слоты
            if current > now:
                candidate_slots.add(current.strftime("%H:%M"))
            current += step

    slots = []
    for slot_time in sorted(candidate_slots):
        schedule = _matching_schedule(day_schedules, slot_time)
        if schedule is None:
            continue
        if parsed_date > now.date() + timedelta(days=schedule["booking_horizon_days"]):
            continue
        if _count_booked(branch_id, date, slot_time) < schedule["capacity"]:
            slots.append(slot_time)
    return slots


def _count_booked(branch_id: int, date: str, slot_time: str) -> int:
    """Количество активных записей на конкретный слот."""
    conn = get_conn()
    count = conn.execute(
        "SELECT COUNT(*) FROM appointments WHERE branch_id=? AND slot_date=? AND slot_time=? AND status='active'",
        (branch_id, date, slot_time)
    ).fetchone()[0]
    conn.close()
    return count


def get_available_dates(branch_id: int) -> list[str]:
    """
    Возвращает список дат ('YYYY-MM-DD') доступных для записи.
    Горизонт берётся из максимального booking_horizon_days среди активных расписаний филиала.
    """
    schedules = get_branch_schedules(branch_id)
    if not schedules:
        return []

    horizon = max(s["booking_horizon_days"] for s in schedules)
    today = datetime.now().date()
    dates = []

    for i in range(horizon + 1):
        d = today + timedelta(days=i)
        date_str = d.strftime("%Y-%m-%d")
        if not is_exception_day(branch_id, date_str):
            weekday = d.weekday()
            if any(s["weekday"] == weekday for s in schedules):
                # Проверяем что есть хотя бы один доступный слот
                if get_available_slots(branch_id, date_str):
                    dates.append(date_str)

    return dates


# -- Записи на приём ----------------------------------------------------------

def get_active_appointment(ls: str) -> sqlite3.Row | None:
    """Активная запись клиента (не более одной — уникальный индекс). REQ-АВТ-07-11."""
    conn = get_conn()
    row = conn.execute(
        "SELECT a.*, b.name AS branch_name, b.address AS branch_address "
        "FROM appointments a JOIN branches b ON b.id = a.branch_id "
        "WHERE a.ls=? AND a.status='active' LIMIT 1",
        (ls,)
    ).fetchone()
    conn.close()
    return row


def create_appointment(
    ls: str,
    branch_id: int,
    slot_date: str,
    slot_time: str,
    channel: str,
    chat_id: int,
    theme: str | None = None,
) -> tuple[int | None, str | None]:
    """
    Создаёт запись на приём.
    Проверяет:
      - нет активной записи на этот ЛС (REQ-АВТ-07-11)
      - слот не переполнен (REQ-АВТ-07-03)
    Возвращает (id, None) при успехе или (None, error_msg) при ошибке.
    Использует транзакцию для защиты от race condition.
    """
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            parsed_date = datetime.strptime(slot_date, "%Y-%m-%d").date()
            parsed_time = datetime.strptime(slot_time, "%H:%M")
        except ValueError:
            return None, "Некорректная дата или время слота"

        slot_dt = datetime.combine(parsed_date, parsed_time.time())
        if slot_dt <= datetime.now():
            return None, "Нельзя записаться на прошедший слот"

        branch = conn.execute(
            "SELECT is_active FROM branches WHERE id=?", (branch_id,)
        ).fetchone()
        if not branch or not branch["is_active"]:
            return None, "Филиал недоступен для записи"

        exception = conn.execute(
            "SELECT 1 FROM schedule_exceptions WHERE branch_id=? AND date=?",
            (branch_id, slot_date),
        ).fetchone()
        if exception:
            return None, "Филиал не работает в выбранную дату"

        # Проверка активной записи на ЛС
        existing = conn.execute(
            "SELECT id FROM appointments WHERE ls=? AND status='active' LIMIT 1",
            (ls,)
        ).fetchone()
        if existing:
            return None, "У вас уже есть активная запись на приём"

        # Выбираем именно то расписание дня и интервала, которое порождает слот.
        schedules = conn.execute(
            "SELECT * FROM branch_schedules WHERE branch_id=? AND weekday=? "
            "AND is_active=1 ORDER BY time_from DESC, id DESC",
            (branch_id, parsed_date.weekday()),
        ).fetchall()
        matched = _matching_schedule(schedules, slot_time)
        if matched is None:
            return None, "Выбранный слот больше недоступен"
        if parsed_date > datetime.now().date() + timedelta(
            days=matched["booking_horizon_days"]
        ):
            return None, "Дата находится за пределами горизонта записи"
        capacity = matched["capacity"]

        booked = conn.execute(
            "SELECT COUNT(*) FROM appointments WHERE branch_id=? AND slot_date=? AND slot_time=? AND status='active'",
            (branch_id, slot_date, slot_time)
        ).fetchone()[0]

        if booked >= capacity:
            return None, "Этот слот уже занят, выберите другое время"

        row_id = conn.execute(
            """INSERT INTO appointments
               (ls, branch_id, slot_date, slot_time, theme, channel, chat_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (ls, branch_id, slot_date, slot_time, theme, channel, chat_id, msk_now())
        ).lastrowid
        conn.commit()
        log.info("Запись создана: id=%s  ls=%s  %s %s", row_id, ls, slot_date, slot_time)
        return row_id, None

    except sqlite3.IntegrityError as exc:
        log.info("create_appointment конфликт: %s", exc)
        return None, "У вас уже есть активная запись или слот недоступен"
    except Exception as exc:
        log.error("create_appointment ошибка: %s", exc)
        return None, f"Ошибка: {exc}"
    finally:
        conn.close()


def cancel_appointment(
    appointment_id: int,
    cancelled_by: str,
    reason: str | None = None,
) -> None:
    """Отменяет запись. cancelled_by: 'client' | 'operator' | 'system'."""
    conn = get_conn()
    conn.execute(
        """UPDATE appointments
           SET status='cancelled', cancelled_at=?, cancelled_by=?, cancel_reason=?
           WHERE id=?""",
        (msk_now(), cancelled_by, reason, appointment_id)
    )
    conn.commit()
    conn.close()
    log.info("Запись отменена: id=%s  by=%s", appointment_id, cancelled_by)


def mark_appointment(appointment_id: int, status: str) -> None:
    """Ставит отметку явки: status = 'visited' | 'no_show'. REQ-СОТ-05-03."""
    conn = get_conn()
    conn.execute(
        "UPDATE appointments SET status=? WHERE id=?",
        (status, appointment_id)
    )
    conn.commit()
    conn.close()


def get_appointments(
    branch_id: int | None = None,
    date: str | None = None,
    status: str | None = None,
) -> list[sqlite3.Row]:
    """Список записей для операторского портала (REQ-СОТ-05-01)."""
    query = """
        SELECT a.*, b.name AS branch_name, b.address AS branch_address
        FROM appointments a
        JOIN branches b ON b.id = a.branch_id
        WHERE 1=1
    """
    params = []
    if branch_id:
        query += " AND a.branch_id=?"
        params.append(branch_id)
    if date:
        query += " AND a.slot_date=?"
        params.append(date)
    if status:
        query += " AND a.status=?"
        params.append(status)
    query += " ORDER BY a.slot_date, a.slot_time"

    conn = get_conn()
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return rows


def get_appointment(appointment_id: int) -> sqlite3.Row | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT a.*, b.name AS branch_name, b.address AS branch_address "
        "FROM appointments a JOIN branches b ON b.id = a.branch_id WHERE a.id=?",
        (appointment_id,)
    ).fetchone()
    conn.close()
    return row


# -- Напоминания (для APScheduler) --------------------------------------------

MOSCOW_TIMEZONE = ZoneInfo("Europe/Moscow")


def _as_moscow_time(current_time: datetime | None = None) -> datetime:
    """Return an aware Moscow datetime while accepting legacy no-arg calls."""
    if current_time is None:
        return datetime.now(MOSCOW_TIMEZONE)
    if current_time.tzinfo is None:
        return current_time.replace(tzinfo=MOSCOW_TIMEZONE)
    return current_time.astimezone(MOSCOW_TIMEZONE)


def get_appointments_for_reminder_24h(
    current_time: datetime | None = None,
) -> list[sqlite3.Row]:
    """
    Возвращает активные записи для напоминания за 24ч (REQ-АВТ-07-06).

    Условия:
      - slot_date+slot_time попадает в окно 23–25 часов от текущего момента
      - reminded_24h=0 (напоминание ещё не отправлялось)
      - created_at <= slot_datetime - 24h, т.е. запись сделана не менее
        чем за 24ч до приёма (REQ-АВТ-07-08). Без этого условия клиент,
        записавшийся на завтра прямо сейчас, получил бы "напоминание за 24ч"
        почти сразу после подтверждения записи — бессмысленно и раздражает.
    """
    now = _as_moscow_time(current_time)
    window_from = (now + timedelta(hours=23)).strftime("%Y-%m-%d %H:%M")
    window_to   = (now + timedelta(hours=25)).strftime("%Y-%m-%d %H:%M")

    conn = get_conn()
    rows = conn.execute(
        """SELECT a.*, b.name AS branch_name, b.address AS branch_address
           FROM appointments a JOIN branches b ON b.id = a.branch_id
           WHERE a.status='active' AND a.reminded_24h=0
             AND (a.slot_date || ' ' || a.slot_time) BETWEEN ? AND ?
             AND a.created_at <= datetime(a.slot_date || ' ' || a.slot_time, '-24 hours')""",
        (window_from, window_to)
    ).fetchall()
    conn.close()
    return rows


def get_appointments_for_reminder_day(
    current_time: datetime | None = None,
) -> list[sqlite3.Row]:
    """
    Возвращает активные записи для напоминания в день приёма в 09:00 (REQ-АВТ-07-07).
    Условие: slot_date = сегодня, слот ещё не наступил И reminded_day=0.
    Задача APScheduler запускается ровно в 09:00 по московскому времени.
    Прошедшие и текущие слоты исключаются самим запросом (REQ-АВТ-07-09).
    """
    now = _as_moscow_time(current_time)
    today = now.strftime("%Y-%m-%d")
    current_slot = now.strftime("%Y-%m-%d %H:%M")

    conn = get_conn()
    rows = conn.execute(
        """SELECT a.*, b.name AS branch_name, b.address AS branch_address
           FROM appointments a JOIN branches b ON b.id = a.branch_id
           WHERE a.status='active' AND a.reminded_day=0 AND a.slot_date=?
             AND (a.slot_date || ' ' || a.slot_time) > ?""",
        (today, current_slot)
    ).fetchall()
    conn.close()
    return rows


def mark_reminded(appointment_id: int, reminder_type: str) -> None:
    """reminder_type: '24h' | 'day'"""
    conn = get_conn()
    col = "reminded_24h" if reminder_type == "24h" else "reminded_day"
    conn.execute(f"UPDATE appointments SET {col}=1 WHERE id=?", (appointment_id,))
    conn.commit()
    conn.close()


# ── Сценарии мониторинга домовых чатов (портал: раздел «Сценарии») ────────────

def create_scenario(
    title: str,
    keywords: list[str],
    response_text: str,
    suggest_appeal: bool,
) -> int:
    conn = get_conn()
    try:
        row_id = conn.execute(
            "INSERT INTO chat_scenarios (title, keywords, response_text, suggest_appeal, is_active) "
            "VALUES (?, ?, ?, ?, 1)",
            (title, json.dumps(keywords, ensure_ascii=False), response_text, int(suggest_appeal)),
        ).lastrowid
        conn.commit()
        return row_id
    finally:
        conn.close()


def get_all_scenarios() -> list[sqlite3.Row]:
    """Все сценарии для портала (включая неактивные — админ должен их видеть)."""
    conn = get_conn()
    rows = conn.execute("SELECT * FROM chat_scenarios ORDER BY title").fetchall()
    conn.close()
    return rows


def get_scenario(scenario_id: int) -> sqlite3.Row | None:
    conn = get_conn()
    row = conn.execute("SELECT * FROM chat_scenarios WHERE id=?", (scenario_id,)).fetchone()
    conn.close()
    return row


def update_scenario(
    scenario_id: int,
    title: str,
    keywords: list[str],
    response_text: str,
    suggest_appeal: bool,
    is_active: bool,
) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE chat_scenarios SET title=?, keywords=?, response_text=?, suggest_appeal=?, is_active=? "
        "WHERE id=?",
        (title, json.dumps(keywords, ensure_ascii=False), response_text,
         int(suggest_appeal), int(is_active), scenario_id),
    )
    conn.commit()
    conn.close()


def delete_scenario(scenario_id: int) -> None:
    """Удаляет сценарий полностью, включая все привязки к чатам (раздел 'как это работает' в UI)."""
    conn = get_conn()
    conn.execute("DELETE FROM chat_scenario_links WHERE scenario_id=?", (scenario_id,))
    conn.execute("DELETE FROM chat_scenarios WHERE id=?", (scenario_id,))
    conn.commit()
    conn.close()


def link_scenario_to_chat(scenario_id: int, house_chat_id: int) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO chat_scenario_links (scenario_id, house_chat_id) VALUES (?, ?)",
        (scenario_id, house_chat_id),
    )
    conn.commit()
    conn.close()


def unlink_scenario_from_chat(scenario_id: int, house_chat_id: int) -> None:
    conn = get_conn()
    conn.execute(
        "DELETE FROM chat_scenario_links WHERE scenario_id=? AND house_chat_id=?",
        (scenario_id, house_chat_id),
    )
    conn.commit()
    conn.close()


def get_linked_scenarios_full(house_chat_id: int) -> list[sqlite3.Row]:
    """
    Сценарии привязанные к дому — для карточки в портале (в отличие от
    get_scenarios_for_chat, которая фильтрует is_active=1 для бота,
    здесь показываем ВСЕ привязанные, включая выключенные, чтобы админ
    видел полную картину и мог отвязать даже неактивный сценарий).
    """
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT cs.*
        FROM chat_scenarios cs
        JOIN chat_scenario_links csl ON csl.scenario_id = cs.id
        WHERE csl.house_chat_id = ?
        ORDER BY cs.title
        """,
        (house_chat_id,),
    ).fetchall()
    conn.close()
    return rows


def get_unlinked_scenarios_for_chat(house_chat_id: int) -> list[sqlite3.Row]:
    """Активные сценарии ещё НЕ привязанные к этому дому — для формы привязки."""
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT * FROM chat_scenarios
        WHERE is_active = 1
          AND id NOT IN (
              SELECT scenario_id FROM chat_scenario_links WHERE house_chat_id = ?
          )
        ORDER BY title
        """,
        (house_chat_id,),
    ).fetchall()
    conn.close()
    return rows


# ── Скрипты FAQ — редактирование (портал: раздел «FAQ-скрипты») ──────────────
# Чтение для бота (get_active_scripts, get_script_tree) уже реализовано выше.

def create_script(title: str, sort_order: int = 0) -> int:
    conn = get_conn()
    try:
        row_id = conn.execute(
            "INSERT INTO scripts (title, sort_order, is_active) VALUES (?, ?, 1)",
            (title, sort_order),
        ).lastrowid
        conn.commit()
        return row_id
    finally:
        conn.close()


def get_all_scripts() -> list[dict]:
    """Все скрипты для портала (не только активные), с количеством узлов."""
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT s.*, COUNT(n.id) AS node_count
        FROM scripts s
        LEFT JOIN script_nodes n ON n.script_id = s.id
        GROUP BY s.id
        ORDER BY s.sort_order, s.id
        """
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_script(script_id: int) -> sqlite3.Row | None:
    conn = get_conn()
    row = conn.execute("SELECT * FROM scripts WHERE id=?", (script_id,)).fetchone()
    conn.close()
    return row


def update_script(script_id: int, title: str, sort_order: int, is_active: bool) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE scripts SET title=?, sort_order=?, is_active=? WHERE id=?",
        (title, sort_order, int(is_active), script_id),
    )
    conn.commit()
    conn.close()


def delete_script(script_id: int) -> None:
    """Удаляет скрипт целиком: рёбра → узлы → сам скрипт (порядок важен из-за FK)."""
    conn = get_conn()
    conn.execute(
        "DELETE FROM script_edges WHERE script_id=?", (script_id,)
    )
    conn.execute(
        "DELETE FROM script_nodes WHERE script_id=?", (script_id,)
    )
    conn.execute("DELETE FROM scripts WHERE id=?", (script_id,))
    conn.commit()
    conn.close()


def get_script_nodes(script_id: int) -> list[sqlite3.Row]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM script_nodes WHERE script_id=? ORDER BY id", (script_id,)
    ).fetchall()
    conn.close()
    return rows


def get_script_edges(script_id: int) -> list[sqlite3.Row]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM script_edges WHERE script_id=? ORDER BY id", (script_id,)
    ).fetchall()
    conn.close()
    return rows


def add_script_node(script_id: int, title: str, is_terminal: bool = False) -> int:
    conn = get_conn()
    try:
        row_id = conn.execute(
            "INSERT INTO script_nodes (script_id, title, is_terminal) VALUES (?, ?, ?)",
            (script_id, title, int(is_terminal)),
        ).lastrowid
        conn.commit()
        return row_id
    finally:
        conn.close()


def update_script_node(node_id: int, title: str, is_terminal: bool) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE script_nodes SET title=?, is_terminal=? WHERE id=?",
        (title, int(is_terminal), node_id),
    )
    conn.commit()
    conn.close()


def delete_script_node(node_id: int) -> None:
    """Удаляет узел вместе со всеми рёбрами где он источник или назначение."""
    conn = get_conn()
    conn.execute(
        "DELETE FROM script_edges WHERE from_node_id=? OR to_node_id=?",
        (node_id, node_id),
    )
    conn.execute("DELETE FROM script_nodes WHERE id=?", (node_id,))
    conn.commit()
    conn.close()


def _edge_creates_cycle(edges: list[sqlite3.Row], from_node_id: int, to_node_id: int) -> bool:
    """
    DFS-проверка: добавление ребра from→to создаёт цикл тогда и только тогда,
    когда уже существует путь to → ... → from в текущем графе.
    (TODO Этап 1/10 — валидация циклов графа скрипта, реализовано здесь.)
    """
    if from_node_id == to_node_id:
        return True  # петля — тоже цикл

    adjacency: dict[int, list[int]] = {}
    for e in edges:
        adjacency.setdefault(e["from_node_id"], []).append(e["to_node_id"])

    visited: set[int] = set()

    def has_path(start: int, target: int) -> bool:
        if start == target:
            return True
        visited.add(start)
        for nxt in adjacency.get(start, []):
            if nxt not in visited and has_path(nxt, target):
                return True
        return False

    return has_path(to_node_id, from_node_id)


def add_script_edge(
    script_id: int,
    from_node_id: int,
    label: str,
    to_node_id: int,
) -> tuple[int | None, str | None]:
    """
    Добавляет переход между узлами скрипта с DFS-проверкой циклов.
    Возвращает (edge_id, None) при успехе или (None, сообщение_об_ошибке).
    """
    conn = get_conn()
    try:
        existing = conn.execute(
            "SELECT from_node_id, to_node_id FROM script_edges WHERE script_id=?",
            (script_id,),
        ).fetchall()

        if _edge_creates_cycle(existing, from_node_id, to_node_id):
            return None, "Такой переход создаст цикл в графе скрипта"

        edge_id = conn.execute(
            "INSERT INTO script_edges (script_id, from_node_id, label, to_node_id) VALUES (?, ?, ?, ?)",
            (script_id, from_node_id, label, to_node_id),
        ).lastrowid
        conn.commit()
        return edge_id, None
    finally:
        conn.close()


def delete_script_edge(edge_id: int) -> None:
    conn = get_conn()
    conn.execute("DELETE FROM script_edges WHERE id=?", (edge_id,))
    conn.commit()
    conn.close()


# ── ИИ-помощник: настройки и однодневные сессии ────────────────────────────────────

def get_ai_settings() -> dict:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM ai_settings WHERE id=1").fetchone()
        if row is None:
            raise RuntimeError("ai_settings is not initialized")
        result = dict(row)
        result["enabled"] = bool(result["enabled"])
        return result
    finally:
        conn.close()


def update_ai_settings(
    *,
    enabled: bool,
    model: str,
    system_prompt: str,
    daily_limit: int,
    temperature: float,
    max_output_tokens: int,
) -> None:
    model = model.strip()
    system_prompt = system_prompt.strip()
    if not model or len(model) > 256:
        raise ValueError("Модель должна содержать от 1 до 256 символов")
    if not system_prompt or len(system_prompt) > 8000:
        raise ValueError("Системный промпт должен содержать от 1 до 8000 символов")
    if not 1 <= daily_limit <= 100:
        raise ValueError("Дневной лимит должен быть от 1 до 100")
    if not 0 <= temperature <= 1:
        raise ValueError("Температура должна быть от 0 до 1")
    if not 1 <= max_output_tokens <= 8000:
        raise ValueError("Максимум токенов должен быть от 1 до 8000")
    conn = get_conn()
    try:
        conn.execute(
            """UPDATE ai_settings
               SET enabled=?, model=?, system_prompt=?, daily_limit=?,
                   temperature=?, max_output_tokens=? WHERE id=1""",
            (int(enabled), model, system_prompt, daily_limit, temperature, max_output_tokens),
        )
        conn.commit()
    finally:
        conn.close()


def _server_date() -> str:
    return datetime.now().astimezone().date().isoformat()


def _ensure_ai_session_locked(
    conn: sqlite3.Connection,
    chat_id: int,
    session_date: str,
    updated_at: str,
) -> sqlite3.Row:
    """Create or roll over a session inside the caller's write transaction."""
    row = conn.execute(
        "SELECT * FROM ai_daily_sessions WHERE chat_id=?", (chat_id,)
    ).fetchone()
    if row is None:
        conn.execute(
            """INSERT INTO ai_daily_sessions
               (chat_id, session_date, question_count, history_json, faq_context, updated_at)
               VALUES (?, ?, 0, '[]', NULL, ?)""",
            (chat_id, session_date, updated_at),
        )
    elif row["session_date"] != session_date:
        conn.execute(
            """UPDATE ai_daily_sessions
               SET session_date=?, question_count=0, history_json='[]',
                   faq_context=NULL, updated_at=? WHERE chat_id=?""",
            (session_date, updated_at, chat_id),
        )
    return conn.execute(
        "SELECT * FROM ai_daily_sessions WHERE chat_id=?", (chat_id,)
    ).fetchone()


def get_ai_session(chat_id: int, *, session_date: str | None = None) -> dict:
    """Return today's sanitized session, replacing any expired daily data."""
    today = session_date or _server_date()
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _ensure_ai_session_locked(conn, chat_id, today, now)
        conn.commit()
        result = dict(row)
        try:
            result["history"] = json.loads(result.pop("history_json"))
        except (TypeError, ValueError):
            result["history"] = []
        return result
    finally:
        conn.close()


def set_ai_context(chat_id: int, context: str | None, *, session_date: str | None = None) -> None:
    today = session_date or _server_date()
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _ensure_ai_session_locked(conn, chat_id, today, now)
        conn.execute(
            "UPDATE ai_daily_sessions SET faq_context=?, updated_at=? WHERE chat_id=?",
            (context or None, now, chat_id),
        )
        conn.commit()
    finally:
        conn.close()


def reserve_ai_question(
    chat_id: int,
    daily_limit: int,
    *,
    session_date: str | None = None,
) -> bool:
    """Atomically reserve one request from the shared daily allowance."""
    today = session_date or _server_date()
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _ensure_ai_session_locked(conn, chat_id, today, now)
        cursor = conn.execute(
            """UPDATE ai_daily_sessions
               SET question_count=question_count+1, updated_at=?
               WHERE chat_id=? AND session_date=? AND question_count < ?""",
            (now, chat_id, today, daily_limit),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def release_ai_question(chat_id: int, *, session_date: str | None = None) -> None:
    today = session_date or _server_date()
    conn = get_conn()
    try:
        conn.execute(
            """UPDATE ai_daily_sessions
               SET question_count=MAX(question_count-1, 0), updated_at=?
               WHERE chat_id=? AND session_date=?""",
            (datetime.now().astimezone().isoformat(timespec="seconds"), chat_id, today),
        )
        conn.commit()
    finally:
        conn.close()


def append_ai_exchange(
    chat_id: int,
    question: str,
    answer: str,
    *,
    session_date: str | None = None,
    max_messages: int = 20,
) -> None:
    today = session_date or _server_date()
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _ensure_ai_session_locked(conn, chat_id, today, now)
        try:
            history = json.loads(row["history_json"])
            if not isinstance(history, list):
                history = []
        except (TypeError, ValueError):
            history = []
        history.extend(
            ({"role": "user", "text": question}, {"role": "assistant", "text": answer})
        )
        history = history[-max_messages:]
        conn.execute(
            "UPDATE ai_daily_sessions SET history_json=?, updated_at=? WHERE chat_id=?",
            (
                json.dumps(history, ensure_ascii=False),
                now,
                chat_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def clear_ai_history(chat_id: int, *, session_date: str | None = None) -> None:
    """Clear context without resetting today's used-question counter."""
    today = session_date or _server_date()
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _ensure_ai_session_locked(conn, chat_id, today, now)
        conn.execute(
            """UPDATE ai_daily_sessions SET history_json='[]', faq_context=NULL, updated_at=?
               WHERE chat_id=?""",
            (now, chat_id),
        )
        conn.commit()
    finally:
        conn.close()


def cleanup_expired_ai_sessions(*, session_date: str | None = None) -> int:
    """Delete all temporary AI sessions from earlier server-local dates."""
    today = session_date or _server_date()
    conn = get_conn()
    try:
        cursor = conn.execute(
            "DELETE FROM ai_daily_sessions WHERE session_date <> ?", (today,)
        )
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()
