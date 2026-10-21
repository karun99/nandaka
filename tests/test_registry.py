"""Model registry: discovery, persistence, tier gates (FR-GGUF-*)."""

from __future__ import annotations

import json
from pathlib import Path

from nandaka.models.registry import (FEATURE_MATRIX, ModelRegistry, TIER_ORDER,
                                     feature_state, tier_gate)
from tests.conftest import build_gguf


def _reg(model_dir: Path) -> ModelRegistry:
    return ModelRegistry(model_dir, model_dir.parent / "registry.json")


# ---------------- feature matrix / gate ---------------- #


def test_feature_state_and_matrix_shape():
    for feature, row in FEATURE_MATRIX.items():
        assert set(row) == {"T0", "T1", "T2", "T3"}, feature
        assert feature_state(feature, "T2") in ("ok", "partial", "basic", "off")
    assert feature_state("no-such-feature", "T3") == "off"


def test_tier_gate_off_requires_force():
    """FR-GGUF-11: 'off' blocks unless --force; partial/basic warn."""
    allowed, msg = tier_gate("long_draft", "T0")
    assert not allowed and "larger model" in msg
    allowed, msg = tier_gate("long_draft", "T0", force=True)
    assert allowed and msg  # forced through, message kept

    allowed, msg = tier_gate("bilingual_te", "T1")
    assert allowed and "partial" in msg and "mandatory" in msg

    allowed, msg = tier_gate("mcq", "T0")
    assert allowed and "HUMAN REVIEW" in msg

    allowed, msg = tier_gate("mcq", "T3")
    assert allowed and msg == ""


def test_tier_order_is_total():
    assert sorted(TIER_ORDER, key=TIER_ORDER.get) == ["T0", "T1", "T2", "T3"]


# ---------------- discovery & persistence ---------------- #


def test_scan_discovers_and_persists(model_dir):
    build_gguf(model_dir / "small.gguf", params=500_000_000)
    build_gguf(model_dir / "mid.gguf", params=1_500_000_000)
    build_gguf(model_dir / "big.gguf", params=4_000_000_000)

    reg = _reg(model_dir)
    found = reg.scan()
    assert len(found) == 3
    tiers = {e["name"]: e["tier"] for e in found}
    assert set(tiers.values()) <= {"T0", "T1", "T2"}
    assert all(e["valid"] for e in found)

    # registry file exists, is JSON, and reloads into a fresh instance
    reg_path = model_dir.parent / "registry.json"
    assert reg_path.exists()
    data = json.loads(reg_path.read_text(encoding="utf-8"))
    assert len(data) == 3

    reg2 = _reg(model_dir)
    assert len(reg2.list()) == 3
    assert all(e.get("parameters") for e in reg2.list())


def test_scan_drops_missing_files(model_dir):
    p = build_gguf(model_dir / "gone.gguf", params=700_000_000)
    reg = _reg(model_dir)
    assert len(reg.scan()) == 1
    p.unlink()
    assert reg.scan() == []
    assert _reg(model_dir).list() == []


def test_resolve_variants(model_dir):
    build_gguf(model_dir / "alpha-model.gguf", params=1_000_000_000)
    reg = _reg(model_dir)
    reg.scan()

    # bare filename inside the model dir
    assert reg.resolve("alpha-model.gguf") is not None
    # bare name without extension
    assert reg.resolve("alpha-model") is not None
    # absolute path
    assert reg.resolve(str(model_dir / "alpha-model.gguf")) is not None
    # unknown ref
    assert reg.resolve("nope") is None
    # default: exactly one model -> auto-select
    assert reg.resolve(None) is not None

    # two models -> no implicit selection (AD-01 requires an explicit choice)
    build_gguf(model_dir / "beta-model.gguf", params=1_000_000_000)
    reg.scan()
    assert reg.resolve(None) is None


def test_registry_gate_uses_entry_tier(model_dir):
    build_gguf(model_dir / "tiny.gguf", params=400_000_000)  # T0
    reg = _reg(model_dir)
    entry = reg.scan()[0]
    assert entry["tier"] == "T0"

    allowed, msg = reg.gate(entry, "long_draft")
    assert not allowed
    allowed, msg = reg.gate(entry, "long_draft", force=True)
    assert allowed and msg


def test_verify_reports_corruption(model_dir):
    good = build_gguf(model_dir / "ok.gguf", params=1_000_000_000)
    bad = build_gguf(model_dir / "bad.gguf", params=1_000_000_000)
    bad.write_bytes(b"NOTA" + bad.read_bytes()[4:])
    reg = _reg(model_dir)
    ok = reg.verify(good)
    assert ok["valid"] is True and ok["magic"] is True and ok["error"] == ""
    assert ok["sha256"] and len(ok["sha256"]) == 64

    broken = reg.verify(bad)
    assert broken["valid"] is False
    assert broken["magic"] is False and "magic" in broken["error"]


def test_mark_used_and_record_probe_persist(model_dir):
    p = build_gguf(model_dir / "used.gguf", params=1_000_000_000)
    reg = _reg(model_dir)
    reg.scan()
    reg.mark_used(p)
    reg.record_probe(p, {"ok": True, "score": 0.9})

    reg2 = _reg(model_dir)
    entry = reg2.get(p)
    assert entry is not None
    assert entry["last_used"] and entry["probe"]["ok"] is True
