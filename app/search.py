"""Find the FAQ chunks that best match a question, three ways:

- search():         vector (meaning), pgvector cosine distance
- keyword_search(): keyword (exact words), Postgres full-text search
- hybrid_search():  both, merged with reciprocal rank fusion (RRF)

They are kept apart so they can be compared. Run all three side by side:

    uv run python -m app.search "what are your opening hours?"

The query is embedded with the same model as the chunks (gemini_embedder in
app/embed.py). Vectors from a different model would give meaningless distances.
"""

import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass, replace

import psycopg

from app.db import get_conn  # importing app.db also loads .env into os.environ
from app.embed import Embedder, _vector_literal, embed_all, gemini_embedder, query_text

# <=> is pgvector's cosine distance: 0 = same direction, up to 2 = opposite.
# There is no vector index (see db/schema.sql), so this is an exact scan. We
# still ORDER BY the raw distance, not by score, so an HNSW index added later
# would be used. Paste this into psql (with a vector in place of %(q)s) to
# debug retrieval.
SEARCH_SQL = """
    SELECT id, source, content,
           1 - (embedding <=> %(q)s::vector) AS score
    FROM faq_chunks
    WHERE embedding IS NOT NULL
    ORDER BY embedding <=> %(q)s::vector
    LIMIT %(k)s
"""

# content_tsv is to_tsvector('simple', content), with a GIN index
# (faq_chunks_content_tsv_idx). 'simple' = lowercase only, no stemming and no
# stop words, so Malay is treated the same as English.
# plainto_tsquery turns the text into quoted words joined by & (AND), which
# also means user text can't inject tsquery operators. We swap & for | (OR):
# with AND, "How much do I pay to see the doctor?" would need every word in one
# chunk and find nothing. ts_rank puts chunks sharing more words first.
KEYWORD_SQL = """
    WITH q AS (
        SELECT replace(plainto_tsquery('simple', %(text)s)::text, ' & ', ' | ')::tsquery AS query
    )
    SELECT id, source, content,
           ts_rank(content_tsv, q.query) AS score
    FROM faq_chunks, q
    WHERE content_tsv @@ q.query
    ORDER BY score DESC, id
    LIMIT %(k)s
"""

# The usual RRF constant (Cormack et al., 2009). It stops rank 1 from swamping
# everything else: rank 1 is worth 1/61, rank 2 is 1/62.
RRF_K = 60


@dataclass(frozen=True)
class Hit:
    id: int
    source: str
    content: str
    # Higher = better match. The meaning depends on the function that returned it:
    # search() 1 - cosine distance, keyword_search() ts_rank, hybrid_search() RRF score.
    score: float


def _check(query: str, k: int) -> None:
    if not query.strip():
        raise ValueError("query is empty")
    if k < 1:
        raise ValueError("k must be at least 1")


def search(conn: psycopg.Connection, embed: Embedder, query: str, k: int = 5) -> list[Hit]:
    """Return the k FAQ chunks most similar in meaning to query, best first."""
    _check(query, k)
    [vector] = embed_all(embed, [query_text(query)])  # also checks the vector size
    rows = conn.execute(SEARCH_SQL, {"q": _vector_literal(vector), "k": k}).fetchall()
    return [Hit(*row) for row in rows]


def keyword_search(conn: psycopg.Connection, query: str, k: int = 5) -> list[Hit]:
    """Return up to k FAQ chunks sharing words with query, best first.

    Chunks sharing no word with the query are not returned, so this can be [].
    """
    _check(query, k)
    rows = conn.execute(KEYWORD_SQL, {"text": query, "k": k}).fetchall()
    return [Hit(*row) for row in rows]


def rrf(rankings: Sequence[Sequence[Hit]], k_rrf: int = RRF_K) -> list[Hit]:
    """Merge ranked lists with reciprocal rank fusion.

    Each chunk scores 1 / (k_rrf + rank) in every list it appears in (rank
    starts at 1), summed over the lists. Only the order inside each list counts,
    so it doesn't matter that cosine scores and ts_rank are on different scales.
    Ties go to the lower id, so the order is stable.
    """
    scores: dict[int, float] = {}
    first_seen: dict[int, Hit] = {}
    for ranking in rankings:
        for rank, hit in enumerate(ranking, start=1):
            scores[hit.id] = scores.get(hit.id, 0.0) + 1 / (k_rrf + rank)
            first_seen.setdefault(hit.id, hit)
    order = sorted(scores, key=lambda i: (-scores[i], i))
    return [replace(first_seen[i], score=scores[i]) for i in order]


def hybrid_search(
    conn: psycopg.Connection, embed: Embedder, query: str, k: int = 5, pool: int = 20
) -> list[Hit]:
    """Return the top k chunks after fusing vector and keyword results with RRF.

    Each method contributes its top `pool` chunks. If one finds nothing (for
    example no shared words), the result is simply the other's ranking.
    """
    _check(query, k)
    vector_hits = search(conn, embed, query, pool)
    keyword_hits = keyword_search(conn, query, pool)
    return rrf([vector_hits, keyword_hits])[:k]


def main() -> None:
    query = " ".join(sys.argv[1:])
    if not query.strip():
        raise SystemExit('usage: uv run python -m app.search "your question"')
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("GEMINI_API_KEY is empty: paste your key into .env (see .env.example).")

    embed = gemini_embedder(api_key)
    with get_conn() as conn:
        results = {
            "vector": search(conn, embed, query),
            "keyword": keyword_search(conn, query),
            "hybrid": hybrid_search(conn, embed, query),
        }
    for name, hits in results.items():
        print(f"== {name}")
        if not hits:
            print("   (no matches)")
        for hit in hits:
            print(f"   {hit.score:.4f}  {hit.source}  {hit.content[:80]!r}")


if __name__ == "__main__":
    main()
