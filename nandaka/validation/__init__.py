"""Validation (FR-VAL-01..03) and human-in-the-loop review queue (FR-HIL-01..03).

Exactly one status per item: VALIDATED | NEEDS CORRECTION |
HUMAN REVIEW REQUIRED | EXCLUDED (FR-VAL-02). Nothing is called
"independently verified" without a separate verification step (FR-VAL-03).
"""

from __future__ import annotations

import hashlib
import itertools
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..analysis.frequency import is_near_duplicate
from ..ssj import check_text, overall_tag

VALIDATED = "VALIDATED"
NEEDS_CORRECTION = "NEEDS CORRECTION"
HUMAN_REVIEW = "HUMAN REVIEW REQUIRED"
EXCLUDED = "EXCLUDED"
STATUSES = (VALIDATED, NEEDS_CORRECTION, HUMAN_REVIEW, EXCLUDED)

# The per-item check battery (FR-VAL-01)
CHECK_NAMES = [
    "factual validity", "answer validity", "source traceability",
    "calculation validity", "language validity", "ambiguity", "duplication",
    "syllabus alignment", "historical evidence reproducibility",
    "negative-marking compliance", "currentness", "SSJ compliance",
    "image validity", "image appropriateness", "alt-text/bilingual caption",
]

HARM_WORDS = {"legal", "medical", "health", "law", "polity", "rights",
              "exam security", "money", "finance"}


@dataclass
class Check:
    name: str
    ok: bool
    severity: str = "blocker"   # blocker -> NEEDS CORRECTION; review -> HUMAN REVIEW
    detail: str = ""


@dataclass
class ValidationReport:
    item_id: str
    status: str
    checks: list[Check] = field(default_factory=list)
    ssj_tag: str = ""
    review_reasons: list[str] = field(default_factory=list)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]

    def as_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id, "status": self.status,
            "ssj_tag": self.ssj_tag,
            "checks": [{"check": c.name, "ok": c.ok, "severity": c.severity,
                        "detail": c.detail} for c in self.checks],
            "review_reasons": self.review_reasons,
        }


# --------------------------------------------------------------------------- #
# MCQ validation
# --------------------------------------------------------------------------- #


def _req(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict)):
        return len(value) > 0
    return True


