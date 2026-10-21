"""One OpenAI-compatible chat client for every backend.

Nandaka speaks the same ``/v1/chat/completions`` dialect to three parties:

  * local llama-server (EX-10) — offline, no key,
  * NVIDIA NIM          (EX-12, replaces Sarvam per build order),
  * OpenRouter          (EX-12).

Having exactly one client keeps the cloud-fallback surface minimal
(FR-CLOUD-01..06): enable flag, token budget, local response cache, audit log.

Stdlib only (urllib) so it runs on Termux with zero compiled dependencies.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT = 120


class ChatError(Exception):
    pass


class ChatClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout: int = DEFAULT_TIMEOUT,
        cache_dir: str | Path | None = None,
        audit_path: str | Path | None = None,
        token_budget: int = 0,
        provider: str = "local",
        temperature: float = 0.4,
        seed: int | None = 42,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.audit_path = Path(audit_path) if audit_path else None
        self.token_budget = token_budget
        self.provider = provider
        self.temperature = temperature
        self.seed = seed
        self._tokens_used = 0
        self.last_usage: dict[str, Any] = {}

    # ------------------------------------------------------------------ #

    def _cache_key(self, payload: dict) -> str:
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _cache_get(self, key: str) -> dict | None:
        if not self.cache_dir:
            return None
        p = self.cache_dir / f"resp-{key}.json"
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
        return None

    def _cache_put(self, key: str, data: dict) -> None:
        if not self.cache_dir:
            return
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            (self.cache_dir / f"resp-{key}.json").write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def _audit(self, record: dict) -> None:
        if not self.audit_path:
            return
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass

    # ------------------------------------------------------------------ #

    @property
    def offline(self) -> bool:
        return self.provider == "local"

    def budget_ok(self, est_tokens: int) -> bool:
        """FR-CLOUD-02: refuse requests that would exceed the token budget."""
        if self.provider == "local" or self.token_budget <= 0:
            return True
        return self._tokens_used + est_tokens <= self.token_budget

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 1024,
        temperature: float | None = None,
        json_mode: bool = False,
        use_cache: bool = True,
        extra: dict | None = None,
    ) -> dict:
        """Run one chat completion. Returns the parsed response JSON."""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": self.temperature if temperature is None else temperature,
        }
        if self.seed is not None and self.provider == "local":
            payload["seed"] = self.seed
        if json_mode and self.provider != "local":
            # Only cloud endpoints are guaranteed to honour response_format;
            # llama-server versions differ, so local JSON relies on the prompt
            # + robust parser instead (offline-first reliability).
            payload["response_format"] = {"type": "json_object"}
        if extra:
            payload.update(extra)

        est = sum(len(str(m.get("content", ""))) for m in messages) // 4 + max_tokens
        if not self.budget_ok(est):
            raise ChatError(
                f"cloud token budget exceeded ({self._tokens_used}/{self.token_budget}); "
                "increase cloud_token_budget or split the task"
            )

        key = self._cache_key(payload)
        if use_cache:
            cached = self._cache_get(key)
            if cached is not None:
                cached["_cached"] = True
                return cached

        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}" if self.api_key else "Bearer none",
                "User-Agent": "Nandaka/1.0",
            },
        )
        started = time.time()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            self._audit({"ts": time.time(), "provider": self.provider,
                         "model": self.model, "status": exc.code, "error": detail,
                         "elapsed_s": round(time.time() - started, 2)})
            raise ChatError(f"HTTP {exc.code} from {self.provider}: {detail}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            self._audit({"ts": time.time(), "provider": self.provider,
                         "model": self.model, "status": 0, "error": str(exc)})
            raise ChatError(f"cannot reach {self.provider} at {self.base_url}: {exc}") from None

        usage = data.get("usage") or {}
        prompt_t = int(usage.get("prompt_tokens", 0))
        comp_t = int(usage.get("completion_tokens", 0))
        self._tokens_used += prompt_t + comp_t  # FR-CLOUD-02 accounting
        self.last_usage = {"prompt_tokens": prompt_t, "completion_tokens": comp_t,
                           "total_tokens": prompt_t + comp_t}
        elapsed = round(time.time() - started, 2)

        # FR-CLOUD-05: every cloud call is logged (never log the key).
        if self.provider != "local":
            self._audit({
                "ts": time.time(), "provider": self.provider, "base_url": self.base_url,
                "model": self.model, "tokens": self.last_usage,
                "estimated_cost_usd": round((prompt_t + comp_t) / 1e6 * 0.5, 6),
                "elapsed_s": elapsed, "status": 200,
            })

        if use_cache:
            self._cache_put(key, data)
        return data

    # convenience ------------------------------------------------------- #

    def text(self, messages: list[dict[str, str]], **kw) -> str:
        data = self.chat(messages, **kw)
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            return ""

    @staticmethod
    def system_user(system: str, user: str) -> list[dict[str, str]]:
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]
