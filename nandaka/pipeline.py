"""Workflow pipeline (FR-WF-01): ingest -> inventory -> analysis ->
prioritisation -> drafting/MCQ -> images -> validation -> SSJ ->
human review -> export -> metrics.

Every step is honest about capability: if a step cannot run (no model, no
network, no OCR), it reports the gap and continues (FR-IN-06, FR-WF-02).
"""

from __future__ import annotations

import csv
import time
from pathlib import Path
from typing import Any

from .analysis.negative_marking import MarkingScheme, advise, break_even_p
from .analysis.frequency import compute_stats, diagram_frequency, paper_set, question_years
from .analysis.tps import RULES_DOC, compute_tps
from .config import Config
from .drafting import Drafter
from .images import ImageLibrary, png_bar_chart, structured_image_description, svg_bar_chart, svg_flow
from .ingest import ingest, inventory
from .models.client import ChatError
from .models.server import ModelManager, cloud_client
from .output.metrics import RunTimer, quality_metrics
from .output.report import write_batch
from .sources import fetch_url, known_urls
from .ssj import check_text, compliance_report, overall_tag
from .validation import (HUMAN_REVIEW, ReviewQueue, stable_id, validate_mcq)


def _esc(text: str) -> str:
    from xml.sax.saxutils import escape
    return escape(str(text))


def _load_papers(path: str | None) -> list[dict[str, Any]]:
    if not path:
        return []
    p = Path(path)
    if not p.exists():
        return []
    with p.open("r", encoding="utf-8", newline="") as fh:
        return [dict(r) for r in csv.DictReader(fh)]


def _analysis_for(spec: dict[str, Any], papers: list[dict[str, Any]],
                  timer: RunTimer) -> dict[str, Any]:
    """Historical analysis -> per-topic stats, TPS rows, probability labels."""
    timer.start("research")
    stats = compute_stats(papers) if papers else {}
    years = question_years(papers)
    papers_seen = paper_set(papers)
    total_papers = len(papers_seen)
    diag = diagram_frequency(papers) if papers else {}
    syllabus = set(spec.get("syllabus") or [])
    relevance: dict[str, float | None] = {}
    for t in stats:
        if syllabus:
            relevance[t] = 1.0 if t in syllabus else 0.0
        else:
            relevance[t] = None

    tps_rows: list[list[Any]] = []
    tps_records: list[dict[str, Any]] = []
    per_topic: dict[str, Any] = {}
    for topic, st in sorted(stats.items(), key=lambda kv: -kv[1].count):
        res = compute_tps(st, all_stats=stats, syllabus_relevance=relevance[topic],
                          years_seen=years, n_papers_total=len(papers_seen))
        d = res.as_dict()
        tps_records.append(d)
        per_topic[topic] = d
        prob = d["occurrence_probability"]
        tps_rows.append([topic, d["tps"], st.count, st.most_recent,
                         st.years_covered, f"{diag.get(topic, 0) * 100:.0f}%",
                         "yes" if d["provisional"] else "no",
                         prob.get("label", "n/a")])
    freq_rows = [[t, st.count, st.years_covered, st.most_recent,
                  st.kinds.get("reworded", 0) + st.kinds.get("concept", 0),
                  f"{diag.get(t, 0) * 100:.0f}%"]
                 for t, st in sorted(stats.items(), key=lambda kv: -kv[1].count)]
    timer.stop("research")
    return {"stats": stats, "tps_per_topic": per_topic, "tps_rows": tps_rows,
            "tps_records": tps_records, "frequency_rows": freq_rows,
            "topics": list(stats) or ([spec.get("topic")] if spec.get("topic") else []),
            "papers": len(papers_seen), "rules": RULES_DOC,
            "probability": {t: p["occurrence_probability"] for t, p in per_topic.items()}}


