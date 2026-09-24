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
        with (
            patch.object(bot, "ENABLE_1C_INTEGRATION", True, create=True),
            patch.object(bot, "_get_saved_ls", return_value=None),
            patch.object(bot, "send_buttons") as send,
        ):
            bot.send_main_menu(42)

        buttons = send.call_args.args[2]
        self.assertEqual(buttons[0][0]["payload"], "auth_1c")

    def test_main_menu_hides_auth_for_bound_user_and_keeps_emoji_actions(self) -> None:
        with (
            patch.object(bot, "ENABLE_1C_INTEGRATION", True, create=True),
            patch.object(bot, "_get_saved_ls", return_value="100001"),
            patch.object(bot, "send_buttons") as send,
        ):
            bot.send_main_menu(42)

        buttons = send.call_args.args[2]
        labels = [row[0]["text"] for row in buttons]
        self.assertNotIn("auth_1c", [row[0]["payload"] for row in buttons])
        self.assertEqual(labels[0], "📝 Подать обращение")
        self.assertTrue(all(not label[0].isalnum() for label in labels))

    def test_appeal_survives_invalid_account_until_local_auth_succeeds(self) -> None:
        st = bot._get_state(42)
        body = "Нет горячей воды со вчерашнего вечера"
        with (
            patch.object(bot, "ENABLE_1C_INTEGRATION", True),
            patch.object(bot, "_get_saved_ls", return_value=None),
            patch.object(bot, "_validate_ls", side_effect=[False, True]),
            patch.object(bot, "_save_ls") as save,
            patch.object(bot, "send_message"),
            patch.object(bot, "send_main_menu"),
            patch.object(
                bot.client_api,
                "create_appeal",
                return_value=(
                    {"ticket_no": "TEST-42"},
                    None,
                ),
            ) as create,
        ):
            bot._appeal_set_category(42, "авария")
            bot._appeal_got_body(42, body)
            self.assertEqual(st["state"], bot.S.AWAIT_LS_1C)
            bot._on_await_ls_1c(42, st, "100001")
            create.assert_not_called()
            self.assertEqual(st["state"], bot.S.AWAIT_LS_1C)
            bot._on_await_ls_1c(42, st, "100001")

        create.assert_called_once_with(
            ls="100001",
            channel="max",
            category="авария",
            body=body,
            chat_id=42,
        )
        save.assert_called_once_with(42, "100001")
        self.assertEqual(st["state"], bot.S.MENU)
        for key in ("appeal", "pending_1c_ls", "after_1c_auth"):
            self.assertNotIn(key, st)

    def test_cancel_deferred_auth_discards_appeal(self) -> None:
        st = bot._get_state(42)
        st["appeal"] = {"category": "авария", "body": "Нет воды"}
        with (
            patch.object(bot, "send_message"),
            patch.object(bot, "send_main_menu"),
            patch.object(bot.client_api, "create_appeal") as create,
        ):
            bot._start_1c_auth(42, after="appeal")
            bot._cb_main_menu(42, st)

        create.assert_not_called()
        self.assertEqual(st["state"], bot.S.MENU)
        self.assertNotIn("appeal", st)
        self.assertNotIn("after_1c_auth", st)

    def test_standalone_auth_discards_previous_flow(self) -> None:
        st = bot._get_state(42)
        st.update(
            {
                "appeal": {"category": "авария", "body": "Нет воды"},
                "after_1c_auth": "appeal",
                "pending_1c_ls": "100001",
            }
        )
        with patch.object(bot, "send_message"):
            bot._start_1c_auth(42)

        self.assertEqual(st["state"], bot.S.AWAIT_LS_1C)
        for key in ("appeal", "pending_1c_ls", "after_1c_auth"):
            self.assertNotIn(key, st)

    def test_valid_ls_authorizes_locally_without_otp_calls(self) -> None:
        st = bot._get_state(42)
        st["state"] = bot.S.AWAIT_LS_1C
        with (
            patch.object(bot, "_validate_ls", return_value=True),
            patch.object(bot, "_save_ls") as save,
            patch.object(bot.client_api, "request_1c_auth_code") as request,
            patch.object(bot.client_api, "verify_1c_auth_code") as verify,
            patch.object(bot, "send_main_menu") as menu,
        ):
            bot._on_await_ls_1c(42, st, "100001")

        self.assertEqual(st["state"], bot.S.MENU)
        save.assert_called_once_with(42, "100001")
        request.assert_not_called()
        verify.assert_not_called()
        menu.assert_called_once_with(42, "✅ Авторизация выполнена. Выберите действие:")

    def test_invalid_ls_keeps_local_auth_state(self) -> None:
        st = bot._get_state(42)
        st["state"] = bot.S.AWAIT_LS_1C
        with (
            patch.object(bot, "_validate_ls", return_value=False),
            patch.object(bot, "send_message") as send,
        ):
            bot._on_await_ls_1c(42, st, "missing")

        self.assertEqual(st["state"], bot.S.AWAIT_LS_1C)
        self.assertEqual(send.call_count, 2)

    def test_integration_mode_does_not_reject_lower_reading_locally(self) -> None:
        st = bot._get_state(42)
        st.update(
            {
                "state": bot.S.WAITING_VALUE1,
                "ls": "100001",
                "meters": [
                    {
                        "meter_number": "M-1",
                        "resource_type": "Электроэнергия",
                        "meter_type": "Однотарифный",
                        "initial1": "100",
                        "initial2": "0",
                    }
                ],
                "meter_idx": 0,
            }
        )
        with (
            patch.object(bot, "ENABLE_1C_INTEGRATION", True),
            patch.object(bot, "_current_reading", return_value=100.0),
            patch.object(bot, "_confirm_meter_reading") as confirm,
        ):
            bot._on_value1(42, st, "50")

        confirm.assert_called_once_with(42)
        self.assertEqual(st["new_value1"], "50.0")

    def test_confirmation_in_integration_mode_says_queued(self) -> None:
        st = bot._get_state(42)
        st.update(
            {
                "state": bot.S.CONFIRM_POKAZANIYA,
                "ls": "100001",
                "meters": [
                    {
                        "meter_number": "M-1",
                        "resource_type": "Электроэнергия",
                        "meter_type": "Однотарифный",
                    }
                ],
                "meter_idx": 0,
                "new_value1": "50.0",
                "new_value2": None,
            }
        )
        with (
            patch.object(bot, "ENABLE_1C_INTEGRATION", True),
            patch.object(bot.db, "add_pokazaniya"),
            patch.object(bot, "send_message") as send,
            patch.object(bot, "_show_meter_select"),
            self.assertLogs("rso.bot", level="INFO") as logs,
        ):
            bot._cb_meter_confirm(42, st)

        self.assertIn("ожидают обработки", send.call_args.args[1])
        self.assertIn("сохранены в очередь", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
