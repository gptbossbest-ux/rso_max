"""FAQ tree flow for the MAX bot.

The flow owns the FAQ state transitions and presentation rules, while all
external operations are supplied by the entry point.  Keeping the dependency
boundary explicit lets the legacy ``bot.py`` wrappers retain their runtime
patch points without introducing a circular import.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

State = dict[str, Any]
Button = dict[str, Any]


@dataclass(frozen=True)
class FaqDependencies:
    """Collaborators and state constants required by the FAQ flow."""

    list_scripts: Callable[[], tuple[list[dict[str, Any]], Any]]
    get_script_tree: Callable[[int], tuple[dict[str, Any] | None, Any]]
    get_state: Callable[[int], State]
    touch: Callable[[State], State]
    make_callback: Callable[[str, str], Button]
    send_message: Callable[[int, str], Any]
    send_buttons: Callable[[int, str, list[list[Button]]], Any]
    send_main_menu: Callable[..., None]
    logger: logging.Logger
    script_list_state: str
    script_node_state: str
    menu_state: str


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

    rows = [
        [deps.make_callback(script["title"], f"script:{script['id']}")]
        for script in scripts
    ]
    rows.append([deps.make_callback("🏠 Главное меню", "main_menu")])
    deps.send_buttons(chat_id, "📚 Выберите тему:", rows)


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
        "nodes": nodes,
        "edges_by_from": edges_by_from,
        "current": root_id,
        "title": tree.get("title", "FAQ"),
        "path": [tree.get("title", "FAQ"), nodes[root_id]["title"]],
    }
    deps.touch(state)
    show_script_node(chat_id, deps)


def show_script_node(chat_id: int, deps: FaqDependencies) -> None:
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

    if node.get("is_terminal") or not edges:
        path = list(script.get("path", []))
        state["ai_faq_context"] = " → ".join(str(item) for item in path if item)
        state["state"] = deps.menu_state
        state.pop("script", None)
        deps.touch(state)
        deps.send_buttons(
            chat_id,
            f"📌 {text}",
            [
                [deps.make_callback("🤖 Спросить у ИИ-помощника", "ai_from_faq")],
                [deps.make_callback("🏠 Главное меню", "main_menu")],
            ],
        )
    else:
        rows = [
            [deps.make_callback(edge["label"], f"script_node:{edge['to_node_id']}")]
            for edge in edges
        ]
        rows.append([deps.make_callback("🏠 Главное меню", "main_menu")])
        deps.send_buttons(chat_id, f"📌 {text}", rows)


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
    deps.touch(state)
    show_script_node(chat_id, deps)
