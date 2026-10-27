from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
import sys

# The indexer test injects a fake store and doesn't connect to Neo4j. Allow the
# test suite to run in lightweight environments where the Neo4j driver isn't installed.
try:
    import neo4j  # noqa: F401
except ImportError:
    neo4j_stub = ModuleType("neo4j")
    neo4j_stub.GraphDatabase = SimpleNamespace(driver=None)  # type: ignore[attr-defined]
    sys.modules["neo4j"] = neo4j_stub

from winkg.config import get_settings
from winkg.indexer import DirectoryIndexer


class FakeStore:
    def __init__(self) -> None:
        self.saved: dict[str, Any] | None = None
        self.file_states: dict[str, dict[str, Any]] = {}
        self.extraction_cache: dict[str, dict[str, Any]] = {}
        self.entity_saves: list[tuple[str, list[dict[str, str]], list[dict[str, str]]]] = []

    def root_id_for(self, path: Path) -> str:
        return "root-test"

    def file_id_for(self, root_id: str, relative_path: str) -> str:
        return "file-test"

    def ensure_schema(self) -> None:
        pass

    def register_root(self, root_id: str, root_path: Path, embedding_signature: str) -> None:
        pass

    def upsert_folders(self, root_id: str, root_path: Path, folders: Any, scan_id: str) -> None:
        pass

    def get_file_state(self, root_id: str) -> dict[str, dict[str, Any]]:
        return self.file_states

    def get_entity_extraction_cache_many(self, cache_ids: list[str]) -> dict[str, dict[str, Any]]:
        return {cache_id: self.extraction_cache[cache_id] for cache_id in cache_ids if cache_id in self.extraction_cache}

    def save_entity_extraction_cache_many(self, entries: list[dict[str, Any]]) -> int:
        for entry in entries:
            self.extraction_cache[entry["id"]] = entry["result"]
        return len(entries)

    def save_file(self, root_id: str, root_path: Path, relative_path: str,
                  parent_relative_path: str, metadata: dict[str, Any],
                  chunks: list[dict[str, Any]]) -> None:
        self.saved = {"metadata": metadata, "chunks": chunks}
        self.file_states[relative_path] = {
            **metadata,
            "chunks_count": len(chunks),
        }

    def save_entities_for_chunk(self, root_id: str, chunk_id: str,
                                entities: list[dict[str, str]],
                                relations: list[dict[str, str]]) -> None:
        self.entity_saves.append((chunk_id, entities, relations))

    def mark_files_seen(self, root_id: str, relative_paths: list[str], scan_id: str) -> None:
        pass

    def cleanup_removed(self, root_id: str, scan_id: str) -> dict[str, int]:
        return {"deleted_files": 0, "deleted_folders": 0}

    def invalidate_derived_analysis(self, root_id: str | None = None, reason: str = "source graph changed") -> None:
        pass

    def stats(self, root_id: str | None = None) -> dict[str, int]:
        return {"roots": 1, "folders": 1, "files": 1, "chunks": len(self.saved["chunks"]) if self.saved else 0,
                "entities": 0, "truncated_files": 1 if self.saved and self.saved["metadata"]["is_truncated"] else 0}


def test_indexer_caps_chunks_and_persists_deep_analysis_marker(tmp_path: Path):
    source = tmp_path / "long_document.md"
    source.write_text(("SAP Materialstamm: Einkaufsdaten, Disposition und Buchhaltung.\n\n") * 350, encoding="utf-8")
    settings = replace(get_settings(), max_chunks_per_file=3, chunk_size=250, chunk_overlap=20)
    store = FakeStore()

    indexer = DirectoryIndexer(settings, store=store)
    try:
        result = indexer.index(tmp_path, no_embeddings=True)
    finally:
        # FakeStore has no network resources; don't invoke the production close method.
        pass

    assert store.saved is not None
    meta = store.saved["metadata"]
    assert len(store.saved["chunks"]) == 3
    assert meta["is_truncated"] is True
    assert meta["needs_deep_analysis"] is True
    assert meta["indexed_chunks"] == 3
    assert meta["total_chunks"] > 3
    assert meta["omitted_chunks"] == meta["total_chunks"] - 3
    assert meta["status"] == "content_only_truncated"
    assert result["truncated_files"] == 1
    assert result["omitted_chunks"] == meta["omitted_chunks"]


