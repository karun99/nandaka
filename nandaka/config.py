"""Configuration for Nandaka.

Design goals:
  * stdlib-only (works on Termux with no compiler),
  * every path overridable by env var so Termux, Linux desktop and CI all work,
  * layered precedence: CLI flags > environment > config file > defaults.

Config file: ``~/.nandaka/config.toml`` (flat TOML, parsed by a tiny built-in
reader so we never depend on tomllib's availability).
"""

from __future__ import annotations

import os
import platform
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# Platform helpers
# --------------------------------------------------------------------------- #


def is_termux() -> bool:
    """True when running inside Termux on Android."""
    prefix = os.environ.get("PREFIX", "")
    return "com.termux" in prefix or Path("/data/data/com.termux").exists()


def home_dir() -> Path:
    return Path(os.environ.get("NANDAKA_HOME", Path.home()))


def default_nandaka_dir() -> Path:
    """Root application directory (overridable for Termux shared storage)."""
    env = os.environ.get("NANDAKA_DIR")
    if env:
        return Path(env).expanduser()
    if is_termux():
        # Keep large artefacts on shared storage when present.
        shared = Path("/storage/emulated/0")
        if shared.exists() and os.access(shared, os.W_OK):
            return shared / "nandaka"
        return home_dir() / "nandaka"
    return home_dir() / ".nandaka"


# --------------------------------------------------------------------------- #
# Minimal flat-TOML reader (stdlib only)
# --------------------------------------------------------------------------- #

_BOOL = re.compile(r"^(true|false)$", re.I)
_NUM = re.compile(r"^-?\d+(\.\d+)?$")


def _scalar(raw: str) -> Any:
    raw = raw.strip()
    if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
        return raw[1:-1].replace('\\"', '"')
    if raw.startswith("'") and raw.endswith("'") and len(raw) >= 2:
        return raw[1:-1]
    if _BOOL.match(raw):
        return raw.lower() == "true"
    if _NUM.match(raw):
        return float(raw) if "." in raw else int(raw)
    return raw


def parse_flat_toml(text: str) -> dict[str, Any]:
    """Parse the flat ``section`` / ``key = value`` subset Nandaka uses."""
    data: dict[str, Any] = {}
    section = ""
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            data.setdefault(section, {})
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        target = data[section] if section else data
        target[key] = _scalar(val)
    return data


# --------------------------------------------------------------------------- #
# Config object
# --------------------------------------------------------------------------- #


