from __future__ import annotations

import hashlib
import logging
import uuid
import time
from threading import Event
from pathlib import Path
from typing import Any, Callable

from .chunking import chunk_text_limited, stable_id
from .config import Settings
from .embeddings import EmbeddingClient
from .entity_extraction import EntityExtractor
from .extractors import SUPPORTED_EXTENSIONS, extract_text
from .neo4j_store import EmbeddingModelMismatch, Neo4jStore
from .scanner import scan_directory

log = logging.getLogger(__name__)


class DirectoryIndexer:
    def __init__(
        self,
        settings: Settings,
        store: Neo4jStore | None = None,
        embedding_progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.settings = settings
        self.store = store or Neo4jStore(settings)
        self.embedding_client = EmbeddingClient(settings, progress_callback=embedding_progress_callback)
        self.entity_extractor = EntityExtractor(settings)
        self._vector_index_dimensions: set[int] = set()

    def close(self) -> None:
        self.store.close()

    def index(self, root: Path, *, force: bool = False, rebuild: bool = False,
              no_embeddings: bool = False, extract_entities: bool | None = None,
              include_ignored: bool = False, include_sensitive: bool = False,
              cancel_event: Event | None = None) -> dict[str, Any]:
        snapshot = scan_directory(
            root, self.settings,
            include_ignored=include_ignored,
            include_sensitive=include_sensitive,
        )
        root = snapshot.root
        root_id = self.store.root_id_for(root)
        signature = "none" if no_embeddings else self.embedding_client.signature
        scan_id = uuid.uuid4().hex

        self.store.ensure_schema()
        if rebuild:
            # Validate other roots before destructive work, so a global embedding-model
            # mismatch cannot delete this root and then abort the rebuild.
            self.store.validate_other_root_signatures(root_id, signature)
            log.warning("--rebuild aktiv: Entferne den bisherigen Graphen dieses Verzeichnisses: %s", root)
            self.store.delete_root(root_id)
        self.store.register_root(root_id, root, signature)
        self.store.upsert_folders(root_id, root, snapshot.folders, scan_id)
        existing = self.store.get_file_state(root_id)

        stats: dict[str, Any] = {
            "root": str(root),
            "root_id": root_id,
            "discovered_files": len(snapshot.files),
            "discovered_folders": len(snapshot.folders),
            "indexed": 0,
            "unchanged": 0,
            "metadata_only": 0,
            "empty_content": 0,
            "too_large": 0,
            "embedding_missing": 0,
            "errors": 0,
            "chunks": 0,
            "truncated_files": 0,
            "omitted_chunks": 0,
            "entities_extracted": 0,
            "entity_cache_hits": 0,
            "entity_cache_misses": 0,
            "entity_llm_calls": 0,
            "entity_extraction_retry_files": 0,
            "skipped_sensitive": snapshot.skipped_sensitive,
            "scan_errors": snapshot.skipped_errors,
            "deleted_files": 0,
            "deleted_folders": 0,
        }
        do_entity_extraction = (self.settings.entity_extraction_enabled if extract_entities is None else bool(extract_entities))
        entity_rate_limited = False
        total = len(snapshot.files)
        started_at = time.monotonic()
        processed = 0

        for number, candidate in enumerate(snapshot.files, start=1):
            if cancel_event is not None and cancel_event.is_set():
                stats["cancelled"] = True
                log.warning("Scan-Abbruch angefordert; beende nach %d von %d Dateien. Bereits gespeicherte Dateien bleiben erhalten.", processed, total)
                break
            file_started_at = time.monotonic()
            log.info("Datei %d/%d gestartet: %s (%d Bytes, %s)", number, total, candidate.relative_path, candidate.size, candidate.extension or "ohne Endung")
            old = existing.get(candidate.relative_path)
            same_metadata = (
                old is not None
                and int(old.get("size") or -1) == candidate.size
                and int(old.get("mtime_ns") or -1) == candidate.mtime_ns
            )
            retry_failed = old is not None and old.get("status") in {"embedding_missing", "extraction_error"}
            retry_entity_extraction = (
                do_entity_extraction
                and old is not None
                and int(old.get("chunks_count") or 0) > 0
                and old.get("entity_extraction_pending") is not False
            )
            old_chunk_count = int(old.get("chunks_count") or 0) if old else 0
            previously_truncated = bool(old.get("needs_deep_analysis")) if old else False
            exceeds_new_cap = old is not None and old_chunk_count > self.settings.max_chunks_per_file
            cap_increased = (
                old is not None
                and previously_truncated
                and old_chunk_count < self.settings.max_chunks_per_file
            )
            if same_metadata and not force and not retry_failed and not retry_entity_extraction and not exceeds_new_cap and not cap_increased:
                stats["unchanged"] += 1
                if bool(old.get("needs_deep_analysis")):
                    stats["truncated_files"] += 1
                    stats["omitted_chunks"] += int(old.get("omitted_chunks") or 0)
                processed += 1
                if number % 5 == 0 or number == total:
                    elapsed = max(time.monotonic() - started_at, 0.001)
                    rate = processed / elapsed
                    eta_seconds = max(total - number, 0) / rate if rate > 0 else 0
                    log.info("Fortschritt %d/%d Dateien | neu/aktualisiert: %d | unverändert: %d | Chunks: %d | Ø %.2f Dateien/s | Rest ca. %.1f min",
                             number, total, stats["indexed"], stats["unchanged"], stats["chunks"], rate, eta_seconds / 60)
                continue

            if retry_entity_extraction:
                stats["entity_extraction_retry_files"] += 1
                log.info("Wiederhole fehlgeschlagene Entity-Extraktion für %s", candidate.relative_path)

            metadata: dict[str, Any] = {
                "size": candidate.size,
                "mtime_ns": candidate.mtime_ns,
                "extension": candidate.extension,
                "sha256": "",
                "status": "metadata_only",
                "error": "",
                "last_scan_id": scan_id,
                "is_truncated": False,
                "total_chunks": 0,
                "indexed_chunks": 0,
                "omitted_chunks": 0,
                "needs_deep_analysis": False,
                "truncation_reason": "",
                "original_text_chars": 0,
                "indexed_text_chars": 0,
                "entity_extraction_pending": (
                    False if do_entity_extraction
                    else old.get("entity_extraction_pending") if old is not None
                    else None
                ),
            }
            chunk_rows: list[dict[str, Any]] = []
            entity_payloads: list[tuple[str, dict[str, Any]]] = []
            try:
                if candidate.size > self.settings.max_file_size_bytes:
                    metadata.update({
                        "status": "too_large",
                        "needs_deep_analysis": True,
                        "truncation_reason": (
                            f"MAX_FILE_SIZE_MB={self.settings.max_file_size_mb}; "
                            f"Datei hat {candidate.size} Bytes und wurde nicht inhaltlich gelesen"
                        ),
                    })
                    stats["too_large"] += 1
                    stats["truncated_files"] += 1
                    log.warning(
                        "Datei überschreitet MAX_FILE_SIZE_MB und wurde nur als Metadaten gespeichert: "
                        "%s | Größe %d Bytes | Limit %d MB | needs_deep_analysis=true",
                        candidate.relative_path, candidate.size, self.settings.max_file_size_mb,
                    )
                elif candidate.extension not in SUPPORTED_EXTENSIONS:
                    metadata["status"] = "metadata_only"
                    stats["metadata_only"] += 1
                else:
                    log.info("Lese Dateiinhalt: %s", candidate.relative_path)
                    raw = candidate.path.read_bytes()
                    digest = hashlib.sha256(raw).hexdigest()
                    metadata["sha256"] = digest
                    log.info("Extrahiere Text: %s", candidate.relative_path)
                    text = extract_text(candidate.path, raw, self.settings)
                    if not text.strip():
                        metadata["status"] = "empty_content"
                        stats["empty_content"] += 1
                    else:
                        text_chunks, omitted_chunks = chunk_text_limited(
                            text,
                            size=self.settings.chunk_size,
                            overlap=self.settings.chunk_overlap,
                            maximum=self.settings.max_chunks_per_file,
                        )
                        total_chunks = len(text_chunks) + omitted_chunks
                        is_truncated = omitted_chunks > 0
                        metadata.update({
                            "is_truncated": is_truncated,
                            "total_chunks": total_chunks,
                            "indexed_chunks": len(text_chunks),
                            "omitted_chunks": omitted_chunks,
                            "needs_deep_analysis": is_truncated,
                            "truncation_reason": (
                                f"MAX_CHUNKS_PER_FILE={self.settings.max_chunks_per_file}"
                                if is_truncated else ""
                            ),
                            "original_text_chars": len(text),
                            "indexed_text_chars": sum(len(chunk.text) for chunk in text_chunks),
                        })
                        if is_truncated:
                            stats["truncated_files"] += 1
                            stats["omitted_chunks"] += omitted_chunks
                            log.warning(
                                "Datei wird für den Knowledge Graph gekürzt: %s | Chunks gesamt %d | indexiert %d | ausgelassen %d | ursprüngliche Zeichen %d | indexierte Zeichen %d | needs_deep_analysis=true",
                                candidate.relative_path, total_chunks, len(text_chunks), omitted_chunks,
                                len(text), metadata["indexed_text_chars"],
                            )
                        embeddings: list[list[float]] = []
                        if text_chunks and not no_embeddings and self.embedding_client.enabled:
                            log.info("Erzeuge Embeddings für %s (%d Chunks)", candidate.relative_path, len(text_chunks))
                            try:
                                batch_size = self.settings.embedding_batch_size
                                total_batches = (len(text_chunks) + batch_size - 1) // batch_size
                                embedding_started = time.monotonic()
                                embedded_count = 0
                                for batch_number, start in enumerate(range(0, len(text_chunks), batch_size), start=1):
                                    if cancel_event is not None and cancel_event.is_set():
                                        metadata["entity_extraction_pending"] = True
                                        break
                                    batch = text_chunks[start:start + batch_size]
                                    batch_started = time.monotonic()
                                    batch_vectors = self.embedding_client.embed_many([chunk.text for chunk in batch])
                                    batch_seconds = max(time.monotonic() - batch_started, 0.001)
                                    embeddings.extend(batch_vectors)
                                    embedded_count += len(batch_vectors)
                                    elapsed_embedding = max(time.monotonic() - embedding_started, 0.001)
                                    rate = embedded_count / elapsed_embedding
                                    remaining = max(len(text_chunks) - embedded_count, 0)
                                    eta_seconds = remaining / rate if rate > 0 else 0.0
                                    log.info(
                                        "Embedding-Fortschritt %s | Batch %d/%d | Chunks %d/%d | "
                                        "letzter Batch %.2fs (%.1f Chunks/s) | Ø %.1f Chunks/s | Rest ca. %.1f min",
                                        candidate.relative_path, batch_number, total_batches,
                                        embedded_count, len(text_chunks), batch_seconds,
                                        len(batch_vectors) / batch_seconds, rate, eta_seconds / 60,
                                    )
                                    if cancel_event is not None and cancel_event.is_set():
                                        break
                                if cancel_event is not None and cancel_event.is_set():
                                    stats["cancelled"] = True
                                    log.warning(
                                        "Scan pausiert während Embeddings für %s. Die aktuelle Datei wird nicht teilweise gespeichert; beim Fortsetzen wird sie mit dem Chunk-Limit erneut verarbeitet.",
                                        candidate.relative_path,
                                    )
                                    break
                                if embeddings:
                                    dimensions = len(embeddings[0])
                                    if dimensions not in self._vector_index_dimensions:
                                        self.store.ensure_vector_index(dimensions)
                                        self._vector_index_dimensions.add(dimensions)
                            except Exception as exc:
                                embeddings = []
                                metadata["status"] = "embedding_missing"
                                metadata["error"] = str(exc)[:1000]
                                stats["embedding_missing"] += 1
                                log.warning("Embeddings nicht verfügbar für %s: %s; Volltext bleibt indexiert.", candidate.relative_path, exc, exc_info=True)
                        elif text_chunks and (no_embeddings or not self.embedding_client.enabled):
                            metadata["status"] = "content_only"

                        file_id = self.store.file_id_for(root_id, candidate.relative_path)
                        for index, chunk in enumerate(text_chunks):
                            chunk_id = stable_id(file_id, str(chunk.ordinal), hashlib.sha256(chunk.text.encode("utf-8")).hexdigest())
                            props: dict[str, Any] = {
                                "id": chunk_id,
                                "root_id": root_id,
                                "file_id": file_id,
                                "path": str(candidate.path),
                                "relative_path": candidate.relative_path,
                                "filename": candidate.name,
                                "extension": candidate.extension,
                                "ordinal": chunk.ordinal,
                                "char_start": chunk.char_start,
                                "char_end": chunk.char_end,
                                "page": chunk.page,
                                "section": chunk.section or "",
                                "text": chunk.text,
                                "sha256": hashlib.sha256(chunk.text.encode("utf-8")).hexdigest(),
                            }
                            if index < len(embeddings):
                                props["embedding"] = embeddings[index]
                                props["embedding_model"] = self.embedding_client.signature
                            chunk_rows.append({"id": chunk_id, "props": props})
                        stats["chunks"] += len(chunk_rows)
                        if metadata["status"] == "content_only" and metadata["is_truncated"]:
                            metadata["status"] = "content_only_truncated"
                        elif metadata["status"] not in {"embedding_missing", "content_only"}:
                            if chunk_rows:
                                metadata["status"] = "indexed_truncated" if metadata["is_truncated"] else "indexed"
                            else:
                                metadata["status"] = "empty_content"
                        if do_entity_extraction and chunk_rows and not entity_rate_limited:
                            extractor_signature = self.entity_extractor.cache_signature
                            cache_ids_by_chunk: dict[str, str] = {}
                            cache_id_by_chunk_id: dict[str, str] = {}
                            for chunk_row in chunk_rows:
                                content_hash = str(chunk_row["props"].get("sha256") or hashlib.sha256(
                                    chunk_row["props"]["text"].encode("utf-8")
                                ).hexdigest())
                                cache_id = stable_id("entity-extraction-cache", content_hash, extractor_signature)
                                cache_ids_by_chunk[chunk_row["id"]] = cache_id
                                cache_id_by_chunk_id[cache_id] = content_hash
                            try:
                                cached_results = self.store.get_entity_extraction_cache_many(
                                    list(cache_id_by_chunk_id)
                                )
                            except Exception as exc:
                                # A cache issue must not block useful indexing or LLM extraction.
                                cached_results = {}
                                log.warning("Entity-Extraktionscache konnte nicht gelesen werden; fahre ohne Cache fort: %s", exc)
                            pending_cache_entries: dict[str, dict[str, Any]] = {}
                            local_results: dict[str, dict[str, Any]] = dict(cached_results)
                            for chunk_row in chunk_rows:
                                if cancel_event is not None and cancel_event.is_set():
                                    break
                                cache_id = cache_ids_by_chunk[chunk_row["id"]]
                                if cache_id in local_results:
                                    stats["entity_cache_hits"] += 1
                                    entity_payloads.append((chunk_row["id"], local_results[cache_id]))
                                    continue
                                stats["entity_cache_misses"] += 1
                                try:
                                    stats["entity_llm_calls"] += 1
                                    result = self.entity_extractor.extract(chunk_row["props"]["text"])
                                    if not isinstance(result, dict):
                                        raise ValueError("Entity-Extraktor lieferte kein Ergebnisobjekt.")
                                    result = {
                                        "entities": result.get("entities", []),
                                        "relations": result.get("relations", []),
                                    }
                                    local_results[cache_id] = result
                                    pending_cache_entries[cache_id] = {
                                        "id": cache_id,
                                        "content_sha256": cache_id_by_chunk_id[cache_id],
                                        "extractor_signature": extractor_signature,
                                        "result": result,
                                    }
                                    entity_payloads.append((chunk_row["id"], result))
                                except Exception as exc:
                                    metadata["entity_extraction_pending"] = True
                                    response = getattr(exc, "response", None)
                                    if getattr(response, "status_code", None) == 429:
                                        entity_rate_limited = True
                                        log.warning("Entity-Extraktion wegen HTTP 429 für den Rest dieses Scans pausiert; der Datei-/Chunk-Import läuft weiter.")
                                        break
                                    log.warning("Entity-Extraktion fehlgeschlagen (%s, Chunk %s): %s",
                                                candidate.relative_path, chunk_row["props"]["ordinal"], exc)
                            if entity_rate_limited and chunk_rows:
                                metadata["entity_extraction_pending"] = True
                            if pending_cache_entries:
                                try:
                                    self.store.save_entity_extraction_cache_many(list(pending_cache_entries.values()))
                                except Exception as exc:
                                    log.warning("Entity-Ergebnisse konnten nicht im Neo4j-Cache gespeichert werden: %s", exc)
                        elif do_entity_extraction and chunk_rows and entity_rate_limited:
                            metadata["entity_extraction_pending"] = True

            except (OSError, ValueError, RuntimeError) as exc:
                metadata["status"] = "extraction_error"
                metadata["error"] = str(exc)[:1000]
                stats["errors"] += 1
                log.warning("Datei konnte nicht extrahiert werden: %s (%s)", candidate.relative_path, exc)
            except Exception as exc:
                # One malformed document should never abort the full directory import.
                metadata["status"] = "extraction_error"
                metadata["error"] = f"{type(exc).__name__}: {exc}"[:1000]
                stats["errors"] += 1
                log.exception("Unerwarteter Fehler bei %s", candidate.relative_path)

            try:
                log.info("Speichere Datei und %d Chunks in Neo4j: %s", len(chunk_rows), candidate.relative_path)
                self.store.save_file(
                    root_id, root, candidate.relative_path,
                    candidate.parent_relative_path, metadata, chunk_rows,
                )
                stats["indexed"] += 1
                for chunk_id, result in entity_payloads:
                    try:
                        source_text = next((row["props"].get("text", "") for row in chunk_rows if row["id"] == chunk_id), "")
                        try:
                            self.store.save_entities_for_chunk(
                                root_id, chunk_id, result.get("entities", []),
                                result.get("relations", []), source_text=source_text,
                            )
                        except TypeError as signature_error:
                            # Preserve compatibility with light-weight/custom store adapters
                            # which still implement the V6 four-argument method.
                            if "source_text" not in str(signature_error):
                                raise
                            self.store.save_entities_for_chunk(
                                root_id, chunk_id, result.get("entities", []), result.get("relations", []),
                            )
                        stats["entities_extracted"] += len(result.get("entities", []))
                    except Exception as exc:
                        log.warning("Graph-Beziehungen konnten nicht gespeichert werden (%s): %s",
                                    candidate.relative_path, exc)
            except Exception:
                stats["errors"] += 1
                log.exception("Neo4j konnte Datei nicht speichern: %s", candidate.relative_path)

            processed += 1
            elapsed = max(time.monotonic() - started_at, 0.001)
            file_elapsed = time.monotonic() - file_started_at
            if number % 5 == 0 or number == total:
                rate = processed / elapsed
                remaining = max(total - number, 0)
                eta_seconds = remaining / rate if rate > 0 else 0
                log.info("Fortschritt %d/%d Dateien | neu/aktualisiert: %d | unverändert: %d | Chunks: %d | LLM-Calls: %d | Cache-Hits: %d | letzte Datei: %.1fs | Ø %.2f Dateien/s | Rest ca. %.1f min",
                         number, total, stats["indexed"], stats["unchanged"], stats["chunks"],
                         stats["entity_llm_calls"], stats["entity_cache_hits"], file_elapsed, rate, eta_seconds / 60)

        # Never interpret an incomplete traversal as mass deletion. Keep stale data
        # until a clean scan can confirm that a file or folder disappeared.
        cancelled = bool(stats.get("cancelled")) or (cancel_event is not None and cancel_event.is_set())
        stats["cancelled"] = cancelled
        if not cancelled and snapshot.skipped_errors == 0:
            self.store.mark_files_seen(root_id, [item.relative_path for item in snapshot.files], scan_id)
            cleanup = self.store.cleanup_removed(root_id, scan_id)
            stats.update(cleanup)
        elif not cancelled:
            log.warning("Bereinigung übersprungen: %d Verzeichnisse/Dateien waren nicht lesbar; "
                        "sonst könnten vorhandene Graphdaten versehentlich gelöscht werden.", snapshot.skipped_errors)

        graph_changed = bool(stats.get("indexed", 0) or stats.get("deleted_files", 0) or stats.get("deleted_folders", 0))
        if graph_changed:
            self.store.invalidate_derived_analysis(
                root_id,
                reason="Quellgraph hat sich durch Dateiindexierung oder Bereinigung geändert",
            )
            # Run maintenance only after a clean scan and after invalidating all
            # derived graph analysis. Cleanup is best-effort: it must never turn a
            # successful source import into a failed import.
            if not cancelled and snapshot.skipped_errors == 0:
                maintenance = getattr(self.store, "cleanup_graph_maintenance", None)
                if callable(maintenance):
                    try:
                        stats["graph_cleanup"] = maintenance(cleanup_orphans=False)
                    except Exception as exc:
                        stats["graph_cleanup_error"] = str(exc)[:1000]
                        log.warning("Automatischer Graph-Cleanup fehlgeschlagen; Indexierung bleibt erhalten: %s", exc)
        if cancelled:
            log.warning("Scan pausiert. Zum Fortsetzen denselben Root normal erneut scannen; bereits gespeicherte Dateien werden inkrementell übersprungen.")
        stats["elapsed_seconds"] = round(time.monotonic() - started_at, 2)
        stats["entity_extraction_paused_429"] = entity_rate_limited
        stats["graph_stats"] = self.store.stats(root_id)
        return stats
