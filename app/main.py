import os
from collections.abc import Iterator
from typing import Annotated

import psycopg
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, StringConstraints

from app.ask import Answerer, AskResult, ask, gemini_answerer
from app.db import get_conn
from app.embed import Embedder, gemini_embedder

app = FastAPI(title="Clinic Agent")

@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}

@app.get("/health/db")
def health_db():
    with get_conn() as conn:
        conn.execute("SELECT 1")
    return {"db": "ok"}


# Dependencies: tests replace these with app.dependency_overrides.
def get_db() -> Iterator[psycopg.Connection]:
    with get_conn() as conn:
        yield conn


def _gemini_key() -> str:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise HTTPException(status_code=503, detail="GEMINI_API_KEY is not set")
    return key


def get_embedder() -> Embedder:
    return gemini_embedder(_gemini_key())


def get_answerer() -> Answerer:
    return gemini_answerer(_gemini_key())


class AskRequest(BaseModel):
    question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


@app.post("/ask")
def ask_endpoint(
    body: AskRequest,
    conn: Annotated[psycopg.Connection, Depends(get_db)],
    embed: Annotated[Embedder, Depends(get_embedder)],
    answer: Annotated[Answerer, Depends(get_answerer)],
) -> AskResult:
    return ask(conn, embed, answer, body.question)
