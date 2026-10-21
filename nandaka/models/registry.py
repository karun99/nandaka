"""Model registry: discovery, capability tiers, probe results (FR-GGUF-*).

The registry is a JSON file (default ``~/.nandaka/registry.json``, DR-08) that
persists across sessions. Adding a model is a file-drop operation: any GGUF
placed in the model directory appears after the next ``models list`` scan.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .gguf import GGUFInfo, classify_tier, read_gguf, verify_gguf

# Feature capability matrix from SRS §3.3.1.1
# value: min tier required, or ("partial", min_tier) when quality is uncertain
FEATURE_MATRIX: dict[str, dict[str, str]] = {
    "short_summary": {"T0": "ok", "T1": "ok", "T2": "ok", "T3": "ok"},
    "short_draft": {"T0": "ok", "T1": "ok", "T2": "ok", "T3": "ok"},
    "long_draft": {"T0": "off", "T1": "partial", "T2": "ok", "T3": "ok"},
    "bilingual_te": {"T0": "partial", "T1": "partial", "T2": "ok", "T3": "ok"},
    "reasoning_math": {"T0": "partial", "T1": "partial", "T2": "ok", "T3": "ok"},
    "mcq": {"T0": "basic", "T1": "ok", "T2": "ok", "T3": "ok"},
    "diagram_spec": {"T0": "ok", "T1": "ok", "T2": "ok", "T3": "ok"},
}

TIER_ORDER = {"T0": 0, "T1": 1, "T2": 2, "T3": 3}


def feature_state(feature: str, tier: str) -> str:
    """Return 'ok' | 'partial' | 'basic' | 'off' for feature x tier."""
    row = FEATURE_MATRIX.get(feature, {})
    return row.get(tier, "off")


def tier_gate(feature: str, tier: str, force: bool = False) -> tuple[bool, str]:
    """FR-GGUF-11: returns (allowed, message)."""
    state = feature_state(feature, tier)
    label = {
        "ok": "",
        "partial": f"'{feature}' is partial on {tier}: quality may be insufficient, "
                   "human review is mandatory.",
        "basic": f"'{feature}' on {tier} is basic quality; every item will be "
                 "forced to HUMAN REVIEW REQUIRED.",
        "off": f"'{feature}' requires a larger model; loaded model is {tier}. "
               "Use --force to attempt anyway.",
    }[state]
    if state == "off" and not force:
        return False, label
    return True, label


class ModelRegistry:
    def __init__(self, model_dir: str | Path, registry_path: str | Path):
        self.model_dir = Path(model_dir)
        self.registry_path = Path(registry_path)
        self.entries: dict[str, dict[str, Any]] = {}
        self._load()

    # ---------------- persistence ---------------- #

    def _load(self) -> None:
        try:
            if self.registry_path.exists():
                self.entries = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.entries = {}

    def save(self) -> None:
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.registry_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.entries, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, self.registry_path)
        try:
            os.chmod(self.registry_path, 0o600)
        except OSError:
            pass

    # ---------------- discovery ---------------- #

    def scan(self) -> list[dict]:
        """FR-GGUF-01: discover GGUF files and refresh metadata in the registry."""
        found: list[dict] = []
        if self.model_dir.exists():
            for p in sorted(self.model_dir.glob("**/*.gguf")):
                found.append(self.inspect(p))
        # drop registry entries whose files disappeared
        live = {e["path"] for e in found}
        for key in [k for k in self.entries if k not in live]:
            del self.entries[key]
        self.save()
        return found

    def inspect(self, path: str | Path, force: bool = False) -> dict:
        """FR-GGUF-03/-04: read metadata + classify tier for one GGUF."""
        p = Path(path).resolve()
        key = str(p)
        stat = p.stat() if p.exists() else None
        cached = self.entries.get(key)
        if cached and not force and cached.get("mtime") == (stat.st_mtime if stat else None):
            return cached
        info: GGUFInfo = read_gguf(p)
        entry = {
            "path": key,
            "name": info.name,
            "architecture": info.architecture,
            "parameters": info.parameter_count,
            "param_source": info.param_source,
            "tier": classify_tier(info.parameter_count),
            "quant": info.quant,
            "context_length": info.trained_context,
            "tokenizer": info.tokenizer_family,
            "has_chat_template": bool(info.chat_template),
            "size_bytes": info.file_size,
            "mtime": stat.st_mtime if stat else None,
            "valid": info.valid,
            "error": info.error,
            "probe": cached.get("probe") if cached else None,
            "last_used": cached.get("last_used") if cached else None,
            "added": (cached or {}).get("added", time.time()),
        }
        self.entries[key] = entry
        self.save()
        return entry

    def resolve(self, model_ref: str | None) -> Path | None:
        """Resolve --model: exact path, filename, or bare name inside model dir."""
        if model_ref:
            p = Path(model_ref).expanduser()
            if p.exists():
                return p.resolve()
            if not p.is_absolute():
                for candidate in (self.model_dir / model_ref,
                                  self.model_dir / f"{model_ref}.gguf"):
                    if candidate.exists():
                        return candidate.resolve()
            return None
        # default: exactly one model present -> use it
        ggufs = sorted(self.model_dir.glob("*.gguf")) if self.model_dir.exists() else []
        return ggufs[0].resolve() if len(ggufs) == 1 else None

    def get(self, path: str | Path) -> dict | None:
        return self.entries.get(str(Path(path).resolve()))

    def mark_used(self, path: str | Path) -> None:
        e = self.entries.get(str(Path(path).resolve()))
        if e:
            e["last_used"] = time.time()
            self.save()

    def record_probe(self, path: str | Path, result: dict) -> None:
        e = self.entries.setdefault(str(Path(path).resolve()),
                                    {"path": str(path), "tier": "T0"})
        e["probe"] = result
        self.save()

    def verify(self, path: str | Path) -> dict:
        return verify_gguf(path)

    def list(self) -> list[dict]:
        """Return registry entries sorted by tier then name."""
        return sorted(self.entries.values(),
                      key=lambda e: (TIER_ORDER.get(e.get("tier", "T0"), 0),
                                     e.get("name", "")))

    # ---------------- capability helpers ---------------- #

    @staticmethod
    def gate(entry: dict, feature: str, force: bool = False) -> tuple[bool, str]:
        return tier_gate(feature, entry.get("tier", "T0"), force)
