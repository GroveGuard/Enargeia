from __future__ import annotations

from types import ModuleType, SimpleNamespace
import sys

# Unit tests do not require a running Neo4j server or the official driver.
try:
    import neo4j  # noqa: F401
except ImportError:
    neo4j_stub = ModuleType("neo4j")
    neo4j_stub.GraphDatabase = SimpleNamespace(driver=None)  # type: ignore[attr-defined]
    sys.modules["neo4j"] = neo4j_stub

from winkg.neo4j_store import Neo4jStore


def test_delete_root_invalidates_derived_data_and_removes_only_root_nodes():
    store = Neo4jStore.__new__(Neo4jStore)
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_run(query: str, parameters: dict | None = None):
        calls.append((query, parameters or {}))
        return []

    store._run = fake_run  # type: ignore[method-assign]
    store.delete_root("root-123")

    assert any("WHERE n.root_id = $root_id DETACH DELETE n" in q for q, _ in calls)
    assert any("MATCH (r:KGRoot {id: $root_id}) DETACH DELETE r" in q for q, _ in calls)
    assert any("a.method IN ['human_review', 'auto_source_evidence', 'llm_autonomous']" in q for q, _ in calls)
    assert all(p.get("root_id", "root-123") == "root-123" for _, p in calls if "root_id" in p)