def _images_stage(spec: dict[str, Any], analysis: dict[str, Any],
                  library: ImageLibrary, cfg: Config,
                  timer: RunTimer) -> dict[str, Any]:
    """IR-01 sourcing order + IR-03 labelling + IR-16 disclosure."""
    timer.start("image sourcing")
    disclosures: list[str] = []
    figures: dict[str, int] = {}
    generated = 0

    # (1/2) source-derived: adopt files listed by the user (docs/workspace)
    for i, entry in enumerate(spec.get("image_files", []) or [], start=1):
        p = Path(entry)
        if not p.exists():
            disclosures.append(f"listed image not found: {p}")
            continue
        rec = library.adopt_file(
            f"img-src-{i}", p, origin=str(p),
            licence=str(spec.get("image_licence", "")),
            caption_en=f"Source image extracted from {p.name}",
            caption_te=f"{p.name} నుండి తీసుకున్న మూల చిత్రం")
        figures[rec.image_id] = figures.get(rec.image_id, 0) + 1
        if not rec.cleared:
            disclosures.append(
                f"IMAGE-RIGHTS: REVIEW REQUIRED for {rec.image_id} (licence "
                "unclear) — excluded from final exports until cleared (IR-04)")

    timer.stop("image sourcing")

    # (4) generate illustrative diagrams from the actual analysis data
    if spec.get("images", "auto") != "off":
        timer.start("image generation")
        tps = {t: d["tps"] for t, d in analysis.get("tps_per_topic", {}).items()}
        if tps:
            svg = svg_bar_chart("Topic Priority Score (0-100)", tps)
            library.add_svg("img-tps-chart", svg,
                            origin="computed from historical papers",
                            caption_en="Bar chart of Topic Priority Scores generated from this batch's data",
                            caption_te="ఈ బ్యాచ్ డేటా నుండి రూపొందించిన TPS బార్ చార్ట్",
                            ssj_tag="SSJ-GUARDRAIL: PASS")
            png = png_bar_chart("TPS (0-100)", tps)
            library.add_png("img-tps-chart-png", png,
                            origin="computed from historical papers",
                            caption_en="Raster version of the TPS chart (AI-generated, not a photograph)",
                            caption_te="TPS చార్ట్ యొక్క రాస్టర్ వెర్షన్ (AI-రూపొందించినది, ఛాయాచిత్రం కాదు)",
                            ssj_tag="SSJ-GUARDRAIL: PASS")
            generated += 2
        steps = spec.get("process_steps") or []
        if steps:
            library.add_svg("img-process-flow",
                            svg_flow(spec.get("process_title", "Process"), steps),
                            origin="generated from spec",
                            caption_en="Flow diagram of the described process",
                            caption_te="వివరించిన ప్రక్రియ యొక్క ప్రవాహ రేఖాచిత్రం",
                            ssj_tag="SSJ-GUARDRAIL: PASS")
            generated += 1
        timer.stop("image generation")
        if spec.get("wants_photograph") or spec.get("photo_request"):
            # IR-16: no photographic generation capability — disclose + describe
            disclosures.append(
                "Photographic image generation is not available in Nandaka; "
                "a structured text description is provided instead (IR-16, CO-10).")
            desc = structured_image_description(
                str(spec.get("photo_request", "photograph")),
                {"purpose": spec.get("photo_purpose", "exam material"),
                 "must_not": "real people, official figures, jatka imagery"})
            lines = desc.splitlines()[3:11]  # keep the card short
            card = (
                '<svg xmlns="http://www.w3.org/2000/svg" width="640" '
                'height="{}"><rect width="640" height="{}" fill="#fff"/>'
                '<text x="16" y="30" font-family="sans-serif" font-size="14">'
                'IMAGE DESCRIPTION (IR-16 — generation unavailable)</text>{}</svg>'
            ).format(46 + 22 * len(lines), 46 + 22 * len(lines),
                     "".join(
                         f'<text x="16" y="{54 + i * 22}" font-family="monospace" '
                         f'font-size="12">{_esc(line[:92])}</text>'
                         for i, line in enumerate(lines)))
            library.add_svg("img-description-card", card,
                            evidence="ILLUSTRATIVE",
                            caption_en="Text-only illustration brief (no image generated)",
                            caption_te="పాఠ్య వివరణ మాత్రమే (చిత్రం రూపొందించబడలేదు)",
                            ssj_tag="SSJ-GUARDRAIL: PASS")
            generated += 1
    return {"disclosures": disclosures, "generated": generated}


