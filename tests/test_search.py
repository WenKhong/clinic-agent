import pytest

from app.db import get_conn
from app.embed import DIMENSIONS, _vector_literal
from app.load_clinic import (
    DEFAULT_PATH,
    FAQ_SOURCE_PREFIX,
    load_clinic,
    read_clinic_yaml,
)
from app.search import Hit, hybrid_search, keyword_search, rrf, search

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


# --- keyword_search: Postgres full-text over content_tsv ---


def chunk_id(conn, faq_id: str) -> int:
    return conn.execute(
        "SELECT id FROM faq_chunks WHERE source = %s", (FAQ_SOURCE_PREFIX + faq_id,)
    ).fetchone()[0]


@pytest.mark.parametrize("query", ["MyKad", "mykad", "Do I need my MYKAD?"])
def test_keyword_finds_exact_word_any_case(conn, query):
    hits = keyword_search(conn, query, k=3)

    assert hits[0].id == chunk_id(conn, "what-to-bring")


def test_keyword_matches_any_word_not_all(conn):
    hits = keyword_search(conn, "parking xyzzy", k=3)

    assert chunk_id(conn, "parking") in [h.id for h in hits]


@pytest.mark.parametrize("query", ["qwertyuiop", "?!", "..."])
def test_keyword_returns_nothing_without_shared_words(conn, query):
    assert keyword_search(conn, query, k=5) == []


def test_keyword_returns_at_most_k_best_first(conn):
    hits = keyword_search(conn, "the clinic", k=3)

    assert len(hits) == 3
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.parametrize(("query", "k"), [("", 5), ("   ", 5), ("hello", 0)])
def test_keyword_rejects_empty_query_or_bad_k(conn, query, k):
    with pytest.raises(ValueError):
        keyword_search(conn, query, k=k)


# --- rrf: pure function, no database ---


def hit(i: int) -> Hit:
    return Hit(id=i, source=f"s{i}", content=f"c{i}", score=0.0)


def test_rrf_rewards_being_in_both_lists():
    # 2 is 2nd in both lists; 1 and 3 are each 1st in only one.
    fused = rrf([[hit(1), hit(2)], [hit(3), hit(2)]])

    assert fused[0].id == 2
    assert fused[0].score == pytest.approx(1 / 62 + 1 / 62)
    assert [h.id for h in fused] == [2, 1, 3]  # 1 and 3 tie at 1/61; lower id first


def test_rrf_lists_each_chunk_once():
    fused = rrf([[hit(1), hit(2)], [hit(2), hit(1)], [hit(1)]])

    assert sorted(h.id for h in fused) == [1, 2]
    assert fused[0].id == 1
    assert fused[0].score == pytest.approx(1 / 61 + 1 / 62 + 1 / 61)


def test_rrf_with_one_empty_list_keeps_the_other_order():
    assert [h.id for h in rrf([[hit(5), hit(3), hit(9)], []])] == [5, 3, 9]


# --- hybrid_search: vector + keyword fused ---


def test_hybrid_keeps_best_of_each_method(conn):
    vector_best = first_chunk_id(conn)  # opening-hours; doesn't contain "parking"
    point_at_axis_0(conn, vector_best)
    keyword_best = chunk_id(conn, "parking")

    hits = hybrid_search(conn, QueryEmbedder(unit_vector(0)), "parking", k=2)

    assert {h.id for h in hits} == {vector_best, keyword_best}


def test_hybrid_puts_chunk_top_in_both_first(conn):
    target = chunk_id(conn, "parking")
    point_at_axis_0(conn, target)

    hits = hybrid_search(conn, QueryEmbedder(unit_vector(0)), "parking", k=3)

    assert hits[0].id == target
    assert hits[0].score == pytest.approx(2 / 61)


@pytest.mark.parametrize(("query", "k"), [("", 5), ("hello", 0)])
def test_hybrid_rejects_empty_query_or_bad_k(conn, query, k):
    with pytest.raises(ValueError):
        hybrid_search(conn, QueryEmbedder(unit_vector(0)), query, k=k)
