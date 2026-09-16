"""
api/schemas.py — Pydantic v2 модели запросов и ответов.

Правила именования:
  *Create  — тело входящего запроса на создание
  *Update  — тело PATCH-запроса
  *Out     — ответ API клиенту
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


# ── Каналы и категории ────────────────────────────────────────────────────────

Channel  = Literal["max", "telegram", "lk", "widget"]
Category = Literal["заявка", "авария", "качество", "прочее"]
Priority = Literal["normal", "high"]
Status   = Literal["new", "in_work", "pending_confirmation", "resolved", "closed"]


# ── Интеграция с 1С ──────────────────────────────────────────────────────────

class Integration1CAuthRequest(BaseModel):
    ls: str = Field(..., min_length=1, max_length=64)
    chat_id: int

    @field_validator("ls")
    @classmethod
    def normalize_ls(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("ls не может быть пустым")
        return value


class Integration1CVerifyRequest(Integration1CAuthRequest):
    code: str = Field(..., min_length=1, max_length=32)

    @field_validator("code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("code не может быть пустым")
        return value


# ── POST /api/v1/appeals ──────────────────────────────────────────────────────

class AppealCreate(BaseModel):
    ls:        str | None = Field(None, description="Лицевой счёт (может отсутствовать)")
    channel:   Channel    = Field(..., description="Канал: max | telegram | lk | widget")
    category:  Category   = Field(..., description="Категория обращения")
    body:      str        = Field(..., min_length=1, description="Текст обращения")
    chat_id:   int | None = Field(None, description="ID чата для обратного ответа")
    file_path: str | None = Field(None, description="Путь к вложению (если есть)")

    @field_validator("body")
    @classmethod
    def body_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("body не может быть пустым")
        return v.strip()


class AppealCreateOut(BaseModel):
    ticket_no: str = Field(..., description="Трек-номер обращения (RSO-YYYYMMDD-NNNN)")


# ── GET /api/v1/appeals/{ticket_no} ──────────────────────────────────────────

class AppealStatusOut(BaseModel):
    ticket_no:         str
    status:            Status
    category:          Category
    priority:          Priority
    channel:           Channel
    ls:                str | None
    body:              str | None
    created_at:        str | None
    first_response_at: str | None
    closed_at:         str | None
    # Последний ответ оператора (если есть)
    last_response:     str | None = None
    last_response_at:  str | None = None


# ── GET /api/v1/appeals ───────────────────────────────────────────────────────

class AppealListItem(BaseModel):
    id:         int
    ticket_no:  str
    status:     Status
    category:   Category
    priority:   Priority
    channel:    Channel
    ls:         str | None
    body:       str | None   # нужно боту для превью в "Мои обращения" (не только детальный эндпоинт)
    created_at: str | None
    updated_at: str | None
    closed_at:  str | None


class AppealListOut(BaseModel):
    total:   int
    appeals: list[AppealListItem]


# ── PATCH /api/v1/appeals/{id}/status ────────────────────────────────────────

class AppealStatusUpdate(BaseModel):
    status:      Status
    operator_id: int | None = Field(
        None,
        description="ID оператора из Flask-сессии. "
                    "Null для внутренних системных вызовов.",
    )
    reason: str | None = Field(
        None,
        min_length=1,
        max_length=2000,
        description="Причина возврата обращения клиентом в работу.",
    )

    @field_validator("reason")
    @classmethod
    def reason_not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("reason не может быть пустым")
        return value


class AppealStatusUpdateOut(BaseModel):
    id:         int
    ticket_no:  str
    status:     Status
    updated_at: str | None
    closed_at:  str | None


# ── POST /api/v1/appeals/{id}/respond ────────────────────────────────────────

class AppealRespondIn(BaseModel):
    body:        str = Field(..., min_length=1)
    operator_id: int | None = Field(
        None,
        description="ID оператора. Null → сохраняется как operator_id=0 (система).",
    )

    @field_validator("body")
    @classmethod
    def body_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("body не может быть пустым")
        return v.strip()


class AppealRespondOut(BaseModel):
    response_id:      int
    appeal_id:        int
    ticket_no:        str
    sent_at:          str | None
    notified_channel: bool = Field(
        ...,
        description="True если уведомление в мессенджер отправлено успешно",
    )


# ── POST /api/v1/appeals/{id}/confirm (Этап 3) ───────────────────────────────
# Вызывается ботом когда клиент нажимает «Подтверждаю закрытие» в MAX/Telegram.

class AppealConfirmIn(BaseModel):
    channel:    Channel = Field(..., description="Канал в котором клиент нажал кнопку")
    chat_id:    int     = Field(..., description="chat_id клиента для подтверждения личности")
    operator_id: int | None = Field(None, description="Зарезервировано, передавать null")


class AppealConfirmOut(BaseModel):
    appeal_id:           int
    ticket_no:           str
    status:              Status  # будет 'resolved' или 'closed'
    closed_at:           str | None
    confirmed_by_client: bool = True
