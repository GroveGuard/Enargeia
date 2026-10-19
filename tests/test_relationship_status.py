from __future__ import annotations

from types import ModuleType, SimpleNamespace
import sys

try:
    import neo4j  # noqa: F401
except ImportError:
    neo4j_stub = ModuleType("neo4j")
    neo4j_stub.GraphDatabase = SimpleNamespace(driver=None)  # type: ignore[attr-defined]
    sys.modules["neo4j"] = neo4j_stub

from winkg.neo4j_store import Neo4jStore


def test_llm_extracted_relationships_are_stored_as_proposals_not_facts():
    store = Neo4jStore.__new__(Neo4jStore)
    calls: list[tuple[str, dict]] = []
    store._run = lambda query, parameters=None: calls.append((query, parameters or {})) or []  # type: ignore[method-assign]

    store.save_entities_for_chunk(
        "root-1", "chunk-1",
        [
            {"name": "System A", "type": "System", "description": ""},
            {"name": "Process B", "type": "Process", "description": ""},
        ],
        [{"source": "System A", "target": "Process B", "type": "supports", "evidence": "The text says so."}],
    )

    relation_query = next(query for query, _ in calls if "MERGE (s)-[r:KG_RELATED_TO" in query)
    assert "SET r.status = item.status" in relation_query
    assert "CALL (item)" in relation_query
    assert "FOREACH (_ IN CASE WHEN item.status = 'accepted'" in relation_query
    assert "SET a.status = item.status" in relation_query
    relation_params = next(params for query, params in calls if "UNWIND $relations AS item" in query)
    assert relation_params["relations"][0]["status"] == "proposed"  # No original source text: fail closed.
    assert "MATCH (s:KGEntity {id: item.source_id}), (t:KGEntity" not in relation_query



def test_chunk_details_separates_confirmed_edges_from_extraction_proposals():
    store = Neo4jStore.__new__(Neo4jStore)
    calls: list[tuple[str, dict]] = []
    store._run = lambda query, parameters=None: calls.append((query, parameters or {})) or [{
        "id": "chunk-1", "entities": [], "graph_relations": [], "proposed_relations": [], "related": []
    }]  # type: ignore[method-assign]

    result = store.get_chunk_details("chunk-1")
    query = calls[0][0]

    assert result is not None
    assert "KG_ASSERTED_LINK {status: 'accepted'}" in query
    assert "AS proposed_relations" in query
    assert "status: 'proposed'" in query
