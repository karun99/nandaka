"""Nandaka CLI (EX-16): ``nandaka <task> --model <gguf> --input <file>
--format <fmt> --images auto|off [--cloud] [--force] [--offline]``.

Every subcommand prints machine-readable JSON with --json for scripting.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .config import Config, get_config, is_termux, machine_profile

# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #


def _dump(data: Any, as_json: bool) -> None:
    if as_json or isinstance(data, (dict, list)):
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    else:
        print(data)


def _load_spec(args) -> dict[str, Any]:
    spec: dict[str, Any] = {}
    if getattr(args, "spec", None):
        p = Path(args.spec)
        if not p.exists():
            sys.exit(f"spec file not found: {p}")
        spec = json.loads(p.read_text(encoding="utf-8"))
    for key in ("topic", "exam", "subject", "class", "title", "papers_csv",
                "syllabus", "portal"):
        val = getattr(args, key.replace("-", "_"), None)
        if val:
            spec[key] = val
    if getattr(args, "input", None):
        spec["inputs"] = list(args.input)
    if getattr(args, "images", None):
        spec["images"] = args.images
    if getattr(args, "marking", None):
        try:
            spec["marking"] = json.loads(args.marking)
        except json.JSONDecodeError:
            sys.exit("--marking must be JSON, e.g. '{\"R\":1,\"W\":0.33,\"U\":0}'")
    if getattr(args, "p", None) is not None:
        spec["p_candidate"] = args.p
    if getattr(args, "image_file", None):
        spec.setdefault("image_files", []).extend(args.image_file)
    return spec


def _print_summary(summary: dict[str, Any]) -> None:
    if not summary.get("ok", True):
        print(f"[nandaka] FAILED: {summary.get('error', 'unknown error')}")
        if summary.get("hint"):
            print(f"[nandaka] hint: {summary['hint']}")
        raise SystemExit(1)
    print(f"[nandaka] batch: {summary.get('batch_dir', '')}")
    for f in summary.get("files", []):
        mark = "ok " if f.get("ok") else "BAD"
        print(f"  [{mark}] {Path(f.get('path', '')).name:<20} {f.get('detail', '')}")
    if summary.get("ssj_tag"):
        print(f"  SSJ: {summary['ssj_tag']}")
    if summary.get("review_queue"):
        print(f"  review queue: {summary['review_queue']} item(s)")
    for w in summary.get("warnings", [])[:5]:
        print(f"  warning: {w}")


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #


def cmd_init(args) -> None:
    cfg = get_config(args.config)
    cfg.ensure_dirs()
    cfg_path = Path(cfg.nandaka_dir) / "config.toml"
    if not cfg_path.exists():
        cfg_path.write_text(f"""# Nandaka configuration (flat TOML)
# Termux: set NANDAKA_DIR to shared storage, e.g.
#   export NANDAKA_DIR=$HOME/storage/shared/nandaka

nandaka_dir = "{cfg.nandaka_dir}"
model_dir = "{cfg.model_dir}"
work_dir = "{cfg.work_dir}"
ram_budget_mb = {cfg.ram_budget_mb}          # CO-01: peak RSS budget
backend = "{cfg.backend}"          # llama_cpp | ollama (FR-GGUF-13)
llama_server_bin = "{cfg.llama_server_bin}"
llama_server_port = {cfg.llama_server_port}
temperature = {cfg.temperature}
seed = {cfg.seed}

