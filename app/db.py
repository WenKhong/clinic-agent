import os

import psycopg
from dotenv import load_dotenv

load_dotenv()  # reads .env → puts DATABASE_URL into os.environ


def get_conn() -> psycopg.Connection:
    """Open a new connection to Postgres."""
    return psycopg.connect(os.environ["DATABASE_URL"])