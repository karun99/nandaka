"""llama-server lifecycle, probe, ollama dialect, and download resume.

Covers the requirement groups that had no automated coverage in the baseline
suite: FR-GGUF-06 (warm reuse), FR-GGUF-09 (capability probe), FR-GGUF-13
(Ollama backend), FR-IN-05 (resumable single-connection downloads), and
NFR-P-07/08 (swap timing + RSS reporting).

Everything runs against local fakes: a stub ``llama-server`` executable and a
local ``http.server``. No network and no real model is required.
"""

from __future__ import annotations

import json
import os
import shlex
import stat
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from nandaka.models.client import ChatError
from nandaka.models.registry import ModelRegistry
from nandaka.models.server import (LlamaServer, ModelManager, PROBE_PROMPTS,
                                   find_server_binary)
from tests.conftest import build_gguf


# --------------------------------------------------------------------------- #
# Local llama-server fake
# --------------------------------------------------------------------------- #


class _LlamaStub:
    """Serves /health and the OpenAI dialect, like the real llama-server."""

    def __init__(self, *, reply: str = "OK", fail_after: int | None = None):
        self.reply = reply
        self.hits = 0
        self.fail_after = fail_after
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def _handler(self):
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path == "/health":
                    self.send_response(200)
                    body = b'{"status": "ok"}'
                else:
                    self.send_response(404)
                    body = b"{}"
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                stub.hits += 1
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
                if stub.fail_after is not None and stub.hits > stub.fail_after:
                    body = json.dumps({"error": "model overloaded"}).encode()
                    self.send_response(503)
                else:
                    body = json.dumps({
                        "choices": [{"message": {"content": stub.reply}}],
                        "usage": {"prompt_tokens": 4, "completion_tokens": 4,
                                  "total_tokens": 8},
                    }).encode()
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _write_stub_binary(path: Path, *, exit_immediately: bool = False,
                       stub: "_LlamaStub | None" = None) -> Path:
    """A fake llama-server binary.

    With *stub*, it proxies every request to that already-listening server and
    then sleeps, so ``LlamaServer`` sees a live child process bound to the
    configured port. Without one it just sleeps, so ``/health`` times out.
    """
    if exit_immediately:
        body = "#!/bin/sh\nexit 3\n"
    elif stub is not None:
        body = (f"#!/bin/sh\n"
                f"exec {shlex.quote(sys.executable)} -m http.server "
                f"{stub.port} --bind 127.0.0.1 >/dev/null 2>&1\n")
    else:
        body = "#!/bin/sh\nexec sleep 60\n"
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    return path


@pytest.fixture()
def stub():
    s = _LlamaStub()
    yield s
    s.close()


# --------------------------------------------------------------------------- #
# find_server_binary (Termux-aware discovery)
# --------------------------------------------------------------------------- #


def test_find_server_binary_env_override(nandaka_home, monkeypatch, tmp_path):
    fake = tmp_path / "my-server"
    fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("NANDAKA_LLAMA_SERVER", str(fake))
    assert find_server_binary(nandaka_home) == str(fake)


def test_find_server_binary_env_missing_is_falsy(nandaka_home, monkeypatch):
    """A bad override must fall back to PATH search, not raise (FR-IN-06)."""
    monkeypatch.setenv("NANDAKA_LLAMA_SERVER", "/nonexistent/llama-server")
    assert find_server_binary(nandaka_home) is None or \
        find_server_binary(nandaka_home).endswith("llama-server")


def test_termux_candidate_prefers_prefix(nandaka_home, monkeypatch):
    """On Termux the PREFIX bin dir is searched first (docs/termux.md)."""
    monkeypatch.setenv("PREFIX", "/data/data/com.termux/files/usr")
    from nandaka.models import server as srv
    assert srv._SERVER_CANDIDATES[0].startswith(
        "/data/data/com.termux/files/usr/bin")


# --------------------------------------------------------------------------- #
# LlamaServer: start / health / stop (EX-10)
# --------------------------------------------------------------------------- #


