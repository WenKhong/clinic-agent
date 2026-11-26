"""Find the FAQ chunks closest in meaning to a question.

Run:  uv run python -m app.search "what are your opening hours?"

The query is embedded with the same model as the chunks (gemini_embedder in
app/embed.py). Vectors from a different model would give meaningless distances.
"""

import os
import sys
from dataclasses import dataclass

import psycopg

from app.db import get_conn  # importing app.db also loads .env into os.environ
from app.embed import Embedder, _vector_literal, embed_all, gemini_embedder, query_text

# <=> is pgvector's cosine distance: 0 = same direction, up to 2 = opposite.
# We ORDER BY the raw distance, not by score, so Postgres can use the HNSW
# index (faq_chunks_embedding_idx, built with vector_cosine_ops). Paste this
# into psql (with a vector in place of %(q)s) to debug retrieval.
SEARCH_SQL = """
    SELECT id, source, content,
           1 - (embedding <=> %(q)s::vector) AS score
    FROM faq_chunks
    WHERE embedding IS NOT NULL
    ORDER BY embedding <=> %(q)s::vector
    LIMIT %(k)s
"""


@dataclass(frozen=True)
class Hit:
    id: int
    source: str
    content: str
    score: float  # 1 - cosine distance; higher = more similar


def search(conn: psycopg.Connection, embed: Embedder, query: str, k: int = 5) -> list[Hit]:
    """Return the k FAQ chunks most similar to query, best first."""
    if not query.strip():
        raise ValueError("query is empty")
    if k < 1:
        raise ValueError("k must be at least 1")

    [vector] = embed_all(embed, [query_text(query)])  # also checks the vector size
    rows = conn.execute(SEARCH_SQL, {"q": _vector_literal(vector), "k": k}).fetchall()
    return [Hit(*row) for row in rows]


def main() -> None:
    query = " ".join(sys.argv[1:])
    if not query.strip():
        raise SystemExit('usage: uv run python -m app.search "your question"')
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("GEMINI_API_KEY is empty: paste your key into .env (see .env.example).")

    with get_conn() as conn:
        hits = search(conn, gemini_embedder(api_key), query)
    for hit in hits:
        print(f"{hit.score:.3f}  {hit.source}  {hit.content[:80]!r}")


if __name__ == "__main__":
    main()
