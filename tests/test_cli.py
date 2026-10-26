"""CLI surface: init, doctor, config, negative, analyse, ingest, models, mcq.

Every command runs against an isolated NANDAKA_DIR (Termux behaviour), and the
CLI must never print a traceback: failures are reported and exit non-zero.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from nandaka.cli import build_parser, main
from tests.conftest import build_gguf, make_papers_csv


def run_cli(capsys, *argv: str) -> tuple[int, str]:
    """Run the CLI, tolerating SystemExit (argparse + failure paths)."""
    code = 0
    try:
        code = main(list(argv))
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    out = capsys.readouterr()
    return int(code or 0), out.out + out.err


def as_json(text: str) -> dict:
    start = text.find("{")
    assert start >= 0, f"no JSON in output: {text[:200]}"
    return json.loads(text[start:])


# ---------------- parser ---------------- #


def test_parser_exposes_all_srs_tasks():
    parser = build_parser()
    subs = parser._subparsers._group_actions[0].choices
    for task in ("init", "doctor", "config", "ingest", "sources", "analyse",
                 "negative", "models", "mcq", "draft", "batch", "bench"):
        assert task in subs, f"missing subcommand: {task}"


def test_no_args_prints_help(capsys):
    code, text = run_cli(capsys)
    assert code == 0
    assert "usage: nandaka" in text
    assert "offline-first" in text


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "nandaka" in capsys.readouterr().out


# ---------------- init / doctor / config ---------------- #


def test_init_creates_config_and_dirs(nandaka_home, capsys):
    code, text = run_cli(capsys, "init")
    assert code == 0
    cfg_path = Path(nandaka_home.nandaka_dir) / "config.toml"
    assert cfg_path.exists()
    content = cfg_path.read_text(encoding="utf-8")
    assert 'cloud_enabled = false' in content      # FR-CLOUD-01: opt-in only
    assert 'cloud_provider = "nvidia"' in content  # Sarvam removed
    assert "NVIDIA_API_KEY" in content
    assert "model dir" in text and "work dir" in text
    assert Path(nandaka_home.model_dir).exists()

    # idempotent: a second init does not rewrite the user's config
    stamp = cfg_path.stat().st_mtime_ns
    run_cli(capsys, "init")
    assert cfg_path.stat().st_mtime_ns == stamp


def test_doctor_reports_environment(nandaka_home, capsys):
    code, text = run_cli(capsys, "doctor")
    assert code == 0
    report = as_json(text)
    for key in ("profile", "python", "nandaka_dir", "model_dir",
                "llama_server", "cloud", "optional_deps", "offline_ready",
                "ollama"):
        assert key in report, f"doctor missing '{key}'"
    assert report["cloud"]["enabled"] is False
    assert report["cloud"]["nvidia_key"] == "missing"
    assert report["nandaka_dir"]["writable"] is True
    # never leak secrets (§3.6)
    assert "api_key" not in json.dumps(report).lower() or "***" in text
    # default backend: the endpoint is disclosed but not contacted (offline)
    assert report["ollama"]["in_use"] is False
    assert report["ollama"]["host"] == "http://127.0.0.1:11434"


def _ollama_stub(payload: dict):
    """Local Ollama /api/tags endpoint for doctor (FR-GGUF-13)."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    body = json.dumps(payload).encode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_doctor_probes_ollama_when_selected(nandaka_home, capsys, monkeypatch):
    """Selected backend -> endpoint is probed and readiness follows it."""
    srv, host = _ollama_stub({"models": [{"name": "llama3.2:1b"}]})
    try:
        monkeypatch.setenv("NANDAKA_BACKEND", "ollama")
        monkeypatch.setenv("NANDAKA_OLLAMA_HOST", host)
        from nandaka.config import get_config
        get_config(reload=True)
        code, text = run_cli(capsys, "doctor")
    finally:
        srv.shutdown()
        srv.server_close()
    assert code == 0
    report = as_json(text)
    assert report["ollama"]["in_use"] is True
    assert report["ollama"]["reachable"] is True
    assert report["ollama"]["models"] == ["llama3.2:1b"]
    # no local binary is needed when Ollama holds the weights
    assert report["llama_server"] == "not used (backend = ollama)"
    assert report["offline_ready"] is True


