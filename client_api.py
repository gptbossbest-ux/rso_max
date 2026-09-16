"""
client_api.py — синхронный HTTP-клиент к внутреннему FastAPI РСО Портал.

Аналог client_1c.py, но для эндпоинтов /api/v1/*.
Используется bot.py (синхронный polling-цикл).

Все функции возвращают (data, error_msg):
  data      — распарсенный dict/list при успехе, None при ошибке
  error_msg — None при успехе, строка с описанием ошибки

Ошибка логируется, но НЕ бросает исключение —
бот показывает пользователю «Сервис временно недоступен».
"""
from __future__ import annotations

import logging

import httpx

from config import FASTAPI_BASE_URL, INTERNAL_API_TOKEN

log = logging.getLogger("rso.client_api")

_TIMEOUT = 5
_HEADERS = {"Authorization": f"Bearer {INTERNAL_API_TOKEN}"}


def _get(path: str, params: dict | None = None) -> tuple[dict | list | None, str | None]:
    url = f"{FASTAPI_BASE_URL}{path}"
    try:
        r = httpx.get(url, headers=_HEADERS, params=params, timeout=_TIMEOUT)
        if r.status_code == 200:
            return r.json(), None
        log.warning("GET %s → %s: %s", path, r.status_code, r.text[:200])
        return None, f"HTTP {r.status_code}"
    except httpx.TimeoutException:
        log.warning("GET %s timeout", path)
        return None, "timeout"
    except Exception as exc:
        log.error("GET %s error: %s", path, exc)
        return None, str(exc)


def _post(path: str, body: dict) -> tuple[dict | None, str | None]:
    url = f"{FASTAPI_BASE_URL}{path}"
    try:
        r = httpx.post(url, headers=_HEADERS, json=body, timeout=_TIMEOUT)
        if r.status_code in (200, 201):
            return r.json(), None
        log.warning("POST %s → %s: %s", path, r.status_code, r.text[:200])
        return None, f"HTTP {r.status_code}: {r.json().get('detail', r.text[:100])}"
    except httpx.TimeoutException:
        log.warning("POST %s timeout", path)
        return None, "timeout"
    except Exception as exc:
        log.error("POST %s error: %s", path, exc)
        return None, str(exc)


def _patch(path: str, body: dict) -> tuple[dict | None, str | None]:
    url = f"{FASTAPI_BASE_URL}{path}"
    try:
        r = httpx.patch(url, headers=_HEADERS, json=body, timeout=_TIMEOUT)
        if r.status_code == 200:
            return r.json(), None
        log.warning("PATCH %s → %s: %s", path, r.status_code, r.text[:200])
        return None, f"HTTP {r.status_code}"
    except httpx.TimeoutException:
        log.warning("PATCH %s timeout", path)
        return None, "timeout"
    except Exception as exc:
        log.error("PATCH %s error: %s", path, exc)
        return None, str(exc)


def _put(path: str, body: dict) -> tuple[dict | None, str | None]:
    url = f"{FASTAPI_BASE_URL}{path}"
    try:
        r = httpx.put(url, headers=_HEADERS, json=body, timeout=_TIMEOUT)
        if r.status_code == 200:
            return r.json(), None
        log.warning("PUT %s → %s: %s", path, r.status_code, r.text[:200])
        return None, f"HTTP {r.status_code}"
    except httpx.TimeoutException:
        log.warning("PUT %s timeout", path)
        return None, "timeout"
    except Exception as exc:
        log.error("PUT %s error: %s", path, exc)
        return None, str(exc)


def _delete(path: str) -> tuple[dict | None, str | None]:
    url = f"{FASTAPI_BASE_URL}{path}"
    try:
        r = httpx.delete(url, headers=_HEADERS, timeout=_TIMEOUT)
        if r.status_code == 200:
            return r.json(), None
        log.warning("DELETE %s → %s: %s", path, r.status_code, r.text[:200])
        return None, f"HTTP {r.status_code}"
    except httpx.TimeoutException:
        log.warning("DELETE %s timeout", path)
        return None, "timeout"
    except Exception as exc:
        log.error("DELETE %s error: %s", path, exc)
        return None, str(exc)


