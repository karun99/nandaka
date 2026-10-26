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


def test_ollama_backend_via_env(nandaka_home, monkeypatch):
    """FR-GGUF-13: on Termux the backend and its host are env-configurable."""
    monkeypatch.setenv("NANDAKA_BACKEND", "ollama")
    monkeypatch.setenv("NANDAKA_OLLAMA_HOST", "http://192.168.1.24:11434")
    cfg = Config.load()
    assert cfg.backend == "ollama"
    assert cfg.using_ollama() is True
    assert cfg.ollama_host == "http://192.168.1.24:11434"


def test_ollama_is_opt_in(nandaka_home):
    """Default stays llama_cpp: selecting Ollama is never implied."""
    cfg = Config.load()
    assert cfg.backend == "llama_cpp"
    assert cfg.using_ollama() is False
    assert cfg.ollama_host == "http://127.0.0.1:11434"


def test_using_ollama_tolerates_spelling(nandaka_home):
    """A hand-edited config.toml saying 'Ollama ' must still select Ollama."""
    cfg = Config.load()
    for spelling in ("ollama", "OLLAMA", " Ollama "):
        cfg.backend = spelling
        assert cfg.using_ollama() is True, spelling
    for spelling in ("llama_cpp", "", "ollamaish"):
        cfg.backend = spelling
        assert cfg.using_ollama() is False, spelling


def test_ollama_host_from_config_file(nandaka_home, tmp_path):
    """config.toml is the other supported route, and env still wins over it."""
    import os
    p = tmp_path / "c.toml"
    p.write_text('backend = "ollama"\nollama_host = "http://10.0.0.5:11434"\n',
                 encoding="utf-8")
    cfg = Config.load(p)
    assert cfg.using_ollama() is True
    assert cfg.ollama_host == "http://10.0.0.5:11434"

    os.environ["NANDAKA_OLLAMA_HOST"] = "http://from-env:11434"
    try:
        cfg2 = Config.load(p)
        assert cfg2.ollama_host == "http://from-env:11434"   # env precedence
    finally:
        del os.environ["NANDAKA_OLLAMA_HOST"]


def test_config_file_read(nandaka_home, tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('cloud_enabled = true\ncloud_provider = "openrouter"\n'
                 'ram_budget_mb = 640\n', encoding="utf-8")
    cfg = Config.load(p)
    assert cfg.cloud_enabled is True
    assert cfg.cloud_provider == "openrouter"
    assert cfg.ram_budget_mb == 640
