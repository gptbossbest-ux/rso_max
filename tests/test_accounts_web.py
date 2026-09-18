from __future__ import annotations

import pytest

import database as db
import web


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


def test_admin_can_add_and_list_account_with_escaped_fields(accounts_db) -> None:
    client = _logged_in_client("admin")

    response = client.post(
        "/accounts/create",
        data={
            "number": "  100001  ",
            "fio": "<script>alert('fio')</script>",
            "address": "<img src=x onerror=alert('address')>",
        },
        follow_redirects=True,
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

    response = client.post(
        "/accounts/create",
        data={"number": "100001", "fio": "Второй", "address": "Новый адрес"},
        follow_redirects=True,
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
        ("x" * 65, "не должен превышать 64 символа"),
        ("100\n001", "содержит недопустимые символы"),
    ],
)
def test_invalid_account_number_is_rejected(accounts_db, number, message) -> None:
    client = _logged_in_client("admin")

    response = client.post(
        "/accounts/create",
        data={"number": number},
        follow_redirects=True,
    )

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

    response = client.post("/accounts/create", data=data, follow_redirects=True)

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