def run_mcq_batch(cfg: Config, spec: dict[str, Any], *,
                  model_ref: str | None = None, count: int = 30,
                  papers_csv: str | None = None, cloud: bool = False,
                  force: bool = False, offline: bool = False,
                  images: str = "auto") -> dict[str, Any]:
    """FR-GOV + full workflow: one complete exam-prep batch."""
    cfg.ensure_dirs()
    timer = RunTimer()
    spec = dict(spec)
    spec["images"] = images
    manager = ModelManager(cfg)

    # 1. ingest supplied sources (FR-IN)
    timer.start("extraction")
    docs = ingest(spec.get("inputs", []) or [])
    inv = inventory(docs)
    sources = [d.path for d in docs if d.kind != "image"]
    spec.setdefault("sources", sources)
    timer.stop("extraction")

    # 2. source portal connectivity is advisory (offline-first)
    if not offline and spec.get("portal"):
        from .sources import enumerate_portal
        r = enumerate_portal(spec["portal"], cfg, allow_network=True)
        inv["gaps"].extend(r.notes)
        if r.ok:
            inv["portal_catalog"] = r.url

    # 3. historical analysis (FR-HA)
    papers = _load_papers(papers_csv or spec.get("papers_csv"))
    analysis = _analysis_for(spec, papers, timer)

    # 4. negative marking (FR-NM)
    timer.start("research")
    scheme = MarkingScheme.from_dict(spec.get("marking", {}) or {})
    if spec.get("marking"):
        scheme.source = scheme.source or "spec"
    p_raw = spec.get("p_candidate")
    p_val = None if p_raw in (None, "", "unknown") else float(p_raw)
    advice = advise(scheme, p_val)
    timer.stop("research")

    # 5. pick backend (local first; cloud only when explicitly enabled)
    timer.start("MCQ generation")
    try:
        client, entry, gate_msg = manager.task_client(
            "mcq", model_ref, force=force, prefer_cloud=cloud)
    except ChatError as exc:
        timer.stop("MCQ generation")
        return {"ok": False, "error": str(exc), "hint":
                "Drop a GGUF into " + cfg.model_dir + " or pass --model, "
                "or enable cloud with --cloud (needs an API key)."}
    drafter = Drafter(client, entry,
                      cloud="yes" if client.provider != "local" else "",
                      warnings=[gate_msg] if gate_msg else [])

    # 5b. probability label per topic, if defensible (FR-HA-05/06)
    topics = analysis["topics"] or [spec.get("topic", "General")]
    main_topic = topics[0]
    tps_data = analysis["tps_per_topic"].get(main_topic, {})
    prob_label = (tps_data.get("occurrence_probability", {}) or {}).get("label", "")
    if not prob_label:
        prob_label = "Not reliably estimable (no historical papers supplied)"
    spec_prob = {
        "exam": spec.get("exam", "government exam"),
        "subject": spec.get("subject", "general studies"),
        "topics": topics,
        "topic": main_topic,
        "evidence": _evidence_text(analysis, main_topic, papers),
        "marking": (f"R={scheme.R}, W={scheme.W}, U={scheme.U}"
                    if spec.get("marking") else "not supplied"),
        "probability_label": prob_label,
        "tps": tps_data.get("tps", ""),
        "negative_marking_note": _nm_note(advice),
    }
    gen = drafter.generate_mcqs(spec_prob, count=count, force_review=force)
    timer.stop("MCQ generation")

    # 6. images (IR)
    library = ImageLibrary(Path(cfg.work_dir) / "assets-tmp", cfg.image_max_kb)
    img_info = _images_stage(spec, analysis, library, cfg, timer)
    per_topic_img: dict[str, int] = {}
    if library.records:
        per_topic_img[main_topic] = len(library.records)

    # 7. validation (FR-VAL)
    timer.start("validation")
    reports = []
    seen: list[dict] = []
    syllabus_set = set(spec.get("syllabus") or [])
    for mcq in gen["mcqs"]:
        rep = validate_mcq(mcq, corpus=seen, marking=spec.get("marking"),
                           tier=entry.get("tier", "T0"),
                           syllabus_topics=syllabus_set or None)
        reports.append(rep)
        mcq["validation_status"] = rep.status
        mcq["ssj_tag"] = rep.ssj_tag
        seen.append(mcq)
    timer.stop("validation")

    # 8. SSJ (§3.2) over every text segment
    timer.start("SSJ check")
    ssj_results = []
    for mcq in gen["mcqs"]:
        for k in ("stem_en", "stem_te", "explanation_en", "explanation_te"):
            ssj_results.append(check_text(str(mcq.get(k, ""))))
    for rec in library.records:
        ssj_results.append(check_text(rec.caption_en + " " + rec.caption_te))
    ssj_rows = compliance_report(ssj_results)
    batch_tag = overall_tag(ssj_results)
    timer.stop("SSJ check")

    # 9. human review queue (FR-HIL)
    timer.start("human review")
    queue = ReviewQueue()
    for rep in reports:
        queue.add_report(rep, kind="mcq")
    if gen["shortfall"]:
        queue.add(stable_id("shortfall", main_topic), "batch",
                  reason="MCQ shortfall", evidence=str(gen["shortfall"]),
                  action="supply more source papers or accept the shortfall",
                  harm=0.3, uncertainty=0.6, importance=0.8)
    for rec in library.blocked_images():
        queue.add(rec.image_id, "image", reason="image rights unclear",
                  evidence=f"licence={rec.licence or 'unknown'}",
                  action="clear licence or drop image before export",
                  harm=0.7, uncertainty=0.6, importance=0.7)

    # 10. metrics (FR-MET)
    status_counts: dict[str, int] = {}
    for rep in reports:
        status_counts[rep.status] = status_counts.get(rep.status, 0) + 1
    lang_defects = sum(1 for r in reports for c in r.checks
                       if c.name == "language validity" and not c.ok)
    dup_count = sum(1 for r in reports for c in r.checks
                    if c.name == "duplication" and not c.ok)
    unsupported = sum(1 for r in reports for c in r.checks
                      if c.name == "source traceability" and not c.ok)
    cleared = len(library.rights_cleared_images())
    quality = quality_metrics(
        items_total=len(reports), duplicates=dup_count,
        translation_defects=lang_defects, unsupported_claims=unsupported,
        human_interventions=status_counts.get(HUMAN_REVIEW, 0),
        ssj_review=sum(1 for r in ssj_results if r.needs_review),
        ssj_excluded=sum(1 for r in ssj_results if r.excluded),
        images_total=len(library.records),
        images_correct=len(library.records) - len(library.blocked_images()),
        images_cleared=cleared,
        images_with_coverage=sum(1 for v in per_topic_img.values() if v),
        topics_total=max(1, len(analysis["topics"])))
    coverage = library.coverage(analysis["topics"] or [main_topic],
                                per_topic_img, len(gen["mcqs"]),
                                0 if not library.records else min(len(gen["mcqs"]),
                                                                  1))
    timer.stop("human review")

    # 11. export (FR-OUT)
    timer.start("export")
    mcq_records = [_mcq_flat(m) for m in gen["mcqs"]]
    prov = {
        "models": [{k: entry.get(k) for k in
                    ("name", "path", "tier", "quant", "parameters",
                     "context_length")}],
        "n_ctx": cfg.effective_n_ctx(),
        "temperature": cfg.temperature,
        "seed": cfg.seed,
        "backend": cfg.backend,
        "cloud": {"enabled": bool(cloud or client.provider != "local"),
                  "provider": cfg.cloud_provider if client.provider != "local" else "",
                  "model": client.model if client.provider != "local" else ""},
        "warnings": gen["warnings"],
    }
    ctx = {
        "model_name": entry.get("name", "unknown"),
        "tier": entry.get("tier", "T0"),
        "backend": client.provider,
        "inventory": inv,
        "topics": analysis["topics"],
        "mcqs_per_topic": {main_topic: len(gen["mcqs"])},
        "figures_per_topic": per_topic_img,
        "frequency_rows": analysis["frequency_rows"],
        "frequency_records": [dict(zip(["topic", "count", "years_covered",
                                        "most_recent", "repetitions",
                                        "diagram_pct"], row))
                              for row in analysis["frequency_rows"]],
        "tps_rows": analysis["tps_rows"],
        "tps_records": analysis["tps_records"],
        "tps_rules": analysis["rules"],
        "negative_rows": [[scheme.R, scheme.W, scheme.U,
                           advice.p if advice.p is not None else "unknown",
                           advice.E_attempt if advice.E_attempt is not None else "n/a",
                           advice.E_skip, round(advice.break_even_p, 3)
                           if advice.break_even_p != float("inf") else "n/a",
                           advice.recommendation]],
        "negative_notes": advice.notes,
        "mcq_count": len(gen["mcqs"]),
        "mcq_rows": [[m.get("id"), m.get("topic"), m.get("stem_en", "")[:70],
                      m.get("stem_te", "")[:60], m.get("difficulty", ""),
                      m.get("validation_status", "")]
                     for m in gen["mcqs"]],
        "answer_rows": [[m.get("id"), m.get("answer", ""),
                         m.get("explanation_en", "")[:90],
                         m.get("explanation_te", "")[:80],
                         m.get("source_reference", "")]
                        for m in gen["mcqs"]],
        "mcq_records": mcq_records,
        "validation_rows": [[r.item_id, r.status,
                             ", ".join(c.name for c in r.failures) or "-",
                             r.ssj_tag] for r in reports],
        "validation_records": [r.as_dict() for r in reports],
        "image_rows": [[r.image_id, r.kind, r.evidence, r.rights,
                        r.caption_en[:60], r.caption_te[:50]]
                       for r in library.records],
        "image_disclosures": img_info["disclosures"],
        "visual_coverage": coverage,
        "ssj_rows": ssj_rows,
        "ssj_tag": batch_tag,
        "review_rows": [[i.priority, i.item_id, i.kind, i.reason,
                         i.evidence[:70], i.action]
                        for i in queue.prioritised()],
        "review_records": queue.as_rows(),
        "review_queue_size": len(queue),
        "status_counts": status_counts,
        "quality": quality,
        "timer": timer,
        "provenance": prov,
        "rss_mb": manager.ram_report().get("rss_mb", "n/a"),
        "ram_budget_mb": cfg.ram_budget_mb,
        "n_ctx": cfg.effective_n_ctx(),
        "executive_summary": (
            f"Generated {len(gen['mcqs'])}/{count} bilingual MCQs for "
            f"'{main_topic}' using {entry.get('name')} ({entry.get('tier')}) "
            f"on the {client.provider} backend. Validation: "
            f"{status_counts}. SSJ: {batch_tag}. "
            f"Review queue: {len(queue)} item(s)."),
        "next_cycle_prompt": _next_cycle(gen, queue, analysis, advice),
        "automation_notes": [
            "Source portal enumeration runs unattended (cache-first).",
            "TPS + probability computation is fully automated.",
            "Image rights clearance and nuanced Telugu remain human tasks "
            "(FR-HIL-01).",
        ],
    }
    summary = write_batch(cfg, ctx)
    timer.stop("export")

    # persist image library manifest next to the batch (DR-03)
    batch_dir = Path(summary["batch_dir"])
    library.write_manifest(batch_dir)
    for rec in library.records:
        if rec.path and Path(rec.path).exists():
            dest = batch_dir / "assets" / Path(rec.path).name
            dest.parent.mkdir(exist_ok=True)
            try:
                dest.write_bytes(Path(rec.path).read_bytes())
            except OSError:
                pass
    summary.update(ok=True, mcqs=len(gen["mcqs"]), shortfall=gen["shortfall"],
                   warnings=gen["warnings"], ssj_tag=batch_tag)
    manager.unload()
    return summary