# Cloud fallback — disabled by default (FR-CLOUD-01). Sarvam removed;
# NVIDIA NIM + OpenRouter only. Keys via env NVIDIA_API_KEY / OPENROUTER_API_KEY.
cloud_enabled = false
cloud_provider = "nvidia"          # nvidia | openrouter
cloud_model = ""
cloud_token_budget = {cfg.cloud_token_budget}
""", encoding="utf-8")
        print(f"[nandaka] wrote {cfg_path}")
    print(f"[nandaka] model dir : {cfg.model_dir}")
    print(f"[nandaka] work dir  : {cfg.work_dir}")
    print(f"[nandaka] termux    : {is_termux()}")
    print("[nandaka] next: drop a .gguf into the model dir, then run "
          "`nandaka models scan`")
    if shutil.which("llama-server") is None:
        print("[nandaka] note: llama-server not on PATH. Install llama.cpp "
              "(Termux: `pkg install llama.cpp`; Linux: build from source) "
              "or set NANDAKA_LLAMA_SERVER.")


def cmd_doctor(args) -> None:
    cfg = get_config(args.config)
    cfg.ensure_dirs()
    from .models.server import find_server_binary
    checks = {
        "profile": machine_profile(),
        "python": sys.version.split()[0],
        "nandaka_dir": {"path": cfg.nandaka_dir,
                        "writable": os.access(cfg.nandaka_dir, os.W_OK)},
        "model_dir": {"path": cfg.model_dir,
                      "exists": Path(cfg.model_dir).exists(),
                      "gguf_count": len(list(Path(cfg.model_dir).glob("**/*.gguf")))
                      if Path(cfg.model_dir).exists() else 0},
        "llama_server": find_server_binary(cfg) or "NOT FOUND — see install notes",
        "ram_budget_mb": cfg.ram_budget_mb,
        "n_ctx": cfg.effective_n_ctx(),
        "backend": cfg.backend,
        "cloud": {
            "enabled": cfg.cloud_enabled,
            "provider": cfg.cloud_provider,
            "nvidia_key": "set" if cfg.nvidia_api_key else "missing",
            "openrouter_key": "set" if cfg.openrouter_api_key else "missing",
        },
        "optional_deps": {},
    }
    for mod in ("pypdf", "openpyxl", "fpdf"):
        try:
            __import__(mod)
            checks["optional_deps"][mod] = "installed"
        except ImportError:
            checks["optional_deps"][mod] = "absent (optional)"
    from .output.pdfwriter import _font_search_paths, pick_unicode_font
    font = pick_unicode_font("A క")
    checks["fonts"] = {
        "candidates": len(_font_search_paths()),
        "selected": str(font.path) if font else "none — Latin-only PDF fallback",
        "telugu_capable": bool(font and font.has("క")),
        "latin_fallback": "Helvetica (built in; always available)",
    }
    checks["offline_ready"] = bool(
        find_server_binary(cfg)
        and next(Path(cfg.model_dir).glob("*.gguf"), None))
    _dump(checks, True)


def cmd_config_show(args) -> None:
    _dump(get_config(args.config).summary(), True)


def cmd_ingest(args) -> None:
    from .ingest import ingest, inventory
    docs = ingest(args.files)
    inv = inventory(docs)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(inv, indent=2, ensure_ascii=False),
                                  encoding="utf-8")
        print(f"[nandaka] inventory written to {args.out}")
    _dump(inv, True)


def cmd_sources(args) -> None:
    from .sources import PORTALS, allowed_by_robots, enumerate_portal, extract_links, known_urls
    cfg = get_config(args.config)
    if args.action == "list":
        _dump({"portals": known_urls()}, True)
        return
    res = enumerate_portal(args.portal, cfg, allow_network=not args.offline)
    out: dict[str, Any] = {"portal": args.portal, "url": res.url,
                           "ok": res.ok, "from_cache": res.from_cache,
                           "status": res.status, "notes": res.notes}
    if res.ok and args.links:
        pattern = PORTALS[args.portal].link_pattern
        links = extract_links(res.text, res.url, pattern)
        out["links"] = links[: args.limit]
        out["link_count"] = len(links)
    _dump(out, True)


def cmd_analyse(args) -> None:
    from .analysis.frequency import compute_stats, diagram_frequency, paper_set, question_years
    from .analysis.tps import RULES_DOC, compute_tps
    import csv as _csv
    papers = []
    if args.papers and Path(args.papers).exists():
        with Path(args.papers).open("r", encoding="utf-8", newline="") as fh:
            papers = [dict(r) for r in _csv.DictReader(fh)]
    stats = compute_stats(papers)
    years = question_years(papers)
    total_papers = len(paper_set(papers))
    rows = []
    for topic, st in sorted(stats.items(), key=lambda kv: -kv[1].count):
        res = compute_tps(st, all_stats=stats, syllabus_relevance=None,
                          years_seen=years, n_papers_total=total_papers)
        rows.append(res.as_dict())
    _dump({"papers": len(papers), "years": [min(years), max(years)] if years else [],
           "diagram_frequency": diagram_frequency(papers),
           "rules": RULES_DOC, "tps": rows}, True)


def cmd_negative(args) -> None:
    from .analysis.negative_marking import MarkingScheme, advise, parse_marking_text
    if args.scheme:
        scheme = parse_marking_text(args.scheme) or MarkingScheme(source=args.scheme)
    else:
        scheme = MarkingScheme(R=args.R, W=args.W, U=args.U,
                               source="cli" if args.W or args.U else "")
    p = None if args.p in (None, "unknown") else float(args.p)
    res = advise(scheme, p)
    _dump(res.as_dict(), True)


def cmd_models(args) -> int:
    cfg = get_config(args.config)
    cfg.ensure_dirs()
    from .models.registry import FEATURE_MATRIX, ModelRegistry, TIER_ORDER
    reg = ModelRegistry(cfg.model_dir, cfg.registry_path)
    if args.action == "scan":
        entries = reg.scan()
        _dump({"scanned": len(entries),
               "models": [{k: e.get(k) for k in
                           ("path", "name", "architecture", "parameters",
                            "param_source", "tier", "quant", "context_length",
                            "valid", "error")} for e in entries]}, True)
        return 0
    if args.action == "verify":
        if not args.model:
            sys.exit("models verify requires --model <path>")
        _dump(reg.verify(args.model), True)
        return 0
    if args.action == "probe":
        from .models.server import ModelManager
        mgr = ModelManager(cfg)
        try:
            result = mgr.probe(args.model, force=args.force)
        except Exception as exc:  # probe must not crash the CLI
            _dump({"ok": False, "error": str(exc)}, True)
            return 1
        _dump({"ok": result.get("ok"), "tier": result.get("tier"),
               "results": result.get("results")}, True)
        return 0 if result.get("ok") else 1
    # default: list
    entries = reg.scan()
    rows = [{k: e.get(k) for k in ("name", "tier", "quant", "parameters",
                                   "param_source", "context_length",
                                   "has_chat_template", "size_bytes", "path")}
            for e in sorted(entries, key=lambda e: (TIER_ORDER.get(e.get("tier", "T0"), 0),
                                                    e.get("name", "")))]
    if args.json:
        _dump({"model_dir": cfg.model_dir, "tiers": FEATURE_MATRIX,
               "models": rows}, True)
    else:
        print(f"models in {cfg.model_dir}: {len(rows)}")
        for r in rows:
            probe = reg.get(r["path"]) or {}
            probe_state = probe.get("probe")
            flag = ("probed:ok" if (probe_state or {}).get("ok")
                    else "probed:fail" if probe_state else "unprobed")
            print(f"  {r['tier']}  {r['quant']:<7} "
                  f"{(r['parameters'] or 0) / 1e9:>6.2f}B  {r['name']:<28} "
                  f"ctx={r['context_length'] or '?':<7} {flag}  {r['path']}")
        if not rows:
            print("  (none — drop any .gguf file here; no code changes needed)")
    return 0


def cmd_mcq(args) -> None:
    from .pipeline import run_mcq_batch
    cfg = get_config(args.config)
    if args.ram_budget:
        cfg.ram_budget_mb = args.ram_budget
        cfg._resolve_paths()
    spec = _load_spec(args)
    if not spec.get("topic") and not spec.get("exam"):
        print("[nandaka] warning: no --topic/--exam given; using 'General'")
    summary = run_mcq_batch(
        cfg, spec, model_ref=args.model, count=args.count,
        papers_csv=args.papers or spec.get("papers_csv"),
        cloud=args.cloud, force=args.force, offline=args.offline,
        images=args.images)
    _print_summary(summary)


def cmd_draft(args) -> None:
    from .pipeline import run_book, run_chapter
    cfg = get_config(args.config)
    if args.ram_budget:
        cfg.ram_budget_mb = args.ram_budget
        cfg._resolve_paths()
    if args.mode == "chapter":
        spec = _load_spec(args)
        spec.setdefault("target_words", args.words)
        if args.outcomes:
            spec["outcomes"] = [o.strip() for o in args.outcomes.split(";") if o.strip()]
        summary = run_chapter(cfg, spec, model_ref=args.model,
                              force=args.force, cloud=args.cloud,
                              formats=tuple(args.format.split(",")))
    else:
        outline = json.loads(Path(args.outline).read_text(encoding="utf-8")) \
            if args.outline else {"title": args.title or "Untitled",
                                  "chapters": []}
        summary = run_book(cfg, outline, model_ref=args.model,
                           force=args.force, cloud=args.cloud)
    _print_summary(summary)


def cmd_batch(args) -> None:
    """Full FR-WF-01 pipeline from a spec file."""
    cmd_mcq(args)


def cmd_bench(args) -> None:
    from .models.server import ModelManager
    cfg = get_config(args.config)
    if args.ram_budget:
        cfg.ram_budget_mb = args.ram_budget
    cfg.ensure_dirs()
    mgr = ModelManager(cfg)
    result: dict[str, Any] = {"budget_mb": cfg.ram_budget_mb,
                              "n_ctx": cfg.effective_n_ctx()}
    try:
        client = mgr.client(args.model)
        import time as _t
        t0 = _t.time()
        text = client.text([{"role": "user", "content": "Say OK."}],
                           max_tokens=8, use_cache=False)
        result["first_token_latency_s"] = round(_t.time() - t0, 2)
        result["reply"] = text.strip()[:40]
        result["rss"] = mgr.ram_report()
        result["within_budget"] = result["rss"].get("rss_mb", 0) <= cfg.ram_budget_mb
    except Exception as exc:
        result["error"] = str(exc)
    finally:
        mgr.unload()
    _dump(result, True)


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="nandaka",
        description="Nandaka — offline-first, GGUF-agnostic academic content "
                    "and exam-preparation workbench (SSJ-guarded).",
        epilog="Examples:\n"
               "  nandaka init\n"
               "  nandaka models scan\n"
               "  nandaka mcq --topic 'Constitution' --count 15 --papers past.csv\n"
               "  nandaka draft chapter --class 8 --subject 'Civics' "
               "--title 'Fundamental Rights'\n"
               "  nandaka negative --R 1 --W 0.33 --U 0 --p 0.6\n",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"nandaka {__version__}")
    ap.add_argument("--config", help="path to config.toml")
    ap.add_argument("--json", action="store_true", help="force JSON output")
    sub = ap.add_subparsers(dest="task")

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--model", help="path or name of a GGUF file")
        p.add_argument("--cloud", action="store_true",
                       help="explicitly allow cloud fallback (NVIDIA/OpenRouter)")
        p.add_argument("--force", action="store_true",
                       help="attempt tasks above the loaded model's tier")
        p.add_argument("--ram-budget", type=int, metavar="MB",
                       help="override RAM budget (CO-01)")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("init", help="create directories and a config template")
    p.add_argument("--config", help=argparse.SUPPRESS)
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("doctor", help="diagnose the environment (Termux-aware)")
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("config", help="show effective configuration")
    p.add_argument("--config", help=argparse.SUPPRESS)
    p.set_defaults(fn=cmd_config_show)

    p = sub.add_parser("ingest", help="read inputs and print a source inventory")
    p.add_argument("files", nargs="+")
    p.add_argument("--out", help="write inventory JSON here")
    p.set_defaults(fn=cmd_ingest)

    p = sub.add_parser("sources", help="list/fetch official portals (EX-03..09)")
    p.add_argument("action", choices=["list", "fetch"])
    p.add_argument("portal", nargs="?", default="ncert",
                   help="ncert|diksha|ap_scert|tg_scert|yesonlinebooks")
    p.add_argument("--offline", action="store_true", help="cache only")
    p.add_argument("--links", action="store_true", help="extract links")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--config", help=argparse.SUPPRESS)
    p.set_defaults(fn=cmd_sources)

    p = sub.add_parser("analyse", help="historical frequency + TPS (FR-HA)")
    p.add_argument("--papers", help="CSV of past papers")
    p.set_defaults(fn=cmd_analyse)

    p = sub.add_parser("negative", help="negative-marking maths (FR-NM)")
    p.add_argument("--R", type=float, default=1.0, help="reward per correct")
    p.add_argument("--W", type=float, default=0.0, help="penalty per wrong")
    p.add_argument("--U", type=float, default=0.0, help="credit if unattempted")
    p.add_argument("--p", default=None, help="candidate accuracy or 'unknown'")
    p.add_argument("--scheme", help="free-text marking rules")
    p.set_defaults(fn=cmd_negative)

    p = sub.add_parser("models", help="manage GGUF models (FR-GGUF)")
    p.add_argument("action", choices=["list", "scan", "verify", "probe"],
                   nargs="?", default="list")
    p.add_argument("--model", help="GGUF path (verify/probe)")
    p.add_argument("--force", action="store_true", help="re-run probe")
    p.add_argument("--json", action="store_true")
    p.add_argument("--config", help=argparse.SUPPRESS)
    p.set_defaults(fn=cmd_models)

    p = sub.add_parser("mcq", help="generate a bilingual MCQ batch (FR-GOV)")
    p.add_argument("--spec", help="JSON spec file")
    p.add_argument("--topic")
    p.add_argument("--exam")
    p.add_argument("--subject")
    p.add_argument("--papers", help="CSV of past papers for frequency/TPS")
    p.add_argument("--papers_csv", help=argparse.SUPPRESS)
    p.add_argument("--count", type=int, default=30, help="target MCQ count")
    p.add_argument("--input", nargs="*", help="source files (md/txt/pdf/csv/xlsx)")
    p.add_argument("--format", default="md", help=argparse.SUPPRESS)
    p.add_argument("--images", choices=["auto", "off"], default="auto")
    p.add_argument("--image-file", action="append",
                   help="adopt an image file (source-derived)")
    p.add_argument("--marking", help='JSON: {"R":1,"W":0.33,"U":0}')
    p.add_argument("--p", dest="p", help="candidate accuracy for advice")
    p.add_argument("--portal", choices=["ncert", "diksha", "ap_scert",
                                        "tg_scert", "yesonlinebooks"])
    p.add_argument("--offline", action="store_true", help="no network at all")
    p.add_argument("--config", help=argparse.SUPPRESS)
    common(p)
    p.set_defaults(fn=cmd_mcq)

    p = sub.add_parser("draft", help="school/academic drafting (FR-SB/FR-AB)")
    p.add_argument("mode", choices=["chapter", "book"])
    p.add_argument("--spec", help="JSON spec (chapter)")
    p.add_argument("--outline", help="JSON outline (book)")
    p.add_argument("--title")
    p.add_argument("--class", dest="class_", help="class (e.g. 8)")
    p.add_argument("--subject")
    p.add_argument("--words", type=int, default=1500)
    p.add_argument("--outcomes", help="semicolon-separated learning outcomes")
    p.add_argument("--input", nargs="*", help="source files")
    p.add_argument("--format", default="md,pdf", help="comma list: md,txt,pdf")
    p.add_argument("--images", choices=["auto", "off"], default="off")
    p.add_argument("--offline", action="store_true")
    p.add_argument("--config", help=argparse.SUPPRESS)
    common(p)
    p.set_defaults(fn=cmd_draft)

    p = sub.add_parser("batch", help="full pipeline from a spec (FR-WF-01)")
    p.add_argument("--spec", required=True)
    p.add_argument("--count", type=int, default=30)
    p.add_argument("--papers", default=None)
    p.add_argument("--input", nargs="*")
    p.add_argument("--topic")
    p.add_argument("--exam")
    p.add_argument("--subject")
    p.add_argument("--papers_csv", help=argparse.SUPPRESS)
    p.add_argument("--images", choices=["auto", "off"], default="auto")
    p.add_argument("--marking", default=None)
    p.add_argument("--p", dest="p", default=None)
    p.add_argument("--image-file", action="append")
    p.add_argument("--portal", default=None)
    p.add_argument("--offline", action="store_true")
    p.add_argument("--format", default="md", help=argparse.SUPPRESS)
    p.add_argument("--config", help=argparse.SUPPRESS)
    common(p)
    p.set_defaults(fn=cmd_batch)

    p = sub.add_parser("bench", help="run a probe and report RSS (NFR-P)")
    p.add_argument("--config", help=argparse.SUPPRESS)
    common(p)
    p.set_defaults(fn=cmd_bench)

    return ap


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "task", None):
        parser.print_help()
        return 0
    try:
        result = args.fn(args)
        return int(result) if isinstance(result, int) else 0
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("\n[nandaka] interrupted")
        return 130
    except Exception as exc:  # never traceback at users; keep CLI honest
        if os.environ.get("NANDAKA_DEBUG"):
            raise
        print(f"[nandaka] error: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("[nandaka] set NANDAKA_DEBUG=1 for a full traceback", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
