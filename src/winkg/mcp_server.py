from __future__ import annotations

import json
import logging
from typing import Any

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover - depends on optional extra
    raise SystemExit(
        "MCP-Abhängigkeit fehlt. Installiere das Projekt mit: pip install -e '.[mcp]'"
    ) from exc

from .config import get_settings
from .graph_intelligence import GraphIntelligence
from .neo4j_store import Neo4jStore
from .search import KnowledgeSearch, format_search_results

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
log = logging.getLogger(__name__)
mcp = FastMCP("Windows Knowledge Graph")
_searcher: KnowledgeSearch | None = None
_store: Neo4jStore | None = None


def _runtime() -> tuple[KnowledgeSearch, Neo4jStore]:
    global _searcher, _store
    if _searcher is None or _store is None:
        settings = get_settings()
        settings.validate_neo4j()
        _store = Neo4jStore(settings)
        _searcher = KnowledgeSearch(settings, store=_store)
    return _searcher, _store


def _root_id(root: str | None, store: Neo4jStore) -> str | None:
    if not root:
        return None
    from pathlib import Path
    return store.root_id_for(Path(root).expanduser().resolve())


@mcp.tool()
def search_knowledge(query: str, limit: int = 5, root: str | None = None) -> str:
    """Durchsucht indexierte Windows-Dateien hybrid über Volltext, Embeddings und Graphkontext. Gibt Quellenpfade zurück."""
    searcher, store = _runtime()
    results = searcher.search(query, limit=max(1, min(limit, 15)), root_id=_root_id(root, store))
    return format_search_results(query, results)


@mcp.tool()
def global_search_knowledge(query: str, limit: int = 8, root: str | None = None) -> str:
    """Beantwortet korpusweite Fragen mit gespeicherten Community-Zusammenfassungen und konkreten Quellabschnitten; kennzeichnet offene Fragen und gibt Originalpfade zurück."""
    searcher, store = _runtime()
    result = searcher.global_search(query, limit=max(2, min(limit, 12)), root_id=_root_id(root, store))
    return json.dumps(result, ensure_ascii=False, indent=2, default=str)


@mcp.tool()
def get_file_context(path: str, root: str | None = None) -> str:
    """Liefert Metadaten und extrahierte Textabschnitte für einen absoluten oder relativen Dateipfad."""
    searcher, store = _runtime()
    result = searcher.get_file_context(path, _root_id(root, store))
    if not result:
        return "Datei nicht gefunden. Gib den relativen Pfad innerhalb des indexierten Roots oder den absoluten Pfad an."
    return json.dumps(result, ensure_ascii=False, indent=2, default=str)


@mcp.tool()
def find_related_files(path: str, limit: int = 10, root: str | None = None) -> str:
    """Findet Dateien, die über extrahierte Entitäten im Knowledge Graph mit einer Datei verbunden sind."""
    searcher, store = _runtime()
    results = searcher.related_files(path, max(1, min(limit, 25)), _root_id(root, store))
    if not results:
        return "Keine verwandten Dateien gefunden. Für semantische Beziehungen zuerst einen Scan mit --extract-entities durchführen."
    return json.dumps(results, ensure_ascii=False, indent=2, default=str)


@mcp.tool()
def get_graph_stats(root: str | None = None) -> str:
    """Gibt die Anzahl der Roots, Ordner, Dateien, Text-Chunks und Entitäten im Neo4j-Graph zurück."""
    _, store = _runtime()
    return json.dumps(store.stats(_root_id(root, store)), ensure_ascii=False, indent=2)



@mcp.tool()
def get_graph_capabilities() -> str:
    """Zeigt Neo4j-Version, vorhandene Indizes und tatsächlich verfügbare Graph Data Science-Algorithmen an."""
    _, store = _runtime()
    return json.dumps(GraphIntelligence(store).capabilities(), ensure_ascii=False, indent=2, default=str)


@mcp.tool()
def analyze_knowledge_graph(max_sample_chunks: int = 5000, max_candidates: int = 250,
                            run_gds: bool = True) -> str:
    """Analysiert den Neo4j-Wissensgraphen ohne LLM: Inhaltsduplikate, Abdeckungslücken, isolierte Entitäten und Beziehungskandidaten. GDS-Algorithmen werden nur ausgeführt, wenn sie installiert sind und das Sicherheitslimit eingehalten wird. Vorschläge werden nicht automatisch zu Fakten."""
    _, store = _runtime()
    result = GraphIntelligence(store).analyze(
        max_sample_chunks=max(100, min(max_sample_chunks, 100000)),
        max_candidates=max(1, min(max_candidates, 2000)), run_gds=run_gds,
    )
    return json.dumps(result, ensure_ascii=False, indent=2, default=str)


@mcp.tool()
def list_graph_link_candidates(status: str | None = None, limit: int = 100, include_stale: bool = False) -> str:
    """Listet Beziehungskandidaten mit Belegen und Prüfstatus. Offene veraltete Vorschläge sind standardmäßig ausgeblendet."""
    _, store = _runtime()
    rows = GraphIntelligence(store).list_link_candidates(
        status=status, limit=max(1, min(limit, 1000)), include_stale=include_stale
    )
    return json.dumps(rows, ensure_ascii=False, indent=2, default=str)


@mcp.tool()
def review_graph_link_candidate(candidate_id: str, decision: str, note: str = "") -> str:
    """Bewertet einen Beziehungsvorschlag. Akzeptierte Kandidaten bleiben als KGAssertion mit freiem Prädikat erhalten und erzeugen eine generische KG_ASSERTED_LINK-Kante; verworfene oder zurückgestellte Kandidaten werden nicht als bestätigte Kante geführt."""
    _, store = _runtime()
    row = GraphIntelligence(store).review_link_candidate(candidate_id, decision, note)
    return json.dumps(row, ensure_ascii=False, indent=2, default=str)


@mcp.tool()
def list_graph_assertions(status: str | None = None, predicate: str | None = None,
                         limit: int = 100) -> str:
    """Listet generische, quellengebundene Aussagen aus Neo4j. Prädikate sind offen; Aussagen enthalten Status, Methode und Quellenhinweise."""
    _, store = _runtime()
    rows = GraphIntelligence(store).list_assertions(
        status=status, predicate=predicate, limit=max(1, min(limit, 1000))
    )
    return json.dumps(rows, ensure_ascii=False, indent=2, default=str)


@mcp.tool()
def get_graph_insights(limit: int = 50, run_id: str | None = None) -> str:
    """Liefert in Neo4j gespeicherte Analysebefunde mit Begründung und strukturierten Details."""
    _, store = _runtime()
    rows = GraphIntelligence(store).list_insights(run_id=run_id, limit=max(1, min(limit, 500)))
    return json.dumps(rows, ensure_ascii=False, indent=2, default=str)


@mcp.tool()
def get_files_needing_deep_analysis(limit: int = 100, root: str | None = None) -> str:
    """Listet Dateien für eine spätere Analyse: zu große Dateien (nur Metadaten) und wegen MAX_CHUNKS_PER_FILE gekürzte Inhalte."""
    _, store = _runtime()
    rows = store.files_needing_deep_analysis(_root_id(root, store), limit=max(1, min(limit, 1000)))
    return json.dumps(rows, ensure_ascii=False, indent=2, default=str)


def main() -> None:
    # stdout is reserved for the MCP stdio protocol.
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