# --------------------------------------------------------------------------- #
# Drafting pipelines (FR-SB, FR-AB)
# --------------------------------------------------------------------------- #


def run_chapter(cfg: Config, spec: dict[str, Any], *,
                model_ref: str | None = None, force: bool = False,
                cloud: bool = False, formats: tuple[str, ...] = ("md", "pdf")
                ) -> dict[str, Any]:
    from .output.writers import write_output, validate_output
    cfg.ensure_dirs()
    timer = RunTimer()
    manager = ModelManager(cfg)
    try:
        client, entry, gate_msg = manager.task_client(
            "long_draft", model_ref, force=force, prefer_cloud=cloud)
    except ChatError as exc:
        return {"ok": False, "error": str(exc)}
    drafter = Drafter(client, entry, warnings=[gate_msg] if gate_msg else [])
    timer.start("drafting")
    chapter = drafter.draft_chapter(spec)
    timer.stop("drafting")
    if not chapter.get("ok"):
        manager.unload()
        return {"ok": False, "error": "; ".join(chapter.get("warnings", [])) or "no content"}

    timer.start("validation")
    ssj_results = [check_text(s["body"]) for s in chapter["sections"]]
    ssj_rows = compliance_report(ssj_results)
    batch_tag = overall_tag(ssj_results)
    queue = ReviewQueue()
    for u in chapter.get("unsourced", []):
        queue.add(stable_id("unsourced", u["section"]), "section",
                  reason="unsourced factual claim (FR-SB-03)",
                  evidence="; ".join(u["claims"][:3]),
                  action="attach a source reference or rewrite",
                  harm=0.6, uncertainty=0.7, importance=0.8)
    for w in chapter.get("warnings", []):
        queue.add(stable_id("warn", w[:40]), "section", reason=w,
                  evidence=w, action="inspect section", harm=0.4,
                  uncertainty=0.6, importance=0.6)
    timer.stop("validation")

    md = f"# {spec.get('title', 'Chapter')}\n\n" + chapter["text"]
    section_count = len(chapter["sections"])
    batch_dir = Path(cfg.work_dir) / time.strftime("chapter-%Y%m%d-%H%M%S")
    batch_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for fmt in formats:
        info = write_output(fmt, batch_dir / f"chapter.{fmt}", md)
        info.update(validate_output(batch_dir / f"chapter.{fmt}", fmt))
        written.append(info)
    (batch_dir / "provenance.json").write_text(
        __import__("json").dumps(
            {"sections": chapter["provenance"], "tier": entry.get("tier"),
             "n_ctx": cfg.effective_n_ctx(), "temperature": cfg.temperature,
             "seed": cfg.seed, "cloud": bool(cloud),
             "warnings": chapter["warnings"]}, indent=2, ensure_ascii=False),
        encoding="utf-8")
    import csv as _csv
    with (batch_dir / "review-queue.csv").open("w", encoding="utf-8", newline="") as fh:
        rows = queue.as_rows()
        w = _csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else
                            ["item_id", "reason"])
        w.writeheader()
        w.writerows(rows)
    import json as _json
    (batch_dir / "ssj-report.json").write_text(
        _json.dumps(ssj_rows, indent=2, ensure_ascii=False), encoding="utf-8")
    summary = {"ok": True, "batch_dir": str(batch_dir), "files": written,
               "sections": section_count,
               "ssj_tag": batch_tag, "review_queue": len(queue),
               "unsourced_claims": sum(len(u["claims"])
                                       for u in chapter.get("unsourced", [])),
               "warnings": chapter["warnings"], "gate": gate_msg}
    manager.unload()
    return summary


