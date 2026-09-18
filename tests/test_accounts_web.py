from __future__ import annotations

import logging
import re
import sqlite3

import pytest
from openpyxl import Workbook

import database as db
import web

_CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


@pytest.fixture()
def accounts_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "accounts.sqlite"))
    monkeypatch.setattr(db, "BOOTSTRAP_ADMIN_PASSWORD", "")
    db.init_db()


def _logged_in_client(role: str):
    ok, _ = db.create_user(f"{role}-accounts", "initial-password", role.title(), role)
    assert ok
    client = web.app.test_client()
    response = client.post(
        "/login",
        data={"username": f"{role}-accounts", "password": "initial-password"},
    )
    assert response.status_code == 302
    return client


def _csrf_token(client) -> str:
    response = client.get("/accounts")
    assert response.status_code == 200
    match = _CSRF_RE.search(response.text)
    assert match is not None
    return match.group(1)


def _create_account(client, **data):
    return client.post(
        "/accounts/create",
        data={"csrf_token": _csrf_token(client), **data},
        follow_redirects=True,
    )


def test_admin_can_add_and_list_account_with_escaped_fields(accounts_db) -> None:
    client = _logged_in_client("admin")

    response = _create_account(
        client,
        number="  100001  ",
        fio="<script>alert('fio')</script>",
        address="<img src=x onerror=alert('address')>",
    )

    assert response.status_code == 200
    assert "Лицевой счёт добавлен" in response.text
    assert "100001" in response.text
    assert "<script>" not in response.text
    assert "<img src=x" not in response.text
    assert "&lt;script&gt;" in response.text
    assert "&lt;img src=x" in response.text
    account = db.get_ls("100001")
    assert account is not None
    assert account["fio"] == "<script>alert('fio')</script>"


def test_duplicate_account_does_not_overwrite_existing_data(accounts_db) -> None:
    client = _logged_in_client("admin")
    assert db.create_lschet("100001", "Первый", "Старый адрес")

    response = _create_account(
        client,
        number="100001",
        fio="Второй",
        address="Новый адрес",
    )

    assert response.status_code == 200
    assert "уже существует" in response.text
    account = db.get_ls("100001")
    assert account is not None
    assert (account["fio"], account["address"]) == ("Первый", "Старый адрес")
    assert len(db.list_lschet()) == 1


@pytest.mark.parametrize(
    ("number", "message"),
    [
        ("   ", "Введите номер лицевого счёта"),
        ("x" * 65, "от 1 до 64 символов"),
        ("100\n001", "латинские буквы"),
        ("СЧЕТ-1", "латинские буквы"),
        ("ＴＥＳＴ-1", "латинские буквы"),
        ("TEST\u200b-1", "латинские буквы"),
        ("TEST\u202e-1", "латинские буквы"),
        ("TEST:1", "латинские буквы"),
        ("TEST/1", "латинские буквы"),
    ],
)
def test_invalid_account_number_is_rejected(accounts_db, number, message) -> None:
    client = _logged_in_client("admin")

    response = _create_account(client, number=number)

    assert response.status_code == 200
    assert message in response.text
    assert db.list_lschet() == []


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("fio", "x" * 257, "Поле «ФИО» не должно превышать 256 символов"),
        ("fio", "Иван\nИванов", "Поле «ФИО» содержит недопустимые символы"),
        ("address", "x" * 513, "Поле «Адрес» не должно превышать 512 символов"),
        ("address", "Дом\t1", "Поле «Адрес» содержит недопустимые символы"),
    ],
)
def test_invalid_optional_account_fields_are_rejected(
    accounts_db, field, value, message
) -> None:
    client = _logged_in_client("admin")
    data = {"number": "100001", "fio": "", "address": ""}
    data[field] = value

    response = _create_account(client, **data)

    assert response.status_code == 200
    assert message in response.text
    assert db.list_lschet() == []


def test_operator_is_denied_account_page_and_create(accounts_db) -> None:
    client = _logged_in_client("operator")

    get_response = client.get("/accounts")
    post_response = client.post(
        "/accounts/create",
        data={"number": "100001"},
    )

    assert get_response.status_code == 302
    assert post_response.status_code == 302
    assert get_response.headers["Location"].endswith("/")
    assert post_response.headers["Location"].endswith("/")
    assert db.get_ls("100001") is None


@pytest.mark.parametrize("number", ["100001", "TEST-LS-001", "account_01"])
def test_canonical_account_formats_work_for_create_and_lookup(
    accounts_db, number
) -> None:
    assert db.create_lschet(f"  {number}  ")
    assert db.get_ls(number)["number"] == number


@pytest.mark.parametrize(
    "number",
    ["СЧЕТ-1", "ＴＥＳＴ-1", "TEST\u200b-1", "TEST\u202e-1", "TEST 1", "../TEST"],
)
def test_lookup_rejects_noncanonical_accounts(accounts_db, number) -> None:
    with pytest.raises(ValueError, match="латинские буквы"):
        db.get_ls(number)


def test_legacy_invalid_account_remains_visible_but_cannot_be_looked_up(
    accounts_db,
) -> None:
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO licschet (number, fio, address) VALUES (?, ?, ?)",
        ("СЧЕТ-1", "Legacy", None),
    )
    conn.commit()
    conn.close()

    assert db.list_lschet()[0]["number"] == "СЧЕТ-1"
    with pytest.raises(ValueError):
        db.get_ls("СЧЕТ-1")


