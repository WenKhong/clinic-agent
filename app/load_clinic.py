"""Load the clinic data file (data/clinic.yaml) into Postgres.

Run:  uv run python -m app.load_clinic [path/to/clinic.yaml]

Safe to run again and again (idempotent): each row is matched on its unique
key and updated in place, so a second run adds no duplicates.
"""

import argparse
from datetime import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import yaml

from app.db import get_conn

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "clinic.yaml"

REQUIRED_KEYS = {"clinic", "hours", "doctors", "services", "faqs"}
WEEKDAYS = {"mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6, "sun": 7}  # ISO
FAQ_SOURCE_PREFIX = "clinic.yaml#"
FAQ_LANGUAGE = "en"

# The embedding is made from name + description; name is the conflict key so
# it can't change here, but if the description changed, clear the embedding.
UPSERT_SERVICE = """
    INSERT INTO services (name, description, duration_minutes, price_myr)
    VALUES (%s, %s, %s, %s)
    ON CONFLICT (name) DO UPDATE SET
        description      = EXCLUDED.description,
        duration_minutes = EXCLUDED.duration_minutes,
        price_myr        = EXCLUDED.price_myr,
        embedding        = CASE WHEN services.description IS NOT DISTINCT FROM EXCLUDED.description
                                THEN services.embedding END
"""

UPSERT_DOCTOR = """
    INSERT INTO doctors (full_name, specialty, languages)
    VALUES (%s, %s, %s)
    ON CONFLICT (full_name) DO UPDATE SET
        specialty = EXCLUDED.specialty,
        languages = EXCLUDED.languages
"""

# If the FAQ text changed, its old embedding no longer matches it, so clear it
# (a CASE with no ELSE gives NULL) and let it be re-embedded later.
UPSERT_FAQ = """
    INSERT INTO faq_chunks (source, language, content)
    VALUES (%s, %s, %s)
    ON CONFLICT (source, language) DO UPDATE SET
        content   = EXCLUDED.content,
        embedding = CASE WHEN faq_chunks.content = EXCLUDED.content
                         THEN faq_chunks.embedding END
"""

# Opening hours are replaced wholesale, so a day changed to [] really closes.
DELETE_HOURS = "DELETE FROM opening_hours"
INSERT_HOURS = """
    INSERT INTO opening_hours (weekday, opens_at, closes_at)
    VALUES (%s, %s, %s)
"""


class ClinicDataError(ValueError):
    """The clinic YAML has a value we can't or shouldn't load."""


def parse_time(value: Any, where: str) -> time:
    """Turn "HH:MM" into a time. YAML reads unquoted 13:00 as the number 780."""
    if not isinstance(value, str):
        raise ClinicDataError(f'{where}: {value!r} must be a quoted "HH:MM" string')
    try:
        return time.fromisoformat(value)
    except ValueError:
        raise ClinicDataError(f'{where}: {value!r} is not a valid "HH:MM" time') from None


def validate_clinic(data: Any) -> None:
    """Raise ClinicDataError if the data is not safe to load."""
    if not isinstance(data, dict):
        raise ClinicDataError("clinic YAML must be a mapping at the top level")
    missing = REQUIRED_KEYS - data.keys()
    if missing:
        raise ClinicDataError(f"clinic YAML is missing: {', '.join(sorted(missing))}")

    # PDPA: this project only ever loads made-up data.
    if not isinstance(data["clinic"], dict) or data["clinic"].get("fictional") is not True:
        raise ClinicDataError("refusing to load: clinic.fictional must be true")

    weekly = data["hours"]["weekly"]
    if set(weekly) != set(WEEKDAYS):
        raise ClinicDataError(f"hours.weekly must list exactly: {', '.join(WEEKDAYS)}")
    for day, sessions in weekly.items():
        for session in sessions:
            for key in ("open", "close"):
                parse_time(session[key], where=f"hours.weekly.{day}.{key}")


def read_clinic_yaml(path: Path = DEFAULT_PATH) -> dict[str, Any]:
    """Read and check the clinic YAML. safe_load only builds plain data types."""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    validate_clinic(data)
    return data


def _price(value: Any) -> Decimal | None:
    # YAML gives floats; go via str so 40.1 doesn't become 40.0999999...
    return None if value is None else Decimal(str(value))


def load_clinic(conn: psycopg.Connection, data: dict[str, Any]) -> dict[str, int]:
    """Write the clinic data to the DB. Returns rows written per table.

    Does not commit: the caller owns the transaction, so either everything is
    loaded or (on error / rollback) nothing is.
    """
    validate_clinic(data)

    services = [
        (s["name"], s.get("description"), s["duration_minutes"], _price(s.get("price_myr")))
        for s in data["services"]
    ]
    doctors = [(d["full_name"], d["specialty"], d["languages"]) for d in data["doctors"]]
    faqs = [
        (FAQ_SOURCE_PREFIX + f["id"], FAQ_LANGUAGE, f"Q: {f['question']}\nA: {f['answer']}")
        for f in data["faqs"]
    ]
    hours = [
        (
            WEEKDAYS[day],
            parse_time(s["open"], where=f"hours.weekly.{day}.open"),
            parse_time(s["close"], where=f"hours.weekly.{day}.close"),
        )
        for day, sessions in data["hours"]["weekly"].items()
        for s in sessions
    ]

    with conn.cursor() as cur:
        cur.executemany(UPSERT_SERVICE, services)
        cur.executemany(UPSERT_DOCTOR, doctors)
        cur.executemany(UPSERT_FAQ, faqs)
        cur.execute(DELETE_HOURS)
        cur.executemany(INSERT_HOURS, hours)

    return {
        "services": len(services),
        "doctors": len(doctors),
        "faq_chunks": len(faqs),
        "opening_hours": len(hours),
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Load clinic YAML into Postgres.")
    parser.add_argument("path", nargs="?", type=Path, default=DEFAULT_PATH)
    args = parser.parse_args(argv)

    data = read_clinic_yaml(args.path)
    with get_conn() as conn:  # commits on success, rolls back on any error
        counts = load_clinic(conn, data)
    for table, n in counts.items():
        print(f"{table}: {n} rows loaded")


if __name__ == "__main__":
    main()
