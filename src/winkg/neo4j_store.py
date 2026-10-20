from __future__ import annotations

import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any, Iterable

from neo4j import GraphDatabase

from .chunking import stable_id
from .config import Settings
from .quality import normalize_text, parse_confidence, validate_relation_evidence

log = logging.getLogger(__name__)


class EmbeddingModelMismatch(RuntimeError):
    pass


class Neo4jStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.driver = GraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_username, settings.neo4j_password),
            connection_timeout=15,
            max_connection_lifetime=3600,
        )
        self.driver.verify_connectivity()

    def close(self) -> None:
        self.driver.close()

    def _run(self, query: str, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        with self.driver.session(database=self.settings.neo4j_database) as session:
            return [record.data() for record in session.run(query, parameters or {})]

    @staticmethod
    def root_id_for(path: Path) -> str:
        return stable_id("windows-root", str(path.resolve()).casefold())

    @staticmethod
    def folder_id_for(root_id: str, relative_path: str) -> str:
        return stable_id(root_id, "folder", relative_path.casefold())

    @staticmethod
    def file_id_for(root_id: str, relative_path: str) -> str:
        return stable_id(root_id, "file", relative_path.casefold())

    def ensure_schema(self) -> None:
        constraints = [
            "CREATE CONSTRAINT kg_root_id IF NOT EXISTS FOR (n:KGRoot) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT kg_folder_id IF NOT EXISTS FOR (n:KGFolder) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT kg_file_id IF NOT EXISTS FOR (n:KGFile) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT kg_chunk_id IF NOT EXISTS FOR (n:KGChunk) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT kg_entity_id IF NOT EXISTS FOR (n:KGEntity) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT kg_node_id IF NOT EXISTS FOR (n:KGNode) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT kg_assertion_id IF NOT EXISTS FOR (n:KGAssertion) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT kg_analysis_run_id IF NOT EXISTS FOR (n:KGAnalysisRun) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT kg_insight_id IF NOT EXISTS FOR (n:KGInsight) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT kg_link_candidate_id IF NOT EXISTS FOR (n:KGLinkCandidate) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT kg_content_identity_id IF NOT EXISTS FOR (n:KGContentIdentity) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT kg_community_id IF NOT EXISTS FOR (n:KGCommunity) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT kg_component_id IF NOT EXISTS FOR (n:KGComponent) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT kg_extraction_cache_id IF NOT EXISTS FOR (n:KGExtractionCache) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT kg_analysis_history_summary_id IF NOT EXISTS FOR (n:KGAnalysisHistorySummary) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT kg_consolidated_fact_id IF NOT EXISTS FOR (n:KGConsolidatedFact) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT kg_quality_profile_id IF NOT EXISTS FOR (n:KGQualityProfile) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT kg_quality_decision_id IF NOT EXISTS FOR (n:KGQualityDecision) REQUIRE n.id IS UNIQUE",
        ]
        indexes = [
            "CREATE FULLTEXT INDEX kg_chunks_fulltext IF NOT EXISTS FOR (n:KGChunk) ON EACH [n.text, n.path, n.filename, n.section]",
            "CREATE FULLTEXT INDEX kg_files_fulltext IF NOT EXISTS FOR (n:KGFile) ON EACH [n.name, n.path, n.relative_path, n.extension]",
            "CREATE FULLTEXT INDEX kg_entities_fulltext IF NOT EXISTS FOR (n:KGEntity) ON EACH [n.name, n.type, n.description]",
            "CREATE INDEX kg_entity_canonical_id IF NOT EXISTS FOR (n:KGEntity) ON (n.canonical_entity_id)",
            "CREATE INDEX kg_link_candidate_status IF NOT EXISTS FOR (n:KGLinkCandidate) ON (n.status)",
            "CREATE INDEX kg_link_candidate_method_status IF NOT EXISTS FOR (n:KGLinkCandidate) ON (n.method, n.status)",
            "CREATE INDEX kg_assertion_predicate_status IF NOT EXISTS FOR (n:KGAssertion) ON (n.predicate, n.status)",
            "CREATE INDEX kg_insight_run_id IF NOT EXISTS FOR (n:KGInsight) ON (n.run_id)",
            "CREATE INDEX kg_extraction_cache_content_hash IF NOT EXISTS FOR (n:KGExtractionCache) ON (n.content_sha256)",
        ]
        for query in constraints + indexes:
            self._run(query)

    def ensure_vector_index(self, dimensions: int) -> str:
        if not (1 <= dimensions <= 65535):
            raise ValueError(f"Ungültige Embedding-Dimension: {dimensions}")
        # Include dimensions in the index name to avoid an index-definition mismatch
        # when a database is reused after an explicit full rebuild.
        index_name = f"kg_chunk_embedding_{dimensions}"
        query = (
            f"CREATE VECTOR INDEX `{index_name}` IF NOT EXISTS FOR (n:KGChunk) ON (n.embedding) "
            "OPTIONS {indexConfig: {`vector.dimensions`: " + str(dimensions) + ", "
            "`vector.similarity_function`: 'cosine'}}"
        )
        self._run(query)
        return index_name

    def register_root(self, root_id: str, root_path: Path, embedding_signature: str) -> None:
        existing = self._run(
            "MATCH (r:KGRoot {id: $id}) RETURN r.embedding_signature AS signature LIMIT 1",
            {"id": root_id},
        )
        if existing and existing[0].get("signature") not in (None, embedding_signature):
            raise EmbeddingModelMismatch(
                "Für diesen Verzeichnis-Graph ist bereits ein anderes Embedding-Modell registriert "
                f"({existing[0].get('signature')!r} statt {embedding_signature!r}). "
                "Nutze für diesen Graphen dasselbe Modell oder starte bewusst mit --rebuild."
            )
        self.validate_other_root_signatures(root_id, embedding_signature)
        self._run(
            "MERGE (r:KGRoot {id: $id}) "
            "SET r.path = $path, r.name = $name, r.embedding_signature = $signature, "
            "r.updated_at = datetime()",
            {"id": root_id, "path": str(root_path), "name": root_path.name,
             "signature": embedding_signature},
        )

    def validate_other_root_signatures(self, root_id: str, embedding_signature: str) -> None:
        other_signatures = self._run(
            "MATCH (other:KGRoot) WHERE other.id <> $id AND other.embedding_signature IS NOT NULL "
            "AND other.embedding_signature <> $signature "
            "RETURN collect(DISTINCT other.embedding_signature) AS signatures LIMIT 1",
            {"id": root_id, "signature": embedding_signature},
        )
        signatures = other_signatures[0].get("signatures", []) if other_signatures else []
        if signatures:
            raise EmbeddingModelMismatch(
                "In derselben Neo4j-Datenbank sind bereits andere Embedding-Räume registriert: "
                f"{signatures!r}. Alle Roots müssen dasselbe Embedding-Modell verwenden. "
                "Entferne die abweichenden Roots oder verwende eine getrennte Neo4j-Datenbank."
            )

    def invalidate_derived_analysis(self, root_id: str | None = None, reason: str = "source graph changed") -> None:
        """Mark analyses derived from changed source data as stale, not silently current."""
        params = {"root_id": root_id, "reason": reason[:500]}
        self._run(
            "MATCH (r:KGAnalysisRun) WHERE r.status IN ['completed', 'running'] "
            "AND (r.root_id IS NULL OR $root_id IS NULL OR r.root_id = $root_id) "
            "SET r.status = 'stale', r.stale_at = datetime(), r.stale_reason = $reason",
            params,
        )
        self._run(
            "MATCH (r:KGAnalysisRun)-[:KG_PRODUCED]->(i:KGInsight) WHERE r.status = 'stale' "
            "SET i.is_stale = true, i.stale_at = datetime(), i.stale_reason = $reason",
            params,
        )
        self._run(
            "MATCH (c:KGCommunity) SET c.is_stale = true, c.stale_at = datetime(), c.stale_reason = $reason",
            params,
        )
        self._run(
            "MATCH (c:KGComponent) SET c.is_stale = true, c.stale_at = datetime(), c.stale_reason = $reason",
            params,
        )
        self._run(
            "MATCH (e:KGEntity) WHERE e.kg_gds_community_run_id IS NOT NULL "
            "SET e.kg_gds_community_stale = true",
            {},
        )
        self._run(
            "MATCH (e:KGEntity) WHERE e.kg_gds_pagerank_run_id IS NOT NULL "
            "SET e.kg_gds_pagerank_stale = true",
            {},
        )
        self._run(
            "MATCH (e:KGEntity) WHERE e.kg_gds_component_run_id IS NOT NULL "
            "SET e.kg_gds_component_stale = true",
            {},
        )
        # Candidate links depend on the current source graph. Keep the decisions
        # (accepted/rejected/deferred) as durable feedback, but require proposed
        # candidates to be regenerated before they can be reviewed after a scan.
        self._run(
            "MATCH (c:KGLinkCandidate) WHERE coalesce(c.is_stale, false) = false "
            "AND (c.status IN ['proposed', 'deferred'] OR "
            "(coalesce(c.review_method, '') = 'llm_autonomous' AND c.status IN ['accepted', 'rejected'])) "
            "AND ($root_id IS NULL OR c.root_id IS NULL OR c.root_id = $root_id OR EXISTS { "
            "UNWIND coalesce(c.evidence_chunk_ids, []) AS evidence_id "
            "MATCH (chunk:KGChunk {id: evidence_id, root_id: $root_id}) }) "
            "SET c.is_stale = true, c.stale_at = datetime(), c.stale_reason = $reason",
            params,
        )
        self._run("MATCH ()-[r:KG_SEMANTIC_SIMILAR_TO]->() DELETE r", {})
        self._mark_stale_human_assertions(reason[:500])

    def _mark_stale_human_assertions(self, reason: str) -> None:
        """Deactivate any accepted assertion whose original evidence disappeared.

        The method name is retained for compatibility with V6 callers, but this now
        also covers automatically accepted source-extraction and LLM-reviewed claims.
        Historical assertion nodes remain available for audit and quality statistics.
        """
        self._run(
            "MATCH (a:KGAssertion) WHERE a.status = 'accepted' "
            "AND a.method IN ['human_review', 'auto_source_evidence', 'llm_autonomous'] "
            "AND size(coalesce(a.evidence_chunk_ids, [])) > 0 "
            "WITH a, [evidence_id IN a.evidence_chunk_ids WHERE EXISTS { "
            "MATCH (chunk:KGChunk {id: evidence_id}) }] AS live_ids "
            "SET a.evidence_chunk_ids = live_ids, a.evidence_count = size(live_ids), "
            "a.status = CASE WHEN size(live_ids) = 0 THEN 'stale' ELSE 'accepted' END, "
            "a.is_stale = (size(live_ids) = 0), "
            "a.stale_reason = CASE WHEN size(live_ids) = 0 THEN $reason ELSE null END, "
            "a.updated_at = datetime()",
            {"reason": reason},
        )
        # Structural-similarity approvals without direct evidence expire with their run.
        self._run(
            "MATCH (a:KGAssertion {status: 'accepted'}) "
            "WHERE a.method IN ['human_review', 'llm_autonomous'] "
            "AND a.candidate_method = 'gds_node_similarity' "
            "AND a.source_analysis_run_id IS NOT NULL AND (NOT EXISTS { "
            "MATCH (r:KGAnalysisRun {id: a.source_analysis_run_id}) } OR EXISTS { "
            "MATCH (r:KGAnalysisRun {id: a.source_analysis_run_id}) WHERE r.status = 'stale' }) "
            "SET a.status = 'stale', a.is_stale = true, a.stale_reason = $reason, "
            "a.updated_at = datetime()",
            {"reason": reason},
        )
        self._run(
            "MATCH (a:KGAssertion {status: 'stale'}) "
            "MATCH ()-[link:KG_ASSERTED_LINK]->() WHERE link.assertion_id = a.id "
            "DELETE link",
            {},
        )
        self._run(
            "MATCH (c:KGLinkCandidate)-[:KG_REVIEWED_AS]->(a:KGAssertion {status: 'stale'}) "
            "SET c.is_stale = true, c.stale_at = datetime(), c.stale_reason = $reason",
            {"reason": reason},
        )
    def delete_root(self, root_id: str) -> None:
        self.invalidate_derived_analysis(root_id, reason="Root wird neu aufgebaut oder entfernt")
        self._run("MATCH (n) WHERE n.root_id = $root_id DETACH DELETE n", {"root_id": root_id})
        self._run("MATCH (r:KGRoot {id: $root_id}) DETACH DELETE r", {"root_id": root_id})
        self._cleanup_graph_intelligence_orphans()
        # Root deletion occurs after the first invalidation; re-check provenance now
        # that the root's evidence nodes are actually gone.
        self._mark_stale_human_assertions("Root wurde neu aufgebaut oder entfernt; Belege erneut prüfen")
        self.cleanup_graph_maintenance(cleanup_orphans=False)

    def _cleanup_graph_intelligence_orphans(self) -> dict[str, int]:
        """Remove derived nodes whose supporting sources no longer exist."""
        queries = {
            "orphan_insights_deleted": (
                "MATCH (i:KGInsight) "
                "WHERE NOT EXISTS { MATCH (r:KGAnalysisRun) WHERE r.id = i.run_id } "
                "WITH i DETACH DELETE i RETURN count(i) AS deleted"
            ),
            "orphan_content_identities_deleted": (
                "MATCH (g:KGContentIdentity) "
                "WHERE NOT EXISTS { MATCH (:KGFile)-[:KG_HAS_CONTENT_IDENTITY]->(g) } "
                "WITH g DETACH DELETE g RETURN count(g) AS deleted"
            ),
            "orphan_extraction_assertions_deleted": (
                "MATCH (a:KGAssertion) WHERE a.method = 'llm_entity_extraction' "
                "AND NOT EXISTS { MATCH (a)-[:KG_ASSERTED_FROM]->(:KGChunk) } "
                "WITH a DETACH DELETE a RETURN count(a) AS deleted"
            ),
            "proposed_candidates_without_evidence_deleted": (
                "MATCH (c:KGLinkCandidate) WHERE c.status = 'proposed' "
                "AND size(coalesce(c.evidence_chunk_ids, [])) > 0 "
                "AND NOT EXISTS { UNWIND c.evidence_chunk_ids AS evidence_id "
                "MATCH (chunk:KGChunk) WHERE chunk.id = evidence_id } "
                "WITH c DETACH DELETE c RETURN count(c) AS deleted"
            ),
            "orphan_communities_deleted": (
                "MATCH (c:KGCommunity) "
                "WHERE NOT EXISTS { MATCH (:KGEntity)-[:KG_IN_COMMUNITY]->(c) } "
                "WITH c DETACH DELETE c RETURN count(c) AS deleted"
            ),
            "orphan_components_deleted": (
                "MATCH (c:KGComponent) "
                "WHERE NOT EXISTS { MATCH (:KGEntity)-[:KG_IN_COMPONENT]->(c) } "
                "WITH c DETACH DELETE c RETURN count(c) AS deleted"
            ),
            "orphan_consolidated_facts_deleted": (
                "MATCH (f:KGConsolidatedFact) "
                "WHERE NOT EXISTS { MATCH (:KGAssertion)-[:KG_SUPPORTS_FACT]->(f) } "
                "WITH f DETACH DELETE f RETURN count(f) AS deleted"
            ),
        }
        counts: dict[str, int] = {}
        for key, query in queries.items():
            rows = self._run(query, {})
            counts[key] = int(rows[0].get("deleted", 0) or 0) if rows else 0
        return counts

    @staticmethod
    def _first_count(rows: list[dict[str, Any]]) -> int:
        return int(rows[0].get("deleted", rows[0].get("cleaned", 0)) or 0) if rows else 0

    def cleanup_graph_maintenance(
        self, *, keep_analysis_runs: int = 20, cleanup_orphans: bool = True
    ) -> dict[str, int]:
        """Keep the Neo4j graph compact without deleting source-backed knowledge.

        The most recent analysis runs are retained per indexed root (and separately
        for global analyses). Older runs are rolled into one singleton summary node.
        Only explicitly derived/stale nodes and unreviewed stale candidates are
        removed; accepted assertions, evidence, files, chunks and extraction cache
        entries are deliberately preserved.
        """
        keep_analysis_runs = max(1, min(int(keep_analysis_runs), 100))
        result: dict[str, int] = {}

        if cleanup_orphans:
            result.update(self._cleanup_graph_intelligence_orphans())

        # A source change invalidates these GDS results. Removing the derived values
        # prevents stale community IDs/scores from being mistaken for current ones.
        stale_cleanup_queries = {
            "stale_communities_deleted": (
                "MATCH (c:KGCommunity) WHERE coalesce(c.is_stale, false) = true "
                "WITH c DETACH DELETE c RETURN count(c) AS deleted"
            ),
            "stale_components_deleted": (
                "MATCH (c:KGComponent) WHERE coalesce(c.is_stale, false) = true "
                "WITH c DETACH DELETE c RETURN count(c) AS deleted"
            ),
            "stale_community_properties_cleared": (
                "MATCH (e:KGEntity) WHERE coalesce(e.kg_gds_community_stale, false) = true "
                "REMOVE e.kg_gds_community_id, e.kg_gds_community_run_id, "
                "e.kg_gds_community_algorithm, e.kg_gds_community_updated_at, "
                "e.kg_gds_community_stale RETURN count(e) AS cleaned"
            ),
            "stale_component_properties_cleared": (
                "MATCH (e:KGEntity) WHERE coalesce(e.kg_gds_component_stale, false) = true "
                "REMOVE e.kg_gds_component_id, e.kg_gds_component_run_id, "
                "e.kg_gds_component_updated_at, e.kg_gds_component_stale "
                "RETURN count(e) AS cleaned"
            ),
            "stale_pagerank_properties_cleared": (
                "MATCH (e:KGEntity) WHERE coalesce(e.kg_gds_pagerank_stale, false) = true "
                "REMOVE e.kg_gds_pagerank, e.kg_gds_pagerank_run_id, "
                "e.kg_gds_pagerank_updated_at, e.kg_gds_pagerank_stale "
                "RETURN count(e) AS cleaned"
            ),
            "similarity_edges_without_embeddings_deleted": (
                "MATCH (a:KGChunk)-[r:KG_SEMANTIC_SIMILAR_TO]-(b:KGChunk) "
                "WHERE a.embedding IS NULL OR b.embedding IS NULL "
                "WITH DISTINCT r DELETE r RETURN count(r) AS deleted"
            ),
            # Proposed candidates are reproducible and may be regenerated. Preserve
            # all human-reviewed decisions as durable feedback/history.
            "stale_proposed_candidates_deleted": (
                "MATCH (c:KGLinkCandidate) WHERE c.status = 'proposed' "
                "AND coalesce(c.is_stale, false) = true "
                "WITH c DETACH DELETE c RETURN count(c) AS deleted"
            ),
            "old_quality_decisions_deleted": (
                "MATCH (c:KGLinkCandidate)-[:KG_HAS_QUALITY_DECISION]->(d:KGQualityDecision) "
                "WITH c, d ORDER BY d.created_at DESC, d.id DESC "
                "WITH c, collect(d) AS decisions WITH decisions[10..] AS obsolete "
                "UNWIND obsolete AS d WITH DISTINCT d DETACH DELETE d RETURN count(d) AS deleted"
            ),
        }
        for key, query in stale_cleanup_queries.items():
            rows = self._run(query, {})
            result[key] = self._first_count(rows)

        # A process can be force-closed while a run is still marked running. Age
        # out only clearly abandoned runs (24h) so they can enter the normal retention
        # and summary path; short or currently active runs are never touched.
        abandoned_rows = self._run(
            "MATCH (r:KGAnalysisRun {status: 'running'}) "
            "WHERE r.started_at IS NOT NULL AND r.started_at < datetime() - duration('P1D') "
            "SET r.status = 'failed', r.finished_at = datetime(), "
            "r.error = coalesce(r.error, 'Analyse seit mehr als 24 Stunden ohne Abschluss; Cleanup hat den Lauf als fehlgeschlagen markiert') "
            "RETURN count(r) AS marked_failed",
            {},
        )
        result["abandoned_analysis_runs_marked_failed"] = int(abandoned_rows[0].get("marked_failed", 0) or 0) if abandoned_rows else 0

        # Read completed/non-running runs beyond the retention window per root.
        # A single summary node stores compact aggregate history so the graph does
        # not grow by one archive node per run.
        old_runs = self._run(
            "MATCH (r:KGAnalysisRun) WHERE coalesce(r.status, '') <> 'running' "
            "WITH r.root_id AS scope, r "
            "ORDER BY scope, coalesce(r.finished_at, r.started_at) DESC, r.id DESC "
            "WITH scope, collect(r) AS ordered_runs "
            "WITH ordered_runs[$keep_runs..] AS obsolete_runs "
            "UNWIND obsolete_runs AS r "
            "OPTIONAL MATCH (r)-[:KG_PRODUCED]->(i:KGInsight) "
            "WITH r, count(DISTINCT i) AS linked_insights "
            "RETURN r.id AS id, r.root_id AS root_id, r.analysis_type AS analysis_type, "
            "r.status AS status, r.summary_json AS summary_json, "
            "coalesce(r.insight_count, linked_insights) AS insight_count, "
            "coalesce(r.candidate_count, 0) AS candidate_count, "
            "toString(r.finished_at) AS finished_at, toString(r.started_at) AS started_at "
            "ORDER BY finished_at DESC, started_at DESC, id DESC",
            {"keep_runs": keep_analysis_runs},
        )
        run_ids = [str(row.get("id")) for row in old_runs if row.get("id")]
        result["analysis_runs_archived"] = len(run_ids)
        result["analysis_runs_deleted"] = 0
        result["analysis_insights_archived"] = sum(
            int(row.get("insight_count", 0) or 0) for row in old_runs
        )
        result["analysis_candidates_archived_count"] = sum(
            int(row.get("candidate_count", 0) or 0) for row in old_runs
        )

        if run_ids:
            summary_rows = self._run(
                "MATCH (s:KGAnalysisHistorySummary {id: 'global'}) "
                "RETURN coalesce(s.status_counts_json, '{}') AS status_counts_json, "
                "coalesce(s.type_counts_json, '{}') AS type_counts_json",
                {},
            )
            try:
                status_counts = json.loads(summary_rows[0].get("status_counts_json") or "{}") if summary_rows else {}
            except (TypeError, json.JSONDecodeError):
                status_counts = {}
            try:
                type_counts = json.loads(summary_rows[0].get("type_counts_json") or "{}") if summary_rows else {}
            except (TypeError, json.JSONDecodeError):
                type_counts = {}
            for row in old_runs:
                status = str(row.get("status") or "unknown")
                analysis_type = str(row.get("analysis_type") or "unknown")
                status_counts[status] = int(status_counts.get(status, 0) or 0) + 1
                type_counts[analysis_type] = int(type_counts.get(analysis_type, 0) or 0) + 1

            latest = old_runs[0]
            latest_summary = latest.get("summary_json")
            try:
                # Normalize JSON where possible but preserve raw legacy data otherwise.
                latest_summary = json.dumps(json.loads(latest_summary or "{}"), ensure_ascii=False)
            except (TypeError, json.JSONDecodeError):
                latest_summary = str(latest_summary or "{}")[:10000]

            # Update the singleton archive and delete the corresponding detail runs
            # in one Cypher transaction. A failure cannot increment the archive while
            # leaving the same run nodes behind to be counted a second time.
            deleted = self._run(
                "UNWIND $run_ids AS run_id "
                "MATCH (r:KGAnalysisRun {id: run_id}) "
                "WITH collect(r) AS runs "
                "WITH runs, size(runs) AS actual_runs, "
                "reduce(total = 0, item IN runs | total + coalesce(item.insight_count, 0)) AS actual_insights, "
                "reduce(total = 0, item IN runs | total + coalesce(item.candidate_count, 0)) AS actual_candidates "
                "MERGE (s:KGAnalysisHistorySummary {id: 'global'}) "
                "ON CREATE SET s.created_at = datetime(), s.archived_runs_total = 0, "
                "s.archived_insights_total = 0, s.archived_candidates_total = 0 "
                "SET s.archived_runs_total = coalesce(s.archived_runs_total, 0) + actual_runs, "
                "s.archived_insights_total = coalesce(s.archived_insights_total, 0) + actual_insights, "
                "s.archived_candidates_total = coalesce(s.archived_candidates_total, 0) + actual_candidates, "
                "s.status_counts_json = $status_counts_json, s.type_counts_json = $type_counts_json, "
                "s.last_archived_run_id = $last_archived_run_id, "
                "s.last_archived_status = $last_archived_status, "
                "s.last_archived_at = datetime(), s.last_archived_summary_json = $last_summary_json, "
                "s.last_cleanup_at = datetime() "
                "WITH s, runs UNWIND runs AS r "
                "WITH s, r DETACH DELETE r RETURN count(r) AS deleted",
                {"status_counts_json": json.dumps(status_counts, ensure_ascii=False),
                 "type_counts_json": json.dumps(type_counts, ensure_ascii=False),
                 "last_archived_run_id": str(latest.get("id") or ""),
                 "last_archived_status": str(latest.get("status") or "unknown"),
                 "last_summary_json": latest_summary,
                 "run_ids": run_ids},
            )
            result["analysis_runs_deleted"] = int(deleted[0].get("deleted", len(run_ids)) or 0) if deleted else len(run_ids)
            result["analysis_runs_archived"] = result["analysis_runs_deleted"]
            # Old run nodes own their diagnostics. Remove only the now-orphaned
            # KGInsight records after archiving their aggregate counts.
            post_archive = self._cleanup_graph_intelligence_orphans()
            result["archived_insights_deleted"] = post_archive.get("orphan_insights_deleted", 0)
            result["post_archive_orphans_deleted"] = sum(post_archive.values())
        else:
            result["archived_insights_deleted"] = 0
            result["post_archive_orphans_deleted"] = 0

        summary = self._run(
            "MATCH (s:KGAnalysisHistorySummary {id: 'global'}) "
            "RETURN coalesce(s.archived_runs_total, 0) AS archived_runs_total, "
            "coalesce(s.archived_insights_total, 0) AS archived_insights_total LIMIT 1",
            {},
        )
        result["analysis_history_archived_runs_total"] = int(summary[0].get("archived_runs_total", 0) or 0) if summary else 0
        result["analysis_history_archived_insights_total"] = int(summary[0].get("archived_insights_total", 0) or 0) if summary else 0
        log.info("Graph-Cleanup abgeschlossen: %s", result)
        return result

    def get_file_state(self, root_id: str) -> dict[str, dict[str, Any]]:
        rows = self._run(
            "MATCH (f:KGFile {root_id: $root_id}) "
            "RETURN f.relative_path AS relative_path, f.size AS size, f.mtime_ns AS mtime_ns, "
            "f.sha256 AS sha256, f.status AS status, f.chunks_count AS chunks_count, "
            "f.is_truncated AS is_truncated, f.needs_deep_analysis AS needs_deep_analysis, "
            "f.total_chunks AS total_chunks, f.omitted_chunks AS omitted_chunks",
            {"root_id": root_id},
        )
        return {str(row["relative_path"]): row for row in rows}

    def upsert_folders(self, root_id: str, root_path: Path, relative_paths: Iterable[str], scan_id: str) -> None:
        folders = sorted(set(relative_paths), key=lambda value: (value.count("/"), value.casefold()))
        rows: list[dict[str, Any]] = []
        links: list[dict[str, str]] = []
        for relative in folders:
            folder_id = self.folder_id_for(root_id, relative)
            path = root_path if not relative else root_path.joinpath(*relative.split("/"))
            name = root_path.name if not relative else Path(relative).name
            rows.append({"id": folder_id, "props": {
                "id": folder_id, "root_id": root_id, "path": str(path), "name": name,
                "relative_path": relative, "last_scan_id": scan_id,
            }})
            if relative:
                parent_relative = relative.rsplit("/", 1)[0] if "/" in relative else ""
                links.append({
                    "parent_id": self.folder_id_for(root_id, parent_relative),
                    "child_id": folder_id,
                })
        if rows:
            for start in range(0, len(rows), 500):
                self._run(
                    "UNWIND $rows AS row MERGE (f:KGFolder {id: row.id}) SET f += row.props",
                    {"rows": rows[start:start + 500]},
                )
            for start in range(0, len(links), 500):
                self._run(
                    "UNWIND $links AS link MATCH (p:KGFolder {id: link.parent_id}) "
                    "MATCH (f:KGFolder {id: link.child_id}) MERGE (p)-[:KG_CONTAINS]->(f)",
                    {"links": links[start:start + 500]},
                )
            root_folder_id = self.folder_id_for(root_id, "")
            self._run(
                "MATCH (r:KGRoot {id: $root_id}) "
                "MATCH (f:KGFolder {id: $folder_id}) "
                "MERGE (r)-[:KG_CONTAINS]->(f)",
                {"root_id": root_id, "folder_id": root_folder_id},
            )

    def get_entity_extraction_cache_many(self, cache_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Load successful LLM extraction results from Neo4j, updating usage metadata."""
        if not cache_ids:
            return {}
        rows = self._run(
            "UNWIND $ids AS cache_id MATCH (c:KGExtractionCache {id: cache_id}) "
            "SET c.hit_count = coalesce(c.hit_count, 0) + 1, c.last_used_at = datetime() "
            "RETURN c.id AS id, c.result_json AS result_json",
            {"ids": cache_ids},
        )
        results: dict[str, dict[str, Any]] = {}
        for row in rows:
            try:
                payload = json.loads(row.get("result_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                log.warning("Ungültiger Entity-Extraktionscache in Neo4j (id=%s)", row.get("id"))
                continue
            if isinstance(payload, dict) and isinstance(payload.get("entities", []), list) and isinstance(payload.get("relations", []), list):
                results[str(row["id"])] = {
                    "entities": payload.get("entities", []),
                    "relations": payload.get("relations", []),
                }
        return results

    def save_entity_extraction_cache_many(self, entries: list[dict[str, Any]]) -> int:
        """Persist successful extraction results for reuse across identical chunks."""
        if not entries:
            return 0
        rows = []
        for item in entries:
            rows.append({
                "id": str(item["id"]),
                "content_sha256": str(item["content_sha256"]),
                "extractor_signature": str(item["extractor_signature"]),
                "result_json": json.dumps(item["result"], ensure_ascii=False, separators=(",", ":")),
            })
        result = self._run(
            "UNWIND $entries AS item MERGE (c:KGExtractionCache {id: item.id}) "
            "ON CREATE SET c.created_at = datetime(), c.hit_count = 0 "
            "SET c.content_sha256 = item.content_sha256, "
            "c.extractor_signature = item.extractor_signature, c.result_json = item.result_json, "
            "c.updated_at = datetime() RETURN count(c) AS saved",
            {"entries": rows},
        )
        return int(result[0].get("saved", 0)) if result else 0

    def save_file(self, root_id: str, root_path: Path, relative_path: str,
                  parent_relative_path: str, metadata: dict[str, Any],
                  chunks: list[dict[str, Any]]) -> None:
        file_id = self.file_id_for(root_id, relative_path)
        folder_id = self.folder_id_for(root_id, parent_relative_path)
        file_props = {
            "id": file_id,
            "root_id": root_id,
            "path": str(root_path.joinpath(*relative_path.split("/"))),
            "relative_path": relative_path,
            "name": Path(relative_path).name,
            "extension": metadata.get("extension", Path(relative_path).suffix.casefold()),
            "size": int(metadata.get("size", 0)),
            "mtime_ns": int(metadata.get("mtime_ns", 0)),
            "sha256": metadata.get("sha256", ""),
            "status": metadata.get("status", "metadata_only"),
            "error": metadata.get("error", ""),
            "chunks_count": len(chunks),
            "is_truncated": bool(metadata.get("is_truncated", False)),
            "total_chunks": int(metadata.get("total_chunks", len(chunks))),
            "indexed_chunks": int(metadata.get("indexed_chunks", len(chunks))),
            "omitted_chunks": int(metadata.get("omitted_chunks", 0)),
            "needs_deep_analysis": bool(metadata.get("needs_deep_analysis", False)),
            "truncation_reason": str(metadata.get("truncation_reason", "")),
            "original_text_chars": int(metadata.get("original_text_chars", 0)),
            "indexed_text_chars": int(metadata.get("indexed_text_chars", 0)),
            "indexed_at": None,
            "last_scan_id": metadata.get("last_scan_id", ""),
        }
        # Persist a file and its chunks in one managed transaction. Previously each
        # of the three statements opened and committed its own session, multiplying
        # transaction/round-trip overhead for large directory scans.
        def write_file(tx: Any) -> None:
            # If content changed, the prior SHA-256 identity is no longer valid.
            # Remove only the derived identity edge; the identity node is cleaned
            # after scans if no files reference it anymore.
            tx.run(
                "MATCH (f:KGFile {id: $id})-[r:KG_HAS_CONTENT_IDENTITY]->() "
                "WHERE coalesce(f.sha256, '') <> $sha256 DELETE r",
                {"id": file_id, "sha256": file_props["sha256"]},
            ).consume()
            tx.run(
                "MERGE (f:KGFile {id: $id}) SET f += $props "
                "WITH f OPTIONAL MATCH (f)-[:KG_HAS_CHUNK]->(old:KGChunk) "
                "WITH f, collect(old) AS olds "
                "UNWIND CASE WHEN size(olds) = 0 THEN [null] ELSE [c IN olds | c.id] END AS old_id "
                "OPTIONAL MATCH ()-[r:KG_RELATED_TO {source_chunk_id: old_id}]-() DELETE r "
                "WITH DISTINCT f, olds "
                "UNWIND CASE WHEN size(olds) = 0 THEN [null] ELSE olds END AS old DETACH DELETE old",
                {"id": file_id, "props": file_props},
            ).consume()
            tx.run(
                "MATCH (folder:KGFolder {id: $folder_id}) "
                "MATCH (f:KGFile {id: $file_id}) "
                "MERGE (folder)-[:KG_CONTAINS]->(f)",
                {"folder_id": folder_id, "file_id": file_id},
            ).consume()
            if chunks:
                tx.run(
                    "MATCH (f:KGFile {id: $file_id}) "
                    "UNWIND $chunks AS row "
                    "MERGE (c:KGChunk {id: row.id}) SET c += row.props "
                    "FOREACH (_ IN CASE WHEN row.props.embedding IS NOT NULL THEN [1] ELSE [] END | SET c:KGChunkEmbedded) "
                    "MERGE (f)-[:KG_HAS_CHUNK]->(c)",
                    {"file_id": file_id, "chunks": chunks},
                ).consume()

        with self.driver.session(database=self.settings.neo4j_database) as session:
            session.execute_write(write_file)

    def mark_files_seen(self, root_id: str, relative_paths: list[str], scan_id: str) -> None:
        # Bound parameter size for very large directory trees.
        for start in range(0, len(relative_paths), 1000):
            batch = relative_paths[start:start + 1000]
            if batch:
                self._run(
                    "UNWIND $paths AS path MATCH (f:KGFile {root_id: $root_id, relative_path: path}) "
                    "SET f.last_scan_id = $scan_id",
                    {"root_id": root_id, "paths": batch, "scan_id": scan_id},
                )

    def cleanup_removed(self, root_id: str, scan_id: str) -> dict[str, int]:
        stale_files = self._run(
            "MATCH (f:KGFile {root_id: $root_id}) "
            "WHERE coalesce(f.last_scan_id, '') <> $scan_id "
            "WITH f DETACH DELETE f RETURN count(*) AS deleted",
            {"root_id": root_id, "scan_id": scan_id},
        )
        stale_folders = self._run(
            "MATCH (f:KGFolder {root_id: $root_id}) "
            "WHERE coalesce(f.last_scan_id, '') <> $scan_id "
            "WITH f DETACH DELETE f RETURN count(*) AS deleted",
            {"root_id": root_id, "scan_id": scan_id},
        )
        self._run(
            "MATCH ()-[r:KG_RELATED_TO]->() "
            "WHERE r.root_id = $root_id AND NOT EXISTS { MATCH (:KGChunk {id: r.source_chunk_id}) } "
            "DELETE r",
            {"root_id": root_id},
        )
        self._run(
            "MATCH (e:KGEntity {root_id: $root_id}) "
            "WHERE NOT EXISTS { MATCH (:KGChunk)-[:KG_MENTIONS]->(e) } DETACH DELETE e",
            {"root_id": root_id},
        )
        self._cleanup_graph_intelligence_orphans()
        return {
            "deleted_files": int(stale_files[0]["deleted"]) if stale_files else 0,
            "deleted_folders": int(stale_folders[0]["deleted"]) if stale_folders else 0,
        }

    def _quality_threshold(self, method: str) -> float:
        """Read a conservative learned threshold from Neo4j, falling back to config."""
        default = float(getattr(getattr(self, "settings", None), "quality_min_confidence", 0.82))
        try:
            rows = self._run(
                "MATCH (p:KGQualityProfile {id: $id}) RETURN p.threshold AS threshold LIMIT 1",
                {"id": method},
            )
            if rows and rows[0].get("threshold") is not None:
                return max(default, min(0.95, float(rows[0]["threshold"])))
        except Exception as exc:
            log.debug("Qualitätsschwelle konnte nicht geladen werden (%s): %s", method, exc)
        return default

    def _record_quality_outcomes(self, method: str, outcomes: list[dict[str, Any]]) -> None:
        """Persist audited validation results; repeated failures can only raise thresholds."""
        if not outcomes:
            return
        rows = [{"valid": bool(x.get("valid")), "reason": str(x.get("reason", ""))[:100]} for x in outcomes]
        base = float(getattr(getattr(self, "settings", None), "quality_min_confidence", 0.82))
        self._run(
            "UNWIND $outcomes AS item "
            "MERGE (p:KGQualityProfile {id: $method}) "
            "ON CREATE SET p.threshold = $base, p.created_at = datetime(), "
            "p.validation_count = 0, p.validation_passes = 0, p.validation_failures = 0 "
            "WITH p, item, coalesce(p.validation_count, 0) + 1 AS validations, "
            "coalesce(p.validation_passes, 0) + CASE WHEN item.valid THEN 1 ELSE 0 END AS passes, "
            "coalesce(p.validation_failures, 0) + CASE WHEN item.valid THEN 0 ELSE 1 END AS failures "
            "SET p.validation_count = validations, p.validation_passes = passes, p.validation_failures = failures, "
            "p.failure_rate = toFloat(failures) / validations, "
            "p.threshold = CASE WHEN validations >= 10 AND validations % 10 = 0 "
            "AND toFloat(failures) / validations >= 0.20 "
            "THEN CASE WHEN coalesce(p.threshold, $base) + 0.02 > 0.95 THEN 0.95 ELSE coalesce(p.threshold, $base) + 0.02 END "
            "ELSE coalesce(p.threshold, $base) END, "
            "p.last_validation_reason = item.reason, p.updated_at = datetime()",
            {"method": method, "outcomes": rows, "base": base},
        )

    def save_entities_for_chunk(self, root_id: str, chunk_id: str,
                                entities: list[dict[str, Any]],
                                relations: list[dict[str, Any]],
                                source_text: str | None = None) -> None:
        """Store extracted entities and source claims, promoting only deterministic, cited claims.

        LLM confidence is only one signal. Auto-activation also requires literal evidence,
        both names in the source, known polarity, a valid time interval and fresh source text.
        """
        if not entities:
            return
        mapped: list[dict[str, Any]] = []
        ids_by_name: dict[str, str] = {}
        canonical_by_name: dict[str, str] = {}
        for item in entities:
            name = str(item.get("name", "")).strip()
            entity_type = str(item.get("type", "Sonstiges")).strip() or "Sonstiges"
            if not name:
                continue
            entity_id = stable_id(root_id, "entity", entity_type.casefold(), name.casefold())
            canonical_id = stable_id("canonical-entity", entity_type.casefold(), normalize_text(name))
            mapped_item = {
                "id": entity_id, "name": name, "type": entity_type,
                "description": str(item.get("description", ""))[:700],
                "canonical_entity_id": canonical_id,
            }
            mapped.append(mapped_item)
            ids_by_name[name.casefold()] = entity_id
            canonical_by_name[name.casefold()] = canonical_id
        if not mapped:
            return
        self._run(
            "UNWIND $entities AS item MERGE (e:KGEntity {id: item.id}) "
            "SET e:KGNode, e.name = item.name, e.type = item.type, e.description = item.description, "
            "e.canonical_entity_id = item.canonical_entity_id, e.root_id = $root_id, e.updated_at = datetime()",
            {"entities": mapped, "root_id": root_id},
        )
        # Keep root-local entity nodes and explicitly connect exact normalized name/type matches.
        self._run(
            "UNWIND $entities AS item MATCH (e:KGEntity {id: item.id}) "
            "MATCH (other:KGEntity {canonical_entity_id: item.canonical_entity_id}) "
            "WHERE other.id <> e.id "
            "WITH e, other, CASE WHEN e.id < other.id THEN e ELSE other END AS left_node, "
            "CASE WHEN e.id < other.id THEN other ELSE e END AS right_node "
            "MERGE (left_node)-[:SAME_ENTITY]->(right_node)",
            {"entities": mapped},
        )
        mentions = [{"id": item["id"]} for item in mapped]
        self._run(
            "MATCH (c:KGChunk {id: $chunk_id}) UNWIND $entities AS item "
            "MATCH (e:KGEntity {id: item.id}) MERGE (c)-[:KG_MENTIONS]->(e)",
            {"chunk_id": chunk_id, "entities": mentions},
        )
        threshold = self._quality_threshold("source_extraction")
        relation_rows: list[dict[str, Any]] = []
        quality_outcomes: list[dict[str, Any]] = []
        for relation in relations:
            source_name = str(relation.get("source", "")).strip()
            target_name = str(relation.get("target", "")).strip()
            source_id = ids_by_name.get(source_name.casefold())
            target_id = ids_by_name.get(target_name.casefold())
            if not source_id or not target_id or source_id == target_id:
                continue
            relation_type = str(relation.get("type", "RELATED_TO")).strip().upper() or "RELATED_TO"
            evidence = str(relation.get("evidence", "")).strip()[:500]
            confidence = relation.get("confidence", 0.0)
            polarity = str(relation.get("polarity", "unknown"))
            valid_from = str(relation.get("valid_from", "")).strip()[:10]
            valid_until = str(relation.get("valid_until", "")).strip()[:10]
            validation = validate_relation_evidence(
                text=source_text or "", source=source_name, target=target_name,
                evidence=evidence, confidence=confidence, polarity=polarity,
                valid_from=valid_from, valid_until=valid_until,
                min_confidence=threshold,
            ) if source_text else {
                "accepted": False, "reason": "source_text_unavailable",
                "confidence": 0.0, "polarity": "unknown",
            }
            if source_text is not None:
                quality_outcomes.append({"valid": bool(validation["accepted"]), "reason": validation["reason"]})
            canonical_source = canonical_by_name.get(source_name.casefold(), stable_id("canonical-entity", source_name.casefold()))
            canonical_target = canonical_by_name.get(target_name.casefold(), stable_id("canonical-entity", target_name.casefold()))
            assertion_id = stable_id(
                "assertion", root_id, chunk_id, source_id, relation_type.casefold(), target_id,
            )
            row_status = "accepted" if validation.get("accepted") else "proposed"
            relation_rows.append({
                "source_id": source_id, "target_id": target_id, "type": relation_type,
                "evidence": evidence, "source_chunk_id": chunk_id, "root_id": root_id,
                "assertion_id": assertion_id, "status": row_status,
                "method": "auto_source_evidence" if row_status == "accepted" else "llm_entity_extraction",
                "confidence": float(validation.get("confidence", 0.0)),
                "polarity": str(validation.get("polarity", "unknown")),
                "valid_from": valid_from or None, "valid_until": valid_until or None,
                "validation_reason": str(validation.get("reason", "")),
                "canonical_subject_id": canonical_source,
                "canonical_object_id": canonical_target,
                "fact_id": stable_id("consolidated-fact", canonical_source, relation_type.casefold(), canonical_target),
            })
        if relation_rows:
            self._run(
                "UNWIND $relations AS item "
                "CALL (item) { MATCH (s:KGEntity {id: item.source_id}) RETURN s } "
                "CALL (item) { MATCH (t:KGEntity {id: item.target_id}) RETURN t } "
                "CALL (item) { MATCH (chunk:KGChunk {id: item.source_chunk_id}) RETURN chunk } "
                "MERGE (s)-[r:KG_RELATED_TO {source_chunk_id: item.source_chunk_id, relation_type: item.type}]->(t) "
                "SET r.status = item.status, r.evidence = item.evidence, r.confidence = item.confidence, "
                "r.polarity = item.polarity, r.validation_reason = item.validation_reason, "
                "r.root_id = item.root_id, r.updated_at = datetime() "
                "MERGE (a:KGAssertion {id: item.assertion_id}) "
                "ON CREATE SET a.created_at = datetime() "
                "SET a.status = item.status, a.method = item.method, a.subject_id = item.source_id, "
                "a.predicate = item.type, a.object_id = item.target_id, a.root_id = item.root_id, "
                "a.evidence = item.evidence, a.confidence = item.confidence, a.polarity = item.polarity, "
                "a.valid_from = item.valid_from, a.valid_until = item.valid_until, "
                "a.validation_reason = item.validation_reason, a.is_stale = false, "
                "a.evidence_chunk_ids = [item.source_chunk_id], a.evidence_count = 1, a.updated_at = datetime() "
                "MERGE (s)-[:KG_ASSERTS_SUBJECT]->(a) MERGE (a)-[:KG_ASSERTS_OBJECT]->(t) "
                "MERGE (a)-[:KG_ASSERTED_FROM]->(chunk) "
                "FOREACH (_ IN CASE WHEN item.status = 'accepted' THEN [1] ELSE [] END | "
                "MERGE (f:KGConsolidatedFact {id: item.fact_id}) "
                "SET f.canonical_subject_id = item.canonical_subject_id, f.predicate = item.type, "
                "f.canonical_object_id = item.canonical_object_id, f.updated_at = datetime() "
                "MERGE (a)-[:KG_SUPPORTS_FACT]->(f) "
                "MERGE (f)-[:KG_FACT_SUBJECT]->(s) MERGE (f)-[:KG_FACT_OBJECT]->(t))",
                {"relations": relation_rows},
            )
            fact_ids = sorted({row["fact_id"] for row in relation_rows if row["status"] == "accepted"})
            for fact_id in fact_ids:
                self._refresh_consolidated_fact(fact_id)
        if quality_outcomes:
            try:
                self._record_quality_outcomes("source_extraction", quality_outcomes)
            except Exception as exc:
                log.debug("Qualitätsprofil für Quellrelationen konnte nicht aktualisiert werden: %s", exc)

    def _refresh_consolidated_fact(self, fact_id: str) -> dict[str, Any]:
        """Aggregate independent evidence and suppress active links on contradiction."""
        rows = self._run(
            "MATCH (f:KGConsolidatedFact {id: $id}) "
            "OPTIONAL MATCH (a:KGAssertion)-[:KG_SUPPORTS_FACT]->(f) "
            "WHERE a.status = 'accepted' AND coalesce(a.is_stale, false) = false "
            "AND (a.valid_from IS NULL OR date(a.valid_from) <= date()) "
            "AND (a.valid_until IS NULL OR date(a.valid_until) >= date()) "
            "RETURN f.id AS id, collect(DISTINCT {id: a.id, polarity: coalesce(a.polarity, 'positive'), "
            "subject_id: a.subject_id, object_id: a.object_id, predicate: a.predicate, "
            "confidence: a.confidence, evidence: a.evidence}) AS assertions",
            {"id": fact_id},
        )
        if not rows:
            return {"id": fact_id, "status": "missing"}
        assertions = [item for item in (rows[0].get("assertions") or []) if item and item.get("id")]
        positives = [item for item in assertions if item.get("polarity") == "positive"]
        negatives = [item for item in assertions if item.get("polarity") == "negative"]
        if positives and negatives:
            status = "conflict"
        elif positives:
            status = "supported"
        elif negatives:
            status = "refuted"
        else:
            status = "unresolved"
        metrics = self._run(
            "MATCH (f:KGConsolidatedFact {id: $id}) "
            "OPTIONAL MATCH (a:KGAssertion)-[:KG_SUPPORTS_FACT]->(f) "
            "WHERE a.status = 'accepted' AND coalesce(a.is_stale, false) = false "
            "OPTIONAL MATCH (a)-[:KG_ASSERTED_FROM]->(chunk:KGChunk)<-[:KG_HAS_CHUNK]-(file:KGFile) "
            "RETURN count(DISTINCT chunk) AS evidence_chunks, count(DISTINCT file) AS source_files, "
            "count(DISTINCT CASE WHEN a.polarity = 'positive' THEN file END) AS positive_source_files, "
            "count(DISTINCT CASE WHEN a.polarity = 'negative' THEN file END) AS negative_source_files",
            {"id": fact_id},
        )
        metric = metrics[0] if metrics else {}
        self._run(
            "MATCH (f:KGConsolidatedFact {id: $id}) SET f.status = $status, "
            "f.supporting_assertion_ids = $assertion_ids, f.positive_assertion_count = $positive_count, "
            "f.negative_assertion_count = $negative_count, f.evidence_chunk_count = $evidence_chunks, "
            "f.independent_source_count = $source_files, f.positive_source_count = $positive_sources, "
            "f.negative_source_count = $negative_sources, f.updated_at = datetime()",
            {"id": fact_id, "status": status, "assertion_ids": [x["id"] for x in assertions],
             "positive_count": len(positives), "negative_count": len(negatives),
             "evidence_chunks": int(metric.get("evidence_chunks") or 0),
             "source_files": int(metric.get("source_files") or 0),
             "positive_sources": int(metric.get("positive_source_files") or 0),
             "negative_sources": int(metric.get("negative_source_files") or 0)},
        )
        # Remove all prior shortcut edges for every assertion attached to this fact first;
        # expired or stale assertions are not in the active list above and must not linger.
        self._run(
            "MATCH (a:KGAssertion)-[:KG_SUPPORTS_FACT]->(:KGConsolidatedFact {id: $id}) "
            "WITH collect(a.id) AS ids MATCH ()-[r:KG_ASSERTED_LINK]->() "
            "WHERE r.assertion_id IN ids DELETE r",
            {"id": fact_id},
        )
        assertion_ids = [x["id"] for x in assertions]
        if status == "supported":
            self._run(
                "UNWIND $assertions AS item MATCH (a:KGAssertion {id: item.id}) "
                "MATCH (s:KGEntity {id: a.subject_id}) MATCH (o:KGEntity {id: a.object_id}) "
                "WHERE coalesce(a.polarity, 'positive') = 'positive' "
                "MERGE (s)-[link:KG_ASSERTED_LINK {assertion_id: a.id}]->(o) "
                "SET link.predicate = a.predicate, link.status = 'accepted', link.method = a.method, "
                "link.evidence = a.evidence, link.score = coalesce(a.confidence, 0.0), "
                "link.root_id = a.root_id, link.fact_id = $fact_id, link.updated_at = datetime()",
                {"assertions": [x for x in assertions if x.get("polarity") == "positive"], "fact_id": fact_id},
            )
        else:
            self._run(
                "MATCH ()-[r:KG_ASSERTED_LINK]->() WHERE r.assertion_id IN $ids DELETE r",
                {"ids": assertion_ids},
            )
        return {
            "id": fact_id, "status": status, "positive_assertions": len(positives),
            "negative_assertions": len(negatives), "independent_sources": int(metric.get("source_files") or 0),
        }

    def refresh_consolidated_facts(self) -> dict[str, int]:
        """Re-evaluate temporal validity and contradictions without repeating LLM calls."""
        rows = self._run("MATCH (f:KGConsolidatedFact) RETURN f.id AS id ORDER BY f.id", {})
        counts = {"checked": 0, "supported": 0, "conflict": 0, "refuted": 0}
        for row in rows:
            result = self._refresh_consolidated_fact(str(row.get("id")))
            counts["checked"] += 1
            if result.get("status") in counts:
                counts[result["status"]] += 1
        return counts

    def apply_autonomous_candidate_review(
        self, candidate_id: str, *, decision: str, predicate: str = "RELATED_TO",
        evidence: str = "", evidence_chunk_id: str = "", confidence: float = 0.0,
        polarity: str = "unknown", valid_from: str = "", valid_until: str = "",
        reason: str = "", min_confidence: float | None = None,
        source_snapshot: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Finalize an LLM decision only after re-reading and verifying source chunks."""
        rows = self._run(
            "MATCH (c:KGLinkCandidate {id: $id}) "
            "OPTIONAL MATCH (chunk:KGChunk) WHERE chunk.id IN coalesce(c.evidence_chunk_ids, []) "
            "RETURN c.id AS id, c.status AS status, c.subject_id AS subject_id, c.subject_name AS subject_name, "
            "c.subject_type AS subject_type, c.object_id AS object_id, c.object_name AS object_name, "
            "c.is_stale AS is_stale, c.evidence_chunk_ids AS evidence_chunk_ids, "
            "collect(DISTINCT {id: chunk.id, text: chunk.text, sha256: chunk.sha256, path: chunk.path}) AS sources",
            {"id": candidate_id},
        )
        if not rows:
            raise ValueError(f"Beziehungskandidat nicht gefunden: {candidate_id}")
        row = rows[0]
        if row.get("status") != "proposed" or row.get("is_stale"):
            return {"id": candidate_id, "status": row.get("status"), "reason": "candidate_not_open_or_stale"}
        # Guard against a source changing while the LLM call was in flight. The same
        # candidate ID can survive chunk replacement, so candidate status alone is not
        # enough to prove that the model saw the current evidence.
        if source_snapshot is not None:
            current_sources = {str(item.get("id")): str(item.get("sha256") or "")
                               for item in (row.get("sources") or []) if item and item.get("id")}
            changed = any(
                str(item.get("id")) not in current_sources
                or current_sources[str(item.get("id"))] != str(item.get("sha256") or "")
                for item in source_snapshot if item and item.get("id")
            )
            if changed:
                decision_id = uuid.uuid4().hex
                self._run(
                    "MATCH (c:KGLinkCandidate {id: $id}) WHERE c.status = 'proposed' "
                    "SET c.is_stale = true, c.stale_reason = 'source_changed_during_review', "
                    "c.updated_at = datetime() "
                    "CREATE (d:KGQualityDecision {id: $decision_id, candidate_id: c.id, "
                    "decision: 'stale', method: 'source_snapshot_guard', "
                    "reason: 'Source changed while the LLM was reviewing the candidate', "
                    "created_at: datetime()}) MERGE (c)-[:KG_HAS_QUALITY_DECISION]->(d)",
                    {"id": candidate_id, "decision_id": decision_id},
                )
                return {"id": candidate_id, "status": "stale", "reason": "source_changed_during_review"}
        decision_id = uuid.uuid4().hex
        if decision == "reject":
            self._run(
                "MATCH (c:KGLinkCandidate {id: $id}) WHERE c.status = 'proposed' AND coalesce(c.is_stale, false) = false "
                "SET c.status = 'rejected', c.review_method = 'llm_autonomous', c.review_note = $reason, "
                "c.reviewed_at = datetime(), c.updated_at = datetime() "
                "CREATE (d:KGQualityDecision {id: $decision_id, candidate_id: c.id, decision: 'rejected', "
                "method: 'llm_autonomous', reason: $reason, confidence: $confidence, created_at: datetime()}) "
                "MERGE (c)-[:KG_HAS_QUALITY_DECISION]->(d) RETURN c.id AS id",
                {"id": candidate_id, "reason": str(reason)[:2000], "decision_id": decision_id,
                 "confidence": parse_confidence(confidence)},
            )
            return {"id": candidate_id, "status": "rejected", "reason": reason}
        if decision != "accept":
            self._run(
                "MATCH (c:KGLinkCandidate {id: $id}) WHERE c.status = 'proposed' "
                "SET c.status = 'deferred', c.review_method = 'llm_autonomous', c.review_note = $reason, "
                "c.reviewed_at = datetime(), c.updated_at = datetime() "
                "CREATE (d:KGQualityDecision {id: $decision_id, candidate_id: c.id, decision: 'deferred', "
                "method: 'llm_autonomous', reason: $reason, confidence: $confidence, created_at: datetime()}) "
                "MERGE (c)-[:KG_HAS_QUALITY_DECISION]->(d) RETURN c.id AS id",
                {"id": candidate_id, "reason": str(reason)[:2000], "decision_id": decision_id,
                 "confidence": parse_confidence(confidence)},
            )
            return {"id": candidate_id, "status": "deferred", "reason": reason}
        threshold = max(
            float(min_confidence if min_confidence is not None else getattr(self.settings, "quality_min_confidence", 0.82)),
            self._quality_threshold("candidate_review"),
        )
        sources = [source for source in (row.get("sources") or []) if source and source.get("id") and source.get("text")]
        selected = next((source for source in sources if source.get("id") == evidence_chunk_id), None)
        if not selected:
            selected = next((source for source in sources if normalize_text(evidence) in normalize_text(str(source.get("text", ""))) and len(normalize_text(evidence)) >= 12), None)
        if not selected:
            validation = {"accepted": False, "reason": "evidence_not_found_in_candidate_sources"}
        else:
            validation = validate_relation_evidence(
                text=str(selected.get("text", "")), source=str(row.get("subject_name", "")),
                target=str(row.get("object_name", "")), evidence=evidence, confidence=confidence,
                polarity=polarity, valid_from=valid_from, valid_until=valid_until,
                min_confidence=threshold,
            )
        if not validation.get("accepted"):
            final_reason = str(validation.get("reason") or "source_validation_failed")
            self._run(
                "MATCH (c:KGLinkCandidate {id: $id}) WHERE c.status = 'proposed' "
                "SET c.status = 'deferred', c.review_method = 'llm_autonomous', c.review_note = $reason, "
                "c.reviewed_at = datetime(), c.updated_at = datetime() "
                "CREATE (d:KGQualityDecision {id: $decision_id, candidate_id: c.id, decision: 'deferred', "
                "method: 'deterministic_source_validation', reason: $reason, confidence: $confidence, "
                "evidence: $evidence, evidence_chunk_id: $chunk_id, created_at: datetime()}) "
                "MERGE (c)-[:KG_HAS_QUALITY_DECISION]->(d)",
                {"id": candidate_id, "reason": final_reason, "decision_id": decision_id,
                 "confidence": parse_confidence(confidence),
                 "evidence": str(evidence)[:500], "chunk_id": selected.get("id") if selected else None},
            )
            try:
                self._record_quality_outcomes("candidate_review", [{"valid": False, "reason": final_reason}])
            except Exception:
                pass
            return {"id": candidate_id, "status": "deferred", "reason": final_reason}
        assertion_id = stable_id("autonomous-assertion", candidate_id, selected["id"], predicate, evidence)
        fact_id = stable_id(
            "consolidated-fact", stable_id("canonical-entity", str(row.get("subject_type") or "Sonstiges").casefold(), normalize_text(str(row.get("subject_name") or ""))),
            predicate.casefold(), stable_id("canonical-entity", str(row.get("object_type") or "Sonstiges").casefold(), normalize_text(str(row.get("object_name") or ""))),
        )
        saved = self._run(
            "MATCH (c:KGLinkCandidate {id: $candidate_id}) WHERE c.status = 'proposed' AND coalesce(c.is_stale, false) = false "
            "MATCH (s:KGEntity {id: c.subject_id}) MATCH (o:KGEntity {id: c.object_id}) "
            "MATCH (chunk:KGChunk {id: $chunk_id}) WHERE chunk.sha256 = $chunk_sha256 "
            "MERGE (a:KGAssertion {id: $assertion_id}) ON CREATE SET a.created_at = datetime() "
            "SET s.canonical_entity_id = $canonical_subject_id, o.canonical_entity_id = $canonical_object_id, "
            "a.subject_id = s.id, a.predicate = $predicate, a.object_id = o.id, a.root_id = c.root_id, "
            "a.status = 'accepted', a.method = 'llm_autonomous', a.evidence = $evidence, "
            "a.confidence = $confidence, a.polarity = $polarity, a.valid_from = $valid_from, "
            "a.valid_until = $valid_until, a.evidence_chunk_ids = [$chunk_id], a.evidence_count = 1, "
            "a.validation_reason = $reason, a.is_stale = false, a.source_candidate_id = c.id, a.updated_at = datetime() "
            "MERGE (s)-[:KG_ASSERTS_SUBJECT]->(a) MERGE (a)-[:KG_ASSERTS_OBJECT]->(o) "
            "MERGE (a)-[:KG_ASSERTED_FROM]->(chunk) "
            "MERGE (f:KGConsolidatedFact {id: $fact_id}) ON CREATE SET f.created_at = datetime() "
            "SET f.canonical_subject_id = $canonical_subject_id, f.predicate = $predicate, "
            "f.canonical_object_id = $canonical_object_id, f.updated_at = datetime() "
            "MERGE (a)-[:KG_SUPPORTS_FACT]->(f) MERGE (f)-[:KG_FACT_SUBJECT]->(s) MERGE (f)-[:KG_FACT_OBJECT]->(o) "
            "SET c.status = 'accepted', c.review_method = 'llm_autonomous', c.review_note = $reason, "
            "c.predicate = $predicate, c.evidence = $evidence, c.evidence_chunk_ids = [$chunk_id], "
            "c.confidence = $confidence, c.reviewed_at = datetime(), c.updated_at = datetime() "
            "CREATE (d:KGQualityDecision {id: $decision_id, candidate_id: c.id, decision: 'accepted', "
            "method: 'llm_autonomous', reason: $reason, confidence: $confidence, evidence: $evidence, "
            "polarity: $polarity, evidence_chunk_id: $chunk_id, predicate: $predicate, created_at: datetime()}) "
            "MERGE (c)-[:KG_HAS_QUALITY_DECISION]->(d) "
            "RETURN a.id AS assertion_id, c.id AS id",
            {"candidate_id": candidate_id, "chunk_id": selected["id"], "chunk_sha256": selected.get("sha256"),
             "decision_id": decision_id, "assertion_id": assertion_id, "predicate": str(predicate or "RELATED_TO")[:100],
             "evidence": str(evidence)[:500], "confidence": validation.get("confidence", confidence),
             "valid_from": valid_from or None, "valid_until": valid_until or None,
             "reason": str(reason)[:2000], "polarity": validation.get("polarity", "positive"), "fact_id": fact_id,
             "canonical_subject_id": stable_id("canonical-entity", str(row.get("subject_type") or "Sonstiges").casefold(), normalize_text(str(row.get("subject_name") or ""))),
             "canonical_object_id": stable_id("canonical-entity", str(row.get("object_type") or "Sonstiges").casefold(), normalize_text(str(row.get("object_name") or "")))},
        )
        if not saved:
            return {"id": candidate_id, "status": "deferred", "reason": "candidate_or_source_changed_during_review"}
        refresh = self._refresh_consolidated_fact(fact_id)
        try:
            self._record_quality_outcomes("candidate_review", [{"valid": True, "reason": "evidence_validated"}])
        except Exception:
            pass
        return {"id": candidate_id, "status": "accepted", "assertion_id": assertion_id, "fact": refresh}

    def vector_search(self, query_vector: list[float], limit: int,
                      root_id: str | None = None) -> list[dict[str, Any]]:
        index_name = f"kg_chunk_embedding_{len(query_vector)}"
        query = (
            f"CALL db.index.vector.queryNodes($index, $k, $embedding) YIELD node, score "
            "WHERE $root_id IS NULL OR node.root_id = $root_id "
            "RETURN node.id AS id, score ORDER BY score DESC LIMIT $limit"
        )
        try:
            return self._run(query, {
                "index": index_name, "k": max(limit * 5, limit),
                "embedding": query_vector, "root_id": root_id, "limit": limit,
            })
        except Exception as exc:
            log.debug("Vektorsuche nicht verfügbar (Index %s): %s", index_name, exc)
            return []

    @staticmethod
    def _lucene_query(query: str) -> str:
        # Quote terms to avoid treating user input as Lucene syntax.
        terms = re.findall(r"[\w.-]+", query, flags=re.UNICODE)
        terms = [term for term in terms if term]
        return " AND ".join('"' + term.replace('\\', '\\\\').replace('"', '\\"') + '"' for term in terms)

    def fulltext_search(self, query: str, limit: int,
                        root_id: str | None = None) -> list[dict[str, Any]]:
        lucene = self._lucene_query(query)
        if not lucene:
            return []
        try:
            return self._run(
                "CALL db.index.fulltext.queryNodes('kg_chunks_fulltext', $query) YIELD node, score "
                "WHERE $root_id IS NULL OR node.root_id = $root_id "
                "RETURN node.id AS id, score ORDER BY score DESC LIMIT $limit",
                {"query": lucene, "root_id": root_id, "limit": limit},
            )
        except Exception as exc:
            log.debug("Chunk-Volltextsuche fehlgeschlagen: %s", exc)
            return []

    def file_fulltext_search(self, query: str, limit: int,
                             root_id: str | None = None) -> list[dict[str, Any]]:
        lucene = self._lucene_query(query)
        if not lucene:
            return []
        try:
            return self._run(
                "CALL db.index.fulltext.queryNodes('kg_files_fulltext', $query) YIELD node, score "
                "WHERE $root_id IS NULL OR node.root_id = $root_id "
                "RETURN node.id AS id, score ORDER BY score DESC LIMIT $limit",
                {"query": lucene, "root_id": root_id, "limit": limit},
            )
        except Exception as exc:
            log.debug("Datei-Volltextsuche fehlgeschlagen: %s", exc)
            return []

    def graph_related_chunk_search(
        self, seed_chunk_ids: list[str], limit: int = 20, root_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Expand lexical/vector seeds through entity relations stored in Neo4j.

        This is a bounded graph-retrieval path, not an LLM step. It traverses at
        most two entity relationships and only follows active asserted links.
        """
        seed_ids = list(dict.fromkeys(str(item) for item in seed_chunk_ids if item))[:20]
        if not seed_ids:
            return []
        limit = max(1, min(int(limit), 100))
        try:
            return self._run(
                "UNWIND $seed_ids AS seed_id "
                "MATCH (seed:KGChunk {id: seed_id})-[:KG_MENTIONS]->(start:KGEntity) "
                "MATCH p=(start)-[:KG_ASSERTED_LINK*1..2]-(target:KGEntity) "
                "WHERE target.id <> start.id "
                "AND ($root_id IS NULL OR target.root_id = $root_id) "
                "AND all(rel IN relationships(p) WHERE rel.status = 'accepted') "
                "MATCH (target_chunk:KGChunk)-[:KG_MENTIONS]->(target) "
                "WHERE target_chunk.id <> seed.id AND NOT target_chunk.id IN $seed_ids "
                "AND ($root_id IS NULL OR target_chunk.root_id = $root_id) "
                "WITH target_chunk, min(length(p)) AS hops, count(DISTINCT seed) AS supporting_seeds, "
                "collect(DISTINCT target.name)[0..8] AS via_entities "
                "RETURN target_chunk.id AS id, hops, via_entities, supporting_seeds, "
                "(1.0 / toFloat(hops) + 0.1 * toFloat(supporting_seeds)) AS score "
                "ORDER BY score DESC, target_chunk.id LIMIT $limit",
                {"seed_ids": seed_ids, "root_id": root_id, "limit": limit},
            )
        except Exception as exc:
            log.debug("Graph-basierte Sucherweiterung nicht verfügbar: %s", exc)
            return []

    def semantic_related_chunk_search(
        self, seed_chunk_ids: list[str], limit: int = 20, root_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Expand search seeds over current GDS KNN similarity edges.

        Similarity edges represent embedding proximity only, not confirmed
        business relations. They are a separate retrieval signal and are removed
        when source data changes or marked stale by the analysis lifecycle.
        """
        seed_ids = list(dict.fromkeys(str(item) for item in seed_chunk_ids if item))[:20]
        if not seed_ids:
            return []
        limit = max(1, min(int(limit), 100))
        try:
            return self._run(
                "UNWIND $seed_ids AS seed_id "
                "MATCH (seed:KGChunk {id: seed_id})-[r:KG_SEMANTIC_SIMILAR_TO]-(similar:KGChunk) "
                "WHERE coalesce(r.is_stale, false) = false AND similar.id <> seed.id "
                "AND NOT (similar.id IN $seed_ids) "
                "AND ($root_id IS NULL OR similar.root_id = $root_id) "
                "WITH similar, max(coalesce(r.similarity, 0.0)) AS similarity, "
                "count(DISTINCT seed.id) AS supporting_seeds "
                "RETURN similar.id AS id, 1 AS hops, [] AS via_entities, supporting_seeds, "
                "similarity AS similarity, similarity AS score, 'semantic_similarity' AS kind "
                "ORDER BY score DESC, supporting_seeds DESC, similar.id LIMIT $limit",
                {"seed_ids": seed_ids, "root_id": root_id, "limit": limit},
            )
        except Exception as exc:
            log.debug("Embeddings-basierte Graphsuche übersprungen: %s", exc)
            return []

    def get_chunk_details(self, chunk_id: str) -> dict[str, Any] | None:
        rows = self._run(
            "MATCH (c:KGChunk {id: $id}) "
            "OPTIONAL MATCH (f:KGFile {id: c.file_id}) "
            "OPTIONAL MATCH (c)-[:KG_MENTIONS]->(e:KGEntity) "
            "WITH c, f, collect(DISTINCT CASE WHEN e IS NULL THEN null ELSE {name: e.name, type: e.type, description: e.description} END) AS entities "
            "OPTIONAL MATCH (c)-[:KG_MENTIONS]->(mentioned:KGEntity)-[rel:KG_ASSERTED_LINK {status: 'accepted'}]-(linked:KGEntity) "
            "WITH c, f, entities, collect(DISTINCT CASE WHEN linked IS NULL THEN null ELSE "
            "{source: mentioned.name, target: linked.name, type: rel.predicate, "
            "status: rel.status, evidence: rel.evidence} END)[0..8] AS graph_relations "
            "OPTIONAL MATCH (c)-[:KG_MENTIONS]->(proposed:KGEntity)-[proposal:KG_RELATED_TO {status: 'proposed'}]-(proposed_linked:KGEntity) "
            "WITH c, f, entities, graph_relations, collect(DISTINCT CASE WHEN proposed_linked IS NULL THEN null ELSE "
            "{source: proposed.name, target: proposed_linked.name, type: proposal.relation_type, "
            "status: 'proposed', evidence: proposal.evidence} END)[0..8] AS proposed_relations "
            "OPTIONAL MATCH (e2:KGEntity)<-[:KG_MENTIONS]-(other:KGChunk)<-[:KG_HAS_CHUNK]-(otherFile:KGFile) "
            "WHERE e2.name IN [item IN entities WHERE item IS NOT NULL | item.name] AND other.id <> c.id "
            "WITH c, f, entities, graph_relations, proposed_relations, collect(DISTINCT CASE WHEN otherFile IS NULL THEN null ELSE {path: otherFile.path, name: otherFile.name} END)[0..5] AS related "
            "RETURN c.id AS id, c.text AS text, c.path AS path, c.filename AS filename, "
            "c.section AS section, c.page AS page, c.ordinal AS ordinal, "
            "coalesce(f.size, 0) AS file_size, f.status AS file_status, "
            "coalesce(f.is_truncated, false) AS is_truncated, "
            "coalesce(f.needs_deep_analysis, false) AS needs_deep_analysis, "
            "coalesce(f.total_chunks, 0) AS total_chunks, "
            "coalesce(f.indexed_chunks, 0) AS indexed_chunks, "
            "coalesce(f.omitted_chunks, 0) AS omitted_chunks, entities, graph_relations, proposed_relations, related",
            {"id": chunk_id},
        )
        if not rows:
            return None
        row = rows[0]
        row["entities"] = [item for item in (row.get("entities") or []) if item]
        row["related"] = [item for item in (row.get("related") or []) if item]
        return row

    def get_file(self, path_or_relative: str, root_id: str | None = None) -> dict[str, Any] | None:
        query = (
            "MATCH (f:KGFile) WHERE (f.path = $path OR f.relative_path = $path) "
            "AND ($root_id IS NULL OR f.root_id = $root_id) "
            "OPTIONAL MATCH (f)-[:KG_HAS_CHUNK]->(c:KGChunk) "
            "RETURN f.id AS id, f.path AS path, f.relative_path AS relative_path, f.name AS name, "
            "f.extension AS extension, f.size AS size, f.status AS status, "
            "coalesce(f.is_truncated, false) AS is_truncated, "
            "coalesce(f.total_chunks, 0) AS total_chunks, "
            "coalesce(f.indexed_chunks, 0) AS indexed_chunks, "
            "coalesce(f.omitted_chunks, 0) AS omitted_chunks, "
            "coalesce(f.needs_deep_analysis, false) AS needs_deep_analysis, "
            "f.truncation_reason AS truncation_reason, "
            "f.original_text_chars AS original_text_chars, f.indexed_text_chars AS indexed_text_chars, "
            "collect({id: c.id, text: c.text, page: c.page, section: c.section})[0..8] AS chunks "
            "ORDER BY f.path LIMIT 1"
        )
        rows = self._run(query, {"path": path_or_relative, "root_id": root_id})
        if not rows:
            return None
        rows[0]["chunks"] = [item for item in (rows[0].get("chunks") or []) if item.get("id")]
        return rows[0]

    def chunks_for_file(self, file_id: str, limit: int = 3) -> list[dict[str, Any]]:
        return self._run(
            "MATCH (f:KGFile {id: $id})-[:KG_HAS_CHUNK]->(c:KGChunk) "
            "RETURN c.id AS id ORDER BY c.ordinal LIMIT $limit",
            {"id": file_id, "limit": limit},
        )

    def related_files(self, path_or_relative: str, limit: int = 10,
                      root_id: str | None = None) -> list[dict[str, Any]]:
        return self._run(
            "MATCH (f:KGFile) WHERE (f.path = $path OR f.relative_path = $path) "
            "AND ($root_id IS NULL OR f.root_id = $root_id) "
            "MATCH (f)-[:KG_HAS_CHUNK]->(:KGChunk)-[:KG_MENTIONS]->(e:KGEntity)<-[:KG_MENTIONS]-(:KGChunk)<-[:KG_HAS_CHUNK]-(other:KGFile) "
            "WHERE other.id <> f.id "
            "RETURN other.path AS path, other.name AS name, count(DISTINCT e) AS shared_entities, "
            "collect(DISTINCT e.name)[0..8] AS entities "
            "ORDER BY shared_entities DESC LIMIT $limit",
            {"path": path_or_relative, "root_id": root_id, "limit": limit},
        )


    def list_roots(self) -> list[dict[str, Any]]:
        """Return indexed source roots for the GUI and API clients."""
        return self._run(
            "MATCH (r:KGRoot) "
            "RETURN r.id AS id, r.path AS path, r.name AS name, "
            "toString(r.updated_at) AS updated_at, "
            "r.embedding_signature AS embedding_signature "
            "ORDER BY toLower(r.name), r.path"
        )

    def graph_snapshot(self) -> dict[str, list[dict[str, Any]]]:
        """Return every node and relationship for the whole Neo4j database.

        Only lightweight display metadata is selected; document/chunk bodies and
        embedding vectors are intentionally not transferred to the visualizer.
        The snapshot is read-only and is not restricted to a selected source root.
        """
        nodes = self._run(
            "MATCH (n) "
            "RETURN elementId(n) AS id, labels(n) AS labels, "
            "coalesce(toString(n.name), toString(n.title), toString(n.path), "
            "toString(n.relative_path), toString(n.filename), toString(n.section), "
            "toString(n.predicate), toString(n.type), toString(n.canonical_name), "
            "toString(n.summary), toString(n.id), labels(n)[0], 'Knoten') AS label, "
            "coalesce(toString(n.path), toString(n.relative_path), "
            "toString(n.filename), toString(n.source_path), '') AS path, "
            "coalesce(toString(n.description), toString(n.summary), "
            "toString(n.section), toString(n.predicate), toString(n.type), '') AS detail"
        )
        relationships = self._run(
            "MATCH (a)-[r]->(b) "
            "RETURN elementId(a) AS source, elementId(b) AS target, type(r) AS type"
        )
        return {"nodes": nodes, "relationships": relationships}

    def graph_neighborhood(self, query: str, root_id: str | None = None,
                           limit: int = 12) -> dict[str, Any] | None:
        """Resolve a file and return a small, source-grounded graph neighborhood.

        The result is deliberately bounded for responsive GUI rendering. It is a
        read-only operation and does not change the stored graph.
        """
        query = query.strip()
        if not query:
            return None
        limit = max(1, min(int(limit), 30))
        files = self._run(
            "MATCH (f:KGFile) "
            "WHERE ($root_id IS NULL OR f.root_id = $root_id) "
            "AND (f.path = $query OR f.relative_path = $query OR f.name = $query "
            "OR toLower(f.name) CONTAINS toLower($query) "
            "OR toLower(f.relative_path) CONTAINS toLower($query)) "
            "RETURN f.id AS id, f.root_id AS root_id, f.path AS path, "
            "f.relative_path AS relative_path, f.name AS name, "
            "f.extension AS extension, f.status AS status, f.size AS size, "
            "coalesce(f.is_truncated, false) AS is_truncated, "
            "coalesce(f.total_chunks, 0) AS total_chunks, "
            "coalesce(f.indexed_chunks, 0) AS indexed_chunks, "
            "coalesce(f.omitted_chunks, 0) AS omitted_chunks, "
            "coalesce(f.needs_deep_analysis, false) AS needs_deep_analysis "
            "ORDER BY CASE WHEN f.path = $query OR f.relative_path = $query OR f.name = $query "
            "THEN 0 ELSE 1 END, f.path LIMIT 1",
            {"query": query, "root_id": root_id},
        )
        if not files:
            return None
        file_row = files[0]
        file_id = str(file_row["id"])
        parents = self._run(
            "MATCH (p:KGFolder)-[:KG_CONTAINS]->(f:KGFile {id: $id}) "
            "RETURN p.id AS id, p.name AS name, p.path AS path, "
            "p.relative_path AS relative_path ORDER BY p.path LIMIT 4",
            {"id": file_id},
        )
        chunks = self._run(
            "MATCH (f:KGFile {id: $id})-[:KG_HAS_CHUNK]->(c:KGChunk) "
            "WITH c ORDER BY c.ordinal "
            "RETURN c.id AS id, c.ordinal AS ordinal, c.section AS section, "
            "c.page AS page, substring(coalesce(c.text, ''), 0, 260) AS preview "
            "LIMIT $limit",
            {"id": file_id, "limit": min(limit, 12)},
        )
        entities = self._run(
            "MATCH (f:KGFile {id: $id})-[:KG_HAS_CHUNK]->(:KGChunk)-[:KG_MENTIONS]->(e:KGEntity) "
            "RETURN e.id AS id, e.name AS name, e.type AS type, "
            "e.description AS description ORDER BY toLower(e.name) LIMIT $limit",
            {"id": file_id, "limit": min(limit, 20)},
        )
        related = self.related_files(file_row["path"], limit, file_row["root_id"])
        return {
            "file": file_row,
            "parent_folders": parents,
            "chunks": chunks,
            "entities": entities,
            "related_files": related,
        }

    def files_needing_deep_analysis(self, root_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """Return files requiring follow-up because they were truncated or too large to read."""
        return self._run(
            "MATCH (f:KGFile) WHERE coalesce(f.needs_deep_analysis, false) = true "
            "AND ($root_id IS NULL OR f.root_id = $root_id) "
            "RETURN f.path AS path, f.relative_path AS relative_path, f.name AS name, "
            "f.status AS status, f.size AS size_bytes, f.total_chunks AS total_chunks, "
            "f.indexed_chunks AS indexed_chunks, f.omitted_chunks AS omitted_chunks, "
            "f.original_text_chars AS original_text_chars, f.indexed_text_chars AS indexed_text_chars, "
            "f.truncation_reason AS truncation_reason "
            "ORDER BY CASE WHEN f.status = 'too_large' THEN 0 ELSE 1 END, "
            "coalesce(f.omitted_chunks, 0) DESC, coalesce(f.size, 0) DESC LIMIT $limit",
            {"root_id": root_id, "limit": max(1, min(int(limit), 1000))},
        )

    def stats(self, root_id: str | None = None) -> dict[str, int]:
        where = " WHERE n.root_id = $root_id" if root_id else ""
        params = {"root_id": root_id} if root_id else {}
        result: dict[str, int] = {}
        for label, key in [("KGRoot", "roots"), ("KGFolder", "folders"), ("KGFile", "files"), ("KGChunk", "chunks"), ("KGEntity", "entities")]:
            if label == "KGRoot":
                q = "MATCH (n:KGRoot)" + (" WHERE n.id = $root_id" if root_id else "") + " RETURN count(n) AS count"
                qparams = {"root_id": root_id} if root_id else {}
            else:
                q = f"MATCH (n:{label})" + where + " RETURN count(n) AS count"
                qparams = params
            rows = self._run(q, qparams)
            result[key] = int(rows[0]["count"]) if rows else 0
        truncated_query = (
            "MATCH (n:KGFile) WHERE coalesce(n.needs_deep_analysis, false) = true"
            + (" AND n.root_id = $root_id" if root_id else "")
            + " RETURN count(n) AS count"
        )
        truncated_rows = self._run(truncated_query, params)
        result["truncated_files"] = int(truncated_rows[0]["count"]) if truncated_rows else 0
        for label, key in [("KGInsight", "insights"), ("KGLinkCandidate", "link_candidates"),
                           ("KGAssertion", "assertions"), ("KGAnalysisRun", "analysis_runs"), ("KGCommunity", "communities"),
                           ("KGComponent", "components"), ("KGContentIdentity", "content_identities"),
                           ("KGExtractionCache", "extraction_cache_entries"),
                           ("KGAnalysisHistorySummary", "analysis_history_summaries")]:
            query = f"MATCH (n:{label}) RETURN count(n) AS count"
            rows = self._run(query)
            result[key] = int(rows[0]["count"]) if rows else 0
        return result
