from __future__ import annotations

from unittest.mock import Mock

import bot
from rso_bot.flows import faq


def _dependencies(
    *,
    scripts: tuple[list[dict], object | None] = ([], None),
    tree: tuple[dict | None, object | None] = (None, None),
) -> tuple[faq.FaqDependencies, dict]:
    state: dict = {"state": "menu"}
    deps = faq.FaqDependencies(
        list_scripts=Mock(return_value=scripts),
        get_script_tree=Mock(return_value=tree),
        get_state=Mock(return_value=state),
        touch=Mock(side_effect=lambda value: value),
        make_callback=lambda label, payload: {
            "type": "callback",
            "text": label,
            "payload": payload,
        },
        send_message=Mock(),
        send_buttons=Mock(),
        send_main_menu=Mock(),
        logger=Mock(),
        script_list_state="script_list",
        script_node_state="script_node",
        menu_state="menu",
    )
    return deps, state


def test_show_scripts_list_renders_topics_and_main_menu():
    deps, state = _dependencies(
        scripts=([{"id": 8, "title": "Оплата"}, {"id": 3, "title": "Льготы"}], None),
    )

    faq.show_scripts_list(42, deps)

    assert state["state"] == "script_list"
    deps.touch.assert_called_once_with(state)
    deps.send_buttons.assert_called_once_with(
        42,
        "📚 Выберите тему:",
        [
            [{"type": "callback", "text": "Оплата", "payload": "script:8"}],
            [{"type": "callback", "text": "Льготы", "payload": "script:3"}],
            [{"type": "callback", "text": "🏠 Главное меню", "payload": "main_menu"}],
        ],
    )


def test_show_scripts_list_preserves_empty_and_error_responses():
    empty, _ = _dependencies()
    faq.show_scripts_list(42, empty)
    empty.send_message.assert_called_once_with(42, "📚 Раздел FAQ пуст.")
    empty.send_main_menu.assert_called_once_with(42)

    failed, _ = _dependencies(scripts=([], "unavailable"))
    faq.show_scripts_list(43, failed)
    failed.send_message.assert_called_once_with(43, "⚠️ Сервис временно недоступен.")
    failed.send_main_menu.assert_called_once_with(43)


def test_open_script_selects_root_and_renders_its_children():
    tree = {
        "nodes": [
            {"id": 20, "title": "Ответ", "is_terminal": True},
            {"id": 10, "title": "Вопрос", "is_terminal": False},
        ],
        "edges": [{"from_node_id": 10, "label": "Продолжить", "to_node_id": 20}],
    }
    deps, state = _dependencies(tree=(tree, None))

    faq.open_script(42, 7, deps)

    assert state["state"] == "script_node"
    assert state["script"]["current"] == 10
    deps.send_buttons.assert_called_once_with(
        42,
        "📌 Вопрос",
        [
            [{"type": "callback", "text": "Продолжить", "payload": "script_node:20"}],
            [{"type": "callback", "text": "🏠 Главное меню", "payload": "main_menu"}],
        ],
    )


def test_open_script_uses_smallest_root_and_logs_ambiguous_tree():
    tree = {
        "nodes": [
            {"id": 8, "title": "Восьмой", "is_terminal": True},
            {"id": 2, "title": "Второй", "is_terminal": True},
        ],
        "edges": [],
    }
    deps, state = _dependencies(tree=(tree, None))

    faq.open_script(42, 99, deps)

    deps.logger.warning.assert_called_once_with(
        "Скрипт id=%s: найдено %d корневых узлов, берём минимальный",
        99,
        2,
    )
    deps.send_message.assert_called_once_with(42, "📌 Второй")
    assert state["state"] == "menu"
    assert "script" not in state


def test_terminal_node_returns_to_main_menu_and_clears_script():
    deps, state = _dependencies()
    state.update(
        {
            "state": "script_node",
            "script": {
                "nodes": {5: {"id": 5, "title": "Готовый ответ", "is_terminal": True}},
                "edges_by_from": {},
                "current": 5,
            },
        }
    )

    faq.show_script_node(42, deps)

    deps.send_message.assert_called_once_with(42, "📌 Готовый ответ")
    deps.send_main_menu.assert_called_once_with(42, "Выберите следующее действие:")
    assert state["state"] == "menu"
    assert "script" not in state


def test_navigate_can_move_to_child_and_back_to_parent():
    deps, state = _dependencies()
    state["script"] = {
        "nodes": {
            1: {"id": 1, "title": "Родитель", "is_terminal": False},
            2: {"id": 2, "title": "Ребёнок", "is_terminal": False},
        },
        "edges_by_from": {
            1: [{"label": "Вперёд", "to_node_id": 2}],
            2: [{"label": "Назад", "to_node_id": 1}],
        },
        "current": 1,
    }

    faq.navigate_script_node(42, 2, deps)
    assert state["script"]["current"] == 2
    assert deps.send_buttons.call_args.args[1] == "📌 Ребёнок"

    faq.navigate_script_node(42, 1, deps)
    assert state["script"]["current"] == 1
    assert deps.send_buttons.call_args.args[1] == "📌 Родитель"


def test_navigate_without_active_script_returns_to_menu():
    deps, _ = _dependencies()

    faq.navigate_script_node(42, 7, deps)

    deps.send_main_menu.assert_called_once_with(42)
    deps.touch.assert_not_called()


def test_missing_node_preserves_completion_response():
    deps, state = _dependencies()
    state["script"] = {"nodes": {}, "edges_by_from": {}, "current": 404}

    faq.show_script_node(42, deps)

    deps.send_message.assert_called_once_with(42, "Скрипт завершён.")
    deps.send_main_menu.assert_called_once_with(42)


def test_open_script_handles_load_failure_and_empty_tree():
    failed, _ = _dependencies(tree=(None, "timeout"))
    faq.open_script(42, 7, failed)
    failed.send_message.assert_called_once_with(42, "⚠️ Не удалось загрузить скрипт.")
    failed.send_main_menu.assert_called_once_with(42)

    empty, _ = _dependencies(tree=({"nodes": [], "edges": []}, None))
    faq.open_script(43, 8, empty)
    empty.send_message.assert_called_once_with(43, "Скрипт пуст.")
    empty.send_main_menu.assert_called_once_with(43)


def test_bot_wrappers_delegate_with_runtime_patch_points(monkeypatch):
    delegated = Mock()
    monkeypatch.setattr(bot.faq, "show_scripts_list", delegated)
    patched_sender = Mock()
    monkeypatch.setattr(bot, "send_message", patched_sender)

    bot._show_scripts_list(42)

    delegated.assert_called_once()
    chat_id, deps = delegated.call_args.args
    assert chat_id == 42
    assert deps.send_message is patched_sender
    assert deps.list_scripts is bot.client_api.list_scripts
    assert deps.get_state is bot._get_state


def test_all_bot_faq_wrappers_delegate(monkeypatch):
    open_script = Mock()
    show_node = Mock()
    navigate = Mock()
    monkeypatch.setattr(bot.faq, "open_script", open_script)
    monkeypatch.setattr(bot.faq, "show_script_node", show_node)
    monkeypatch.setattr(bot.faq, "navigate_script_node", navigate)

    bot._open_script(42, 7)
    bot._show_script_node(42)
    bot._navigate_script_node(42, 8)

    assert open_script.call_args.args[:2] == (42, 7)
    assert show_node.call_args.args[:1] == (42,)
    assert navigate.call_args.args[:2] == (42, 8)