def run_book(cfg: Config, outline: dict[str, Any], *,
             model_ref: str | None = None, force: bool = False,
             cloud: bool = False) -> dict[str, Any]:
    from .output.writers import write_output, validate_output
    cfg.ensure_dirs()
    manager = ModelManager(cfg)
    try:
        client, entry, gate_msg = manager.task_client(
            "long_draft", model_ref, force=force, prefer_cloud=cloud)
    except ChatError as exc:
        return {"ok": False, "error": str(exc)}
    drafter = Drafter(client, entry, warnings=[gate_msg] if gate_msg else [])
    book = drafter.draft_book(outline)
    if not book.get("ok"):
        manager.unload()
        return {"ok": False, "error": "; ".join(book.get("warnings", [])) or "no content"}
    md = book["text"]
    bib = "\n".join(f"- {c}" for c in book["bibliography"]) or "- (none cited)"
    md += "\n\n## Bibliography\n\n" + bib + "\n"
    batch_dir = Path(cfg.work_dir) / time.strftime("book-%Y%m%d-%H%M%S")
    batch_dir.mkdir(parents=True, exist_ok=True)
    written = [write_output(f, batch_dir / f"book.{f}", md)
               for f in ("md", "txt", "pdf")]
    for w in written:
        w.update(validate_output(w["path"], w.get("format", "md")))
    import json as _json
    (batch_dir / "provenance.json").write_text(_json.dumps(
        {"provenance": book["provenance"], "bibliography": book["bibliography"],
         "structure_warnings": book["structure_warnings"],
         "tier": entry.get("tier"), "n_ctx": cfg.effective_n_ctx(),
         "temperature": cfg.temperature, "seed": cfg.seed,
         "cloud": bool(cloud)}, indent=2, ensure_ascii=False), encoding="utf-8")
    manager.unload()
    return {"ok": True, "batch_dir": str(batch_dir), "files": written,
            "chapters": len(book["chapters"]),
            "bibliography": len(book["bibliography"]),
            "structure_warnings": book["structure_warnings"],
            "warnings": book["warnings"], "gate": gate_msg}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _evidence_text(analysis: dict[str, Any], topic: str,
                   papers: list[dict]) -> str:
    tps = analysis["tps_per_topic"].get(topic)
    if not tps:
        return ("No historical papers supplied yet: questions must come from "
                "textbook knowledge only, and probability is not estimable.")
    prob = tps["occurrence_probability"]
    lines = [f"Topic: {topic}", f"TPS: {tps['tps']} (provisional={tps['provisional']})",
             f"Historical appearances over {prob.get('sample_size_papers', 0)} "
             f"paper(s), period {prob.get('period', 'unknown')}",
             f"Occurrence probability: {prob.get('label', 'Not reliably estimable')}"]
    hits = [r for r in papers if str(r.get("topic")) == topic][:10]
    if hits:
        lines.append("Question evidence (sample):")
        lines += [f"- [{r.get('year', '?')}] {str(r.get('text', ''))[:140]}"
                  for r in hits]
    return "\n".join(lines)


