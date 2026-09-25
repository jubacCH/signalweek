"""Shared pytest fixtures for the Signalweek test suite."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator

import numpy as np
import pytest
from sqlalchemy.engine import Engine

from signalweek.db.session import create_db_engine
from signalweek.ingest import embed
from signalweek.sources import sources_metadata


@pytest.fixture()
def curated_engine() -> Iterator[Engine]:
    """A fresh in-memory SQLite engine with the curated-digest schema created.

    Mirrors what ``alembic upgrade head`` produces: the five tables in
    :data:`signalweek.sources.sources_metadata` and their indexes.
    """
    engine = create_db_engine("sqlite:///:memory:")
    sources_metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


class BagOfWordsEmbedder:
    """Deterministic stand-in for the ONNX model: a hashed bag of lowercase
    words. Identical headlines score 1.0, headlines with no shared words score
    0.0, and adding one word to a five-word headline scores about 0.91."""

    def embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), embed.MODEL_DIM), dtype=np.float32)
        for row, text in enumerate(texts):
            for word in re.findall(r"[a-z0-9]+", text.lower()):
                digest = hashlib.sha1(word.encode()).digest()
                out[row, int.from_bytes(digest[:4], "big") % embed.MODEL_DIM] += 1.0
        return embed.normalize(out)


@pytest.fixture(autouse=True)
def _fake_embedder(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the suite independent of the 90 MB model file. Tests marked
    ``real_embedder`` use the real model and skip when it is not installed."""
    if request.node.get_closest_marker("real_embedder"):
        if not embed.model_available():
            pytest.skip(f"embedding model not installed ({embed.MODEL_DIR_ENV})")
        return
    monkeypatch.setattr(embed, "get_default_embedder", BagOfWordsEmbedder)
