"""SSJ guardrail engine tests (§3.2, TC-13, TC-14, §3.8 report)."""

from nandaka.ssj import (EXCLUDED, NA, PASS, REVIEW, REPORT_CHECKS,
                         check_image, check_text, compliance_report,
                         overall_tag)


def test_fatalism_excluded_tc14():
    """TC-14: 'your jatka guarantees success' must be refused."""
    res = check_text("Your jatka guarantees success in this exam; the stars "
                     "have fixed your rank.")
    assert res.tag == EXCLUDED
    assert any(f.rule == "SSJ-05" and f.category == "fatalism"
               for f in res.findings)


def test_telugu_fatalism_detected():
    res = check_text("మీ జాతకం ప్రకారం ఈ పరీక్షలో విజయం ఖాయం.")
    assert res.tag == EXCLUDED


def test_malpractice_excluded():
    res = check_text("Here are the leaked paper questions from today's "
                     "live exam with the answer key.")
    assert res.tag == EXCLUDED
    assert any(f.rule == "SSJ-11" for f in res.findings)


def test_discrimination_excluded():
    res = check_text("Students of lower caste are inferior and cannot succeed.")
    assert res.tag == EXCLUDED
    assert any(f.rule == "SSJ-08" for f in res.findings)


def test_privacy_request_excluded():
    res = check_text("Please share your horoscope and birth details so I can "
                     "predict your rank.")
    assert res.tag == EXCLUDED
    assert any(f.rule == "SSJ-06" for f in res.findings)


def test_clean_academic_text_passes():
    res = check_text("The Directive Principles were adopted from the Irish "
                     "Constitution and guide the state in making policy.")
    assert res.tag == PASS
    assert res.evidence_label in ("verified fact", "source-grounded fact")


def test_uncertainty_goes_to_review():
    """SSJ-14: uncertain claims -> REVIEW REQUIRED, not silent PASS."""
    res = check_text("This topic might perhaps appear in the exam; they say "
                     "it is always important to prepare it carefully.",
                     allow_review=True)
    assert res.tag == REVIEW
    assert any(f.rule == "SSJ-14" for f in res.findings)


def test_medical_authority_review():
    res = check_text("Take this tablet daily; you have depression and this "
                     "is guaranteed to cure it.")
    assert res.tag in (REVIEW, EXCLUDED)
    assert any(f.rule == "SSJ-10" for f in res.findings)


def test_evidence_labels_declared_and_validated():
    ok = check_text("Net domestic product rose 6.2% this quarter.",
                    declared_label="statistical inference")
    assert ok.evidence_label == "statistical inference"
    bad = check_text("Something.", declared_label="vibes")
    assert any(f.rule == "SSJ-04" for f in bad.findings)


def test_overall_tag_precedence():
    """SSJ-13: exclusion outranks review outranks pass."""
    assert overall_tag([check_text("fine."), check_text("your jatka is fixed "
                                                        "by stars")]) == EXCLUDED
    assert overall_tag([check_text("fine."), check_text("x", allow_review=True)])
    assert overall_tag([check_text("capital of India is New Delhi")]) == PASS
    assert overall_tag([]) == NA


def test_image_checks():
    bad = check_image({"path": "a.png", "licence": "unknown",
                       "IMAGE-EVIDENCE": "AI-GENERATED"})
    assert bad.tag == REVIEW
    assert any(f.rule == "IR-04" for f in bad.findings)
    excluded = check_image({"caption": "actually photographed in 1947",
                            "licence": "public domain"})
    assert excluded.tag == EXCLUDED
    good = check_image({"path": "b.svg", "licence": "CC0",
                        "IMAGE-EVIDENCE": "AI-GENERATED",
                        "caption": "generated chart"})
    assert good.tag in (PASS, REVIEW)   # IR-09 requires visible caption: present


def test_compliance_report_schema():
    """§3.8: exactly the 11 documented checks, every row with status."""
    results = [check_text("Your jatka guarantees rank"),
               check_text("leaked paper available")]
    rows = compliance_report(results)
    assert [r["check"] for r in rows] == REPORT_CHECKS
    assert len(rows) == 11
    fatal = next(r for r in rows if r["check"] == "No fatalism or fear prediction")
    assert fatal["status"] == "EXCLUDED" and fatal["evidence"]
    clean = compliance_report([check_text("The constitution was adopted in 1949.")])
    assert all(r["status"] == "PASS" for r in clean)
