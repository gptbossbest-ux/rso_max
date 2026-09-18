"""Meter-reading flow for the MAX bot.

Update parsing and routing stay in :mod:`bot`.  This module owns the meter
selection, value entry, validation, confirmation and persistence transitions.
Every collaborator is supplied explicitly so importing the flow never starts
the bot or creates a circular dependency.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

State = dict[str, Any]
Button = dict[str, Any]
Record = Mapping[str, Any]


@dataclass(frozen=True)
class ReadingDependencies:
    """Database, session, transport and continuation dependencies."""

    get_meters: Callable[[str], Sequence[Record]]
    get_last_reading: Callable[[str, str], Record | None]
    add_reading: Callable[..., Any]
    get_state: Callable[[int], State]
    touch: Callable[[State], State]
    reset_meter_input: Callable[[State], None]
    clear_flow: Callable[[State], None]
    get_saved_ls: Callable[[int], str | None]
    request_ls: Callable[[int, str], None]
    # Calls through entry-point wrappers preserve runtime monkeypatch seams.
    parse_input: Callable[[int, str], float | None]
    get_current_value: Callable[[str, Record, str, str], float]
    show_meter_select: Callable[[int, str], None]
    ask_meter_value: Callable[[int], None]
    confirm_meter_reading: Callable[[int], None]
    make_callback: Callable[[str, str], Button]
    send_message: Callable[[int, str], Any]
    send_buttons: Callable[[int, str, list[list[Button]]], Any]
    send_main_menu: Callable[..., None]
    logger: logging.Logger
    integration_enabled: bool
    meter_select_state: str
    waiting_value1_state: str
    waiting_value2_state: str
    confirm_state: str


def parse_reading(chat_id: int, text: str, deps: ReadingDependencies) -> float | None:
    """Parse a reading, retaining the legacy ``float`` input semantics."""
    try:
        return float(text.replace(",", "."))
    except ValueError:
        deps.send_message(chat_id, "Введите числовое значение.")
        return None


def current_reading(
    account: str,
    meter: Record,
    column: str,
    initial_key: str,
    deps: ReadingDependencies,
) -> float:
    """Return the latest non-empty reading or the meter's initial value."""
    last = deps.get_last_reading(account, str(meter["meter_number"]))
    if last and last[column]:
        return float(last[column])
    return float(meter.get(initial_key, "0") or "0")


def start(chat_id: int, deps: ReadingDependencies) -> None:
    """Start readings for a saved account, or defer until account binding."""
    account = deps.get_saved_ls(chat_id)
    if not account:
        deps.request_ls(chat_id, "pokazaniya")
        return
    deps.show_meter_select(chat_id, account)


def show_meter_select(chat_id: int, account: str, deps: ReadingDependencies) -> None:
    """Load account meters and present the meter selection keyboard."""
    meters = deps.get_meters(account)
    if not meters:
        deps.send_message(chat_id, "По вашему счёту счётчики не найдены.")
        deps.send_main_menu(chat_id)
        return

    meter_list = [dict(meter) for meter in meters]
    state = deps.get_state(chat_id)
    state["state"] = deps.meter_select_state
    state["meters"] = meter_list
    deps.touch(state)

    rows = []
    for index, meter in enumerate(meter_list):
        suffix = " (2Т)" if meter["meter_type"] == "Двухтарифный" else ""
        rows.append(
            [
                deps.make_callback(
                    f"{meter['resource_type']} №{meter['meter_number']}{suffix}",
                    f"meter:{index}",
                )
            ]
        )
    rows.append([deps.make_callback("🏠 Главное меню", "main_menu")])
    deps.send_buttons(chat_id, "Выберите счётчик:", rows)


def ask_meter_value(chat_id: int, deps: ReadingDependencies) -> None:
    """Prompt for T1/single-tariff value or T2 according to session state."""
    state = deps.get_state(chat_id)
    meter = state["meters"][state["meter_idx"]]
    waiting_v2 = state.get("state") == deps.waiting_value2_state

    resource = meter["resource_type"]
    number = meter["meter_number"]
    is_two_tariff = meter["meter_type"] == "Двухтарифный"

    if deps.integration_enabled:
        current_info = ""
    else:
        last = deps.get_last_reading(state["ls"], number)
        if last:
            if is_two_tariff and last["value2"]:
                current_info = f"Текущие: Т1={last['value1']}, Т2={last['value2']}"
            else:
                current_info = f"Текущее показание: {last['value1']}"
            current_info += f" (от {(last['created_at'] or '')[:10]})"
        else:
            initial = meter.get("initial2" if waiting_v2 else "initial1", "0")
            current_info = f"Начальное: {initial}"

    info_line = f"\n{current_info}" if current_info else ""
    if waiting_v2:
        prompt = f"{resource} №{number}{info_line}\nВведите Т2 (ночь):"
    elif is_two_tariff:
        prompt = f"{resource} №{number} (двухтарифный){info_line}\nВведите Т1 (день):"
    else:
        prompt = f"{resource} №{number}{info_line}\nВведите показание:"
    deps.send_message(chat_id, prompt)


