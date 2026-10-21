"""Negative-marking mathematics (FR-NM-01..04).

E(attempt) = p*R - (1-p)*W ;  E(skip) = U
Recommend "attempt" only when E(attempt) > E(skip).
Break-even: p* = (U + W) / (R + W); with U = 0 this is W / (R + W).

FR-NM-04: historical topic frequency is NEVER used as candidate correctness
probability. Without candidate data, p is reported as unknown and no attempt
advice is given.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

P_UNKNOWN = "unknown"


@dataclass
class MarkingScheme:
    R: float = 1.0    # reward per correct answer
    W: float = 0.0    # penalty per wrong answer
    U: float = 0.0    # credit for unattempted (usually 0)
    source: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MarkingScheme":
        def num(*keys: str, default: float = 0.0) -> float:
            for k in keys:
                if k in d and d[k] not in (None, ""):
                    return float(d[k])
            return default
        return cls(R=num("R", "correct", "right", "positive", default=1.0),
                   W=num("W", "wrong", "incorrect", "negative", "penalty"),
                   U=num("U", "unattempted", "skipped", "unanswered"),
                   source=str(d.get("source", "")))

    @property
    def is_read(self) -> bool:
        """FR-NM-01: rules must actually be read before advising."""
        return bool(self.source) or self.W > 0


@dataclass
class AttemptAdvice:
    scheme: MarkingScheme
    p: float | None
    E_attempt: float | None
    E_skip: float
    break_even_p: float
    recommendation: str
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "R": self.scheme.R, "W": self.scheme.W, "U": self.scheme.U,
            "p": self.p if self.p is not None else P_UNKNOWN,
            "E_attempt": round(self.E_attempt, 4) if self.E_attempt is not None else None,
            "E_skip": round(self.E_skip, 4),
            "break_even_p_pct": round(self.break_even_p * 100, 1),
            "recommendation": self.recommendation,
            "notes": self.notes,
        }


def expected_attempt(R: float, W: float, p: float) -> float:
    return p * R - (1.0 - p) * W


def break_even_p(R: float, W: float, U: float = 0.0) -> float:
    """Minimum correctness probability that makes attempting worthwhile."""
    if R + W == 0:
        return float("inf")
    return (U + W) / (R + W)


def advise(scheme: MarkingScheme, p: float | None) -> AttemptAdvice:
    """FR-NM-02/03/04: compute expectations and recommend (or decline)."""
    notes: list[str] = []
    p_star = break_even_p(scheme.R, scheme.W, scheme.U)
    E_skip = scheme.U

    if p is None:
        notes.append(
            "p = unknown: candidate correctness probability is not measured here; "
            "historical topic frequency must not be substituted (FR-NM-04)"
        )
        return AttemptAdvice(scheme, None, None, E_skip, p_star,
                             "INSUFFICIENT DATA — no attempt advice", notes)

    p = max(0.0, min(1.0, float(p)))
    E_att = expected_attempt(scheme.R, scheme.W, p)
    if E_att > E_skip:
        rec = "ATTEMPT"
        notes.append(f"E(attempt) {E_att:.4f} > E(skip) {E_skip:.4f}")
    elif E_att < E_skip:
        rec = "SKIP"
        notes.append(f"E(attempt) {E_att:.4f} < E(skip) {E_skip:.4f}")
    else:
        rec = "NEUTRAL — expected values are equal"
        notes.append(f"E(attempt) == E(skip) == {E_skip:.4f}")
    if scheme.U == 0 and scheme.W > 0:
        notes.append(f"break-even accuracy: > {p_star * 100:.1f}% "
                     f"(W/(R+W) = {scheme.W}/{scheme.R + scheme.W})")
    return AttemptAdvice(scheme, p, E_att, E_skip, p_star, rec, notes)


def parse_marking_text(text: str) -> MarkingScheme | None:
    """Best-effort read of marking rules from syllabus/exam metadata (FR-NM-01).

    Understands the common shapes: "2 marks for correct, 0.5 negative for
    wrong", "+2 / -0.5", "correct: 1, wrong: 0.25", "unattempted 0".
    """
    import re
    low = (text or "").lower()
    if not low.strip():
        return None

    def first(*patterns: str) -> float | None:
        for pat in patterns:
            m = re.search(pat, low)
            if m:
                for g in m.groups():
                    if g is not None:
                        return float(g)
        return None

    r = first(
        r"(?:\+)?(\d+(?:\.\d+)?)\s*(?:marks?\s*)?(?:for|per|on)\s*(?:each\s*)?"
        r"(?:correct|right)\b",
        r"(?:correct|right)\s*(?:answer|option)?\s*(?:is|=|:|-)?\s*(\d+(?:\.\d+)?)",
    )
    w = first(
        r"-\s*(\d+(?:\.\d+)?)\b",
        r"(\d+(?:\.\d+)?)\s*(?:marks?\s*)?(?:negative|penalty)\s*(?:marking\s*)?"
        r"(?:of|is|=|:)?\s*(?:by\s+)?",
        r"(\d+(?:\.\d+)?)\s*(?:marks?\s*)?(?:for|per|on)\s*(?:each\s*)?"
        r"(?:wrong|incorrect)\b",
        r"(?:wrong|incorrect)\s*(?:answer|option)?\s*(?:is|=|:|-)?\s*(\d+(?:\.\d+)?)",
    )
    u = first(
        r"(?:\+)?(\d+(?:\.\d+)?)\s*(?:marks?\s*)?(?:for|per|on)\s*"
        r"(?:each\s*)?unattempt(?:ed)?\b",
        r"unattempt(?:ed)?\s*(?:is|=|:|-)?\s*(\d+(?:\.\d+)?)",
        r"(?:skip(?:ped)?|unanswered)\s*(?:is|=|:|-)?\s*(\d+(?:\.\d+)?)",
    )
    if r is None and w is None and u is None:
        return None
    return MarkingScheme(R=r if r is not None else 1.0,
                         W=w if w is not None else 0.0,
                         U=u if u is not None else 0.0,
                         source=text.strip()[:200])
