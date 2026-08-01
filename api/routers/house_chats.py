"""
api/routers/house_chats.py — REST API домовых чатов, Этап 7.

Эндпоинты:
  GET    /house-chats                          — список чатов
  POST   /house-chats                          — подключить чат
  DELETE /house-chats/{id}                     — отвязать чат (is_active=0)
  POST   /broadcast                            — рассылка в домовые чаты

  POST   /house-chats/{id}/scenarios/{sid}   — привязать сценарий
  DELETE /house-chats/{id}/scenarios/{sid}   — отвязать сценарий

  GET    /house-chats/{id}/exclusions          — список исключений
  POST   /house-chats/{id}/exclusions          — добавить исключение
  DELETE /house-chats/{id}/exclusions/{eid}    — удалить исключение

  GET    /scenarios                            — список сценариев (Этап 9а)
  POST   /scenarios                            — создать сценарий
  PUT    /scenarios/{id}                       — обновить сценарий
  DELETE /scenarios/{id}                       — удалить сценарий

Роутер подключается в main.py с prefix="/api/v1":
    app.include_router(house_chats_router, prefix="/api/v1")

Аутентификация мутаций — Bearer-токен через deps.verify_token.
Токен MAX передаётся БЕЗ префикса Bearer: {"Authorization": TOKEN}
"""
from __future__ import annotations

import json
import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import database as db
from api.deps import verify_token
from config import TOKEN, API

log = logging.getLogger("rso.api.house_chats")

router = APIRouter(tags=["house_chats"])

_MAX_HEADERS = {"Authorization": TOKEN}
_MAX_TIMEOUT = 5


# ── Pydantic-схемы ────────────────────────────────────────────────────────────

class HouseChatCreate(BaseModel):
    address: str = Field(..., min_length=1, max_length=255)
    messenger: str = Field(..., pattern="^(max|telegram)$")
    chat_id: str = Field(..., min_length=1, max_length=64)


class ExclusionCreate(BaseModel):
    messenger: str = Field(..., pattern="^(max|telegram)$")
    user_id: str = Field(..., min_length=1, max_length=64)
    reason: str | None = None


class BroadcastBody(BaseModel):
    house_chat_ids: list[int] = Field(..., min_length=1)
    text: str = Field(..., min_length=1)


class ScenarioCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    keywords: list[str] = Field(..., min_length=1)
    response_text: str = Field(..., min_length=1)
    suggest_appeal: bool = False


