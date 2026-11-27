import pytest
from fastapi.testclient import TestClient

from app.ask import NO_ANSWER_REPLY, Draft
from app.db import get_conn
from app.embed import DIMENSIONS, _vector_literal
from app.load_clinic import DEFAULT_PATH, load_clinic, read_clinic_yaml
from app.main import app, get_answerer, get_db, get_embedder
from app.safety import EMERGENCY_REPLY

# These tests never call Gemini: fakes stand in for the embedder and the LLM.


def unit_vector(i: int) -> list[float]:
    v = [0.0] * DIMENSIONS
    v[i] = 1.0
    return v


class FakeEmbedder:
    """Embeds every query as axis 0, so the chunk set to axis 0 ranks first."""

    def __init__(self):
        self.calls = 0

    def __call__(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [unit_vector(0) for _ in texts]


class FakeAnswerer:
    """Returns a fixed draft and records the hit ids it was shown."""

    def __init__(self, draft: Draft):
        self.draft = draft
        self.shown: list[list[int]] = []

    def __call__(self, question, hits):
        self.shown.append([h.id for h in hits])
        return self.draft


@pytest.fixture
def conn():
    """Sample data; every chunk on axis 1 except the target chunk on axis 0."""
    with get_conn() as conn:
        load_clinic(conn, read_clinic_yaml(DEFAULT_PATH))
        conn.execute(
            "UPDATE faq_chunks SET embedding = %s::vector", (_vector_literal(unit_vector(1)),)
        )
        yield conn
        conn.rollback()


@pytest.fixture
def target(conn) -> tuple[int, str]:
    chunk_id, source = conn.execute(
        "SELECT id, source FROM faq_chunks ORDER BY id LIMIT 1"
    ).fetchone()
    conn.execute(
        "UPDATE faq_chunks SET embedding = %s::vector WHERE id = %s",
        (_vector_literal(unit_vector(0)), chunk_id),
    )
    return chunk_id, source


@pytest.fixture
def make_client(conn):
    """Builds a TestClient wired to the test DB connection and the given fakes."""

    def build(embedder, answerer) -> TestClient:
        app.dependency_overrides[get_db] = lambda: conn
        app.dependency_overrides[get_embedder] = lambda: embedder
        app.dependency_overrides[get_answerer] = lambda: answerer
        return TestClient(app)

    yield build
    app.dependency_overrides.clear()


def test_answers_with_citation(make_client, target):
    target_id, target_source = target
    answerer = FakeAnswerer(Draft(answered=True, answer="We open at 8:30am.", cited_ids=[target_id]))

    r = make_client(FakeEmbedder(), answerer).post("/ask", json={"question": "When do you open?"})

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "answered"
    assert body["answer"] == "We open at 8:30am."
    assert [(c["id"], c["source"]) for c in body["citations"]] == [(target_id, target_source)]
    assert body["citations"][0]["score"] == pytest.approx(1.0)
    assert answerer.shown[0][0] == target_id  # LLM saw the best hit first


def test_drops_citations_that_were_not_retrieved(make_client, target):
    target_id, _ = target
    answerer = FakeAnswerer(Draft(answered=True, answer="...", cited_ids=[999_999, target_id]))

    body = make_client(FakeEmbedder(), answerer).post("/ask", json={"question": "hours?"}).json()

    assert [c["id"] for c in body["citations"]] == [target_id]


def test_no_answer_when_only_invented_citations(make_client, target):
    answerer = FakeAnswerer(Draft(answered=True, answer="Made-up fact.", cited_ids=[999_999]))

    body = make_client(FakeEmbedder(), answerer).post("/ask", json={"question": "Botox?"}).json()

    assert body == {"status": "no_answer", "answer": NO_ANSWER_REPLY, "citations": []}


def test_no_answer_when_llm_says_not_answered(make_client, target):
    target_id, _ = target
    answerer = FakeAnswerer(Draft(answered=False, answer="I think maybe...", cited_ids=[target_id]))

    body = make_client(FakeEmbedder(), answerer).post("/ask", json={"question": "Botox?"}).json()

    assert body == {"status": "no_answer", "answer": NO_ANSWER_REPLY, "citations": []}


def test_emergency_skips_search_and_llm(make_client, target):
    embedder = FakeEmbedder()
    answerer = FakeAnswerer(Draft(answered=True, answer="x", cited_ids=[target[0]]))

    body = make_client(embedder, answerer).post(
        "/ask", json={"question": "Ayah saya sakit dada"}
    ).json()

    assert body == {"status": "emergency", "answer": EMERGENCY_REPLY, "citations": []}
    assert embedder.calls == 0
    assert answerer.shown == []


def test_no_answer_without_calling_llm_when_nothing_embedded(make_client, conn):
    conn.execute("UPDATE faq_chunks SET embedding = NULL")
    answerer = FakeAnswerer(Draft(answered=True, answer="x", cited_ids=[1]))

    body = make_client(FakeEmbedder(), answerer).post("/ask", json={"question": "hours?"}).json()

    assert body["status"] == "no_answer"
    assert answerer.shown == []


@pytest.mark.parametrize("question", ["", "   "])
def test_rejects_empty_question(make_client, question):
    answerer = FakeAnswerer(Draft(answered=False, answer="", cited_ids=[]))

    r = make_client(FakeEmbedder(), answerer).post("/ask", json={"question": question})

    assert r.status_code == 422
