import pytest

from app.db import get_conn
from app.embed import DIMENSIONS, _vector_literal
from app.load_clinic import DEFAULT_PATH, load_clinic, read_clinic_yaml
from app.search import search

# These tests never call Gemini: QueryEmbedder stands in for it.


def unit_vector(i: int) -> list[float]:
    """A vector pointing along axis i. Two different axes have cosine distance 1."""
    v = [0.0] * DIMENSIONS
    v[i] = 1.0
    return v


class QueryEmbedder:
    """Returns the same vector for every text and records what it was given."""

    def __init__(self, vector: list[float]):
        self.vector = vector
        self.texts: list[str] = []

    def __call__(self, texts: list[str]) -> list[list[float]]:
        self.texts.extend(texts)
        return [self.vector for _ in texts]


@pytest.fixture
def conn():
    """Sample clinic data where every chunk points along axis 1, except one
    chunk (the "target") that points along axis 0. Rolled back afterwards."""
    with get_conn() as conn:
        load_clinic(conn, read_clinic_yaml(DEFAULT_PATH))
        conn.execute(
            "UPDATE faq_chunks SET embedding = %s::vector", (_vector_literal(unit_vector(1)),)
        )
        yield conn
        conn.rollback()


def first_chunk_id(conn) -> int:
    return conn.execute("SELECT min(id) FROM faq_chunks").fetchone()[0]


def point_at_axis_0(conn, chunk_id: int) -> None:
    conn.execute(
        "UPDATE faq_chunks SET embedding = %s::vector WHERE id = %s",
        (_vector_literal(unit_vector(0)), chunk_id),
    )


def test_closest_chunk_comes_first_with_score_one(conn):
    target = first_chunk_id(conn)
    point_at_axis_0(conn, target)

    hits = search(conn, QueryEmbedder(unit_vector(0)), "what are your opening hours?", k=3)

    assert hits[0].id == target
    assert hits[0].score == pytest.approx(1.0)
    assert all(h.score == pytest.approx(0.0) for h in hits[1:])  # orthogonal = distance 1


def test_returns_at_most_k_hits_best_first(conn):
    point_at_axis_0(conn, first_chunk_id(conn))

    hits = search(conn, QueryEmbedder(unit_vector(0)), "parking", k=4)

    assert len(hits) == 4
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)


def test_skips_chunks_without_embedding(conn):
    target = first_chunk_id(conn)
    point_at_axis_0(conn, target)
    conn.execute("UPDATE faq_chunks SET embedding = NULL WHERE id = %s", (target,))
    embedded = conn.execute(
        "SELECT count(*) FROM faq_chunks WHERE embedding IS NOT NULL"
    ).fetchone()[0]

    hits = search(conn, QueryEmbedder(unit_vector(0)), "opening hours", k=1000)

    assert target not in [h.id for h in hits]
    assert len(hits) == embedded


def test_query_is_sent_in_search_format(conn):
    embedder = QueryEmbedder(unit_vector(0))

    search(conn, embedder, "is there parking?", k=1)

    assert embedder.texts == ["task: search result | query: is there parking?"]


@pytest.mark.parametrize(("query", "k"), [("", 5), ("   ", 5), ("hello", 0)])
def test_rejects_empty_query_or_bad_k(conn, query, k):
    with pytest.raises(ValueError):
        search(conn, QueryEmbedder(unit_vector(0)), query, k=k)