# ── Обращения ─────────────────────────────────────────────────────────────────

def create_appeal(
    ls: str | None,
    channel: str,
    category: str,
    body: str,
    chat_id: int,
    file_path: str | None = None,
) -> tuple[dict | None, str | None]:
    """POST /api/v1/appeals → {"ticket_no": "RSO-..."}"""
    return _post("/api/v1/appeals", {
        "ls": ls,
        "channel": channel,
        "category": category,
        "body": body,
        "chat_id": chat_id,
        "file_path": file_path,
    })


def get_appeal_status(ticket_no: str) -> tuple[dict | None, str | None]:
    """GET /api/v1/appeals/{ticket_no}"""
    return _get(f"/api/v1/appeals/{ticket_no}")


def list_appeals_by_ls(
    ls: str,
    status: str | None = None,
) -> tuple[dict | None, str | None]:
    """GET /api/v1/appeals?ls=...&status=..."""
    params: dict = {"ls": ls}
    if status:
        params["status"] = status
    return _get("/api/v1/appeals", params)


def confirm_appeal(
    appeal_id: int,
    channel: str,
    chat_id: int,
) -> tuple[dict | None, str | None]:
    """POST /api/v1/appeals/{id}/confirm"""
    return _post(f"/api/v1/appeals/{appeal_id}/confirm", {
        "channel": channel,
        "chat_id": chat_id,
    })


def respond_to_appeal(
    appeal_id: int,
    body: str,
    operator_id: int | None = None,
) -> tuple[dict | None, str | None]:
    """POST /api/v1/appeals/{id}/respond — отправить ответ клиенту."""
    return _post(f"/api/v1/appeals/{appeal_id}/respond", {
        "body": body,
        "operator_id": operator_id,
    })


def change_appeal_status(
    appeal_id: int,
    status: str,
    operator_id: int | None = None,
) -> tuple[dict | None, str | None]:
    """PATCH /api/v1/appeals/{id}/status — сменить статус (+ уведомление клиенту)."""
    return _patch(f"/api/v1/appeals/{appeal_id}/status", {
        "status": status,
        "operator_id": operator_id,
    })


def reopen_appeal(appeal_id: int) -> tuple[dict | None, str | None]:
    """PATCH /api/v1/appeals/{id}/status → in_work (используется ботом)."""
    return change_appeal_status(appeal_id, "in_work")


# ── Авторизация через 1С ─────────────────────────────────────────────────────

def request_1c_auth_code(ls: str, chat_id: int) -> tuple[dict | None, str | None]:
    return _post("/api/v1/integrations/1c/auth/request-code", {
        "ls": ls,
        "chat_id": chat_id,
    })


def verify_1c_auth_code(
    ls: str,
    chat_id: int,
    code: str,
) -> tuple[dict | None, str | None]:
    return _post("/api/v1/integrations/1c/auth/verify-code", {
        "ls": ls,
        "chat_id": chat_id,
        "code": code,
    })


# ── Скрипты ───────────────────────────────────────────────────────────────────

def list_scripts() -> tuple[list | None, str | None]:
    """GET /api/v1/scripts → список активных скриптов"""
    data, err = _get("/api/v1/scripts")
    if err:
        return None, err
    # data может быть {"scripts": [...]} или список напрямую
    if isinstance(data, list):
        return data, None
    return data.get("scripts", []), None


def get_script_tree(script_id: int) -> tuple[dict | None, str | None]:
    """GET /api/v1/scripts/{id}/tree → {id, title, nodes, edges}"""
    return _get(f"/api/v1/scripts/{script_id}/tree")


# ── Домовые чаты ──────────────────────────────────────────────────────────────

def house_chat_create(
    address: str,
    messenger: str,
    chat_id: str,
) -> tuple[dict | None, str | None]:
    """POST /api/v1/house-chats — подключить домовой чат."""
    return _post("/api/v1/house-chats", {
        "address":   address,
        "messenger": messenger,
        "chat_id":   chat_id,
    })


