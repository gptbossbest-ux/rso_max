"""
api/routers/appeals.py — эндпоинты обращений.

POST   /api/v1/appeals                    — создать обращение
GET    /api/v1/appeals/{ticket_no}        — статус + последний ответ
GET    /api/v1/appeals                    — список с фильтрами (оператор)
PATCH  /api/v1/appeals/{appeal_id}/status — сменить статус, уведомить клиента
POST   /api/v1/appeals/{appeal_id}/respond — ответить клиенту

Аутентификация: все маршруты защищены Bearer-токеном (deps.verify_token).
Логика маркерных слов: POST /appeals принудительно выставляет category=авария
  и priority=high при обнаружении слов из config.EMERGENCY_KEYWORDS в теле.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status

import database as db
from api.deps import verify_token
from api.notifier import (
    build_response_notification,
    build_status_notification,
    notify_client,
)
from api.schemas import (
    AppealCreate,
    AppealCreateOut,
    AppealConfirmIn,
    AppealConfirmOut,
    AppealListItem,
    AppealListOut,
    AppealRespondIn,
    AppealRespondOut,
    AppealStatusOut,
    AppealStatusUpdate,
    AppealStatusUpdateOut,
)
from config import EMERGENCY_KEYWORDS

log = logging.getLogger("rso.api.appeals")

router = APIRouter(
    prefix="/appeals",
    tags=["appeals"],
    dependencies=[Depends(verify_token)],
)


# ── Вспомогательные функции ───────────────────────────────────────────────────

def _detect_emergency(text: str) -> bool:
    """True если текст содержит хотя бы одно маркерное слово аварии."""
    lower = text.lower()
    return any(kw in lower for kw in EMERGENCY_KEYWORDS)


def _row_to_list_item(row) -> AppealListItem:
    r = dict(row)
    return AppealListItem(
        id=r["id"],
        ticket_no=r["ticket_no"],
        status=r["status"],
        category=r["category"],
        priority=r["priority"],
        channel=r["channel"],
        ls=r.get("ls"),
        body=r.get("body"),
        created_at=r.get("created_at"),
        updated_at=r.get("updated_at"),   # None если колонка отсутствует в старой схеме
        closed_at=r.get("closed_at"),
    )


# ── POST /api/v1/appeals ──────────────────────────────────────────────────────

@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=AppealCreateOut,
    summary="Создать обращение",
    description=(
        "Принимает обращение от бота, ЛК или виджета. "
        "Автоматически повышает priority='high' и category='авария' "
        "при обнаружении маркерных слов в теле."
    ),
)
async def create_appeal(payload: AppealCreate) -> AppealCreateOut:
    category = payload.category
    priority = "normal"

    # Автоматическая маршрутизация аварий (раздел 5.1 ТЗ)
    if _detect_emergency(payload.body):
        category = "авария"
        priority = "high"
        log.info(
            "Маркерные слова обнаружены — category='авария', priority='high'. "
            "Исходная category='%s'",
            payload.category,
        )
    elif category == "авария":
        priority = "high"

    ticket_no = db.create_appeal(
        ls=payload.ls,
        channel=payload.channel,
        category=category,
        body=payload.body,
        chat_id=payload.chat_id,
        file_path=payload.file_path,
        priority=priority,
    )
    log.info("Создано обращение %s  channel=%s  category=%s  priority=%s",
             ticket_no, payload.channel, category, priority)
    return AppealCreateOut(ticket_no=ticket_no)


# ── GET /api/v1/appeals/{ticket_no} ──────────────────────────────────────────

@router.get(
    "/{ticket_no}",
    response_model=AppealStatusOut,
    summary="Статус обращения по трек-номеру",
    description="Возвращает статус обращения и последний ответ оператора.",
)
async def get_appeal_status(
    ticket_no: str = Path(
        pattern=r"^[A-Z]+-\d{8}-\d{4}$",
        description="Трек-номер вида RSO-20260702-0001",
    ),
) -> AppealStatusOut:
    row = db.get_appeal_by_ticket(ticket_no)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Обращение '{ticket_no}' не найдено",
        )

    last_resp = db.get_last_appeal_response(row["id"])

    return AppealStatusOut(
        ticket_no=row["ticket_no"],
        status=row["status"],
        category=row["category"],
        priority=row["priority"],
        channel=row["channel"],
        ls=row["ls"],
        body=row["body"],
        created_at=row["created_at"],
        first_response_at=row["first_response_at"],
        closed_at=row["closed_at"],
        last_response=last_resp["body"] if last_resp else None,
        last_response_at=last_resp["sent_at"] if last_resp else None,
    )


# ── GET /api/v1/appeals ───────────────────────────────────────────────────────

@router.get(
    "",
    response_model=AppealListOut,
    summary="Список обращений (оператор)",
    description=(
        "Возвращает список обращений с фильтрами. "
        "date_from / date_to — строки формата 'YYYY-MM-DD' или 'YYYY-MM-DD HH:MM'."
    ),
)
async def list_appeals(
    status_filter: str | None = Query(None, alias="status",   description="Фильтр по статусу"),
    category:      str | None = Query(None,                   description="Фильтр по категории"),
    priority:      str | None = Query(None,                   description="Фильтр по приоритету"),
    date_from:     str | None = Query(None,                   description="С даты (включительно)"),
    date_to:       str | None = Query(None,                   description="По дату (включительно)"),
    ls:            str | None = Query(None,                   description="Фильтр по лицевому счёту"),
) -> AppealListOut:
    rows = db.list_appeals(
        status=status_filter,
        category=category,
        priority=priority,
        date_from=date_from,
        date_to=date_to,
        ls=ls,
    )
    items = [_row_to_list_item(r) for r in rows]
    return AppealListOut(total=len(items), appeals=items)


# ── PATCH /api/v1/appeals/{appeal_id}/status ─────────────────────────────────

@router.patch(
    "/{appeal_id}/status",
    response_model=AppealStatusUpdateOut,
    summary="Сменить статус обращения",
    description=(
        "Меняет статус. "
        "Триггер уведомления клиенту в исходный канал при любой смене статуса. "
        "Ошибка уведомления не роллбэкает смену статуса."
    ),
)
async def update_status(
    appeal_id: int,
    payload: AppealStatusUpdate,
) -> AppealStatusUpdateOut:
    row = db.get_appeal_by_id(appeal_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Обращение id={appeal_id} не найдено",
        )

    old_status = row["status"]
    db.update_appeal_status(appeal_id, payload.status, payload.operator_id)

    # Уведомляем клиента в его канал
    notified = False
    if row["chat_id"] and old_status != payload.status:
        text = build_status_notification(row["ticket_no"], payload.status)
        buttons: list[tuple[str, str]] | None = (
            [
                ("✅ Подтверждаю закрытие", f"confirm:{appeal_id}"),
                ("↩️ Вернуть в работу",     f"reopen:{appeal_id}"),
            ]
            if payload.status == "pending_confirmation"
            else None
        )
        notified = await notify_client(row["channel"], row["chat_id"], text, buttons)
        if not notified:
            log.warning(
                "Уведомление клиента не доставлено: ticket=%s  channel=%s  chat_id=%s",
                row["ticket_no"], row["channel"], row["chat_id"],
            )

    updated = db.get_appeal_by_id(appeal_id)
    log.info(
        "Статус %s: %s → %s  (оператор=%s, уведомление=%s)",
        row["ticket_no"], old_status, payload.status,
        payload.operator_id, notified,
    )
    return AppealStatusUpdateOut(
        id=updated["id"],
        ticket_no=updated["ticket_no"],
        status=updated["status"],
        updated_at=updated["updated_at"],
        closed_at=updated["closed_at"],
    )


# ── POST /api/v1/appeals/{appeal_id}/respond ─────────────────────────────────

@router.post(
    "/{appeal_id}/respond",
    status_code=status.HTTP_201_CREATED,
    response_model=AppealRespondOut,
    summary="Ответить клиенту",
    description=(
        "Фиксирует ответ оператора в appeal_responses "
        "и отправляет его клиенту в исходный канал."
    ),
)
async def respond_to_appeal(
    appeal_id: int,
    payload: AppealRespondIn,
) -> AppealRespondOut:
    row = db.get_appeal_by_id(appeal_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Обращение id={appeal_id} не найдено",
        )

    operator_id = payload.operator_id or 0
    response_id = db.add_appeal_response(appeal_id, operator_id, payload.body)
    resp_row = db.get_last_appeal_response(appeal_id)

    # Отправляем клиенту
    notified = False
    if row["chat_id"]:
        text = build_response_notification(row["ticket_no"], payload.body)
        notified = await notify_client(row["channel"], row["chat_id"], text)
        if not notified:
            log.warning(
                "Ответ не доставлен клиенту: ticket=%s  channel=%s  chat_id=%s",
                row["ticket_no"], row["channel"], row["chat_id"],
            )

    log.info(
        "Ответ по %s от оператора %s  уведомление=%s",
        row["ticket_no"], operator_id, notified,
    )
    return AppealRespondOut(
        response_id=response_id,
        appeal_id=appeal_id,
        ticket_no=row["ticket_no"],
        sent_at=resp_row["sent_at"] if resp_row else None,
        notified_channel=notified,
    )


# ── POST /api/v1/appeals/{appeal_id}/confirm ─────────────────────────────────

@router.post(
    "/{appeal_id}/confirm",
    response_model=AppealConfirmOut,
    summary="Клиент подтверждает закрытие обращения",
    description=(
        "Вызывается ботом когда клиент нажимает «✅ Подтверждаю закрытие» "
        "в MAX или Telegram (callback payload: `confirm:{appeal_id}`).\n\n"
        "Переводит обращение из `pending_confirmation` → `resolved`. "
        "Если статус уже не `pending_confirmation` — возвращает 409. "
        "Если `chat_id` не совпадает с записью в БД — 403 (защита от чужого нажатия)."
    ),
)
async def confirm_appeal(
    appeal_id: int,
    payload: AppealConfirmIn,
) -> AppealConfirmOut:
    row = db.get_appeal_by_id(appeal_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Обращение id={appeal_id} не найдено",
        )

    if row["status"] != "pending_confirmation":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Обращение id={appeal_id} не ожидает подтверждения "
                f"(текущий статус: {row['status']})"
            ),
        )

    # Проверяем что кнопку нажал именно тот клиент которому отправили уведомление
    if row["chat_id"] and payload.chat_id != row["chat_id"]:
        log.warning(
            "confirm_appeal: chat_id мismatch — appeal=%s  ожидался=%s  получен=%s",
            appeal_id, row["chat_id"], payload.chat_id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="chat_id не совпадает с обращением",
        )

    db.update_appeal_status(appeal_id, "resolved")
    updated = db.get_appeal_by_id(appeal_id)

    log.info(
        "Клиент подтвердил закрытие: ticket=%s  chat_id=%s  channel=%s",
        row["ticket_no"], payload.chat_id, payload.channel,
    )
    return AppealConfirmOut(
        appeal_id=appeal_id,
        ticket_no=updated["ticket_no"],
        status=updated["status"],
        closed_at=updated["closed_at"],
        confirmed_by_client=True,
    )
