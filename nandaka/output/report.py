"""Batch assembly: standard output structure (FR-OUT-01..04, DR-03..DR-06).

``write_batch(ctx)`` takes a context dict produced by the pipeline and emits
a complete, self-validating batch directory:

    batch-YYYYmmdd-HHMMSS/
      report.md / report.txt / report.pdf
      mcqs.csv / mcqs.xlsx           validation.csv
      review-queue.csv               ssj-report.csv
      images.csv / images.json       assets/
      provenance.json                metrics.json
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .metrics import BASELINE_MINUTES, build_report
from .writers import validate_output, write_output


def _md_table(headers: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return "_(no data)_\n"
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join([" --- "] * len(headers)) + "|"]
    for row in rows:
        cells = [str(c).replace("|", "/").replace("\n", " ")[:120] for c in row]
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out) + "\n"


def assemble_report(ctx: dict[str, Any]) -> str:
    """Render the 16-section batch report as Markdown."""
    m: dict[str, Any] = ctx.get("metrics", {})
    meas = m.get("measurement", {})
    S: list[str] = []
    add = S.append

    add("# NANDAKA Batch Report")
    add(f"\nBatch: `{ctx.get('batch_id', '')}`  |  Generated: "
        f"{time.strftime('%Y-%m-%d %H:%M:%S')}  |  Model: "
        f"{ctx.get('model_name', '?')} ({ctx.get('tier', '?')})  |  Backend: "
        f"{ctx.get('backend', 'local')}\n")

    # 1 executive summary ------------------------------------------------
    add("## 1. Executive Summary\n")
    add(ctx.get("executive_summary",
                "No executive summary supplied.") + "\n")
    sc = m.get("automation", {}).get("status_counts", {})
    add(_md_table(["Metric", "Value"], [
        ["MCQs generated", ctx.get("mcq_count", 0)],
        ["Validation statuses", json.dumps(sc, ensure_ascii=False)],
        ["Time saved vs 120-min baseline",
         f"{meas.get('time_saved_pct', 'n/a')}%"],
        ["SSJ tag", ctx.get("ssj_tag", "SSJ-GUARDRAIL: NOT APPLICABLE")],
        ["Review queue size", ctx.get("review_queue_size", 0)],
    ]))

    # 2 source inventory --------------------------------------------------
    add("## 2. Source Inventory and Gaps\n")
    inv = ctx.get("inventory", {})
    add(_md_table(["Source", "Kind", "Chars", "Pages", "Notes"],
                  [[s.get("path", ""), s.get("kind", ""), s.get("chars", 0),
                    s.get("pages", 0), "; ".join(s.get("notes", []))]
                   for s in inv.get("sources", [])]))
    gaps = inv.get("gaps", [])
    if gaps:
        add("**Gaps reported (assumptions are labelled, never silent):**\n")
        for g in gaps:
            add(f"- {g}")
        add("")

    # 3 syllabus / topic coverage -----------------------------------------
    add("## 3. Syllabus / Topic Coverage Matrix\n")
    add(_md_table(["Topic", "In syllabus", "MCQs", "Figures"],
                  [[t, "yes" if ctx.get("syllabus", {}).get(t, True) else "no",
                    ctx.get("mcqs_per_topic", {}).get(t, 0),
                    ctx.get("figures_per_topic", {}).get(t, 0)]
                   for t in ctx.get("topics", [])]))

    # 4 frequency table ----------------------------------------------------
    add("## 4. Historical Frequency Table\n")
    add(_md_table(["Topic", "Count", "Years covered", "Most recent",
                   "Repetitions", "Diagram %"],
                  ctx.get("frequency_rows", [])))

    # 5 TPS + probability --------------------------------------------------
    add("## 5. Topic Priority Scores and Occurrence Estimates\n")
    add(_md_table(["Topic", "TPS", "Freq", "Rec", "Syl", "Cons", "Provisional",
                   "Probability"],
                  ctx.get("tps_rows", [])))
    add("_Scoring rules, windows and denominators (FR-HA-03):_\n")
    add("```json\n" + json.dumps(ctx.get("tps_rules", {}), indent=2) + "\n```\n")

    # 6 negative marking ----------------------------------------------------
    add("## 6. Negative-Marking Analysis\n")
    add(_md_table(["R", "W", "U", "p", "E(attempt)", "E(skip)", "Break-even",
                   "Recommendation"],
                  ctx.get("negative_rows", [])))
    for note in ctx.get("negative_notes", []):
        add(f"- {note}")
    add("")

    # 7 MCQ set --------------------------------------------------------------
    add("## 7. MCQ Set (stems)\n")
    add(_md_table(["ID", "Topic", "EN stem", "TE stem", "Difficulty", "Status"],
                  ctx.get("mcq_rows", [])))

    # 8 answer key -----------------------------------------------------------
    add("## 8. Answer Key and Explanations (EN + TE)\n")
    add(_md_table(["ID", "Answer", "Explanation (EN)", "వివరణ (TE)", "Source"],
                  ctx.get("answer_rows", [])))

    # 9 validation -----------------------------------------------------------
    add("## 9. Validation Report\n")
    add(_md_table(["Item", "Status", "Failed checks", "SSJ tag"],
                  ctx.get("validation_rows", [])))

    # 10 visual assets --------------------------------------------------------
    add("## 10. Visual Assets Bundle\n")
    add(_md_table(["Image ID", "Kind", "Evidence", "Rights", "Caption (EN)",
                   "క్యాప్షన్ (TE)"],
                  ctx.get("image_rows", [])))
    cov = ctx.get("visual_coverage", {})
    if cov:
        add(f"Visual coverage: {cov.get('visual_coverage_pct', 0)}% of topics "
            f"have a validated figure; {cov.get('mcq_image_pct', 0)}% of MCQs "
            f"carry an image (IR-17).\n")
    for disc in ctx.get("image_disclosures", []):
        add(f"- {disc}")
    add("")

    # 11 SSJ compliance --------------------------------------------------------
    add("## 11. SSJ Compliance Report\n")
    add(_md_table(["Check", "Status", "Evidence / Notes", "Action"],
                  ctx.get("ssj_rows", [])))
    add(f"Overall batch tag: **{ctx.get('ssj_tag', 'SSJ-GUARDRAIL: NOT APPLICABLE')}**\n")

    # 12 review queue -----------------------------------------------------------
    add("## 12. Human-Review Queue\n")
    add(_md_table(["Priority", "Item", "Kind", "Reason", "Evidence", "Action"],
                  ctx.get("review_rows", [])))

    # 13 automation opportunities ------------------------------------------------
    add("## 13. Automation Opportunities\n")
    for line in ctx.get("automation_notes",
                        ["- Record stage timings for one full manual run to "
                         "replace estimates with measurements (FR-MET-04)."]):
        add(line if line.startswith("-") else f"- {line}")
    add("")

    # 14 performance --------------------------------------------------------------
    add("## 14. Performance and Time-Savings\n")
    add(_md_table(["Metric", "Value"], [
        ["Baseline (procedure documentation)", f"{BASELINE_MINUTES:.0f} min"],
        ["Actual (this run)", f"{meas.get('actual_minutes', 'n/a')} min"],
        ["Time saved", f"{meas.get('time_saved_pct', 'n/a')}%"],
        ["Peak RSS reported", f"{ctx.get('rss_mb', 'n/a')} MB "
                              f"(budget {ctx.get('ram_budget_mb', 'n/a')} MB)"],
        ["n_ctx used", ctx.get("n_ctx", "auto")],
    ]))
    add("\n_Phase timings (minutes):_\n")
    add("```json\n" + json.dumps(meas.get("phases_minutes", {}), indent=2) + "\n```\n")

    # 15 quality metrics -------------------------------------------------------------
    add("## 15. Quality Metrics\n")
    q = m.get("quality", {})
    add(_md_table(["Metric", "Value"],
                  [[k, v] for k, v in q.items()]))
    add("_Measured vs estimated: see `metrics.json` note (FR-MET-04)._\n")

    # 16 next-cycle prompt --------------------------------------------------------------
    add("## 16. Next-Cycle Prompt\n")
    add(ctx.get("next_cycle_prompt",
                "- Re-run after clearing the review queue; add source PDFs for "
                "topics marked provisional; verify image licences before export."))
    add("")
    return "\n".join(S)


# --------------------------------------------------------------------------- #
# Full batch writer
# --------------------------------------------------------------------------- #


def write_batch(cfg, ctx: dict[str, Any]) -> dict[str, Any]:
    """Write every batch artefact, validate them, and summarise (FR-OUT-*)."""
    batch_id = ctx.get("batch_id") or time.strftime("batch-%Y%m%d-%H%M%S")
    batch_dir = Path(cfg.work_dir) / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)
    ctx["batch_id"] = batch_id
    if "metrics" not in ctx:
        if ctx.get("timer") is not None:
            ctx["metrics"] = build_report(
                timer=ctx["timer"],
                status_counts=ctx.get("status_counts", {}),
                quality=ctx.get("quality", {}),
                coverage=ctx.get("visual_coverage", {}) or {})
        else:
            ctx["metrics"] = {"note": "no timer supplied; timings not measured"}

    md_text = assemble_report(ctx)
    written: list[dict[str, Any]] = []

    def emit(kind: str, name: str, content: Any) -> None:
        info = write_output(kind, batch_dir / name, content)
        info.update(validate_output(batch_dir / name, kind))
        written.append(info)

    emit("md", "report.md", md_text)
    emit("txt", "report.txt", md_text)
    emit("pdf", "report.pdf", md_text)
    if ctx.get("mcq_records"):
        emit("csv", "mcqs.csv", ctx["mcq_records"])
        emit("xlsx", "mcqs.xlsx", _mcq_sheets(ctx))
    emit("csv", "validation.csv", ctx.get("validation_records", []))
    emit("csv", "review-queue.csv", ctx.get("review_records", []))
    emit("csv", "ssj-report.csv", ctx.get("ssj_rows", []))
    emit("csv", "frequency.csv", ctx.get("frequency_records", []))
    emit("csv", "tps.csv", ctx.get("tps_records", []))

    # provenance (DR-06)
    prov = ctx.get("provenance", {})
    prov.update({
        "batch_id": batch_id,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "nandaka_version": __import__("nandaka").__version__,
    })
    (batch_dir / "provenance.json").write_text(
        json.dumps(prov, indent=2, ensure_ascii=False), encoding="utf-8")
    (batch_dir / "metrics.json").write_text(
        json.dumps(ctx.get("metrics", {}), indent=2, ensure_ascii=False),
        encoding="utf-8")

    summary = {
        "batch_dir": str(batch_dir),
        "files": written,
        "all_valid": all(f.get("ok", False) for f in written),
        "invalid": [f["path"] for f in written if not f.get("ok")],
        "ssj_tag": ctx.get("ssj_tag", "SSJ-GUARDRAIL: NOT APPLICABLE"),
        "review_queue": ctx.get("review_queue_size", 0),
    }
    (batch_dir / "batch-summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary


def _mcq_sheets(ctx: dict[str, Any]) -> dict[str, list[list[Any]]]:
    mcqs = ctx.get("mcq_records", [])
    validations = ctx.get("validation_records", [])
    sheets: dict[str, list[list[Any]]] = {}
    if mcqs:
        headers = list(dict.fromkeys(k for r in mcqs for k in r))
        sheets["MCQs"] = [headers] + [[r.get(h, "") for h in headers] for r in mcqs]
    if validations:
        headers = list(dict.fromkeys(k for r in validations for k in r))
        sheets["Validation"] = [headers] + \
            [[r.get(h, "") for h in headers] for r in validations]
    return sheets or {"Empty": [["no rows"]]}
