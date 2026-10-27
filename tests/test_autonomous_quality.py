from __future__ import annotations

from types import ModuleType, SimpleNamespace
import sys

try:
    import neo4j  # noqa: F401
except ImportError:
    neo4j_stub = ModuleType("neo4j")
    neo4j_stub.GraphDatabase = SimpleNamespace(driver=None)  # type: ignore[attr-defined]
    sys.modules["neo4j"] = neo4j_stub

from winkg.quality import validate_relation_evidence
from winkg.neo4j_store import Neo4jStore


def test_relation_is_accepted_only_with_literal_evidence_and_mentions():
    result = validate_relation_evidence(
        text="System A uses Process B to update the master data.",
        source="System A", target="Process B", evidence="System A uses Process B",
        confidence=0.93, polarity="positive", min_confidence=0.82,
    )
    assert result["accepted"] is True
    assert result["polarity"] == "positive"


def test_paraphrase_and_low_confidence_remain_unconfirmed():
    paraphrase = validate_relation_evidence(
        text="System A uses Process B to update the master data.",
        source="System A", target="Process B", evidence="A is used by B",
        confidence=0.99, polarity="positive", min_confidence=0.82,
    )
    weak = validate_relation_evidence(
        text="System A uses Process B to update the master data.",
        source="System A", target="Process B", evidence="System A uses Process B",
        confidence=0.55, polarity="positive", min_confidence=0.82,
    )
    assert not paraphrase["accepted"]
    assert paraphrase["reason"] == "evidence_not_verbatim"
    assert not weak["accepted"]
    assert weak["reason"] == "confidence_below_threshold"


def test_entity_name_requires_whole_identifier_match():
    from winkg.quality import entity_is_mentioned

    assert entity_is_mentioned("SAP transports the report", "SAP")
    assert not entity_is_mentioned("SAPUI5 transports the report", "SAP")


def test_negative_evidence_can_be_saved_but_is_not_positive_relation():
    result = validate_relation_evidence(
        text="System A does not use Process B in this workflow.",
        source="System A", target="Process B", evidence="System A does not use Process B",
        confidence=0.93, polarity="negative", min_confidence=0.82,
    )
    assert result["accepted"] is True
    assert result["polarity"] == "negative"


def test_entities_must_be_in_quote_or_nearby_in_same_sentence_or_line():
    quoted = validate_relation_evidence(
        text="System A uses Process B. Another sentence follows.",
        source="system a", target="PROCESS B", evidence="System A uses Process B",
        confidence=0.93, polarity="positive", min_confidence=0.82,
    )
    nearby = validate_relation_evidence(
        text="System A and Process B are connected. Evidence confirms this relationship.",
        source="System A", target="Process B", evidence="Evidence confirms this relationship",
        confidence=0.93, polarity="positive", min_confidence=0.82,
    )
    far_apart = validate_relation_evidence(
        text="System A appears here. " + ("filler " * 60) + "Evidence confirms this relationship. "
        + ("filler " * 60) + "Process B appears far away.",
        source="System A", target="Process B", evidence="Evidence confirms this relationship",
        confidence=0.93, polarity="positive", min_confidence=0.82,
    )
    separate_lines = validate_relation_evidence(
        text="System A is discussed here.\nEvidence confirms this relationship.\nProcess B is elsewhere.",
        source="System A", target="Process B", evidence="Evidence confirms this relationship",
        confidence=0.93, polarity="positive", min_confidence=0.82,
    )

    assert quoted["accepted"]
    assert nearby["accepted"]
    assert not far_apart["accepted"]
    assert far_apart["reason"] == "entities_not_near_evidence"
    assert not separate_lines["accepted"]
    assert separate_lines["reason"] == "entities_not_near_evidence"


