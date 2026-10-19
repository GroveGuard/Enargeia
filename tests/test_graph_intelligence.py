from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from winkg.graph_intelligence import GraphIntelligence


class FakeStore:
    def __init__(self, responses: dict[str, list[dict[str, Any]]] | None = None):
        self.responses = responses or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.settings = SimpleNamespace(gds_max_entity_nodes=50000)

    def _run(self, query: str, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        self.calls.append((query, parameters or {}))
        for key, value in self.responses.items():
            if key in query:
                return value
        return []

    def ensure_schema(self) -> None:
        self.calls.append(("ensure_schema", {}))


def test_capabilities_handles_missing_gds_and_reports_neo4j():
    store = FakeStore({
        "CALL dbms.components()": [{"name": "Neo4j Kernel", "version": "5.26.0", "edition": "community"}],
        "RETURN gds.version() AS version": [],
        "SHOW PROCEDURES": [{"names": []}],
        "SHOW INDEXES": [{"indexes": [{"name": "kg_chunks_fulltext", "type": "FULLTEXT", "state": "ONLINE"}]}],
    })

    result = GraphIntelligence(store).capabilities()

    assert result["neo4j"]["connected"] is True
    assert result["neo4j"]["version"] == "5.26.0"
    assert result["gds"]["available"] is False
    assert result["indexes"][0]["name"] == "kg_chunks_fulltext"


def test_capabilities_reads_gds_version_as_cypher_function():
    store = FakeStore({
        "CALL dbms.components()": [{"name": "Neo4j Kernel", "version": "2026.09.0", "edition": "enterprise"}],
        "RETURN gds.version() AS version": [{"version": "2.26.0"}],
        "SHOW PROCEDURES": [{"names": ["gds.leiden.write"]}],
        "SHOW INDEXES": [{"indexes": []}],
    })

    result = GraphIntelligence(store).capabilities()

    assert result["gds"]["available"] is True
    assert result["gds"]["version"] == "2.26.0"
    assert any(query == "RETURN gds.version() AS version" for query, _ in store.calls)


def test_capabilities_discovers_gds_algorithms_even_if_version_probe_fails():
    store = FakeStore({
        "CALL dbms.components()": [{"name": "Neo4j Kernel", "version": "5.26.0", "edition": "enterprise"}],
        "RETURN gds.version() AS version": [],
        "SHOW PROCEDURES": [{"names": ["gds.leiden.write", "gds.pageRank.write", "db.labels"]}],
        "SHOW INDEXES": [{"indexes": []}],
    })

    result = GraphIntelligence(store).capabilities()

    assert result["gds"]["available"] is True
    assert "gds.leiden.write" in result["gds"]["algorithms"]
    assert "gds.pageRank.write" in result["gds"]["algorithms"]
    assert "db.labels" not in result["gds"]["algorithms"]


def test_analyze_records_a_completed_run_without_gds_or_llm():
    store = FakeStore({
        "CALL dbms.components()": [{"name": "Neo4j Kernel", "version": "5.26.0", "edition": "community"}],
        "RETURN gds.version() AS version": [],
        "SHOW PROCEDURES": [{"names": []}],
        "SHOW INDEXES": [{"indexes": []}],
        "MATCH (f:KGFile) WHERE f.sha256": [],
        "RETURN count(f) AS count": [{"count": 0}],
        "RETURN r.status AS status": [{"status": "completed"}],
    })

    result = GraphIntelligence(store).analyze(run_gds=False)

    assert result["status"] == "completed"
    assert result["gds"]["status"] == "not requested"
    assert result["link_candidates_created_or_updated"] == 0
    assert any("CREATE (r:KGAnalysisRun" in query for query, _ in store.calls)
    assert any("CASE WHEN r.status = 'running' THEN 'completed'" in query for query, _ in store.calls)


def test_review_candidate_validation_and_feedback_persistence():
    store = FakeStore({
        "MATCH (c:KGLinkCandidate {id: $id})": [{
            "id": "candidate-1", "status": "proposed", "subject_id": "s1", "object_id": "o1",
            "subject_name": "Material A", "object_name": "Process B", "predicate": "CO_MENTIONED_WITH",
            "reason": "Repeated co-mention", "evidence_count": 2, "evidence_chunk_ids": ["chunk-1"],
            "review_note": None, "is_stale": False,
        }]
    })
    intelligence = GraphIntelligence(store)

    result = intelligence.review_link_candidate("candidate-1", "rejected", "Only a coincidental mention")

    assert result["status"] == "rejected"
    candidate_query, _ = store.calls[0]
    assert "RETURN c.id AS id" in candidate_query
    update_query, parameters = next(
        (query, params) for query, params in store.calls
        if "SET c.status = $decision" in query
    )
    assert "SET c.status = $decision" in update_query
    assert parameters["decision"] == "rejected"
    assert not any("KG_RELATED_TO" in query for query, _ in store.calls)
    assert any("SET a.status = 'retracted'" in query and "DELETE link" in query for query, _ in store.calls)


def test_review_accepted_candidate_creates_generic_assertion_not_fixed_edge():
    store = FakeStore({
        "MATCH (c:KGLinkCandidate {id: $id})": [{
            "id": "candidate-1", "status": "proposed", "subject_id": "s1", "object_id": "o1",
            "subject_name": "Material A", "object_name": "Process B", "predicate": "CO_MENTIONED_WITH",
            "reason": "Repeated co-mention", "evidence_count": 2, "evidence_chunk_ids": ["chunk-1"],
            "review_note": None, "is_stale": False,
        }],
        "MATCH (c:KGLinkCandidate {id: $candidate_id})": [{
            "assertion_id": "assertion-1", "assertion_status": "accepted", "status": "accepted"
        }],
    })

    result = GraphIntelligence(store).review_link_candidate("candidate-1", "accepted", "Verified")

    assert result["assertion_status"] == "accepted"
    assertion_query = next(query for query, _ in store.calls if "MERGE (a:KGAssertion" in query)
    assert "KG_ASSERTS_SUBJECT" in assertion_query
    assert "KG_ASSERTS_OBJECT" in assertion_query
    assert "KG_ASSERTED_LINK" in assertion_query
    assert "KG_RELATED_TO" not in assertion_query


def test_review_candidate_rejects_invalid_decision_before_database_call():
    store = FakeStore()

    with pytest.raises(ValueError, match="decision"):
        GraphIntelligence(store).review_link_candidate("candidate-1", "maybe")

    assert store.calls == []


def test_review_candidate_raises_when_id_does_not_exist():
    intelligence = GraphIntelligence(FakeStore())

    with pytest.raises(ValueError, match="nicht gefunden"):
        intelligence.review_link_candidate("missing", "accepted")


def test_co_mention_query_excludes_previously_reviewed_candidates():
    store = FakeStore()
    intelligence = GraphIntelligence(store)

    assert intelligence._propose_co_mention_candidates("run", None, 100, 20) == 0
    candidate_query = next(query for query, _ in store.calls if "WITH DISTINCT c ORDER BY c.id" in query)
    assert "existing.status IN ['accepted', 'rejected', 'deferred']" in candidate_query


def test_gds_analysis_skips_cleanly_when_gds_is_missing():
    store = FakeStore({"MATCH (e:KGEntity)": [{"nodes": 100, "relationship_matches": 200}]})
    intelligence = GraphIntelligence(store)
    intelligence.capabilities = lambda: {"gds": {"available": False, "algorithms": []}}  # type: ignore[method-assign]

    result = intelligence.run_gds_analysis(run_id="test-run")

    assert result["status"] == "skipped"
    assert "nicht verfügbar" in result["reason"]


def test_chunk_knn_projection_uses_nonempty_relationship_schema():
    store = FakeStore({
        "SET c:KGChunkEmbedded RETURN count(c) AS embedded_chunks": [{"embedded_chunks": 5}],
        "CALL gds.graph.project": [{"graphName": "winkg_knn_test", "nodeCount": 5, "relationshipCount": 0}],
        "YIELD nodesCompared, relationshipsWritten, computeMillis": [
            {"nodesCompared": 5, "relationshipsWritten": 4, "computeMillis": 15}
        ],
        "SET r.analysis_run_id = $run_id": [{"stored_edges": 4}],
    })
    intelligence = GraphIntelligence(store)

    result = intelligence._run_chunk_similarity_knn(run_id="test-run", knn_proc="gds.knn.write")

    assert result["status"] == "completed"
    projection = next((query, params) for query, params in store.calls if "CALL gds.graph.project" in query)
    assert "KG_HAS_CHUNK: {orientation: 'UNDIRECTED'}" in projection[0]
    assert "{nodeProperties: ['embedding']}" in projection[0]
    assert "{}" not in projection[0]


def test_gds_analysis_honours_memory_safety_limit():
    store = FakeStore({"MATCH (e:KGEntity)": [{"nodes": 60000, "relationship_matches": 200}]})
    intelligence = GraphIntelligence(store)
    intelligence.capabilities = lambda: {"gds": {"available": True, "algorithms": ["gds.louvain.write"]}}  # type: ignore[method-assign]

    result = intelligence.run_gds_analysis(run_id="test-run")

    assert result["status"] == "skipped"
    assert result["max_entity_nodes"] == 50000
    assert not any("gds.graph.project" in query for query, _ in store.calls)


def test_stale_candidate_cannot_be_accepted_and_decision_is_not_mutated():
    store = FakeStore({
        "MATCH (c:KGLinkCandidate {id: $id})": [{
            "id": "candidate-stale", "status": "proposed", "subject_id": "s1", "object_id": "o1",
            "subject_name": "A", "object_name": "B", "predicate": "CO_MENTIONED_WITH",
            "reason": "Outdated", "evidence_count": 1, "evidence_chunk_ids": ["old-chunk"],
            "review_note": None, "is_stale": True,
        }]
    })

    with pytest.raises(ValueError, match="veraltet"):
        GraphIntelligence(store).review_link_candidate("candidate-stale", "accepted")

    assert len(store.calls) == 1
    assert "SET c.status" not in store.calls[0][0]


def test_gds_skips_if_no_actual_relationships_exist():
    store = FakeStore({
        "OPTIONAL MATCH (a:KGEntity)-[r:KG_ASSERTED_LINK {status: 'accepted'}]-(b:KGEntity)": [
            {"nodes": 2, "relationship_matches": 0}
        ]
    })
    intelligence = GraphIntelligence(store)
    intelligence.capabilities = lambda: {"gds": {"available": True, "algorithms": ["gds.leiden.write"]}}  # type: ignore[method-assign]

    result = intelligence.run_gds_analysis(run_id="test-run")

    assert result["status"] == "skipped"
    assert any("relationship_matches" in query for query, _ in store.calls)
    assert not any("gds.graph.project" in query for query, _ in store.calls)


def test_gds_node_similarity_creates_reviewable_structural_candidates():
    store = FakeStore({
        "OPTIONAL MATCH (a:KGEntity)-[r:KG_ASSERTED_LINK {status: 'accepted'}]-(b:KGEntity)": [
            {"nodes": 3, "relationship_matches": 2}
        ],
        "CALL gds.graph.project": [{"graphName": "winkg_test", "nodeCount": 3, "relationshipCount": 2}],
        "CALL gds.leiden.write": [{"communityCount": 1, "nodePropertiesWritten": 3}],
        "CALL gds.pageRank.write": [{"nodePropertiesWritten": 3}],
        "YIELD node1, node2, similarity": [{
            "subject_id": "entity-a", "subject_name": "A", "subject_type": "System",
            "object_id": "entity-b", "object_name": "B", "object_type": "System", "similarity": 0.75,
        }],
    })
    intelligence = GraphIntelligence(store)
    intelligence.capabilities = lambda: {"gds": {"available": True, "algorithms": [
        "gds.leiden.write", "gds.pageRank.write", "gds.nodeSimilarity.stream"
    ]}}  # type: ignore[method-assign]

    result = intelligence.run_gds_analysis(run_id="test-run")

    assert result["node_similarity"]["status"] == "completed"
    assert result["node_similarity"]["candidates_created_or_updated"] == 1
    candidate_call = next(
        (query, parameters) for query, parameters in store.calls
        if "MERGE (c:KGLinkCandidate {id: $id})" in query
    )
    assert candidate_call[1]["predicate"] == "STRUCTURALLY_SIMILAR_TO"
    assert candidate_call[1]["score"] == 0.75


def test_review_feedback_metrics_reports_rates_by_method_and_threshold():
    store = FakeStore({
        "MATCH (c:KGLinkCandidate) WHERE c.status IN ['accepted', 'rejected']": [
            {"method": "cypher_co_mention", "accepted": 4, "rejected": 1,
             "reviewed": 5, "acceptance_rate": 0.8},
            {"method": "gds_node_similarity", "accepted": 1, "rejected": 2,
             "reviewed": 3, "acceptance_rate": 1 / 3},
        ]
    })

    result = GraphIntelligence(store)._review_feedback_metrics()

    assert result[0]["method"] == "cypher_co_mention"
    assert result[0]["calibration_ready"] is True
    assert result[0]["acceptance_rate"] == 0.8
    assert result[1]["calibration_ready"] is False
    assert result[1]["rejected"] == 2


def test_gds_weakly_connected_components_are_persisted_to_neo4j():
    store = FakeStore({
        "OPTIONAL MATCH (a:KGEntity)-[r:KG_ASSERTED_LINK {status: 'accepted'}]-(b:KGEntity)": [
            {"nodes": 5, "relationship_matches": 3,
             "relationship_types": ["KG_ASSERTED_LINK"]}
        ],
        "CALL gds.graph.project": [{"graphName": "winkg_test", "nodeCount": 5, "relationshipCount": 3}],
        "CALL gds.wcc.write": [{"componentCount": 2, "nodePropertiesWritten": 5}],
        "MATCH (e:KGEntity) WHERE e.kg_gds_component_run_id": [{"component_nodes": 2, "largest_component_size": 4}],
    })
    intelligence = GraphIntelligence(store)
    intelligence.capabilities = lambda: {"gds": {"available": True, "algorithms": ["gds.wcc.write"]}}  # type: ignore[method-assign]

    result = intelligence.run_gds_analysis(run_id="test-run")

    assert result["weakly_connected_components"]["status"] == "completed"
    assert result["weakly_connected_components"]["componentCount"] == 2
    projection_query, projection_parameters = next(
        (query, params) for query, params in store.calls if "CALL gds.graph.project" in query
    )
    assert projection_parameters["relationship_projection"] == {
        "KG_ASSERTED_LINK": {"orientation": "UNDIRECTED"}
    }
    assert any("MERGE (c:KGComponent" in query for query, _ in store.calls)
    assert any("kind = $kind" in query for query, _ in store.calls)


def test_accepted_review_preserves_candidate_method_and_analysis_provenance():
    store = FakeStore({
        "MATCH (c:KGLinkCandidate {id: $id})": [{
            "id": "candidate-structural", "status": "proposed", "subject_id": "s1", "object_id": "o1",
            "subject_name": "A", "object_name": "B", "predicate": "STRUCTURALLY_SIMILAR_TO",
            "candidate_method": "gds_node_similarity", "analysis_run_id": "run-42",
            "reason": "Similar graph neighborhoods", "evidence_count": 0, "evidence_chunk_ids": [],
            "review_note": None, "is_stale": False,
        }],
        "MATCH (c:KGLinkCandidate {id: $candidate_id})": [{
            "assertion_id": "assertion-42", "assertion_status": "accepted", "status": "accepted"
        }],
    })

    GraphIntelligence(store).review_link_candidate("candidate-structural", "accepted", "checked")

    _, params = next(
        (query, params) for query, params in store.calls if "MERGE (a:KGAssertion" in query
    )
    assert params["candidate_method"] == "gds_node_similarity"
    assert params["analysis_run_id"] == "run-42"


def test_gds_knn_query_uses_valid_configuration_map_closing():
    store = FakeStore({
        "SET c:KGChunkEmbedded RETURN count(c) AS embedded_chunks": [{"embedded_chunks": 2}],
        "CALL gds.graph.project($graph_name, 'KGChunkEmbedded'": [
            {"graphName": "winkg_test", "nodeCount": 2, "relationshipCount": 0}
        ],
        "CALL gds.knn.write": [{"nodesCompared": 2, "relationshipsWritten": 1, "computeMillis": 10}],
        "RETURN count(r) AS stored_edges": [{"stored_edges": 1}],
        "RETURN a.path AS source_path": [{"source_path": "a.pdf", "target_path": "b.pdf", "similarity": 0.9}],
    })
    store.settings.gds_max_knn_chunks = 10000

    result = GraphIntelligence(store)._run_chunk_similarity_knn("run-test", "gds.knn.write")

    assert result["status"] == "completed"
    knn_query = next(query for query, _ in store.calls if "CALL gds.knn.write" in query)
    assert "writeProperty: 'similarity'})" in knn_query
    assert "writeProperty: 'similarity'}})" not in knn_query


def test_analysis_counts_only_accepted_asserted_links_as_factual_relationships():
    store = FakeStore()
    GraphIntelligence(store)._analysis_counts(None)
    rel_query = next(query for query, _ in store.calls if "KG_ASSERTED_LINK" in query)

    assert "KG_ASSERTED_LINK {status: 'accepted'}" in rel_query
    assert "KG_RELATED_TO" not in rel_query


def test_entity_gds_projection_cleans_nonaccepted_edge_history_before_projection():
    store = FakeStore({
        "OPTIONAL MATCH (a:KGEntity)-[r:KG_ASSERTED_LINK {status: 'accepted'}]-(b:KGEntity)": [
            {"nodes": 2, "relationship_matches": 1, "relationship_types": ["KG_ASSERTED_LINK"]}
        ],
        "CALL gds.graph.project": [{"graphName": "winkg_test", "nodeCount": 2, "relationshipCount": 1}],
        "CALL gds.wcc.write": [{"componentCount": 1, "nodePropertiesWritten": 2}],
        "MATCH (e:KGEntity) WHERE e.kg_gds_component_run_id": [{"component_nodes": 1, "largest_component_size": 2}],
    })
    intelligence = GraphIntelligence(store)
    intelligence.capabilities = lambda: {"gds": {"available": True, "algorithms": ["gds.wcc.write"]}}  # type: ignore[method-assign]

    intelligence.run_gds_analysis(run_id="test-run")

    cleanup_index = next(i for i, (q, _) in enumerate(store.calls)
                         if "WHERE coalesce(r.status, 'proposed') <> 'accepted' DELETE r" in q)
    projection_index = next(i for i, (q, _) in enumerate(store.calls) if "CALL gds.graph.project" in q)
    assert cleanup_index < projection_index
    assert "KG_ASSERTED_LINK {status: 'accepted'}" in next(q for q, _ in store.calls if "relationship_matches" in q)


def test_autonomous_quality_cycle_batches_candidates_and_revalidates_in_store(monkeypatch):
    import winkg.graph_intelligence as module

    candidates = [
        {"id": f"c{i}", "predicate": "CO_MENTIONED_WITH", "method": "co_mention",
         "reason": "Repeated co-mention", "evidence_count": 1, "evidence_chunk_ids": [f"chunk{i}"],
         "subject_id": f"s{i}", "subject_name": f"System {i}", "subject_type": "System",
         "object_id": f"o{i}", "object_name": f"Process {i}", "object_type": "Process",
         "sources": [{"id": f"chunk{i}", "text": f"System {i} uses Process {i} for processing.", "sha256": f"sha{i}", "path": f"file{i}.txt"}]}
        for i in range(3)
    ]
    store = FakeStore({"MATCH (c:KGLinkCandidate) ": candidates})
    store.settings = SimpleNamespace(
        gds_max_entity_nodes=50000, autonomous_quality_enabled=True, llm_base_url="http://llm/v1",
        llm_model="test-model", quality_max_candidates_per_run=3, quality_batch_size=2,
        quality_min_confidence=0.82,
    )
    stored = []
    def apply(candidate_id, **kwargs):
        stored.append((candidate_id, kwargs))
        return {"id": candidate_id, "status": "accepted"}
    store.apply_autonomous_candidate_review = apply  # type: ignore[attr-defined]

    class FakeExtractor:
        def __init__(self, settings):
            pass
        def review_candidates(self, batch):
            return [{"candidate_id": item["id"], "decision": "accept", "predicate": "USES",
                     "evidence": f"{item['subject_name']} uses {item['object_name']}",
                     "evidence_chunk_id": item["sources"][0]["id"], "confidence": 0.95,
                     "polarity": "positive", "reason": "Explicit source statement"}
                    for item in batch]
    monkeypatch.setattr(module, "EntityExtractor", FakeExtractor)

    result = GraphIntelligence(store)._run_autonomous_quality_cycle(root_id=None)

    assert result["status"] == "completed"
    assert result["eligible"] == 3
    assert result["accepted"] == 3
    assert result["llm_calls"] == 2
    assert len(stored) == 3
    assert all(call[1]["decision"] == "accept" for call in stored)


def test_autonomous_quality_cycle_skips_when_disabled():
    store = FakeStore()
    store.settings = SimpleNamespace(autonomous_quality_enabled=False)
    result = GraphIntelligence(store)._run_autonomous_quality_cycle(root_id=None)

    assert result["status"] == "disabled"
    assert result["llm_calls"] == 0
