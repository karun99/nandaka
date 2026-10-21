"""llama-server lifecycle + model manager (EX-10, FR-GGUF-05/06/09/13).

One process serves one model at a time; swapping unloads and reloads so peak
RAM never exceeds the budget (CO-01, NFR-P-07). An already-running warm
process is reused when it already hosts the requested model.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

from ..config import Config, is_termux
from .client import ChatClient, ChatError
from .gguf import classify_tier
from .registry import ModelRegistry

_SERVER_CANDIDATES = [
    "llama-server",
    "/usr/local/bin/llama-server",
    "/usr/bin/llama-server",
    "/opt/llama.cpp/build/bin/llama-server",
    str(Path.home() / "llama.cpp" / "build" / "bin" / "llama-server"),
]
if is_termux():  # Termux package: pkg install llama.cpp
    _SERVER_CANDIDATES = [f"{os.environ.get('PREFIX', '/data/data/com.termux/files/usr')}/bin/llama-server"] + _SERVER_CANDIDATES

PROBE_PROMPTS = [
    ("factual", "What is the capital of France? Answer in one short sentence."),
    ("reasoning", "A train travels 60 km in 45 minutes. What is its average speed in km/h? Show one line of working."),
    ("instruction", "Reply with exactly the word: READY"),
]


def find_server_binary(cfg: Config) -> str | None:
    env = os.environ.get("NANDAKA_LLAMA_SERVER")
    if env:
        return env if Path(env).exists() else shutil.which(env)
    for cand in [cfg.llama_server_bin] + _SERVER_CANDIDATES:
        found = shutil.which(cand) or (cand if Path(cand).is_file() else None)
        if found:
            return found
    return None


def _free_port(preferred: int) -> int:
    for port in (preferred, preferred + 1, preferred + 2, 0):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
        except OSError:
            continue
    return preferred


class LlamaServer:
    """Manage one llama-server process serving one GGUF at a time."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.proc: subprocess.Popen | None = None
        self.model_path: str | None = None
        self.port: int = cfg.llama_server_port
        self.host = cfg.llama_server_host
        self.last_error = ""

    # ------------------------------------------------------------------ #

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}/v1"

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def health(self, timeout: float = 0.5) -> bool:
        try:
            with urllib.request.urlopen(
                f"http://{self.host}:{self.port}/health", timeout=timeout
            ) as resp:
                return resp.status == 200
        except Exception:
            return False

    # ------------------------------------------------------------------ #

    def start(self, model_path: str | Path, n_ctx: int | None = None,
              gpu_layers: int | None = None, wait_s: float = 60.0) -> bool:
        """Start llama-server for *model_path* (FR-GGUF-05 auto n_ctx)."""
        if self.running and str(model_path) == self.model_path and self.health():
            return True  # warmed process reuse (FR-GGUF-06)
        self.stop()

        binary = find_server_binary(self.cfg)
        if not binary:
            self.last_error = (
                "llama-server binary not found. Install llama.cpp "
                "(Linux: build from source; Termux: pkg install llama.cpp) "
                "or set NANDAKA_LLAMA_SERVER=/path/to/llama-server."
            )
            return False

        n_ctx = n_ctx or self.cfg.effective_n_ctx()  # NFR-D-03
        gpu = self.cfg.gpu_layers if gpu_layers is None else gpu_layers
        self.port = _free_port(self.port)
        cmd = [
            binary, "-m", str(model_path),
            "--host", self.host, "--port", str(self.port),
            "-c", str(n_ctx),
            "--no-display-prompt",
        ]
        if gpu and gpu > 0:
            cmd += ["-ngl", str(gpu)]  # NFR-D-09 opt-in
        try:
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except OSError as exc:
            self.last_error = f"cannot launch llama-server: {exc}"
            return False

        deadline = time.time() + wait_s
        while time.time() < deadline:
            if self.proc.poll() is not None:
                self.last_error = "llama-server exited during startup (model incompatible or out of RAM)"
                return False
            if self.health():
                self.model_path = str(model_path)
                return True
            time.sleep(0.25)
        self.last_error = f"llama-server not healthy after {wait_s:.0f}s"
        self.stop()
        return False

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        self.proc = None
        self.model_path = None

    def swap(self, model_path: str | Path, **kw) -> tuple[bool, float]:
        """Unload current + load next; returns (ok, elapsed_s) (NFR-P-07)."""
        t0 = time.time()
        ok = self.start(model_path, **kw)
        return ok, time.time() - t0


# --------------------------------------------------------------------------- #
# Cloud fallback (FR-CLOUD): NVIDIA NIM + OpenRouter only
# --------------------------------------------------------------------------- #


def cloud_client(cfg: Config) -> ChatClient | None:
    """FR-CLOUD-01/-06: disabled by default; missing creds never break runs."""
    if not cfg.cloud_enabled:
        return None
    base_url, key = cfg.cloud_credentials()
    if not key:
        print(
            f"[nandaka] cloud fallback enabled but no API key for "
            f"{cfg.cloud_provider} (set {'NVIDIA_API_KEY' if cfg.cloud_provider == 'nvidia' else 'OPENROUTER_API_KEY'}); "
            "continuing offline."
        )
        return None
    model = cfg.cloud_model or (
        "meta/llama-3.1-8b-instruct" if cfg.cloud_provider == "nvidia"
        else "meta-llama/llama-3.1-8b-instruct"
    )
    return ChatClient(
        base_url=base_url, model=model, api_key=key,
        timeout=cfg.cloud_timeout_s, cache_dir=cfg.cache_dir + "/cloud",
        audit_path=cfg.nandaka_dir + "/cloud-audit.jsonl",
        token_budget=cfg.cloud_token_budget, provider=cfg.cloud_provider,
        temperature=cfg.temperature, seed=None,
    )