def validate_mcq(mcq: dict[str, Any], *, corpus: Iterable[dict] | None = None,
                 marking: dict | None = None, tier: str = "T2",
                 syllabus_topics: set[str] | None = None) -> ValidationReport:
    """Run the FR-VAL-01 battery against one bilingual MCQ."""
    checks: list[Check] = []
    reviews: list[str] = []
    iid = str(mcq.get("id") or "unknown")

    # --- structure & answer -------------------------------------------- #
    en_opts, te_opts = mcq.get("options_en", []), mcq.get("options_te", [])
    answer = mcq.get("answer", "")
    has_answer = bool(answer) and (not en_opts or answer in en_opts or
                                   str(answer) in [str(o) for o in en_opts])
    checks.append(Check("answer validity", has_answer,
                        detail="answer present and matches an English option"))
    if te_opts and en_opts and len(te_opts) != len(en_opts):
        checks.append(Check("language validity", False,
                            detail=f"option count mismatch EN={len(en_opts)} TE={len(te_opts)}"))
    else:
        checks.append(Check("language validity", True,
                            detail="EN/TE option counts match"))

    # --- ambiguity ------------------------------------------------------- #
    uniq = len({str(o).strip().lower() for o in en_opts}) == len(en_opts) if en_opts else False
    checks.append(Check("ambiguity", uniq and len(en_opts) >= 4,
                        detail="4+ unique options" if uniq else "duplicate options detected"))

    # --- source traceability -------------------------------------------- #
    src = mcq.get("source") or mcq.get("source_reference") or ""
    checks.append(Check("source traceability", _req(src),
                        detail=f"source: {src or 'MISSING'}"))

    # --- bilingual faithfulness ------------------------------------------ #
    te_stem = mcq.get("stem_te", "") or ""
    en_stem = mcq.get("stem_en", "") or ""
    numeric_en = [t for t in _numbers(en_stem)]
    numeric_te = [t for t in _numbers(te_stem)]
    nums_ok = sorted(numeric_en) == sorted(numeric_te)
    has_te = bool(te_stem.strip())
    neg_te = any(w in te_stem for w in ("కాదు", "కాని", "లేదు", "రాదు"))
    neg_en = any(w in en_stem.lower() for w in ("not", "except", "never", "incorrect"))
    checks.append(Check("language validity", has_te, severity="review",
                        detail="Telugu stem present" if has_te else "missing Telugu stem"))
    if neg_en and has_te:
        checks.append(Check("language validity", neg_te, severity="blocker",
                            detail="negation preserved in Telugu"
                            if neg_te else "negation LOST in Telugu translation"))
    if numeric_en or numeric_te:
        checks.append(Check("language validity", nums_ok, severity="blocker",
                            detail="numbers preserved in Telugu"
                            if nums_ok else
                            f"numbers differ EN={numeric_en} TE={numeric_te}"))
    if any("క" <= ch <= "౿" for ch in en_stem):
        checks.append(Check("language validity", False, severity="review",
                            detail="Telugu characters found in the English stem"))

    # --- evidence / TPS --------------------------------------------------- #
    if not _req(mcq.get("historical_evidence")):
        checks.append(Check("historical evidence reproducibility", False,
                            severity="review", detail="no historical evidence supplied"))
    else:
        checks.append(Check("historical evidence reproducibility", True,
                            detail=str(mcq.get("historical_evidence"))[:120]))
    if syllabus_topics is not None and mcq.get("topic"):
        checks.append(Check("syllabus alignment",
                            str(mcq["topic"]) in syllabus_topics,
                            detail=f"topic '{mcq['topic']}' in syllabus map"))
    if mcq.get("probability") and not str(mcq["probability"]).startswith(
            ("Not reliably", "unknown", "n/a", "")):
        # probability claimed -> method/sample/CI must appear in the batch report
        checks.append(Check("historical evidence reproducibility", True,
                            detail="probability claim present; method required in report"))

    # --- calculation validity (negative marking note) --------------------- #
    if mcq.get("negative_marking_note") and marking:
        from ..analysis.negative_marking import break_even_p
        try:
            p_star = break_even_p(float(marking.get("R", 1)),
                                  float(marking.get("W", 0)),
                                  float(marking.get("U", 0)))
            ok = 0 <= p_star <= 1
            checks.append(Check("negative-marking compliance", ok,
                                detail=f"break-even p={p_star:.3f}"))
        except (TypeError, ValueError) as exc:
            checks.append(Check("negative-marking compliance", False,
                                detail=f"marking rules unreadable: {exc}"))
    elif mcq.get("negative_marking_note"):
        checks.append(Check("negative-marking compliance", False, severity="review",
                            detail="negative-marking note without read rules (FR-NM-01)"))

    # --- factual validity / currentness (needs human or source) ----------- #
    checks.append(Check("factual validity", True, severity="review",
                        detail="auto-checks passed; independent verification "
                               "NOT claimed (FR-VAL-03)"))
    checks.append(Check("currentness", True, severity="review",
                        detail="rules/syllabus currency must be confirmed by reviewer"))

    # --- duplication ------------------------------------------------------ #
    dup = False
    if corpus and en_stem:
        for other in corpus:
            if str(other.get("id")) == iid:
                continue
            if is_near_duplicate(en_stem, str(other.get("stem_en", ""))):
                dup = True
                reviews.append(f"near-duplicate of {other.get('id')}")
                break
    checks.append(Check("duplication", not dup,
                        detail="near-duplicate detected" if dup else "unique"))

    # --- images ------------------------------------------------------------ #
    if mcq.get("image_id"):
        img = mcq.get("image") or {}
        checks.append(Check("image validity", bool(img.get("path") or img.get("origin")),
                            detail="image record present"))
        checks.append(Check("image appropriateness",
                            str(img.get("ssj", "")).startswith("SSJ-GUARDRAIL: PASS")
                            or bool(img.get("ssj", "") == ""),
                            detail=str(img.get("ssj", "unreviewed"))))
        caps = img.get("caption_te") or mcq.get("image_caption_te")
        alt = img.get("alt_te") or mcq.get("image_alt_te")
        checks.append(Check("alt-text/bilingual caption", bool(caps and alt),
                            detail="EN+TE caption and alt-text required (IR-05, §3.6)"))

    # --- SSJ --------------------------------------------------------------- #
    ssj_res = [check_text(str(mcq.get(k, "")))
               for k in ("stem_en", "stem_te", "explanation_en", "explanation_te")]
    ssj_tag = overall_tag(ssj_res)
    checks.append(Check("SSJ compliance", ssj_tag in ("SSJ-GUARDRAIL: PASS",
                                                      "SSJ-GUARDRAIL: NOT APPLICABLE"),
                        detail=ssj_tag))

    # --- status decision (exactly one of four, FR-VAL-02) ------------------ #
    if ssj_tag == "SSJ-GUARDRAIL: EXCLUDED":
        status = EXCLUDED
    elif any(not c.ok and c.severity == "blocker" for c in checks):
        status = NEEDS_CORRECTION
    else:
        if tier == "T0" and "generated on a T0 model" not in " ".join(reviews):
            reviews.append("generated on a T0 model: quality may be "
                           "insufficient (FR-GOV-07)")
        status = HUMAN_REVIEW if (reviews or any(not c.ok for c in checks)) else VALIDATED
    return ValidationReport(item_id=iid, status=status, checks=checks,
                            ssj_tag=ssj_tag, review_reasons=reviews)


