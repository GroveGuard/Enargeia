from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from winkg import entity_extraction as extraction_module
from winkg import graph_intelligence as intelligence_module
from winkg.entity_extraction import EntityExtractor
from winkg.graph_intelligence import GraphIntelligence
from winkg.search import KnowledgeSearch
from winkg.chunking import stable_id


class FakeResponse:
    def __init__(self, content: str):
        self.content = content

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {"choices": [{"message": {"content": self.content}}]}


class FakeSession:
    def __init__(self, content: str):
        self.content = content
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        return FakeResponse(self.content)


class FakeStore:
    def __init__(self, responses: dict[str, list[dict[str, Any]]] | None = None):
        self.responses = responses or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.settings = SimpleNamespace(
            community_summaries_enabled=True,
            community_summary_min_members=3,
            community_summary_max_per_run=8,
            community_summary_batch_size=2,
            llm_base_url="http://llm/v1",
            llm_model="test-model",
            llm_api_key="",
            llm_timeout_seconds=10,
            global_search_enabled=True,
        )

    def _run(self, query: str, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        params = parameters or {}
        self.calls.append((query, params))
        for key, rows in self.responses.items():
            if key in query:
                return rows
        return []


def test_community_summary_generation_is_grounded_and_bounds_payload():
    settings = SimpleNamespace(
        llm_base_url="http://llm/v1", llm_model="test-model", llm_api_key="", llm_timeout_seconds=10
    )
    extractor = EntityExtractor(settings)  # type: ignore[arg-type]
    extractor.session = FakeSession(json.dumps({"results": [
        {"community_id": "stable-c1", "title": "SAP Materialstamm", "summary": "Die Gruppe verbindet Materialstammdaten und Änderungsprozesse über bestätigte Beziehungen.", "key_points": ["Materialstammdaten sind verknüpft."], "conflicts": [], "caveats": ["Nur gespeicherte Beziehungen wurden berücksichtigt."]},
        {"community_id": "not-in-input", "title": "Halluziniert", "summary": "Eine ausreichend lange, aber nicht zulässige Zusammenfassung."},
        {"community_id": "stable-c1", "title": "Duplikat", "summary": "Duplikat, das nicht ein zweites Mal übernommen werden darf."},
    ]}))  # type: ignore[assignment]

    result = extractor.summarize_communities([{
        "id": "stable-c1", "member_count": 3,
        "members": [{"name": "Material", "type": "Konzept", "description": "Materialstamm"}],
        "relations": [{"source": "Material", "predicate": "PART_OF", "target": "SAP", "evidence": "Material ist Teil von SAP", "source_path": "a.txt"}],
    }])

    assert len(result) == 1
    assert result[0]["community_id"] == "stable-c1"
    assert result[0]["title"] == "SAP Materialstamm"
    assert "not-in-input" not in {item["community_id"] for item in result}
    request_payload = json.loads(extractor.session.calls[0]["json"]["messages"][1]["content"])
    assert request_payload["communities"][0]["confirmed_relations"][0]["source_path"] == "a.txt"
    assert extractor.session.calls[0]["json"]["temperature"] == 0


def test_unchanged_community_summary_is_reused_without_an_llm_call():
    members = [
        {"id": "e1", "name": "A", "type": "System", "description": ""},
        {"id": "e2", "name": "B", "type": "System", "description": ""},
        {"id": "e3", "name": "C", "type": "Konzept", "description": ""},
    ]
    relations = [{
        "source": "A", "predicate": "USES", "target": "B", "evidence": "A nutzt B",
        "source_path": "a.txt", "polarity": "positive", "assertion_id": "a1",
    }]
    fingerprint_payload = {
        "members": sorted([
            {"id": str(m.get("id") or ""), "name": str(m.get("name") or ""),
             "type": str(m.get("type") or ""), "description": str(m.get("description") or "")}
            for m in members
        ], key=lambda item: item["id"]),
        "relations": sorted([
            {"source": str(r.get("source") or ""), "predicate": str(r.get("predicate") or ""),
             "target": str(r.get("target") or ""), "evidence": str(r.get("evidence") or ""),
             "source_path": str(r.get("source_path") or ""),
             "source_sha256": str(r.get("source_sha256") or ""),
             "polarity": str(r.get("polarity") or "")}
            for r in relations
        ], key=lambda item: (item["source"], item["predicate"], item["target"], item["evidence"])),
    }
    fingerprint = stable_id("community-summary-source-v1", json.dumps(fingerprint_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    detail_json = json.dumps({"source_fingerprint": fingerprint, "summary": "existing"})
    store = FakeStore({
        "MATCH (c:KGCommunity) WHERE coalesce(c.is_stale, false) = false": [
            {"id": "community:abc", "member_count": 3, "summary_fingerprint": fingerprint}
        ],
        "RETURN c.id AS id, c.member_count AS member_count, c.root_id AS root_id, ": [
            {"id": "community:abc", "member_count": 3, "root_id": "root1", "members": [
                {"id": "e1", "name": "A", "type": "System", "description": ""},
                {"id": "e2", "name": "B", "type": "System", "description": ""},
                {"id": "e3", "name": "C", "type": "Konzept", "description": ""},
            ], "relations": [
                {"source": "A", "predicate": "USES", "target": "B", "evidence": "A nutzt B", "source_path": "a.txt", "polarity": "positive", "assertion_id": "a1"}
            ], "summary_fingerprint": fingerprint}
        ],
        "MATCH (i:KGInsight {id: $id}) RETURN i.details_json": [
            {"details_json": detail_json, "is_stale": True}
        ],
        "MATCH (r:KGAnalysisRun {id: $run_id}) WHERE r.status = 'running'": [{"id": "run-current"}],
    })
    result = GraphIntelligence(store)._generate_community_summaries(run_id="run-current")

    assert result["status"] == "completed"
    assert result["unchanged"] == 1
    assert result["llm_calls"] == 0
    reuse_query = next(query for query, _ in store.calls if "MERGE (i)-[:KG_SUMMARIZES_COMMUNITY]->(c)" in query)
    assert "FOREACH (rel IN old_rels | DELETE rel)" in reuse_query


def test_changed_community_summary_is_saved_as_stable_insight(monkeypatch):
    fingerprint = "new-fingerprint"
    store = FakeStore({
        "MATCH (c:KGCommunity) WHERE coalesce(c.is_stale, false) = false": [
            {"id": "community:abc", "member_count": 3, "summary_fingerprint": "old"}
        ],
        "RETURN c.id AS id, c.member_count AS member_count, c.root_id AS root_id, ": [
            {"id": "community:abc", "member_count": 3, "root_id": "root1", "members": [
                {"id": "e1", "name": "A", "type": "System", "description": "first"},
                {"id": "e2", "name": "B", "type": "System", "description": "second"},
                {"id": "e3", "name": "C", "type": "Konzept", "description": "third"},
            ], "relations": [
                {"source": "A", "predicate": "USES", "target": "B", "evidence": "A nutzt B", "source_path": "a.txt", "polarity": "positive", "assertion_id": "a1"}
            ], "summary_fingerprint": "old"}
        ],
        "MATCH (i:KGInsight {id: $id}) RETURN i.details_json": [{"details_json": "{}", "is_stale": True}],
        "MATCH (r:KGAnalysisRun {id: $run_id}) WHERE r.status = 'running'": [{"id": "run-current"}],
    })

    class FakeExtractor:
        def __init__(self, settings: Any):
            pass

        def summarize_communities(self, communities: list[dict[str, Any]]) -> list[dict[str, Any]]:
            assert communities[0]["id"] == "community:abc"
            return [{
                "community_id": "community:abc", "title": "A and B", 
                "summary": "A verwendet B gemäß dem gespeicherten, bestätigten Quellenbeleg.",
                "key_points": ["A verwendet B."], "conflicts": [], "caveats": [],
            }]

    monkeypatch.setattr(intelligence_module, "EntityExtractor", FakeExtractor)
    result = GraphIntelligence(store)._generate_community_summaries(run_id="run-current")

    assert result["generated"] == 1
    assert result["llm_calls"] == 1
    save_query, params = next(
        (query, params) for query, params in store.calls if "i.kind = 'community_summary'" in query
    )
    assert "MERGE (i:KGInsight {id: $insight_id})" in save_query
    assert params["title"] == "A and B"
    saved_details = json.loads(params["details_json"])
    assert saved_details["source_fingerprint"] != "old"
    assert saved_details["source_paths"] == ["a.txt"]


def test_global_search_uses_summary_and_source_context(monkeypatch):
    store = FakeStore({
        "MATCH (i:KGInsight {kind: 'community_summary'})": [{
            "id": "insight-1", "run_id": "run-1", "title": "SAP Architektur",
            "description": "Materialstamm und SAP Prozesse hängen zusammen.",
            "details_json": json.dumps({"summary": "SAP Materialstamm wird in Prozessen verwendet.", "member_names": ["SAP", "Material"]}),
            "relevance": 3, "community_id": "community-1", "member_count": 4,
        }]
    })
    searcher = object.__new__(KnowledgeSearch)
    searcher.settings = store.settings
    searcher.store = store
    searcher.search = lambda query, limit, root_id=None: [{
        "id": "chunk-1", "path": "src/main.py", "filename": "main.py", "text": "SAP Material wird verarbeitet.",
        "entities": [{"name": "SAP", "type": "System"}], "score": 0.1,
    }]
    monkeypatch.setattr(
        extraction_module.EntityExtractor, "answer_global_query",
        lambda self, query, communities, sources: {
            "answer": "SAP verarbeitet Material [S1] und gehört zum Kontext [C1].",
            "used_sources": ["S1", "S99"], "used_communities": ["C1"], "uncertainties": [],
        },
    )

    result = searcher.global_search("Welche SAP Architektur?", limit=5)

    assert result["status"] == "completed"
    assert result["community_summaries_used"] == 1
    assert result["source_chunks_retrieved"] == 1
    assert result["used_sources"] == ["S1"]
    assert "[S99]" not in result["answer"]
    query, params = next((q, p) for q, p in store.calls if "MATCH (i:KGInsight {kind: 'community_summary'})" in q)
    assert "coalesce(i.is_stale, false) = false" in query
    assert "c.root_id = $root_id" in query
    assert "root_id" in params
