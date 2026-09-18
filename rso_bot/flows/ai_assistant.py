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


class PersonalDataDetected(ValueError):
    """Input may contain PII that cannot be safely sent to an external API."""


_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-zА-Яа-я]{2,}(?!\w)")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+7|8)[\s()\-]*\d{3}[\s()\-]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)")
_ACCOUNT_RE = re.compile(
    r"(?i)\b(?:лицев(?:ой|ого)\s+сч(?:ё|е)т(?:а)?|л\.?\s*с\.?)"
    r"\s*[:№#-]?\s*[A-ZА-Я0-9][A-ZА-Я0-9_./\\-]{0,63}"
)
_LONG_ID_RE = re.compile(r"(?<!\d)\d{6,20}(?!\d)")
_FULL_NAME_RE = re.compile(
    r"\b[A-ZА-ЯЁ][a-zа-яё-]+\s+[A-ZА-ЯЁ][a-zа-яё-]+\s+[A-ZА-ЯЁ][a-zа-яё-]+\b"
)
_TWO_PART_NAME_RE = re.compile(
    r"\b[A-ZА-ЯЁ][a-zа-яё-]{1,}\s+[A-ZА-ЯЁ][a-zа-яё-]{1,}\b"
)
_INITIALS_NAME_RE = re.compile(
    r"\b(?:[А-ЯЁ][а-яё-]+\s+[А-ЯЁ]\.\s*[А-ЯЁ]\.?|"
    r"[А-ЯЁ]\.\s*[А-ЯЁ]\.?\s+[А-ЯЁ][а-яё-]+)",
    re.IGNORECASE,
)
_INTRO_NAME_RE = re.compile(
    r"(?i)\b(?:меня\s+зовут|моё\s+имя)\s+[а-яё-]+(?:\s+[а-яё-]+){1,2}"
)
_CONTEXTUAL_NAME_RE = re.compile(
    r"(?i)\b(?P<first>[а-яё][а-яё-]{1,})\s+"
    r"(?P<second>[а-яё][а-яё-]{1,})(?="
    r"\s*,\s*(?:нет|не\s+работает|отсутствует)\b|"
    r"\s+(?:жалуется|сообщает|просит|обратился|обратилась|проживает)\b)"
)
_GENERIC_UTILITY_WORDS = frozenset(
    {
        "весь",
        "вся",
        "все",
        "без",
        "дом",
        "горячая",
        "горячей",
        "жильцы",
        "жители",
        "квартира",
        "лифт",
        "мусор",
        "мусора",
        "мой",
        "моя",
        "наш",
        "наша",
        "отопление",
        "отопления",
        "отходов",
        "отходы",
        "подъезд",
        "ремонт",
        "свет",
        "света",
        "сосед",
        "соседи",
        "соседка",
        "тепла",
        "тепло",
        "вода",
        "воды",
        "холодная",
        "холодной",
        "электричество",
        "электричества",
        "этот",
        "эта",
    }
)
_STRUCTURED_ADDRESS_RE = re.compile(
    r"(?i)\b(?:"
    r"(?:живу|нахожусь|проживаю|по\s+адресу)\s+"
    r"(?:(?:улиц(?:а|е)|ул\.)\s+)?"
    r"[а-яё][а-яё-]{1,}(?:\s+[а-яё][а-яё-]{1,}){0,2}"
    r"|(?:на|по)\s+(?:"
    r"(?:улиц(?:а|е)|ул\.|проспект(?:е)?|пр-т|переулок|пер\.)\s+"
    r"[а-яё][а-яё-]{1,}(?:\s+[а-яё][а-яё-]{1,}){0,2}"
    r"|(?:мира|победы|[а-яё][а-яё-]*(?:ина|ова|ева|ёва|ского|цкого|ская|ское))"
    r")"
    r")\s+(?:(?:д(?:ом)?\.?)\s*)?№?\s*\d{1,4}(?:[/.-]\d{1,4})?\b"
)
_ADDRESS_RE = re.compile(
    r"(?i)\b(?:адрес|улица|ул\.|проспект|пр-т|переулок|пер\.)"
    r"\s*[:,-]?\s*[^\n;]{1,120}"
)
_HOUSE_NUMBER_RE = re.compile(
    r"(?i)\b(дом(?:е)?|д\.|\bквартир(?:а|е|ы)?|кв\.)\s*№?\s*\d+[A-Za-zА-Яа-яЁё/-]*"
)
_UNMARKED_ADDRESS_RE = re.compile(
    r"\b[А-ЯЁ][а-яё]{2,}(?:-[А-ЯЁ]?[а-яё]+)*\s*,?\s*(?:д\.?\s*)?\d{1,4}(?:[/.-]\d{1,4})?\b"
)
_LABELED_NAME_RE = re.compile(r"(?i)\b(?:фио|получатель|собственник)\s*[:,-]?\s*[A-ZА-ЯЁ][A-Za-zА-яЁё-]+(?:\s+[A-ZА-ЯЁ][A-Za-zА-яЁё-]+){1,2}")
_REDACTION_RE = re.compile(
    r"(?i)(?:лицевой\s+счёт|фио|адрес)?\s*\[[^\]]*удал[^\]]*\]"
)
_RESIDUAL_PII_HINT_RE = re.compile(
    r"(?i)(?:\bфио\b|лицев\w*\s+сч|л\.?\s*с\.?|адрес|"
    r"(?<!\d)\d{3,5}(?!\d)|\b(?=[A-ZА-Я0-9/\\-]*\d)[A-ZА-Я0-9]{1,12}[-/\\][A-ZА-Я0-9/\\-]{1,20}\b)"
)
_SHORT_ALNUM_ID_RE = re.compile(
    r"\b(?=[A-Za-zА-Яа-яЁё0-9]{4,12}\b)(?=[A-Za-zА-Яа-яЁё]*\d)(?=\d*[A-Za-zА-Яа-яЁё])[A-Za-zА-Яа-яЁё0-9]+\b"
)
_YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
_AMBIGUOUS_ACCOUNT_RE = re.compile(
    r"(?i)\b(?:лицев(?:ой|ого)\s+сч(?:ё|е)т(?:а)?|л\.?\s*с\.?)"
    r"\s*[:№#-]?\s*[A-ZА-Я0-9]+\s+[A-ZА-Я0-9]+"
)