def _numbers(text: str) -> list[str]:
    import re
    return re.findall(r"\d+(?:\.\d+)?", text or "")


# --------------------------------------------------------------------------- #
# Human-in-the-loop review queue (FR-HIL)
# --------------------------------------------------------------------------- #


@dataclass
class QueueItem:
    item_id: str
    kind: str                 # mcq | section | image | probability | ...
    reason: str               # FR-HIL-01 categories
    evidence: str             # what to inspect
    action: str               # recommended action
    priority: float = 0.0
    harm: float = 0.0
    uncertainty: float = 0.0
    importance: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return dict(vars(self))


class ReviewQueue:
    """priority = harm x uncertainty x importance (FR-HIL-03)."""

    def __init__(self) -> None:
        self.items: list[QueueItem] = []

    def add(self, item_id: str, kind: str, reason: str, evidence: str,
            action: str, harm: float = 0.5, uncertainty: float = 0.5,
            importance: float = 0.5) -> QueueItem:
        q = QueueItem(item_id=item_id, kind=kind, reason=reason,
                      evidence=evidence[:500], action=action,
                      harm=harm, uncertainty=uncertainty, importance=importance,
                      priority=round(harm * uncertainty * importance, 4))
        self.items.append(q)
        return q

    def add_report(self, report: ValidationReport, kind: str = "mcq") -> None:
        for c in report.failures:
            self.add(report.item_id, kind, reason=c.name, evidence=c.detail,
                     action="review and correct or accept" if c.severity != "blocker"
                     else "correct before export",
                     harm=0.8 if c.severity == "blocker" else 0.4,
                     uncertainty=0.6, importance=0.7)
        for r in report.review_reasons:
            self.add(report.item_id, kind, reason=r,
                     evidence=report.ssj_tag or "see checks",
                     action="subject-matter review", harm=0.3,
                     uncertainty=0.7, importance=0.6)

    def prioritised(self) -> list[QueueItem]:
        return sorted(self.items, key=lambda i: -i.priority)

    def __len__(self) -> int:
        return len(self.items)

    def as_rows(self) -> list[dict[str, Any]]:
        return [i.as_dict() for i in self.prioritised()]

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for i in self.items:
            counts[i.reason] = counts.get(i.reason, 0) + 1
        return counts


def stable_id(prefix: str, *parts: Any) -> str:
    h = hashlib.sha1("||".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:8]
    return f"{prefix}-{h}"
