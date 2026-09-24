"""Appeal creation, listing and lifecycle flow for the MAX bot.

The entry point owns routing and supplies all external collaborators.  This
module owns only appeal-specific state transitions and presentation rules.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from rso_bot.flows.auth import LsValidation

State = dict[str, Any]
Button = dict[str, Any]
ApiResult = tuple[dict[str, Any] | None, Any]


@dataclass(frozen=True)
class AppealDependencies:
    """Collaborators and state constants required by the appeal flow."""

    create_appeal: Callable[..., ApiResult]
    list_appeals_by_ls: Callable[[str], ApiResult]
    confirm_appeal: Callable[[int, str, int], ApiResult]
    reopen_appeal: Callable[[int, str], ApiResult]
    get_state: Callable[[int], State]
    touch: Callable[[State], State]
    get_saved_ls: Callable[[int], str | None]
    request_ls: Callable[[int, str], None]
    check_ls_brute: Callable[[int], str | None]
    validate_ls: Callable[[str], bool | LsValidation]
    fail_ls: Callable[[int], str]
    reset_ls_brute: Callable[[int], None]
    save_ls: Callable[[int, str], None]
    # Calls the entry-point wrapper so existing runtime monkeypatch seams remain.
    submit_appeal: Callable[[int, str], None]
    make_callback: Callable[[str, str], Button]
    send_message: Callable[[int, str], Any]
    send_buttons: Callable[[int, str, list[list[Button]]], Any]
    send_main_menu: Callable[..., None]
    logger: logging.Logger
    categories: Mapping[str, str]
    category_state: str
    body_state: str
    reopen_comment_state: str
    menu_state: str


def start_appeal(
    chat_id: int,
    deps: AppealDependencies,
    draft_body: str | None = None,
) -> None:
    """Start appeal creation by asking the user for a category."""
    state = deps.get_state(chat_id)
    state["state"] = deps.category_state
    if draft_body:
        state["appeal"] = {"body": draft_body, "is_draft": True}
    else:
        state.pop("appeal", None)
    deps.touch(state)

    rows = [
        [deps.make_callback(label, f"cat:{value}")]
        for label, value in deps.categories.items()
    ]
    rows.append([deps.make_callback("❌ Отмена", "cancel")])
    deps.send_buttons(chat_id, "Выберите категорию обращения:", rows)


def set_category(chat_id: int, category: str, deps: AppealDependencies) -> None:
    """Store the chosen category and ask for the appeal body."""
    state = deps.get_state(chat_id)
    state["state"] = deps.body_state
    appeal = state.setdefault("appeal", {})
    appeal["category"] = category
    deps.touch(state)
    if appeal.get("is_draft") and appeal.get("body"):
        deps.send_buttons(
            chat_id,
            "Черновик обращения:\n\n"
            f"{appeal['body']}\n\nОтправить его или изменить?",
            [
                [deps.make_callback("✅ Отправить черновик", "appeal_draft_submit")],
                [deps.make_callback("✏️ Изменить текст", "appeal_draft_edit")],
                [deps.make_callback("❌ Отмена", "cancel")],
            ],
        )
    else:
        deps.send_message(chat_id, "Опишите вашу проблему или вопрос:")


def submit_draft(chat_id: int, deps: AppealDependencies) -> None:
    state = deps.get_state(chat_id)
    appeal = state.get("appeal", {})
    if not appeal.get("body"):
        start_appeal(chat_id, deps)
        return
    account = deps.get_saved_ls(chat_id)
    if account:
        deps.submit_appeal(chat_id, account)
    else:
        deps.request_ls(chat_id, "appeal")


def edit_draft(chat_id: int, deps: AppealDependencies) -> None:
    state = deps.get_state(chat_id)
    state["state"] = deps.body_state
    deps.touch(state)
    deps.send_message(chat_id, "Отправьте изменённый текст обращения:")


def got_body(chat_id: int, text: str, deps: AppealDependencies) -> None:
    """Store the body and submit immediately when an account is already bound."""
    state = deps.get_state(chat_id)
    state["appeal"]["body"] = text
    deps.touch(state)

    account = deps.get_saved_ls(chat_id)
    if account:
        deps.submit_appeal(chat_id, account)
    else:
        # The entry point decides whether this is local validation or 1C auth;
        # appeal context remains in state across either deferred flow.
        deps.request_ls(chat_id, "appeal")


def got_ls(chat_id: int, ls_input: str, deps: AppealDependencies) -> None:
    """Validate an entered account number and resume appeal submission."""
    block_message = deps.check_ls_brute(chat_id)
    if block_message:
        deps.send_message(chat_id, block_message)
        return

    validation = deps.validate_ls(ls_input)
    if validation is LsValidation.UNAVAILABLE:
        deps.send_message(chat_id, "⚠️ Сервис временно недоступен. Попробуйте позже.")
        return
    if not validation:
        deps.send_message(chat_id, deps.fail_ls(chat_id))
        deps.send_message(chat_id, "Введите номер лицевого счёта повторно:")
        return

    deps.reset_ls_brute(chat_id)
    deps.save_ls(chat_id, ls_input)
    deps.submit_appeal(chat_id, ls_input)


def submit_appeal(chat_id: int, ls: str, deps: AppealDependencies) -> None:
    """Create an appeal through the internal API and finish the flow."""
    state = deps.get_state(chat_id)
    appeal = state.get("appeal", {})

    data, error = deps.create_appeal(
        ls=ls,
        channel="max",
        category=appeal.get("category", "прочее"),
        body=appeal.get("body", ""),
        chat_id=chat_id,
    )

    state["state"] = deps.menu_state
    state.pop("appeal", None)
    deps.touch(state)

    if error:
        deps.logger.error("create_appeal chat_id=%s err=%s", chat_id, error)
        deps.send_message(chat_id, "⚠️ Сервис временно недоступен. Попробуйте позже.")
    else:
        ticket = data["ticket_no"]
        deps.send_message(
            chat_id,
            f"✅ Обращение принято!\n"
            f"Номер: {ticket}\n\n"
            f"Мы свяжемся с вами в ближайшее время.",
        )
        deps.logger.info("Создано обращение %s  chat_id=%s", ticket, chat_id)

    deps.send_main_menu(chat_id)


def show_my_appeals(chat_id: int, deps: AppealDependencies) -> None:
    """Show active appeals for the bound account."""
    account = deps.get_saved_ls(chat_id)
    if not account:
        deps.request_ls(chat_id, "my_appeals")
        return

    data, error = deps.list_appeals_by_ls(account)
    if error:
        deps.send_message(chat_id, "⚠️ Сервис временно недоступен. Попробуйте позже.")
        deps.send_main_menu(chat_id)
        return

    items = data.get("appeals", [])
    active = [
        item for item in items if item.get("status") not in ("resolved", "closed")
    ]

    if not active:
        deps.send_message(chat_id, "✅ У вас нет активных обращений.")
    else:
        status_labels = {
            "new": "🆕 Новое",
            "in_work": "⚙️ В работе",
            "pending_confirmation": "⏳ На подтверждении",
        }
        lines = ["📋 Ваши активные обращения:\n"]
        for item in active:
            body = item.get("body") or ""
            preview = body[:60] + ("..." if len(body) > 60 else "")
            status = item.get("status", "")
            lines.append(
                f"№ {item.get('ticket_no', '?')} — {status_labels.get(status, status)}\n"
                f"📝 {preview}\n"
                f"📅 {(item.get('created_at') or '')[:16]}\n"
            )
        deps.send_message(chat_id, "\n".join(lines))

    deps.send_main_menu(chat_id)


def confirm_appeal(
    chat_id: int,
    state: State,
    arg: str,
    deps: AppealDependencies,
) -> None:
    """Confirm closure of an appeal selected by callback payload."""
    del state  # Callback signature is intentionally retained for routing compatibility.
    data, error = deps.confirm_appeal(int(arg), "max", chat_id)
    if error:
        deps.send_message(chat_id, f"⚠️ Не удалось подтвердить закрытие: {error}")
    else:
        deps.send_message(
            chat_id, f"✅ Обращение №{data['ticket_no']} закрыто.\nСпасибо!"
        )
    deps.send_main_menu(chat_id)


def begin_reopen(
    chat_id: int,
    state: State,
    arg: str,
    deps: AppealDependencies,
) -> None:
    """Remember the appeal selected for reopening and ask for a reason."""
    state["state"] = deps.reopen_comment_state
    state["reopen_appeal_id"] = int(arg)
    deps.touch(state)
    deps.send_message(
        chat_id, "Опишите, пожалуйста, причину возврата обращения в работу:"
    )


def on_reopen_comment(
    chat_id: int,
    state: State,
    text: str,
    deps: AppealDependencies,
) -> None:
    """Submit the reopen reason and return to the main menu."""
    appeal_id = state.pop("reopen_appeal_id", None)
    state["state"] = deps.menu_state
    deps.touch(state)

    if appeal_id:
        data, error = deps.reopen_appeal(appeal_id, text)
        if error:
            deps.send_message(chat_id, f"⚠️ Не удалось вернуть обращение: {error}")
        else:
            deps.send_message(
                chat_id,
                f"↩️ Обращение №{data['ticket_no']} возвращено в работу.\n"
                f"Ваш комментарий: {text}",
            )
    deps.send_main_menu(chat_id)
