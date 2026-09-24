from __future__ import annotations

from unittest.mock import Mock

import pytest

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


@pytest.mark.parametrize("url", [
    "javascript:alert(1)", "data:text/plain,x", "file:///etc/passwd",
    "https://user:password@example.test/path", "//example.test/path",
    "https://example.test:444/path", "https://example.test/" + "x" * 2048,
    "https://example.test/path\nnext",
    "https://example.test/path\\next", "https://bad_host.test/path",
    "https://example.test./path", "https://exa\u200bmple.test/path",
    "http://example.test:443/path",
    "https://example.test:80/path", "https://example.test/%0aheader",
])
def test_link_button_rejects_unsafe_urls(url):
    with pytest.raises(ValueError):
        max_transport.make_link_button("Сайт", url)


def test_callback_payload_limit_is_enforced_in_builder_and_keyboard():
    oversized = "я" * 513
    with pytest.raises(ValueError):
        max_transport.make_callback_button("Далее", oversized)
    with pytest.raises(ValueError):
        max_transport.validate_inline_keyboard([[
            {"type": "callback", "text": "Далее", "payload": oversized},
        ]])


def test_link_button_matches_max_inline_keyboard_contract():
    button = max_transport.make_link_button(
        "Открыть личный кабинет", "https://example.test/account?q=1",
    )
    assert button == {
        "type": "link", "text": "Открыть личный кабинет",
        "url": "https://example.test/account?q=1",
    }
    sender = Mock(return_value=True)
    max_transport.send_buttons(7, "Текст [не становится](разметкой)", [[button]], sender=sender)
    payload = sender.call_args.args[1]
    assert payload["text"] == "Текст [не становится](разметкой)"
    assert "format" not in payload
    assert payload["attachments"][0]["payload"]["buttons"] == [[button]]


def test_link_button_normalizes_idna_and_rejects_label_format_controls():
    assert max_transport.make_link_button(
        "Сайт", "https://пример.рф/помощь",
    )["url"] == "https://xn--e1afmkfd.xn--p1ai/помощь"
    assert max_transport.validate_link_url("HTTPS://EXAMPLE.TEST/path") == (
        "https://example.test/path"
    )
    with pytest.raises(ValueError):
        max_transport.make_link_button("Са\u200bйт", "https://example.test")
    with pytest.raises(ValueError):
        max_transport.make_link_button("Сайт\n", "https://example.test")
    with pytest.raises(ValueError):
        max_transport.make_link_button("Сайт", " https://example.test")


def _callback(index=0):
    return {"type": "callback", "text": f"Кнопка {index}", "payload": f"p:{index}"}


def test_keyboard_accepts_official_boundaries():
    rows = [[_callback(row * 7 + item) for item in range(7)] for row in range(30)]
    assert max_transport.validate_inline_keyboard(rows) is rows


@pytest.mark.parametrize("buttons", [
    [[_callback()]] * 31,
    [[_callback(index) for index in range(8)]],
    [[
        max_transport.make_link_button("Ссылка", "https://example.test"),
        _callback(1), _callback(2), _callback(3),
    ]],
    [[{"type": "callback", "text": "X", "payload": ""}]],
    [[{"type": "unknown", "text": "X"}]],
])
def test_keyboard_rejects_limit_and_contract_violations(buttons):
    with pytest.raises(ValueError):
        max_transport.validate_inline_keyboard(buttons)


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
