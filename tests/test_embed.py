import pytest

from app.db import get_conn
from app.embed import DIMENSIONS, embed_all, embed_pending
from app.load_clinic import DEFAULT_PATH, load_clinic, read_clinic_yaml

# These tests never call Gemini: FakeEmbedder stands in for it.


class FakeEmbedder:
    """Records the texts it was given and returns fixed vectors."""

    def __init__(self, dimensions: int = DIMENSIONS):
        self.dimensions = dimensions
        self.calls: list[list[str]] = []

    def __call__(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[0.1] * self.dimensions for _ in texts]


def missing_counts(conn) -> tuple[int, int]:
    faq = conn.execute("SELECT count(*) FROM faq_chunks WHERE embedding IS NULL")
    services = conn.execute("SELECT count(*) FROM services WHERE embedding IS NULL")
    return faq.fetchone()[0], services.fetchone()[0]


def test_embeds_missing_rows_then_nothing_on_rerun():
    with get_conn() as conn:
        load_clinic(conn, read_clinic_yaml(DEFAULT_PATH))
        # Start from "nothing embedded", whatever the dev DB already holds.
        conn.execute("UPDATE faq_chunks SET embedding = NULL")
        conn.execute("UPDATE services SET embedding = NULL")
        faq_missing, services_missing = missing_counts(conn)

        fake = FakeEmbedder()
        counts = embed_pending(conn, fake)
        assert counts == {"faq_chunks": faq_missing, "services": services_missing}
        assert missing_counts(conn) == (0, 0)

        texts = [t for call in fake.calls for t in call]
        assert any(t.startswith("title: none | text: Q: What are your opening hours?") for t in texts)
        assert any(t.startswith("title: General consultation | text: A visit with a GP") for t in texts)

        rerun = FakeEmbedder()
        assert embed_pending(conn, rerun) == {"faq_chunks": 0, "services": 0}
        assert rerun.calls == []  # nothing missing, so no API calls

        conn.rollback()


def test_embed_all_sends_batches(monkeypatch):
    monkeypatch.setattr("app.embed.BATCH_SIZE", 10)
    fake = FakeEmbedder()

    vectors = embed_all(fake, [f"text {i}" for i in range(25)])

    assert [len(call) for call in fake.calls] == [10, 10, 5]
    assert len(vectors) == 25


def test_embed_all_rejects_wrong_dimensions():
    with pytest.raises(RuntimeError, match=str(DIMENSIONS)):
        embed_all(FakeEmbedder(dimensions=3), ["hello"])
