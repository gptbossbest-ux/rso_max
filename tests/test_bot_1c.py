from __future__ import annotations

import unittest
from unittest.mock import patch

import bot


class Bot1CFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        bot.user_states.clear()
        bot._auth_attempts.clear()

    def test_start_auth_requests_ls(self) -> None:
        with patch.object(bot, "send_message") as send:
            bot._start_1c_auth(42)

        self.assertEqual(bot.user_states[42]["state"], bot.S.AWAIT_LS_1C)
        send.assert_called_once_with(42, "Введите номер вашего лицевого счёта:")

    def test_main_menu_offers_auth_only_for_unbound_integration_user(self) -> None:
        with patch.object(bot, "ENABLE_1C_INTEGRATION", True, create=True), \
             patch.object(bot, "_get_saved_ls", return_value=None), \
             patch.object(bot, "send_buttons") as send:
            bot.send_main_menu(42)

        buttons = send.call_args.args[2]
        self.assertEqual(buttons[0][0]["payload"], "auth_1c")

    def test_appeal_survives_auth_and_wrong_code_until_submission(self) -> None:
        st = bot._get_state(42)
        body = "Нет горячей воды со вчерашнего вечера"
        with patch.object(bot, "ENABLE_1C_INTEGRATION", True), \
             patch.object(bot, "_get_saved_ls", return_value=None), \
             patch.object(bot, "_save_ls") as save, \
             patch.object(bot, "send_message"), \
             patch.object(bot, "send_main_menu"), \
             patch.object(bot.client_api, "request_1c_auth_code", return_value=(
                 {"status": "ok", "message": "Код отправлен"}, None,
             )), \
             patch.object(bot.client_api, "verify_1c_auth_code", side_effect=[
                 ({"status": "wrong_code", "message": "Код неверный"}, None),
                 ({"status": "ok", "message": "Авторизация выполнена", "meters": []}, None),
             ]), \
             patch.object(bot.client_api, "create_appeal", return_value=(
                 {"ticket_no": "TEST-42"}, None,
             )) as create:
            bot._appeal_set_category(42, "авария")
            bot._appeal_got_body(42, body)
            self.assertEqual(st["state"], bot.S.AWAIT_LS_1C)
            bot._on_await_ls_1c(42, st, "100001")
            bot._on_await_code_1c(42, st, "111111")
            create.assert_not_called()
            self.assertEqual(st["state"], bot.S.AWAIT_CODE_1C)
            bot._on_await_code_1c(42, st, "222222")

        create.assert_called_once_with(
            ls="100001", channel="max", category="авария", body=body, chat_id=42,
        )
        save.assert_called_once_with(42, "100001")
        self.assertEqual(st["state"], bot.S.MENU)
        for key in ("appeal", "pending_1c_ls", "after_1c_auth"):
            self.assertNotIn(key, st)

    def test_cancel_deferred_auth_discards_appeal(self) -> None:
        st = bot._get_state(42)
        st["appeal"] = {"category": "авария", "body": "Нет воды"}
        with patch.object(bot, "send_message"), \
             patch.object(bot, "send_main_menu"), \
             patch.object(bot.client_api, "create_appeal") as create:
            bot._start_1c_auth(42, after="appeal")
            bot._cb_main_menu(42, st)

        create.assert_not_called()
        self.assertEqual(st["state"], bot.S.MENU)
        self.assertNotIn("appeal", st)
        self.assertNotIn("after_1c_auth", st)

    def test_standalone_auth_discards_previous_flow(self) -> None:
        st = bot._get_state(42)
        st.update({
            "appeal": {"category": "авария", "body": "Нет воды"},
            "after_1c_auth": "appeal",
            "pending_1c_ls": "100001",
        })
        with patch.object(bot, "send_message"):
            bot._start_1c_auth(42)

        self.assertEqual(st["state"], bot.S.AWAIT_LS_1C)
        for key in ("appeal", "pending_1c_ls", "after_1c_auth"):
            self.assertNotIn(key, st)

    def test_valid_ls_requests_code_and_waits_for_it(self) -> None:
        st = bot._get_state(42)
        st["state"] = bot.S.AWAIT_LS_1C
        with patch.object(
            bot.client_api,
            "request_1c_auth_code",
            return_value=({"status": "ok", "message": "Код отправлен"}, None),
        ), patch.object(bot, "send_message") as send:
            bot._on_await_ls_1c(42, st, "100001")

        self.assertEqual(st["state"], bot.S.AWAIT_CODE_1C)
        self.assertEqual(st["pending_1c_ls"], "100001")
        send.assert_called_once_with(42, "Код отправлен")

    def test_wrong_code_keeps_code_state(self) -> None:
        st = bot._get_state(42)
        st.update({"state": bot.S.AWAIT_CODE_1C, "pending_1c_ls": "100001"})
        with patch.object(
            bot.client_api,
            "verify_1c_auth_code",
            return_value=({"status": "wrong_code", "message": "Код неверный"}, None),
        ), patch.object(bot, "send_message") as send:
            bot._on_await_code_1c(42, st, "111111")

        self.assertEqual(st["state"], bot.S.AWAIT_CODE_1C)
        self.assertEqual(st["pending_1c_ls"], "100001")
        send.assert_called_once_with(42, "Код неверный")

    def test_authorization_code_is_not_written_to_debug_log(self) -> None:
        st = bot._get_state(42)
        st.update({"state": bot.S.AWAIT_CODE_1C, "pending_1c_ls": "100001"})
        message = {"recipient": {"chat_id": 42}, "body": {"text": "654321"}}
        with patch.object(
            bot.client_api,
            "verify_1c_auth_code",
            return_value=({"status": "wrong_code", "message": "Код неверный"}, None),
        ), patch.object(bot, "send_message"), self.assertLogs("rso.bot", level="DEBUG") as logs:
            bot.handle_message(message)

        self.assertNotIn("654321", "\n".join(logs.output))

    def test_expired_code_returns_to_main_menu(self) -> None:
        st = bot._get_state(42)
        st.update({"state": bot.S.AWAIT_CODE_1C, "pending_1c_ls": "100001"})
        with patch.object(
            bot.client_api,
            "verify_1c_auth_code",
            return_value=({"status": "expired_code", "message": "Код истёк"}, None),
        ), patch.object(bot, "send_message"), patch.object(bot, "send_main_menu") as menu:
            bot._on_await_code_1c(42, st, "000000")

        self.assertEqual(st["state"], bot.S.MENU)
        self.assertNotIn("pending_1c_ls", st)
        menu.assert_called_once_with(42)

    def test_success_saves_ls_and_returns_to_menu(self) -> None:
        st = bot._get_state(42)
        st.update({"state": bot.S.AWAIT_CODE_1C, "pending_1c_ls": "100001"})
        result = {"status": "ok", "message": "Авторизация выполнена", "meters": []}
        with patch.object(bot.client_api, "verify_1c_auth_code", return_value=(result, None)), \
             patch.object(bot, "_save_ls") as save, \
             patch.object(bot, "send_message"), \
             patch.object(bot, "send_main_menu"):
            bot._on_await_code_1c(42, st, "000000")

        save.assert_called_once_with(42, "100001")
        self.assertEqual(st["state"], bot.S.MENU)

    def test_integration_mode_does_not_reject_lower_reading_locally(self) -> None:
        st = bot._get_state(42)
        st.update({
            "state": bot.S.WAITING_VALUE1,
            "ls": "100001",
            "meters": [{
                "meter_number": "M-1",
                "resource_type": "Электроэнергия",
                "meter_type": "Однотарифный",
                "initial1": "100",
                "initial2": "0",
            }],
            "meter_idx": 0,
        })
        with patch.object(bot, "ENABLE_1C_INTEGRATION", True), \
             patch.object(bot, "_current_reading", return_value=100.0), \
             patch.object(bot, "_confirm_meter_reading") as confirm:
            bot._on_value1(42, st, "50")

        confirm.assert_called_once_with(42)
        self.assertEqual(st["new_value1"], "50.0")

    def test_confirmation_in_integration_mode_says_queued(self) -> None:
        st = bot._get_state(42)
        st.update({
            "state": bot.S.CONFIRM_POKAZANIYA,
            "ls": "100001",
            "meters": [{
                "meter_number": "M-1",
                "resource_type": "Электроэнергия",
                "meter_type": "Однотарифный",
            }],
            "meter_idx": 0,
            "new_value1": "50.0",
            "new_value2": None,
        })
        with patch.object(bot, "ENABLE_1C_INTEGRATION", True), \
             patch.object(bot.db, "add_pokazaniya"), \
             patch.object(bot, "send_message") as send, \
             patch.object(bot, "_show_meter_select"), \
             self.assertLogs("rso.bot", level="INFO") as logs:
            bot._cb_meter_confirm(42, st)

        self.assertIn("ожидают обработки", send.call_args.args[1])
        self.assertIn("сохранены в очередь", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