def test_server_start_without_binary_reports_actionable_error(nandaka_home,
                                                              monkeypatch):
    monkeypatch.delenv("NANDAKA_LLAMA_SERVER", raising=False)
    monkeypatch.setattr("nandaka.models.server._SERVER_CANDIDATES", [])
    srv = LlamaServer(nandaka_home)
    assert srv.start("model.gguf", wait_s=1) is False
    assert "llama.cpp" in srv.last_error
    assert "Termux" in srv.last_error          # platform-specific hint kept
    assert "NANDAKA_LLAMA_SERVER" in srv.last_error
    assert srv.running is False


def test_server_reports_exited_process(nandaka_home, stub, tmp_path,
                                       monkeypatch):
    """A model that kills the server at boot must be reported, not hung on."""
    bad = _write_stub_binary(tmp_path / "llama-server", exit_immediately=True)
    monkeypatch.setenv("NANDAKA_LLAMA_SERVER", str(bad))
    srv = LlamaServer(nandaka_home)
    assert srv.start("m.gguf", wait_s=5) is False
    assert "exited during startup" in srv.last_error
    assert srv.running is False


def test_server_health_reflects_state(nandaka_home, stub):
    srv = LlamaServer(nandaka_home)
    assert srv.health(timeout=0.3) is False    # nothing listening yet
    srv.port = stub.port
    assert srv.health(timeout=1.0) is True


def test_server_stop_is_idempotent(nandaka_home):
    srv = LlamaServer(nandaka_home)
    srv.stop()
    srv.stop()                                  # must not raise
    assert srv.proc is None and srv.model_path is None


# --------------------------------------------------------------------------- #
# FR-GGUF-06: warm-process reuse
# --------------------------------------------------------------------------- #


def test_warm_process_reused_for_same_model(nandaka_home, monkeypatch):
    """Same model + healthy process => no relaunch (FR-GGUF-06, NFR-P-07)."""
    launches = {"n": 0}

    class FakeProc:
        pid = os.getpid()

        def poll(self):
            return None

        def terminate(self):
            launches["stopped"] = launches.get("stopped", 0) + 1

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr("nandaka.models.server.subprocess.Popen",
                        lambda *a, **k: launches.__setitem__("n", launches["n"] + 1)
                        or FakeProc())
    monkeypatch.setenv("NANDAKA_LLAMA_SERVER", "/bin/true")   # discovery only
    # health is stubbed so this exercises launch bookkeeping, not TCP binding
    monkeypatch.setattr(LlamaServer, "health", lambda self, timeout=1.0: True)

    srv = LlamaServer(nandaka_home)
    assert srv.start("m.gguf", wait_s=5) is True
    assert launches["n"] == 1
    assert srv.running and srv.model_path == "m.gguf"

    # second start for the SAME model: reused, no second launch
    assert srv.start("m.gguf", wait_s=5) is True
    assert launches["n"] == 1, "warm process must be reused (FR-GGUF-06)"

    # switching model must stop the old one first (peak RSS guard, CO-01)
    assert srv.start("other.gguf", wait_s=5) is True
    assert launches["n"] == 2
    assert launches.get("stopped") == 1, "old model must be stopped before swap"
    srv.stop()


# --------------------------------------------------------------------------- #
# FR-GGUF-09: capability probe
# --------------------------------------------------------------------------- #


def test_probe_records_results_and_persists(nandaka_home, model_dir, stub,
                                            monkeypatch):
    build_gguf(model_dir / "probe-model.gguf", params=1_500_000_000)
    monkeypatch.setenv("NANDAKA_LLAMA_SERVER", "/bin/true")
    monkeypatch.setattr(nandaka_home, "llama_server_port", stub.port)

    mgr = ModelManager(nandaka_home)
    # bypass process launch: hand the manager a live client against the stub
    from nandaka.models.client import ChatClient
    live = ChatClient(base_url=f"http://127.0.0.1:{stub.port}/v1",
                      model="local", api_key="", provider="local",
                      temperature=0.4, seed=42, cache_dir=None)
    monkeypatch.setattr(mgr, "client", lambda ref=None, n_ctx=None: live)

    outcome = mgr.probe("probe-model.gguf")
    assert outcome["ok"] is True, outcome
    assert outcome["tier"] == "T1"
    assert [r["kind"] for r in outcome["results"]] == \
        [k for k, _ in PROBE_PROMPTS]
    assert all(r["non_empty"] for r in outcome["results"])
    assert stub.hits == len(PROBE_PROMPTS)
    assert all("latency_s" in r for r in outcome["results"])

    # persisted into the registry: a second probe is free (no model relaunch)
    registry = ModelRegistry(nandaka_home.model_dir, nandaka_home.registry_path)
    entry = registry.get(Path(nandaka_home.model_dir) / "probe-model.gguf")
    assert entry and entry["probe"]["ok"] is True
    assert mgr.probe("probe-model.gguf")["ok"] is True
    assert stub.hits == len(PROBE_PROMPTS), "cached probe must not re-query"

    # force re-runs it
    mgr.probe("probe-model.gguf", force=True)
    assert stub.hits == 2 * len(PROBE_PROMPTS)
    mgr.unload()


