from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from .config import get_settings
from .embeddings import EmbeddingClient
from .indexer import DirectoryIndexer
from .graph_intelligence import GraphIntelligence
from .neo4j_store import EmbeddingModelMismatch, Neo4jStore
from .search import KnowledgeSearch, format_search_results


def _json_print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _root_id_for_arg(store: Neo4jStore, value: str | None) -> str | None:
    if not value:
        return None
    path = Path(value).expanduser().resolve()
    return store.root_id_for(path)


def _cmd_scan(args: argparse.Namespace) -> int:
    settings = get_settings()
    settings.validate_neo4j()
    indexer = DirectoryIndexer(settings)
    try:
        result = indexer.index(
            Path(args.directory),
            force=args.force,
            rebuild=args.rebuild,
            no_embeddings=args.no_embeddings,
            extract_entities=args.extract_entities,
            include_ignored=args.include_ignored,
            include_sensitive=args.include_sensitive,
        )
        _json_print(result)
        return 0 if result.get("errors", 0) == 0 else 2
    except EmbeddingModelMismatch as exc:
        print(f"Konfigurationsfehler: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        logging.exception("Scan fehlgeschlagen")
        print(f"Scan fehlgeschlagen: {exc}", file=sys.stderr)
        return 1
    finally:
        indexer.close()


def _cmd_search(args: argparse.Namespace) -> int:
    settings = get_settings()
    settings.validate_neo4j()
    searcher = KnowledgeSearch(settings)
    try:
        root_id = _root_id_for_arg(searcher.store, args.root)
        results = searcher.search(args.query, args.limit, root_id)
        if args.json:
            _json_print(results)
        else:
            print(format_search_results(args.query, results))
        return 0 if results else 1
    finally:
        searcher.close()


def _cmd_file(args: argparse.Namespace) -> int:
    settings = get_settings()
    settings.validate_neo4j()
    searcher = KnowledgeSearch(settings)
    try:
        root_id = _root_id_for_arg(searcher.store, args.root)
        result = searcher.get_file_context(args.path, root_id)
        if not result:
            print("Datei nicht gefunden. Nutze den relativen Pfad innerhalb des Index-Root oder den absoluten Pfad.", file=sys.stderr)
            return 1
        _json_print(result)
        return 0
    finally:
        searcher.close()


def _cmd_related(args: argparse.Namespace) -> int:
    settings = get_settings()
    settings.validate_neo4j()
    searcher = KnowledgeSearch(settings)
    try:
        root_id = _root_id_for_arg(searcher.store, args.root)
        results = searcher.related_files(args.path, args.limit, root_id)
        _json_print(results)
        return 0
    finally:
        searcher.close()


def _cmd_stats(args: argparse.Namespace) -> int:
    settings = get_settings()
    settings.validate_neo4j()
    store = Neo4jStore(settings)
    try:
        root_id = _root_id_for_arg(store, args.root)
        _json_print(store.stats(root_id))
        return 0
    finally:
        store.close()


def _cmd_doctor(args: argparse.Namespace) -> int:
    settings = get_settings()
    settings.validate_neo4j()
    report: dict[str, Any] = {"neo4j": "failed", "embedding": "not tested"}
    store: Neo4jStore | None = None
    try:
        store = Neo4jStore(settings)
        store.ensure_schema()
        report["neo4j"] = "connected"
        report["neo4j_uri"] = settings.neo4j_uri
        report["database"] = settings.neo4j_database
    except Exception as exc:
        report["neo4j_error"] = str(exc)
    finally:
        if store:
            store.close()
    if args.embedding:
        client = EmbeddingClient(settings)
        try:
            vector = client.embed_one("Test für den lokalen Knowledge Graph")
            report["embedding"] = "ok"
            report["embedding_model"] = client.signature
            report["embedding_dimensions"] = len(vector)
        except Exception as exc:
            report["embedding"] = "failed"
            report["embedding_error"] = str(exc)
    _json_print(report)
    return 0 if report["neo4j"] == "connected" and report["embedding"] != "failed" else 1



def _cmd_graph_capabilities(args: argparse.Namespace) -> int:
    settings = get_settings()
    settings.validate_neo4j()
    store = Neo4jStore(settings)
    try:
        _json_print(GraphIntelligence(store).capabilities())
        return 0
    finally:
        store.close()


def _cmd_analyze_graph(args: argparse.Namespace) -> int:
    settings = get_settings()
    settings.validate_neo4j()
    store = Neo4jStore(settings)
    try:
        root_id = _root_id_for_arg(store, args.root)
        result = GraphIntelligence(store).analyze(
            root_id=root_id,
            max_sample_chunks=args.max_sample_chunks,
            max_candidates=args.max_candidates,
            run_gds=not args.no_gds,
        )
        _json_print(result)
        return 0 if result.get("status") == "completed" else 1
    finally:
        store.close()


def _cmd_link_candidates(args: argparse.Namespace) -> int:
    settings = get_settings()
    settings.validate_neo4j()
    store = Neo4jStore(settings)
    try:
        _json_print(GraphIntelligence(store).list_link_candidates(
            status=args.status, limit=args.limit, include_stale=args.include_stale
        ))
        return 0
    finally:
        store.close()


def _cmd_review_link_candidate(args: argparse.Namespace) -> int:
    settings = get_settings()
    settings.validate_neo4j()
    store = Neo4jStore(settings)
    try:
        result = GraphIntelligence(store).review_link_candidate(args.candidate_id, args.decision, args.note or "")
        _json_print(result)
        return 0
    finally:
        store.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="win-kg",
        description="Unternehmenswissen in Neo4j aufbauen, durchsuchen und mit Graphalgorithmen analysieren.",
    )
    parser.add_argument("--log-level", default=None, help="Logging-Level (DEBUG, INFO, WARNING)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan", help="Verzeichnis importieren oder aktualisieren")
    scan.add_argument("directory", help="Zu indexierendes Windows-Verzeichnis")
    scan.add_argument("--force", action="store_true", help="Alle gefundenen Dateien erneut verarbeiten")
    scan.add_argument("--rebuild", action="store_true", help="Bisherigen Graphen dieses Roots vor dem Import löschen")
    scan.add_argument("--no-embeddings", action="store_true", help="Nur Volltext und Graph; Embeddings deaktivieren")
    scan.add_argument("--extract-entities", action="store_true", default=None, help="LLM zur Extraktion von Entitäten/Beziehungen verwenden (überschreibt ENTITY_EXTRACTION_ENABLED aus .env)")
    scan.add_argument("--include-ignored", action="store_true", help="Standardmäßig ausgeschlossene Verzeichnisse mit einlesen")
    scan.add_argument("--include-sensitive", action="store_true", help="Typische Geheimnisdateien ausdrücklich einschließen")
    scan.set_defaults(func=_cmd_scan)

    search = subparsers.add_parser("search", help="Hybrid Search: Volltext plus semantische Vektorsuche")
    search.add_argument("query", help="Suchfrage oder Suchbegriffe")
    search.add_argument("--limit", type=int, default=5)
    search.add_argument("--root", help="Nur innerhalb dieses zuvor indexierten Root-Pfads suchen")
    search.add_argument("--json", action="store_true", help="Ergebnisse als JSON ausgeben")
    search.set_defaults(func=_cmd_search)

    file_parser = subparsers.add_parser("file", help="Metadaten und extrahierte Abschnitte einer Datei anzeigen")
    file_parser.add_argument("path", help="Relativer Pfad oder der beim Import gespeicherte absolute Pfad")
    file_parser.add_argument("--root", help="Index-Root bei mehrfach vorkommenden relativen Pfaden")
    file_parser.set_defaults(func=_cmd_file)

    related = subparsers.add_parser("related", help="Dateien finden, die Entitäten mit einer Datei teilen")
    related.add_argument("path", help="Relativer oder absoluter Dateipfad")
    related.add_argument("--limit", type=int, default=10)
    related.add_argument("--root", help="Index-Root")
    related.set_defaults(func=_cmd_related)

    stats = subparsers.add_parser("stats", help="Anzahl der Roots, Ordner, Dateien, Chunks und Entitäten")
    stats.add_argument("--root", help="Auf einen indexierten Root-Pfad einschränken")
    stats.set_defaults(func=_cmd_stats)

    doctor = subparsers.add_parser("doctor", help="Neo4j-Verbindung prüfen; optional Embedding-API testen")
    doctor.add_argument("--embedding", action="store_true")
    doctor.set_defaults(func=_cmd_doctor)

    capabilities = subparsers.add_parser("graph-capabilities", help="Neo4j-, Index- und GDS-Fähigkeiten anzeigen")
    capabilities.set_defaults(func=_cmd_graph_capabilities)

    analyze = subparsers.add_parser("analyze-graph", help="Graphmuster analysieren, Kandidaten speichern und optional GDS ausführen")
    analyze.add_argument("--root", help="Nur Daten dieses zuvor indexierten Root-Pfads analysieren; ohne Angabe alle Roots")
    analyze.add_argument("--max-sample-chunks", type=int, default=5000, help="Maximal betrachtete Chunks für Co-Mention-Kandidaten (100–100000)")
    analyze.add_argument("--max-candidates", type=int, default=250, help="Maximal neu gefundene Beziehungskandidaten")
    analyze.add_argument("--no-gds", action="store_true", help="Nur deterministische Cypher-Analyse, keine GDS-Algorithmen")
    analyze.set_defaults(func=_cmd_analyze_graph)

    candidates = subparsers.add_parser("link-candidates", help="Vorgeschlagene Beziehungen anzeigen")
    candidates.add_argument("--status", choices=["proposed", "accepted", "rejected", "deferred"])
    candidates.add_argument("--limit", type=int, default=100)
    candidates.add_argument("--include-stale", action="store_true", help="Auch veraltete Vorschläge anzeigen")
    candidates.set_defaults(func=_cmd_link_candidates)

    review = subparsers.add_parser("review-link-candidate", help="Einen Beziehungsvorschlag bewerten; akzeptierte Vorschläge werden als generische KG_ASSERTED_LINK-Kante und KGAssertion gespeichert")
    review.add_argument("candidate_id")
    review.add_argument("decision", choices=["accepted", "rejected", "deferred"])
    review.add_argument("--note", default="", help="Optionale Begründung")
    review.set_defaults(func=_cmd_review_link_candidate)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    settings = get_settings()
    logging.basicConfig(
        level=args.log_level or settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    try:
        code = args.func(args)
    except KeyboardInterrupt:
        print("Abgebrochen.", file=sys.stderr)
        code = 130
    except Exception as exc:
        logging.exception("Befehl fehlgeschlagen")
        print(f"Fehler: {exc}", file=sys.stderr)
        code = 1
    raise SystemExit(code)


if __name__ == "__main__":
    main()