def _nm_note(advice) -> str:
    if advice.p is None:
        return ("p = unknown; no attempt advice issued (FR-NM-04). "
                f"Break-even accuracy would be "
                f"{advice.break_even_p * 100:.1f}% if R/W rules apply.")
    return (f"{advice.recommendation}: E(attempt)={advice.E_attempt:.3f} vs "
            f"E(skip)={advice.E_skip:.3f}; break-even p="
            f"{advice.break_even_p * 100:.1f}%")


def _mcq_flat(m: dict[str, Any]) -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for k, v in m.items():
        if isinstance(v, list):
            flat[k] = " | ".join(str(x) for x in v)
        elif isinstance(v, dict):
            flat[k] = __import__("json").dumps(v, ensure_ascii=False)
        else:
            flat[k] = v
    return flat


def _next_cycle(gen: dict, queue, analysis: dict, advice) -> str:
    lines = []
    if gen.get("shortfall"):
        lines.append(f"- Supply sources for {gen['shortfall']} missing MCQs "
                     "(do not fabricate to hit the target).")
    provisional = [t for t, d in analysis["tps_per_topic"].items()
                   if d.get("provisional")]
    if provisional:
        lines.append(f"- Add past-paper data for provisional topics: "
                     f"{', '.join(provisional[:8])}.")
    if len(queue):
        lines.append(f"- Clear the {len(queue)}-item review queue, starting "
                     "with the highest-priority items.")
    if advice.p is None:
        lines.append("- Supply candidate accuracy data (p) to enable "
                     "negative-marking advice (FR-NM-04).")
    lines.append("- Re-run this batch after corrections to compare metrics "
                 "(FR-MET-01).")
    return "\n".join(lines)