def test_probe_failure_is_recorded_not_fatal(nandaka_home, model_dir,
                                             monkeypatch):
    """FR-GGUF-09 must survive a dead backend (FR-IN-06 graceful degradation)."""
    build_gguf(model_dir / "dead-model.gguf", params=700_000_000)

    class FailingManager(ModelManager):
        def client(self, model_ref=None, n_ctx=None):
            raise ChatError("cannot reach local at http://127.0.0.1:1/v1")

    mgr = FailingManager(nandaka_home)
    outcome = mgr.probe("dead-model.gguf")
    assert outcome["ok"] is False, \
        "a probe where every prompt failed must not be reported as ok"
    assert outcome["results"][0]["kind"] == "error"
    assert "cannot reach" in outcome["results"][0]["error"]

    registry = ModelRegistry(nandaka_home.model_dir, nandaka_home.registry_path)
    entry = registry.get(Path(nandaka_home.model_dir) / "dead-model.gguf")
    assert entry and entry["probe"]["ok"] is False   # failure is on record


def test_probe_without_model_raises(nandaka_home):
    with pytest.raises(ChatError) as exc:
        ModelManager(nandaka_home).probe()
    assert "no GGUF model" in str(exc.value)


# --------------------------------------------------------------------------- #
# FR-GGUF-13: Ollama backend
# --------------------------------------------------------------------------- #


def test_ollama_backend_uses_openai_dialect(nandaka_home, model_dir):
    build_gguf(model_dir / "ollama-model.gguf", params=1_000_000_000)
    nandaka_home.backend = "ollama"
    nandaka_home.ollama_host = "http://127.0.0.1:11434"

    mgr = ModelManager(nandaka_home)
    client = mgr.client("ollama-model.gguf")
    assert client.base_url == "http://127.0.0.1:11434/v1"
    assert client.model == "ollama-model"   # file stem is the ollama tag
    assert client.provider == "local"       # never budgeted/audited as cloud
    assert mgr.server.running is False      # ollama path never spawns llama-server


def test_ollama_backend_without_model_degrades(nandaka_home):
    """An unknown tag still returns a usable client — Ollama resolves it."""
    nandaka_home.backend = "ollama"
    client = ModelManager(nandaka_home).client("llama3.2:1b")
    assert client.model == "llama3.2:1b"
    assert client.base_url.endswith("/v1")


# --------------------------------------------------------------------------- #
# task_client: local-first dispatch (CO-04)
# --------------------------------------------------------------------------- #


def test_task_client_local_first_even_when_cloud_available(nandaka_home,
                                                           model_dir,
                                                           monkeypatch):
    build_gguf(model_dir / "local-first.gguf", params=1_000_000_000)
    nandaka_home.backend = "ollama"

    seen = {"cloud": 0}

    class SpyManager(ModelManager):
        def client(self, model_ref=None, n_ctx=None):
            seen["local"] = True
            return super().client(model_ref, n_ctx)

    from nandaka.models import server as srv

    def spy_cloud(cfg):
        seen["cloud"] += 1
        return None                             # no creds configured

    monkeypatch.setattr(srv, "cloud_client", spy_cloud)
    mgr = SpyManager(nandaka_home)
    client, entry, warning = mgr.task_client("mcq", "local-first.gguf")
    assert seen["local"] is True
    assert seen["cloud"] == 0, \
        "local-first: cloud must not even be consulted without --cloud (CO-04)"
    assert client.provider == "local"
    assert entry["tier"] == "T1"                # 1B params -> 0.6-2B band


