from __future__ import annotations

from pathlib import Path

from winkg import config


def test_fresh_configuration_defaults_to_local_embeddings_without_llm(monkeypatch, tmp_path: Path):
    env_file = tmp_path / "settings.env"
    monkeypatch.setattr(config, "ENV_FILE", env_file)
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "MODEL_CACHE_DIR", tmp_path / "models")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setenv("NEO4J_URI", "neo4j+s://should-not-leak.example")

    settings = config.get_settings()

    assert settings.neo4j_mode == "external"
    assert settings.neo4j_uri == "neo4j://127.0.0.1:7687"
    assert settings.embedding_provider == "fastembed"
    assert settings.embedding_base_url == ""  # no implicit Ollama dependency
    assert settings.embedding_model == "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    assert settings.llm_provider == "none"
    assert settings.llm_base_url == ""
    assert settings.llm_enabled is False
    assert settings.community_summaries_enabled is False
    assert settings.global_search_enabled is False


def test_update_config_roundtrips_values_and_preserves_unrelated_settings(monkeypatch, tmp_path: Path):
    env_file = tmp_path / "settings.env"
    config_dir = tmp_path / "config"
    monkeypatch.setattr(config, "ENV_FILE", env_file)
    monkeypatch.setattr(config, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(config, "MODEL_CACHE_DIR", tmp_path / "models")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    env_file.write_text("CHUNK_SIZE=2400\nNEO4J_MODE=external\n", encoding="utf-8")

    config.update_config({"NEO4J_URI": "neo4j+s://example.databases.neo4j.io", "LLM_PROVIDER": "openai-compatible"})
    settings = config.get_settings()

    assert settings.neo4j_uri == "neo4j+s://example.databases.neo4j.io"
    assert settings.llm_provider == "openai-compatible"
    assert settings.chunk_size == 2400
    assert "NEO4J_URI=neo4j+s://example.databases.neo4j.io" in env_file.read_text(encoding="utf-8")


def test_validate_accepts_fastembed_and_disabled_llm(monkeypatch, tmp_path: Path):
    env_file = tmp_path / "settings.env"
    env_file.write_text(
        "NEO4J_MODE=external\nNEO4J_URI=neo4j://localhost:7687\nNEO4J_USERNAME=neo4j\n"
        "NEO4J_PASSWORD=test-password\nEMBEDDING_PROVIDER=fastembed\nLLM_PROVIDER=none\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "ENV_FILE", env_file)
    settings = config.get_settings()
    settings.validate_neo4j()
    assert settings.embedding_provider == "fastembed"
    assert settings.llm_enabled is False


def test_validate_accepts_managed_ollama_llm(monkeypatch, tmp_path: Path):
    env_file = tmp_path / "settings.env"
    env_file.write_text(
        "NEO4J_MODE=external\nNEO4J_URI=neo4j://localhost:7687\nNEO4J_USERNAME=neo4j\n"
        "NEO4J_PASSWORD=test-password\nEMBEDDING_PROVIDER=fastembed\n"
        "LLM_PROVIDER=ollama-local\nLLM_BASE_URL=http://127.0.0.1:11435/v1\nLLM_MODEL=qwen2.5:3b\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "ENV_FILE", env_file)
    settings = config.get_settings()
    settings.validate_neo4j()
    assert settings.llm_enabled is True
    assert settings.llm_provider == "ollama-local"
