"""Historical frequency analysis (FR-HA-01).

Input: a list of question records from past papers, e.g.

    {"year": 2023, "paper": "APPSC-GS-2023", "topic": "Constitution",
     "subtopic": "Preamble", "kind": "direct",   # direct|reworded|concept|diagram
     "text": "Which article...?"}

Everything here is deterministic and side-effect free so it can be unit
tested without a model (SRS §4 verification).
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = re.sub(r"[^0-9a-z\u0c80-\u0cff ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def is_near_duplicate(a: str, b: str, threshold: float = 0.86) -> bool:
    """FR-GOV-05: near-duplicate prevention (token Jaccard, cheap + stable)."""
    ta, tb = set(normalise(a).split()), set(normalise(b).split())
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) >= threshold


@dataclass
class TopicStats:
    topic: str
    count: int = 0
    years: list[int] = field(default_factory=list)
    kinds: dict[str, int] = field(default_factory=dict)   # direct/reworded/concept/diagram
    papers: set[str] = field(default_factory=set)
    subtopics: Counter = field(default_factory=Counter)

    @property
    def years_covered(self) -> int:
        return len(set(self.years))

    @property
    def most_recent(self) -> int:
        return max(self.years) if self.years else 0

    @property
    def span(self) -> int:
        return (max(self.years) - min(self.years) + 1) if self.years else 0


def compute_stats(records: Iterable[dict[str, Any]],
                  topic_key: str = "topic") -> dict[str, TopicStats]:
    """FR-HA-01: per-topic counts, years, recency, repetition kinds, diagrams."""
    out: dict[str, TopicStats] = {}
    for rec in records:
        topic = str(rec.get(topic_key) or "UNSORTED")
        st = out.setdefault(topic, TopicStats(topic=topic))
        st.count += 1
        year = int(rec.get("year") or 0)
        if year:
            st.years.append(year)
        kind = str(rec.get("kind") or "direct").lower()
        st.kinds[kind] = st.kinds.get(kind, 0) + 1
        if rec.get("paper"):
            st.papers.add(str(rec["paper"]))
        if rec.get("subtopic"):
            st.subtopics[str(rec["subtopic"])] += 1
    return out


def paper_set(records: Iterable[dict[str, Any]]) -> set[str]:
    return {str(r.get("paper")) for r in records if r.get("paper")}


def question_years(records: Iterable[dict[str, Any]]) -> list[int]:
    years = [int(r.get("year") or 0) for r in records]
    return sorted(y for y in years if y)


def diagram_frequency(records: Iterable[dict[str, Any]]) -> dict[str, float]:
    """Share of diagram-based questions per topic (IR/FR-HA-01)."""
    total: Counter = Counter()
    diag: Counter = Counter()
    for r in records:
        t = str(r.get("topic") or "UNSORTED")
        total[t] += 1
        if str(r.get("kind", "")).lower() == "diagram":
            diag[t] += 1
    return {t: round(diag[t] / total[t], 4) for t in total if total[t]}
