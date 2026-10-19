from __future__ import annotations

from types import ModuleType, SimpleNamespace
import sys

# Allow search-orchestration tests to run without a live Neo4j Python driver.
try:
    import neo4j  # noqa: F401
except ImportError:
    neo4j_stub = ModuleType("neo4j")
    neo4j_stub.GraphDatabase = SimpleNamespace(driver=None)  # type: ignore[attr-defined]
    sys.modules["neo4j"] = neo4j_stub

import winkg.search as search_module
from winkg.search import KnowledgeSearch, format_search_results


class FakeEmbeddings:
    enabled = True

    def __init__(self, _settings):
        pass

    def embed_one(self, _query: str) -> list[float]:
        return [0.1, 0.2]


class FakeStore:
    def __init__(self):
        self.graph_seeds: list[str] = []

    def close(self):
        pass

    def vector_search(self, _vector, _limit, _root_id):
        return [{"id": "seed-vector", "score": 0.91}]

    def fulltext_search(self, _query, _limit, _root_id):
        return [{"id": "seed-text", "score": 6.3}]

    def file_fulltext_search(self, _query, _limit, _root_id):
        return [{"id": "file-match", "score": 4.0}]

    def chunks_for_file(self, _file_id, limit=3):
        return [{"id": "seed-file"}][:limit]

    def graph_related_chunk_search(self, seed_chunk_ids, limit=20, root_id=None):
        self.graph_seeds = list(seed_chunk_ids)
        assert limit > 0
        assert root_id is None
        return [{
            "id": "graph-only-result",
            "hops": 2,
            "via_entities": ["M-4711", "Fertigungsprozess"],
            "supporting_seeds": 2,
            "score": 0.7,
        }]

    def get_chunk_details(self, chunk_id):
        return {
            "id": chunk_id,
            "text": f"Text für {chunk_id}",
            "filename": f"{chunk_id}.md",
            "path": f"C:/sources/{chunk_id}.md",
            "entities": [],
            "graph_relations": [],
            "related": [],
        }


def test_search_expands_vector_fulltext_and_file_seeds_through_graph(monkeypatch):
    monkeypatch.setattr(search_module, "EmbeddingClient", FakeEmbeddings)
    store = FakeStore()
    searcher = KnowledgeSearch(SimpleNamespace(), store=store)

    results = searcher.search("Material M-4711", limit=5)

    assert store.graph_seeds == ["seed-vector", "seed-text", "seed-file"]
    graph_result = next(row for row in results if row["id"] == "graph-only-result")
    assert "graph" in graph_result["score_sources"]
    assert graph_result["graph_paths"][0]["hops"] == 2
    formatted = format_search_results("Material M-4711", [graph_result])
    assert "Graph-Kontext" in formatted
    assert "M-4711" in formatted


def test_search_still_works_when_graph_expansion_fails(monkeypatch):
    monkeypatch.setattr(search_module, "EmbeddingClient", FakeEmbeddings)
    store = FakeStore()
    store.graph_related_chunk_search = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("graph not available"))
    searcher = KnowledgeSearch(SimpleNamespace(), store=store)

    results = searcher.search("some query", limit=5)

    assert results
    assert all("graph_paths" not in row for row in results)


def test_graph_expansion_query_traverses_only_accepted_assertions():
    from winkg.neo4j_store import Neo4jStore

    store = Neo4jStore.__new__(Neo4jStore)
    calls = []
    store._run = lambda query, parameters=None: calls.append((query, parameters or {})) or []  # type: ignore[method-assign]

    store.graph_related_chunk_search(["seed-1"], root_id=None)

    query = calls[0][0]
    assert "[:KG_ASSERTED_LINK*1..2]" in query
    assert "rel.status = 'accepted'" in query
    assert "KG_RELATED_TO" not in query


def test_search_formats_extracted_relations_as_unconfirmed_hypotheses():
    from winkg.search import format_search_results

    rendered = format_search_results("example", [{
        "filename": "source.pdf", "path": "C:/source.pdf", "text": "source text", "score": 1.0,
        "entities": [], "graph_relations": [], "related": [],
        "proposed_relations": [{"source": "System A", "target": "Process B", "type": "supports",
                                "status": "proposed", "evidence": "mentions supports"}],
    }])

    assert "Unbestätigte Beziehungshypothesen" in rendered
    assert "keine gesicherten Fakten" in rendered
    assert "Bestätigte Graph-Beziehungen" not in rendered
