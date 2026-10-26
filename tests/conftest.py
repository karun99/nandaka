"""Shared fixtures: temp NANDAKA dir, synthetic GGUF builder, sample data."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest


# --------------------------------------------------------------------------- #
# Synthetic GGUF (matches the on-disk spec our parser reads)
# --------------------------------------------------------------------------- #


def _gstr(s: str) -> bytes:
    b = s.encode("utf-8")
    return struct.pack("<Q", len(b)) + b


def build_gguf(path: Path, *, version: int = 3, params: int = 1_000_000_000,
               arch: str = "llama", context: int = 4096,
               chat_template: str | None = None) -> Path:
    """Write a structurally valid GGUF header (+ tensor infos, no data)."""
    kvs: list[bytes] = []

    def kv_str(key: str, val: str) -> bytes:
        return _gstr(key) + struct.pack("<I", 8) + _gstr(val)

    def kv_u32(key: str, val: int) -> bytes:
        return _gstr(key) + struct.pack("<I", 4) + struct.pack("<I", val)

    kvs.append(kv_str("general.architecture", arch))
    kvs.append(kv_str("general.name", f"synthetic-{arch}-{params}"))
    kvs.append(kv_u32("general.file_type", 12))          # Q8_0
    kvs.append(kv_str("tokenizer.ggml.model", "llama"))
    kvs.append(kv_u32(f"{arch}.context_length", context))
    kvs.append(kv_str("general.description", "test fixture"))
    if chat_template:
        kvs.append(kv_str("chat_template", chat_template))

    # one 2D tensor whose numel == params (parameter count comes from tensors)
    side = 1_000_000
    assert params % side == 0 or params < side
    rows = max(1, params // side)
    tensor = _gstr("token_embd.weight")
    tensor += struct.pack("<I", 2)                      # n_dims
    tensor += struct.pack("<QQ", side, rows)            # dims
    tensor += struct.pack("<I", 0)                      # type: F32
    tensor += struct.pack("<Q", 0)                      # offset

    header = b"GGUF" + struct.pack("<I", version)
    header += struct.pack("<QQ", 1, len(kvs))           # tensors, kvs
    data = header + b"".join(kvs) + tensor
    path.write_bytes(data)
    return path


@pytest.fixture()
def nandaka_home(tmp_path, monkeypatch):
    """Isolated NANDAKA_DIR for every test (Termux-safe: env-driven)."""
    home = tmp_path / "nandaka-home"
    monkeypatch.setenv("NANDAKA_DIR", str(home))
    monkeypatch.delenv("NANDAKA_MODEL_DIR", raising=False)
    monkeypatch.delenv("NANDAKA_BACKEND", raising=False)
    monkeypatch.delenv("NANDAKA_OLLAMA_HOST", raising=False)
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("NANDAKA_CLOUD", raising=False)
    from nandaka.config import get_config
    cfg = get_config(reload=True)
    cfg.ensure_dirs()
    return cfg


@pytest.fixture()
def model_dir(nandaka_home) -> Path:
    return Path(nandaka_home.model_dir)


def make_papers_csv(path: Path) -> Path:
    """Past-paper records: a dense topic, a rare topic (TC-17 case)."""
    rows = ["year,paper,topic,subtopic,kind,text"]
    for y in range(2018, 2025):
        rows.append(f"{y},PAPER-{y},Constitution,Preamble,direct,Which article?")
        rows.append(f"{y},PAPER-{y},Constitution,Fundamentals,direct,Who advises?")
        if y % 2 == 0:
            rows.append(f"{y},PAPER-{y},Constitution,Rights,reworded,Right to ?")
        rows.append(f"{y},PAPER-{y},Economy,Budget,diagram,Identify the curve")
    rows.append("2023,PAPER-2023,Rare Topic,Only Once,direct,Obscure question")
    path.write_text("\n".join(rows), encoding="utf-8")
    return path
