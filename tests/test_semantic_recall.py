"""Поиск по смыслу: гибридный recall, индексация и backfill (тикет #82)."""
import pytest

from app.core import embeddings
from app.core.embeddings import EmbeddingsUnavailable, cosine, pack, unpack
from app.modules.memory import knowledge
from app.modules.memory.models import BoardEntryEmbedding


class FakeEmbedder:
    """Детерминированные векторы: близкие по смыслу слова — близкие векторы."""

    VECTORS = {
        "грибы": [1.0, 0.0, 0.1],
        "шампиньоны в сливках": [0.9, 0.1, 0.1],
        "куплены шампиньоны и сливки": [0.9, 0.1, 0.15],
        "показания счётчика 456": [0.0, 1.0, 0.0],
    }

    def __init__(self):
        self.cfg = type("Cfg", (), {"model": "fake-embed"})()
        self.calls = []

    @property
    def configured(self):
        return True

    def embed(self, texts):
        self.calls.append(list(texts))
        return [self.VECTORS.get(text, [0.0, 0.0, 1.0]) for text in texts]


@pytest.fixture
def board(db, member):
    section = knowledge.create_section(db, member.id, "Кухня")
    return knowledge.create_board(db, member.id, section.id, "Продукты")


def test_pack_unpack_roundtrip_and_cosine():
    vector = [0.25, -1.5, 3.0]
    assert unpack(pack(vector)) == pytest.approx(vector)
    assert cosine([1, 0], [1, 0]) == pytest.approx(1.0)
    assert cosine([1, 0], [0, 1]) == pytest.approx(0.0)
    assert cosine([], [1.0]) == 0.0


def test_without_an_embedder_recall_stays_plain_ilike(db, member, board):
    knowledge.add_entry(db, member.id, board.id, "куплены шампиньоны и сливки")

    found = knowledge.search_entries(db, member.id, "грибы")

    assert found == []          # подстроки нет, эмбеддер не настроен — пусто
    assert db.query(BoardEntryEmbedding).count() == 0


def test_semantic_recall_finds_by_meaning(db, member, board, monkeypatch):
    fake = FakeEmbedder()
    monkeypatch.setattr(embeddings, "client", fake)
    knowledge.add_entry(db, member.id, board.id, "куплены шампиньоны и сливки")
    knowledge.add_entry(db, member.id, board.id, "показания счётчика 456")

    found = knowledge.search_entries(db, member.id, "грибы")

    texts = [entry.text for entry, _board in found]
    assert "куплены шампиньоны и сливки" in texts
    assert "показания счётчика 456" not in texts, "непохожее отсекается порогом"


def test_exact_matches_come_before_semantic_ones(db, member, board, monkeypatch):
    fake = FakeEmbedder()
    monkeypatch.setattr(embeddings, "client", fake)
    knowledge.add_entry(db, member.id, board.id, "куплены шампиньоны и сливки")
    knowledge.add_entry(db, member.id, board.id, "грибы не ем")

    found = knowledge.search_entries(db, member.id, "грибы")

    assert found[0][0].text == "грибы не ем"


def test_backfill_indexes_old_entries(db, member, board, monkeypatch):
    knowledge.add_entry(db, member.id, board.id, "куплены шампиньоны и сливки")
    assert db.query(BoardEntryEmbedding).count() == 0

    fake = FakeEmbedder()
    monkeypatch.setattr(embeddings, "client", fake)

    assert knowledge.backfill_embeddings(db) == 1
    assert db.query(BoardEntryEmbedding).count() == 1
    assert knowledge.backfill_embeddings(db) == 0   # второй прогон — нечего


def test_a_broken_embedder_never_breaks_recall(db, member, board, monkeypatch):
    class Broken(FakeEmbedder):
        def embed(self, texts):
            raise EmbeddingsUnavailable("нет сети")

    monkeypatch.setattr(embeddings, "client", Broken())
    knowledge.add_entry(db, member.id, board.id, "грибы не ем")

    found = knowledge.search_entries(db, member.id, "грибы")

    assert [entry.text for entry, _b in found] == ["грибы не ем"]