def test_accounts_pagination_clamps_boundaries_and_limits_rows(accounts_db) -> None:
    client = _logged_in_client("admin")
    for index in range(123):
        assert db.create_lschet(f"LS-{index:03d}")

    first = client.get("/accounts?page=0")
    last = client.get("/accounts?page=999")

    assert "Страница 1 из 3" in first.text
    assert "LS-000" in first.text and "LS-049" in first.text
    assert "LS-050" not in first.text and "LS-122" not in first.text
    assert "Страница 3 из 3" in last.text
    assert "LS-100" in last.text and "LS-122" in last.text
    assert "LS-099" not in last.text


def test_accounts_empty_and_invalid_page_render_first_page(accounts_db) -> None:
    client = _logged_in_client("admin")

    response = client.get("/accounts?page=not-a-number")

    assert response.status_code == 200
    assert "Страница 1 из 1" in response.text
    assert "Лицевые счета ещё не добавлены" in response.text


def test_accounts_search_treats_like_wildcards_literally(accounts_db) -> None:
    client = _logged_in_client("admin")
    assert db.create_lschet("A_1", "Обычный", "Дом 1")
    assert db.create_lschet("AX1", "100% владелец", "Дом 2")

    underscore = client.get("/accounts?q=_%20")
    percent = client.get("/accounts?q=%25")

    assert "A_1" in underscore.text
    assert "AX1" not in underscore.text
    assert "AX1" in percent.text
    assert "A_1" not in percent.text


def test_accounts_search_query_is_capped(accounts_db) -> None:
    client = _logged_in_client("admin")
    response = client.get("/accounts", query_string={"q": "x" * 150})

    assert response.status_code == 200
    assert f'value="{"x" * 100}"' in response.text
    assert "x" * 101 not in response.text


def test_csrf_token_is_stable_isolated_and_rotated_after_login(accounts_db) -> None:
    first = _logged_in_client("admin")
    second = web.app.test_client()
    ok, _ = db.create_user("admin-two", "initial-password", "Admin Two", "admin")
    assert ok
    second.post(
        "/login",
        data={"username": "admin-two", "password": "initial-password"},
    )

    first_token = _csrf_token(first)
    assert _csrf_token(first) == first_token
    assert _csrf_token(second) != first_token
    first.get("/logout")
    first.post(
        "/login",
        data={"username": "admin-accounts", "password": "initial-password"},
    )
    assert _csrf_token(first) != first_token


@pytest.mark.parametrize("csrf_token", [None, "invalid"])
def test_accounts_create_rejects_missing_or_invalid_csrf(
    accounts_db, csrf_token
) -> None:
    client = _logged_in_client("admin")
    data = {"number": "100001"}
    if csrf_token is not None:
        data["csrf_token"] = csrf_token

    response = client.post("/accounts/create", data=data)

    assert response.status_code == 400
    assert db.get_ls("100001") is None


def test_unexpected_integrity_error_is_not_reported_as_duplicate(accounts_db) -> None:
    conn = db.get_conn()
    conn.execute(
        "CREATE TRIGGER reject_account BEFORE INSERT ON licschet "
        "BEGIN SELECT RAISE(ABORT, 'unexpected'); END"
    )
    conn.commit()
    conn.close()

    with pytest.raises(sqlite3.IntegrityError, match="unexpected"):
        db.create_lschet("100001")


def test_database_failure_is_sanitized_without_logging_submitted_data(
    accounts_db, monkeypatch, caplog
) -> None:
    client = _logged_in_client("admin")
    monkeypatch.setattr(
        web.db,
        "create_lschet",
        lambda *_args: (_ for _ in ()).throw(sqlite3.OperationalError("SECRET-LS")),
    )

    with caplog.at_level(logging.ERROR, logger=web.log.name):
        response = _create_account(
            client,
            number="SECRET-LS",
            fio="Секретное ФИО",
            address="Секретный адрес",
        )

    assert response.status_code == 200
    assert "Не удалось добавить лицевой счёт" in response.text
    assert "SECRET-LS" not in caplog.text
    assert "Секретное ФИО" not in caplog.text
    assert "Секретный адрес" not in caplog.text


def test_excel_import_rejects_invalid_account_without_partial_changes(
    accounts_db, tmp_path
) -> None:
    assert db.create_lschet("EXISTING", "До импорта")
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "ЛС и ФИО"
    sheet.append(["ЛС", "ФИО", "Адрес"])
    sheet.append(["VALID-1", "Первый", "Адрес"])
    sheet.append(["TEST\u200b-2", "Второй", "Адрес"])
    path = tmp_path / "invalid.xlsx"
    workbook.save(path)
    workbook.close()

    with pytest.raises(ValueError, match="строка 3"):
        db.import_from_excel(str(path))

    assert [row["number"] for row in db.list_lschet()] == ["EXISTING"]


def test_excel_import_accepts_canonical_hyphen_and_underscore(
    accounts_db, tmp_path
) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "ЛС и ФИО"
    sheet.append(["ЛС", "ФИО", "Адрес"])
    sheet.append(["TEST-LS_001", "Тест", "Адрес"])
    path = tmp_path / "valid.xlsx"
    workbook.save(path)
    workbook.close()

    db.import_from_excel(str(path))

    assert db.get_ls("TEST-LS_001") is not None
