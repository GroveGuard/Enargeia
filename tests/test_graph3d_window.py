from __future__ import annotations

import json
from pathlib import Path

from winkg.graph3d_window import (
    GraphWindowApi, build_database_loading_payload, build_database_payload,
    classify_database_node, classify_relationship,
    read_graph_payload, write_graph_payload,
)


def test_database_payload_includes_every_node_and_every_relationship():
    snapshot = {
        "nodes": [
            {"id": "r1", "labels": ["KGRoot"], "label": "Root", "path": "C:/docs"},
            {"id": "f1", "labels": ["KGFile"], "label": "a.pdf", "path": "C:/docs/a.pdf"},
            {"id": "c1", "labels": ["KGChunk"], "label": "Chunk 1", "detail": "Preview"},
            {"id": "e1", "labels": ["KGEntity"], "label": "Acme"},
            {"id": "x1", "labels": ["CustomLabel"], "label": "Other node"},
        ],
        "relationships": [
            {"source": "r1", "target": "f1", "type": "KG_CONTAINS"},
            {"source": "f1", "target": "c1", "type": "KG_HAS_CHUNK"},
            {"source": "c1", "target": "e1", "type": "KG_MENTIONS"},
            {"source": "e1", "target": "x1", "type": "CUSTOM_LINK"},
        ],
    }
    payload = build_database_payload(snapshot)
    assert payload["scope"] == "database" and payload["loaded"] is True
    assert {n["id"] for n in payload["nodes"]} == {"r1", "f1", "c1", "e1", "x1"}
    assert len(payload["links"]) == 4
    assert payload["stats"]["node_count"] == 5
    assert payload["stats"]["edge_count"] == 4
    assert payload["stats"]["roots"] == 1
    assert payload["stats"]["files"] == 1
    assert {link["kind"] for link in payload["links"]} == {"structure", "entity", "other"}
    assert payload["stats"]["selected_node_id"] == ""  # start unselected


def test_full_graph_loading_payload_is_explicit_and_empty():
    payload = build_database_loading_payload()
    assert payload["scope"] == "database"
    assert payload["loaded"] is False
    assert payload["nodes"] == []
    assert payload["links"] == []


def test_database_node_and_relationship_classifiers_cover_internal_labels():
    assert classify_database_node(["KGRoot"]) == "root"
    assert classify_database_node(["KGFile"]) == "file"
    assert classify_database_node(["KGLinkCandidate"]) == "knowledge"
    assert classify_database_node(["KGAnalysisRun"]) == "system"
    assert classify_database_node(["ExternalNode"]) == "other"
    assert classify_relationship("KG_CONTAINS") == "structure"
    assert classify_relationship("KG_MENTIONS") == "entity"
    assert classify_relationship("KG_ASSERTS") == "knowledge"
    assert classify_relationship("CUSTOM_LINK") == "other"


def test_3d_viewer_has_no_node_search_and_starts_whole_database_loading():
    html = Path(__file__).parents[1] / "src" / "winkg" / "ui" / "graph3d.html"
    source = html.read_text(encoding="utf-8")
    assert "nodeSearch" not in source
    assert "Knoten suchen" not in source
    assert 'data-kind="knowledge"' in source
    assert "load_database_graph" in source
    assert "Gesamte Datenbank" in source
    assert "function simulate(dt)" in source


def test_graph_snapshot_uses_unrestricted_node_and_relationship_queries():
    from winkg.neo4j_store import Neo4jStore

    store = Neo4jStore.__new__(Neo4jStore)
    queries = []
    def fake_run(query, parameters=None):
        queries.append(query)
        if "MATCH (n)" in query:
            return [{"id": "n1", "labels": ["KGFile"], "label": "a.pdf", "path": "a.pdf", "detail": ""}]
        return [{"source": "n1", "target": "n1", "type": "SELF_LINK"}]
    store._run = fake_run
    snapshot = store.graph_snapshot()
    assert len(snapshot["nodes"]) == 1
    assert len(snapshot["relationships"]) == 1
    assert "LIMIT" not in queries[0].upper()
    assert "MATCH (a)-[r]->(b)" in queries[1]
    assert "LIMIT" not in queries[1].upper()


def test_3d_viewer_supports_clearing_selection_and_starts_unselected():
    html = Path(__file__).parents[1] / "src" / "winkg" / "ui" / "graph3d.html"
    source = html.read_text(encoding="utf-8")
    assert "function clearSelection()" in source
    assert "if(best)selectNode(best);else clearSelection();" in source
    assert "selected=byId.get(dataSelectedId())||null" in source
    assert "selected=byId.get(dataSelectedId())||nodes.reduce" not in source


def test_3d_viewer_neighborhood_mode_limits_labels_to_highlighted_nodes():
    html = Path(__file__).parents[1] / "src" / "winkg" / "ui" / "graph3d.html"
    source = html.read_text(encoding="utf-8")
    assert "showNeighborhood&&!neighborhoodIds.has(n.id)" in source
    assert "Array.from(neighborhoodIds).map(id=>byId.get(id))" in source
    assert "only Labels of highlighted" not in source


def test_3d_viewer_focus_uses_camera_target_in_projection_and_shader():
    html = Path(__file__).parents[1] / "src" / "winkg" / "ui" / "graph3d.html"
    source = html.read_text(encoding="utf-8")
    assert "let cameraTargetId = ''" in source
    assert "function getCameraTarget()" in source
    assert "p.x-target.x" in source
    assert "uniform vec3 uTarget" in source
    assert "cameraTargetId=n.id" in source


def test_obsolete_svg_neighborhood_renderer_is_removed_but_textual_graph_query_remains():
    root = Path(__file__).parents[1] / "src" / "winkg"
    assert not (root / "ui" / "graph_visualization.py").exists()
    window_source = (root / "graph3d_window.py").read_text(encoding="utf-8")
    gui_source = (root / "gui.py").read_text(encoding="utf-8")
    assert "graph_neighborhood_scene" not in window_source
    assert "def graph_task()" in gui_source
    assert "_run_graph_query" in gui_source
