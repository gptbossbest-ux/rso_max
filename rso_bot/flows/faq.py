"""FAQ tree flow for the MAX bot.

The flow owns the FAQ state transitions and presentation rules, while all
external operations are supplied by the entry point.  Keeping the dependency
boundary explicit lets the legacy ``bot.py`` wrappers retain their runtime
patch points without introducing a circular import.
"""

from __future__ import annotations

import logging
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

State = dict[str, Any]
Button = dict[str, Any]
PAGE_ROWS = 29
_TOKEN_PATTERN = re.compile(r"[0-9a-f]{8}\Z", re.ASCII)


def _valid_render_token(value: Any) -> bool:
    return isinstance(value, str) and _TOKEN_PATTERN.fullmatch(value) is not None


@dataclass(frozen=True)
class FaqDependencies:
    """Collaborators and state constants required by the FAQ flow."""

    list_scripts: Callable[[], tuple[list[dict[str, Any]], Any]]
    get_script_tree: Callable[[int], tuple[dict[str, Any] | None, Any]]
    get_state: Callable[[int], State]
    touch: Callable[[State], State]
    make_callback: Callable[[str, str], Button]
    make_link: Callable[[str, str], Button]
    send_message: Callable[[int, str], Any]
    send_buttons: Callable[[int, str, list[list[Button]]], Any]
    send_main_menu: Callable[..., None]
    logger: logging.Logger
    script_list_state: str
    script_node_state: str
    menu_state: str
    operator_available: Callable[[], bool] = lambda: False
    ai_available: Callable[[], bool] = lambda: True


def _safe_callback(deps: FaqDependencies, label: Any, payload: str) -> Button | None:
    try:
        return deps.make_callback(label, payload)
    except (TypeError, ValueError):
        deps.logger.warning("FAQ action skipped because its button label is invalid")
        return None


def _send_page(
    chat_id: int,
    text: str,
    rows: list[list[Button]],
    page: int,
    payload_prefix: str,
    deps: FaqDependencies,
) -> bool:
    """Send exactly one interactive page while retaining every valid action."""
    if len(rows) <= 30:
        if page != 0:
            return False
        deps.send_buttons(chat_id, text, rows)
        return True
    page_count = (len(rows) + PAGE_ROWS - 1) // PAGE_ROWS
    if page < 0 or page >= page_count:
        return False
    page_rows = rows[page * PAGE_ROWS:(page + 1) * PAGE_ROWS]
    navigation = []
    if page:
        navigation.append(deps.make_callback("⬅️ Назад", f"{payload_prefix}:{page - 1}"))
    if page + 1 < page_count:
        navigation.append(deps.make_callback("Далее ➡️", f"{payload_prefix}:{page + 1}"))
    page_rows.append(navigation)
    deps.send_buttons(chat_id, f"{text}\n\nСтраница {page + 1} из {page_count}", page_rows)
    return True


def show_scripts_list(chat_id: int, deps: FaqDependencies) -> None:
    """Load active FAQ scripts and display them as callback buttons."""
    scripts, err = deps.list_scripts()
    if err or not scripts:
        if err:
            deps.send_message(chat_id, "⚠️ Сервис временно недоступен.")
        else:
            deps.send_message(chat_id, "📚 Раздел FAQ пуст.")
        deps.send_main_menu(chat_id)
        return

    state = deps.get_state(chat_id)
    state["state"] = deps.script_list_state
    deps.touch(state)

    valid_scripts = []
    rows = []
    for script in scripts:
        button = _safe_callback(deps, script.get("title"), f"script:{script['id']}")
        if button:
            valid_scripts.append(script)
            rows.append([button])
    rows.append([deps.make_callback("🏠 Главное меню", "main_menu")])
    state["faq_scripts"] = valid_scripts
    state["faq_scripts_token"] = secrets.token_hex(4)
    _send_page(
        chat_id, "📚 Выберите тему:", rows, 0,
        f"faq_scripts_page:{state['faq_scripts_token']}", deps,
    )


def show_scripts_page(chat_id: int, token: str, page: int, deps: FaqDependencies) -> None:
    state = deps.get_state(chat_id)
    if (
        state.get("state") != deps.script_list_state
        or "faq_scripts" not in state
        or not _valid_render_token(token)
        or not secrets.compare_digest(str(state.get("faq_scripts_token", "")), token)
    ):
        state["state"] = deps.menu_state
        state.pop("faq_scripts", None)
        deps.touch(state)
        deps.send_message(chat_id, "Эта страница FAQ устарела.")
        deps.send_main_menu(chat_id)
        return
    rows = []
    for script in state["faq_scripts"]:
        button = _safe_callback(deps, script.get("title"), f"script:{script['id']}")
        if button:
            rows.append([button])
    rows.append([deps.make_callback("🏠 Главное меню", "main_menu")])
    if not _send_page(
        chat_id, "📚 Выберите тему:", rows, page,
        f"faq_scripts_page:{token}", deps,
    ):
        deps.send_message(chat_id, "Неверная страница FAQ.")


