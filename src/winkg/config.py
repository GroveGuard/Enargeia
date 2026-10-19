from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"

# Use only this project's .env. Do not inherit connection settings from Windows
# or from the parent process environment.
if not ENV_FILE.is_file():
    raise FileNotFoundError(
        f"Projekt-Konfiguration fehlt: {ENV_FILE}. "
        "Lege im Projektverzeichnis eine .env-Datei an."
    )

_ENV = dotenv_values(ENV_FILE, interpolate=False)


def _get(name: str, default: str = "") -> str:
    value = _ENV.get(name)
    return default if value is None else str(value)


def _bool(name: str, default: bool = False) -> bool:
    value = _get(name, "false")
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "ja"}


def _int(name: str, default: int, minimum: int | None = None) -> int:
    raw = _get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} muss eine Ganzzahl sein; erhalten: {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} muss mindestens {minimum} sein.")
    return value


@dataclass(frozen=True)
class Settings:
    neo4j_uri: str = _get("NEO4J_URI", "neo4j://localhost:7687")
    neo4j_username: str = _get("NEO4J_USERNAME", "neo4j")
    neo4j_password: str = _get("NEO4J_PASSWORD", "")
    neo4j_database: str = _get("NEO4J_DATABASE", "neo4j")

    embedding_provider: str = _get("EMBEDDING_PROVIDER", "ollama").strip().lower()
    embedding_base_url: str = _get("EMBEDDING_BASE_URL", "http://localhost:11434").rstrip("/")
    embedding_model: str = _get("EMBEDDING_MODEL", "nomic-embed-text")
    embedding_api_key: str = _get("EMBEDDING_API_KEY", "")
    embedding_timeout_seconds: int = _int("EMBEDDING_TIMEOUT_SECONDS", 180, 1)

    llm_base_url: str = _get("LLM_BASE_URL", "http://localhost:11434/v1").rstrip("/")
    llm_model: str = _get("LLM_MODEL", "qwen2.5:7b")
    llm_api_key: str = _get("LLM_API_KEY", "ollama")
    llm_timeout_seconds: int = _int("LLM_TIMEOUT_SECONDS", 180, 1)
    entity_extraction_enabled: bool = _bool("ENTITY_EXTRACTION_ENABLED", False)
    autonomous_quality_enabled: bool = _bool("AUTONOMOUS_QUALITY_ENABLED", True)
    quality_max_candidates_per_run: int = _int("QUALITY_MAX_CANDIDATES_PER_RUN", 20, 1)
    quality_batch_size: int = _int("QUALITY_BATCH_SIZE", 5, 1)
    quality_min_confidence: float = float(_get("QUALITY_MIN_CONFIDENCE", "0.82"))
    community_summaries_enabled: bool = _bool("COMMUNITY_SUMMARIES_ENABLED", True)
    community_summary_min_members: int = _int("COMMUNITY_SUMMARY_MIN_MEMBERS", 3, 2)
    community_summary_max_per_run: int = _int("COMMUNITY_SUMMARY_MAX_PER_RUN", 8, 1)
    community_summary_batch_size: int = _int("COMMUNITY_SUMMARY_BATCH_SIZE", 2, 1)
    global_search_enabled: bool = _bool("GLOBAL_SEARCH_ENABLED", True)

    chunk_size: int = _int("CHUNK_SIZE", 1800, 200)
    chunk_overlap: int = _int("CHUNK_OVERLAP", 220, 0)
    embedding_batch_size: int = _int("EMBEDDING_BATCH_SIZE", 32, 1)
    max_file_size_mb: int = _int("MAX_FILE_SIZE_MB", 80, 1)
    max_chunks_per_file: int = _int("MAX_CHUNKS_PER_FILE", 500, 1)
    gds_max_entity_nodes: int = _int("GDS_MAX_ENTITY_NODES", 50000, 100)
    gds_max_node_similarity_nodes: int = _int("GDS_MAX_NODE_SIMILARITY_NODES", 15000, 100)
    gds_max_knn_chunks: int = _int("GDS_MAX_KNN_CHUNKS", 10000, 100)
    log_level: str = _get("LOG_LEVEL", "INFO").upper()
    include_sensitive_files: bool = _bool("INCLUDE_SENSITIVE_FILES", False)
    use_docling: bool = _bool("USE_DOCLING", False)
    extra_excluded_dirs: tuple[str, ...] = tuple(
        item.strip().casefold()
        for item in _get("EXTRA_EXCLUDED_DIRS", "").split(";")
        if item.strip() and item.strip() != "."
    )

    @property
    def embedding_signature(self) -> str:
        if self.embedding_provider == "none":
            return "none"
        return f"{self.embedding_provider}:{self.embedding_model}"

    @property
    def max_file_size_bytes(self) -> int:
        return self.max_file_size_mb * 1024 * 1024

    def validate_neo4j(self) -> None:
        if not self.neo4j_password:
            raise ValueError(
                "NEO4J_PASSWORD ist leer. Lege eine .env-Datei aus .env.example an "
                "und trage das Passwort deiner Neo4j-Instanz ein."
            )

        if self.embedding_provider not in {"ollama", "openai", "none"}:
            raise ValueError("EMBEDDING_PROVIDER muss ollama, openai oder none sein.")
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("CHUNK_OVERLAP muss kleiner als CHUNK_SIZE sein.")
        if not 0.0 <= self.quality_min_confidence <= 1.0:
            raise ValueError("QUALITY_MIN_CONFIDENCE muss zwischen 0.0 und 1.0 liegen.")


