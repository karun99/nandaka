"""Batch report writer: 16 sections, artefact set, structural validation."""

from __future__ import annotations

import json
from pathlib import Path

from nandaka.output.metrics import RunTimer, build_report, quality_metrics
from nandaka.output.report import assemble_report, write_batch


def _ctx() -> dict:
    timer = RunTimer()
    timer.start("MCQ generation")
    timer.stop("MCQ generation")
    quality = quality_metrics(items_total=3, duplicates=0,
                              translation_defects=1, unsupported_claims=0,
                              human_interventions=1, ssj_review=0,
                              ssj_excluded=0, images_total=2,
                              images_correct=2, images_cleared=1,
                              images_with_coverage=1, topics_total=2)
    return {
        "batch_id": "batch-test-0001",
        "model_name": "synthetic-llama",
        "tier": "T2",
        "backend": "local",
        "executive_summary": "Generated 3/3 bilingual MCQs for 'Constitution'.",
        "inventory": {"sources": [{"path": "a.md", "kind": "markdown",
                                   "chars": 120, "pages": 1, "notes": []}],
                      "gaps": ["no past papers for Economy"]},
        "topics": ["Constitution", "Economy"],
        "syllabus": {"Constitution": True, "Economy": False},
        "mcqs_per_topic": {"Constitution": 3, "Economy": 0},
        "figures_per_topic": {"Constitution": 1},
        "frequency_rows": [["Constitution", 5, 6, 2024, 2, "33%"]],
        "tps_rows": [["Constitution", 71.5, 5, 2024, 6, "33%", "no", "Likely"]],
        "tps_rules": {"window_years": 7, "note": "test rules"},
        "negative_rows": [[1.0, 0.33, 0.0, 0.6, 0.468, 0.0, 0.753, "ATTEMPT"]],
        "negative_notes": ["E(attempt) > E(skip)"],
        "mcq_count": 3,
        "mcq_rows": [["mcq-1", "Constitution", "Which article?", "ఏ అధికరణం?",
                      "medium", "VALIDATED"]],
        "answer_rows": [["mcq-1", "Article 14", "Because the text says so.",
                         "వచనం చెప్పినట్లు", "NCERT Class 8 Civics"]],
        "mcq_records": [{"id": "mcq-1", "topic": "Constitution",
                         "stem_en": "Which article?", "answer": "Article 14"}],
        "validation_rows": [["mcq-1", "VALIDATED", "-", "SSJ-GUARDRAIL: PASS"]],
        "validation_records": [{"item_id": "mcq-1", "status": "VALIDATED"}],
        "image_rows": [["img-1", "svg", "computed", "CC-BY-0",
                        "TPS chart", "TPS చార్ట్"]],
        "image_disclosures": ["IR-16: no photographic generation available"],
        "visual_coverage": {"visual_coverage_pct": 50.0, "mcq_image_pct": 33.3},
        "ssj_rows": [{"check": "No fatalism or fear prediction", "status": "PASS",
                      "evidence": "", "action": ""}],
        "ssj_tag": "SSJ-GUARDRAIL: PASS",
        "review_rows": [[1, "mcq-2", "mcq", "missing Telugu stem", "…",
                         "translate and re-review"]],
        "review_records": [{"item_id": "mcq-2", "priority": 1}],
        "review_queue_size": 1,
        "status_counts": {"VALIDATED": 2, "HUMAN REVIEW REQUIRED": 1},
        "quality": quality,
        "timer": timer,
        "provenance": {"models": [{"name": "synthetic-llama", "tier": "T2"}],
                       "cloud": {"enabled": False}},
        "rss_mb": 412.5,
        "ram_budget_mb": 900,
        "n_ctx": 2048,
        "automation_notes": ["TPS computation is fully automated."],
        "next_cycle_prompt": "- Clear the 1-item review queue.",
    }


def test_assemble_report_has_all_16_sections():
    md = assemble_report(_ctx())
    for i in range(1, 17):
        assert f"## {i}." in md, f"missing section {i}"
    assert "Constitution" in md
    assert "SSJ-GUARDRAIL: PASS" in md
    assert "Economy" in md  # gap disclosed in section 2


def test_write_batch_writes_and_validates_all_outputs(nandaka_home, tmp_path):
    cfg = nandaka_home
    ctx = _ctx()
    summary = write_batch(cfg, ctx)

    # every emitted output reopened and structurally validated (FR-OUT-03)
    assert summary["all_valid"], summary["invalid"]

    batch_dir = Path(summary["batch_dir"])
    assert batch_dir.exists()
    expected = ["report.md", "report.txt", "report.pdf", "mcqs.csv",
                "mcqs.xlsx", "validation.csv", "review-queue.csv",
                "ssj-report.csv", "frequency.csv", "tps.csv",
                "provenance.json", "metrics.json", "batch-summary.json"]
    for name in expected:
        assert (batch_dir / name).exists(), f"missing {name}"
    assert summary["ssj_tag"] == "SSJ-GUARDRAIL: PASS"
    assert summary["review_queue"] == 1

    # provenance and metrics are real JSON
    prov = json.loads((batch_dir / "provenance.json").read_text(encoding="utf-8"))
    assert prov["batch_id"] == "batch-test-0001"
    assert prov["nandaka_version"]
    metrics = json.loads((batch_dir / "metrics.json").read_text(encoding="utf-8"))
    assert "measurement" in metrics and "quality" in metrics

    # report contents survive the round trip
    text = (batch_dir / "report.md").read_text(encoding="utf-8")
    assert "## 16. Next-Cycle Prompt" in text


def test_write_batch_metrics_without_timer(nandaka_home):
    cfg = nandaka_home
    ctx = _ctx()
    ctx.pop("timer")
    summary = write_batch(cfg, ctx)
    assert summary["all_valid"]
    batch_dir = Path(summary["batch_dir"])
    metrics = json.loads((batch_dir / "metrics.json").read_text(encoding="utf-8"))
    assert "note" in metrics  # honest about missing timings (FR-MET-04)


def test_build_report_time_saved():
    timer = RunTimer()
    rep = build_report(timer=timer, status_counts={"VALIDATED": 2},
                       quality={"items_total": 2},
                       coverage={"visual_coverage_pct": 50.0})
    assert rep["measurement"]["baseline_minutes"] == 120
    assert rep["measurement"]["time_saved_pct"] >= 0
    assert rep["automation"]["status_counts"] == {"VALIDATED": 2}
