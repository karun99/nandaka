"""TPS, probability, negative-marking maths (FR-HA, FR-NM, TC-16/TC-17)."""

import math

from nandaka.analysis.frequency import (compute_stats, diagram_frequency,
                                        is_near_duplicate, normalise)
from nandaka.analysis.negative_marking import (MarkingScheme, advise,
                                               break_even_p,
                                               expected_attempt,
                                               parse_marking_text)
from nandaka.analysis.tps import WEIGHTS, compute_tps, occurrence_probability
from tests.conftest import make_papers_csv


# ---------------- frequency ---------------- #


def test_normalise_and_near_duplicate():
    assert normalise("  Hello,  WORLD! ") == "hello world"
    # token-Jaccard: only near-exact duplicates count (FR-GOV-05)
    assert is_near_duplicate("The Constitution was adopted in 1949 by the Assembly",
                             "The Constitution was adopted in 1949 by the assembly.")
    assert not is_near_duplicate("Which article defines equality?",
                                 "Which article defines equality before law?")
    assert not is_near_duplicate("Capital of France?", "Photosynthesis cycle?")


def test_compute_stats_from_papers(tmp_path):
    papers = make_papers_csv(tmp_path / "p.csv")
    import csv
    with papers.open() as fh:
        rows = [dict(r) for r in csv.DictReader(fh)]
    stats = compute_stats(rows)
    assert "Constitution" in stats and "Economy" in stats
    st = stats["Constitution"]
    assert st.count == 18                      # 7y x 2 direct + 4 reworded
    assert st.years_covered == 7
    assert st.most_recent == 2024
    diag = diagram_frequency(rows)
    assert diag["Economy"] == 1.0              # every Economy item is a diagram


# ---------------- TPS ---------------- #


def test_tps_weights_and_bounds(tmp_path):
    papers = make_papers_csv(tmp_path / "p.csv")
    import csv
    with papers.open() as fh:
        rows = [dict(r) for r in csv.DictReader(fh)]
    stats = compute_stats(rows)
    res = compute_tps(stats["Constitution"], all_stats=stats,
                      syllabus_relevance=1.0, years_seen=list(range(2018, 2025)))
    assert 0 <= res.score <= 100
    # weighted sum of normalised components
    expect = sum(res.components[k] * WEIGHTS[k] for k in WEIGHTS)
    assert math.isclose(res.score, expect, rel_tol=1e-9)
    assert res.provisional is False            # plenty of evidence
    assert res.components["syllabus"] == 100.0


def test_tps_provisional_without_syllabus(tmp_path):
    papers = make_papers_csv(tmp_path / "p.csv")
    import csv
    with papers.open() as fh:
        rows = [dict(r) for r in csv.DictReader(fh)]
    stats = compute_stats(rows)
    res = compute_tps(stats["Constitution"], all_stats=stats,
                      syllabus_relevance=None)
    assert res.provisional is True             # FR-HA-04
    assert any("provisional" in r for r in res.reasons)


def test_probability_not_reliably_estimable_tc17():
    """TC-17: 1 appearance across 3 papers -> no probability, provisional TPS."""
    prob = occurrence_probability(appearances=1, n_papers=3,
                                  years=[2023], years_seen=[2022, 2023, 2024])
    assert prob["value"] is None
    assert prob["label"] == "Not reliably estimable"
    assert "provisional TPS" in prob["limitations"][0]


def test_probability_estimated_with_evidence():
    prob = occurrence_probability(appearances=8, n_papers=10,
                                  years=list(range(2015, 2025)),
                                  years_seen=list(range(2015, 2025)))
    assert prob["value"] == 80.0
    assert prob["lo"] < prob["value"] < prob["hi"]     # Wilson interval
    assert prob["confidence"] in ("high", "medium", "low")
    assert "not a guarantee" in prob["limitations"][0]


# ---------------- negative marking ---------------- #


def test_tc16_attempt_recommendation():
    """TC-16: R=1, W=0.33, U=0, p=0.6 -> E=0.468 > 0 => ATTEMPT."""
    res = advise(MarkingScheme(R=1, W=0.33, U=0, source="test"), 0.6)
    assert math.isclose(res.E_attempt, 0.468, abs_tol=1e-9)
    assert res.E_skip == 0.0
    assert res.recommendation == "ATTEMPT"


def test_break_even_matches_formula():
    assert math.isclose(break_even_p(1, 0.33, 0), 0.33 / 1.33)
    assert math.isclose(break_even_p(2, 1, 0), 1 / 3)
    # with U > 0: p* = (U+W)/(R+W)
    assert math.isclose(break_even_p(1, 0.5, 0.5), 1.0 / 1.5)


def test_skip_recommended_when_below_break_even():
    res = advise(MarkingScheme(R=1, W=1, U=0, source="test"), 0.3)
    assert res.recommendation == "SKIP"


def test_p_unknown_never_substituted():
    """FR-NM-04: no candidate data -> p unknown, no advice."""
    res = advise(MarkingScheme(R=1, W=0.25, U=0, source="test"), None)
    assert res.p is None
    assert res.as_dict()["p"] == "unknown"
    assert res.recommendation.startswith("INSUFFICIENT DATA")
    assert any("must not be substituted" in n for n in res.notes)


def test_expected_attempt_formula():
    assert math.isclose(expected_attempt(2, 1, 0.5), 0.5 * 2 - 0.5 * 1)


def test_parse_marking_text():
    s = parse_marking_text("2 marks for correct, 0.5 negative marking for wrong, "
                           "0 for unattempted")
    assert s and s.R == 2 and s.W == 0.5 and s.U == 0
    assert parse_marking_text("no numbers here") is None
