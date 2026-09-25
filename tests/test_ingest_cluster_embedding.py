"""Spec criterion 10 against the real local model (all-MiniLM-L6-v2).

Two items with different URLs become one cluster when their headlines have
cosine similarity >= 0.85, and a near-miss pair below 0.85 stays apart. These
tests need the model files (``python -m signalweek.ingest.embed download
<dir>`` plus ``SIGNALWEEK_EMBED_MODEL_DIR``) and skip otherwise. CI downloads
the model; the Docker image bakes it in.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine

from signalweek.ingest import embed
from signalweek.ingest.cluster import SIMILARITY_THRESHOLD, cluster_raw_items
from signalweek.sources import clusters_table, raw_items_table, sources_table

pytestmark = pytest.mark.real_embedder

BASE_TIME = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)

DUPLICATE_PAIR = (
    "OpenAI releases GPT-5 to all ChatGPT users",
    "GPT-5 is now available to every ChatGPT user, OpenAI says",
)
NEAR_MISS_PAIR = (
    "Nvidia unveils Blackwell Ultra GPUs at GTC",
    "Nvidia announces Blackwell Ultra chips at its GTC conference",
)


def _cosine(a: str, b: str) -> float:
    vectors = embed.get_default_embedder().embed([a, b])
    return float(vectors[0] @ vectors[1])


def _seed_pair(engine: Engine, titles: tuple[str, str]) -> None:
    with engine.begin() as conn:
        ids = [
            conn.execute(
                sources_table.insert()
                .values(url=f"https://outlet{n}.example/feed", kind="rss", active=True)
                .returning(sources_table.c.id)
            ).scalar_one()
            for n in range(2)
        ]
        for n, (source_id, title) in enumerate(zip(ids, titles, strict=True)):
            conn.execute(
                raw_items_table.insert().values(
                    source_id=source_id,
                    url=f"https://outlet{n}.example/story-{n}",
                    canonical_url=f"https://outlet{n}.example/story-{n}",
                    title=title,
                    fetched_at=BASE_TIME + timedelta(hours=n),
                    first_seen_at=BASE_TIME + timedelta(hours=n),
                )
            )


def test_threshold_is_the_spec_value() -> None:
    assert SIMILARITY_THRESHOLD == 0.85


def test_model_output_matches_sentence_transformers_reference() -> None:
    """Unit-length 384-d vectors, and a known pair scores what the reference
    ``sentence-transformers`` implementation gives (0.911)."""
    vectors = embed.get_default_embedder().embed(list(DUPLICATE_PAIR))
    assert vectors.shape == (2, 384)
    assert abs(float((vectors[0] ** 2).sum()) - 1.0) < 1e-5
    assert _cosine(*DUPLICATE_PAIR) == pytest.approx(0.911, abs=0.005)


def test_paraphrased_duplicate_pair_becomes_one_cluster(curated_engine: Engine) -> None:
    assert _cosine(*DUPLICATE_PAIR) >= SIMILARITY_THRESHOLD
    _seed_pair(curated_engine, DUPLICATE_PAIR)

    with curated_engine.begin() as conn:
        result = cluster_raw_items(conn)
        clusters = conn.execute(select(clusters_table)).all()

    assert result.semantic_matches == 1
    assert len(clusters) == 1
    assert clusters[0].canonical_headline == DUPLICATE_PAIR[0]


def test_near_miss_pair_below_threshold_stays_two_clusters(curated_engine: Engine) -> None:
    assert _cosine(*NEAR_MISS_PAIR) < SIMILARITY_THRESHOLD
    _seed_pair(curated_engine, NEAR_MISS_PAIR)

    with curated_engine.begin() as conn:
        result = cluster_raw_items(conn)
        clusters = conn.execute(select(clusters_table)).all()

    assert result.semantic_matches == 0
    assert len(clusters) == 2
