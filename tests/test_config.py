"""Config tests: flat-TOML parsing, env precedence, NFR-D-03 n_ctx rule."""

from nandaka.config import Config, parse_flat_toml


def test_flat_toml_parser():
    data = parse_flat_toml("""
# comment
ram_budget_mb = 950
cloud_enabled = true
work_dir = "/tmp/out"
temperature = 0.6
[sources]
ncert = "https://ncert.nic.in"
""")
    assert data["ram_budget_mb"] == 950
    assert data["cloud_enabled"] is True
    assert data["work_dir"] == "/tmp/out"
    assert data["temperature"] == 0.6
    assert data["sources"]["ncert"] == "https://ncert.nic.in"


def test_env_overrides_and_paths(nandaka_home, monkeypatch):
    monkeypatch.setenv("NANDAKA_RAM_BUDGET_MB", "750")
    cfg = Config.load()
    assert cfg.ram_budget_mb == 750
    assert cfg.model_dir.endswith("models")
    assert cfg.registry_path.endswith("registry.json")


def test_n_ctx_rule(nandaka_home):
    """NFR-D-03: default 1024 when the budget is <= 1 GB, else scales."""
    cfg = Config.load()
    cfg.n_ctx = 0
    cfg.ram_budget_mb = 1024
    assert cfg.effective_n_ctx() == 1024
    cfg.ram_budget_mb = 4096
    assert cfg.effective_n_ctx() == 8192   # scales at 2048 tokens per GB
    cfg.n_ctx = 2048
    assert cfg.effective_n_ctx() == 2048  # explicit value wins


def test_cloud_off_by_default_and_gated(nandaka_home):
    cfg = Config.load()
    assert cfg.cloud_enabled is False            # FR-CLOUD-01
    assert cfg.cloud_provider == "nvidia"        # Sarvam replaced
    base, key = cfg.cloud_credentials()
    assert "nvidia" in base and key == ""
    cfg.cloud_provider = "openrouter"
    base, _ = cfg.cloud_credentials()
    assert "openrouter" in base


def test_config_file_read(nandaka_home, tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('cloud_enabled = true\ncloud_provider = "openrouter"\n'
                 'ram_budget_mb = 640\n', encoding="utf-8")
    cfg = Config.load(p)
    assert cfg.cloud_enabled is True
    assert cfg.cloud_provider == "openrouter"
    assert cfg.ram_budget_mb == 640
