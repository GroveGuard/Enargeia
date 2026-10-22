from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

IS_FROZEN = bool(getattr(sys, "frozen", False))
PROJECT_ROOT = Path(sys.executable).resolve().parent if IS_FROZEN else Path(__file__).resolve().parents[2]
RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", PROJECT_ROOT))
_LOCAL_APP_DATA = (Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) if os.name == "nt" else Path.home() / ".local" / "share")
APP_DATA_DIR = _LOCAL_APP_DATA / "Enargeia"
CONFIG_DIR = APP_DATA_DIR / "config"
CONFIG_FILE = CONFIG_DIR / "settings.env"
MODEL_CACHE_DIR = APP_DATA_DIR / "models"
LOG_DIR = APP_DATA_DIR / "logs"

# Source checkouts may keep their existing project-local .env. Frozen distributions
# always use per-user config, and a fresh source checkout falls back to AppData.
_SOURCE_ENV = PROJECT_ROOT / ".env"
if not IS_FROZEN and _SOURCE_ENV.is_file():
    ENV_FILE = _SOURCE_ENV
elif CONFIG_FILE.is_file():
    ENV_FILE = CONFIG_FILE
else:
    ENV_FILE = CONFIG_FILE

# Compatibility alias for older modules.
CONFIG_PATH = ENV_FILE


def _read_env() -> dict[str, Any]:
    if not ENV_FILE.is_file():
        return {}
    return dotenv_values(ENV_FILE, interpolate=False)


def _get(values: dict[str, Any], name: str, default: str = "") -> str:
    value = values.get(name)
    return default if value is None else str(value)


def _bool(values: dict[str, Any], name: str, default: bool = False) -> bool:
    value = _get(values, name, "true" if default else "false")
    return value.strip().lower() in {"1", "true", "yes", "on", "ja"}


def _int(values: dict[str, Any], name: str, default: int, minimum: int | None = None) -> int:
    raw = _get(values, name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} muss eine Ganzzahl sein; erhalten: {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} muss mindestens {minimum} sein.")
    return value


def _float(values: dict[str, Any], name: str, default: float) -> float:
    raw = _get(values, name, str(default))
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} muss eine Zahl sein; erhalten: {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    neo4j_mode: str = "external"  # local | aura | external
    neo4j_uri: str = "neo4j://127.0.0.1:7687"
    neo4j_username: str = "neo4j"
    neo4j_password: str = ""
    neo4j_database: str = "neo4j"
    neo4j_local_port: int = 17687
    gds_enabled: bool = True

    embedding_provider: str = "fastembed"  # fastembed | ollama | openai | none
    embedding_base_url: str = ""
    embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    embedding_api_key: str = ""
    embedding_timeout_seconds: int = 180
    embedding_threads: int = 0

    llm_provider: str = "none"  # none | ollama-local | ollama | openai-compatible
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str = ""
    llm_timeout_seconds: int = 180
    entity_extraction_enabled: bool = False
    autonomous_quality_enabled: bool = False
    quality_max_candidates_per_run: int = 20
    quality_batch_size: int = 5
    quality_min_confidence: float = 0.82
    community_summaries_enabled: bool = False
    community_summary_min_members: int = 3
    community_summary_max_per_run: int = 8
    community_summary_batch_size: int = 2
    global_search_enabled: bool = False

    chunk_size: int = 1800
    chunk_overlap: int = 220
    embedding_batch_size: int = 32
    max_file_size_mb: int = 80
    max_chunks_per_file: int = 500
    gds_max_entity_nodes: int = 50000
    gds_max_node_similarity_nodes: int = 15000
    gds_max_knn_chunks: int = 10000
    log_level: str = "INFO"
    include_sensitive_files: bool = False
    include_ignored_dirs: bool = False
    use_docling: bool = False
    extra_excluded_dirs: tuple[str, ...] = ()

    @property
    def embedding_signature(self) -> str:
        if self.embedding_provider == "none":
            return "none"
        return f"{self.embedding_provider}:{self.embedding_model}"

    @property
    def max_file_size_bytes(self) -> int:
        return self.max_file_size_mb * 1024 * 1024

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_base_url.strip() and self.llm_model.strip() and self.llm_provider != "none")

    def validate_neo4j(self) -> None:
        if self.neo4j_mode not in {"local", "aura", "external"}:
            raise ValueError("NEO4J_MODE muss local, aura oder external sein.")
        if not self.neo4j_uri.strip():
            raise ValueError("NEO4J_URI fehlt. Bitte die Datenbankverbindung konfigurieren.")
        if not self.neo4j_username.strip() or not self.neo4j_password:
            raise ValueError(
                "Neo4j-Zugangsdaten fehlen. Öffne 'Verbindungen & Dienste' bzw. "
                f"den Konfigurationsassistenten: {ENV_FILE}"
            )
        if self.embedding_provider not in {"fastembed", "ollama", "openai", "none"}:
            raise ValueError("EMBEDDING_PROVIDER muss fastembed, ollama, openai oder none sein.")
        if self.llm_provider not in {"none", "ollama-local", "ollama", "openai-compatible"}:
            raise ValueError("LLM_PROVIDER muss none, ollama-local, ollama oder openai-compatible sein.")
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("CHUNK_OVERLAP muss kleiner als CHUNK_SIZE sein.")
        if not 0.0 <= self.quality_min_confidence <= 1.0:
            raise ValueError("QUALITY_MIN_CONFIDENCE muss zwischen 0.0 und 1.0 liegen.")