def test_generic_entities_and_relation_type_are_never_auto_activated():
    generic_entity = validate_relation_evidence(
        text="A1 uses Process B.", source="A1", target="Process B", evidence="A1 uses Process B",
        confidence=0.99, polarity="positive",
    )
    numeric_entity = validate_relation_evidence(
        text="123 uses Process B.", source="123", target="Process B", evidence="123 uses Process B",
        confidence=0.99, polarity="positive",
    )
    generic_relation = validate_relation_evidence(
        text="System A relates to Process B.", source="System A", target="Process B",
        evidence="System A relates to Process B", confidence=0.99, polarity="positive",
        relation_type="related_to",
    )

    assert generic_entity["reason"] == "entity_name_too_generic"
    assert numeric_entity["reason"] == "entity_name_too_generic"
    assert generic_relation["reason"] == "relation_type_too_generic"


def test_expired_and_future_claims_are_not_activated():
    expired = validate_relation_evidence(
        text="System A uses Process B.", source="System A", target="Process B",
        evidence="System A uses Process B.", confidence=0.99, polarity="positive",
        valid_until="2000-01-01", min_confidence=0.82,
    )
    future = validate_relation_evidence(
        text="System A uses Process B.", source="System A", target="Process B",
        evidence="System A uses Process B.", confidence=0.99, polarity="positive",
        valid_from="2999-01-01", min_confidence=0.82,
    )
    assert not expired["accepted"] and expired["reason"] == "expired"
    assert not future["accepted"] and future["reason"] == "not_yet_valid"


def test_source_extraction_promotes_only_when_original_source_text_is_passed():
    store = Neo4jStore.__new__(Neo4jStore)
    calls = []
    store._run = lambda query, parameters=None: calls.append((query, parameters or {})) or []  # type: ignore[method-assign]
    store.save_entities_for_chunk(
        "root-a", "chunk-a",
        [{"name": "System A", "type": "System"}, {"name": "Process B", "type": "Process"}],
        [{"source": "System A", "target": "Process B", "type": "USES",
          "evidence": "System A uses Process B", "confidence": 0.93, "polarity": "positive"}],
        source_text="System A uses Process B to update master data.",
    )
    relation_call = next((query, params) for query, params in calls if "UNWIND $relations AS item" in query)
    assert "SET r.status = item.status" in relation_call[0]
    assert relation_call[1]["relations"][0]["status"] == "accepted"
    assert relation_call[1]["relations"][0]["method"] == "auto_source_evidence"
    assert "KGConsolidatedFact" in relation_call[0]
    assert not any("KGQualityProfile" in query for query, _ in calls)


def test_quality_threshold_uses_only_configured_setting():
    store = Neo4jStore.__new__(Neo4jStore)
    store.settings = SimpleNamespace(quality_min_confidence=0.82)

    def unexpected_query(*args, **kwargs):
        raise AssertionError("Neo4j must not be queried")

    store._run = unexpected_query  # type: ignore[method-assign]

    assert store._quality_threshold() == 0.82


def test_candidate_review_is_aborted_when_source_hash_changed_during_llm_call():
    store = Neo4jStore.__new__(Neo4jStore)
    calls = []

    def fake_run(query, parameters=None):
        calls.append((query, parameters or {}))
        if "OPTIONAL MATCH (chunk:KGChunk)" in query:
            return [{
                "id": "candidate-1", "status": "proposed", "is_stale": False,
                "subject_id": "s1", "subject_name": "System A", "subject_type": "System",
                "object_id": "o1", "object_name": "Process B", "object_type": "Process",
                "evidence_chunk_ids": ["chunk-1"],
                "sources": [{"id": "chunk-1", "sha256": "new-hash", "text": "System A uses Process B in this process."}],
            }]
        return []

    store._run = fake_run  # type: ignore[method-assign]
    result = store.apply_autonomous_candidate_review(
        "candidate-1", decision="reject", reason="old source decision",
        source_snapshot=[{"id": "chunk-1", "sha256": "old-hash"}],
    )

    assert result["status"] == "stale"
    assert result["reason"] == "source_changed_during_review"
    assert any("source_snapshot_guard" in query for query, _ in calls)
    assert not any("SET c.status = 'rejected'" in query for query, _ in calls)