def test_doctor_ollama_down_is_not_ready(nandaka_home, capsys, monkeypatch):
    """A dead LAN endpoint degrades: reported, exit 0, never a traceback."""
    monkeypatch.setenv("NANDAKA_BACKEND", "ollama")
    monkeypatch.setenv("NANDAKA_OLLAMA_HOST", "http://127.0.0.1:1")
    from nandaka.config import get_config
    get_config(reload=True)
    code, text = run_cli(capsys, "doctor")
    assert code == 0
    report = as_json(text)
    assert report["ollama"]["reachable"] is False
    assert report["ollama"]["reason"]
    assert report["offline_ready"] is False


def test_init_writes_ollama_config(nandaka_home, capsys, monkeypatch):
    """`init` bakes the env-selected backend into config.toml (Termux flow)."""
    monkeypatch.setenv("NANDAKA_BACKEND", "ollama")
    monkeypatch.setenv("NANDAKA_OLLAMA_HOST", "http://192.168.1.24:11434")
    from nandaka.config import get_config
    get_config(reload=True)
    code, text = run_cli(capsys, "init")
    assert code == 0
    content = (Path(nandaka_home.nandaka_dir) / "config.toml").read_text(
        encoding="utf-8")
    assert 'backend = "ollama"' in content
    assert 'ollama_host = "http://192.168.1.24:11434"' in content
    # no confusing "install llama.cpp" advice when Ollama is the backend,
    # and no advice to drop a .gguf either (this backend uses tags)
    assert "llama-server not on PATH" not in text
    assert "drop a .gguf" not in text
    assert "ollama @ http://192.168.1.24:11434" in text


