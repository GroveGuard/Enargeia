"""Neo4j-first graph analysis and autonomous evidence-based quality review.

Durable analysis, candidate decisions and consolidated facts are stored in Neo4j.
Cypher handles deterministic checks, optional GDS handles graph structure, and a
configured OpenAI-compatible LLM reviews bounded batches of open candidates. Every
LLM acceptance is revalidated against current source text before it becomes active.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

from .chunking import stable_id
from .entity_extraction import EntityExtractor
from .quality import normalize_text
if TYPE_CHECKING:
    from .neo4j_store import Neo4jStore

log = logging.getLogger(__name__)


class GraphIntelligence:
    """Analyse the stored graph, record evidence-backed findings, and suggest links."""

    GDS_ALGORITHMS = (
        "gds.leiden.write",
        "gds.louvain.write",
        "gds.wcc.write",
        "gds.labelPropagation.write",
        "gds.pageRank.write",
        "gds.nodeSimilarity.stream",
        "gds.knn.write",
        "gds.betweenness.stream",
        "gds.bridges.stream",
    )

    def __init__(self, store: Neo4jStore):
        self.store = store

    def capabilities(self) -> dict[str, Any]:
        """Report features actually exposed by this Neo4j instance."""
        report: dict[str, Any] = {
            "neo4j": {"connected": True, "version": None, "edition": None},
            "gds": {"available": False, "version": None, "algorithms": []},
            "indexes": [],
        }
        try:
            rows = self.store._run(
                "CALL dbms.components() YIELD name, versions, edition "
                "RETURN name, versions[0] AS version, edition LIMIT 5"
            )
            if rows:
                report["neo4j"].update({
                    "name": rows[0].get("name"),
                    "version": rows[0].get("version"),
                    "edition": rows[0].get("edition"),
                })
        except Exception as exc:
            report["neo4j"]["diagnostic"] = str(exc)[:500]

        try:
            rows = self.store._run("RETURN gds.version() AS version")
            if rows:
                report["gds"]["available"] = True
                report["gds"]["version"] = rows[0].get("version")
        except Exception as exc:
            report["gds"]["diagnostic"] = str(exc)[:500]

        try:
            rows = self.store._run(
                "SHOW PROCEDURES YIELD name "
                "WHERE name STARTS WITH 'gds.' "
                "RETURN collect(name) AS names"
            )
            available = set(rows[0].get("names", [])) if rows else set()
            report["gds"]["algorithms"] = sorted(
                name for name in available
                if any(token in name.lower() for token in (
                    "leiden", "louvain", "wcc", "labelpropagation", "pagerank",
                    "nodesimilarity", ".knn.", "betweenness", "bridges", "fastrp",
                ))
            )
            report["gds"]["available"] = report["gds"]["available"] or bool(available)
        except Exception as exc:
            # gds.version() is the primary availability signal. Procedure listing
            # can be denied even when GDS is installed, depending on privileges.
            report["gds"]["procedure_listing_diagnostic"] = str(exc)[:500]

        try:
            rows = self.store._run(
                "SHOW INDEXES YIELD name, type, state "
                "WHERE name STARTS WITH 'kg_' "
                "RETURN collect({name: name, type: type, state: state}) AS indexes"
            )
            report["indexes"] = rows[0].get("indexes", []) if rows else []
        except Exception as exc:
            report["index_diagnostic"] = str(exc)[:500]
        return report

    def analyze(
        self,
        *,
        root_id: str | None = None,
        max_sample_chunks: int = 5000,
        max_candidates: int = 250,
        run_gds: bool = True,
    ) -> dict[str, Any]:
        """Run deterministic analysis, optional GDS, fact refresh and bounded LLM review.

        Candidate links are not activated merely because they were proposed. A configured
        LLM may review a small batch, but accepted claims must pass deterministic checks
        against current source chunks in Neo4j.
        """
        max_sample_chunks = max(100, min(int(max_sample_chunks), 100_000))
        max_candidates = max(1, min(int(max_candidates), 2_000))
        self.store.ensure_schema()
        run_id = uuid.uuid4().hex
        started = datetime.now(timezone.utc).isoformat()
        self.store._run(
            "CREATE (r:KGAnalysisRun {id: $id, analysis_type: 'graph_intelligence', "
            "status: 'running', started_at: datetime(), root_id: $root_id, "
            "max_sample_chunks: $max_sample_chunks, max_candidates: $max_candidates})",
            {"id": run_id, "root_id": root_id, "max_sample_chunks": max_sample_chunks,
             "max_candidates": max_candidates},
        )

        result: dict[str, Any] = {
            "run_id": run_id,
            "started_at": started,
            "scope": "root" if root_id else "all indexed roots",
            "root_id": root_id,
            "gds": {"status": "not requested"},
            "insights": [],
            "link_candidates_created_or_updated": 0,
        }
        try:
            result["capabilities"] = self.capabilities()
            result["graph_stats"] = self._analysis_counts(root_id)
            result["review_feedback"] = self._review_feedback_metrics()
            reviewed_total = sum(int(item.get("reviewed", 0)) for item in result["review_feedback"])
            if reviewed_total:
                self._save_insight(
                    run_id=run_id,
                    kind="learning_feedback",
                    title="Qualität der Beziehungsvorschläge auswerten",
                    description=(
                        "Die Auswertung fasst menschliche Overrides und autonome Entscheidungen "
                        "nach Vorschlagsverfahren zusammen. Sie ist ein Feedbacksignal; automatische "
                        "Schwellenkalibrierung erfolgt separat nur anhand deterministischer Quellenprüfungen."
                    ),
                    score=float(reviewed_total),
                    details={
                        "methods": result["review_feedback"],
                        "minimum_reviews_for_calibration": 5,
                        "automatic_strategy_changes": False,
                    },
                )
            result["duplicate_content_groups"] = self._find_duplicate_content(run_id, root_id)
            result["unindexed_files"] = self._record_unindexed_files(run_id, root_id)
            result["isolated_entities"] = self._record_isolated_entities(run_id, root_id)
            result["link_candidates_created_or_updated"] = self._propose_co_mention_candidates(
                run_id, root_id, max_sample_chunks, max_candidates
            )
            # Review and consolidate before GDS: newly verified edges should be part
            # of this run's communities and summaries, not appear one analysis later.
            result["autonomous_quality"] = self._run_autonomous_quality_cycle(root_id=root_id)
            try:
                result["consolidated_fact_refresh"] = self.store.refresh_consolidated_facts()
            except Exception as exc:
                result["consolidated_fact_refresh"] = {"status": "failed", "reason": str(exc)[:500]}
                log.warning("Konsolidierte Fakten konnten nicht neu bewertet werden: %s", exc)
            if run_gds:
                result["gds"] = self.run_gds_analysis(run_id=run_id)
            # Include GDS and quality-cycle insights created after the deterministic pass.
            result["insights"] = self.list_insights(run_id=run_id, limit=100)

            summary = {
                "graph_stats": result["graph_stats"],
                "review_feedback": result["review_feedback"],
                "duplicate_content_groups": result["duplicate_content_groups"],
                "unindexed_files": result["unindexed_files"],
                "isolated_entities": result["isolated_entities"],
                "link_candidates_created_or_updated": result["link_candidates_created_or_updated"],
                "gds": result["gds"],
                "autonomous_quality": result.get("autonomous_quality", {}),
                "consolidated_fact_refresh": result.get("consolidated_fact_refresh", {}),
            }
            finished_rows = self.store._run(
                "MATCH (r:KGAnalysisRun {id: $id}) "
                "SET r.status = CASE WHEN r.status = 'running' THEN 'completed' ELSE r.status END, "
                "r.finished_at = datetime(), r.summary_json = $summary_json, "
                "r.insight_count = $insight_count, r.candidate_count = $candidate_count "
                "RETURN r.status AS status",
                {"id": run_id, "summary_json": json.dumps(summary, ensure_ascii=False),
                 "insight_count": len(result["insights"]),
                 "candidate_count": result["link_candidates_created_or_updated"]},
            )
            result["status"] = str(finished_rows[0].get("status", "completed")) if finished_rows else "unknown"
            maintenance = getattr(self.store, "cleanup_graph_maintenance", None)
            if result["status"] == "completed" and callable(maintenance):
                try:
                    result["graph_cleanup"] = maintenance()
                except Exception as exc:
                    # Analysis results are already committed. Maintenance is best-effort
                    # and must not mark an otherwise completed analysis as failed.
                    result["graph_cleanup_warning"] = str(exc)[:1000]
                    log.warning("Graph-Cleanup nach Analyse fehlgeschlagen: %s", exc)
            if result["status"] == "stale":
                result["notice"] = "Der Graph änderte sich während der Analyse. Ergebnisse wurden als veraltet markiert und sollten nach Abschluss der Indexierung erneut berechnet werden."
            return result
        except Exception as exc:
            log.exception("Graph-Analyse fehlgeschlagen (run_id=%s)", run_id)
            self.store._run(
                "MATCH (r:KGAnalysisRun {id: $id}) SET r.status = 'failed', "
                "r.finished_at = datetime(), r.error = $error",
                {"id": run_id, "error": str(exc)[:2000]},
            )
            raise

    def _run_autonomous_quality_cycle(self, *, root_id: str | None) -> dict[str, Any]:
        """Batch-review only open graph candidates; verified evidence is rechecked in Neo4jStore."""
        settings = getattr(self.store, "settings", None)
        if not settings or not bool(getattr(settings, "autonomous_quality_enabled", True)):
            return {"status": "disabled", "reviewed": 0, "llm_calls": 0}
        if not getattr(settings, "llm_base_url", "") or not getattr(settings, "llm_model", ""):
            return {"status": "skipped", "reason": "LLM endpoint/model is not configured", "reviewed": 0, "llm_calls": 0}
        max_candidates = max(1, min(int(getattr(settings, "quality_max_candidates_per_run", 20)), 100))
        batch_size = max(1, min(int(getattr(settings, "quality_batch_size", 5)), 10))
        rows = self.store._run(
            "MATCH (c:KGLinkCandidate) "
            "WHERE c.status = 'proposed' AND coalesce(c.is_stale, false) = false "
            "AND ($root_id IS NULL OR c.root_id = $root_id) "
            "MATCH (s:KGEntity {id: c.subject_id}) MATCH (o:KGEntity {id: c.object_id}) "
            "OPTIONAL MATCH (chunk:KGChunk) WHERE chunk.id IN coalesce(c.evidence_chunk_ids, []) "
            "WITH c, s, o, collect(DISTINCT CASE WHEN chunk IS NULL THEN null ELSE "
            "{id: chunk.id, text: chunk.text, sha256: chunk.sha256, path: chunk.path} END)[0..5] AS source_rows "
            "RETURN c.id AS id, c.predicate AS predicate, c.method AS method, c.reason AS reason, "
            "c.evidence_count AS evidence_count, c.evidence_chunk_ids AS evidence_chunk_ids, "
            "s.id AS subject_id, s.name AS subject_name, s.type AS subject_type, "
            "o.id AS object_id, o.name AS object_name, o.type AS object_type, source_rows AS sources "
            "ORDER BY coalesce(c.evidence_count, 0) DESC, coalesce(c.score, 0.0) DESC, c.last_seen_at DESC LIMIT $limit",
            {"root_id": root_id, "limit": max_candidates},
        )
        candidates: list[dict[str, Any]] = []
        for row in rows:
            row["sources"] = [x for x in (row.get("sources") or []) if x and x.get("id") and x.get("text")]
            if row["sources"]:
                candidates.append(row)
        if not candidates:
            return {"status": "completed", "eligible": 0, "reviewed": 0, "accepted": 0, "rejected": 0, "deferred": 0, "llm_calls": 0}
        extractor = EntityExtractor(settings)
        counts = {"accepted": 0, "rejected": 0, "deferred": 0, "errors": 0}
        llm_calls = 0
        results_by_id = {str(row["id"]): row for row in candidates}
        for offset in range(0, len(candidates), batch_size):
            batch = candidates[offset:offset + batch_size]
            try:
                llm_calls += 1
                decisions = extractor.review_candidates(batch)
            except Exception as exc:
                counts["errors"] += len(batch)
                log.warning("Autonome Qualitätsprüfung für %d Kandidaten fehlgeschlagen: %s", len(batch), exc)
                continue
            received: set[str] = set()
            for decision in decisions:
                cid = str(decision.get("candidate_id", ""))
                if cid not in results_by_id or cid not in {str(x["id"]) for x in batch} or cid in received:
                    continue
                received.add(cid)
                row = results_by_id[cid]
                action = str(decision.get("decision", "defer")).strip().casefold()
                action = action if action in {"accept", "reject"} else "defer"
                try:
                    stored = self.store.apply_autonomous_candidate_review(
                        cid, decision=action,
                        predicate=str(decision.get("predicate") or "RELATED_TO"),
                        evidence=str(decision.get("evidence") or ""),
                        evidence_chunk_id=str(decision.get("evidence_chunk_id") or ""),
                        confidence=decision.get("confidence", 0.0),
                        polarity=str(decision.get("polarity") or "unknown"),
                        valid_from=str(decision.get("valid_from") or ""),
                        valid_until=str(decision.get("valid_until") or ""),
                        reason=str(decision.get("reason") or "LLM autonomous review"),
                        min_confidence=float(getattr(settings, "quality_min_confidence", 0.82)),
                        source_snapshot=[{"id": source.get("id"), "sha256": source.get("sha256")}
                                         for source in row.get("sources", []) if source and source.get("id")],
                    )
                    status = str(stored.get("status", "deferred"))
                    if status in counts:
                        counts[status] += 1
                    else:
                        counts["deferred"] += 1
                except Exception as exc:
                    counts["errors"] += 1
                    log.warning("Autonome Entscheidung für Kandidat %s konnte nicht gespeichert werden: %s", cid, exc)
            # Missing/malformed LLM entries stay open for a later retry.
            counts["errors"] += len(set(str(x["id"]) for x in batch) - received)
        return {
            "status": "completed_with_errors" if counts["errors"] else "completed",
            "eligible": len(candidates), "reviewed": sum(counts[k] for k in ("accepted", "rejected", "deferred")),
            **counts, "llm_calls": llm_calls, "batch_size": batch_size,
            "max_candidates_per_run": max_candidates,
            "confidence_threshold": float(getattr(settings, "quality_min_confidence", 0.82)),
        }

    def _review_feedback_metrics(self) -> list[dict[str, Any]]:
        """Summarize reviewed outcomes per proposal method, entirely inside Neo4j."""
        rows = self.store._run(
            "MATCH (c:KGLinkCandidate) WHERE c.status IN ['accepted', 'rejected'] "
            "AND coalesce(c.is_stale, false) = false "
            "WITH coalesce(c.method, 'unknown') AS method, "
            "sum(CASE WHEN c.status = 'accepted' THEN 1 ELSE 0 END) AS accepted, "
            "sum(CASE WHEN c.status = 'rejected' THEN 1 ELSE 0 END) AS rejected "
            "WITH method, accepted, rejected, accepted + rejected AS reviewed "
            "RETURN method, accepted, rejected, reviewed, "
            "CASE WHEN reviewed = 0 THEN null ELSE toFloat(accepted) / reviewed END AS acceptance_rate "
            "ORDER BY reviewed DESC, method",
            {},
        )
        return [
            {
                "method": str(row.get("method") or "unknown"),
                "accepted": int(row.get("accepted") or 0),
                "rejected": int(row.get("rejected") or 0),
                "reviewed": int(row.get("reviewed") or 0),
                "acceptance_rate": (
                    float(row["acceptance_rate"])
                    if row.get("acceptance_rate") is not None else None
                ),
                "calibration_ready": int(row.get("reviewed") or 0) >= 5,
            }
            for row in rows
        ]

    def _analysis_counts(self, root_id: str | None) -> dict[str, int]:
        params = {"root_id": root_id}
        queries = {
            "files": "MATCH (f:KGFile) WHERE ($root_id IS NULL OR f.root_id = $root_id) RETURN count(f) AS value",
            "chunks": "MATCH (c:KGChunk) WHERE ($root_id IS NULL OR c.root_id = $root_id) RETURN count(c) AS value",
            "entities": "MATCH (e:KGEntity) WHERE ($root_id IS NULL OR e.root_id = $root_id) RETURN count(e) AS value",
            "asserted_entity_relationships": (
                "MATCH (a:KGEntity)-[r:KG_ASSERTED_LINK {status: 'accepted'}]-(b:KGEntity) "
                "WHERE ($root_id IS NULL OR r.root_id = $root_id) RETURN count(DISTINCT r) AS value"
            ),
        }
        counts: dict[str, int] = {}
        for key, query in queries.items():
            rows = self.store._run(query, params)
            counts[key] = int(rows[0].get("value", 0)) if rows else 0
        return counts

    def _find_duplicate_content(self, run_id: str, root_id: str | None) -> int:
        # Exact-content groups are derived data: remove groups that no longer have
        # at least two live file members before attaching the current duplicate set.
        self.store._run(
            "MATCH (g:KGContentIdentity)<-[:KG_HAS_CONTENT_IDENTITY]-(f:KGFile) "
            "WITH g, count(DISTINCT f) AS members WHERE members < 2 DETACH DELETE g",
            {},
        )
        rows = self.store._run(
            "MATCH (f:KGFile) WHERE f.sha256 IS NOT NULL AND f.sha256 <> '' "
            "AND ($root_id IS NULL OR f.root_id = $root_id) "
            "WITH f.sha256 AS sha256, count(f) AS file_count, "
            "collect(f.path)[0..5] AS sample_paths "
            "WHERE file_count > 1 RETURN sha256, file_count, sample_paths "
            "ORDER BY file_count DESC LIMIT 100",
            {"root_id": root_id},
        )
        hashes = [str(row["sha256"]) for row in rows if row.get("sha256")]
        if hashes:
            # A content identity node avoids quadratic pairwise duplicate edges and
            # lets every file with that exact digest join the same graph structure.
            self.store._run(
                "UNWIND $hashes AS hash "
                "MERGE (g:KGContentIdentity {id: hash}) "
                "ON CREATE SET g.sha256 = hash, g.created_at = datetime() "
                "SET g.last_seen_at = datetime() "
                "WITH g, hash MATCH (f:KGFile {sha256: hash}) "
                "MERGE (f)-[:KG_HAS_CONTENT_IDENTITY]->(g)",
                {"hashes": hashes},
            )
        for row in rows:
            self._save_insight(
                run_id=run_id,
                kind="duplicate_content",
                title=f"Identischer Dateiinhalt ({row['file_count']} Dateien)",
                description="Mehrere Dateien besitzen denselben SHA-256-Hash. Das ist ein exakter Inhaltsnachweis, keine semantische Ähnlichkeitsvermutung.",
                score=float(row["file_count"]),
                details={"sha256": row["sha256"], "file_count": row["file_count"],
                         "sample_paths": row.get("sample_paths", [])},
            )
        return len(rows)

    def _record_unindexed_files(self, run_id: str, root_id: str | None) -> int:
        rows = self.store._run(
            "MATCH (f:KGFile) WHERE ($root_id IS NULL OR f.root_id = $root_id) "
            "AND NOT EXISTS { MATCH (f)-[:KG_HAS_CHUNK]->(:KGChunk) } "
            "RETURN count(f) AS count, collect(f.path)[0..10] AS sample_paths",
            {"root_id": root_id},
        )
        count = int(rows[0].get("count", 0)) if rows else 0
        if count:
            self._save_insight(
                run_id=run_id,
                kind="coverage_gap",
                title=f"{count} Dateien ohne Text-Chunks",
                description="Diese Dateien sind nur über Metadaten vertreten oder konnten nicht als Text extrahiert werden. Sie sind Kandidaten für Parser-Verbesserungen oder gezielte Nachanalyse.",
                score=float(count),
                details={"count": count, "sample_paths": rows[0].get("sample_paths", [])},
            )
        return count

    def _record_isolated_entities(self, run_id: str, root_id: str | None) -> int:
        rows = self.store._run(
            "MATCH (e:KGEntity) WHERE ($root_id IS NULL OR e.root_id = $root_id) "
            "AND NOT EXISTS { MATCH (e)-[:KG_ASSERTED_LINK {status: 'accepted'}]-(:KGEntity) } "
            "WITH e OPTIONAL MATCH (chunk:KGChunk)-[:KG_MENTIONS]->(e) "
            "WITH e, count(chunk) AS mentions "
            "RETURN count(e) AS count, collect({name: e.name, type: e.type, mentions: mentions})[0..10] AS examples",
            {"root_id": root_id},
        )
        count = int(rows[0].get("count", 0)) if rows else 0
        if count:
            self._save_insight(
                run_id=run_id,
                kind="graph_gap",
                title=f"{count} Entitäten ohne explizite Beziehungen",
                description="Diese Entitäten werden in Dokumenten erwähnt, besitzen aber noch keine bestätigte fachliche Beziehung (KG_ASSERTED_LINK mit Status accepted). Das ist ein Prüfhinweis, kein Fehler: manche Entitäten sind tatsächlich eigenständig.",
                score=float(count),
                details={"count": count, "examples": rows[0].get("examples", [])},
            )
        return count

    def _propose_co_mention_candidates(
        self, run_id: str, root_id: str | None, max_sample_chunks: int, max_candidates: int
    ) -> int:
        rows = self.store._run(
            "MATCH (c:KGChunk)-[:KG_MENTIONS]->(:KGEntity) "
            "WHERE ($root_id IS NULL OR c.root_id = $root_id) "
            "WITH DISTINCT c ORDER BY c.id LIMIT $max_sample_chunks "
            "MATCH (c)-[:KG_MENTIONS]->(e:KGEntity) "
            "WITH c, collect(DISTINCT e) AS entities WHERE size(entities) > 1 "
            "UNWIND entities AS a UNWIND entities AS b "
            "WITH c, a, b WHERE a.id < b.id AND NOT EXISTS { "
            "MATCH (a)-[confirmed:KG_ASSERTED_LINK {status: 'accepted'}]-(b) } "
            "WITH a, b, count(DISTINCT c) AS evidence_count, "
            "collect(DISTINCT c.id)[0..8] AS evidence_chunk_ids "
            "WHERE evidence_count >= 2 AND NOT EXISTS { "
            "MATCH (existing:KGLinkCandidate) "
            "WHERE existing.subject_id = a.id AND existing.object_id = b.id "
            "AND existing.predicate = 'CO_MENTIONED_WITH' "
            "AND existing.status IN ['accepted', 'rejected', 'deferred'] "
            "AND coalesce(existing.is_stale, false) = false } "
            "RETURN a.id AS subject_id, a.name AS subject_name, a.type AS subject_type, "
            "b.id AS object_id, b.name AS object_name, b.type AS object_type, "
            "evidence_count, evidence_chunk_ids "
            "ORDER BY evidence_count DESC, subject_name, object_name LIMIT $max_candidates",
            {"root_id": root_id, "max_sample_chunks": max_sample_chunks,
             "max_candidates": max_candidates},
        )
        if not rows:
            return 0
        candidates: list[dict[str, Any]] = []
        for row in rows:
            item = {
                "subject_id": row["subject_id"],
                "subject_name": row.get("subject_name", ""),
                "subject_type": row.get("subject_type", ""),
                "object_id": row["object_id"],
                "object_name": row.get("object_name", ""),
                "object_type": row.get("object_type", ""),
                "predicate": "CO_MENTIONED_WITH",
                "evidence_count": int(row.get("evidence_count", 0)),
                "evidence_chunk_ids": row.get("evidence_chunk_ids", []),
                "score": float(row.get("evidence_count", 0)),
                "reason": "Beide Entitäten kommen wiederholt im selben Textabschnitt vor. Die Beziehung ist ein Vorschlag und wurde nicht automatisch als Tatsache bestätigt.",
                "method": "cypher_co_mention",
            }
            self._persist_link_candidate(item, run_id)
            candidates.append(item)
        self._save_insight(
            run_id=run_id,
            kind="link_candidates",
            title=f"{len(candidates)} mögliche Beziehungen zur Prüfung",
            description=(
                "Kandidaten wurden anhand wiederholter gemeinsamer Erwähnungen erzeugt. "
                "Sie bleiben zunächst Hypothesen; der nachfolgende Qualitätszyklus prüft sie "
                "gegen Originalquellen, bevor eine Beziehung aktiviert werden kann."
            ),
            score=float(len(candidates)),
            details={"candidate_count": len(candidates),
                     "sample": [{"subject": item["subject_name"], "predicate": item["predicate"],
                                 "object": item["object_name"], "evidence_count": item["evidence_count"]}
                                for item in candidates[:10]]},
        )
        return len(candidates)

    def _persist_link_candidate(self, item: dict[str, Any], run_id: str) -> str:
        """Store an evidence-grounded candidate without promoting it to a factual edge."""
        subject_id = str(item["subject_id"])
        object_id = str(item["object_id"])
        predicate = str(item.get("predicate") or "RELATED_TO")
        # Canonical ordering prevents duplicate candidates when an algorithm returns
        # the same pair in the opposite order.
        if subject_id > object_id:
            subject_id, object_id = object_id, subject_id
            item = {**item,
                    "subject_id": subject_id, "subject_name": item.get("object_name", ""),
                    "subject_type": item.get("object_type", ""),
                    "object_id": object_id, "object_name": item.get("subject_name", ""),
                    "object_type": item.get("subject_type", "")}
        candidate_id = stable_id("link-candidate", predicate.casefold(), subject_id, object_id)
        params = {
            "id": candidate_id, "predicate": predicate,
            "subject_id": subject_id, "object_id": object_id,
            "subject_name": str(item.get("subject_name", "")),
            "object_name": str(item.get("object_name", "")),
            "subject_type": str(item.get("subject_type", "")),
            "object_type": str(item.get("object_type", "")),
            "evidence_count": int(item.get("evidence_count", 0) or 0),
            "evidence_chunk_ids": list(item.get("evidence_chunk_ids", []) or []),
            "method": str(item.get("method") or "unknown"),
            "score": float(item.get("score", 0.0) or 0.0),
            "reason": str(item.get("reason", ""))[:2000], "run_id": run_id,
        }
        self.store._run(
            "MERGE (c:KGLinkCandidate {id: $id}) "
            "ON CREATE SET c.status = 'proposed', c.created_at = datetime() "
            "SET c.status = CASE WHEN coalesce(c.is_stale, false) = true THEN 'proposed' ELSE c.status END "
            "SET c.predicate = $predicate, c.subject_id = $subject_id, c.object_id = $object_id, "
            "c.subject_name = $subject_name, c.object_name = $object_name, "
            "c.subject_type = $subject_type, c.object_type = $object_type, "
            "c.method = $method, c.evidence_count = $evidence_count, "
            "c.evidence_chunk_ids = $evidence_chunk_ids, "
            "c.score = $score, c.reason = $reason, "
            "c.is_stale = CASE WHEN c.status = 'proposed' THEN false ELSE coalesce(c.is_stale, false) END, "
            "c.stale_reason = CASE WHEN c.status = 'proposed' THEN null ELSE c.stale_reason END, "
            "c.last_seen_at = datetime(), c.analysis_run_id = $run_id "
            "WITH c "
            "CALL (c) { MATCH (s:KGEntity {id: c.subject_id}) RETURN s } "
            "CALL (c) { MATCH (o:KGEntity {id: c.object_id}) RETURN o } "
            "SET c.root_id = CASE WHEN s.root_id = o.root_id THEN s.root_id ELSE null END "
            "MERGE (c)-[:KG_CANDIDATE_SUBJECT]->(s) "
            "MERGE (c)-[:KG_CANDIDATE_OBJECT]->(o) "
            "WITH c MATCH (r:KGAnalysisRun {id: $run_id}) "
            "SET c.is_stale = CASE WHEN c.status = 'proposed' THEN (r.status = 'stale') "
            "ELSE coalesce(c.is_stale, false) END, "
            "c.stale_reason = CASE WHEN c.status = 'proposed' AND r.status = 'stale' "
            "THEN r.stale_reason WHEN c.status = 'proposed' THEN null ELSE c.stale_reason END "
            "MERGE (r)-[:KG_PRODUCED]->(c)",
            params,
        )
        return candidate_id

    def _save_insight(
        self, *, run_id: str, kind: str, title: str, description: str,
        score: float, details: dict[str, Any]
    ) -> None:
        insight_id = stable_id("insight", run_id, kind, title)
        self.store._run(
            "MERGE (i:KGInsight {id: $id}) "
            "ON CREATE SET i.created_at = datetime() "
            "SET i.run_id = $run_id, i.kind = $kind, i.title = $title, "
            "i.description = $description, i.score = $score, "
            "i.details_json = $details_json, i.updated_at = datetime() "
            "WITH i MATCH (r:KGAnalysisRun {id: $run_id}) "
            "MERGE (r)-[:KG_PRODUCED]->(i) "
            "SET i.is_stale = (r.status = 'stale'), "
            "i.stale_reason = CASE WHEN r.status = 'stale' THEN r.stale_reason ELSE null END",
            {"id": insight_id, "run_id": run_id, "kind": kind, "title": title,
             "description": description, "score": score,
             "details_json": json.dumps(details, ensure_ascii=False, default=str)},
        )

    def _community_summary_snapshot(self, community_id: str) -> dict[str, Any] | None:
        """Return current member descriptions and evidence-backed relations for one community."""
        rows = self.store._run(
            "MATCH (c:KGCommunity {id: $community_id}) "
            "MATCH (e:KGEntity)-[:KG_IN_COMMUNITY]->(c) "
            "WITH c, collect(DISTINCT {id: e.id, name: e.name, type: e.type, "
            "description: coalesce(e.description, '')}) AS members "
            "OPTIONAL MATCH (s:KGEntity)-[r:KG_ASSERTED_LINK {status: 'accepted'}]->(o:KGEntity) "
            "WHERE EXISTS { MATCH (s)-[:KG_IN_COMMUNITY]->(c) } "
            "AND EXISTS { MATCH (o)-[:KG_IN_COMMUNITY]->(c) } "
            "OPTIONAL MATCH (a:KGAssertion {id: r.assertion_id, status: 'accepted'})-[:KG_ASSERTED_FROM]->(chunk:KGChunk) "
            "WHERE coalesce(a.is_stale, false) = false AND a.evidence IS NOT NULL "
            "AND size(trim(a.evidence)) >= 12 "
            "AND toLower(coalesce(chunk.text, '')) CONTAINS toLower(a.evidence) "
            "AND (a.valid_from IS NULL OR date(a.valid_from) <= date()) "
            "AND (a.valid_until IS NULL OR date(a.valid_until) >= date()) "
            "WITH c, members, s, r, o, a, chunk "
            "ORDER BY coalesce(s.id, ''), coalesce(r.predicate, ''), coalesce(o.id, ''), "
            "coalesce(chunk.path, ''), coalesce(a.id, '') "
            "WITH c, members, collect(DISTINCT CASE WHEN s IS NULL OR r IS NULL OR a IS NULL OR chunk IS NULL THEN null ELSE "
            "{source: s.name, predicate: coalesce(r.predicate, 'RELATED_TO'), target: o.name, "
            "evidence: a.evidence, source_path: chunk.path, source_sha256: chunk.sha256, "
            "polarity: coalesce(a.polarity, 'positive'), assertion_id: r.assertion_id} END)[0..60] AS relations "
            "RETURN c.id AS id, c.member_count AS member_count, c.root_id AS root_id, "
            "members, relations, c.summary_fingerprint AS summary_fingerprint",
            {"community_id": community_id},
        )
        if not rows:
            return None
        row = rows[0]
        row["members"] = [item for item in (row.get("members") or []) if item and item.get("id")]
        row["relations"] = [item for item in (row.get("relations") or []) if item and item.get("source") and item.get("target")]
        return row

    @staticmethod
    def _community_summary_fingerprint(snapshot: dict[str, Any]) -> str:
        """Hash only the grounded member/fact snapshot used for a summary."""
        fingerprint_payload = {
            "members": sorted([
                {"id": str(m.get("id") or ""), "name": str(m.get("name") or ""),
                 "type": str(m.get("type") or ""), "description": str(m.get("description") or "")}
                for m in (snapshot.get("members") or [])
            ], key=lambda item: item["id"]),
            "relations": sorted([
                {"source": str(r.get("source") or ""), "predicate": str(r.get("predicate") or ""),
                 "target": str(r.get("target") or ""), "evidence": str(r.get("evidence") or ""),
                 "source_path": str(r.get("source_path") or ""),
                 "source_sha256": str(r.get("source_sha256") or ""),
                 "polarity": str(r.get("polarity") or "")}
                for r in (snapshot.get("relations") or [])
            ], key=lambda item: (item["source"], item["predicate"], item["target"], item["evidence"], item["source_path"], item["source_sha256"])),
        }
        return stable_id(
            "community-summary-source-v1",
            json.dumps(fingerprint_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        )

    def _generate_community_summaries(self, *, run_id: str) -> dict[str, Any]:
        """Refresh only eligible summaries whose grounded graph snapshot changed."""
        settings = getattr(self.store, "settings", None)
        if not settings or not bool(getattr(settings, "community_summaries_enabled", True)):
            return {"status": "disabled", "eligible": 0, "generated": 0, "llm_calls": 0}
        if not getattr(settings, "llm_base_url", "") or not getattr(settings, "llm_model", ""):
            return {"status": "skipped", "reason": "LLM-Endpunkt/Modell nicht konfiguriert", "eligible": 0, "generated": 0, "llm_calls": 0}
        min_members = max(2, min(int(getattr(settings, "community_summary_min_members", 3)), 100))
        max_per_run = max(1, min(int(getattr(settings, "community_summary_max_per_run", 8)), 50))
        batch_size = max(1, min(int(getattr(settings, "community_summary_batch_size", 2)), 8))
        community_rows = self.store._run(
            "MATCH (c:KGCommunity) WHERE coalesce(c.is_stale, false) = false "
            "AND coalesce(c.member_count, 0) >= $min_members "
            "RETURN c.id AS id, c.member_count AS member_count, c.summary_fingerprint AS summary_fingerprint "
            "ORDER BY c.member_count DESC, c.id LIMIT 1000",
            {"min_members": min_members},
        )
        if not community_rows:
            return {"status": "completed", "eligible": 0, "generated": 0, "unchanged": 0, "llm_calls": 0,
                    "min_members": min_members, "max_per_run": max_per_run}
        snapshots: list[dict[str, Any]] = []
        unchanged = 0
        stale_inputs = 0
        for row in community_rows:
            snapshot = self._community_summary_snapshot(str(row.get("id") or ""))
            if not snapshot:
                continue
            members = snapshot.get("members") or []
            relations = snapshot.get("relations") or []
            # Do not summarize algorithmic clusters with no confirmed internal relation.
            # Descriptions can help explain a cluster, but cannot by themselves prove links.
            if len(members) < min_members or not relations:
                continue
            fingerprint = self._community_summary_fingerprint(snapshot)
            insight_id = stable_id("community-summary-insight-v1", str(row["id"]))
            existing = self.store._run(
                "MATCH (i:KGInsight {id: $id}) RETURN i.details_json AS details_json, "
                "coalesce(i.is_stale, false) AS is_stale",
                {"id": insight_id},
            )
            detail: dict[str, Any] = {}
            if existing:
                try:
                    detail = json.loads(existing[0].get("details_json") or "{}")
                except (TypeError, json.JSONDecodeError):
                    detail = {}
            if detail.get("source_fingerprint") == fingerprint:
                # No LLM call: reactivate the unchanged summary, attach to the current
                # analysis run, and clear stale markers only because its input hash matches.
                reused_rows = self.store._run(
                    "MATCH (r:KGAnalysisRun {id: $run_id}) WHERE r.status = 'running' "
                    "MATCH (c:KGCommunity {id: $community_id}) "
                    "WHERE coalesce(c.is_stale, false) = false "
                    "MATCH (i:KGInsight {id: $insight_id}) "
                    "OPTIONAL MATCH (old_run:KGAnalysisRun)-[old_rel:KG_PRODUCED]->(i) "
                    "WHERE old_run.id <> $run_id "
                    "WITH r, c, i, collect(old_rel) AS old_rels "
                    "FOREACH (rel IN old_rels | DELETE rel) "
                    "SET c.summary_fingerprint = $fingerprint, "
                    "i.is_stale = false, i.stale_reason = null, i.run_id = $run_id, i.updated_at = datetime() "
                    "MERGE (r)-[:KG_PRODUCED]->(i) MERGE (i)-[:KG_SUMMARIZES_COMMUNITY]->(c) "
                    "RETURN i.id AS id",
                    {"community_id": row["id"], "insight_id": insight_id,
                     "fingerprint": fingerprint, "run_id": run_id},
                )
                if reused_rows:
                    unchanged += 1
                else:
                    stale_inputs += 1
                continue
            snapshots.append({
                "id": str(row["id"]), "insight_id": insight_id, "fingerprint": fingerprint,
                "member_count": len(members), "members": members, "relations": relations,
                "previous_fingerprint": detail.get("source_fingerprint"),
            })
        # Prefer larger and better connected communities; bounded work prevents an
        # oversized API bill when the graph produces many groups at once.
        snapshots.sort(key=lambda item: (item["member_count"], len(item["relations"]), item["id"]), reverse=True)
        pending = snapshots[:max_per_run]
        llm_calls = 0
        generated = 0
        errors = 0
        extractor = EntityExtractor(settings)
        for offset in range(0, len(pending), batch_size):
            batch = pending[offset:offset + batch_size]
            try:
                llm_calls += 1
                summaries = extractor.summarize_communities(batch)
            except Exception as exc:
                errors += len(batch)
                log.warning("Community-Zusammenfassung fehlgeschlagen: %s", exc)
                continue
            by_id = {str(item.get("community_id")): item for item in summaries}
            for item in batch:
                summary = by_id.get(item["id"])
                if not summary:
                    errors += 1
                    continue
                details = {
                    "summary": summary["summary"],
                    "key_points": summary.get("key_points", []),
                    "conflicts": summary.get("conflicts", []),
                    "caveats": summary.get("caveats", []),
                    "community_id": item["id"],
                    "source_fingerprint": item["fingerprint"],
                    "member_count": item["member_count"],
                    "member_names": [str(member.get("name") or member.get("id")) for member in item["members"][:20]],
                    "relation_count": len(item["relations"]),
                    "source_paths": sorted({str(rel.get("source_path")) for rel in item["relations"] if rel.get("source_path")})[:30],
                    "evidence_assertion_ids": sorted({str(rel.get("assertion_id")) for rel in item["relations"] if rel.get("assertion_id")})[:60],
                    "prompt_version": "community-summary-v1",
                    "model": str(getattr(settings, "llm_model", "")),
                }
                # Re-read source material after the LLM response. If the source chunk,
                # assertions or community membership changed during generation, discard
                # the answer instead of persisting a stale summary.
                fresh_snapshot = self._community_summary_snapshot(item["id"])
                if (
                    not fresh_snapshot
                    or len(fresh_snapshot.get("members") or []) < min_members
                    or not fresh_snapshot.get("relations")
                    or self._community_summary_fingerprint(fresh_snapshot) != item["fingerprint"]
                ):
                    stale_inputs += 1
                    continue
                saved_rows = self.store._run(
                    "MATCH (r:KGAnalysisRun {id: $run_id}) WHERE r.status = 'running' "
                    "MATCH (c:KGCommunity {id: $community_id}) "
                    "WHERE coalesce(c.is_stale, false) = false "
                    "MERGE (i:KGInsight {id: $insight_id}) ON CREATE SET i.created_at = datetime() "
                    "WITH r, c, i "
                    "OPTIONAL MATCH (old_run:KGAnalysisRun)-[old_rel:KG_PRODUCED]->(i) "
                    "WHERE old_run.id <> $run_id "
                    "WITH r, c, i, collect(old_rel) AS old_rels "
                    "FOREACH (rel IN old_rels | DELETE rel) "
                    "SET i.run_id = $run_id, i.kind = 'community_summary', i.title = $title, "
                    "i.description = $description, i.score = toFloat($member_count), i.details_json = $details_json, "
                    "i.updated_at = datetime(), i.is_stale = false, i.stale_reason = null, "
                    "c.summary_fingerprint = $fingerprint, c.summary_updated_at = datetime(), "
                    "c.summary_prompt_version = $prompt_version "
                    "MERGE (r)-[:KG_PRODUCED]->(i) MERGE (i)-[:KG_SUMMARIZES_COMMUNITY]->(c) "
                    "RETURN i.id AS id",
                    {"community_id": item["id"], "insight_id": item["insight_id"], "run_id": run_id,
                     "title": summary["title"], "description": summary["summary"],
                     "member_count": item["member_count"], "details_json": json.dumps(details, ensure_ascii=False),
                     "fingerprint": item["fingerprint"], "prompt_version": "community-summary-v1"},
                )
                if saved_rows:
                    generated += 1
                else:
                    stale_inputs += 1
        return {
            "status": "completed_with_errors" if errors else "completed",
            "eligible": len(snapshots) + unchanged, "generated": generated, "unchanged": unchanged,
            "deferred_due_to_run_limit": max(0, len(snapshots) - len(pending)),
            "errors": errors, "stale_inputs": stale_inputs, "llm_calls": llm_calls, "batch_size": batch_size,
            "max_per_run": max_per_run, "min_members": min_members,
        }

    def _run_chunk_similarity_knn(self, run_id: str, knn_proc: str | None) -> dict[str, Any]:
        """Build a Neo4j-native semantic similarity graph from stored chunk embeddings."""
        if not knn_proc:
            return {"status": "skipped", "reason": "GDS KNN ist nicht verfügbar."}
        graph_name = "winkg_knn_" + uuid.uuid4().hex[:12]
        try:
            # The label lets GDS project only chunks with stored embeddings. Backfill
            # it at analysis time so existing test graphs also work without a rebuild.
            count_rows = self.store._run(
                "MATCH (c:KGChunk) WHERE c.embedding IS NOT NULL "
                "SET c:KGChunkEmbedded RETURN count(c) AS embedded_chunks",
                {},
            )
            embedded_chunks = int(count_rows[0].get("embedded_chunks", 0)) if count_rows else 0
            max_chunks = int(getattr(self.store.settings, "gds_max_knn_chunks", 10000))
            if embedded_chunks < 2:
                return {"status": "skipped", "reason": "Weniger als zwei Chunks besitzen gespeicherte Embeddings.",
                        "embedded_chunks": embedded_chunks}
            if embedded_chunks > max_chunks:
                return {
                    "status": "skipped",
                    "reason": (
                        f"GDS-KNN übersprungen: {embedded_chunks} eingebettete Chunks überschreiten "
                        f"GDS_MAX_KNN_CHUNKS={max_chunks}. Erhöhe das Limit bewusst oder verwende "
                        "später eine inkrementelle/partitionierte Ähnlichkeitsberechnung."
                    ),
                    "embedded_chunks": embedded_chunks, "max_chunks": max_chunks,
                }

            # GDS rejects an empty relationship-projection map even though KNN
            # compares node properties and does not require existing graph edges.
            # The real KG_HAS_CHUNK type gives it a non-empty projection schema;
            # because only KGChunkEmbedded nodes are projected, relationships to
            # KGFile nodes do not become edges between projected chunk nodes.
            projection_rows = self.store._run(
                "CALL gds.graph.project($graph_name, 'KGChunkEmbedded', "
                "{KG_HAS_CHUNK: {orientation: 'UNDIRECTED'}}, "
                "{nodeProperties: ['embedding']}) "
                "YIELD graphName, nodeCount, relationshipCount "
                "RETURN graphName, nodeCount, relationshipCount",
                {"graph_name": graph_name},
            )
            projection = projection_rows[0] if projection_rows else {}
            if int(projection.get("nodeCount", embedded_chunks)) < 2:
                return {"status": "skipped", "reason": "GDS-Projektion enthält weniger als zwei eingebettete Chunks.",
                        "projection": projection}

            # These edges are fully derived from current embeddings. Remove old ones
            # only after the new projection was created successfully; source nodes and
            # text embeddings are untouched.
            self.store._run("MATCH ()-[r:KG_SEMANTIC_SIMILAR_TO]->() DELETE r", {})
            rows = self.store._run(
                f"CALL {knn_proc}($graph_name, {{nodeProperties: {{embedding: 'COSINE'}}, topK: 8, "
                "similarityCutoff: 0.72, sampleRate: 0.5, maxIterations: 50, concurrency: 1, "
                "deltaThreshold: 0.001, randomSeed: 42, "
                "writeRelationshipType: 'KG_SEMANTIC_SIMILAR_TO', writeProperty: 'similarity'}) "
                "YIELD nodesCompared, relationshipsWritten, computeMillis "
                "RETURN nodesCompared, relationshipsWritten, computeMillis",
                {"graph_name": graph_name},
            )
            algorithm_result = rows[0] if rows else {}
            stored_rows = self.store._run(
                "MATCH ()-[r:KG_SEMANTIC_SIMILAR_TO]->() "
                "SET r.analysis_run_id = $run_id, r.is_stale = false, r.generated_at = datetime() "
                "RETURN count(r) AS stored_edges",
                {"run_id": run_id},
            )
            stored_edges = int(stored_rows[0].get("stored_edges", 0)) if stored_rows else 0
            samples = self.store._run(
                "MATCH (a:KGChunk)-[r:KG_SEMANTIC_SIMILAR_TO]->(b:KGChunk) "
                "WHERE r.analysis_run_id = $run_id AND a.id < b.id "
                "RETURN a.path AS source_path, a.filename AS source_filename, "
                "b.path AS target_path, b.filename AS target_filename, "
                "max(r.similarity) AS similarity ORDER BY similarity DESC LIMIT 10",
                {"run_id": run_id},
            )
            result = {
                "status": "completed", "algorithm": "knn", "projection": projection,
                "embedded_chunks": embedded_chunks, "stored_edges": stored_edges,
                **algorithm_result, "sample_pairs": samples,
            }
            if stored_edges:
                self._save_insight(
                    run_id=run_id, kind="semantic_chunk_similarity",
                    title=f"{stored_edges} semantische KNN-Kanten aus Text-Embeddings",
                    description=(
                        "Neo4j GDS KNN hat ähnliche Textabschnitte über ihre bereits vorhandenen "
                        "Embeddings verbunden. Diese Kanten benötigen kein Chat-LLM und können die "
                        "Graphsuche über verschiedene Dateien hinweg erweitern. Ähnlichkeit allein "
                        "belegt keine fachliche Beziehung."
                    ),
                    score=float(stored_edges),
                    details={"algorithm": "knn", "embedded_chunks": embedded_chunks,
                             "stored_edges": stored_edges, "parameters": {
                                 "topK": 8, "similarityCutoff": 0.72, "sampleRate": 0.5,
                                 "maxIterations": 50, "randomSeed": 42,
                             }, "sample_pairs": samples},
                )
            return result
        except Exception as exc:
            log.warning("GDS-KNN auf Chunk-Embeddings fehlgeschlagen: %s", exc)
            return {"status": "failed", "reason": str(exc)[:1200]}
        finally:
            try:
                self.store._run(
                    "CALL gds.graph.drop($graph_name, false) YIELD graphName RETURN graphName",
                    {"graph_name": graph_name},
                )
            except Exception as exc:
                log.debug("GDS-KNN-Projektionsgraph konnte nicht entfernt werden: %s", exc)

    def run_gds_analysis(self, *, run_id: str | None = None) -> dict[str, Any]:
        """Run available GDS analyses and optionally summarize changed communities with an LLM."""
        run_id = run_id or uuid.uuid4().hex
        caps = self.capabilities()
        if not caps.get("gds", {}).get("available"):
            return {"status": "skipped", "reason": "Neo4j Graph Data Science (GDS) ist nicht verfügbar."}
        names = set(caps.get("gds", {}).get("algorithms", []))
        names_by_lower = {name.lower(): name for name in names}
        community_proc = next((names_by_lower[name] for name in ("gds.leiden.write", "gds.louvain.write")
                               if name in names_by_lower), None)
        wcc_proc = names_by_lower.get("gds.wcc.write")
        pagerank_proc = names_by_lower.get("gds.pagerank.write")
        node_similarity_proc = names_by_lower.get("gds.nodesimilarity.stream")
        knn_proc = names_by_lower.get("gds.knn.write")
        chunk_similarity = self._run_chunk_similarity_knn(run_id, knn_proc)
        # Native GDS relationship projections select by relationship type, not by
        # status property. Keep inactive review-history on KGAssertion nodes, but
        # physically remove its redundant, non-factual KG_ASSERTED_LINK edge so the
        # in-memory graph cannot accidentally include rejected/retracted links.
        self.store._run(
            "MATCH ()-[r:KG_ASSERTED_LINK]->() "
            "WHERE coalesce(r.status, 'proposed') <> 'accepted' DELETE r",
            {},
        )
        counts = self.store._run(
            "MATCH (e:KGEntity) WITH count(e) AS nodes "
            "OPTIONAL MATCH (a:KGEntity)-[r:KG_ASSERTED_LINK {status: 'accepted'}]-(b:KGEntity) "
            "RETURN nodes, count(DISTINCT r) AS relationship_matches, "
            "collect(DISTINCT type(r)) AS relationship_types"
        )
        graph_nodes = int(counts[0].get("nodes", 0)) if counts else 0
        relationship_matches = int(counts[0].get("relationship_matches", 0)) if counts else 0
        max_nodes = int(getattr(self.store.settings, "gds_max_entity_nodes", 50000))
        chunk_completed = chunk_similarity.get("status") == "completed"
        if graph_nodes > max_nodes:
            return {"status": "completed" if chunk_completed else "skipped",
                    "reason": f"KGEntity umfasst {graph_nodes} Knoten und überschreitet GDS_MAX_ENTITY_NODES={max_nodes}; die separate Chunk-Analyse kann dennoch gelaufen sein.",
                    "entity_nodes": graph_nodes, "max_entity_nodes": max_nodes,
                    "semantic_chunk_similarity": chunk_similarity}
        if graph_nodes < 2 or relationship_matches < 1:
            return {"status": "completed" if chunk_completed else "skipped",
                    "reason": "Zu wenige KGEntity-Knoten mit aktiven Beziehungen für Community-/Zentralitätsanalysen.",
                    "entity_nodes_with_edges": graph_nodes,
                    "semantic_chunk_similarity": chunk_similarity}
        if not community_proc and not wcc_proc and not pagerank_proc and not node_similarity_proc:
            return {"status": "completed" if chunk_completed else "skipped",
                    "reason": "GDS ist installiert, aber keine unterstützten Entitätsalgorithmen sind verfügbar.",
                    "available_algorithms": sorted(names),
                    "semantic_chunk_similarity": chunk_similarity}

        graph_name = "winkg_analysis_" + uuid.uuid4().hex[:12]
        result: dict[str, Any] = {"status": "completed", "graph_name": graph_name,
                                  "projection": {}, "semantic_chunk_similarity": chunk_similarity,
                                  "community_detection": {"status": "not available"},
                                  "weakly_connected_components": {"status": "not available"},
                                  "pagerank": {"status": "not available"},
                                  "node_similarity": {"status": "not available", "candidates_created": 0}}
        try:
            relationship_types = set(counts[0].get("relationship_types") or []) if counts else set()
            # Some installations/graphs have only one of these relationship types.
            # Project only types actually present to avoid failures on empty projections.
            # The source-extracted KG_RELATED_TO edges are provisional; only the
            # reviewer-approved KG_ASSERTED_LINK type belongs in the factual graph.
            relationship_projection = (
                {"KG_ASSERTED_LINK": {"orientation": "UNDIRECTED"}}
                if "KG_ASSERTED_LINK" in relationship_types else {}
            )
            rows = self.store._run(
                "CALL gds.graph.project($graph_name, 'KGEntity', $relationship_projection) "
                "YIELD graphName, nodeCount, relationshipCount "
                "RETURN graphName, nodeCount, relationshipCount",
                {"graph_name": graph_name, "relationship_projection": relationship_projection},
            )
            if rows:
                result["projection"] = rows[0]

            if wcc_proc:
                try:
                    rows = self.store._run(
                        f"CALL {wcc_proc}($graph_name, {{writeProperty: 'kg_gds_component_id'}}) "
                        "YIELD componentCount, nodePropertiesWritten "
                        "RETURN componentCount, nodePropertiesWritten",
                        {"graph_name": graph_name},
                    )
                    algo_result = rows[0] if rows else {}
                    self.store._run(
                        "MATCH (e:KGEntity) WHERE e.kg_gds_component_id IS NOT NULL "
                        "SET e.kg_gds_component_run_id = $run_id, e.kg_gds_component_stale = false, "
                        "e.kg_gds_component_updated_at = datetime()",
                        {"run_id": run_id},
                    )
                    self.store._run("MATCH ()-[r:KG_IN_COMPONENT]->() DELETE r", {})
                    self.store._run("MATCH (c:KGComponent) DETACH DELETE c", {})
                    components = self.store._run(
                        "MATCH (e:KGEntity) WHERE e.kg_gds_component_run_id = $run_id "
                        "WITH e.kg_gds_component_id AS component_id, collect(e) AS members "
                        "WITH component_id, members, $run_id + ':component:' + toString(component_id) AS component_key "
                        "MERGE (c:KGComponent {id: component_key}) "
                        "SET c.component_id = component_id, c.run_id = $run_id, c.algorithm = 'wcc', "
                        "c.is_stale = false, c.member_count = size(members), "
                        "c.sample_members = [e IN members[0..10] | coalesce(e.name, e.id)], "
                        "c.updated_at = datetime() WITH c, members UNWIND members AS e "
                        "MERGE (e)-[:KG_IN_COMPONENT]->(c) "
                        "RETURN count(DISTINCT c) AS component_nodes, "
                        "max(c.member_count) AS largest_component_size "
                        "ORDER BY largest_component_size DESC",
                        {"run_id": run_id},
                    )
                    component_summary = components[0] if components else {}
                    result["weakly_connected_components"] = {
                        "status": "completed", "algorithm": "wcc", **algo_result,
                        **component_summary,
                    }
                    component_count = int(algo_result.get("componentCount", 0) or 0)
                    if component_count > 1:
                        self._save_insight(
                            run_id=run_id, kind="disconnected_components",
                            title=f"{component_count} getrennte Wissenskomponenten gefunden",
                            description=(
                                "Neo4j GDS Weakly Connected Components hat voneinander getrennte "
                                "Komponenten im Entitäts-Beziehungsgraphen erkannt. Das kann echte "
                                "Fachbereiche, fehlende Beziehungen oder unvollständige Extraktion "
                                "anzeigen; es ist kein automatischer Fehlerbefund."
                            ),
                            score=float(component_count),
                            details={"algorithm": "wcc", **algo_result, **component_summary},
                        )
                except Exception as exc:
                    log.warning("GDS Weakly Connected Components fehlgeschlagen: %s", exc)
                    result["weakly_connected_components"] = {"status": "failed", "error": str(exc)[:1000]}

            if community_proc:
                try:
                    algorithm = "leiden" if ".leiden." in community_proc else "louvain"
                    rows = self.store._run(
                        f"CALL {community_proc}($graph_name, {{writeProperty: 'kg_gds_community_id'}}) "
                        "YIELD communityCount, nodePropertiesWritten "
                        "RETURN communityCount, nodePropertiesWritten",
                        {"graph_name": graph_name},
                    )
                    algo_result = rows[0] if rows else {}
                    current_run_rows = self.store._run(
                        "MATCH (r:KGAnalysisRun {id: $run_id}) RETURN r.status AS status",
                        {"run_id": run_id},
                    )
                    current_status = str(current_run_rows[0].get("status") or "") if current_run_rows else ""
                    if current_status and current_status != "running":
                        result["community_detection"] = {
                            "status": "stale",
                            "reason": "Analyse-Lauf wurde während GDS als veraltet markiert.",
                            "algorithm": algorithm,
                            **algo_result,
                        }
                        result["community_summaries"] = {
                            "status": "skipped", "reason": "Analyse-Lauf ist veraltet"
                        }
                    else:
                        self.store._run(
                            "MATCH (e:KGEntity) WHERE e.kg_gds_community_id IS NOT NULL "
                            "SET e.kg_gds_community_run_id = $run_id, "
                            "e.kg_gds_community_algorithm = $algorithm, e.kg_gds_community_stale = false, "
                            "e.kg_gds_community_updated_at = datetime()",
                            {"run_id": run_id, "algorithm": algorithm},
                        )
                        # Read the result before changing old membership edges. A failed
                        # aggregation must not erase the last usable community snapshot.
                        grouped_rows = self.store._run(
                            "MATCH (e:KGEntity) WHERE e.kg_gds_community_run_id = $run_id "
                            "WITH e.kg_gds_community_id AS community_number, collect(DISTINCT e) AS members "
                            "RETURN community_number, [e IN members | {id: e.id, name: e.name, type: e.type, "
                            "description: e.description, root_id: e.root_id}] AS members "
                            "ORDER BY size(members) DESC, toString(community_number)",
                            {"run_id": run_id},
                        )
                        communities_to_store: list[dict[str, Any]] = []
                        for item in grouped_rows:
                            members = [member for member in (item.get("members") or []) if member and member.get("id")]
                            member_ids = sorted({str(member["id"]) for member in members})
                            if not member_ids:
                                continue
                            community_key = "community:" + stable_id("kg-community-v1", *member_ids)
                            roots = sorted({str(member.get("root_id")) for member in members if member.get("root_id")})
                            communities_to_store.append({
                                "id": community_key,
                                "community_number": item.get("community_number"),
                                "member_ids": member_ids,
                                "member_count": len(member_ids),
                                "sample_members": [
                                    str(member.get("name") or member.get("id"))[:160]
                                    for member in members[:10]
                                ],
                                "root_id": roots[0] if len(roots) == 1 else None,
                            })

                        # Keep a stable key based on member entity IDs. Old communities
                        # and summaries are marked stale until current membership is stored.
                        self.store._run(
                            "MATCH (c:KGCommunity) SET c.is_stale = true, "
                            "c.stale_reason = 'awaiting current GDS membership'",
                            {},
                        )
                        self.store._run(
                            "MATCH (i:KGInsight {kind: 'community_summary'}) "
                            "SET i.is_stale = true, i.stale_reason = 'awaiting current GDS membership'",
                            {},
                        )
                        self.store._run("MATCH ()-[r:KG_IN_COMMUNITY]->() DELETE r", {})
                        if communities_to_store:
                            self.store._run(
                                "UNWIND $communities AS item "
                                "MERGE (c:KGCommunity {id: item.id}) "
                                "SET c.community_id = item.community_number, c.run_id = $run_id, "
                                "c.algorithm = $algorithm, c.is_stale = false, c.stale_reason = null, "
                                "c.member_count = item.member_count, c.sample_members = item.sample_members, "
                                "c.root_id = item.root_id, c.updated_at = datetime() "
                                "WITH c, item UNWIND item.member_ids AS entity_id "
                                "MATCH (e:KGEntity {id: entity_id}) MERGE (e)-[:KG_IN_COMMUNITY]->(c) "
                                "RETURN count(DISTINCT c) AS communities_stored",
                                {"communities": communities_to_store, "run_id": run_id, "algorithm": algorithm},
                            )
                        result["community_detection"] = {
                            "status": "completed", "algorithm": algorithm, **algo_result,
                            "stable_communities_stored": len(communities_to_store),
                        }
                        self._save_insight(
                            run_id=run_id,
                            kind="communities",
                            title="Graph-Communities neu berechnet",
                            description=(
                                "Neo4j GDS hat Gruppen im Entitäts-Beziehungsgraphen berechnet. "
                                "Community-IDs basieren auf stabilen Mitglieds-IDs; LLM-Zusammenfassungen "
                                "werden nur bei geänderter Grundlage neu erzeugt."
                            ),
                            score=float(algo_result.get("communityCount", 0)),
                            details={
                                "algorithm": algorithm, **algo_result,
                                "stable_communities_stored": len(communities_to_store),
                            },
                        )
                        result["community_summaries"] = self._generate_community_summaries(run_id=run_id)
                except Exception as exc:
                    log.warning("GDS Community Detection/Zusammenfassung fehlgeschlagen: %s", exc)
                    result["community_detection"] = {"status": "failed", "error": str(exc)[:1000]}
                    result["community_summaries"] = {"status": "failed", "reason": str(exc)[:1000]}

            if pagerank_proc:
                try:
                    rows = self.store._run(
                        f"CALL {pagerank_proc}($graph_name, {{writeProperty: 'kg_gds_pagerank', maxIterations: 20}}) "
                        "YIELD nodePropertiesWritten RETURN nodePropertiesWritten",
                        {"graph_name": graph_name},
                    )
                    self.store._run(
                        "MATCH (e:KGEntity) WHERE e.kg_gds_pagerank IS NOT NULL "
                        "SET e.kg_gds_pagerank_run_id = $run_id, e.kg_gds_pagerank_stale = false, "
                        "e.kg_gds_pagerank_updated_at = datetime()",
                        {"run_id": run_id},
                    )
                    top = self.store._run(
                        "MATCH (e:KGEntity) WHERE e.kg_gds_pagerank_run_id = $run_id "
                        "RETURN e.id AS id, e.name AS name, e.type AS type, e.kg_gds_pagerank AS score "
                        "ORDER BY score DESC LIMIT 20",
                        {"run_id": run_id},
                    )
                    algo_result = rows[0] if rows else {}
                    result["pagerank"] = {"status": "completed", **algo_result, "top_entities": top}
                    self._save_insight(
                        run_id=run_id, kind="centrality",
                        title="Zentrale Entitäten über PageRank ermittelt",
                        description="PageRank bewertet strukturelle Zentralität im vorhandenen KGEntity-Beziehungsgraphen. Ein hoher Wert ist kein Beleg für fachliche Wichtigkeit, sondern ein Hinweis auf graphstrukturelle Zentralität.",
                        score=float(len(top)),
                        details={"algorithm": "pageRank", "top_entities": top},
                    )
                except Exception as exc:
                    log.warning("GDS PageRank fehlgeschlagen: %s", exc)
                    result["pagerank"] = {"status": "failed", "error": str(exc)[:1000]}
            if node_similarity_proc:
                max_similarity_nodes = int(
                    getattr(self.store.settings, "gds_max_node_similarity_nodes", 15000)
                )
                if graph_nodes > max_similarity_nodes:
                    result["node_similarity"] = {
                        "status": "skipped",
                        "reason": (
                            f"Node Similarity übersprungen: {graph_nodes} Entitäten überschreiten "
                            f"GDS_MAX_NODE_SIMILARITY_NODES={max_similarity_nodes}. "
                            "Community Detection und PageRank können trotzdem ausgeführt werden."
                        ),
                        "entity_nodes": graph_nodes,
                        "max_entity_nodes": max_similarity_nodes,
                    }
                else:
                    try:
                        pairs = self.store._run(
                            f"CALL {node_similarity_proc}($graph_name, "
                            "{topK: 10, similarityCutoff: 0.5, degreeCutoff: 2}) "
                            "YIELD node1, node2, similarity "
                            "WITH gds.util.asNode(node1) AS a, gds.util.asNode(node2) AS b, similarity "
                            "WHERE a.id < b.id AND NOT EXISTS { "
                            "MATCH (a)-[confirmed:KG_ASSERTED_LINK {status: 'accepted'}]-(b) } "
                            "RETURN a.id AS subject_id, a.name AS subject_name, a.type AS subject_type, "
                            "b.id AS object_id, b.name AS object_name, b.type AS object_type, "
                            "similarity ORDER BY similarity DESC LIMIT $limit",
                            {"graph_name": graph_name, "limit": 100},
                        )
                        saved = 0
                        for pair in pairs:
                            candidate = {
                                "subject_id": pair.get("subject_id"),
                                "subject_name": pair.get("subject_name", ""),
                                "subject_type": pair.get("subject_type", ""),
                                "object_id": pair.get("object_id"),
                                "object_name": pair.get("object_name", ""),
                                "object_type": pair.get("object_type", ""),
                                "predicate": "STRUCTURALLY_SIMILAR_TO",
                                "evidence_count": 0,
                                "evidence_chunk_ids": [],
                                "score": float(pair.get("similarity", 0.0)),
                                "reason": (
                                    "Neo4j GDS Node Similarity hat ähnliche Nachbarschaften im "
                                    "bestehenden KGEntity-Beziehungsgraphen gefunden. Das ist ein "
                                    "struktureller Hinweis, keine automatisch bestätigte Fachbeziehung."
                                ),
                            }
                            if candidate["subject_id"] and candidate["object_id"]:
                                self._persist_link_candidate(candidate, run_id)
                                saved += 1
                        result["node_similarity"] = {
                            "status": "completed", "pairs_examined": len(pairs),
                            "candidates_created_or_updated": saved, "algorithm": "nodeSimilarity",
                        }
                        if saved:
                            self._save_insight(
                                run_id=run_id, kind="structural_similarity",
                                title=f"{saved} strukturelle Ähnlichkeitskandidaten",
                                description=(
                                    "GDS Node Similarity hat Entitäten mit ähnlichen Graphnachbarschaften "
                                    "gefunden. Die Ergebnisse sind prüfbare Kandidaten und keine Fakten."
                                ),
                                score=float(saved),
                                details={"algorithm": "nodeSimilarity", "pairs_examined": len(pairs),
                                         "sample": [{"subject": p.get("subject_name"),
                                                     "object": p.get("object_name"),
                                                     "similarity": p.get("similarity")} for p in pairs[:10]]},
                            )
                    except Exception as exc:
                        log.warning("GDS Node Similarity fehlgeschlagen: %s", exc)
                        result["node_similarity"] = {"status": "failed", "error": str(exc)[:1000]}

            failed_statuses = [
                result["community_detection"].get("status"),
                result["weakly_connected_components"].get("status"),
                result["pagerank"].get("status"),
                result["node_similarity"].get("status"),
            ]
            if failed_statuses and all(status in {"failed", "not available", "skipped"} for status in failed_statuses):
                result["status"] = "failed"
            return result
        except Exception as exc:
            log.warning("GDS-Projektion oder Analyse fehlgeschlagen: %s", exc)
            return {"status": "failed", "graph_name": graph_name, "error": str(exc)[:1500]}
        finally:
            try:
                self.store._run(
                    "CALL gds.graph.drop($graph_name, false) YIELD graphName RETURN graphName",
                    {"graph_name": graph_name},
                )
            except Exception as exc:
                log.debug("GDS-Projektionsgraph konnte nicht entfernt werden: %s", exc)

    def list_insights(self, *, run_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        rows = self.store._run(
            "MATCH (i:KGInsight) WHERE ($run_id IS NULL OR i.run_id = $run_id) "
            "RETURN i.id AS id, i.run_id AS run_id, i.kind AS kind, i.title AS title, "
            "i.description AS description, i.score AS score, i.details_json AS details_json, "
            "coalesce(i.is_stale, false) AS is_stale, i.stale_reason AS stale_reason, "
            "toString(i.updated_at) AS updated_at "
            "ORDER BY i.updated_at DESC LIMIT $limit",
            {"run_id": run_id, "limit": limit},
        )
        for row in rows:
            try:
                row["details"] = json.loads(row.pop("details_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                row["details"] = {}
                row.pop("details_json", None)
        return rows

    def list_link_candidates(
        self, *, status: str | None = None, limit: int = 100, include_stale: bool = False
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        allowed_statuses = {"proposed", "accepted", "rejected", "deferred"}
        if status is not None and status not in allowed_statuses:
            raise ValueError(f"Ungültiger Status. Erlaubt: {', '.join(sorted(allowed_statuses))}")
        rows = self.store._run(
            "MATCH (c:KGLinkCandidate) "
            "WHERE ($status IS NULL OR c.status = $status) "
            "AND ($include_stale OR c.status <> 'proposed' OR coalesce(c.is_stale, false) = false) "
            "CALL { WITH c "
            "OPTIONAL MATCH (reviewed:KGLinkCandidate) "
            "WHERE coalesce(reviewed.method, 'unknown') = coalesce(c.method, 'unknown') "
            "AND reviewed.status IN ['accepted', 'rejected'] "
            "WITH collect(reviewed) AS reviews "
            "RETURN size([r IN reviews WHERE r.status = 'accepted']) AS method_accepted, "
            "size(reviews) AS method_reviewed } "
            "WITH c, method_accepted, method_reviewed, "
            "CASE WHEN method_reviewed >= 5 "
            "THEN toFloat(method_accepted + 1) / toFloat(method_reviewed + 2) ELSE 0.5 END AS method_priority, "
            "CASE WHEN method_reviewed >= 5 "
            "THEN toFloat(method_accepted) / toFloat(method_reviewed) ELSE null END AS method_acceptance_rate "
            "RETURN c.id AS id, c.predicate AS predicate, c.status AS status, "
            "c.subject_id AS subject_id, c.subject_name AS subject_name, "
            "c.object_id AS object_id, c.object_name AS object_name, c.method AS method, "
            "c.evidence_count AS evidence_count, c.evidence_chunk_ids AS evidence_chunk_ids, "
            "c.reason AS reason, coalesce(c.is_stale, false) AS is_stale, "
            "c.score AS score, toString(c.last_seen_at) AS last_seen_at, "
            "method_accepted, method_reviewed, method_acceptance_rate, method_priority, "
            "(method_reviewed >= 5) AS method_calibrated "
            "ORDER BY method_priority DESC, c.evidence_count DESC, c.score DESC, c.last_seen_at DESC LIMIT $limit",
            {"status": status, "limit": limit, "include_stale": include_stale},
        )
        return rows

    def list_assertions(
        self, *, status: str | None = None, predicate: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """List source-grounded, predicate-open statements stored in Neo4j."""
        limit = max(1, min(int(limit), 1000))
        rows = self.store._run(
            "MATCH (s:KGNode)-[:KG_ASSERTS_SUBJECT]->(a:KGAssertion)-[:KG_ASSERTS_OBJECT]->(o:KGNode) "
            "WHERE ($status IS NULL OR a.status = $status) "
            "AND ($predicate IS NULL OR a.predicate = $predicate) "
            "OPTIONAL MATCH (a)-[:KG_ASSERTED_FROM]->(chunk:KGChunk) "
            "RETURN a.id AS id, a.predicate AS predicate, a.status AS status, a.method AS method, "
            "s.name AS subject, o.name AS object, a.evidence AS evidence, "
            "a.evidence_count AS evidence_count, a.evidence_chunk_ids AS evidence_chunk_ids, "
            "collect(DISTINCT chunk.path)[0..8] AS source_paths, "
            "coalesce(a.is_stale, false) AS is_stale, a.analysis_run_id AS analysis_run_id, "
            "toString(a.created_at) AS created_at, toString(a.updated_at) AS updated_at "
            "ORDER BY a.updated_at DESC LIMIT $limit",
            {"status": status, "predicate": predicate, "limit": limit},
        )
        for row in rows:
            row["source_paths"] = [path for path in (row.get("source_paths") or []) if path]
            row["evidence_chunk_ids"] = [item for item in (row.get("evidence_chunk_ids") or []) if item]
        return rows

    def review_link_candidate(self, candidate_id: str, decision: str, note: str = "") -> dict[str, Any]:
        """Persist reviewer feedback; accepted candidates become generic assertions.

        Stale candidates cannot be accepted because their evidence may have changed.
        The decision and accepted assertion are written together for the acceptance
        path, avoiding a candidate being marked accepted if the assertion cannot be
        created.
        """
        if decision not in {"accepted", "rejected", "deferred"}:
            raise ValueError("decision muss accepted, rejected oder deferred sein")
        rows = self.store._run(
            "MATCH (c:KGLinkCandidate {id: $id}) "
            "OPTIONAL MATCH (s_type:KGEntity {id: c.subject_id}) "
            "OPTIONAL MATCH (o_type:KGEntity {id: c.object_id}) "
            "RETURN c.id AS id, c.status AS status, c.subject_id AS subject_id, "
            "c.subject_name AS subject_name, c.object_id AS object_id, c.object_name AS object_name, "
            "s_type.type AS subject_type, o_type.type AS object_type, "
            "c.predicate AS predicate, c.method AS candidate_method, "
            "c.analysis_run_id AS analysis_run_id, c.reason AS reason, c.evidence AS evidence, "
            "c.evidence_count AS evidence_count, c.evidence_chunk_ids AS evidence_chunk_ids, "
            "c.review_note AS review_note, coalesce(c.is_stale, false) AS is_stale",
            {"id": candidate_id},
        )
        if not rows:
            raise ValueError(f"Beziehungskandidat nicht gefunden: {candidate_id}")
        row = rows[0]
        if row.get("is_stale") and decision == "accepted":
            raise ValueError(
                "Dieser Vorschlag ist veraltet, weil sich der Quellgraph geändert hat. "
                "Bitte zuerst eine neue Graphanalyse ausführen."
            )

        assertion_id = stable_id("reviewed-assertion", candidate_id)
        note = note[:2000]
        if decision == "accepted":
            accepted_rows = self.store._run(
                "MATCH (c:KGLinkCandidate {id: $candidate_id}) "
                "MATCH (s:KGEntity {id: $subject_id}) "
                "MATCH (o:KGEntity {id: $object_id}) "
                "WHERE coalesce(c.is_stale, false) = false "
                "MERGE (a:KGAssertion {id: $assertion_id}) "
                "ON CREATE SET a.created_at = datetime() "
                "SET s.canonical_entity_id = $canonical_subject_id, o.canonical_entity_id = $canonical_object_id, "
                "c.status = 'accepted', c.review_method = 'human_review', c.review_note = $note, c.reviewed_at = datetime(), "
                "a.subject_id = $subject_id, a.predicate = $predicate, a.object_id = $object_id, "
                "a.status = 'accepted', a.method = 'human_review', a.polarity = 'positive', a.confidence = 1.0, "
                "a.root_id = coalesce(s.root_id, o.root_id), a.evidence = $evidence, "
                "a.evidence_count = $evidence_count, a.evidence_chunk_ids = $evidence_chunk_ids, "
                "a.source_candidate_id = $candidate_id, a.candidate_method = $candidate_method, "
                "a.source_analysis_run_id = $analysis_run_id, a.is_stale = false, "
                "a.review_note = $note, a.updated_at = datetime() "
                "MERGE (s)-[:KG_ASSERTS_SUBJECT]->(a) MERGE (a)-[:KG_ASSERTS_OBJECT]->(o) "
                "WITH c, a, s, o "
                "MERGE (f:KGConsolidatedFact {id: $fact_id}) "
                "SET f.canonical_subject_id = $canonical_subject_id, f.predicate = $predicate, "
                "f.canonical_object_id = $canonical_object_id, f.updated_at = datetime() "
                "MERGE (a)-[:KG_SUPPORTS_FACT]->(f) MERGE (f)-[:KG_FACT_SUBJECT]->(s) MERGE (f)-[:KG_FACT_OBJECT]->(o) "
                "MERGE (s)-[link:KG_ASSERTED_LINK {assertion_id: $assertion_id}]->(o) "
                "SET link.predicate = $predicate, link.status = 'accepted', "
                "link.method = 'human_review', link.evidence = $evidence, "
                "link.score = coalesce(c.score, 1.0), link.root_id = coalesce(s.root_id, o.root_id), "
                "link.fact_id = $fact_id, link.updated_at = datetime() "
                "MERGE (c)-[:KG_REVIEWED_AS]->(a) "
                "RETURN a.id AS assertion_id, a.status AS assertion_status, c.status AS status",
                {"candidate_id": candidate_id, "assertion_id": assertion_id,
                 "subject_id": row["subject_id"], "object_id": row["object_id"],
                 "candidate_method": row.get("candidate_method") or "unknown",
                 "analysis_run_id": row.get("analysis_run_id"),
                 "predicate": row.get("predicate", "CO_MENTIONED_WITH"),
                 "evidence": row.get("evidence") or row.get("reason", "Reviewed graph candidate"),
                 "evidence_count": int(row.get("evidence_count") or 0),
                 "evidence_chunk_ids": row.get("evidence_chunk_ids") or [], "note": note,
                 "canonical_subject_id": stable_id("canonical-entity", str(row.get("subject_type") or "Sonstiges").casefold(), normalize_text(str(row.get("subject_name") or ""))),
                 "canonical_object_id": stable_id("canonical-entity", str(row.get("object_type") or "Sonstiges").casefold(), normalize_text(str(row.get("object_name") or ""))),
                 "fact_id": stable_id("consolidated-fact", stable_id("canonical-entity", str(row.get("subject_type") or "Sonstiges").casefold(), normalize_text(str(row.get("subject_name") or ""))), str(row.get("predicate", "CO_MENTIONED_WITH")).casefold(), stable_id("canonical-entity", str(row.get("object_type") or "Sonstiges").casefold(), normalize_text(str(row.get("object_name") or ""))))},
            )
            if not accepted_rows:
                raise ValueError(
                    "Kandidat konnte nicht akzeptiert werden: Quellknoten fehlen oder der Vorschlag wurde inzwischen veraltet."
                )
            self.store._run(
                "MATCH (a:KGAssertion {id: $assertion_id}) "
                "UNWIND $evidence_chunk_ids AS chunk_id "
                "MATCH (chunk:KGChunk {id: chunk_id}) "
                "MERGE (a)-[:KG_ASSERTED_FROM]->(chunk)",
                {"assertion_id": assertion_id, "evidence_chunk_ids": row.get("evidence_chunk_ids") or []},
            )
            row["status"] = "accepted"
            row["assertion_id"] = assertion_id
            row["assertion_status"] = "accepted"
            try:
                self.store._refresh_consolidated_fact(stable_id(
                    "consolidated-fact",
                    stable_id("canonical-entity", str(row.get("subject_type") or "Sonstiges").casefold(), normalize_text(str(row.get("subject_name") or ""))),
                    str(row.get("predicate", "CO_MENTIONED_WITH")).casefold(),
                    stable_id("canonical-entity", str(row.get("object_type") or "Sonstiges").casefold(), normalize_text(str(row.get("object_name") or ""))),
                ))
            except Exception as exc:
                log.debug("Manuell bestätigte Aussage konnte nicht konsolidiert werden: %s", exc)
        else:
            self.store._run(
                "MATCH (c:KGLinkCandidate {id: $candidate_id}) "
                "SET c.status = $decision, c.review_note = $note, c.reviewed_at = datetime() "
                "RETURN c.id AS id",
                {"candidate_id": candidate_id, "decision": decision, "note": note},
            )
            # If a previously accepted review is withdrawn, retain its history.
            self.store._run(
                "MATCH (a:KGAssertion {id: $assertion_id}) "
                "SET a.status = 'retracted', a.review_note = $note, a.updated_at = datetime() "
                "WITH a OPTIONAL MATCH ()-[link:KG_ASSERTED_LINK {assertion_id: $assertion_id}]->() "
                "DELETE link RETURN a.id AS assertion_id",
                {"assertion_id": assertion_id, "note": note},
            )
            row["status"] = decision
            row["review_note"] = note
        return row

