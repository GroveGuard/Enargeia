"""Native WebView2 window hosting the offline, animated 3D Neo4j graph explorer.

The Flet desktop app runs on Flutter, whose WebView control does not support
Windows. This small companion process hosts the graph in pywebview/WebView2,
while all graph data and follow-up queries still come from the existing Python
and Neo4j layers.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT, get_settings

LOG = logging.getLogger("winkg.graph3d")
HTML_PATH = Path(__file__).parent / "ui" / "graph3d.html"


_NODE_PALETTE = {
    "root": "#55e2c4", "folder": "#3ed7be", "file": "#38dfc2",
    "chunk": "#58aaff", "entity": "#ffb75f", "knowledge": "#b99aff",
    "system": "#f28cd9", "other": "#8ca6bf",
}


def classify_database_node(labels: list[str] | None) -> str:
    """Map Neo4j labels into readable 3D galaxy categories without dropping nodes."""
    label_set = set(str(label) for label in (labels or []))
    direct = {
        "KGRoot": "root", "KGFolder": "folder", "KGFile": "file",
        "KGChunk": "chunk", "KGEntity": "entity",
    }
    for label, kind in direct.items():
        if label in label_set:
            return kind
    if label_set.intersection({
        "KGAssertion", "KGInsight", "KGLinkCandidate", "KGConsolidatedFact",
        "KGCommunity", "KGComponent", "KGContentIdentity", "KGNode",
    }):
        return "knowledge"
    if label_set.intersection({
        "KGAnalysisRun", "KGAnalysisHistorySummary", "KGExtractionCache",
        "KGQualityProfile", "KGQualityDecision",
    }):
        return "system"
    return "other"


def classify_relationship(relation_type: str) -> str:
    """Convert relationship types into a small visual palette while preserving the real type."""
    name = str(relation_type or "").upper()
    if any(token in name for token in ("CONTAIN", "HAS_CHUNK", "IN_ROOT", "PARENT", "CHILD")):
        return "structure"
    if any(token in name for token in ("MENTION", "ENTITY", "CANONICAL")):
        return "entity"
    if any(token in name for token in ("SIMILAR", "SHARED", "RELATED", "NEIGHBOR")):
        return "shared"
    if any(token in name for token in ("ASSERT", "CANDIDATE", "INSIGHT", "FACT", "QUALITY", "COMMUNITY", "COMPONENT")):
        return "knowledge"
    if any(token in name for token in ("CHUNK", "CONTENT", "TEXT", "EXTRACT")):
        return "content"
    return "other"


def build_database_payload(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Convert the complete, unbounded database snapshot into a compact display payload."""
    source_nodes = snapshot.get("nodes") or []
    source_relationships = snapshot.get("relationships") or []
    nodes: list[dict[str, Any]] = []
    nodes_by_id: dict[str, dict[str, Any]] = {}
    counts: dict[str, int] = {}
    for row in source_nodes:
        node_id = str(row.get("id") or "")
        if not node_id or node_id in nodes_by_id:
            continue
        labels = [str(label) for label in (row.get("labels") or [])]
        kind = classify_database_node(labels)
        label = str(row.get("label") or (labels[0] if labels else "Knoten"))
        path = str(row.get("path") or "")
        detail = str(row.get("detail") or "")[:500]
        node = {
            "id": node_id,
            "label": label[:180],
            "kind": kind,
            "labels": labels,
            "detail": detail,
            "path": path[:2000],
            "color": _NODE_PALETTE[kind],
            "degree": 0,
            "isRoot": False,
        }
        nodes.append(node)
        nodes_by_id[node_id] = node
        counts[kind] = counts.get(kind, 0) + 1

    links: list[dict[str, Any]] = []
    for row in source_relationships:
        source, target = str(row.get("source") or ""), str(row.get("target") or "")
        if not source or not target or source not in nodes_by_id or target not in nodes_by_id:
            continue
        raw_type = str(row.get("type") or "RELATIONSHIP")
        kind = classify_relationship(raw_type)
        nodes_by_id[source]["degree"] += 1
        nodes_by_id[target]["degree"] += 1
        links.append({"source": source, "target": target, "kind": kind, "type": raw_type[:120]})

    return {
        "version": str(time.time_ns()),
        "scope": "database",
        "loaded": True,
        "title": "Gesamte Neo4j-Datenbank",
        "root_filter": "",
        "root": {"id": "", "name": "Gesamte Neo4j-Datenbank", "path": ""},
        "nodes": nodes,
        "links": links,
        "stats": {
            "node_count": len(nodes),
            "edge_count": len(links),
            "neighbor_count": len(nodes),
            "roots": counts.get("root", 0),
            "folders": counts.get("folder", 0),
            "files": counts.get("file", 0),
            "chunks": counts.get("chunk", 0),
            "entities": counts.get("entity", 0),
            "knowledge": counts.get("knowledge", 0),
            "system": counts.get("system", 0),
            "other": counts.get("other", 0),
            # Start with no selected node; the user may explore the galaxy freely.
            "selected_node_id": "",
        },
    }


def build_database_loading_payload() -> dict[str, Any]:
    """Payload used while the complete database graph is loaded in the viewer process."""
    return {
        "version": str(time.time_ns()), "scope": "database", "loaded": False,
        "title": "Gesamte Neo4j-Datenbank", "root_filter": "",
        "root": {"id": "", "name": "Gesamte Neo4j-Datenbank", "path": ""},
        "nodes": [], "links": [],
        "stats": {"node_count": 0, "edge_count": 0, "neighbor_count": 0},
    }