def house_chat_deactivate(house_chat_id: int) -> tuple[dict | None, str | None]:
    """DELETE /api/v1/house-chats/{id} — отвязать домовой чат."""
    url = f"{FASTAPI_BASE_URL}/api/v1/house-chats/{house_chat_id}"
    try:
        import httpx as _httpx
        r = _httpx.delete(url, headers=_HEADERS, timeout=_TIMEOUT)
        if r.status_code == 200:
            return r.json(), None
        log.warning("DELETE /house-chats/%s → %s", house_chat_id, r.status_code)
        return None, f"HTTP {r.status_code}"
    except Exception as exc:
        log.error("house_chat_deactivate: %s", exc)
        return None, str(exc)


def house_chat_link_scenario(
    house_chat_id: int,
    scenario_id: int,
) -> tuple[dict | None, str | None]:
    """POST /api/v1/house-chats/{id}/scenarios/{sid} — привязать сценарий."""
    return _post(f"/api/v1/house-chats/{house_chat_id}/scenarios/{scenario_id}", {})


def house_chat_unlink_scenario(
    house_chat_id: int,
    scenario_id: int,
) -> tuple[dict | None, str | None]:
    """DELETE /api/v1/house-chats/{id}/scenarios/{sid} — отвязать сценарий."""
    url = f"{FASTAPI_BASE_URL}/api/v1/house-chats/{house_chat_id}/scenarios/{scenario_id}"
    try:
        import httpx as _httpx
        r = _httpx.delete(url, headers=_HEADERS, timeout=_TIMEOUT)
        if r.status_code == 200:
            return r.json(), None
        log.warning("DELETE /house-chats/%s/scenarios/%s → %s",
                    house_chat_id, scenario_id, r.status_code)
        return None, f"HTTP {r.status_code}"
    except Exception as exc:
        log.error("house_chat_unlink_scenario: %s", exc)
        return None, str(exc)


def house_chat_add_exclusion(
    house_chat_id: int,
    messenger: str,
    user_id: str,
    reason: str | None = None,
) -> tuple[dict | None, str | None]:
    """POST /api/v1/house-chats/{id}/exclusions — добавить исключение."""
    return _post(f"/api/v1/house-chats/{house_chat_id}/exclusions", {
        "messenger": messenger,
        "user_id":   user_id,
        "reason":    reason,
    })


def house_chat_remove_exclusion(
    house_chat_id: int,
    exclusion_id: int,
) -> tuple[dict | None, str | None]:
    """DELETE /api/v1/house-chats/{id}/exclusions/{eid} — удалить исключение."""
    url = f"{FASTAPI_BASE_URL}/api/v1/house-chats/{house_chat_id}/exclusions/{exclusion_id}"
    try:
        import httpx as _httpx
        r = _httpx.delete(url, headers=_HEADERS, timeout=_TIMEOUT)
        if r.status_code == 200:
            return r.json(), None
        log.warning("DELETE exclusion %s → %s", exclusion_id, r.status_code)
        return None, f"HTTP {r.status_code}"
    except Exception as exc:
        log.error("house_chat_remove_exclusion: %s", exc)
        return None, str(exc)


def house_chats_broadcast(
    house_chat_ids: list[int],
    text: str,
) -> tuple[dict | None, str | None]:
    """POST /api/v1/broadcast — рассылка в домовые чаты."""
    return _post("/api/v1/broadcast", {
        "house_chat_ids": house_chat_ids,
        "text":           text,
    })

# ── Сценарии мониторинга: CRUD (Этап 9а) ──────────────────────────────────────

def list_scenarios() -> tuple[list | None, str | None]:
    """GET /api/v1/scenarios → список всех сценариев (keywords уже list)."""
    data, err = _get("/api/v1/scenarios")
    if err:
        return None, err
    if isinstance(data, list):
        return data, None
    return data.get("scenarios", []), None