def test_task_client_cloud_used_only_when_opted_in(nandaka_home, model_dir,
                                                   monkeypatch):
    build_gguf(model_dir / "gated.gguf", params=400_000_000)   # T0
    nandaka_home.backend = "ollama"
    nandaka_home.cloud_enabled = True
    nandaka_home.nvidia_api_key = "nvapi-test"

    from nandaka.models.client import ChatClient
    from nandaka.models import server as srv
    cloud = ChatClient(base_url="http://127.0.0.1:9/v1", model="cloud-model",
                       api_key="k", provider="nvidia", token_budget=0)
    monkeypatch.setattr(srv, "cloud_client", lambda cfg: cloud)

    mgr = ModelManager(nandaka_home)
    client, entry, warning = mgr.task_client("mcq", "gated.gguf",
                                             prefer_cloud=True)
    assert client is cloud
    assert client.provider == "nvidia"
    assert "cloud fallback active" in warning

    # without --cloud the same feature stays local
    client2, _, _ = mgr.task_client("mcq", "gated.gguf")
    assert client2.provider == "local"


# --------------------------------------------------------------------------- #
# ram_report (NFR-P-08)
# --------------------------------------------------------------------------- #


def test_ram_report_without_process(nandaka_home):
    report = ModelManager(nandaka_home).ram_report()
    assert report["rss_mb"] == 0
    assert report["state"] == "no model loaded"


def test_ram_report_reads_current_process(nandaka_home):
    """Against our own PID the kernel-reported RSS must be > 0."""
    srv = LlamaServer(nandaka_home)

    class P:
        pid = os.getpid()

        def poll(self):
            return None

    srv.proc = P()
    mgr = ModelManager(nandaka_home)
    mgr.server = srv
    data = mgr.ram_report()
    assert data["rss_mb"] > 0
    assert data["pid"] == os.getpid()
    assert data["budget_mb"] == nandaka_home.ram_budget_mb


# --------------------------------------------------------------------------- #
# FR-IN-05: cache-first + resumable downloads
# --------------------------------------------------------------------------- #


class _FileStub:
    """Minimal HTTP file server honouring Range requests."""

    def __init__(self, payload: bytes):
        self.payload = payload
        self.requests: list[dict] = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        # Unique path per stub: fetch tests share one NANDAKA_DIR, and the
        # cache is keyed by URL, so a common path would cross-contaminate.
        self._name = f"/file-{id(self):x}.pdf"

    @property
    def url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}{self._name}"

    def our_requests(self, ua: str) -> list[dict]:
        """Requests carrying *ua* — excludes the robots.txt probe (AD-05),
        which urllib sends with its own User-Agent."""
        return [r for r in self.requests if r["ua"] == ua]

    def _handler(self):
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                stub.requests.append({"range": self.headers.get("Range"),
                                      "ua": self.headers.get("User-Agent", "")})
                rng = self.headers.get("Range")
                if rng and rng.startswith("bytes="):
                    start = int(rng.split("=")[1].split("-")[0] or 0)
                    if start >= len(stub.payload):
                        self.send_response(416)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    body = stub.payload[start:]
                    self.send_response(206)
                    # A Range-capable server must state the total length;
                    # without it the client cannot validate a resumed file.
                    self.send_header("Content-Range",
                                     f"bytes {start}-{len(stub.payload)-1}/"
                                     f"{len(stub.payload)}")
                else:
                    body = stub.payload
                    self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def test_fetch_obeys_robots_and_sends_ua(nandaka_home, monkeypatch):
    from nandaka.sources import allowed_by_robots, fetch_url
    monkeypatch.setattr("nandaka.sources._robots_cache", {})
    assert allowed_by_robots("http://127.0.0.1:1/page") is True  # unreachable

    payload = b"hello world"
    fs = _FileStub(payload)
    try:
        res = fetch_url(fs.url, nandaka_home)
        assert res.ok and res.status == "fetched"
        assert res.data == payload
        ours = fs.our_requests(nandaka_home.user_agent)
        assert ours, "the fetch must identify itself (AD-05)"
        assert all(r["ua"] == nandaka_home.user_agent for r in ours)
    finally:
        fs.close()