def _is_suspicious_contextual_name(match: re.Match[str]) -> bool:
    words = {match.group("first").lower(), match.group("second").lower()}
    return not words.intersection(_GENERIC_UTILITY_WORDS)


def _contains_suspicious_contextual_name(text: str) -> bool:
    return any(
        _is_suspicious_contextual_name(match)
        for match in _CONTEXTUAL_NAME_RE.finditer(text)
    )


def _redact_contextual_name(match: re.Match[str]) -> str:
    return "ФИО [удалено]" if _is_suspicious_contextual_name(match) else match.group(0)


def sanitize_personal_data(text: str, known_values: Iterable[str] = ()) -> str:
    """Redact obvious PII plus exact customer data known by the application."""
    cleaned = str(text or "")
    cleaned = _EMAIL_RE.sub("[эл. почта удалена]", cleaned)
    cleaned = _PHONE_RE.sub("[телефон удалён]", cleaned)
    cleaned = _ACCOUNT_RE.sub("лицевой счёт [удалён]", cleaned)
    cleaned = _LONG_ID_RE.sub("[номер удалён]", cleaned)
    cleaned = _LABELED_NAME_RE.sub("ФИО [удалено]", cleaned)
    cleaned = _INTRO_NAME_RE.sub("ФИО [удалено]", cleaned)
    cleaned = _CONTEXTUAL_NAME_RE.sub(_redact_contextual_name, cleaned)
    cleaned = _FULL_NAME_RE.sub("ФИО [удалено]", cleaned)
    cleaned = _INITIALS_NAME_RE.sub("ФИО [удалено]", cleaned)
    cleaned = _TWO_PART_NAME_RE.sub("ФИО [удалено]", cleaned)
    cleaned = _STRUCTURED_ADDRESS_RE.sub("адрес [удалён]", cleaned)
    cleaned = _ADDRESS_RE.sub("адрес [удалён]", cleaned)
    cleaned = _HOUSE_NUMBER_RE.sub(r"\1 [номер удалён]", cleaned)
    cleaned = _UNMARKED_ADDRESS_RE.sub("адрес [удалён]", cleaned)
    for value in sorted(
        {str(item).strip() for item in known_values if str(item).strip()},
        key=len,
        reverse=True,
    ):
        cleaned = re.sub(re.escape(value), "[ПДн удалены]", cleaned, flags=re.IGNORECASE)
        if re.fullmatch(r"[A-Za-zА-Яа-яЁё0-9_./\\-]+", value) and any(
            char.isdigit() for char in value
        ):
            compact = [char for char in value if char.isalnum()]
            if len(compact) >= 2:
                flexible = r"[\s._/\\-]*".join(re.escape(char) for char in compact)
                cleaned = re.sub(flexible, "[ПДн удалены]", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def sanitize_ai_input(text: str, known_values: Iterable[str] = ()) -> str:
    """Sanitize input or reject it when any ambiguous PII indicator remains."""
    raw_text = str(text or "")
    if (
        _AMBIGUOUS_ACCOUNT_RE.search(raw_text)
        or _contains_suspicious_contextual_name(raw_text)
        or _STRUCTURED_ADDRESS_RE.search(raw_text)
    ):
        raise PersonalDataDetected("contextual personal data")
    cleaned = sanitize_personal_data(text, known_values)
    inspectable = _REDACTION_RE.sub(" ", cleaned).strip(" ,;:.-")
    pii_inspection = _YEAR_RE.sub(" ", inspectable)
    if (
        not cleaned
        or not inspectable
        or _RESIDUAL_PII_HINT_RE.search(pii_inspection)
        or _SHORT_ALNUM_ID_RE.search(pii_inspection)
        or _TWO_PART_NAME_RE.search(pii_inspection)
        or _contains_suspicious_contextual_name(pii_inspection)
        or _STRUCTURED_ADDRESS_RE.search(pii_inspection)
        or _INITIALS_NAME_RE.search(pii_inspection)
        or _UNMARKED_ADDRESS_RE.search(pii_inspection)
    ):
        raise PersonalDataDetected("potential personal data")
    return cleaned


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
            if not isinstance(data, dict):
                raise AIServiceError("malformed response")
            result = data.get("result")
            if not isinstance(result, dict):
                raise AIServiceError("malformed response")
            alternatives = result.get("alternatives")
            if not isinstance(alternatives, list) or not alternatives:
                raise AIServiceError("malformed response")
            alternative = alternatives[0]
            if not isinstance(alternative, dict):
                raise AIServiceError("malformed response")
            message = alternative.get("message")
            if not isinstance(message, dict) or not isinstance(message.get("text"), str):
                raise AIServiceError("malformed response")
            answer = message["text"].strip()
            if not answer:
                raise AIServiceError("empty response")
            return answer
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise AIServiceError("provider request failed") from exc


@dataclass(frozen=True)
class AIDependencies:
    get_settings: Callable[[], dict[str, Any]]
    get_session: Callable[..., dict[str, Any]]
    set_context: Callable[[int, str | None], None]
    reserve_question: Callable[..., bool]
    release_question: Callable[..., None]
    append_exchange: Callable[..., None]
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
    get_operation_date: Callable[[], str]
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
        try:
            clean_context = sanitize_ai_input(faq_context, known)
        except PersonalDataDetected:
            clean_context = None
        deps.set_context(chat_id, clean_context)
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
    operation_date = deps.get_operation_date()
    settings = deps.get_settings()
    if not settings["enabled"] or not deps.is_configured(settings["model"]):
        deps.send_buttons(chat_id, "⚠️ ИИ-помощник сейчас недоступен.", _fallback_buttons(deps))
        return
    known = tuple(deps.get_sensitive_values(chat_id))
    try:
        question = sanitize_ai_input(text, known)
    except PersonalDataDetected:
        deps.send_buttons(
            chat_id,
            "В вопросе могут быть личные данные. Переформулируйте его без ФИО, адреса, телефона и номера лицевого счёта.",
            _fallback_buttons(deps),
        )
        return
    if not deps.reserve_question(
        chat_id,
        int(settings["daily_limit"]),
        session_date=operation_date,
    ):
        state = deps.get_state(chat_id)
        state["ai_last_exchange"] = {"question": question, "answer": ""}
        deps.touch(state)
        deps.send_buttons(
            chat_id,
            "Вы достигли дневного лимита вопросов к ИИ. Вы можете оформить обращение.",
            _fallback_buttons(deps),
        )
        return
    session = deps.get_session(
        chat_id,
        session_date=operation_date,
        create_if_missing=False,
    )
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
        answer = sanitize_personal_data(raw_answer, known)
        if not answer:
            raise AIServiceError("empty sanitized response")
    except AIServiceError:
        deps.release_question(chat_id, session_date=operation_date)
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
    deps.append_exchange(chat_id, question, answer, session_date=operation_date)
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
