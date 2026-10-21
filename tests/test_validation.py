"""Validation battery + review queue (FR-VAL, FR-HIL, TC-10)."""

from nandaka.ssj import EXCLUDED as SSJ_EXCLUDED
from nandaka.validation import (EXCLUDED, HUMAN_REVIEW, NEEDS_CORRECTION,
                                STATUSES, VALIDATED, ReviewQueue, stable_id,
                                validate_mcq)


def good_mcq(**over):
    mcq = {
        "id": "mcq-1",
        "topic": "Constitution",
        "subtopic": "Preamble",
        "stem_en": "Which article of the Constitution deals with equality before law?",
        "stem_te": "సమానత్వం అనే అంశం రాజ్యాంగంలోని ఏ అధికరణం?",
        "options_en": ["Article 12", "Article 14", "Article 19", "Article 21"],
        "options_te": ["అధికరణ 12", "అధికరణ 14", "అధికరణ 19", "అధికరణ 21"],
        "answer": "Article 14",
        "explanation_en": "Article 14 guarantees equality before law (source: NCERT Class 10, ch. 2, p. 45).",
        "explanation_te": "అధికరణ 14 చట్టం ముందు సమానత్వాన్ని హామీ ఇస్తుంది (మూలం: NCERT).",
        "source_reference": "NCERT Class 10 Democratic Politics ch.2 p.45",
        "difficulty": "medium",
        "historical_evidence": "[2023] Which article ensures GPs?",
        "tps": 78.5,
        "probability": "60.0% (95% CI 40.0-80.0)",
        "negative_marking_note": "",
    }
    mcq.update(over)
    return mcq


def test_well_formed_mcq_validated():
    rep = validate_mcq(good_mcq(), marking={"R": 1, "W": 0.33, "U": 0},
                       tier="T2", syllabus_topics={"Constitution"})
    assert rep.status == VALIDATED, [c.__dict__ for c in rep.failures]
    assert rep.ssj_tag.startswith("SSJ-GUARDRAIL")
    assert all(c.ok for c in rep.checks)


def test_missing_answer_is_blocker():
    rep = validate_mcq(good_mcq(answer=""), tier="T2")
    assert rep.status == NEEDS_CORRECTION
    assert any(c.name == "answer validity" and not c.ok for c in rep.checks)


def test_lost_negation_in_telugu_is_blocker():
    rep = validate_mcq(
        good_mcq(stem_en="Which of the following is NOT a fundamental duty?",
                 stem_te="కింది వాటిలో ఏది ప్రాథమిక బాధ్యత?"),
        tier="T2")
    assert rep.status == NEEDS_CORRECTION
    assert any(c.name == "language validity" and not c.ok
               and "negation" in c.detail for c in rep.checks)


def test_number_mismatch_is_blocker():
    rep = validate_mcq(good_mcq(stem_te="సమానత్వం అనేది 14 అధికరణం?"), tier="T2")
    assert rep.status == NEEDS_CORRECTION
    assert any("numbers differ" in c.detail for c in rep.checks if not c.ok)


def test_t0_forces_human_review_tc10():
    """TC-10: T0-generated MCQs must be HUMAN REVIEW REQUIRED."""
    rep = validate_mcq(good_mcq(), tier="T0")
    assert rep.status == HUMAN_REVIEW
    assert any("T0" in r for r in rep.review_reasons)


def test_duplicate_detection():
    corpus = [good_mcq(id="other")]
    rep = validate_mcq(good_mcq(), corpus=corpus, tier="T2")
    assert any(c.name == "duplication" and not c.ok for c in rep.checks)
    assert rep.status in (NEEDS_CORRECTION, HUMAN_REVIEW)
    assert any("near-duplicate" in r for r in rep.review_reasons)


def test_ssj_exclusion_overrides_status():
    rep = validate_mcq(good_mcq(stem_en="Your jatka guarantees success in "
                                         "this exam, the stars fixed your rank."),
                       tier="T2")
    assert rep.status == EXCLUDED
    assert rep.ssj_tag == SSJ_EXCLUDED


def test_status_is_always_one_of_four():
    for variant in (good_mcq(), good_mcq(answer=""), good_mcq(source_reference="")):
        rep = validate_mcq(variant, tier="T2")
        assert rep.status in STATUSES


def test_no_independent_verification_claim():
    """FR-VAL-03: auto-checks must not masquerade as independent verification."""
    rep = validate_mcq(good_mcq(), tier="T2")
    fact = next(c for c in rep.checks if c.name == "factual validity")
    assert "NOT claimed" in fact.detail


def test_review_queue_priority_order():
    """FR-HIL-03: priority = harm x uncertainty x importance, descending."""
    q = ReviewQueue()
    q.add("low", "mcq", reason="minor wording", evidence="e", action="polish",
          harm=0.2, uncertainty=0.2, importance=0.2)
    q.add("high", "image", reason="licence unclear", evidence="no licence",
          action="clear rights", harm=0.9, uncertainty=0.9, importance=0.9)
    ordered = q.prioritised()
    assert ordered[0].item_id == "high"
    assert ordered[0].priority > ordered[1].priority
    assert len(q) == 2
    assert q.as_rows()[0]["item_id"] == "high"


def test_queue_from_report_lists_reason_evidence_action():
    rep = validate_mcq(good_mcq(answer=""), tier="T2")
    q = ReviewQueue()
    q.add_report(rep)
    assert len(q) >= 1
    item = q.prioritised()[0]
    assert item.reason and item.evidence and item.action


def test_stable_id_deterministic():
    assert stable_id("x", "a", 1) == stable_id("x", "a", 1)
    assert stable_id("x", "a", 1) != stable_id("x", "a", 2)
