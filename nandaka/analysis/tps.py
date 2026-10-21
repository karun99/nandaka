"""Topic Priority Score + occurrence probability (FR-HA-02..06).

TPS (0-100) = 40% frequency + 25% recency + 25% syllabus relevance
            + 10% consistency, every component normalised to 0..100.

Scoring rules, windows and denominators are returned alongside the score so
the batch report can document them (FR-HA-03). Insufficient evidence never
produces a fabricated probability (FR-HA-06).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

WEIGHTS = {"frequency": 0.40, "recency": 0.25, "syllabus": 0.25, "consistency": 0.10}
RECENCY_WINDOW_YEARS = 10     # documented time window (FR-HA-03)
MIN_ITEMS_FOR_TPS = 3         # below this TPS is provisional (FR-HA-04)
MIN_PAPERS_FOR_PROBABILITY = 5
MIN_HITS_FOR_PROBABILITY = 2

RULES_DOC = {
    "weights": dict(WEIGHTS),
    "recency_window_years": RECENCY_WINDOW_YEARS,
    "frequency_denominator": "max topic count within this batch",
    "recency_formula": "100 * (1 - age/window), clamped to 0..100",
    "consistency_formula": "years_covered / span * 100",
    "provisional_below_items": MIN_ITEMS_FOR_TPS,
    "probability_min_papers": MIN_PAPERS_FOR_PROBABILITY,
    "probability_min_hits": MIN_HITS_FOR_PROBABILITY,
    "probability_method": "Wilson score interval over per-paper appearances",
}


@dataclass
class TPSResult:
    topic: str
    score: float
    components: dict[str, float]
    provisional: bool
    reasons: list[str]
    occurrence_probability: dict[str, Any]
    rules: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "tps": round(self.score, 1),
            "components": {k: round(v, 1) for k, v in self.components.items()},
            "provisional": self.provisional,
            "reasons": self.reasons,
            "occurrence_probability": self.occurrence_probability,
            "rules": self.rules,
        }


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def _wilson(hits: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return 0.0, 0.0
    p = min(1.0, max(0.0, hits / n))
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    var = max(0.0, p * (1 - p) / n + z * z / (4 * n * n))  # clamp fp noise
    half = (z / denom) * math.sqrt(var)
    return _clamp((centre - half) * 100, 0, 100), _clamp((centre + half) * 100, 0, 100)


def compute_tps(
    stats,                          # TopicStats from analysis.frequency
    *,
    all_stats: dict | None = None,  # for frequency normalisation
    syllabus_relevance: float | None = None,   # 0..1 from syllabus mapping
    current_year: int | None = None,
    years_seen: list[int] | None = None,       # global exam years (denominator)
    n_papers_total: int | None = None,         # papers in the whole batch
) -> TPSResult:
    """FR-HA-02: weighted TPS with documented rules and provisional flags."""
    from .frequency import TopicStats
    assert isinstance(stats, TopicStats)
    reasons: list[str] = []
    cur = current_year or _current_year()

    # --- frequency (40%): share against the busiest topic --------------- #
    max_count = max((s.count for s in (all_stats or {}).values()), default=stats.count)
    freq = _clamp(100.0 * stats.count / max_count) if max_count else 0.0

    # --- recency (25%): linear decay over the documented window ---------- #
    if stats.most_recent:
        age = max(0, cur - stats.most_recent)
        recency = _clamp(100.0 * (1 - age / RECENCY_WINDOW_YEARS))
    else:
        recency = 0.0
        reasons.append("no recent appearance recorded")

    # --- syllabus relevance (25%) --------------------------------------- #
    if syllabus_relevance is None:
        syll = 50.0
        reasons.append("syllabus mapping absent: relevance defaulted to 50 (provisional)")
    else:
        syll = _clamp(float(syllabus_relevance) * 100)

    # --- consistency (10%): share of exam years with an appearance ------- #
    span = stats.span or ((max(years_seen) - min(years_seen) + 1) if years_seen else 0)
    if span:
        consistency = _clamp(100.0 * stats.years_covered / span)
    else:
        consistency = 0.0
        reasons.append("no year span available: consistency = 0")

    components = {"frequency": freq, "recency": recency,
                  "syllabus": syll, "consistency": consistency}
    score = sum(components[k] * WEIGHTS[k] for k in WEIGHTS)

    provisional = stats.count < MIN_ITEMS_FOR_TPS
    if provisional:
        reasons.append(
            f"only {stats.count} item(s) < {MIN_ITEMS_FOR_TPS}: TPS provisional (FR-HA-04)"
        )
    if syllabus_relevance is None:
        provisional = True

    # Probability basis: share of papers (or exam years) that contained the
    # topic AT LEAST ONCE — not the raw question count, which can exceed the
    # number of papers and would corrupt p > 1 (FR-HA-05 honesty).
    if stats.papers:
        appearances = len(stats.papers)
        n_papers = n_papers_total or appearances
    else:
        appearances = len(set(stats.years))
        n_papers = n_papers_total or len(set(years_seen or [])) or appearances
    prob = occurrence_probability(
        appearances=min(appearances, max(n_papers, 0)),
        n_papers=n_papers,
        years=sorted(set(stats.years)),
        years_seen=years_seen or sorted(set(stats.years)),
        question_count=stats.count,
    )
    return TPSResult(topic=stats.topic, score=score, components=components,
                     provisional=provisional, reasons=reasons,
                     occurrence_probability=prob, rules=dict(RULES_DOC))


def occurrence_probability(*, appearances: int, n_papers: int,
                           years: list[int], years_seen: list[int],
                           question_count: int | None = None) -> dict[str, Any]:
    """FR-HA-05/06: only estimate when the evidence supports it."""
    basis = {
        "method": RULES_DOC["probability_method"],
        "sample_size_papers": n_papers,
        "appearances": appearances,
        "questions": question_count if question_count is not None else appearances,
        "period": f"{min(years_seen)}-{max(years_seen)}" if years_seen else "unknown",
        "confidence": "n/a",
        "limitations": [],
    }
    if (n_papers < MIN_PAPERS_FOR_PROBABILITY
            or appearances < MIN_HITS_FOR_PROBABILITY):
        basis["value"] = None
        basis["label"] = "Not reliably estimable"
        basis["confidence"] = "insufficient evidence"
        basis["limitations"].append(
            f"needs >= {MIN_PAPERS_FOR_PROBABILITY} papers and "
            f">= {MIN_HITS_FOR_PROBABILITY} appearances (FR-HA-06); "
            "use the provisional TPS instead"
        )
        return basis

    lo, hi = _wilson(appearances, n_papers)
    p = 100.0 * min(appearances, n_papers) / n_papers if n_papers else 0.0
    width = hi - lo
    confidence = "high" if width <= 25 else ("medium" if width <= 45 else "low")
    basis.update(value=round(p, 1), lo=round(lo, 1), hi=round(hi, 1),
                 label=f"{p:.1f}% (95% CI {lo:.1f}-{hi:.1f})",
                 confidence=confidence,
                 limitations=["historical frequency only; not a guarantee of "
                              "appearance in any future paper"])
    return basis


def _current_year() -> int:
    from datetime import date
    return date.today().year
