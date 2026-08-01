"""
seed_demo.py — заполняет базу тестовыми данными.
Запуск: python seed_demo.py

Переписан под текущую схему database.py (Горизонт 1).
Вместо sync_from_1c использует прямые INSERT через get_conn().
"""
from database import init_db, get_conn, msk_now

init_db()

# ===========================================================================
# Тестовые данные
# ===========================================================================

accounts = [
    {
        "ls":      "100001",
        "fio":     "Иванов Иван Иванович",
        "address": "ул. Ленина, 10, кв. 5",
        "meters":  [
            {"meter_number": "СЭ-001", "meter_type": "Однотарифный",
             "resource_type": "Электроэнергия",
             "value1": 5670.30, "value2": None, "value3": None, "value4": None},
            {"meter_number": "СЭ-002", "meter_type": "Двухтарифный",
             "resource_type": "Электроэнергия",
             "value1": 12450.50, "value2": 8320.10, "value3": None, "value4": None},
        ],
    },
    {
        "ls":      "100002",
        "fio":     "Петрова Мария Сергеевна",
        "address": "пр. Мира, 3, кв. 12",
        "meters":  [
            {"meter_number": "СЭ-003", "meter_type": "Двунаправленный",
             "resource_type": "Электроэнергия",
             "value1": 9100.00, "value2": 430.20, "value3": None, "value4": None},
        ],
    },
    {
        "ls":      "100003",
        "fio":     "Сидоров Алексей Петрович",
        "address": "ул. Гагарина, 7, кв. 1",
        "meters":  [
            {"meter_number": "СЭ-004", "meter_type": "Двухтарифный+",
             "resource_type": "Электроэнергия",
             "value1": 7000.00, "value2": 3500.00, "value3": 200.00, "value4": 100.00},
        ],
    },
    {
        "ls":      "100004",
        "fio":     "Козлова Елена Николаевна",
        "address": "ул. Советская, 44, кв. 8",
        "meters":  [
            {"meter_number": "СЭ-005", "meter_type": "Однотарифный",
             "resource_type": "Электроэнергия",
             "value1": 3210.50, "value2": None, "value3": None, "value4": None},
            {"meter_number": "СЭ-006", "meter_type": "Однотарифный",
             "resource_type": "Электроэнергия",
             "value1": 1540.80, "value2": None, "value3": None, "value4": None},
        ],
    },
]

conn = get_conn()

for acc in accounts:
    ls      = acc["ls"]
    fio     = acc["fio"]
    address = acc["address"]

    # Лицевой счёт
    conn.execute(
        "INSERT OR REPLACE INTO licschet (number, fio, address) VALUES (?, ?, ?)",
        (ls, fio, address)
    )

    for m in acc["meters"]:
        # Счётчик
        conn.execute(
            """INSERT OR IGNORE INTO schetchiki
               (ls, resource_type, meter_number, meter_type, initial1, initial2)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                ls,
                m["resource_type"],
                m["meter_number"],
                m["meter_type"],
                str(m["value1"]),
                str(m["value2"]) if m["value2"] is not None else "0",
            )
        )

        # Начальные показания в pokazaniya (только если ещё нет записи)
        existing = conn.execute(
            "SELECT id FROM pokazaniya WHERE ls=? AND meter_number=? LIMIT 1",
            (ls, m["meter_number"])
        ).fetchone()

        if not existing:
            conn.execute(
                """INSERT INTO pokazaniya
                   (chat_id, ls, resource_type, meter_number, value1, value2, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    0,
                    ls,
                    m["resource_type"],
                    m["meter_number"],
                    str(m["value1"]),
                    str(m["value2"]) if m["value2"] is not None else None,
                    "2000-01-01 00:00",
                )
            )

    conn.commit()
    print(f"  ✓ ЛС {ls}  {address}")

conn.close()

print()
print("Тестовые данные загружены!")
print()
print("Лицевые счета для проверки бота:")
for acc in accounts:
    print(f"  {acc['ls']}  —  {acc['address']}")
    for m in acc["meters"]:
        mtype = m["meter_type"]
        if mtype == "Однотарифный":
            readings = f"показание: {m['value1']}"
        elif mtype == "Двухтарифный":
            readings = f"Т1: {m['value1']}  Т2: {m['value2']}"
        elif mtype == "Двунаправленный":
            readings = f"приход: {m['value1']}  отдача: {m['value2']}"
        else:
            readings = f"Т1: {m['value1']}  Т2: {m['value2']}  отд.д: {m['value3']}  отд.н: {m['value4']}"
        print(f"    № {m['meter_number']}  [{mtype}]  {readings}")
    print()