class ScenarioUpdate(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    keywords: list[str] = Field(..., min_length=1)
    response_text: str = Field(..., min_length=1)
    suggest_appeal: bool = False
    is_active: bool = True


# ── Вспомогательные функции ───────────────────────────────────────────────────

def _rows(rs) -> list[dict]:
    return [dict(r) for r in rs]


def _get_chat_or_404(house_chat_id: int) -> dict:
    """Возвращает dict домового чата или бросает 404."""
    conn = db.get_conn()
    row = conn.execute(
        "SELECT * FROM house_chats WHERE id=?", (house_chat_id,)
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Домовой чат не найден")
    return dict(row)


# ── Домовые чаты ──────────────────────────────────────────────────────────────

@router.get("/house-chats")
def list_house_chats(active_only: bool = True):
    """
    Список домовых чатов.
    active_only=false — включая отключённые (для истории в портале).
    """
    rows = db.get_house_chats(active_only=active_only)
    return {"house_chats": _rows(rows)}


@router.post("/house-chats", status_code=201, dependencies=[Depends(verify_token)])
def create_house_chat(body: HouseChatCreate):
    """
    Подключить домовой чат.
    Уникальный индекс uq_house_chats_active_address не позволяет
    иметь два активных чата на один адрес → 409.
    """
    try:
        row_id = db.add_house_chat(body.address, body.messenger, body.chat_id)
    except Exception as exc:
        if "UNIQUE" in str(exc):
            raise HTTPException(
                status_code=409,
                detail=f"Активный чат для адреса '{body.address}' уже существует",
            )
        log.error("create_house_chat error: %s", exc)
        raise HTTPException(status_code=500, detail="Ошибка базы данных")
    log.info("Домовой чат подключён: id=%s  адрес=%s", row_id, body.address)
    return {"id": row_id, "address": body.address, "messenger": body.messenger}


@router.delete("/house-chats/{house_chat_id}", dependencies=[Depends(verify_token)])
def deactivate_house_chat(house_chat_id: int):
    """Отвязать домовой чат (is_active=0, disconnected_at=now)."""
    _get_chat_or_404(house_chat_id)
    db.deactivate_house_chat(house_chat_id)
    return {"ok": True, "id": house_chat_id}


# ── Рассылка в домовые чаты ───────────────────────────────────────────────────

@router.post("/broadcast", dependencies=[Depends(verify_token)])
async def broadcast(body: BroadcastBody):
    """
    Рассылка текста в указанные домовые чаты через MAX Bot API.
    Токен передаётся БЕЗ префикса Bearer.

    Возвращает результат по каждому чату:
      {"chat_id": "...", "ok": true/false, "error": "..."|null}

    Ошибка отдельного чата не прерывает рассылку остальным.
    """
    # Загружаем активные чаты одним запросом
    conn = db.get_conn()
    placeholders = ",".join("?" * len(body.house_chat_ids))
    rows = conn.execute(
        f"SELECT id, chat_id, messenger FROM house_chats "
        f"WHERE id IN ({placeholders}) AND is_active=1",
        body.house_chat_ids,
    ).fetchall()
    conn.close()

    found_ids = {r["id"] for r in rows}
    results = []

    async with httpx.AsyncClient(timeout=_MAX_TIMEOUT) as http:
        for row in rows:
            chat_id = row["chat_id"]
            messenger = row["messenger"]
            try:
                if messenger == "max":
                    resp = await http.post(
                        f"{API}/messages",
                        headers=_MAX_HEADERS,
                        params={"chat_id": chat_id},
                        json={"text": body.text},
                    )
                    ok = resp.status_code == 200
                    error = None if ok else f"HTTP {resp.status_code}"
                else:
                    # Telegram — заглушка (Этап 10)
                    ok = False
                    error = "telegram не реализован в Горизонте 1"

                results.append({
                    "house_chat_id": row["id"],
                    "chat_id": chat_id,
                    "ok": ok,
                    "error": error,
                })
                log.info(
                    "broadcast → chat_id=%s  ok=%s  error=%s",
                    chat_id, ok, error,
                )
            except Exception as exc:
                log.error("broadcast chat_id=%s: %s", chat_id, exc)
                results.append({
                    "house_chat_id": row["id"],
                    "chat_id": chat_id,
                    "ok": False,
                    "error": str(exc),
                })

    # Чаты из запроса, которых не оказалось в БД или они неактивны
    for hc_id in body.house_chat_ids:
        if hc_id not in found_ids:
            results.append({
                "house_chat_id": hc_id,
                "chat_id": None,
                "ok": False,
                "error": "чат не найден или неактивен",
            })

    sent = sum(1 for r in results if r["ok"])
    failed = len(results) - sent
    log.info("broadcast завершена: отправлено=%d  ошибок=%d", sent, failed)
    return {"sent": sent, "failed": failed, "results": results}



# ── Сценарии конкретного чата ─────────────────────────────────────────────────

@router.post(
    "/house-chats/{house_chat_id}/scenarios/{scenario_id}",
    status_code=201,
    dependencies=[Depends(verify_token)],
)
def link_scenario(house_chat_id: int, scenario_id: int):
    """
    Привязать сценарий мониторинга к домовому чату.
    INSERT OR IGNORE — повторная привязка не даёт ошибку.
    """
    _get_chat_or_404(house_chat_id)
    # Проверяем существование сценария
    conn = db.get_conn()
    sc = conn.execute(
        "SELECT id FROM chat_scenarios WHERE id=?", (scenario_id,)
    ).fetchone()
    conn.close()
    if not sc:
        raise HTTPException(status_code=404, detail="Сценарий не найден")
    db.link_scenario_to_chat(house_chat_id, scenario_id)
    return {"ok": True, "house_chat_id": house_chat_id, "scenario_id": scenario_id}


@router.delete(
    "/house-chats/{house_chat_id}/scenarios/{scenario_id}",
    dependencies=[Depends(verify_token)],
)
def unlink_scenario(house_chat_id: int, scenario_id: int):
    """Отвязать сценарий мониторинга от домового чата."""
    _get_chat_or_404(house_chat_id)
    db.unlink_scenario_from_chat(house_chat_id, scenario_id)
    return {"ok": True}


# ── Исключения ────────────────────────────────────────────────────────────────

@router.get("/house-chats/{house_chat_id}/exclusions")
def list_exclusions(house_chat_id: int):
    """Список пользователей-исключений для домового чата."""
    _get_chat_or_404(house_chat_id)
    rows = db.list_chat_exclusions(house_chat_id)
    return {"exclusions": _rows(rows)}


@router.post(
    "/house-chats/{house_chat_id}/exclusions",
    status_code=201,
    dependencies=[Depends(verify_token)],
)
def add_exclusion(house_chat_id: int, body: ExclusionCreate):
    """
    Добавить пользователя в список исключений.
    INSERT OR IGNORE — повторное добавление не даёт ошибку.
    """
    _get_chat_or_404(house_chat_id)
    db.add_chat_exclusion(
        house_chat_id,
        body.messenger,
        body.user_id,
        body.reason,
    )
    return {"ok": True, "house_chat_id": house_chat_id, "user_id": body.user_id}


@router.delete(
    "/house-chats/{house_chat_id}/exclusions/{exclusion_id}",
    dependencies=[Depends(verify_token)],
)
def remove_exclusion(house_chat_id: int, exclusion_id: int):
    """
    Удалить пользователя из исключений по id записи.

    database.py::remove_chat_exclusion принимает (house_chat_id, messenger, user_id).
    Поэтому сначала читаем запись по id, затем удаляем по составному ключу.
    Это соответствует UNIQUE(house_chat_id, messenger, user_id) в схеме.
    """
    conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT messenger, user_id FROM chat_exclusions "
            "WHERE id=? AND house_chat_id=?",
            (exclusion_id, house_chat_id),
        ).fetchone()
        if not row:
            raise HTTPException(
                status_code=404,
                detail="Исключение не найдено",
            )
        # Используем существующую функцию — она удаляет по составному ключу
        messenger = row["messenger"]
        user_id = row["user_id"]
    finally:
        conn.close()

    db.remove_chat_exclusion(house_chat_id, messenger, user_id)
    log.info(
        "Исключение удалено: house_chat_id=%s  user_id=%s",
        house_chat_id, user_id,
    )
    return {"ok": True}

# ── Сценарии мониторинга: CRUD (Этап 9а) ──────────────────────────────────────

def _scenario_to_dict(row) -> dict:
    """Row → dict с десериализацией keywords JSON → list."""
    d = dict(row)
    try:
        d["keywords"] = json.loads(d["keywords"])
    except (json.JSONDecodeError, TypeError):
        d["keywords"] = []
    return d


@router.get("/scenarios")
def list_scenarios():
    """Список всех сценариев мониторинга (включая неактивные)."""
    rows = db.get_all_scenarios()
    return {"scenarios": [_scenario_to_dict(r) for r in rows]}


@router.post("/scenarios", status_code=201, dependencies=[Depends(verify_token)])
def create_scenario(body: ScenarioCreate):
    """Создать сценарий мониторинга. is_active=1 по умолчанию."""
    keywords_json = json.dumps(
        [kw.strip() for kw in body.keywords if kw.strip()],
        ensure_ascii=False,
    )
    row_id = db.create_scenario(
        title=body.title,
        keywords_json=keywords_json,
        response_text=body.response_text,
        suggest_appeal=body.suggest_appeal,
    )
    return {"id": row_id, "title": body.title}


@router.put("/scenarios/{scenario_id}", dependencies=[Depends(verify_token)])
def update_scenario(scenario_id: int, body: ScenarioUpdate):
    """Полное обновление сценария (все поля)."""
    row = db.get_scenario(scenario_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Сценарий не найден")

    keywords_json = json.dumps(
        [kw.strip() for kw in body.keywords if kw.strip()],
        ensure_ascii=False,
    )
    db.update_scenario(
        scenario_id=scenario_id,
        title=body.title,
        keywords_json=keywords_json,
        response_text=body.response_text,
        suggest_appeal=body.suggest_appeal,
        is_active=body.is_active,
    )
    return {"ok": True, "id": scenario_id}


@router.delete("/scenarios/{scenario_id}", dependencies=[Depends(verify_token)])
def delete_scenario(scenario_id: int):
    """Удалить сценарий вместе с привязками к чатам."""
    row = db.get_scenario(scenario_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Сценарий не найден")
    db.delete_scenario(scenario_id)
    return {"ok": True}

