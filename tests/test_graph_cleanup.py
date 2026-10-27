from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from winkg.neo4j_store import Neo4jStore


class FakeStore:
    def __init__(self, old_runs: list[dict[str, Any]] | None = None):
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.old_runs = old_runs or []

    def _run(self, query: str, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        params = parameters or {}
        self.calls.append((query, params))
        if "WITH ordered_runs[$keep_runs..] AS obsolete_runs" in query:
            return self.old_runs
        if "WITH s, r DETACH DELETE r RETURN count(r) AS deleted" in query:
            return [{"deleted": len(params.get("run_ids", []))}]
        if "RETURN coalesce(s.status_counts_json" in query:
            return [{"status_counts_json": '{"stale": 2}', "type_counts_json": '{"graph_intelligence": 3}'}]
        if "RETURN coalesce(s.archived_runs_total" in query:
            return [{"archived_runs_total": 1, "archived_insights_total": 2}]
        if "RETURN count(e) AS cleaned" in query:
            return [{"cleaned": 0}]
        if "RETURN count(" in query and " AS deleted" in query:
            return [{"deleted": 0}]
        return []


def test_cleanup_targets_only_stale_derived_data_and_reviewable_proposals():
    store = Neo4jStore.__new__(Neo4jStore)
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_run(query: str, parameters: dict[str, Any] | None = None):
        calls.append((query, parameters or {}))
        if "RETURN count(e) AS cleaned" in query:
            return [{"cleaned": 0}]
        if "RETURN count(" in query and " AS deleted" in query:
            return [{"deleted": 0}]
        if "RETURN coalesce(s.archived_runs_total" in query:
            return [{"archived_runs_total": 0, "archived_insights_total": 0}]
        return []

    store._run = fake_run  # type: ignore[method-assign]
    result = store.cleanup_graph_maintenance()
    all_queries = "\n".join(query for query, _ in calls)

    assert result["analysis_runs_archived"] == 0
    assert result["abandoned_analysis_runs_marked_failed"] == 0
    assert "duration('P1D')" in all_queries
    assert "coalesce(c.is_stale, false) = true" in all_queries
    assert "c.status = 'proposed'" in all_queries
    assert "KGCommunity" in all_queries and "KGComponent" in all_queries
    assert "kg_gds_pagerank_stale" in all_queries
    assert "a.embedding IS NULL OR b.embedding IS NULL" in all_queries
    assert "KGAnalysisHistorySummary" in all_queries
    assert not any("MATCH (a:KGAssertion) DETACH DELETE a" in query for query, _ in calls)
    assert not any("MATCH (chunk:KGChunk) DETACH DELETE" in query for query, _ in calls)


def test_orphan_quality_decisions_cleanup_only_targets_disconnected_decisions():
    store = Neo4jStore.__new__(Neo4jStore)
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_run(query: str, parameters: dict[str, Any] | None = None):
        calls.append((query, parameters or {}))
        return [{"deleted": 0}]

    store._run = fake_run  # type: ignore[method-assign]
    store._cleanup_graph_intelligence_orphans()

    decision_query = next(query for query, _ in calls if "MATCH (d:KGQualityDecision)" in query)
    assert "NOT EXISTS { MATCH (:KGLinkCandidate)-[:KG_HAS_QUALITY_DECISION]->(d) }" in decision_query
    assert "WITH d DETACH DELETE d RETURN count(d) AS deleted" in decision_query


def test_extraction_cache_cleanup_is_disabled_when_retention_is_zero():
    store = Neo4jStore.__new__(Neo4jStore)
    store.settings = SimpleNamespace(extraction_cache_retention_days=0)
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_run(query: str, parameters: dict[str, Any] | None = None):
        calls.append((query, parameters or {}))
        if "RETURN coalesce(s.archived_runs_total" in query:
            return [{"archived_runs_total": 0, "archived_insights_total": 0}]
        return [{"deleted": 0}]

    store._run = fake_run  # type: ignore[method-assign]
    result = store.cleanup_graph_maintenance()

    assert result["old_extraction_cache_deleted"] == 0
    assert not any("KGExtractionCache" in query for query, _ in calls)


def test_extraction_cache_cleanup_passes_configured_retention_days():
    store = Neo4jStore.__new__(Neo4jStore)
    store.settings = SimpleNamespace(extraction_cache_retention_days=30)
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_run(query: str, parameters: dict[str, Any] | None = None):
        calls.append((query, parameters or {}))
        if "KGExtractionCache" in query:
            return [{"deleted": 2}]
        if "RETURN coalesce(s.archived_runs_total" in query:
            return [{"archived_runs_total": 0, "archived_insights_total": 0}]
        return [{"deleted": 0}]

    store._run = fake_run  # type: ignore[method-assign]
    result = store.cleanup_graph_maintenance()

    cache_call = next((query, params) for query, params in calls if "KGExtractionCache" in query)
    assert cache_call[1] == {"days": 30}
    assert "coalesce(c.last_used_at, c.created_at)" in cache_call[0]
    assert "duration({days: $days})" in cache_call[0]
    assert result["old_extraction_cache_deleted"] == 2


def test_cleanup_archives_expired_analysis_runs_into_one_summary_node():
    old_runs = [{
        "id": "old-run-1",
        "root_id": "root-a",
        "analysis_type": "graph_intelligence",
        "status": "stale",
        "summary_json": json.dumps({"graph_stats": {"files": 100}, "gds": {"status": "completed"}}),
        "insight_count": 4,
        "candidate_count": 7,
        "finished_at": "2026-10-01T10:00:00Z",
        "started_at": "2026-10-01T09:00:00Z",
    }]
    store = Neo4jStore.__new__(Neo4jStore)
    fake = FakeStore(old_runs)
    store._run = fake._run  # type: ignore[method-assign]

    result = store.cleanup_graph_maintenance(keep_analysis_runs=20)

    archive_call = next(
        (query, params) for query, params in fake.calls
        if "MERGE (s:KGAnalysisHistorySummary {id: 'global'})" in query
    )
    delete_call = archive_call
    assert result["analysis_runs_archived"] == 1
    assert result["analysis_insights_archived"] == 4
    assert result["analysis_candidates_archived_count"] == 7
    assert archive_call[1]["status_counts_json"] == json.dumps({"stale": 3})
    assert archive_call[1]["run_ids"] == ["old-run-1"]
    assert "actual_runs" in delete_call[0]
    assert "actual_insights" in delete_call[0]
    assert "DETACH DELETE r" in delete_call[0]


def test_analysis_retention_is_clamped_to_safe_bounds():
    store = Neo4jStore.__new__(Neo4jStore)
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_run(query: str, parameters: dict[str, Any] | None = None):
        calls.append((query, parameters or {}))
        if "RETURN coalesce(s.archived_runs_total" in query:
            return [{"archived_runs_total": 0, "archived_insights_total": 0}]
        if "RETURN count(e) AS cleaned" in query:
            return [{"cleaned": 0}]
        if "RETURN count(" in query and " AS deleted" in query:
            return [{"deleted": 0}]
        return []

    store._run = fake_run  # type: ignore[method-assign]
    store.cleanup_graph_maintenance(keep_analysis_runs=10000)
    select_call = next((q, p) for q, p in calls if "WITH ordered_runs[$keep_runs..]" in q)
    assert select_call[1]["keep_runs"] == 100


def test_indexer_cleanup_does_not_delete_source_backed_facts():
    # Safety contract: cleanup queries target derived analysis labels and stale
    # proposals, not source documents or accepted assertions.
    store = Neo4jStore.__new__(Neo4jStore)
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_run(query: str, parameters: dict[str, Any] | None = None):
        calls.append((query, parameters or {}))
        if "RETURN coalesce(s.archived_runs_total" in query:
            return [{"archived_runs_total": 0, "archived_insights_total": 0}]
        if "RETURN count(e) AS cleaned" in query:
            return [{"cleaned": 0}]
        if "RETURN count(" in query and " AS deleted" in query:
            return [{"deleted": 0}]
        return []

    store._run = fake_run  # type: ignore[method-assign]
    store.cleanup_graph_maintenance()
    import re
    destructive = [query for query, _ in calls if "DETACH DELETE" in query]
    assert destructive
    deleted_labels = []
    for query in destructive:
        match = re.search(r"MATCH \(\w+:([A-Za-z0-9_]+)\)", query)
        if match:
            deleted_labels.append(match.group(1))
    assert not ({"KGFile", "KGFolder", "KGChunk", "KGRoot"} & set(deleted_labels))
    # LLM extraction assertions can be removed only when they have no source chunk;
    # accepted/rejected review history is not bulk-deleted.
    assert not any("MATCH (a:KGAssertion) WHERE a.method = 'human_review'" in query for query in destructive)
