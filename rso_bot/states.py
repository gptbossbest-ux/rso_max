"""Session state names and flow-specific state keys."""

from __future__ import annotations


class S:
    """Stable string values used by the bot state machine."""

    MENU = "menu"

    APPEAL_CATEGORY = "appeal_category"
    APPEAL_BODY = "appeal_body"
    AWAIT_LS = "await_ls"
    AWAIT_LS_1C = "await_ls_1c"

    REOPEN_COMMENT = "reopen_comment"

    SCRIPT_LIST = "script_list"
    SCRIPT_NODE = "script_node"

    METER_SELECT = "meter_select"
    WAITING_VALUE1 = "waiting_value1"
    WAITING_VALUE2 = "waiting_value2"
    CONFIRM_POKAZANIYA = "confirm_pokazaniya"

    APPOINTMENT_BRANCH = "appointment_branch"
    APPOINTMENT_DATE = "appointment_date"
    APPOINTMENT_TIME = "appointment_time"
    APPOINTMENT_THEME = "appointment_theme"
    APPOINTMENT_CONFIRM = "appointment_confirm"


FLOW_KEYS = (
    "appeal",
    "script",
    "after_ls",
    "reopen_appeal_id",
    "meters",
    "meter_idx",
    "new_value1",
    "new_value2",
    "appt_branch_id",
    "appt_date",
    "appt_time",
    "appt_theme",
    # Cleans sessions left by deployments that still used the OTP flow.
    "pending_1c_ls",
    "after_1c_auth",
)

METER_INPUT_KEYS = ("new_value1", "new_value2")