def _seed_partial(cpath: Path, data: bytes, total: int | None = None) -> Path:
    """Write a .part exactly as fetch_url would have left it, validator and all."""
    import json as _json
    ppath = cpath.with_name(cpath.name + ".part")
    cpath.parent.mkdir(parents=True, exist_ok=True)
    ppath.write_bytes(data)
    cpath.with_name(cpath.name + ".part.meta").write_text(
        _json.dumps({"total": len(data) if total is None else total,
                     "etag": None, "last_modified": None}), encoding="utf-8")
    return ppath


def test_fetch_resumes_partial_download(nandaka_home):
    """FR-IN-05: an interrupted download resumes via Range, no re-download."""
    from nandaka.sources import _cache_path, fetch_url
    payload = bytes(range(256)) * 20             # unique, position-revealing
    fs = _FileStub(payload)
    try:
        cpath = _cache_path(Path(nandaka_home.cache_dir), fs.url)
        _seed_partial(cpath, payload[:400], total=len(payload))

        res = fetch_url(fs.url, nandaka_home)
        assert res.ok
        assert res.status == "resumed"
        assert res.data == payload                # complete, not duplicated
        assert fs.requests[-1]["range"] == "bytes=400-"
        assert cpath.read_bytes() == payload      # cache now whole
        assert not cpath.with_name(cpath.name + ".part").exists()
    finally:
        fs.close()


def test_fetch_restart_when_file_changed_since_partial(nandaka_home):
    """Regression: a file edited between the pull and its resume must NOT be
    spliced into a corrupt prefix+tail mixture (was silently cached as ok)."""
    from nandaka.sources import _cache_path, fetch_url
    old = b"A" * 1000
    new = bytes(range(256)) * 6                   # 1536 bytes, different content
    fs = _FileStub(new)
    try:
        cpath = _cache_path(Path(nandaka_home.cache_dir), fs.url)
        _seed_partial(cpath, old[:400], total=len(old))   # recorded for "old"

        res = fetch_url(fs.url, nandaka_home)
        assert res.ok
        assert res.data == new                    # clean full copy, never mixed
        assert res.data[:400] != old[:400], "stale prefix must be discarded"
        assert any("re-downloading in full" in n for n in res.notes), res.notes
        assert cpath.read_bytes() == new
    finally:
        fs.close()


def test_fetch_resume_when_file_shrunk(nandaka_home):
    """A remote smaller than our partial would truncate silently; restart."""
    from nandaka.sources import _cache_path, fetch_url
    fs = _FileStub(b"B" * 200)
    try:
        cpath = _cache_path(Path(nandaka_home.cache_dir), fs.url)
        _seed_partial(cpath, b"A" * 150, total=900)   # recorded size > remote

        res = fetch_url(fs.url, nandaka_home)
        assert res.ok
        assert res.data == b"B" * 200
        assert any("re-downloading in full" in n for n in res.notes), res.notes
    finally:
        fs.close()


def test_fetch_partial_beyond_remote_end_gets_416(nandaka_home):
    """416 means the partial cannot belong to this file: fall back to full."""
    from nandaka.sources import _cache_path, fetch_url
    payload = b"C" * 200
    fs = _FileStub(payload)
    try:
        cpath = _cache_path(Path(nandaka_home.cache_dir), fs.url)
        _seed_partial(cpath, payload[:50] + b"Z" * 250, total=900)
        ppath = cpath.with_name(cpath.name + ".part")
        ppath.write_bytes(b"Z" * 300)             # longer than the remote file

        res = fetch_url(fs.url, nandaka_home)
        assert res.ok and res.data == payload
        assert any("416" in n for n in res.notes), res.notes
        assert any("re-downloading in full" in n for n in res.notes), res.notes
        assert cpath.read_bytes() == payload
    finally:
        fs.close()


