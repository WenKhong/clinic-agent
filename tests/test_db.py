from app.db import get_conn


def test_service_round_trip():
    with get_conn() as conn:
        with conn.cursor() as cur:
            # insert one service, get its id back
            cur.execute(
                "INSERT INTO services (name, duration_minutes) VALUES (%s, %s) RETURNING id",
                ("Test consultation",15),
            )
            service_id = cur.fetchone()[0]

            # read it back by id
            cur.execute(
                "SELECT name, duration_minutes FROM services WHERE id = %s", (service_id,),)
            name, duration = cur.fetchone()
            assert name == "Test consultation"
            assert duration == 15

        conn.rollback()  # undo the insert so the DB stays clean