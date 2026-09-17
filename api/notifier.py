"""
api/notifier.py — отправка уведомлений клиенту в исходный канал.

Используется при:
  - смене статуса обращения (PATCH /api/v1/appeals/{id}/status)
  - ответе оператора (POST /api/v1/appeals/{id}/respond)

Текущее состояние Горизонта 1:
  - MAX  → реализован
  - Telegram → заглушка (Этап 10)
  - lk / widget → нет пуша (клиент сам опрашивает GET /appeals/{ticket_no})

Все отправки — async, не блокируют основной поток FastAPI.
Ошибка отправки логируется, но НЕ роллбэкает изменение в БД:
пользователь не получил уведомление, но статус всё равно изменился.
"""
from __future__ import annotations

import logging

import httpx

from config import API, TOKEN

log = logging.getLogger("rso.api.notifier")

_MAX_TIMEOUT = 5  # секунд на одну попытку


async def notify_client(
    channel: str,
    chat_id: int | None,
    text: str,
    buttons: list[tuple[str, str]] | None = None,
) -> bool:
    """
    Отправляет `text` клиенту в указанный `channel`.

    buttons — список пар (label, payload):
      label   — текст кнопки, видимый пользователю
      payload — машинный идентификатор, приходит в callback боту

    Пример для pending_confirmation:
      buttons=[
          ("✅ Подтверждаю закрытие", f"confirm:{appeal_id}"),
          ("↩️ Вернуть в работу",     f"reopen:{appeal_id}"),
      ]

    Бот разбирает payload через split(":") и знает и действие, и ID обращения
    без зависимости от состояния сессии (Вариант А, Этап 3).

    Возвращает True при успехе, False при любой ошибке.
    При chat_id=None возвращает False (некуда отправлять).
    """
    if chat_id is None:
        log.debug("notify_client: chat_id=None, уведомление пропущено")
        return False

    if channel == "max":
        return await _notify_max(chat_id, text, buttons)

    if channel == "telegram":
        # TODO Этап 10: Telegram-адаптер
        log.debug("notify_client: telegram не реализован в Горизонте 1")
        return False

    # lk / widget — нет push, клиент сам опрашивает статус
    log.debug("notify_client: канал %s не поддерживает push", channel)
    return False


async def _notify_max(
    chat_id: int,
    text: str,
    buttons: list[tuple[str, str]] | None = None,
) -> bool:
    """
    Отправляет сообщение через MAX Bot API.
    buttons: список (label, payload) — label показывается пользователю,
    payload приходит в callback боту. Каждая кнопка — отдельный ряд.
    """
    if not TOKEN:
        log.warning("_notify_max: TOKEN не задан, отправка пропущена")
        return False

    body: dict = {"text": text}
    if buttons:
        body["attachments"] = [{
            "type": "inline_keyboard",
            "payload": {
                "buttons": [
                    [{"type": "callback", "text": label, "payload": payload}]
                    for label, payload in buttons
                ]
            },
        }]

    try:
        async with httpx.AsyncClient(timeout=_MAX_TIMEOUT) as http:
            resp = await http.post(
                f"{API}/messages",
                headers={"Authorization": TOKEN},  # без Bearer — см. dev.max.ru/docs-api
                params={"chat_id": chat_id},
                json=body,
            )
        if resp.status_code == 200:
            log.info("MAX уведомление отправлено: chat_id=%s  кнопок=%d",
                     chat_id, len(buttons) if buttons else 0)
            return True

        log.warning(
            "MAX API вернул %s для chat_id=%s",
            resp.status_code, chat_id,
        )
        return False

    except httpx.TimeoutException:
        log.warning("MAX API timeout: chat_id=%s", chat_id)
        return False
    except Exception as exc:  # noqa: BLE001 - transport failures must not escape notifier
        # Текст исключения может содержать URL, заголовки или токены транспорта.
        log.error("MAX API ошибка: chat_id=%s  (%s)", chat_id, type(exc).__name__)
        return False


def build_status_notification(ticket_no: str, status: str) -> str:
    """Текст уведомления клиенту при смене статуса."""
    labels = {
        "in_work":              "✅ Ваше обращение принято в работу.",
        "pending_confirmation": "🔔 Обращение готово к закрытию. Подтвердите или верните в работу.",
        "resolved":             "✔️ Обращение закрыто.",
        "closed":               "✔️ Обращение закрыто.",
        "new":                  "📥 Обращение зарегистрировано.",
    }
    label = labels.get(status, f"Статус обращения изменён: {status}.")
    return f"По обращению №{ticket_no}:\n{label}"


def build_response_notification(ticket_no: str, body: str) -> str:
    """Текст уведомления клиенту при ответе оператора."""
    return (
        f"💬 Ответ по обращению №{ticket_no}:\n\n"
        f"{body}"
    )