def get_settings() -> Settings:
    """Create settings from the project .env file only."""
    # Re-read the project file so edits are reflected without consulting process env.
    global _ENV
    _ENV = dotenv_values(ENV_FILE, interpolate=False)
    return Settings(
        neo4j_uri=_get("NEO4J_URI", "neo4j://localhost:7687"),
        neo4j_username=_get("NEO4J_USERNAME", "neo4j"),
        neo4j_password=_get("NEO4J_PASSWORD", ""),
        neo4j_database=_get("NEO4J_DATABASE", "neo4j"),
        embedding_provider=_get("EMBEDDING_PROVIDER", "ollama").strip().lower(),
        embedding_base_url=_get("EMBEDDING_BASE_URL", "http://localhost:11434").rstrip("/"),
        embedding_model=_get("EMBEDDING_MODEL", "nomic-embed-text"),
        embedding_api_key=_get("EMBEDDING_API_KEY", ""),
        embedding_timeout_seconds=_int("EMBEDDING_TIMEOUT_SECONDS", 180, 1),
        llm_base_url=_get("LLM_BASE_URL", "http://localhost:11434/v1").rstrip("/"),
        llm_model=_get("LLM_MODEL", "qwen2.5:7b"),
        llm_api_key=_get("LLM_API_KEY", "ollama"),
        llm_timeout_seconds=_int("LLM_TIMEOUT_SECONDS", 180, 1),
        entity_extraction_enabled=_bool("ENTITY_EXTRACTION_ENABLED", False),
        autonomous_quality_enabled=_bool("AUTONOMOUS_QUALITY_ENABLED", True),
        quality_max_candidates_per_run=_int("QUALITY_MAX_CANDIDATES_PER_RUN", 20, 1),
        quality_batch_size=_int("QUALITY_BATCH_SIZE", 5, 1),
        quality_min_confidence=float(_get("QUALITY_MIN_CONFIDENCE", "0.82")),
        community_summaries_enabled=_bool("COMMUNITY_SUMMARIES_ENABLED", True),
        community_summary_min_members=_int("COMMUNITY_SUMMARY_MIN_MEMBERS", 3, 2),
        community_summary_max_per_run=_int("COMMUNITY_SUMMARY_MAX_PER_RUN", 8, 1),
        community_summary_batch_size=_int("COMMUNITY_SUMMARY_BATCH_SIZE", 2, 1),
        global_search_enabled=_bool("GLOBAL_SEARCH_ENABLED", True),
        chunk_size=_int("CHUNK_SIZE", 1800, 200),
        chunk_overlap=_int("CHUNK_OVERLAP", 220, 0),
        embedding_batch_size=_int("EMBEDDING_BATCH_SIZE", 32, 1),
        max_file_size_mb=_int("MAX_FILE_SIZE_MB", 80, 1),
        max_chunks_per_file=_int("MAX_CHUNKS_PER_FILE", 500, 1),
        gds_max_entity_nodes=_int("GDS_MAX_ENTITY_NODES", 50000, 100),
        gds_max_node_similarity_nodes=_int("GDS_MAX_NODE_SIMILARITY_NODES", 15000, 100),
        gds_max_knn_chunks=_int("GDS_MAX_KNN_CHUNKS", 10000, 100),
        log_level=_get("LOG_LEVEL", "INFO").upper(),
        include_sensitive_files=_bool("INCLUDE_SENSITIVE_FILES", False),
        use_docling=_bool("USE_DOCLING", False),
        extra_excluded_dirs=tuple(
            item.strip().casefold()
            for item in _get("EXTRA_EXCLUDED_DIRS", "").split(";")
            if item.strip() and item.strip() != "."
        ),
    )
