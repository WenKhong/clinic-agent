from fastapi import FastAPI
from app.db import get_conn

app = FastAPI(title="Clinic Agent")

@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}

@app.get("/health/db")
def health_db():
    with get_conn() as conn:
        conn.execute("SELECT 1")
    return {"db": "ok"}