def write_graph_payload(path: Path, payload: dict[str, Any]) -> None:
    """Atomically publish a payload to the companion viewer process."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + f".{os.getpid()}.tmp")
    # Keep graph data local; prevent a source text such as </script> from ever
    # becoming executable if the representation is reused in an HTML context.
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    with temp_path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(encoded.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))
    os.replace(temp_path, path)


def read_graph_payload(path: Path) -> dict[str, Any] | None:
    try:
        with Path(path).open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
        return payload if isinstance(payload, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def query_database_snapshot() -> dict[str, Any]:
    """Read the complete Neo4j database's node/link structure for the galaxy view."""
    from .neo4j_store import Neo4jStore

    settings = get_settings()
    settings.validate_neo4j()
    store = Neo4jStore(settings)
    try:
        return store.graph_snapshot()
    finally:
        store.close()


class GraphWindowApi:
    """Narrow pywebview bridge; all graph queries remain parameterized in Neo4jStore."""

    def __init__(self, payload_path: Path):
        self._payload_path = Path(payload_path).resolve()
        self._payload_mtime_ns = -1
        self._cached_payload: dict[str, Any] | None = None

    def get_graph_update(self, current_version: str = "") -> dict[str, Any] | None:
        try:
            mtime_ns = self._payload_path.stat().st_mtime_ns
        except OSError:
            return None
        if mtime_ns != self._payload_mtime_ns:
            self._cached_payload = read_graph_payload(self._payload_path)
            self._payload_mtime_ns = mtime_ns
        payload = self._cached_payload
        if not payload or str(payload.get("version") or "") == str(current_version or ""):
            return None
        return payload

    def load_database_graph(self) -> dict[str, Any]:
        """Load every Neo4j node and relationship, replacing the initial loading payload."""
        try:
            snapshot = query_database_snapshot()
            payload = build_database_payload(snapshot)
            write_graph_payload(self._payload_path, payload)
            # Refresh this API instance's cache so the next poll sees the new version.
            try:
                self._payload_mtime_ns = self._payload_path.stat().st_mtime_ns
                self._cached_payload = payload
            except OSError:
                pass
            return {"ok": True, "version": payload["version"], "stats": payload["stats"]}
        except Exception as exc:
            LOG.exception("Vollständiger Neo4j-Graph konnte nicht geladen werden")
            return {"ok": False, "error": f"Gesamter Graph konnte nicht geladen werden: {exc}"}

    def refresh_graph(self) -> dict[str, Any]:
        """Refresh the entire database galaxy from Neo4j."""
        return self.load_database_graph()

    def open_in_explorer(self, raw_path: str) -> dict[str, Any]:
        """Open a selected path using Windows Explorer without mutating the graph."""
        raw_path = str(raw_path or "").strip()
        if not raw_path:
            return {"ok": False, "error": "Für diesen Knoten ist kein Dateipfad hinterlegt."}
        target = Path(raw_path).expanduser()
        if not target.exists():
            return {"ok": False, "error": f"Der Originalpfad ist nicht erreichbar: {target}"}
        try:
            if os.name == "nt":
                if target.is_dir():
                    subprocess.Popen(["explorer.exe", str(target)])
                else:
                    subprocess.Popen(["explorer.exe", "/select,", str(target)])
                return {"ok": True}
            return {"ok": False, "error": "Der Explorer-Shortcut ist nur unter Windows verfügbar."}
        except Exception as exc:
            LOG.exception("Originalpfad konnte nicht im Explorer geöffnet werden")
            return {"ok": False, "error": str(exc)}



def main() -> int:
    parser = argparse.ArgumentParser(description="Neo4j Company Knowledge Graph 3D viewer")
    parser.add_argument("--payload", required=True, help="Temporary JSON graph payload shared with the parent GUI")
    args = parser.parse_args()
    payload_path = Path(args.payload).resolve()
    if not HTML_PATH.is_file():
        raise FileNotFoundError(f"3D-Ansicht fehlt: {HTML_PATH}")
    if not read_graph_payload(payload_path):
        raise ValueError(f"3D-Graphdaten konnten nicht gelesen werden: {payload_path}")

    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=log_dir / "winkg-graph3d.log",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        encoding="utf-8",
    )

    try:
        import webview  # optional GUI dependency, installed with the [gui] extra

        api = GraphWindowApi(payload_path)
        webview.create_window(
            "Company Knowledge Graph · 3D Explorer",
            html=HTML_PATH.read_text(encoding="utf-8"),
            js_api=api,
            width=1600,
            height=980,
            min_size=(1050, 700),
            background_color="#050b14",
            text_select=True,
        )
        webview.start(gui="edgechromium" if os.name == "nt" else None, debug=False)
        return 0
    except Exception as exc:
        LOG.exception("3D-Graph-Fenster konnte nicht gestartet werden")
        if os.name == "nt":
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(
                    None,
                    "Die 3D-Graphansicht konnte nicht geöffnet werden.\n\n"
                    "Bitte prüfe, ob pywebview und die Microsoft Edge WebView2 Runtime installiert sind.\n"
                    "Details stehen in logs\\winkg-graph3d.log.\n\n" + str(exc),
                    "Company Knowledge Graph – 3D-Ansicht",
                    0x10,
                )
            except Exception:
                pass
        return 1
    finally:
        try:
            payload_path.unlink(missing_ok=True)
        except OSError:
            LOG.warning("Temporäre Graphdaten konnten nicht entfernt werden: %s", payload_path)


if __name__ == "__main__":
    sys.exit(main())