@dataclass
class Config:
    # Paths
    nandaka_dir: str = ""
    model_dir: str = ""
    registry_path: str = ""
    cache_dir: str = ""
    work_dir: str = "nandaka-out"

    # Model runtime
    ram_budget_mb: int = 900
    n_ctx: int = 0  # 0 = auto (1024 when budget <= 1 GB, see NFR-D-03)
    llama_server_bin: str = "llama-server"
    llama_server_host: str = "127.0.0.1"
    llama_server_port: int = 8973
    backend: str = "llama_cpp"  # llama_cpp | ollama
    ollama_host: str = "http://127.0.0.1:11434"
    gpu_layers: int = 0  # opt-in (-ngl), NFR-D-09
    temperature: float = 0.4
    seed: int = 42

    # Cloud fallback (FR-CLOUD): NVIDIA NIM + OpenRouter only (Sarvam removed)
    cloud_enabled: bool = False
    cloud_provider: str = "nvidia"  # nvidia | openrouter
    cloud_model: str = ""
    cloud_token_budget: int = 20000
    nvidia_api_key: str = ""
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    cloud_timeout_s: int = 60

    # Networking / sources
    user_agent: str = "Nandaka/1.0 (offline-first academic workbench)"
    download_timeout_s: int = 30
    max_concurrent_downloads: int = 1  # CO-03 / FR-IN-05

    # Images (IR-07)
    image_max_kb: int = 200

    # SSJ (CO-06): never disable-able via config; recorded for audit only
    ssj_enforced: bool = True

    extra: dict = field(default_factory=dict)

    # ---------------- construction ---------------- #

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        cfg = cls()
        cfg_path = Path(path) if path else default_nandaka_dir() / "config.toml"
        if cfg_path.exists():
            try:
                flat = parse_flat_toml(cfg_path.read_text(encoding="utf-8"))
            except OSError:
                flat = {}
            for key, value in flat.items():
                if isinstance(value, dict):          # [section] block
                    for k, v in value.items():
                        if hasattr(cfg, k):
                            setattr(cfg, k, v)
                        else:
                            cfg.extra[k] = v
                elif hasattr(cfg, key):              # top-level flat key
                    setattr(cfg, key, value)
                else:
                    cfg.extra[key] = value
        cfg._apply_env()
        cfg._resolve_paths()
        return cfg

    def _apply_env(self) -> None:
        env_map = {
            "NANDAKA_MODEL_DIR": ("model_dir", str),
            "NANDAKA_WORK_DIR": ("work_dir", str),
            "NANDAKA_RAM_BUDGET_MB": ("ram_budget_mb", int),
            "NANDAKA_N_CTX": ("n_ctx", int),
            "NANDAKA_LLAMA_SERVER": ("llama_server_bin", str),
            "NANDAKA_BACKEND": ("backend", str),
            # Termux has no Ollama package, so the endpoint is usually a LAN
            # host or a proot-distro instance — configurable without editing
            # config.toml (FR-GGUF-13).
            "NANDAKA_OLLAMA_HOST": ("ollama_host", str),
            "NVIDIA_API_KEY": ("nvidia_api_key", str),
            "OPENROUTER_API_KEY": ("openrouter_api_key", str),
            "NANDAKA_CLOUD": ("cloud_enabled", lambda v: v.lower() in ("1", "true", "yes", "on")),
            "NANDAKA_CLOUD_PROVIDER": ("cloud_provider", str),
        }
        for env_name, (attr, cast) in env_map.items():
            if env_name in os.environ and os.environ[env_name] != "":
                try:
                    setattr(self, attr, cast(os.environ[env_name]))
                except (ValueError, TypeError):
                    pass

    def _resolve_paths(self) -> None:
        root = Path(self.nandaka_dir).expanduser() if self.nandaka_dir else default_nandaka_dir()
        self.nandaka_dir = str(root)
        if not self.model_dir:
            self.model_dir = str(root / "models")
        if not self.registry_path:
            self.registry_path = str(root / "registry.json")
        if not self.cache_dir:
            self.cache_dir = str(root / "cache")
        if not self.work_dir:
            self.work_dir = str(root / "nandaka-out")
        elif not Path(self.work_dir).expanduser().is_absolute():
            # DR-08: a relative output dir resolves under NANDAKA_DIR, not the CWD
            self.work_dir = str(root / Path(self.work_dir).name)

    def ensure_dirs(self) -> None:
        for p in (self.nandaka_dir, self.model_dir, self.cache_dir, self.work_dir):
            try:
                Path(p).mkdir(parents=True, exist_ok=True)
                os.chmod(p, 0o700)  # §3.6 Security: caches mode 0700
            except OSError:
                pass

    # ---------------- derived values ---------------- #

    def effective_n_ctx(self) -> int:
        """NFR-D-03: auto n_ctx from RAM budget; default 1024 when <= 1 GB."""
        if self.n_ctx and self.n_ctx > 0:
            return int(self.n_ctx)
        if self.ram_budget_mb <= 1024:
            return 1024
        # ~1 MB of KV cache per 64 tokens per B of model, rough ceiling:
        return max(1024, min(8192, (self.ram_budget_mb // 1024) * 2048))

    def using_ollama(self) -> bool:
        """FR-GGUF-13: is the Ollama backend selected?

        Termux ships no Ollama package, so this is usually a server on the LAN
        or an instance inside proot-distro; every call site should treat the
        endpoint as optional and degrade when it is absent.
        """
        return str(self.backend).strip().lower() == "ollama"

    def cloud_credentials(self) -> tuple[str, str]:
        """Return (base_url, api_key) for the selected provider, or ("", "")."""
        if self.cloud_provider == "openrouter":
            return self.openrouter_base_url, self.openrouter_api_key
        return self.nvidia_base_url, self.nvidia_api_key

    def summary(self) -> dict:
        d = asdict(self)
        for secret in ("nvidia_api_key", "openrouter_api_key"):
            if d.get(secret):
                d[secret] = d[secret][:4] + "***"
        return d


_CURRENT: Config | None = None


def get_config(path: str | Path | None = None, reload: bool = False) -> Config:
    global _CURRENT
    if _CURRENT is None or reload or path is not None:
        _CURRENT = Config.load(path)
    return _CURRENT


def machine_profile() -> dict:
    return {
        "python": platform.python_version(),
        "machine": platform.machine(),
        "system": platform.system(),
        "termux": is_termux(),
    }
