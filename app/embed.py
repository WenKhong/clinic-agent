"""Embed FAQ chunks and service descriptions with Gemini.

Run:  uv run python -m app.embed

Only rows whose embedding is NULL are embedded, so it's safe to re-run. When
the loader changes a row's text it clears that row's embedding, and the next
run re-embeds just those rows.

The API key comes only from GEMINI_API_KEY in .env (never from code).
"""

import os
from collections.abc import Callable

import psycopg
from google import genai
from google.genai import types

from app.db import get_conn  # importing app.db also loads .env into os.environ

MODEL = "gemini-embedding-2"
DIMENSIONS = 1536  # must match vector(1536) in db/schema.sql
BATCH_SIZE = 100

# Takes a list of texts, returns one vector per text (same order).
Embedder = Callable[[list[str]], list[list[float]]]

FAQ_PENDING = "SELECT id, content FROM faq_chunks WHERE embedding IS NULL ORDER BY id"
SERVICE_PENDING = (
    "SELECT id, name, description FROM services WHERE embedding IS NULL ORDER BY id"
)

# The extra text check means: if the text changed while we were calling the
# API, don't save an embedding made from the old text.
FAQ_SAVE = """
    UPDATE faq_chunks SET embedding = %s::vector
    WHERE id = %s AND content = %s
"""
SERVICE_SAVE = """
    UPDATE services SET embedding = %s::vector
    WHERE id = %s AND description IS NOT DISTINCT FROM %s
"""


def document_text(title: str | None, text: str) -> str:
    """Format a document for gemini-embedding-2.

    This model has no task_type setting; the task goes in the text instead.
    Search queries will need the matching "task: search result | query: ..."
    form when we add retrieval.
    """
    return f"title: {title or 'none'} | text: {text}"


def gemini_embedder(api_key: str) -> Embedder:
    client = genai.Client(api_key=api_key)

    def embed(texts: list[str]) -> list[list[float]]:
        result = client.models.embed_content(
            model=MODEL,
            # One Content per text. A plain list of strings would be merged
            # into a single embedding by this model.
            contents=[types.Content(parts=[types.Part(text=t)]) for t in texts],
            config=types.EmbedContentConfig(output_dimensionality=DIMENSIONS),
        )
        return [e.values for e in result.embeddings]

    return embed


def embed_all(embed: Embedder, texts: list[str]) -> list[list[float]]:
    """Embed texts in batches and check every vector has the right size."""
    vectors: list[list[float]] = []
    for start in range(0, len(texts), BATCH_SIZE):
        batch = texts[start : start + BATCH_SIZE]
        result = embed(batch)
        if len(result) != len(batch):
            raise RuntimeError(f"asked for {len(batch)} embeddings, got {len(result)}")
        for vector in result:
            if len(vector) != DIMENSIONS:
                raise RuntimeError(f"expected {DIMENSIONS} dimensions, got {len(vector)}")
        vectors.extend(result)
    return vectors


def _vector_literal(vector: list[float]) -> str:
    # pgvector's text form, e.g. "[0.1,0.2,...]"; cast with ::vector in SQL.
    return "[" + ",".join(str(x) for x in vector) + "]"


def embed_pending(conn: psycopg.Connection, embed: Embedder) -> dict[str, int]:
    """Embed every FAQ chunk and service that has no embedding yet.

    Does not commit: the caller owns the transaction. Returns rows embedded.
    """
    faqs = conn.execute(FAQ_PENDING).fetchall()
    faq_vectors = embed_all(embed, [document_text(None, content) for _, content in faqs])
    with conn.cursor() as cur:
        cur.executemany(
            FAQ_SAVE,
            [
                (_vector_literal(v), faq_id, content)
                for v, (faq_id, content) in zip(faq_vectors, faqs, strict=True)
            ],
        )

    services = conn.execute(SERVICE_PENDING).fetchall()
    service_vectors = embed_all(
        embed, [document_text(name, description or name) for _, name, description in services]
    )
    with conn.cursor() as cur:
        cur.executemany(
            SERVICE_SAVE,
            [
                (_vector_literal(v), service_id, description)
                for v, (service_id, _, description) in zip(service_vectors, services, strict=True)
            ],
        )

    return {"faq_chunks": len(faqs), "services": len(services)}


def main() -> None:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("GEMINI_API_KEY is empty: paste your key into .env (see .env.example).")

    embed = gemini_embedder(api_key)
    with get_conn() as conn:  # commits on success, rolls back on any error
        counts = embed_pending(conn, embed)
    for table, n in counts.items():
        print(f"{table}: {n} rows embedded")


if __name__ == "__main__":
    main()
