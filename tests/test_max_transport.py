from __future__ import annotations

from unittest.mock import Mock

import bot
from rso_bot import max_transport


def test_send_raw_posts_expected_max_request():
    client = Mock()
    client.post.return_value.status_code = 200
    logger = Mock()
    body = {"text": "Привет"}

    assert max_transport.send_raw(
        "https://max.example.test",
        {"Authorization": "secret"},
        42,
        body,
        http_client=client,
        logger=logger,
    )

    client.post.assert_called_once_with(
        "https://max.example.test/messages",
        headers={"Authorization": "secret"},
        params={"chat_id": 42},
        json=body,
        timeout=5,
    )
    logger.warning.assert_not_called()
    logger.error.assert_not_called()


def test_send_raw_rejects_non_200_response_and_logs_status():
    client = Mock()
    client.post.return_value.status_code = 202
    logger = Mock()

    assert not max_transport.send_raw(
        "https://max.example.test",
        {},
        17,
        {"text": "queued"},
        http_client=client,
        logger=logger,
    )
    logger.warning.assert_called_once_with("MAX API %s для chat_id=%s", 202, 17)


def test_send_raw_contains_network_error_and_returns_false():
    client = Mock()
    error = RuntimeError("network down")
    client.post.side_effect = error
    logger = Mock()

    assert not max_transport.send_raw(
        "https://max.example.test",
        {},
        9,
        {"text": "hello"},
        http_client=client,
        logger=logger,
    )
    logger.error.assert_called_once_with("_send_raw chat_id=%s: %s", 9, error)


def test_send_message_builds_plain_text_payload_and_returns_sender_result():
    sender = Mock(return_value=False)

    assert not max_transport.send_message(5, "Текст", sender=sender)
    sender.assert_called_once_with(5, {"text": "Текст"})


def test_send_buttons_builds_inline_keyboard_without_changing_buttons():
    buttons = [
        [{"type": "callback", "text": "Да", "payload": "yes"}],
        [{"type": "callback", "text": "Нет", "payload": "no"}],
    ]
    sender = Mock(return_value=True)

    assert max_transport.send_buttons(6, "Выберите:", buttons, sender=sender)
    sender.assert_called_once_with(
        6,
        {
            "text": "Выберите:",
            "attachments": [
                {
                    "type": "inline_keyboard",
                    "payload": {"buttons": buttons},
                }
            ],
        },
    )
    assert buttons[0][0]["payload"] == "yes"


def test_ack_callback_posts_expected_answer():
    client = Mock()
    logger = Mock()

    result = max_transport.ack_callback(
        "https://max.example.test",
        {"Authorization": "secret"},
        "callback-123",
        http_client=client,
        logger=logger,
    )

    assert result is None
    client.post.assert_called_once_with(
        "https://max.example.test/answers",
        headers={"Authorization": "secret"},
        json={"callback_id": "callback-123", "notification": ""},
        timeout=3,
    )
    logger.debug.assert_not_called()


def test_ack_callback_contains_network_error_and_logs_debug():
    client = Mock()
    error = RuntimeError("network down")
    client.post.side_effect = error
    logger = Mock()

    assert (
        max_transport.ack_callback(
            "https://max.example.test",
            {},
            "callback-456",
            http_client=client,
            logger=logger,
        )
        is None
    )
    logger.debug.assert_called_once_with("_ack_callback %s: %s", "callback-456", error)


def test_legacy_send_message_wrapper_keeps_send_raw_patch_point(monkeypatch):
    sender = Mock(return_value=True)
    monkeypatch.setattr(bot, "_send_raw", sender)

    assert bot.send_message(10, "Совместимость")
    sender.assert_called_once_with(10, {"text": "Совместимость"})


def test_legacy_send_buttons_wrapper_keeps_send_raw_patch_point(monkeypatch):
    sender = Mock(return_value=False)
    buttons = [[{"type": "callback", "text": "OK", "payload": "ok"}]]
    monkeypatch.setattr(bot, "_send_raw", sender)

    assert not bot.send_buttons(11, "Кнопка", buttons)
    sender.assert_called_once_with(
        11,
        {
            "text": "Кнопка",
            "attachments": [
                {
                    "type": "inline_keyboard",
                    "payload": {"buttons": buttons},
                }
            ],
        },
    )


def test_legacy_raw_wrapper_passes_runtime_configuration(monkeypatch):
    delegated = Mock(return_value=True)
    fake_client = object()
    fake_logger = object()
    monkeypatch.setattr(bot.max_transport, "send_raw", delegated)
    monkeypatch.setattr(bot, "API", "https://patched.example.test")
    monkeypatch.setattr(bot, "_MAX_HEADERS", {"Authorization": "patched"})
    monkeypatch.setattr(bot, "httpx", fake_client)
    monkeypatch.setattr(bot, "log", fake_logger)

    assert bot._send_raw(12, {"text": "payload"})
    delegated.assert_called_once_with(
        "https://patched.example.test",
        {"Authorization": "patched"},
        12,
        {"text": "payload"},
        http_client=fake_client,
        logger=fake_logger,
    )


def test_legacy_ack_wrapper_passes_runtime_configuration(monkeypatch):
    delegated = Mock()
    fake_client = object()
    fake_logger = object()
    monkeypatch.setattr(bot.max_transport, "ack_callback", delegated)
    monkeypatch.setattr(bot, "API", "https://patched.example.test")
    monkeypatch.setattr(bot, "_MAX_HEADERS", {"Authorization": "patched"})
    monkeypatch.setattr(bot, "httpx", fake_client)
    monkeypatch.setattr(bot, "log", fake_logger)

    assert bot._ack_callback("callback-789") is None
    delegated.assert_called_once_with(
        "https://patched.example.test",
        {"Authorization": "patched"},
        "callback-789",
        http_client=fake_client,
        logger=fake_logger,
    )
