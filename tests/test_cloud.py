"""Cloud fallback behaviour (FR-CLOUD-01..06): opt-in, budget, cache, audit.

Provider surface is NVIDIA NIM + OpenRouter only (Sarvam removed). Every
network call goes to a local http.server stub — no external service is
contacted by this test module.
"""

from __future__ import annotations

import json
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from nandaka.models.client import ChatClient, ChatError
from nandaka.models.server import cloud_client


# --------------------------------------------------------------------------- #
# Local OpenAI-compatible stub
# --------------------------------------------------------------------------- #


class _Stub:
    def __init__(self, *, fail_status: int | None = None, tokens: int = 120):
        self.hits = 0
        self.fail_status = fail_status
        self.tokens = tokens
        self.last_body: dict = {}
        self.last_auth = ""
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}/v1"

    def _handler(self):
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep test output clean
                pass

            def do_POST(self):
                stub.hits += 1
                length = int(self.headers.get("Content-Length", 0))
                stub.last_body = json.loads(self.rfile.read(length) or b"{}")
                stub.last_auth = self.headers.get("Authorization", "")
                if stub.fail_status:
                    payload = json.dumps({"error": "stub failure"}).encode()
                    self.send_response(stub.fail_status)
                else:
                    payload = json.dumps({
                        "choices": [{"message": {"role": "assistant",
                                                 "content": "Paris."}}],
                        "usage": {"prompt_tokens": stub.tokens // 2,
                                  "completion_tokens": stub.tokens - stub.tokens // 2,
                                  "total_tokens": stub.tokens},
                    }).encode()
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        return Handler

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture()
def stub():
    s = _Stub()
    yield s
    s.close()


def _client(stub, **kw) -> ChatClient:
    defaults = dict(base_url=stub.base_url, model="meta/llama-3.1-8b-instruct",
                    api_key="test-key", provider="nvidia", timeout=10,
                    temperature=0.4, seed=None)
    defaults.update(kw)
    return ChatClient(**defaults)


MSG = [{"role": "user", "content": "What is the capital of France?"}]


# --------------------------------------------------------------------------- #
# FR-CLOUD-01: disabled by default
# --------------------------------------------------------------------------- #


def test_cloud_disabled_by_default(nandaka_home):
    cfg = nandaka_home
    assert cfg.cloud_enabled is False
    assert cloud_client(cfg) is None  # opt-in only


def test_cloud_enabled_without_key_returns_none(nandaka_home, capsys):
    cfg = nandaka_home
    cfg.cloud_enabled = True
    cfg.nvidia_api_key = ""
    assert cloud_client(cfg) is None
    out = capsys.readouterr().out
    assert "no API key" in out and "NVIDIA_API_KEY" in out
    assert "continuing offline" in out  # never fatal (FR-IN-06)


def test_cloud_client_provider_wiring(nandaka_home):
    cfg = nandaka_home
    cfg.cloud_enabled = True
    cfg.nvidia_api_key = "nvapi-test"
    c = cloud_client(cfg)
    assert c is not None
    assert c.provider == "nvidia"
    assert c.base_url == "https://integrate.api.nvidia.com/v1"
    assert c.api_key == "nvapi-test"
    assert c.token_budget == cfg.cloud_token_budget > 0
    assert str(c.audit_path).endswith("cloud-audit.jsonl")
    assert str(c.cache_dir).endswith("cloud")

    cfg.cloud_provider = "openrouter"
    cfg.openrouter_api_key = "sk-or-test"
    c2 = cloud_client(cfg)
    assert c2 is not None
    assert c2.provider == "openrouter"
    assert c2.base_url == "https://openrouter.ai/api/v1"