def open_script(chat_id: int, script_id: int, deps: FaqDependencies) -> None:
    """Load an FAQ tree, select its root and render the root node."""
    tree, err = deps.get_script_tree(script_id)
    if err or not tree:
        deps.send_message(chat_id, "⚠️ Не удалось загрузить скрипт.")
        deps.send_main_menu(chat_id)
        return

    nodes = {node["id"]: node for node in tree.get("nodes", [])}
    edges_by_from: dict[int, list[dict[str, Any]]] = {}
    for edge in tree.get("edges", []):
        edges_by_from.setdefault(edge["from_node_id"], []).append(edge)

    if not nodes:
        deps.send_message(chat_id, "Скрипт пуст.")
        deps.send_main_menu(chat_id)
        return

    all_to = {edge["to_node_id"] for edge in tree.get("edges", [])}
    roots = [node_id for node_id in nodes if node_id not in all_to]
    if len(roots) > 1:
        deps.logger.warning(
            "Скрипт id=%s: найдено %d корневых узлов, берём минимальный",
            script_id,
            len(roots),
        )
    root_id = min(roots) if roots else min(nodes)

    state = deps.get_state(chat_id)
    state["state"] = deps.script_node_state
    state["script"] = {
        "id": script_id,
        "render_token": secrets.token_hex(4),
        "nodes": nodes,
        "edges_by_from": edges_by_from,
        "current": root_id,
        "title": tree.get("title", "FAQ"),
        "path": [tree.get("title", "FAQ"), nodes[root_id]["title"]],
    }
    deps.touch(state)
    show_script_node(chat_id, deps)


def show_script_node(chat_id: int, deps: FaqDependencies, page: int = 0) -> None:
    """Render the current FAQ node and its available transitions."""
    state = deps.get_state(chat_id)
    script = state.get("script", {})
    nodes = script.get("nodes", {})
    edges_by_from = script.get("edges_by_from", {})
    current_id = script.get("current")

    node = nodes.get(current_id)
    if not node:
        deps.send_message(chat_id, "Скрипт завершён.")
        deps.send_main_menu(chat_id)
        return

    text = node["title"]
    edges = edges_by_from.get(current_id, [])
    link_rows = []
    if node.get("link_url"):
        try:
            link_rows = [[deps.make_link(
                node.get("link_text") or "Открыть сайт", node["link_url"],
            )]]
        except (TypeError, ValueError):
            deps.logger.warning("FAQ node id=%s contains an invalid legacy link", current_id)

    if node.get("is_terminal") or not edges:
        path = list(script.get("path", []))
        state["ai_faq_context"] = " → ".join(str(item) for item in path if item)
        state["state"] = deps.menu_state
        state.pop("script", None)
        deps.touch(state)
        invitation = (
            "\n\nЕсли вы не получили ответ на ваш вопрос, "
            "вы можете обратиться к ИИ-помощнику."
            if deps.ai_available() else ""
        )
        rows = (
            link_rows
            + ([[deps.make_callback("🤖 Спросить у ИИ-помощника", "ai_from_faq")]] if deps.ai_available() else [])
            + ([[deps.make_callback("🎧 Связаться с оператором", "operator_start")]] if deps.operator_available() else [])
            + [[deps.make_callback("🏠 Главное меню", "main_menu")]]
        )
        _send_page(
            chat_id, f"📌 {text}{invitation}", rows, 0,
            f"faq_node_page:{script.get('id')}:{current_id}:{script.get('render_token')}", deps,
        )
    else:
        rows = list(link_rows)
        for edge in edges:
            button = _safe_callback(
                deps, edge.get("label"),
                f"faq_go:{script.get('id')}:{current_id}:{edge['to_node_id']}:{script.get('render_token')}",
            )
            if button:
                rows.append([button])
        rows.append([deps.make_callback("🏠 Главное меню", "main_menu")])
        if not _send_page(
            chat_id, f"📌 {text}", rows, page,
            f"faq_node_page:{script.get('id')}:{current_id}:{script.get('render_token')}", deps,
        ):
            deps.send_message(chat_id, "Неверная страница FAQ.")


def show_node_page(
    chat_id: int, script_id: int, node_id: int, token: str, page: int,
    deps: FaqDependencies,
) -> None:
    state = deps.get_state(chat_id)
    script = state.get("script", {})
    if (
        state.get("state") != deps.script_node_state
        or script.get("id") != script_id
        or script.get("current") != node_id
        or not _valid_render_token(token)
        or not secrets.compare_digest(str(script.get("render_token", "")), token)
    ):
        state["state"] = deps.menu_state
        state.pop("script", None)
        deps.touch(state)
        deps.send_message(chat_id, "Эта страница FAQ устарела.")
        deps.send_main_menu(chat_id)
        return
    show_script_node(chat_id, deps, page=page)


def navigate_script_node(
    chat_id: int,
    node_id: int,
    deps: FaqDependencies,
) -> None:
    """Move the active FAQ session to a node selected by callback payload."""
    state = deps.get_state(chat_id)
    if "script" not in state:
        deps.send_main_menu(chat_id)
        return
    script = state["script"]
    current_id = script.get("current")
    selected_label = next(
        (
            edge.get("label")
            for edge in script.get("edges_by_from", {}).get(current_id, [])
            if edge.get("to_node_id") == node_id
        ),
        None,
    )
    if selected_label:
        script.setdefault("path", []).append(selected_label)
    target = script.get("nodes", {}).get(node_id)
    if target:
        script.setdefault("path", []).append(target.get("title", ""))
    script["current"] = node_id
    script["render_token"] = secrets.token_hex(4)
    deps.touch(state)
    show_script_node(chat_id, deps)


def navigate_bound_action(
    chat_id: int,
    script_id: int,
    current_id: int,
    target_id: int,
    token: str,
    deps: FaqDependencies,
) -> None:
    state = deps.get_state(chat_id)
    script = state.get("script", {})
    valid_edge = any(
        edge.get("to_node_id") == target_id
        for edge in script.get("edges_by_from", {}).get(current_id, [])
    )
    if (
        state.get("state") != deps.script_node_state
        or script.get("id") != script_id
        or script.get("current") != current_id
        or not _valid_render_token(token)
        or not secrets.compare_digest(str(script.get("render_token", "")), token)
        or not valid_edge
    ):
        deps.send_message(chat_id, "Эта кнопка FAQ устарела.")
        return
    navigate_script_node(chat_id, target_id, deps)