_SECRET_KEYS = {"NEO4J_PASSWORD", "EMBEDDING_API_KEY", "LLM_API_KEY"}
_KEYRING_SERVICE = "Enargeia"


def _resolve_secret(name: str, value: str) -> str:
    if not value.startswith("keyring://"):
        return value
    username = value.removeprefix("keyring://")
    try:
        import keyring
        secret = keyring.get_password(_KEYRING_SERVICE, username)
    except Exception as exc:
        raise ValueError(
            f"Das gespeicherte Geheimnis {name} konnte nicht aus dem Windows Credential Manager gelesen werden. "
            "Öffne den Setup-Assistenten und speichere die Verbindung erneut."
        ) from exc
    if secret is None:
        raise ValueError(f"Das gespeicherte Geheimnis {name} fehlt im Windows Credential Manager.")
    return secret


def get_settings() -> Settings:
    """Read only this application's selected configuration file, never ambient env vars."""
    values = _read_env()
    secret_values = {key: _resolve_secret(key, _get(values, key, "")) for key in _SECRET_KEYS}
    return Settings(
        neo4j_mode=_get(values, "NEO4J_MODE", "external").strip().lower(),
        neo4j_uri=_get(values, "NEO4J_URI", "neo4j://127.0.0.1:7687").strip(),
        neo4j_username=_get(values, "NEO4J_USERNAME", "neo4j"),
        neo4j_password=secret_values["NEO4J_PASSWORD"],
        neo4j_database=_get(values, "NEO4J_DATABASE", "neo4j"),
        neo4j_local_port=_int(values, "NEO4J_LOCAL_PORT", 17687, 1),
        gds_enabled=_bool(values, "GDS_ENABLED", True),
        embedding_provider=_get(values, "EMBEDDING_PROVIDER", "fastembed").strip().lower(),
        embedding_base_url=_get(values, "EMBEDDING_BASE_URL", "").rstrip("/"),
        embedding_model=_get(values, "EMBEDDING_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"),
        embedding_api_key=secret_values["EMBEDDING_API_KEY"],
        embedding_timeout_seconds=_int(values, "EMBEDDING_TIMEOUT_SECONDS", 180, 1),
        embedding_threads=_int(values, "EMBEDDING_THREADS", 0, 0),
        llm_provider=_get(values, "LLM_PROVIDER", "none").strip().lower(),
        llm_base_url=_get(values, "LLM_BASE_URL", "").rstrip("/"),
        llm_model=_get(values, "LLM_MODEL", ""),
        llm_api_key=secret_values["LLM_API_KEY"],
        llm_timeout_seconds=_int(values, "LLM_TIMEOUT_SECONDS", 180, 1),
        entity_extraction_enabled=_bool(values, "ENTITY_EXTRACTION_ENABLED", False),
        autonomous_quality_enabled=_bool(values, "AUTONOMOUS_QUALITY_ENABLED", False),
        quality_max_candidates_per_run=_int(values, "QUALITY_MAX_CANDIDATES_PER_RUN", 20, 1),
        quality_batch_size=_int(values, "QUALITY_BATCH_SIZE", 5, 1),
        quality_min_confidence=_float(values, "QUALITY_MIN_CONFIDENCE", 0.82),
        community_summaries_enabled=_bool(values, "COMMUNITY_SUMMARIES_ENABLED", False),
        community_summary_min_members=_int(values, "COMMUNITY_SUMMARY_MIN_MEMBERS", 3, 2),
        community_summary_max_per_run=_int(values, "COMMUNITY_SUMMARY_MAX_PER_RUN", 8, 1),
        community_summary_batch_size=_int(values, "COMMUNITY_SUMMARY_BATCH_SIZE", 2, 1),
        global_search_enabled=_bool(values, "GLOBAL_SEARCH_ENABLED", False),
        chunk_size=_int(values, "CHUNK_SIZE", 1800, 200),
        chunk_overlap=_int(values, "CHUNK_OVERLAP", 220, 0),
        embedding_batch_size=_int(values, "EMBEDDING_BATCH_SIZE", 32, 1),
        max_file_size_mb=_int(values, "MAX_FILE_SIZE_MB", 80, 1),
        max_chunks_per_file=_int(values, "MAX_CHUNKS_PER_FILE", 500, 1),
        gds_max_entity_nodes=_int(values, "GDS_MAX_ENTITY_NODES", 50000, 100),
        gds_max_node_similarity_nodes=_int(values, "GDS_MAX_NODE_SIMILARITY_NODES", 15000, 100),
        gds_max_knn_chunks=_int(values, "GDS_MAX_KNN_CHUNKS", 10000, 100),
        log_level=_get(values, "LOG_LEVEL", "INFO").upper(),
        include_sensitive_files=_bool(values, "INCLUDE_SENSITIVE_FILES", False),
        include_ignored_dirs=_bool(values, "INCLUDE_IGNORED_DIRS", False),
        use_docling=_bool(values, "USE_DOCLING", False),
        extra_excluded_dirs=tuple(
            item.strip().casefold()
            for item in _get(values, "EXTRA_EXCLUDED_DIRS", "").split(";")
            if item.strip() and item.strip() != "."
        ),
    )


