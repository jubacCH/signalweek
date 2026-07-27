"""Rule-based classifier that maps every cluster to one of five categories.

The curated digest has a fixed five-category taxonomy — Models, Lawsuits &
policy, Funding, Research, Industry moves — and every cluster must land in
exactly one bucket. There is deliberately no "Uncategorized" fallback: an
industry-moves bucket is the broad catch-all when nothing more specific fits.

Classification uses two deterministic signals:

1. **Keyword lexicons** per category, matched against the cluster headline
   case-insensitively with word-boundary regexes.
2. The originating source's ``category_hint``, used as a tiebreak when the
   keyword score is inconclusive.

If keyword matches are absent, the source hint wins; if that is also missing
the classifier falls back to ``industry_moves``. That guarantees the pass is
total by construction — every cluster ends up in exactly one of the five
buckets.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from signalweek.sources import clusters_table, raw_items_table, sources_table

CATEGORIES: tuple[str, ...] = (
    "models",
    "lawsuits_policy",
    "funding",
    "research",
    "industry_moves",
)

CATEGORY_LABELS: dict[str, str] = {
    "models": "Models",
    "lawsuits_policy": "Lawsuits & policy",
    "funding": "Funding",
    "research": "Research",
    "industry_moves": "Industry moves",
}

# The catch-all bucket used when neither keywords nor a hint apply.
FALLBACK_CATEGORY = "industry_moves"

# Tie-break priority applied when several categories share the top keyword
# score AND the source hint is not among them. The broadest bucket,
# ``industry_moves``, ranks last so it only wins when nothing more specific
# matches.
_TIE_BREAK_PRIORITY: tuple[str, ...] = (
    "lawsuits_policy",
    "funding",
    "models",
    "research",
    "industry_moves",
)

# Keyword lexicons per category. Terms are matched as whole words,
# case-insensitively (see ``_compile_lexicons``). They are intentionally
# skewed toward high-precision AI-industry phrasing rather than exhaustive
# coverage — false positives hurt the digest more than missed classifications
# do, because the source hint provides a safety net.
_LEXICONS: dict[str, tuple[str, ...]] = {
    "models": (
        "gpt",
        "claude",
        "gemini",
        "llama",
        "mistral",
        "grok",
        "phi",
        "qwen",
        "deepseek",
        "open weights",
        "open-weights",
        "weights",
        "checkpoint",
        "foundation model",
        "llm",
        "large language model",
        "small language model",
        "multimodal",
        "fine-tune",
        "fine-tuned",
        "fine-tuning",
        "instruction-tuned",
        "context window",
        "reasoning model",
        "model card",
        "unveil",
        "unveils",
        "unveiled",
        "ships",
        "shipped",
        "shipping",
        "release",
        "released",
        "releases",
        "launch",
        "launches",
        "launched",
    ),
    "lawsuits_policy": (
        "lawsuit",
        "lawsuits",
        "sue",
        "sues",
        "sued",
        "suing",
        "court",
        "ruling",
        "rules",
        "judge",
        "plaintiff",
        "defendant",
        "class action",
        "copyright",
        "infringement",
        "trademark",
        "regulation",
        "regulator",
        "regulators",
        "regulatory",
        "eu ai act",
        "ai act",
        "gdpr",
        "privacy",
        "antitrust",
        "executive order",
        "white house",
        "ftc",
        "doj",
        "sec",
        "european commission",
        "european union",
        "brussels",
        "policy",
        "legislation",
        "bill",
        "law",
        "senate",
        "congress",
        "ban",
        "banned",
        "banning",
        "subpoena",
        "settlement",
        "complaint",
        "fined",
        "fine",
        "penalty",
        "penalties",
    ),
    "funding": (
        "raise",
        "raises",
        "raised",
        "raising",
        "funding",
        "funding round",
        "round",
        "series a",
        "series b",
        "series c",
        "series d",
        "series e",
        "series f",
        "series g",
        "seed round",
        "seed funding",
        "pre-seed",
        "valuation",
        "valued at",
        "ipo",
        "acquires",
        "acquired",
        "acquisition",
        "acquiring",
        "buyout",
        "merger",
        "investment",
        "investor",
        "investors",
        "venture",
        "vc",
        "term sheet",
    ),
    "research": (
        "paper",
        "papers",
        "arxiv",
        "preprint",
        "study",
        "researchers",
        "research",
        "we propose",
        "we present",
        "we introduce",
        "findings",
        "empirical",
        "benchmark",
        "benchmarks",
        "state-of-the-art",
        "sota",
        "ablation",
        "dataset",
        "evaluation",
        "novel method",
        "algorithm",
        "theoretical",
    ),
    "industry_moves": (
        "hire",
        "hires",
        "hired",
        "hiring",
        "appoints",
        "appointed",
        "promotes",
        "promoted",
        "ceo",
        "cto",
        "cfo",
        "coo",
        "chief",
        "joins",
        "joined",
        "resigns",
        "resigned",
        "departs",
        "departed",
        "layoff",
        "layoffs",
        "restructure",
        "restructuring",
        "partnership",
        "partners with",
    ),
}


def _compile_lexicons() -> dict[str, tuple[re.Pattern[str], ...]]:
    compiled: dict[str, tuple[re.Pattern[str], ...]] = {}
    for category, terms in _LEXICONS.items():
        compiled[category] = tuple(
            re.compile(
                rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])",
                re.IGNORECASE,
            )
            for term in terms
        )
    return compiled


_COMPILED_LEXICONS: dict[str, tuple[re.Pattern[str], ...]] = _compile_lexicons()


def classify_text(text: str, *, category_hint: str | None = None) -> str:
    """Return the best-fit category for ``text``.

    Word-level matches from each category's lexicon are counted. If a single
    category has the top score it wins. On a tie, ``category_hint`` wins when
    it is among the top scorers; otherwise the fixed ``_TIE_BREAK_PRIORITY``
    decides. When no lexicon matches at all, ``category_hint`` is used (when
    valid) or :data:`FALLBACK_CATEGORY`.

    The return value is always one of :data:`CATEGORIES`.
    """
    scores = _score(text or "")
    top_score = max(scores.values())

    if top_score == 0:
        if category_hint in CATEGORIES:
            return category_hint
        return FALLBACK_CATEGORY

    top = [cat for cat in CATEGORIES if scores[cat] == top_score]
    if len(top) == 1:
        return top[0]

    if category_hint in top:
        return category_hint

    for cat in _TIE_BREAK_PRIORITY:
        if cat in top:
            return cat
    return top[0]  # unreachable — every category is in _TIE_BREAK_PRIORITY


def _score(text: str) -> dict[str, int]:
    scores = dict.fromkeys(CATEGORIES, 0)
    for category, patterns in _COMPILED_LEXICONS.items():
        for pattern in patterns:
            if pattern.search(text):
                scores[category] += 1
    return scores


@dataclass
class ClassifyRunResult:
    """Outcome of one :func:`classify_clusters` run.

    ``categories`` maps each cluster id visited to the chosen category (even
    when unchanged). ``updated`` counts cluster rows whose ``category`` value
    was rewritten; ``unchanged`` counts rows that already had the right
    category. ``total`` is their sum, i.e. the number of clusters seen.
    """

    updated: int = 0
    unchanged: int = 0
    categories: dict[int, str] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return self.updated + self.unchanged


def classify_clusters(bind: Session | Connection) -> ClassifyRunResult:
    """Reclassify every cluster and write the result to the DB.

    For each ``clusters`` row the classifier reads:

    * ``canonical_headline`` — the headline text scored against lexicons.
    * The anchor raw_item's source ``category_hint`` — used as a tiebreak
      when keywords are inconclusive. The anchor is looked up by matching
      ``raw_items.url`` against ``clusters.primary_url``. If no matching
      raw_item is found (e.g. the anchor was pruned), the cluster's current
      ``category`` is used as the hint instead so idempotent re-runs keep
      the previously-chosen bucket.

    Rows whose classification is unchanged are left alone.
    """
    connection = _as_connection(bind)

    stmt = (
        select(
            clusters_table.c.id,
            clusters_table.c.canonical_headline,
            clusters_table.c.category,
            sources_table.c.category_hint,
        )
        .select_from(
            clusters_table.outerjoin(
                raw_items_table,
                raw_items_table.c.url == clusters_table.c.primary_url,
            ).outerjoin(
                sources_table,
                raw_items_table.c.source_id == sources_table.c.id,
            )
        )
        .order_by(clusters_table.c.id)
    )

    result = ClassifyRunResult()
    seen: set[int] = set()
    for row in connection.execute(stmt).all():
        cluster_id = int(row.id)
        if cluster_id in seen:
            continue
        seen.add(cluster_id)

        hint = row.category_hint if row.category_hint in CATEGORIES else row.category
        chosen = classify_text(row.canonical_headline, category_hint=hint)
        result.categories[cluster_id] = chosen

        if chosen == row.category:
            result.unchanged += 1
            continue

        connection.execute(
            clusters_table.update().where(clusters_table.c.id == cluster_id).values(category=chosen)
        )
        result.updated += 1

    return result


def _as_connection(bind: Session | Connection) -> Connection:
    if isinstance(bind, Session):
        return bind.connection()
    return bind
