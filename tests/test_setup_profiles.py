from __future__ import annotations

from winkg.setup_wizard import (
    ADVANCED,
    DB_LABEL_TO_VALUE,
    EMBED_LABEL_TO_VALUE,
    LLM_LABEL_TO_VALUE,
    PROFILE_VALUES,
)


def test_install_profiles_keep_database_embeddings_and_llm_independent():
    assert PROFILE_VALUES["Vollständig lokal (ohne verpflichtendes LLM)"] == (
        "local", "fastembed", "none"
    )
    assert PROFILE_VALUES["Lokale Datenbank + externe KI"] == (
        "local", "fastembed", "openai-compatible"
    )
    assert PROFILE_VALUES["Neo4j Aura + lokale Embeddings + externe KI"] == (
        "aura", "fastembed", "openai-compatible"
    )
    assert PROFILE_VALUES["Alles lokal inklusive LLM"] == (
        "local", "fastembed", "ollama-local"
    )
    assert DB_LABEL_TO_VALUE["Externe Neo4j-Instanz"] == "external"
    assert EMBED_LABEL_TO_VALUE["Externe Embedding-API"] == "openai"
    assert LLM_LABEL_TO_VALUE["Keine LLM-Anbindung"] == "none"


def test_ollama_is_not_required_when_no_local_llm_is_selected():
    from winkg.config import Settings
    from winkg.ollama_runtime import LocalOllamaManager

    # Must return without checking the OS, accessing the network or writing files.
    LocalOllamaManager(Settings(llm_provider="none")).ensure_ready()


def test_extraction_cache_retention_is_configurable_in_setup_wizard():
    assert ADVANCED["EXTRACTION_CACHE_RETENTION_DAYS"] == (
        "Aufbewahrung Extraktions-Cache (Tage, 0 = deaktiviert)", "90", "int"
    )