def test_indexer_flags_files_above_max_file_size_for_deep_analysis(tmp_path: Path):
    source = tmp_path / "large_document.txt"
    source.write_bytes(b"x" * (1024 * 1024 + 1))
    settings = replace(get_settings(), max_file_size_mb=1)
    store = FakeStore()

    indexer = DirectoryIndexer(settings, store=store)
    result = indexer.index(tmp_path, no_embeddings=True)

    assert store.saved is not None
    meta = store.saved["metadata"]
    assert meta["status"] == "too_large"
    assert meta["needs_deep_analysis"] is True
    assert "MAX_FILE_SIZE_MB=1" in meta["truncation_reason"]
    assert store.saved["chunks"] == []
    assert result["too_large"] == 1
    assert result["truncated_files"] == 1


def test_entity_extraction_cache_reuses_identical_chunks_across_files(tmp_path: Path):
    text = "Material M-4711 wird in Prozess P-100 verwendet und in Dokumentation X beschrieben."
    (tmp_path / "a.txt").write_text(text, encoding="utf-8")
    (tmp_path / "b.txt").write_text(text, encoding="utf-8")
    settings = replace(get_settings(), entity_extraction_enabled=True, chunk_size=500, chunk_overlap=20)
    store = FakeStore()
    indexer = DirectoryIndexer(settings, store=store)
    calls: list[str] = []

    def fake_extract(value: str) -> dict[str, list[dict[str, str]]]:
        calls.append(value)
        return {
            "entities": [{"name": "M-4711", "type": "Produkt", "description": "Material"}],
            "relations": [],
        }

    indexer.entity_extractor.extract = fake_extract  # type: ignore[method-assign]
    try:
        result = indexer.index(tmp_path, no_embeddings=True)
    finally:
        pass

    assert len(calls) == 1
    assert result["entity_llm_calls"] == 1
    assert result["entity_cache_misses"] == 1
    assert result["entity_cache_hits"] == 1
    assert len(store.extraction_cache) == 1
    assert len(store.entity_saves) == 2


def test_failed_entity_extraction_retries_on_incremental_scan(tmp_path: Path):
    source = tmp_path / "retry.txt"
    source.write_text("Material M-4711 wird im Prozess P-100 verwendet.", encoding="utf-8")
    settings = replace(get_settings(), entity_extraction_enabled=True, chunk_size=500, chunk_overlap=20)
    store = FakeStore()
    indexer = DirectoryIndexer(settings, store=store)
    calls = 0

    def fail_once(value: str) -> dict[str, list[dict[str, str]]]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("invalid JSON response")
        return {
            "entities": [{"name": "M-4711", "type": "Produkt", "description": "Material"}],
            "relations": [],
        }

    indexer.entity_extractor.extract = fail_once  # type: ignore[method-assign]
    first = indexer.index(tmp_path, no_embeddings=True)
    assert first["entity_llm_calls"] == 1
    assert store.file_states["retry.txt"]["entity_extraction_pending"] is True

    second = indexer.index(tmp_path, no_embeddings=True)
    assert second["entity_extraction_retry_files"] == 1
    assert second["entity_llm_calls"] == 1
    assert store.file_states["retry.txt"]["entity_extraction_pending"] is False
    assert calls == 2

    third = indexer.index(tmp_path, no_embeddings=True)
    assert third["unchanged"] == 1
    assert third["entity_llm_calls"] == 0
    assert calls == 2


def test_legacy_index_without_extraction_state_gets_entity_extraction(tmp_path: Path):
    source = tmp_path / "legacy.txt"
    source.write_text("Material M-4711 wird im Prozess P-100 verwendet.", encoding="utf-8")
    settings = replace(get_settings(), entity_extraction_enabled=False, chunk_size=500, chunk_overlap=20)
    store = FakeStore()
    indexer = DirectoryIndexer(settings, store=store)
    indexer.index(tmp_path, no_embeddings=True)
    assert store.file_states["legacy.txt"]["entity_extraction_pending"] is None

    indexer.settings = replace(settings, entity_extraction_enabled=True)
    calls: list[str] = []

    def fake_extract(value: str) -> dict[str, list[dict[str, str]]]:
        calls.append(value)
        return {"entities": [], "relations": []}

    indexer.entity_extractor.extract = fake_extract  # type: ignore[method-assign]
    result = indexer.index(tmp_path, no_embeddings=True)

    assert result["entity_extraction_retry_files"] == 1
    assert result["entity_llm_calls"] == 1
    assert store.file_states["legacy.txt"]["entity_extraction_pending"] is False
    assert len(calls) == 1