def create_scenario(
    title: str,
    keywords: list[str],
    response_text: str,
    suggest_appeal: bool = False,
) -> tuple[dict | None, str | None]:
    """POST /api/v1/scenarios — создать сценарий."""
    return _post("/api/v1/scenarios", {
        "title":          title,
        "keywords":       keywords,
        "response_text":  response_text,
        "suggest_appeal": suggest_appeal,
    })


def update_scenario(
    scenario_id: int,
    title: str,
    keywords: list[str],
    response_text: str,
    suggest_appeal: bool,
    is_active: bool,
) -> tuple[dict | None, str | None]:
    """PUT /api/v1/scenarios/{id} — полное обновление сценария."""
    return _put(f"/api/v1/scenarios/{scenario_id}", {
        "title":          title,
        "keywords":       keywords,
        "response_text":  response_text,
        "suggest_appeal": suggest_appeal,
        "is_active":      is_active,
    })


def delete_scenario(scenario_id: int) -> tuple[dict | None, str | None]:
    """DELETE /api/v1/scenarios/{id} — удалить сценарий с привязками."""
    return _delete(f"/api/v1/scenarios/{scenario_id}")

# ── FAQ-скрипты: мутации (Этап 10) ────────────────────────────────────────────
# list_scripts() и get_script_tree() уже определены выше (используются ботом).

def create_script(title: str) -> tuple[dict | None, str | None]:
    """POST /api/v1/scripts — создать скрипт."""
    return _post("/api/v1/scripts", {"title": title})


def update_script(
    script_id: int,
    title: str,
    is_active: bool,
    sort_order: int,
) -> tuple[dict | None, str | None]:
    """PUT /api/v1/scripts/{id} — обновить заголовок/статус/порядок."""
    return _put(f"/api/v1/scripts/{script_id}", {
        "title":      title,
        "is_active":  is_active,
        "sort_order": sort_order,
    })


def delete_script(script_id: int) -> tuple[dict | None, str | None]:
    """DELETE /api/v1/scripts/{id} — удалить скрипт каскадно."""
    return _delete(f"/api/v1/scripts/{script_id}")


def add_node(
    script_id: int,
    title: str,
    is_terminal: bool = False,
    image_path: str | None = None,
) -> tuple[dict | None, str | None]:
    """POST /api/v1/scripts/{id}/nodes — добавить узел."""
    return _post(f"/api/v1/scripts/{script_id}/nodes", {
        "title":       title,
        "is_terminal": is_terminal,
        "image_path":  image_path,
    })


def update_node(
    script_id: int,
    node_id: int,
    title: str,
    is_terminal: bool = False,
    image_path: str | None = None,
) -> tuple[dict | None, str | None]:
    """PUT /api/v1/scripts/{id}/nodes/{nid} — обновить узел."""
    return _put(f"/api/v1/scripts/{script_id}/nodes/{node_id}", {
        "title":       title,
        "is_terminal": is_terminal,
        "image_path":  image_path,
    })


def delete_node(
    script_id: int,
    node_id: int,
) -> tuple[dict | None, str | None]:
    """DELETE /api/v1/scripts/{id}/nodes/{nid} — удалить узел с рёбрами."""
    return _delete(f"/api/v1/scripts/{script_id}/nodes/{node_id}")


def add_edge(
    script_id: int,
    from_node_id: int,
    label: str,
    to_node_id: int,
) -> tuple[dict | None, str | None]:
    """
    POST /api/v1/scripts/{id}/edges — добавить переход.
    HTTP 422 от API = петля или цикл в графе; текст причины в error_msg.
    """
    return _post(f"/api/v1/scripts/{script_id}/edges", {
        "from_node_id": from_node_id,
        "label":        label,
        "to_node_id":   to_node_id,
    })


def delete_edge(
    script_id: int,
    edge_id: int,
) -> tuple[dict | None, str | None]:
    """DELETE /api/v1/scripts/{id}/edges/{eid} — удалить переход."""
    return _delete(f"/api/v1/scripts/{script_id}/edges/{edge_id}")

