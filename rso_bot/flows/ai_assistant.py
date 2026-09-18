"""Privacy-preserving YandexGPT conversation flow.

Only sanitized text crosses the external API boundary.  Conversation history
is an operational, one-day session rather than an audit log and is removed when
the server-local calendar date changes.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import httpx

State = dict[str, Any]
Button = dict[str, Any]


class AIServiceError(RuntimeError):
    """A safe, content-free indication that the provider request failed."""


_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-zА-Яа-я]{2,}(?!\w)")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+7|8)[\s()\-]*\d{3}[\s()\-]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)")
_ACCOUNT_RE = re.compile(
    r"(?i)\b(?:лицев(?:ой|ого)\s+сч(?:ё|е)т(?:а)?|л\.?\s*с\.?)\s*[:№#-]?\s*[A-ZА-Я0-9_-]{3,64}"
)
_LONG_ID_RE = re.compile(r"(?<!\d)\d{6,20}(?!\d)")
_FULL_NAME_RE = re.compile(
    r"\b[A-ZА-ЯЁ][a-zа-яё-]+\s+[A-ZА-ЯЁ][a-zа-яё-]+\s+[A-ZА-ЯЁ][a-zа-яё-]+\b"
)
_ADDRESS_RE = re.compile(
    r"(?i)\b(?:адрес|улица|ул\.| проспект|пр-т|переулок|пер\.)\s*[:,-]?\s*[^\n;]{2,120}"
)
_LABELED_NAME_RE = re.compile(r"(?i)\b(?:фио|получатель|собственник)\s*[:,-]?\s*[A-ZА-ЯЁ][A-Za-zА-яЁё-]+(?:\s+[A-ZА-ЯЁ][A-Za-zА-яЁё-]+){1,2}")


def sanitize_personal_data(text: str, known_values: Iterable[str] = ()) -> str:
    """Redact obvious PII plus exact customer data known by the application."""
    cleaned = str(text or "")
    for value in sorted(
        {str(item).strip() for item in known_values if str(item).strip()},
        key=len,
        reverse=True,
    ):
        cleaned = re.sub(re.escape(value), "[ПДн удалены]", cleaned, flags=re.IGNORECASE)
    cleaned = _EMAIL_RE.sub("[эл. почта удалена]", cleaned)
    cleaned = _PHONE_RE.sub("[телефон удалён]", cleaned)
    cleaned = _ACCOUNT_RE.sub("лицевой счёт [удалён]", cleaned)
    cleaned = _LONG_ID_RE.sub("[номер удалён]", cleaned)
    cleaned = _LABELED_NAME_RE.sub("ФИО [удалено]", cleaned)
    cleaned = _FULL_NAME_RE.sub("ФИО [удалено]", cleaned)
    cleaned = _ADDRESS_RE.sub("адрес [удалён]", cleaned)
    return cleaned.strip()


@dataclass(frozen=True)
class YandexGPTClient:
    api_key: str
    folder_id: str
    api_url: str
    timeout_seconds: float = 20.0
    http_client: Any = httpx

    def configured_for(self, model: str) -> bool:
        return bool(self.api_key and (self.folder_id or model.startswith("gpt://")))

    def complete(
        self,
        *,
        settings: dict[str, Any],
        history: list[dict[str, str]],
        question: str,
        faq_context: str | None,
    ) -> str:
        model = settings["model"].strip()
        if not self.configured_for(model):
            raise AIServiceError("not configured")
        model_uri = model if model.startswith("gpt://") else f"gpt://{self.folder_id}/{model}"
        system_prompt = settings["system_prompt"].strip()
        if faq_context:
            system_prompt += (
                "\n\nОбезличенный путь по FAQ, который прошёл пользователь: "
                + faq_context
            )
        messages = [{"role": "system", "text": system_prompt}]
        messages.extend(history)
        messages.append({"role": "user", "text": question})
        try:
            response = self.http_client.post(
                self.api_url,
                headers={
                    "Authorization": f"Api-Key {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "modelUri": model_uri,
                    "completionOptions": {
                        "stream": False,
                        "temperature": float(settings["temperature"]),
                        "maxTokens": str(int(settings["max_output_tokens"])),
                    },
                    "messages": messages,
                },
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
            alternatives = data.get("result", {}).get("alternatives", [])
            answer = alternatives[0].get("message", {}).get("text", "").strip()
            if not answer:
                raise AIServiceError("empty response")
            return answer
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise AIServiceError("provider request failed") from exc


@dataclass(frozen=True)
class AIDependencies:
    get_settings: Callable[[], dict[str, Any]]
    get_session: Callable[[int], dict[str, Any]]
    set_context: Callable[[int, str | None], None]
    reserve_question: Callable[[int, int], bool]
    release_question: Callable[[int], None]
    append_exchange: Callable[[int, str, str], None]
    clear_history: Callable[[int], None]
    get_sensitive_values: Callable[[int], Iterable[str]]
    complete: Callable[..., str]
    get_state: Callable[[int], State]
    touch: Callable[[State], State]
    start_appeal_draft: Callable[[int, str], None]
    make_callback: Callable[[str, str], Button]
    send_buttons: Callable[[int, str, list[list[Button]]], Any]
    send_main_menu: Callable[..., None]
    is_configured: Callable[[str], bool]
    logger: logging.Logger
    question_state: str


def _fallback_buttons(deps: AIDependencies) -> list[list[Button]]:
    return [
        [deps.make_callback("📝 Оформить обращение", "ai_appeal")],
        [deps.make_callback("🏠 Главное меню", "main_menu")],
    ]


def _answer_buttons(deps: AIDependencies) -> list[list[Button]]:
    return [
        [deps.make_callback("📝 Оформить обращение", "ai_appeal")],
        [deps.make_callback("❓ Задать ещё вопрос", "ai_more")],
        [deps.make_callback("🧹 Новый диалог", "ai_new")],
        [deps.make_callback("🏠 Главное меню", "main_menu")],
    ]


def start(chat_id: int, deps: AIDependencies, faq_context: str | None = None) -> None:
    settings = deps.get_settings()
    if not settings["enabled"] or not deps.is_configured(settings["model"]):
        deps.send_buttons(
            chat_id,
            "⚠️ ИИ-помощник сейчас недоступен. Вы можете оформить обращение.",
            _fallback_buttons(deps),
        )
        return
    known = tuple(deps.get_sensitive_values(chat_id))
    if faq_context:
        deps.set_context(chat_id, sanitize_personal_data(faq_context, known))
    else:
        deps.get_session(chat_id)
    state = deps.get_state(chat_id)
    state["state"] = deps.question_state
    deps.touch(state)
    deps.send_buttons(
        chat_id,
        "🤖 Задайте вопрос по теме ЖКХ. Не указывайте ФИО, адрес, номер лицевого счёта и другие личные данные.",
        [
            [deps.make_callback("🧹 Новый диалог", "ai_new")],
            [deps.make_callback("🏠 Главное меню", "main_menu")],
        ],
    )


def ask(chat_id: int, text: str, deps: AIDependencies) -> None:
    settings = deps.get_settings()
    if not settings["enabled"] or not deps.is_configured(settings["model"]):
        deps.send_buttons(chat_id, "⚠️ ИИ-помощник сейчас недоступен.", _fallback_buttons(deps))
        return
    known = tuple(deps.get_sensitive_values(chat_id))
    question = sanitize_personal_data(text, known)
    if not question:
        deps.send_buttons(chat_id, "Напишите вопрос без персональных данных.", _fallback_buttons(deps))
        return
    if not deps.reserve_question(chat_id, int(settings["daily_limit"])):
        state = deps.get_state(chat_id)
        state["ai_last_exchange"] = {"question": question, "answer": ""}
        deps.touch(state)
        deps.send_buttons(
            chat_id,
            "Вы достигли дневного лимита вопросов к ИИ. Вы можете оформить обращение.",
            _fallback_buttons(deps),
        )
        return
    session = deps.get_session(chat_id)
    history = [
        {"role": item["role"], "text": sanitize_personal_data(item["text"], known)}
        for item in session.get("history", [])
        if item.get("role") in {"user", "assistant"} and item.get("text")
    ]
    faq_context = sanitize_personal_data(session.get("faq_context") or "", known) or None
    try:
        raw_answer = deps.complete(
            settings=settings,
            history=history,
            question=question,
            faq_context=faq_context,
        )
    except AIServiceError:
        deps.release_question(chat_id)
        state = deps.get_state(chat_id)
        state["ai_last_exchange"] = {"question": question, "answer": ""}
        deps.touch(state)
        deps.logger.warning("YandexGPT request failed chat_id=%s", chat_id)
        deps.send_buttons(
            chat_id,
            "⚠️ ИИ-помощник временно недоступен. Попробуйте позже или оформите обращение.",
            _fallback_buttons(deps),
        )
        return
    answer = sanitize_personal_data(raw_answer, known)
    deps.append_exchange(chat_id, question, answer)
    state = deps.get_state(chat_id)
    state["state"] = deps.question_state
    state["ai_last_exchange"] = {"question": question, "answer": answer}
    deps.touch(state)
    deps.send_buttons(chat_id, f"🤖 {answer}", _answer_buttons(deps))


def new_dialog(chat_id: int, deps: AIDependencies) -> None:
    deps.clear_history(chat_id)
    state = deps.get_state(chat_id)
    state.pop("ai_last_exchange", None)
    state["state"] = deps.question_state
    deps.touch(state)
    deps.send_buttons(
        chat_id,
        "🧹 Контекст диалога очищен. Задайте новый вопрос. Использованные сегодня вопросы остаются в счётчике лимита.",
        [[deps.make_callback("🏠 Главное меню", "main_menu")]],
    )


def create_appeal_draft(chat_id: int, deps: AIDependencies) -> None:
    exchange = deps.get_state(chat_id).get("ai_last_exchange") or {}
    question = exchange.get("question", "")
    answer = exchange.get("answer", "")
    if question or answer:
        draft = f"Вопрос пользователя:\n{question}\n\nОтвет ИИ-помощника:\n{answer}".strip()
    else:
        draft = "Нужна консультация по вопросу ЖКХ."
    deps.start_appeal_draft(chat_id, draft)
