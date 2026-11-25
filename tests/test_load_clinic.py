import copy
from datetime import time
from decimal import Decimal

import pytest
from psycopg import sql

from app.db import get_conn
from app.load_clinic import DEFAULT_PATH, ClinicDataError, load_clinic, read_clinic_yaml

# Every DB test ends with conn.rollback(), so nothing it loads is kept.

TABLES = ("services", "doctors", "faq_chunks", "opening_hours")


def count_rows(conn) -> dict[str, int]:
    # Table names can't be %s parameters; sql.Identifier quotes them safely.
    query = sql.SQL("SELECT count(*) FROM {}")
    return {t: conn.execute(query.format(sql.Identifier(t))).fetchone()[0] for t in TABLES}


def test_load_twice_no_duplicates():
    data = read_clinic_yaml(DEFAULT_PATH)
    with get_conn() as conn:
        load_clinic(conn, data)
        after_first = count_rows(conn)
        load_clinic(conn, data)
        after_second = count_rows(conn)

        assert after_second == after_first

        # Every YAML entry is in the DB.
        service_names = [s["name"] for s in data["services"]]
        doctor_names = [d["full_name"] for d in data["doctors"]]
        faq_sources = ["clinic.yaml#" + f["id"] for f in data["faqs"]]
        assert conn.execute(
            "SELECT count(*) FROM services WHERE name = ANY(%s)", (service_names,)
        ).fetchone()[0] == len(service_names)
        assert conn.execute(
            "SELECT count(*) FROM doctors WHERE full_name = ANY(%s)", (doctor_names,)
        ).fetchone()[0] == len(doctor_names)
        assert conn.execute(
            "SELECT count(*) FROM faq_chunks WHERE source = ANY(%s)", (faq_sources,)
        ).fetchone()[0] == len(faq_sources)
        # Hours are replaced, so the table matches the YAML exactly.
        sessions = sum(len(s) for s in data["hours"]["weekly"].values())
        assert after_second["opening_hours"] == sessions

        conn.rollback()


def test_loaded_values():
    with get_conn() as conn:
        load_clinic(conn, read_clinic_yaml(DEFAULT_PATH))

        price, minutes = conn.execute(
            "SELECT price_myr, duration_minutes FROM services WHERE name = %s",
            ("General consultation",),
        ).fetchone()
        assert price == Decimal("40.00")
        assert minutes == 15

        languages = conn.execute(
            "SELECT languages FROM doctors WHERE full_name = %s", ("Dr. Tan Wei Ling",)
        ).fetchone()[0]
        assert languages == ["en", "zh", "yue"]

        friday = conn.execute(
            "SELECT opens_at, closes_at FROM opening_hours WHERE weekday = 5 ORDER BY opens_at"
        ).fetchall()
        assert friday == [(time(8, 30), time(12, 30)), (time(14, 45), time(21, 0))]

        sunday = conn.execute("SELECT count(*) FROM opening_hours WHERE weekday = 7")
        assert sunday.fetchone()[0] == 0

        conn.rollback()


def test_reload_updates_changed_value():
    data = read_clinic_yaml(DEFAULT_PATH)
    changed = copy.deepcopy(data)
    changed["services"][0]["price_myr"] = 45.00
    name = changed["services"][0]["name"]

    with get_conn() as conn:
        load_clinic(conn, data)
        before = count_rows(conn)
        load_clinic(conn, changed)

        assert count_rows(conn) == before
        price = conn.execute(
            "SELECT price_myr FROM services WHERE name = %s", (name,)
        ).fetchone()[0]
        assert price == Decimal("45.00")

        conn.rollback()


def test_faq_embedding_cleared_only_when_text_changes():
    data = read_clinic_yaml(DEFAULT_PATH)
    source = "clinic.yaml#" + data["faqs"][0]["id"]
    fake_embedding = "[" + ",".join(["0.1"] * 1536) + "]"

    def embedding_is_null(conn) -> bool:
        return conn.execute(
            "SELECT embedding IS NULL FROM faq_chunks WHERE source = %s", (source,)
        ).fetchone()[0]

    with get_conn() as conn:
        load_clinic(conn, data)
        conn.execute(
            "UPDATE faq_chunks SET embedding = %s::vector WHERE source = %s",
            (fake_embedding, source),
        )

        load_clinic(conn, data)  # same text: embedding kept
        assert not embedding_is_null(conn)

        changed = copy.deepcopy(data)
        changed["faqs"][0]["answer"] += " (updated)"
        load_clinic(conn, changed)  # new text: embedding cleared
        assert embedding_is_null(conn)

        conn.rollback()


@pytest.mark.parametrize(
    ("old", "new", "error"),
    [
        # Unquoted 13:00 is read by YAML as the number 780.
        ('close: "13:00"', "close: 13:00", "quoted"),
        ("fictional: true", "fictional: false", "fictional"),
    ],
)
def test_bad_yaml_rejected(tmp_path, old, new, error):
    original = DEFAULT_PATH.read_text(encoding="utf-8")
    broken = original.replace(old, new, 1)
    assert broken != original  # make sure the edit happened

    path = tmp_path / "clinic.yaml"
    path.write_text(broken, encoding="utf-8")

    with pytest.raises(ClinicDataError, match=error):
        read_clinic_yaml(path)