def ensure_config_directory() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def update_config(updates: dict[str, str]) -> Path:
    """Atomically update settings; Windows secrets are kept in Credential Manager."""
    ensure_config_directory()
    current = {str(k): str(v) for k, v in _read_env().items() if v is not None}
    normalized_updates = {key: str(value) for key, value in updates.items()}
    for key in _SECRET_KEYS:
        if key not in normalized_updates:
            continue
        secret = normalized_updates[key]
        if secret.startswith("keyring://"):
            continue
        if os.name == "nt":
            try:
                import keyring
                if not secret:
                    try:
                        keyring.delete_password(_KEYRING_SERVICE, key)
                    except Exception:
                        pass
                    normalized_updates[key] = ""
                else:
                    keyring.set_password(_KEYRING_SERVICE, key, secret)
                    normalized_updates[key] = f"keyring://{key}"
            except Exception as exc:
                raise RuntimeError(
                    f"Das Geheimnis {key} konnte nicht sicher im Windows Credential Manager gespeichert werden. "
                    "Die Konfiguration wurde nicht geschrieben."
                ) from exc
    current.update(normalized_updates)

    def quote(value: str) -> str:
        if not value or any(char in value for char in '\n\r#"') or value != value.strip():
            return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r') + '"'
        return value

    lines = [f"{key}={quote(value)}" for key, value in sorted(current.items())]
    temporary = ENV_FILE.with_suffix(ENV_FILE.suffix + ".tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary.replace(ENV_FILE)
    return ENV_FILE
