from __future__ import annotations

import json
import logging
import re
from typing import Any

from .config import Settings
from .embeddings import EmbeddingClient
from .neo4j_store import Neo4jStore

log = logging.getLogger(__name__)


class KnowledgeSearch:
    def __init__(self, settings: Settings, store: Neo4jStore | None = None):
        self.settings = settings
        self.store = store or Neo4jStore(settings)
        self.embeddings = EmbeddingClient(settings)

    def close(self) -> None:
        self.store.close()

    def search(self, query: str, limit: int = 5, root_id: str | None = None) -> list[dict[str, Any]]:
        query = query.strip()
        if not query:
            return []
        limit = max(1, min(int(limit), 30))
        ranks: dict[str, float] = {}
        source_scores: dict[str, dict[str, float]] = {}
        vector_results: list[dict[str, Any]] = []
        if self.embeddings.enabled:
            try:
                vector = self.embeddings.embed_one(query)
                vector_results = self.store.vector_search(vector, limit * 3, root_id)
            except Exception as exc:
                log.warning("Vektorsuche nicht verfügbar; verwende Volltextsuche: %s", exc)

        text_results = self.store.fulltext_search(query, limit * 5, root_id)
        # Reciprocal-rank fusion avoids comparing unrelated raw vector/FTS score scales.
        constant = 60.0
        for source, rows, weight in (("vector", vector_results, 1.0), ("fulltext", text_results, 1.0)):
            for rank, row in enumerate(rows, start=1):
                chunk_id = str(row["id"])
                ranks[chunk_id] = ranks.get(chunk_id, 0.0) + weight / (constant + rank)
                source_scores.setdefault(chunk_id, {})[source] = float(row.get("score", 0.0))

        # A filename/path match boosts chunks in that file. Files without readable
        # content remain available through get_file_context() but have no chunk to cite.
        file_rows = self.store.file_fulltext_search(query, limit * 3, root_id)
        file_seed_ids: list[str] = []
        for file_rank, file_row in enumerate(file_rows, start=1):
            for chunk_rank, chunk in enumerate(self.store.chunks_for_file(str(file_row["id"]), limit=3), start=1):
                chunk_id = str(chunk["id"])
                file_seed_ids.append(chunk_id)
                ranks[chunk_id] = ranks.get(chunk_id, 0.0) + 0.7 / (constant + file_rank + chunk_rank)
                source_scores.setdefault(chunk_id, {})["filename"] = float(file_row.get("score", 0.0))

        # Neo4j is also a retrieval engine, not only a place to display relations.
        # Start from strong vector/full-text/file hits and walk one or two semantic
        # entity edges to surface supporting documents that lexical search may miss.
        seed_ids = list(dict.fromkeys(
            [str(row["id"]) for row in vector_results + text_results if row.get("id")]
            + file_seed_ids
        ))[:12]
        graph_paths: dict[str, list[dict[str, Any]]] = {}
        if seed_ids:
            try:
                graph_results = self.store.graph_related_chunk_search(seed_ids, limit=limit * 3, root_id=root_id)
            except Exception as exc:
                graph_results = []
                log.debug("Graph-Retrieval übersprungen: %s", exc)
            for graph_rank, row in enumerate(graph_results, start=1):
                chunk_id = str(row.get("id") or "")
                if not chunk_id:
                    continue
                ranks[chunk_id] = ranks.get(chunk_id, 0.0) + 0.9 / (constant + graph_rank)
                source_scores.setdefault(chunk_id, {})["graph"] = float(row.get("score", 0.0))
                graph_paths.setdefault(chunk_id, []).append({
                    "kind": row.get("kind") or "entity_relations",
                    "hops": int(row.get("hops") or 0),
                    "via_entities": row.get("via_entities") or [],
                    "supporting_seeds": int(row.get("supporting_seeds") or 0),
                    "similarity": row.get("similarity"),
                })

            # A second, independent Neo4j graph signal comes from GDS KNN edges
            # over stored chunk embeddings. This works even when entity extraction
            # is disabled and does not invoke an LLM.
            try:
                semantic_results = self.store.semantic_related_chunk_search(
                    seed_ids, limit=limit * 3, root_id=root_id
                )
            except Exception as exc:
                semantic_results = []
                log.debug("Semantische Graph-Erweiterung übersprungen: %s", exc)
            for semantic_rank, row in enumerate(semantic_results, start=1):
                chunk_id = str(row.get("id") or "")
                if not chunk_id:
                    continue
                ranks[chunk_id] = ranks.get(chunk_id, 0.0) + 0.75 / (constant + semantic_rank)
                source_scores.setdefault(chunk_id, {})["graph_semantic"] = float(row.get("similarity", row.get("score", 0.0)))
                graph_paths.setdefault(chunk_id, []).append({
                    "kind": "semantic_similarity",
                    "hops": 1,
                    "via_entities": [],
                    "supporting_seeds": int(row.get("supporting_seeds") or 0),
                    "similarity": row.get("similarity", row.get("score")),
                })

        ordered = sorted(ranks.items(), key=lambda pair: pair[1], reverse=True)[:limit]
        results: list[dict[str, Any]] = []
        for chunk_id, score in ordered:
            detail = self.store.get_chunk_details(chunk_id)
            if not detail:
                continue
            detail["score"] = round(score, 6)
            detail["score_sources"] = source_scores.get(chunk_id, {})
            if chunk_id in graph_paths:
                detail["graph_paths"] = graph_paths[chunk_id][:3]
            results.append(detail)
        return results

    def _community_summaries_for_query(
        self, query: str, *, entity_names: list[str], root_id: str | None, limit: int = 4
    ) -> list[dict[str, Any]]:
        tokens = list(dict.fromkeys(token.casefold() for token in re.findall(r"[\w.-]{3,}", query) if len(token) >= 3))[:12]
        entity_names = list(dict.fromkeys(str(name)[:200] for name in entity_names if name))[:40]
        if not tokens and not entity_names:
            return []
        rows = self.store._run(
            "MATCH (i:KGInsight {kind: 'community_summary'})-[:KG_SUMMARIZES_COMMUNITY]->(c:KGCommunity) "
            "WHERE coalesce(i.is_stale, false) = false AND coalesce(c.is_stale, false) = false "
            # A root-scoped search may only reuse summaries that contain no cross-root members.
            "AND ($root_id IS NULL OR c.root_id = $root_id) "
            "OPTIONAL MATCH (e:KGEntity)-[:KG_IN_COMMUNITY]->(c) "
            "WITH i, c, collect(DISTINCT e.name) AS member_names "
            "WITH i, c, member_names, "
            "reduce(score = 0, token IN $tokens | score + CASE WHEN "
            "toLower(coalesce(i.title, '') + ' ' + coalesce(i.description, '') + ' ' + coalesce(i.details_json, '')) "
            "CONTAINS token THEN 1 ELSE 0 END) + "
            "3 * size([name IN member_names WHERE name IN $entity_names]) AS relevance "
            "WHERE relevance > 0 "
            "RETURN i.id AS id, i.run_id AS run_id, i.title AS title, i.description AS description, "
            "i.details_json AS details_json, relevance, c.id AS community_id, c.member_count AS member_count "
            "ORDER BY relevance DESC, member_count DESC, i.updated_at DESC LIMIT $limit",
            {"tokens": tokens, "entity_names": entity_names, "root_id": root_id,
             "limit": max(1, min(int(limit), 10))},
        )
        results: list[dict[str, Any]] = []
        for row in rows:
            try:
                row["details"] = json.loads(row.pop("details_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                row["details"] = {}
                row.pop("details_json", None)
            results.append(row)
        return results

    def global_search(
        self, query: str, limit: int = 8, root_id: str | None = None
    ) -> dict[str, Any]:
        """Answer corpus-wide questions using community summaries plus retrieved source chunks."""
        query = query.strip()
        if not query:
            return {"status": "empty_query", "answer": "Bitte eine Frage eingeben.", "communities": [], "sources": []}
        limit = max(2, min(int(limit), 12))
        if not bool(getattr(self.settings, "global_search_enabled", True)):
            return {"status": "disabled", "answer": "Global Search ist in der Projektkonfiguration deaktiviert.",
                    "communities": [], "sources": []}
        sources = self.search(query, limit=limit, root_id=root_id)
        entity_names = [
            str(entity.get("name"))
            for source in sources for entity in (source.get("entities") or [])
            if entity.get("name")
        ]
        communities = self._community_summaries_for_query(
            query, entity_names=entity_names, root_id=root_id, limit=3
        )
        if not self.settings.llm_base_url or not self.settings.llm_model:
            return {
                "status": "llm_not_configured",
                "answer": "Für eine zusammengefasste Global-Search-Antwort sind LLM_BASE_URL und LLM_MODEL erforderlich. Die gefundenen Community-Zusammenfassungen und Originalquellen stehen dennoch bereit.",
                "communities": communities, "sources": sources,
            }
        try:
            from .entity_extraction import EntityExtractor
            answer = EntityExtractor(self.settings).answer_global_query(query, communities, sources)
        except Exception as exc:
            log.warning("Global-Search-Antwort konnte nicht erstellt werden: %s", exc)
            return {
                "status": "answer_failed", "answer": "Die LLM-Antwort konnte nicht erstellt werden. Nutze die aufgeführten Originalquellen und Community-Zusammenfassungen. " + str(exc)[:400],
                "communities": communities, "sources": sources,
            }
        allowed_citations = {f"S{i}" for i in range(1, min(len(sources), 4) + 1)} | {
            f"C{i}" for i in range(1, min(len(communities), 3) + 1)
        }
        answer_text = str(answer.get("answer") or "")
        answer_text = re.sub(
            r"\[(S\d+|C\d+)\]",
            lambda match: match.group(0) if match.group(1) in allowed_citations else "",
            answer_text,
        )
        answer["answer"] = answer_text
        answer["used_sources"] = [
            value for value in (answer.get("used_sources") or [])
            if str(value) in {f"S{i}" for i in range(1, min(len(sources), 4) + 1)}
        ]
        answer["used_communities"] = [
            value for value in (answer.get("used_communities") or [])
            if str(value) in {f"C{i}" for i in range(1, min(len(communities), 3) + 1)}
        ]
        return {
            "status": "completed", **answer, "communities": communities, "sources": sources,
            "community_summaries_used": len(communities), "source_chunks_retrieved": len(sources),
            "has_community_summaries": bool(communities),
        }

    def get_file_context(self, path_or_relative: str, root_id: str | None = None) -> dict[str, Any] | None:
        return self.store.get_file(path_or_relative, root_id)

    def related_files(self, path_or_relative: str, limit: int = 10,
                      root_id: str | None = None) -> list[dict[str, Any]]:
        return self.store.related_files(path_or_relative, limit, root_id)


def format_search_results(query: str, results: list[dict[str, Any]]) -> str:
    if not results:
        return f"Keine Treffer für: {query}"
    output = [f"Suchanfrage: {query}", f"Treffer: {len(results)}"]
    for number, row in enumerate(results, start=1):
        page = f", Seite {row['page']}" if row.get("page") else ""
        section = f", Abschnitt: {row['section']}" if row.get("section") else ""
        output.extend([
            "",
            f"[{number}] {row.get('filename', 'Unbekannte Datei')}{page}{section}",
            f"Pfad: {row.get('path', '')}",
            f"Relevanz (RRF): {row.get('score', 0)}",
            (row.get("text") or "").strip()[:1400],
        ])
        if row.get("needs_deep_analysis"):
            output.append(
                "HINWEIS: Quelldatei wurde für den Graph-Index gekürzt. "
                f"Indexiert {row.get('indexed_chunks', 0)} von {row.get('total_chunks', 0)} Chunks; "
                f"ausgelassen: {row.get('omitted_chunks', 0)}. Gesonderte Analyse empfohlen. "
                "Die vollständige Originaldatei liegt am Quellpfad."
            )
        entities = row.get("entities") or []
        if entities:
            output.append("Entitäten: " + ", ".join(f"{entity.get('name')} ({entity.get('type')})" for entity in entities[:8]))
        graph_paths = row.get("graph_paths") or []
        if graph_paths:
            explanations = []
            for path in graph_paths[:3]:
                if path.get("kind") == "semantic_similarity":
                    similarity = path.get("similarity")
                    detail = f", Similarität {float(similarity):.3f}" if similarity is not None else ""
                    explanations.append(f"über Embedding-Ähnlichkeit{detail}")
                    continue
                via = ", ".join(str(name) for name in path.get("via_entities", [])[:5]) or "verbundene Entitäten"
                hops = int(path.get("hops") or 0)
                explanations.append(f"über {via} ({hops} Beziehungsschritte)")
            output.append("Graph-Kontext: " + "; ".join(explanations))
        graph_relations = row.get("graph_relations") or []
        if graph_relations:
            output.append("Bestätigte Graph-Beziehungen: " + "; ".join(
                f"{item.get('source')} -[{item.get('type')}]-> {item.get('target')} (Beleg: {item.get('evidence')})"
                for item in graph_relations[:4]
            ))
        proposed_relations = row.get("proposed_relations") or []
        if proposed_relations:
            output.append("Unbestätigte Beziehungshypothesen aus der Text-Extraktion (keine gesicherten Fakten): " + "; ".join(
                f"{item.get('source')} -[{item.get('type')}]-> {item.get('target')} "
                f"(Status: {item.get('status') or 'proposed'}; Beleg: {item.get('evidence')})"
                for item in proposed_relations[:4]
            ))
        related = row.get("related") or []
        if related:
            output.append("Verwandte Dateien: " + "; ".join(item.get("path", "") for item in related[:4]))
    return "\n".join(output)