def test_no_sarvam_anywhere():
    """Build order: the cloud fallback is NVIDIA NIM + OpenRouter only.

    Comments may mention that Sarvam was removed, but no Sarvam API surface
    (endpoint, client, key, or model id) may remain in the shipped code.
    """
    from pathlib import Path
    import re
    root = Path(__file__).resolve().parent.parent / "nandaka"
    pattern = re.compile(
        r"sarvam\.ai|api\.sarvam|sarvam_api|SARVAM_API|SarvamClient|"
        r"sarvam_client|sarvam-cloud|sarvam/chat|\"sarvam\"|'sarvam'",
        re.I)
    hits = []
    for p in root.rglob("*.py"):
        for lineno, line in enumerate(
                p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if pattern.search(line):
                hits.append(f"{p}:{lineno}: {line.strip()}")
    assert hits == [], f"Sarvam API surface found in:\n" + "\n".join(hits)

    # and the configured providers are exactly the two approved ones
    from nandaka.config import Config
    cfg = Config()
    assert cfg.cloud_provider in ("nvidia", "openrouter")
    assert cfg.nvidia_base_url == "https://integrate.api.nvidia.com/v1"
    assert cfg.openrouter_base_url == "https://openrouter.ai/api/v1"


# --------------------------------------------------------------------------- #
# FR-CLOUD-02: token budget
# --------------------------------------------------------------------------- #


def test_budget_refuses_request_before_network(stub, tmp_path):
    client = _client(stub, token_budget=10)  # estimate will exceed 10
    with pytest.raises(ChatError) as exc:
        client.chat(MSG, max_tokens=500)
    assert "budget exceeded" in str(exc.value)
    assert stub.hits == 0  # refused before any network call


def test_budget_accounting_across_calls(stub):
    client = _client(stub, token_budget=130)
    client.chat(MSG, max_tokens=10)          # est small -> allowed
    assert client._tokens_used == 120        # billed from server usage
    with pytest.raises(ChatError):
        client.chat(MSG, max_tokens=10)      # 120 + est > 130
    assert stub.hits == 1

    # budget=0 disables the cap (local semantics)
    free = _client(stub, token_budget=0)
    for _ in range(3):
        free.chat(MSG, max_tokens=10)
    assert stub.hits == 4


# --------------------------------------------------------------------------- #
# FR-CLOUD-04: local response cache
# --------------------------------------------------------------------------- #


def test_cache_serves_repeat_offline(stub, tmp_path):
    client = _client(stub, cache_dir=tmp_path / "cache")
    first = client.chat(MSG, max_tokens=10)
    assert stub.hits == 1
    assert "_cached" not in first

    second = client.chat(MSG, max_tokens=10)
    assert stub.hits == 1          # served from disk, zero network
    assert second["_cached"] is True
    assert second["choices"][0]["message"]["content"] == "Paris."

    # cache is keyed on the payload: a different prompt misses
    client.chat([{"role": "user", "content": "2+2?"}], max_tokens=10)
    assert stub.hits == 2

    # use_cache=False bypasses the cache entirely
    client.chat(MSG, max_tokens=10, use_cache=False)
    assert stub.hits == 3


def test_cache_survives_a_new_client(tmp_path, stub):
    c1 = _client(stub, cache_dir=tmp_path / "cache")
    c1.chat(MSG, max_tokens=10)
    c2 = _client(stub, cache_dir=tmp_path / "cache")
    c2.chat(MSG, max_tokens=10)
    assert stub.hits == 1          # persisted across instances (FR-CLOUD-04)


# --------------------------------------------------------------------------- #
# FR-CLOUD-05: audit log
# --------------------------------------------------------------------------- #


def test_audit_logs_every_call_without_keys(stub, tmp_path):
    audit = tmp_path / "audit" / "cloud-audit.jsonl"
    client = _client(stub, audit_path=audit, cache_dir=None, token_budget=0)
    client.chat(MSG, max_tokens=10)

    assert audit.exists()
    lines = [json.loads(l) for l in audit.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 1
    rec = lines[0]
    assert rec["provider"] == "nvidia"
    assert rec["status"] == 200
    assert rec["tokens"]["total_tokens"] == 120
    assert "estimated_cost_usd" in rec and rec["estimated_cost_usd"] >= 0
    assert "elapsed_s" in rec

    raw = audit.read_text(encoding="utf-8")
    assert "test-key" not in raw and "api_key" not in raw  # never log secrets


def test_audit_logs_failures(stub, tmp_path):
    s = _Stub(fail_status=503)
    try:
        audit = tmp_path / "fail.jsonl"
        client = _client(s, audit_path=audit, cache_dir=None, token_budget=0)
        with pytest.raises(ChatError) as exc:
            client.chat(MSG, max_tokens=10)
        assert "HTTP 503" in str(exc.value)
        recs = [json.loads(l)
                for l in audit.read_text(encoding="utf-8").splitlines()]
        assert recs and recs[0]["status"] == 503
        assert recs[0]["provider"] == "nvidia"
    finally:
        s.close()


def test_unreachable_endpoint_is_chaterror(tmp_path):
    client = ChatClient(base_url="http://127.0.0.1:1/v1", model="m",
                        api_key="k", provider="nvidia", timeout=2,
                        cache_dir=None, audit_path=tmp_path / "a.jsonl",
                        token_budget=0)
    with pytest.raises(ChatError) as exc:
        client.chat(MSG, max_tokens=5)
    assert "cannot reach nvidia" in str(exc.value)
    # the failure itself was audited (FR-CLOUD-05)
    recs = [json.loads(l)
            for l in (tmp_path / "a.jsonl").read_text().splitlines()]
    assert recs and recs[0]["status"] == 0


# --------------------------------------------------------------------------- #
# Request shape
# --------------------------------------------------------------------------- #


def test_request_uses_openai_dialect_and_bearer(stub):
    client = _client(stub, token_budget=0)
    client.chat(MSG, max_tokens=7, json_mode=True, temperature=0.2)
    body = stub.last_body
    assert body["model"] == "meta/llama-3.1-8b-instruct"
    assert body["messages"] == MSG
    assert body["max_tokens"] == 7
    assert body["temperature"] == 0.2
    assert body["response_format"] == {"type": "json_object"}
    assert stub.last_auth == "Bearer test-key"
    assert client.last_usage["total_tokens"] == 120


def test_local_provider_never_sends_json_mode_or_budget(stub):
    """llama-server dialect: no response_format, budget cap not applied."""
    client = _client(stub, provider="local", api_key="", token_budget=1,
                     cache_dir=None)
    client.chat(MSG, max_tokens=10, json_mode=True)
    assert "response_format" not in stub.last_body
    assert stub.hits == 1