def test_config_show_masks_secrets(nandaka_home, capsys, monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-super-secret-value")
    monkeypatch.setenv("NANDAKA_CLOUD", "1")
    from nandaka.config import get_config
    get_config(reload=True)
    code, text = run_cli(capsys, "config")
    assert code == 0
    assert "super-secret-value" not in text
    assert "nvapi" not in text
    assert "cloud_enabled" in text
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.delenv("NANDAKA_CLOUD", raising=False)
    get_config(reload=True)


# ---------------- analysis commands ---------------- #


def test_negative_marking_maths(nandaka_home, capsys):
    """TC-16: E = p*R - (1-p)*W with p=0.6, R=1, W=0.33 -> 0.468."""
    code, text = run_cli(capsys, "negative", "--R", "1", "--W", "0.33",
                         "--U", "0", "--p", "0.6")
    assert code == 0
    res = as_json(text)
    assert res["recommendation"] == "ATTEMPT"
    assert abs(res["E_attempt"] - 0.468) < 1e-9
    # break-even p = W/(R+W) = 0.33/1.33 -> 24.8%
    assert 0 < res["break_even_p_pct"] < 100
    assert abs(res["break_even_p_pct"] - 24.8) < 0.1


def test_negative_unknown_p_declines_advice(nandaka_home, capsys):
    code, text = run_cli(capsys, "negative", "--R", "1", "--W", "0.5",
                         "--p", "unknown")
    assert code == 0
    res = as_json(text)
    assert res["E_attempt"] is None
    assert "INSUFFICIENT" in res["recommendation"]


def test_negative_free_text_scheme(nandaka_home, capsys):
    code, text = run_cli(capsys, "negative",
                         "--scheme", "2 marks for correct, 0.5 negative for wrong")
    assert code == 0
    res = as_json(text)
    assert res["R"] == 2.0 and res["W"] == 0.5


def test_analyse_with_papers(nandaka_home, tmp_path, capsys):
    papers = make_papers_csv(tmp_path / "past.csv")
    code, text = run_cli(capsys, "analyse", "--papers", str(papers))
    assert code == 0
    res = as_json(text)
    assert res["papers"] > 0
    assert res["years"] == [2018, 2024]
    topics = [r["topic"] for r in res["tps"]]
    assert "Constitution" in topics and "Rare Topic" in topics
    assert res["rules"]  # FR-HA-03: scoring rules disclosed


def test_analyse_without_papers_is_honest(nandaka_home, capsys):
    code, text = run_cli(capsys, "analyse")
    assert code == 0
    res = as_json(text)
    assert res["papers"] == 0 and res["tps"] == []


# ---------------- ingest / sources ---------------- #


def test_ingest_inventory(nandaka_home, tmp_path, capsys):
    src = tmp_path / "chapter.md"
    src.write_text("# Title\n\nSome source content for the inventory.\n",
                   encoding="utf-8")
    out = tmp_path / "inv.json"
    code, text = run_cli(capsys, "ingest", str(src), "--out", str(out))
    assert code == 0
    assert out.exists()
    inv = json.loads(out.read_text(encoding="utf-8"))
    assert inv["total"] == 1
    assert inv["sources"][0]["chars"] > 0
    assert inv["sources"][0]["kind"] == "text"


def test_ingest_missing_file_reports_gap(nandaka_home, capsys):
    code, text = run_cli(capsys, "ingest", "/nonexistent/file.md")
    assert code == 0
    inv = as_json(text)
    assert inv["gaps"] or inv["sources"][0]["notes"]


def test_sources_list_offline(capsys):
    code, text = run_cli(capsys, "sources", "list")
    assert code == 0
    portals = as_json(text)["portals"]
    for key in ("ncert", "diksha", "ap_scert", "tg_scert"):
        assert key in portals


def test_sources_fetch_offline_uses_cache(nandaka_home, capsys):
    code, text = run_cli(capsys, "sources", "fetch", "ncert", "--offline")
    assert code == 0
    res = as_json(text)
    assert res["portal"] == "ncert"
    # offline: either cached page or an honest note about the miss
    assert res["ok"] or res["notes"]


# ---------------- models ---------------- #


def test_models_list_empty(nandaka_home, capsys):
    code, text = run_cli(capsys, "models", "list")
    assert code == 0
    assert "(none" in text or "0" in text


def test_models_scan_discovers_gguf(nandaka_home, capsys):
    build_gguf(Path(nandaka_home.model_dir) / "demo-model.gguf",
               params=1_500_000_000)
    code, text = run_cli(capsys, "models", "scan")
    assert code == 0
    res = as_json(text)
    assert res["scanned"] == 1
    assert res["models"][0]["tier"] == "T1"
    assert res["models"][0]["valid"] is True

    code, text = run_cli(capsys, "models", "list")
    assert code == 0
    assert "T1" in text and "demo-model" in text and "unprobed" in text


def test_models_verify_and_probe_graceful(nandaka_home, capsys):
    p = build_gguf(Path(nandaka_home.model_dir) / "v.gguf",
                   params=700_000_000)
    code, text = run_cli(capsys, "models", "verify", "--model", str(p))
    assert code == 0 and as_json(text)["valid"] is True

    # probe needs llama-server; without it the CLI must report, not crash
    code, text = run_cli(capsys, "models", "probe", "--model", str(p))
    assert code in (0, 1)
    assert "Traceback" not in text


# ---------------- mcq / batch failure paths ---------------- #


def test_mcq_without_model_exits_nonzero(nandaka_home, capsys):
    code, text = run_cli(capsys, "mcq", "--topic", "Constitution",
                         "--count", "3", "--offline")
    assert code == 1
    assert "FAILED" in text
    assert "no GGUF model selected" in text
    assert "Traceback" not in text


def test_batch_requires_spec(nandaka_home, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["batch"])
    assert exc.value.code == 2  # argparse usage error, not a crash


def test_error_paths_never_traceback(nandaka_home, capsys, monkeypatch):
    """FR-UX: unexpected exceptions are summarised, with NANDAKA_DEBUG hint."""
    import nandaka.cli as cli
    monkeypatch.setattr(
        cli, "cmd_doctor",
        lambda args: (_ for _ in ()).throw(RuntimeError("boom")))
    code, text = run_cli(capsys, "doctor")
    assert code == 1
    assert "RuntimeError: boom" in text
    assert "NANDAKA_DEBUG" in text
    assert "Traceback" not in text


def test_mcq_spec_file(nandaka_home, tmp_path, capsys):
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps({
        "topic": "Economy", "exam": "Group-2", "count": 2}), encoding="utf-8")
    code, text = run_cli(capsys, "mcq", "--spec", str(spec_path),
                         "--offline", "--count", "2")
    # still no model -> graceful failure, never a traceback
    assert code == 1
    assert "Traceback" not in text
    assert "FAILED" in text