# --------------------------------------------------------------------------- #
# ModelManager
# --------------------------------------------------------------------------- #


class ModelManager:
    """Glue: registry + llama-server + optional cloud (F-GGUF, F-CLOUD)."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.registry = ModelRegistry(cfg.model_dir, cfg.registry_path)
        self.server = LlamaServer(cfg)
        self._client: ChatClient | None = None
        self._client_model: str | None = None

    # ---------------- local client ---------------- #

    def client(self, model_ref: str | None = None, n_ctx: int | None = None) -> ChatClient:
        if self.cfg.backend == "ollama":
            # FR-GGUF-13: Ollama exposes the same OpenAI dialect on :11434
            entry_path = self.registry.resolve(model_ref)
            tag = Path(entry_path).stem if entry_path else (model_ref or "")
            return ChatClient(base_url=self.cfg.ollama_host.rstrip("/") + "/v1",
                              model=tag, api_key="", cache_dir=None,
                              temperature=self.cfg.temperature, seed=None,
                              provider="local")
        path = self.registry.resolve(model_ref)
        if path is None:
            raise ChatError(
                "no GGUF model selected. Drop a .gguf file into "
                f"{self.cfg.model_dir} or pass --model /path/to/model.gguf (AD-01)."
            )
        if self._client is None or self._client_model != str(path):
            ok, elapsed = self.server.swap(path, n_ctx=n_ctx)
            if not ok:
                raise ChatError(self.server.last_error)
            if elapsed > 5.0:
                print(f"[nandaka] note: model swap took {elapsed:.1f}s (target <= 5s, NFR-P-07)")
            self._client = ChatClient(
                base_url=self.server.base_url, model="local",
                api_key="", cache_dir=None,
                temperature=self.cfg.temperature, seed=self.cfg.seed,
                provider="local",
            )
            self._client_model = str(path)
            self.registry.mark_used(path)
        return self._client

    def unload(self) -> None:
        self.server.stop()
        self._client = None
        self._client_model = None

    # ---------------- capability probe (FR-GGUF-09) ---------------- #

    def probe(self, model_ref: str | None = None, force: bool = False) -> dict:
        path = self.registry.resolve(model_ref)
        if path is None:
            raise ChatError("no GGUF model to probe")
        entry = self.registry.inspect(path, force=True)
        if entry.get("probe") and not force:
            return entry["probe"]
        results = []
        try:
            client = self.client(str(path))
            for kind, prompt in PROBE_PROMPTS:
                t0 = time.time()
                reply = client.text([{"role": "user", "content": prompt}],
                                    max_tokens=64, use_cache=False)
                results.append({
                    "kind": kind,
                    "latency_s": round(time.time() - t0, 2),
                    "chars": len(reply),
                    "non_empty": bool(reply.strip()),
                    "sample": reply.strip()[:120],
                })
        except ChatError as exc:
            results.append({"kind": "error", "error": str(exc)})
        outcome = {
            "time": time.time(),
            "tier": entry.get("tier"),
            "results": results,
            "ok": all(r.get("non_empty") for r in results if "kind" in r and r["kind"] != "error"),
        }
        self.registry.record_probe(path, outcome)
        return outcome

    # ---------------- feature gating ---------------- #

    def ensure_feature(self, feature: str, model_ref: str | None = None,
                       force: bool = False) -> tuple[dict, str]:
        """Returns (entry, warning). Raises ChatError when feature not allowed."""
        path = self.registry.resolve(model_ref)
        if path is None:
            raise ChatError("no GGUF model selected")
        entry = self.registry.inspect(path)
        allowed, msg = self.registry.gate(entry, feature, force)
        if not allowed:
            raise ChatError(msg)
        return entry, msg

    # ---------------- smart dispatch: local vs cloud ---------------- #

    def task_client(self, feature: str, model_ref: str | None = None,
                    force: bool = False, prefer_cloud: bool = False
                    ) -> tuple[ChatClient, dict, str]:
        """Pick the right backend for a task.

        Local first (offline-first CO-04). Cloud only when explicitly enabled
        AND the feature is gated off for the loaded tier (graceful degradation
        FR-GGUF-11 + FR-CLOUD-01), or the caller passes --cloud.
        """
        entry, warning = self.ensure_feature(feature, model_ref, force)
        # warning == "" means fully supported; otherwise it is a
        # partial/basic notice (soft) or the gate already raised (hard).
        if prefer_cloud or (force and warning and self.cfg.cloud_enabled):
            cc = cloud_client(self.cfg)
            if cc:
                return cc, entry, f"cloud fallback active ({self.cfg.cloud_provider})"
        return self.client(model_ref), entry, warning

    def ram_report(self) -> dict:
        """Best-effort RSS of llama-server for bench/reporting (NFR-P-08)."""
        if not self.server.running or self.server.proc is None:
            return {"rss_mb": 0, "state": "no model loaded"}
        pid = self.server.proc.pid
        rss_pages = None
        try:
            with open(f"/proc/{pid}/status", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("VmRSS:"):
                        rss_pages = int(line.split()[1])  # kB actually
                        break
        except OSError:
            pass
        return {"rss_mb": round((rss_pages or 0) / 1024, 1), "pid": pid,
                "n_ctx": self.cfg.effective_n_ctx(), "budget_mb": self.cfg.ram_budget_mb}


def tier_of(path: str | Path) -> str:
    from .gguf import read_gguf
    return classify_tier(read_gguf(path).parameter_count)