def confirm_meter_reading(chat_id: int, deps: ReadingDependencies) -> None:
    """Show the confirmation keyboard for the values in session state."""
    state = deps.get_state(chat_id)
    meter = state["meters"][state["meter_idx"]]
    value1 = state.get("new_value1")
    value2 = state.get("new_value2")

    if meter["meter_type"] == "Двухтарифный":
        summary = (
            f"{meter['resource_type']} №{meter['meter_number']}: "
            f"Т1={value1}, Т2={value2}"
        )
    else:
        summary = f"{meter['resource_type']} №{meter['meter_number']}: {value1}"

    state["state"] = deps.confirm_state
    deps.touch(state)
    deps.send_buttons(
        chat_id,
        f"Проверьте показания:\n{summary}",
        [
            [deps.make_callback("✅ Подтвердить", "meter_confirm")],
            [deps.make_callback("✏️ Скорректировать", "meter_retry")],
        ],
    )


def select_meter(
    chat_id: int,
    state: State,
    argument: str,
    deps: ReadingDependencies,
) -> None:
    """Select a meter and reset value entry to T1."""
    state["meter_idx"] = int(argument)
    deps.reset_meter_input(state)
    deps.touch(state)
    deps.ask_meter_value(chat_id)


def confirm(chat_id: int, state: State, deps: ReadingDependencies) -> None:
    """Persist confirmed values and return to the account's meter list."""
    if state.get("state") != deps.confirm_state:
        return

    meter = state["meters"][state["meter_idx"]]
    deps.add_reading(
        chat_id,
        state["ls"],
        meter["resource_type"],
        meter["meter_number"],
        state.get("new_value1"),
        state.get("new_value2"),
    )
    if deps.integration_enabled:
        deps.logger.info(
            "Показания сохранены в очередь 1С: ЛС=%s  счётчик=%s  chat_id=%s",
            state["ls"],
            meter["meter_number"],
            chat_id,
        )
    else:
        deps.logger.info(
            "Показания приняты: ЛС=%s  счётчик=%s  chat_id=%s",
            state["ls"],
            meter["meter_number"],
            chat_id,
        )

    account = state["ls"]
    deps.clear_flow(state)
    deps.touch(state)
    if deps.integration_enabled:
        deps.send_message(
            chat_id,
            f"✅ Показания по счётчику {meter['resource_type']} "
            f"№{meter['meter_number']} сохранены и ожидают обработки в 1С.",
        )
    else:
        deps.send_message(
            chat_id,
            f"✅ Показания по счётчику {meter['resource_type']} "
            f"№{meter['meter_number']} приняты!",
        )
    deps.show_meter_select(chat_id, account)


def retry(chat_id: int, state: State, deps: ReadingDependencies) -> None:
    """Reset the current meter's values and ask for T1 again."""
    if state.get("state") != deps.confirm_state:
        return
    deps.reset_meter_input(state)
    deps.touch(state)
    deps.ask_meter_value(chat_id)


def on_value1(
    chat_id: int,
    state: State,
    text: str,
    deps: ReadingDependencies,
) -> None:
    """Accept T1 or a single-tariff value and advance the flow."""
    meter = state["meters"][state["meter_idx"]]
    value = deps.parse_input(chat_id, text)
    if value is None:
        return

    current = deps.get_current_value(state.get("ls", ""), meter, "value1", "initial1")
    if not deps.integration_enabled and value < current:
        deps.send_message(
            chat_id,
            f"Показание {value} не может быть меньше текущего {current}.\n"
            "Введите корректное значение:",
        )
        return

    state["new_value1"] = str(value)
    if meter["meter_type"] == "Двухтарифный":
        state["state"] = deps.waiting_value2_state
        deps.touch(state)
        deps.ask_meter_value(chat_id)
    else:
        state["new_value2"] = None
        deps.touch(state)
        deps.confirm_meter_reading(chat_id)


def on_value2(
    chat_id: int,
    state: State,
    text: str,
    deps: ReadingDependencies,
) -> None:
    """Accept T2 and advance the flow to confirmation."""
    meter = state["meters"][state["meter_idx"]]
    value = deps.parse_input(chat_id, text)
    if value is None:
        return

    current = deps.get_current_value(state.get("ls", ""), meter, "value2", "initial2")
    if not deps.integration_enabled and value < current:
        deps.send_message(
            chat_id,
            f"Показание Т2 {value} не может быть меньше текущего {current}.\n"
            "Введите корректное значение:",
        )
        return

    state["new_value2"] = str(value)
    deps.touch(state)
    deps.confirm_meter_reading(chat_id)