def test_fetch_without_validator_restarts_cleanly(nandaka_home):
    """A .part with no recorded length cannot be proven safe -> full fetch."""
    from nandaka.sources import _cache_path, fetch_url
    payload = bytes(range(256)) * 4
    fs = _FileStub(payload)
    try:
        cpath = _cache_path(Path(nandaka_home.cache_dir), fs.url)
        ppath = cpath.with_name(cpath.name + ".part")
        cpath.parent.mkdir(parents=True, exist_ok=True)
        ppath.write_bytes(payload[:100])          # bytes only, no .part.meta

        res = fetch_url(fs.url, nandaka_home)
        assert res.ok and res.data == payload
        assert res.status == "fetched"            # never claimed a resume
        assert any("re-downloading in full" in n for n in res.notes), res.notes
    finally:
        fs.close()


def test_truncated_download_is_not_cached(nandaka_home):
    """A file cut off by max_bytes must never be promoted into the cache,
    where it would be served as a valid, silently-short document forever."""
    from nandaka.sources import _cache_path, fetch_url
    fs = _FileStub(b"A" * 5000)
    try:
        cpath = _cache_path(Path(nandaka_home.cache_dir), fs.url)
        res = fetch_url(fs.url, nandaka_home, max_bytes=1000)
        assert res.ok is False
        assert any("NOT cached" in n for n in res.notes), res.notes
        assert not cpath.exists(), "truncated file must not enter the cache"
        assert cpath.with_name(cpath.name + ".part").exists(), \
            "partial kept so a later run can resume it"

        # a follow-up fetch with a generous budget resumes and completes
        done = fetch_url(fs.url, nandaka_home)
        assert done.ok and done.data == b"A" * 5000
        assert cpath.read_bytes() == b"A" * 5000
    finally:
        fs.close()


def test_fetch_cache_wins_over_network(nandaka_home):
    """FR-IN-04: a cached page means zero network (CO-03)."""
    from nandaka.sources import fetch_url
    fs = _FileStub(b"network body")
    try:
        first = fetch_url(fs.url, nandaka_home)
        assert first.ok and not first.from_cache
        assert len(fs.our_requests(nandaka_home.user_agent)) == 1

        second = fetch_url(fs.url, nandaka_home)
        assert second.ok and second.from_cache
        assert second.status == "cache"
        assert second.data == first.data
        assert len(fs.our_requests(nandaka_home.user_agent)) == 1, \
            "cache must win (FR-IN-04/CO-03)"
    finally:
        fs.close()


def test_fetch_offline_mode_never_touches_network(nandaka_home):
    """allow_network=False must not open a socket at all (CO-04)."""
    from nandaka.sources import fetch_url
    fs = _FileStub(b"data")
    try:
        res = fetch_url(fs.url, nandaka_home, allow_network=False)
        assert res.ok is False and res.status == "offline"
        assert fs.requests == [], "offline mode must not fetch"
        assert any("network disabled" in n for n in res.notes)
    finally:
        fs.close()


def test_fetch_truncates_at_size_limit(nandaka_home):
    """max_bytes caps the response at chunk granularity AND marks it incomplete."""
    from nandaka.sources import fetch_url
    fs = _FileStub(b"A" * 5000)
    try:
        res = fetch_url(fs.url, nandaka_home, max_bytes=1000)
        assert len(res.data) <= 1000 + 65536      # chunk-granular, not whole
        assert any("truncated" in n for n in res.notes)
        assert res.ok is False                    # short != complete (FR-IN-04)
    finally:
        fs.close()


def test_fetch_network_error_degrades_not_fails(nandaka_home):
    from nandaka.sources import fetch_url
    res = fetch_url("http://127.0.0.1:1/never.pdf", nandaka_home)
    assert res.ok is False
    assert res.status in ("network-error", "blocked-by-robots")
    assert res.notes, "degradation must be explained, not silent (FR-IN-06)"


# --------------------------------------------------------------------------- #
# NFR-P-07: swap timing is reported
# --------------------------------------------------------------------------- #


def test_swap_returns_elapsed_time(nandaka_home, stub, monkeypatch):
    srv = LlamaServer(nandaka_home)
    t0 = time.time()
    ok, elapsed = srv.swap("nonexistent.gguf", wait_s=1)
    assert ok is False                          # no binary configured
    assert elapsed >= 0.0
    assert isinstance(elapsed, float)
    assert time.time() - t0 < 10                # bounded, never hangs
