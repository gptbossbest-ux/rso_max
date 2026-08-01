"""
api/routers/scripts.py — эндпоинты FAQ-скриптов.

GET /api/v1/scripts             — список активных скриптов (для меню бота)
GET /api/v1/scripts/{id}/tree   — полный граф скрипта (узлы + рёбра)

Реализует то, что было отложено как TODO Этап 9 в api/main.py.
bot.py уже вызывает эти эндпоинты через client_api.list_scripts() /
client_api.get_script_tree() — до этого роутера они получали 404,
поэтому раздел «Ответ на типовой вопрос» в боте не работал.

Редактирование скриптов (создание/узлы/рёбра) идёт напрямую через
database.py из web.py — здесь только чтение для бота, как и было
спроектировано на Этапе 3.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status

import database as db
from api.deps import verify_token

log = logging.getLogger("rso.api.scripts")

router = APIRouter(
    prefix="/scripts",
    tags=["scripts"],
    dependencies=[Depends(verify_token)],
)


@router.get(
    "",
    summary="Список активных скриптов",
    description="Возвращает активные скрипты для показа в меню бота (раздел FAQ).",
)
async def list_scripts() -> dict:
    rows = db.get_active_scripts()
    return {
        "scripts": [
            {"id": r["id"], "title": r["title"], "sort_order": r["sort_order"]}
            for r in rows
        ]
    }


@router.get(
    "/{script_id}/tree",
    summary="Полный граф скрипта",
    description="Возвращает узлы и рёбра активного скрипта для движка бота.",
)
async def get_script_tree(script_id: int) -> dict:
    tree = db.get_script_tree(script_id)
    if tree is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Скрипт id={script_id} не найден или неактивен",
        )
    return tree
