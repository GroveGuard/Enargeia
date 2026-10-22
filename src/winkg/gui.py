from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import queue
import subprocess
import sys
import tempfile
import uuid
from logging.handlers import RotatingFileHandler
import threading
from pathlib import Path
from typing import Any, Callable

import flet as ft

from . import __version__
from .branding import image_as_base64
from .config import IS_FROZEN, LOG_DIR, PROJECT_ROOT, get_settings
from .embeddings import EmbeddingClient
from .graph_intelligence import GraphIntelligence
from .indexer import DirectoryIndexer
from .neo4j_store import Neo4jStore

from .ui.components import (
    card as _card, section_title as _section_title, eyebrow as _eyebrow,
    status_pill as _status_pill, icon_tile as _icon_tile,
)
from .graph3d_window import build_database_loading_payload, write_graph_payload
from .ui.theme import (
    ACCENT,
    ACCENT_SOFT,
    APP_NAME,
    BLUE,
    BAD,
    BG,
    BORDER,
    HEADER,
    GOOD,
    MUTED,
    PANEL,
    PANEL_ALT,
    PANEL_RAISED,
    PURPLE,
    SIDEBAR,
    SUBTLE,
    TEXT,
    WARN,
)


def _safe_icon(name: str) -> Any:
    """Return an icon available in the installed Flet/Material icon catalogue."""
    try:
        return getattr(ft.Icons, name)
    except AttributeError:
        return ft.Icons.HUB_OUTLINED


def _configure_logging() -> Path:
    """Write application and scan diagnostics to a rotating project-local log."""
    log_dir = LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "enargeia-gui.log"
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    # Avoid duplicate handlers if main() is invoked more than once in a process.
    if not any(getattr(handler, "_winkg_file_handler", False) for handler in root_logger.handlers):
        handler = RotatingFileHandler(
            log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        handler._winkg_file_handler = True  # type: ignore[attr-defined]
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s [%(name)s] %(threadName)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        root_logger.addHandler(handler)
    logging.getLogger("winkg").setLevel(logging.INFO)
    logging.getLogger(__name__).info("GUI-Prozess gestartet; Logdatei: %s", log_path)
    return log_path


def _settings_preview() -> dict[str, str]:
    settings = get_settings()
    return {
        "Neo4j URI": settings.neo4j_uri,
        "Benutzer": settings.neo4j_username,
        "Datenbank": settings.neo4j_database,
        "Embedding-Provider": settings.embedding_provider,
        "Embedding-Modell": settings.embedding_model,
        "Embedding-Endpunkt": settings.embedding_base_url,
        "GDS max. Entity-Knoten": str(settings.gds_max_entity_nodes),
        "GDS max. Node-Similarity-Knoten": str(settings.gds_max_node_similarity_nodes),
        "GDS max. KNN-Textabschnitte": str(settings.gds_max_knn_chunks),
        "Max. Chunks je Datei": str(settings.max_chunks_per_file),
        "Max. Dateigröße (MB)": str(settings.max_file_size_mb),
        "LLM-Modell für Extraktion": settings.llm_model,
        "LLM-Endpunkt für Extraktion": settings.llm_base_url,
    }


def _root_id_from_path(store: Neo4jStore, value: str | None) -> str | None:
    if not value or not value.strip():
        return None
    return store.root_id_for(Path(value.strip()).expanduser().resolve())


def _load_dashboard_data() -> tuple[dict[str, int], list[dict[str, Any]]]:
    settings = get_settings()
    settings.validate_neo4j()
    store = Neo4jStore(settings)
    try:
        return store.stats(), store.list_roots()
    finally:
        store.close()


def _show_dialog_compat(page: Any, dialog: Any) -> None:
    """Support both current Flet dialogs and older Page.open() versions."""
    show_dialog = getattr(page, "show_dialog", None)
    if callable(show_dialog):
        show_dialog(dialog)
        return
    open_dialog = getattr(page, "open", None)
    if callable(open_dialog):
        open_dialog(dialog)
        return
    dialog.open = True
    page.update()


def _close_dialog_compat(page: Any, dialog: Any) -> None:
    """Close a dialog across Flet versions without depending on one API name."""
    pop_dialog = getattr(page, "pop_dialog", None)
    if callable(pop_dialog):
        pop_dialog()
        return
    close_dialog = getattr(page, "close", None)
    if callable(close_dialog):
        close_dialog(dialog)
        return
    dialog.open = False
    page.update()


def _run_delete_root(root_path: str) -> dict[str, Any]:
    """Remove one indexed source root from Neo4j; never touch filesystem files."""
    path = Path(root_path).expanduser().resolve()
    settings = get_settings()
    settings.validate_neo4j()
    store = Neo4jStore(settings)
    try:
        root_id = store.root_id_for(path)
        existing = next((root for root in store.list_roots() if root.get("id") == root_id), None)
        if existing is None:
            raise ValueError(f"Der Ordner ist nicht als Root im Knowledge Graph registriert: {path}")
        store.delete_root(root_id)
        logging.getLogger("winkg.gui").info("Indexierten Root aus Neo4j entfernt: %s (%s)", path, root_id)
        return {"path": str(path), "root_id": root_id}
    finally:
        store.close()


def _run_doctor(
    test_embeddings: bool,
    embedding_progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    settings.validate_neo4j()
    store = Neo4jStore(settings)
    try:
        stats = store.stats()
    finally:
        store.close()
    result: dict[str, Any] = {
        "neo4j": "OK",
        "uri": settings.neo4j_uri,
        "database": settings.neo4j_database,
        "stats": stats,
    }
    if test_embeddings and settings.embedding_provider != "none":
        client = EmbeddingClient(settings, progress_callback=embedding_progress_callback)
        vector = client.embed_one("Verbindungstest für den Knowledge Graph")
        result["embedding"] = {
            "status": "OK",
            "model": client.signature,
            "dimensions": len(vector),
        }
    elif test_embeddings:
        result["embedding"] = {
            "status": "DEAKTIVIERT",
            "model": "none",
            "message": "Semantische Vektorsuche ist deaktiviert; Volltext- und Graphfunktionen bleiben verfügbar.",
        }
    return result


def _run_graph_query(query: str, root_path: str) -> dict[str, Any] | None:
    settings = get_settings()
    settings.validate_neo4j()
    store = Neo4jStore(settings)
    try:
        root_id = _root_id_from_path(store, root_path)
        return store.graph_neighborhood(query, root_id=root_id, limit=30)
    finally:
        store.close()


def _run_graph_capabilities() -> dict[str, Any]:
    settings = get_settings()
    settings.validate_neo4j()
    store = Neo4jStore(settings)
    try:
        return GraphIntelligence(store).capabilities()
    finally:
        store.close()


def _run_graph_analysis(run_gds: bool) -> dict[str, Any]:
    settings = get_settings()
    settings.validate_neo4j()
    store = Neo4jStore(settings)
    try:
        return GraphIntelligence(store).analyze(run_gds=(run_gds and not (settings.neo4j_mode == "local" and not settings.gds_enabled)))
    finally:
        store.close()


def _run_graph_link_candidates(status: str | None = "proposed") -> list[dict[str, Any]]:
    settings = get_settings()
    settings.validate_neo4j()
    store = Neo4jStore(settings)
    try:
        return GraphIntelligence(store).list_link_candidates(status=status, limit=100)
    finally:
        store.close()


def _run_review_graph_link_candidate(candidate_id: str, decision: str, note: str = "") -> dict[str, Any]:
    settings = get_settings()
    settings.validate_neo4j()
    store = Neo4jStore(settings)
    try:
        return GraphIntelligence(store).review_link_candidate(candidate_id, decision, note)
    finally:
        store.close()




def main(page: ft.Page) -> None:
    _configure_logging()
    page.title = APP_NAME
    page.theme_mode = ft.ThemeMode.DARK
    page.theme = ft.Theme(color_scheme_seed=ACCENT)
    page.bgcolor = BG
    page.padding = 0
    page.spacing = 0
    try:
        page.window.width = 1500
        page.window.height = 940
        page.window.min_width = 1120
        page.window.min_height = 720
    except Exception:
        pass

    scan_log_queue: queue.Queue[str] = queue.Queue()
    scan_state: dict[str, Any] = {"running": False, "result": None, "error": None, "cancel_event": None}
    embedding_progress_lock = threading.Lock()
    embedding_progress_state: dict[str, Any] = {"generation": 0, "phase": "idle"}
    embedding_progress_ui_generation = {"value": -1}

    def publish_embedding_progress(event: dict[str, Any]) -> None:
        """Thread-safe event bridge: model download worker -> Flet event loop."""
        with embedding_progress_lock:
            generation = int(embedding_progress_state.get("generation", 0)) + 1
            embedding_progress_state.clear()
            embedding_progress_state.update(event)
            embedding_progress_state["generation"] = generation

    def reset_embedding_progress() -> None:
        publish_embedding_progress({"phase": "idle", "message": "Kein Modell-Download aktiv."})
    graph3d_process: subprocess.Popen[Any] | None = None
    graph3d_payload_path: Path | None = None

    # ---------------- Shared controls ----------------
    header_title = ft.Text("Übersicht", size=23, weight=ft.FontWeight.BOLD, color=TEXT)
    header_subtitle = ft.Text("Graph aufbauen, Qualität sichern und Struktur analysieren", size=12, color=MUTED)
    global_status = ft.Text("Verbindung noch nicht geprüft", size=12, color=MUTED)
    view_host = ft.Container(expand=True, padding=22, bgcolor=BG)

    stat_values: dict[str, ft.Text] = {}
    for key in ("roots", "folders", "files", "chunks", "entities", "assertions", "truncated_files", "insights", "link_candidates", "communities", "components", "semantic_similarity_edges"):
        stat_values[key] = ft.Text("–", size=26, weight=ft.FontWeight.BOLD, color=TEXT)

    overview_message = ft.Text("Graph-Bestand wird beim Start geladen.", size=12, color=MUTED, expand=True, max_lines=2)
    quality_stat_values = {
        "truncated_files": ft.Text("–", size=19, weight=ft.FontWeight.W_600, color=TEXT),
        "link_candidates": ft.Text("–", size=19, weight=ft.FontWeight.W_600, color=TEXT),
    }
    source_root_list = ft.Column(spacing=8, controls=[])
    source_registry_status = ft.Text("Root-Registry wird geladen …", size=11, color=MUTED)

    directory_field = ft.TextField(
        label="Windows-Verzeichnis",
        hint_text=r"z. B. C:\MS_VSCode_Workspace\Fletapps_HA",
        value="",
        expand=True,
        text_size=13,
        border=ft.OutlineInputBorder(border_radius=10),
    )
    graph_root_filter = ft.TextField(
        label="Root eingrenzen (optional)",
        hint_text="Leer lassen, um alle indexierten Roots einzubeziehen",
        expand=True,
        text_size=12,
    )
    graph_query = ft.TextField(
        label="Dateiname oder Pfad",
        hint_text="z. B. main.py oder src/winkg/indexer.py",
        expand=True,
        on_submit=lambda e: page.run_task(graph_task),
    )
    force_checkbox = ft.Checkbox(label="Alle Dateien erneut verarbeiten", value=False)
    extract_entities_checkbox = ft.Checkbox(label="Entitäten und Beziehungen per Chat-LLM extrahieren", value=False)
    no_embeddings_checkbox = ft.Checkbox(label="Ohne Embeddings (nur Graph/Volltext)", value=False)
    include_ignored_checkbox = ft.Checkbox(label="Standardmäßig ausgeschlossene Ordner einbeziehen", value=False)
    include_sensitive_checkbox = ft.Checkbox(label="Sensible Dateien ausdrücklich einschließen", value=False)
    rebuild_confirm_checkbox = ft.Checkbox(label="Ich bestätige, dass der Graph dieses Roots gelöscht und neu aufgebaut wird.", value=False)

    scan_status = ft.Text("Bereit für den Scan.", size=12, color=MUTED)
    scan_progress = ft.ProgressRing(width=18, height=18, visible=False, stroke_width=2)
    scan_embedding_progress_text = ft.Text("Modell-Download wartet auf den ersten lokalen Embedding-Aufruf.", size=10, color=MUTED, selectable=True)
    scan_embedding_progress_bar = ft.ProgressBar(value=None, visible=True, color=ACCENT, bgcolor=BORDER, bar_height=6)
    _loading_image_b64 = image_as_base64("Enargeia_Loading_Indicator.png")
    scan_embedding_progress_panel = ft.Container(
        visible=False,
        content=ft.Row(controls=[
            ft.Image(src=_loading_image_b64, width=32, height=32, fit=ft.BoxFit.CONTAIN),
            ft.Column(controls=[scan_embedding_progress_text, scan_embedding_progress_bar], spacing=6, expand=True),
        ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        padding=11, bgcolor=PANEL_ALT, border_radius=9,
    )
    scan_start_button: ft.Button
    scan_rebuild_button: ft.Button
    scan_log = ft.ListView(expand=True, spacing=4, auto_scroll=True, controls=[ft.Text("Noch keine Scan-Aktivität. Starte einen Scan, um Live-Protokolle zu sehen.", size=11, color=MUTED)])

    graph_status = ft.Text("Suche nach Dateien und gespeicherten Graph-Beziehungen.", size=12, color=MUTED)
    galaxy_status = ft.Text("Bereit · vollständige Neo4j-Datenbank wird beim Öffnen geladen.", size=11, color=MUTED)
    graph_results = ft.Column(spacing=12, controls=[ft.Container(content=ft.Text("Wähle eine Datei oder einen Pfad, um die gespeicherten Graph-Beziehungen zu untersuchen.", size=12, color=MUTED), padding=18, bgcolor=PANEL, border=ft.Border.all(1, BORDER), border_radius=10)], expand=False)
    connection_status = ft.Text("Noch nicht geprüft", size=13, color=MUTED)
    connection_details = ft.Column(spacing=7, controls=[ft.Container(content=ft.Text("Starte den Systemtest, um Verbindungsdetails und Laufzeitdiagnose zu laden.", size=11, color=MUTED), padding=12, bgcolor=PANEL_ALT, border_radius=8)], expand=False)
    connection_embedding_progress_text = ft.Text("Modell-Download wartet auf den ersten lokalen Embedding-Aufruf.", size=10, color=MUTED, selectable=True)
    connection_embedding_progress_bar = ft.ProgressBar(value=None, visible=True, color=ACCENT, bgcolor=BORDER, bar_height=6)
    connection_embedding_progress_panel = ft.Container(
        visible=False,
        content=ft.Row(controls=[
            ft.Image(src=_loading_image_b64, width=32, height=32, fit=ft.BoxFit.CONTAIN),
            ft.Column(controls=[connection_embedding_progress_text, connection_embedding_progress_bar], spacing=6, expand=True),
        ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        padding=11, bgcolor=PANEL_ALT, border_radius=9,
    )
    graph_intelligence_status = ft.Text("Graphanalyse wurde noch nicht ausgeführt.", size=12, color=MUTED)
    graph_intelligence_output = ft.Column(spacing=9, controls=[ft.Container(content=ft.Text("Noch keine Analyseergebnisse. Führe zuerst eine Graphanalyse aus.", size=12, color=MUTED), padding=16, bgcolor=PANEL_ALT, border_radius=8)], expand=False)
    quality_status = ft.Text("Beziehungsvorschläge warten auf Prüfung.", size=12, color=MUTED)
    quality_output = ft.Column(spacing=9, controls=[ft.Container(content=ft.Text("Beziehungsvorschläge werden hier nach dem Laden angezeigt.", size=12, color=MUTED), padding=16, bgcolor=PANEL_ALT, border_radius=8)], expand=False)
    gds_analysis_checkbox = ft.Checkbox(label="GDS-Algorithmen zusätzlich ausführen (sofern installiert)", value=True)

    # ---------------- Utility rendering ----------------
    def _format_embedding_progress(event: dict[str, Any]) -> tuple[str, float | None, bool]:
        phase = str(event.get("phase") or "idle")
        current = max(0, int(event.get("current_bytes") or 0))
        total = max(0, int(event.get("total_bytes") or 0))
        if phase == "idle":
            return "", None, False
        if phase == "preparing":
            return str(event.get("message") or "Pruefe Modellcache und bereite Download vor ..."), None, True
        if phase == "download":
            current_mib = current / (1024 * 1024)
            if total > 0:
                total_mib = total / (1024 * 1024)
                ratio = min(1.0, current / total)
                return f"{event.get('description') or 'Modell herunterladen'}: {current_mib:.1f} / {total_mib:.1f} MiB ({ratio:.0%})", ratio, True
            return f"Modell-Dateien werden heruntergeladen: {current_mib:.1f} MiB empfangen ...", None, True
        if phase == "testing":
            return str(event.get("message") or "Initialisiere ONNX Runtime und teste ein Embedding ..."), None, True
        if phase == "complete":
            dimensions = event.get("dimensions")
            suffix = f" ({dimensions} Dimensionen)" if dimensions else ""
            return str(event.get("message") or f"Lokales Embedding-Modell ist bereit{suffix}."), 1.0, True
        if phase == "error":
            return str(event.get("message") or "Modell-Download fehlgeschlagen."), 0.0, True
        return str(event.get("message") or "Lokales Embedding-Modell wird vorbereitet ..."), None, True

    def refresh_embedding_progress_controls() -> bool:
        with embedding_progress_lock:
            event = dict(embedding_progress_state)
        generation = int(event.get("generation", 0))
        if generation == embedding_progress_ui_generation["value"]:
            return False
        embedding_progress_ui_generation["value"] = generation
        text_value, progress_value, visible = _format_embedding_progress(event)
        for label, bar, panel in (
            (scan_embedding_progress_text, scan_embedding_progress_bar, scan_embedding_progress_panel),
            (connection_embedding_progress_text, connection_embedding_progress_bar, connection_embedding_progress_panel),
        ):
            label.value = text_value or "Modell-Download wartet auf den ersten lokalen Embedding-Aufruf."
            label.color = BAD if event.get("phase") == "error" else (GOOD if event.get("phase") == "complete" else MUTED)
            bar.value = progress_value
            panel.visible = visible
        return True

    def open_graph3d_viewer(data: dict[str, Any], root_filter: str = "") -> tuple[bool, str]:
        """Open or update the native WebView2 3D viewer without blocking Flet."""
        nonlocal graph3d_process, graph3d_payload_path
        if IS_FROZEN:
            companion = Path(sys.executable).parent / "Enargeia3D" / "Enargeia3D.exe"
            if not companion.is_file():
                companion = Path(sys.executable).with_name("Enargeia3D.exe")
            if not companion.is_file():
                return False, f"Die 3D-Komponente fehlt: {companion}"
        elif importlib.util.find_spec("webview") is None:
            return False, "3D-Engine fehlt. Installiere für die Entwicklung die optionale GUI-Abhängigkeit pywebview."
        try:
            # The 3D window always represents the whole Neo4j database, not a selected-file neighborhood.
            payload = build_database_loading_payload()
            if graph3d_process is not None and graph3d_process.poll() is None and graph3d_payload_path is not None:
                write_graph_payload(graph3d_payload_path, payload)
                return True, "Die geöffnete 3D-Ansicht wurde aktualisiert."

            if graph3d_payload_path is not None:
                try:
                    graph3d_payload_path.unlink(missing_ok=True)
                except OSError:
                    pass
            graph3d_payload_path = Path(tempfile.gettempdir()) / f"winkg_graph3d_{os.getpid()}_{uuid.uuid4().hex}.json"
            write_graph_payload(graph3d_payload_path, payload)

            if IS_FROZEN:
                # The packaged application uses a small dedicated companion executable;
                # invoking the main EXE with ``-m`` would not work under PyInstaller.
                companion = Path(sys.executable).parent / "Enargeia3D" / "Enargeia3D.exe"
                if not companion.is_file():
                    companion = Path(sys.executable).with_name("Enargeia3D.exe")
                if not companion.is_file():
                    return False, f"Die 3D-Komponente fehlt: {companion}"
                command = [str(companion), "--payload", str(graph3d_payload_path)]
            else:
                python_executable = sys.executable
                if os.name == "nt":
                    pythonw = Path(sys.executable).with_name("pythonw.exe")
                    if pythonw.is_file():
                        python_executable = str(pythonw)
                command = [python_executable, "-m", "winkg.graph3d_window", "--payload", str(graph3d_payload_path)]
            graph3d_process = subprocess.Popen(
                command,
                cwd=str(PROJECT_ROOT),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            logging.getLogger(__name__).info("3D-Graphfenster gestartet (PID %s)", graph3d_process.pid)
            return True, "Die animierte 3D-Graphansicht wurde in einem eigenen Desktopfenster geöffnet."
        except Exception as exc:
            logging.getLogger(__name__).exception("3D-Graphfenster konnte nicht geöffnet werden")
            return False, f"3D-Ansicht konnte nicht geöffnet werden: {exc}"

    def graph3d_launch_panel() -> ft.Control:
        """Prominent, independent launcher for the full-database 3D galaxy."""
        return _card(ft.Row(controls=[
            _icon_tile(_safe_icon("HUB_OUTLINED"), ACCENT, size=48),
            ft.Column(controls=[
                _eyebrow("LIVE 3D GALAXY · GESAMTER GRAPH"),
                ft.Text("Die Wissensgalaxie live erleben", size=17, weight=ft.FontWeight.BOLD, color=TEXT),
                ft.Text("Alle Neo4j-Knoten und Beziehungen in einer animierten 3D-Ansicht. Unabhängig von der SVG-Suche darunter.", size=11, color=MUTED),
                galaxy_status,
            ], spacing=4, expand=True),
            ft.Button(content="Live-3D-Galaxie öffnen", icon=_safe_icon("OPEN_IN_NEW_ROUNDED"), on_click=launch_graph3d_from_button),
        ], spacing=14, vertical_alignment=ft.CrossAxisAlignment.CENTER), padding=18, bgcolor="#0B1C36", border_color="#1A3554")

    def launch_graph3d_from_button(_: Any = None) -> None:
        opened, message = open_graph3d_viewer({}, "")
        galaxy_status.value = message
        galaxy_status.color = GOOD if opened else WARN
        page.update()

    def add_notice(target: ft.Column, message: str, color: str = MUTED) -> None:
        target.controls.clear()
        target.controls.append(_card(ft.Text(message, color=color, size=13)))

    def stat_card(key: str, label: str, note: str) -> ft.Container:
        icon_map = {
            "roots": (_safe_icon("FOLDER_SPECIAL_OUTLINED"), BLUE),
            "folders": (ft.Icons.FOLDER_OPEN_OUTLINED, BLUE),
            "files": (_safe_icon("DRAFT_OUTLINED"), ACCENT),
            "chunks": (_safe_icon("COPY_ALL_OUTLINED"), WARN),
            "entities": (ft.Icons.HUB_OUTLINED, PURPLE),
            "assertions": (_safe_icon("VERIFIED_OUTLINED"), GOOD),
            "truncated_files": (_safe_icon("CONTENT_CUT_OUTLINED"), WARN),
            "insights": (_safe_icon("RULE_OUTLINED"), PURPLE),
            "link_candidates": (ft.Icons.LINK_OUTLINED, WARN),
            "communities": (_safe_icon("BUBBLE_CHART_OUTLINED"), BLUE),
            "components": (ft.Icons.ACCOUNT_TREE_OUTLINED, ACCENT),
            "semantic_similarity_edges": (ft.Icons.HUB_OUTLINED, PURPLE),
        }
        icon, color = icon_map.get(key, (_safe_icon("ANALYTICS_OUTLINED"), ACCENT))
        return _card(
            ft.Column(
                controls=[
                    ft.Row(
                        controls=[ft.Text(label, size=11, color=MUTED, expand=True), _icon_tile(icon, color, size=34)],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Container(content=stat_values[key], padding=ft.Padding.only(top=4, bottom=1)),
                    ft.Text(note, size=10, color=SUBTLE),
                ],
                spacing=7,
            ),
            padding=15,
            bgcolor=PANEL,
            width=235,
            border_color=BORDER,
            radius=14,
        )

    def status_color_for_status(value: str) -> str:
        normalized = value.casefold()
        if any(part in normalized for part in ("error", "fehlgeschlagen", "nicht erreichbar", "fehler")):
            return BAD
        if any(part in normalized for part in ("ok", "verbunden", "abgeschlossen", "fertig")):
            return GOOD
        if any(part in normalized for part in ("läuft", "prüfe", "verarbeite")):
            return WARN
        return MUTED

    def node_card(title: str, subtitle: str, kind: str, *, width: int = 235, on_open: Callable[[], None] | None = None) -> ft.Container:
        details: list[ft.Control] = [
            ft.Text(kind.upper(), size=9, color=ACCENT, weight=ft.FontWeight.BOLD),
            ft.Text(title or "(ohne Namen)", size=12, weight=ft.FontWeight.W_600, color=TEXT),
            ft.Text(subtitle or "", size=10, color=MUTED, selectable=True),
        ]
        if on_open:
            details.append(ft.Button(content="Öffnen", on_click=lambda e: on_open()))
        return _card(ft.Column(controls=details, spacing=6), padding=12, bgcolor=PANEL_ALT, width=width)

    def relation_section(label: str, relation: str, nodes: list[ft.Control], empty: str) -> ft.Control:
        if nodes:
            content: ft.Control = ft.Row(controls=nodes, wrap=True, spacing=10, run_spacing=10, vertical_alignment=ft.CrossAxisAlignment.START)
        else:
            content = ft.Text(empty, size=11, color=MUTED)
        return ft.Column(
            controls=[
                ft.Row(
                    controls=[ft.Text(label, size=13, weight=ft.FontWeight.W_600, color=TEXT), ft.Text(f"— {relation} →", size=11, color=ACCENT)],
                    spacing=8,
                ),
                content,
            ],
            spacing=9,
        )

    # ---------------- Dashboard ----------------
    def build_dashboard_view() -> ft.Control:
        """Dashboard: concise status and quality, without duplicate navigation links."""
        hero = _card(
            ft.Column(
                controls=[
                    _eyebrow(f"GRAPH OPERATIONS CENTER  /  V{__version__}"),
                    ft.Text("Dein Unternehmenswissen. Als Graph strukturiert.", size=25, weight=ft.FontWeight.BOLD, color=TEXT),
                    ft.Text(
                        "Indexierung, Datenabdeckung und Beziehungsqualität im Blick. "
                        "Inhaltliche Fragen werden von externen LLMs über MCP beantwortet.",
                        size=12, color=MUTED,
                    ),
                ],
                spacing=8,
            ),
            padding=22,
            bgcolor="#0B1C36",
            border_color="#1A3554",
            radius=18,
        )

        primary_stats = ft.Row(
            controls=[
                stat_card("files", "Indexierte Dateien", "Erfasste Dateiknoten"),
                stat_card("chunks", "Textabschnitte", "Gespeicherte Quelleninhalte"),
                stat_card("entities", "Entitäten", "Semantische Knoten im Graph"),
                stat_card("roots", "Datenquellen", "Indexierte Root-Verzeichnisse"),
            ],
            wrap=True,
            spacing=12,
            run_spacing=12,
        )

        operation_status = ft.Row(
            controls=[ft.Icon(_safe_icon("INFO_OUTLINE"), size=15, color=ACCENT), overview_message],
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        coverage_panel = _card(
            ft.Column(
                controls=[
                    ft.Row(
                        controls=[
                            ft.Column(
                                controls=[
                                    _eyebrow("DATA QUALITY"),
                                    ft.Text("Abdeckung & Verlässlichkeit", size=17, weight=ft.FontWeight.W_600, color=TEXT),
                                    ft.Text("Konkrete Qualitätsindikatoren aus dem Graphen.", size=11, color=MUTED),
                                ],
                                spacing=4,
                                expand=True,
                            ),
                            _status_pill("Evidenzbasiert", GOOD),
                        ],
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        spacing=12,
                    ),
                    ft.Divider(color=BORDER),
                    ft.Row(
                        controls=[
                            ft.Container(
                                content=ft.Row(
                                    controls=[
                                        _icon_tile(_safe_icon("CONTENT_CUT_OUTLINED"), WARN, size=38),
                                        ft.Column(controls=[
                                            ft.Text("Gekürzter Inhalt", size=12, color=TEXT),
                                            ft.Text("Dateien für eine spätere Analyse markiert", size=10, color=MUTED),
                                        ], spacing=4, expand=True),
                                        quality_stat_values["truncated_files"],
                                    ],
                                    spacing=10,
                                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                ),
                                padding=12,
                                bgcolor=PANEL_ALT,
                                border_radius=11,
                                expand=True,
                            ),
                            ft.Container(
                                content=ft.Row(
                                    controls=[
                                        _icon_tile(ft.Icons.LINK_OUTLINED, PURPLE, size=38),
                                        ft.Column(controls=[
                                            ft.Text("Offene Beziehungen", size=12, color=TEXT),
                                            ft.Text("Hypothesen, die noch geprüft werden müssen", size=10, color=MUTED),
                                        ], spacing=4, expand=True),
                                        quality_stat_values["link_candidates"],
                                    ],
                                    spacing=10,
                                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                ),
                                padding=12,
                                bgcolor=PANEL_ALT,
                                border_radius=11,
                                expand=True,
                            ),
                        ],
                        spacing=12,
                        vertical_alignment=ft.CrossAxisAlignment.STRETCH,
                    ),
                    ft.Row(
                        controls=[
                            ft.Icon(_safe_icon("INFO_OUTLINE"), size=15, color=SUBTLE),
                            ft.Text(
                                "Vorschläge bleiben getrennt von bestätigten Fakten; Quellen und Evidenz sind maßgeblich.",
                                size=10,
                                color=MUTED,
                            ),
                        ],
                        spacing=8,
                    ),
                ],
                spacing=13,
            ),
            padding=18,
            bgcolor=PANEL,
            border_color=BORDER,
        )

        graph_stats = ft.Row(
            controls=[
                stat_card("folders", "Ordner", "Strukturknoten"),
                stat_card("assertions", "Belegte Aussagen", "Quellgestützte Fakten"),
                stat_card("communities", "Communities", "Graph-Gruppierungen"),
                stat_card("components", "Komponenten", "Zusammenhängende Bereiche"),
                stat_card("insights", "Analysebefunde", "Gespeicherte Einsichten"),
                stat_card("semantic_similarity_edges", "Ähnlichkeitskanten", "GDS-Ähnlichkeiten zwischen Chunks"),
            ],
            wrap=True,
            spacing=10,
            run_spacing=10,
        )

        # Flet's ExpansionTile was not visibly discoverable in the deployed desktop
        # view. Use an explicit, full-width disclosure row with a clear action label.
        # Keep the statistics controls mounted and toggle visibility without rebuilding
        # the dashboard or detaching controls from their parent.
        graph_stats.visible = False

        def toggle_graph_stats(_: Any) -> None:
            graph_stats.visible = not bool(graph_stats.visible)
            graph_stats_toggle.content = ft.Row(
                controls=[
                    ft.Icon(
                        ft.Icons.KEYBOARD_ARROW_UP if graph_stats.visible else ft.Icons.KEYBOARD_ARROW_DOWN,
                        color=ACCENT,
                        size=17,
                    ),
                    ft.Text(
                        "Statistik ausblenden" if graph_stats.visible else "Erweiterte Statistik anzeigen",
                        size=11,
                        weight=ft.FontWeight.W_600,
                        color=TEXT,
                    ),
                ],
                spacing=5,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )
            page.update()

        # Put the disclosure beside the section label, above every status message.
        # This keeps it visible in the initial viewport and avoids hiding it at the
        # bottom of a long dashboard.
        graph_stats_toggle = ft.Container(
            content=ft.Row(
                controls=[
                    ft.Icon(ft.Icons.KEYBOARD_ARROW_DOWN, color=ACCENT, size=17),
                    ft.Text("Erweiterte Statistik anzeigen", size=11, weight=ft.FontWeight.W_600, color=TEXT),
                ],
                spacing=5,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.Padding.symmetric(horizontal=10, vertical=7),
            bgcolor=PANEL_ALT,
            border=ft.Border.all(1, BORDER),
            border_radius=9,
            ink=True,
            tooltip="Zusätzliche Graph-Kennzahlen ein- oder ausblenden",
            on_click=toggle_graph_stats,
        )

        # A non-scrollable, viewport-sized Column is intentional here: the Overview
        # has a small, finite set of panels and should not create a second scrollable
        # surface. Other long pages own their own explicit scroll containers.
        return ft.Column(
            controls=[
                hero,
                ft.Row(
                    controls=[
                        ft.Text("GRAPH AT A GLANCE", size=10, weight=ft.FontWeight.BOLD, color=SUBTLE),
                        ft.Container(expand=True),
                        graph_stats_toggle,
                        _status_pill("Live-Werte aus Neo4j", ACCENT),
                    ],
                    spacing=10,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                primary_stats,
                graph_stats,
                operation_status,
                coverage_panel,
            ],
            spacing=15,
            expand=True,
        )

    # ---------------- Scan view ----------------
    def pick_directory(_: Any) -> None:
        # The scanner reads server-side paths. For the intended local Windows GUI,
        # use the native OS picker rather than a browser upload picker (which cannot
        # expose arbitrary local paths to the application process).
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            try:
                root.attributes("-topmost", True)
            except Exception:
                pass
            selected = filedialog.askdirectory(title="Verzeichnis für den Knowledge Graph auswählen")
            root.destroy()
            if selected:
                directory_field.value = selected
                page.update()
        except Exception as exc:
            scan_status.value = f"Ordnerdialog nicht verfügbar: {exc}. Pfad bitte manuell eintragen."
            scan_status.color = WARN
            page.update()

    class QueueLogHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            try:
                scan_log_queue.put_nowait(self.format(record))
            except Exception:
                pass

    def start_scan(rebuild: bool = False) -> None:
        if scan_state["running"]:
            return
        raw_path = (directory_field.value or "").strip()
        if not raw_path:
            scan_status.value = "Bitte zuerst ein Verzeichnis angeben."
            scan_status.color = BAD
            page.update()
            return
        root_path = Path(raw_path).expanduser()
        if not root_path.exists() or not root_path.is_dir():
            scan_status.value = f"Verzeichnis nicht gefunden oder kein Ordner: {root_path}"
            scan_status.color = BAD
            page.update()
            return
        if rebuild and not rebuild_confirm_checkbox.value:
            scan_status.value = "Vor dem Neuaufbau bitte die Löschbestätigung aktivieren."
            scan_status.color = BAD
            page.update()
            return
        if rebuild:
            # Require a fresh confirmation for every destructive rebuild.
            rebuild_confirm_checkbox.value = False
        reset_embedding_progress()
        refresh_embedding_progress_controls()
        cancel_event = threading.Event()
        scan_state.update({"running": True, "result": None, "error": None, "cancel_event": cancel_event})
        scan_status.value = "Scan läuft … Die Oberfläche bleibt währenddessen bedienbar."
        scan_status.color = WARN
        scan_progress.visible = True
        scan_start_button.disabled = True
        scan_rebuild_button.disabled = True
        scan_cancel_button.disabled = False
        scan_log.controls.clear()
        scan_log.controls.append(ft.Text(f"Starte Scan: {root_path}", size=11, color=ACCENT))

        handler = QueueLogHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)s  %(message)s", datefmt="%H:%M:%S"))
        app_logger = logging.getLogger("winkg")
        app_logger.addHandler(handler)
        if app_logger.level > logging.INFO or app_logger.level == logging.NOTSET:
            app_logger.setLevel(logging.INFO)

        def worker() -> None:
            indexer: DirectoryIndexer | None = None
            try:
                settings = get_settings()
                settings.validate_neo4j()
                indexer = DirectoryIndexer(settings, embedding_progress_callback=publish_embedding_progress)
                result = indexer.index(
                    root_path,
                    force=bool(force_checkbox.value) and not rebuild,
                    rebuild=rebuild,
                    no_embeddings=bool(no_embeddings_checkbox.value),
                    extract_entities=bool(extract_entities_checkbox.value),
                    include_ignored=bool(include_ignored_checkbox.value),
                    include_sensitive=bool(include_sensitive_checkbox.value),
                    cancel_event=cancel_event,
                )
                scan_state["result"] = result
            except Exception as exc:
                scan_state["error"] = f"{type(exc).__name__}: {exc}"
                logging.getLogger("winkg.gui").exception("Scan fehlgeschlagen")
            finally:
                if indexer is not None:
                    try:
                        indexer.close()
                    except Exception:
                        pass
                app_logger.removeHandler(handler)
                scan_state["running"] = False

        threading.Thread(target=worker, name="enargeia-scan", daemon=True).start()
        page.run_task(monitor_scan_task)
        page.update()

    async def monitor_scan_task() -> None:
        while scan_state["running"]:
            drain_scan_logs()
            if refresh_embedding_progress_controls():
                page.update()
            await asyncio.sleep(0.25)
        drain_scan_logs(max_count=5000)
        refresh_embedding_progress_controls()
        scan_progress.visible = False
        scan_start_button.disabled = False
        scan_rebuild_button.disabled = False
        scan_cancel_button.disabled = True
        if scan_state.get("error"):
            scan_status.value = f"Scan fehlgeschlagen: {scan_state['error']}"
            scan_status.color = BAD
            scan_log.controls.append(ft.Text(scan_state["error"], color=BAD, size=11))
        else:
            result = scan_state.get("result") or {}
            if result.get("cancelled"):
                scan_status.value = (f"Scan pausiert · gespeichert: {result.get('indexed', 0)} · "
                                     f"unverändert: {result.get('unchanged', 0)}. Zum Fortsetzen denselben Root erneut scannen.")
                scan_status.color = WARN
            else:
                scan_status.value = (
                f"Scan abgeschlossen · neu/aktualisiert: {result.get('indexed', 0)} · "
                f"unverändert: {result.get('unchanged', 0)} · Fehler: {result.get('errors', 0)}"
                )
                scan_status.color = GOOD if not result.get("errors", 0) else WARN
            scan_log.controls.append(ft.Text(json.dumps(result, ensure_ascii=False, indent=2, default=str), size=10, color=TEXT, selectable=True))
            await load_dashboard_data_into_controls()
        page.update()

    def drain_scan_logs(max_count: int = 80) -> None:
        count = 0
        while count < max_count:
            try:
                message = scan_log_queue.get_nowait()
            except queue.Empty:
                break
            scan_log.controls.append(ft.Text(message, size=10, color=MUTED, selectable=True))
            count += 1
        if len(scan_log.controls) > 350:
            del scan_log.controls[:len(scan_log.controls) - 350]

    scan_start_button = ft.Button(content="Inkrementell indexieren", icon=_safe_icon("SYNC_ROUNDED"), on_click=lambda e: start_scan(False))
    scan_rebuild_button = ft.Button(content="Root neu aufbauen", icon=_safe_icon("DELETE_OUTLINE"), on_click=lambda e: start_scan(True), color=BAD)
    def request_scan_cancel(_: Any) -> None:
        event = scan_state.get("cancel_event")
        if scan_state.get("running") and event is not None:
            event.set()
            scan_cancel_button.disabled = True
            scan_status.value = "Abbruch angefordert … Die aktuelle Datei wird noch sicher abgeschlossen."
            scan_status.color = WARN
            page.update()
    scan_cancel_button = ft.Button(content="Scan abbrechen / pausieren", icon=_safe_icon("PAUSE_CIRCLE_OUTLINED"), on_click=request_scan_cancel, disabled=True, color=WARN)

    def build_scan_view() -> ft.Control:
        selection_panel = _card(
            ft.Column(
                controls=[
                    ft.Row(controls=[_icon_tile(_safe_icon("FOLDER_SYNC_OUTLINED"), ACCENT, size=42), ft.Column(controls=[ft.Text("Datenquelle konfigurieren", size=17, weight=ft.FontWeight.W_600, color=TEXT), ft.Text("Inkrementeller Scan ist der sichere Standard.", size=11, color=MUTED)], spacing=4, expand=True)], spacing=12),
                    ft.Divider(color=BORDER),
                    ft.Text("QUELLVERZEICHNIS", size=10, weight=ft.FontWeight.BOLD, color=SUBTLE),
                    ft.Row(controls=[directory_field, ft.Button(content="Auswählen", icon=ft.Icons.FOLDER_OPEN_OUTLINED, on_click=pick_directory)], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    ft.Container(
                        content=ft.Column(controls=[
                            ft.Text("Indexierung starten", size=12, weight=ft.FontWeight.W_600, color=TEXT),
                            ft.Text("Neue und geänderte Dateien werden eingelesen; unveränderte Inhalte bleiben bestehen.", size=10, color=MUTED),
                            ft.Row(controls=[scan_start_button, scan_cancel_button, scan_progress], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER, wrap=True),
                        ], spacing=8), padding=14, bgcolor=PANEL_ALT, border_radius=12,
                    ),
                    ft.Row(controls=[ft.Icon(_safe_icon("SHIELD_OUTLINED"), color=GOOD, size=16), ft.Text("Quelldateien werden nicht verändert.", size=10, color=GOOD)], spacing=8),
                    scan_status,
                    scan_embedding_progress_panel,
                    ft.ExpansionTile(
                        title=ft.Text("Verarbeitungsoptionen", color=TEXT, size=12, weight=ft.FontWeight.W_600),
                        subtitle=ft.Text("Embeddings, LLM-Extraktion, erneute Verarbeitung und Ausschlüsse", color=MUTED, size=10),
                        expanded=False,
                        controls=[ft.Container(content=ft.Column(controls=[force_checkbox, no_embeddings_checkbox, extract_entities_checkbox, include_ignored_checkbox, ft.Text("Sensible Dateien können Zugangsdaten enthalten.", size=10, color=WARN), include_sensitive_checkbox], spacing=9), padding=ft.Padding.only(left=12, right=12, bottom=12))],
                        collapsed_text_color=MUTED, text_color=TEXT,
                    ),
                    ft.ExpansionTile(
                        title=ft.Text("Destruktiver Neuaufbau", color=WARN, size=12, weight=ft.FontWeight.W_600),
                        subtitle=ft.Text("Entfernt den Graph-Bestand des gewählten Roots vor der Neuindexierung", color=MUTED, size=10),
                        expanded=False,
                        controls=[ft.Container(content=ft.Column(controls=[ft.Text("Nur einsetzen, wenn ein vollständiger Neubau erforderlich ist.", size=10, color=WARN), rebuild_confirm_checkbox, scan_rebuild_button], spacing=8), padding=ft.Padding.only(left=12, right=12, bottom=12))],
                        collapsed_text_color=MUTED, text_color=TEXT,
                    ),
                ], spacing=13, scroll=ft.ScrollMode.AUTO, expand=True,
            ), padding=18,
        )
        logs_panel = _card(
            ft.Column(
                controls=[
                    ft.Row(controls=[ft.Column(controls=[ft.Text("Laufaktivität", size=17, weight=ft.FontWeight.W_600, color=TEXT), ft.Text("Fortschritt und Diagnostik des aktuellen Scans.", size=11, color=MUTED)], spacing=4, expand=True), ft.IconButton(icon=_safe_icon("DELETE_SWEEP_OUTLINED"), tooltip="Protokoll leeren", on_click=lambda e: clear_scan_log())], vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=8),
                    ft.Container(content=ft.Row(controls=[_icon_tile(_safe_icon("TERMINAL_OUTLINED"), BLUE, size=34), ft.Column(controls=[ft.Text("Scan-Protokoll", size=11, weight=ft.FontWeight.W_600, color=TEXT), ft.Text("Letzte 350 Logzeilen", size=9, color=SUBTLE)], spacing=2)], spacing=9), padding=10, bgcolor=PANEL_ALT, border_radius=10),
                    ft.Container(content=scan_log, height=405, padding=12, bgcolor="#071326", border=ft.Border.all(1, BORDER), border_radius=10),
                    ft.Row(controls=[ft.Icon(_safe_icon("PAUSE_CIRCLE_OUTLINED"), color=WARN, size=16), ft.Text("Abbrechen stoppt an einer sicheren Verarbeitungsgrenze; den gleichen Root später erneut starten.", size=10, color=MUTED, expand=True)], spacing=8),
                ], spacing=12,
            ), padding=18,
        )
        registry_panel = _card(ft.Column(controls=[
            ft.Row(controls=[ft.Column(controls=[ft.Text("Registrierte Datenquellen", size=17, weight=ft.FontWeight.W_600, color=TEXT), ft.Text("Roots erneut indexieren oder ausschließlich aus Neo4j entfernen.", size=11, color=MUTED)], spacing=4, expand=True), ft.Button(content="Aktualisieren", icon=_safe_icon("REFRESH_ROUNDED"), on_click=lambda e: page.run_task(load_dashboard_task))], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            source_registry_status,
            source_root_list,
        ], spacing=13), padding=18)
        return ft.Column(
            controls=[
                ft.Row(
                    controls=[
                        ft.Container(content=selection_panel, expand=7, height=620),
                        ft.Container(content=logs_panel, expand=5, height=620),
                    ],
                    spacing=14,
                    # Fixed, equal-height viewport cards avoid STRETCH against the
                    # unbounded cross-axis of this vertically scrollable parent.
                    vertical_alignment=ft.CrossAxisAlignment.START,
                ),
                registry_panel,
            ], spacing=15, scroll=ft.ScrollMode.AUTO, expand=True,
        )

    def clear_scan_log() -> None:
        scan_log.controls.clear()
        scan_log.controls.append(ft.Text("Protokoll geleert. Neue Scan-Aktivität erscheint hier.", size=11, color=MUTED))
        page.update()

    # Graph Explorer is for structural inspection only. Content Q&A remains an MCP capability.
    def open_in_graph(path: str) -> None:
        graph_query.value = path
        navigate("graph")
        page.run_task(graph_task)

    # ---------------- Graph explorer ----------------
    def graph_click(_: Any) -> None:
        if not (graph_query.value or "").strip():
            graph_status.value = "Bitte einen Dateinamen oder relativen/absoluten Pfad eingeben."
            graph_status.color = WARN
            page.update()
            return
        graph_status.value = "Graph-Nachbarschaft wird geladen …"
        graph_status.color = WARN
        graph_results.controls.clear()
        graph_results.controls.append(_card(ft.Row(controls=[ft.ProgressRing(width=18, height=18, stroke_width=2, color=ACCENT), ft.Text("Graph-Nachbarschaft wird aus Neo4j geladen …", size=12, color=MUTED)], spacing=10), bgcolor=PANEL_ALT))
        page.update()
        page.run_task(graph_task)

    async def graph_task() -> None:
        query = (graph_query.value or "").strip()
        root_filter = (graph_root_filter.value or "").strip()
        try:
            data = await asyncio.to_thread(_run_graph_query, query, root_filter)
            graph_results.controls.clear()
            if not data:
                add_notice(graph_results, f"Keine Datei zu „{query}“ gefunden. Nutze den relativen oder absoluten Pfad oder einen eindeutigen Dateinamen.")
                graph_status.value = "Keine passende Datei gefunden"
                graph_status.color = WARN
                page.update()
                return
            file_info = data["file"]
            graph_results.controls.append(
                _card(
                    ft.Column(
                        controls=[
                            ft.Text("ZENTRALER KNOTEN · KGFile", size=10, color=ACCENT, weight=ft.FontWeight.BOLD),
                            ft.Text(file_info.get("name") or "(ohne Namen)", size=19, weight=ft.FontWeight.BOLD, color=TEXT),
                            ft.Text(str(file_info.get("path") or ""), size=11, color=MUTED, selectable=True),
                            ft.Row(
                                controls=[
                                    ft.Text(f"Status: {file_info.get('status') or 'unbekannt'}", size=11, color=TEXT),
                                    ft.Text(f"Typ: {file_info.get('extension') or '–'}", size=11, color=MUTED),
                                    ft.Text(f"Größe: {file_info.get('size') or 0:,} Bytes", size=11, color=MUTED),
                                ],
                                wrap=True,
                                spacing=14,
                            ),
                        ],
                        spacing=8,
                    ),
                    bgcolor="#15345A",
                )
            )

            if file_info.get("needs_deep_analysis"):
                if file_info.get("status") == "too_large":
                    warning_text = (
                        "WICHTIG: Diese Datei überschreitet MAX_FILE_SIZE_MB und wurde nur als Metadaten gespeichert. "
                        "Der Inhalt ist nicht durchsuchbar und muss für eine spätere Analyse separat verarbeitet werden. "
                        f"Grund: {file_info.get('truncation_reason') or 'Dateigrößenlimit überschritten'}"
                    )
                else:
                    warning_text = (
                        f"WICHTIG: Diese Datei wurde gekürzt: {file_info.get('indexed_chunks', 0)} von "
                        f"{file_info.get('total_chunks', 0)} Chunks indexiert; "
                        f"{file_info.get('omitted_chunks', 0)} ausgelassen. "
                        "Die Originaldatei sollte bei Bedarf gesondert analysiert werden."
                    )
                graph_results.controls.append(_card(ft.Text(warning_text, color=WARN, size=12)))

            parents = [node_card(row.get("name", "Ordner"), row.get("path", ""), "KGFolder", width=300) for row in data.get("parent_folders", [])]
            chunks = []
            for row in data.get("chunks", []):
                label = f"Chunk {row.get('ordinal', '?')}"
                meta = " · ".join(part for part in [str(row.get("section") or "").strip(), f"Seite {row['page']}" if row.get("page") else ""] if part)
                preview = str(row.get("preview") or "").strip().replace("\n", " ")
                chunks.append(node_card(label, (meta + "\n" if meta else "") + preview, "KGChunk", width=285))
            entities = [node_card(row.get("name", "Entität"), f"{row.get('type') or 'Entity'} · {row.get('description') or ''}", "KGEntity", width=230) for row in data.get("entities", [])]
            related = []
            for row in data.get("related_files", []):
                related_path = str(row.get("path") or "")
                shared = ", ".join(row.get("entities") or [])
                subtitle = f"Gemeinsame Entitäten: {shared or '–'}"
                related.append(
                    node_card(
                        row.get("name", Path(related_path).name),
                        f"{related_path}\n{subtitle}",
                        "KGFile · RELATES THROUGH ENTITY",
                        width=290,
                        on_open=lambda p=related_path: open_in_graph(p),
                    )
                )
            graph_results.controls.extend([
                relation_section("Übergeordneter Ordner", "KG_CONTAINS", parents, "Kein übergeordneter Ordner gefunden."),
                relation_section("Textabschnitte", "KG_HAS_CHUNK", chunks, "Keine Textabschnitte gespeichert; möglicherweise Metadaten-only oder nicht lesbare Datei."),
                relation_section("Erkannte Entitäten", "KG_MENTIONS", entities, "Keine semantischen Entitäten. Aktivieren Sie die optionale Entity-Extraktion beim Scan."),
                relation_section("Verwandte Dateien", "gemeinsame Entitäten", related, "Keine über gemeinsame Entitäten verknüpften Dateien gefunden."),
            ])
            graph_status.value = "Nachbarschaft geladen · Beziehungen stammen aus dem gespeicherten Neo4j-Graphen."
            graph_status.color = GOOD
        except Exception as exc:
            add_notice(graph_results, f"Graph-Abfrage fehlgeschlagen: {exc}", BAD)
            graph_status.value = "Graph-Abfrage fehlgeschlagen"
            graph_status.color = BAD
        page.update()

    def build_graph_view() -> ft.Control:
        galaxy_panel = graph3d_launch_panel()
        explorer_controls = _card(ft.Column(controls=[
            ft.Row(controls=[graph_query, ft.Button(content="Knoten laden", icon=_safe_icon("TRAVEL_EXPLORE_OUTLINED"), on_click=graph_click)], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            graph_root_filter,
            ft.Row(controls=[ft.Icon(_safe_icon("INFO_OUTLINE"), color=BLUE, size=15), graph_status], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        ], spacing=11), padding=16)
        return ft.Column(controls=[galaxy_panel, explorer_controls, graph_results], spacing=14, scroll=ft.ScrollMode.AUTO, expand=True)

    # ---------------- Graph Intelligence view ----------------
    def analysis_report_controls(report: dict[str, Any]) -> list[ft.Control]:
        """Render the useful analysis summary first and keep raw JSON for diagnostics."""
        controls: list[ft.Control] = []
        gds = report.get("gds") or {}
        counts = report.get("graph_stats") or {}
        candidates = report.get("link_candidates_created_or_updated", 0)
        insights = report.get("insights") or []
        summary_items = [
            ("Status", str(report.get("status", "–"))),
            ("Analysierter Umfang", str(report.get("scope", "–"))),
            ("Neue/aktualisierte Vorschläge", str(candidates)),
            ("Analysebefunde", str(len(insights))),
            ("Doppelte Inhaltsgruppen", str(report.get("duplicate_content_groups", 0))),
            ("Dateien ohne vollständige Indexierung", str(report.get("unindexed_files", 0))),
            ("Isolierte Entitäten", str(report.get("isolated_entities", 0))),
            ("GDS", str(gds.get("status", "nicht ausgeführt"))),
        ]
        controls.append(ft.Row(
            controls=[
                _card(ft.Column(controls=[
                    ft.Text(label.upper(), size=9, color=SUBTLE, weight=ft.FontWeight.BOLD),
                    ft.Text(value, size=18, weight=ft.FontWeight.W_600, color=GOOD if label == "Status" and value == "completed" else TEXT, max_lines=2),
                ], spacing=7), padding=14, bgcolor=PANEL_ALT, width=205, border_color=BORDER, radius=12)
                for label, value in summary_items
            ],
            wrap=True,
            spacing=10,
            run_spacing=10,
        ))
        if report.get("notice"):
            controls.append(ft.Text(str(report["notice"]), size=12, color=WARN, selectable=True))
        if insights:
            controls.append(_section_title("Wesentliche Befunde", "Aus dem aktuellen Analyselauf gespeicherte Einsichten"))
            for insight in insights[:30]:
                controls.append(_card(ft.Column(controls=[
                    ft.Row(controls=[
                        ft.Text(str(insight.get("title") or insight.get("kind") or "Befund"), size=13, weight=ft.FontWeight.W_600, color=TEXT, expand=True),
                        ft.Text(f"Score: {insight.get('score', '–')}", size=10, color=ACCENT),
                    ], spacing=8),
                    ft.Text(str(insight.get("description") or "Keine weitere Beschreibung vorhanden."), size=12, color=MUTED, selectable=True),
                    ft.Text(
                        f"Typ: {insight.get('kind', '–')} · Stand: {'veraltet' if insight.get('is_stale') else 'aktuell'} · Aktualisiert: {insight.get('updated_at', '–')}",
                        size=10, color=WARN if insight.get("is_stale") else MUTED,
                    ),
                ], spacing=7), padding=13, bgcolor=PANEL_ALT))
        # The detailed JSON remains available for debugging and API-level diagnostics.
        controls.append(ft.ExpansionTile(
            title=ft.Text("Technische Detailausgabe (JSON)", color=TEXT, size=12),
            controls=[ft.Container(
                content=ft.Text(json.dumps(report, ensure_ascii=False, indent=2, default=str), size=10, color=MUTED, selectable=True),
                padding=12,
                bgcolor=BG,
                border_radius=8,
            )],
            collapsed_text_color=MUTED,
            text_color=TEXT,
        ))
        return controls

    async def graph_capabilities_task() -> None:
        graph_intelligence_status.value = "Prüfe Neo4j- und GDS-Fähigkeiten …"
        graph_intelligence_status.color = WARN
        graph_intelligence_output.controls.clear()
        graph_intelligence_output.controls.append(ft.Container(
            content=ft.Row(controls=[ft.ProgressRing(width=18, height=18, stroke_width=2, color=ACCENT), ft.Text("Analysefähigkeiten werden geprüft …", size=12, color=MUTED)], spacing=10),
            padding=14, bgcolor=PANEL_ALT, border_radius=8,
        ))
        page.update()
        try:
            report = await asyncio.to_thread(_run_graph_capabilities)
            graph_intelligence_output.controls.clear()
            graph_intelligence_output.controls.extend(analysis_report_controls(report))
            graph_intelligence_status.value = (
                "GDS verfügbar" if report.get("gds", {}).get("available")
                else "Neo4j erreichbar · GDS nicht erkannt; Cypher-Analysen bleiben verfügbar"
            )
            graph_intelligence_status.color = GOOD if report.get("gds", {}).get("available") else WARN
        except Exception as exc:
            graph_intelligence_status.value = f"Fähigkeitsprüfung fehlgeschlagen: {exc}"
            graph_intelligence_status.color = BAD
            graph_intelligence_output.controls.clear()
            graph_intelligence_output.controls.append(_card(ft.Column(controls=[
                ft.Row(controls=[ft.Icon(_safe_icon("ERROR_OUTLINE"), color=BAD, size=20), ft.Text("Fähigkeitsprüfung fehlgeschlagen", size=13, weight=ft.FontWeight.W_600, color=TEXT)], spacing=8),
                ft.Text(str(exc), size=11, color=MUTED, selectable=True),
            ], spacing=8), bgcolor=PANEL_ALT, border_color=BORDER))
        page.update()

    async def graph_analysis_task() -> None:
        graph_intelligence_status.value = "Graph wird analysiert. Bei großen Datenbeständen kann das dauern …"
        graph_intelligence_status.color = WARN
        graph_intelligence_output.controls.clear()
        graph_intelligence_output.controls.append(ft.Container(
            content=ft.Row(controls=[ft.ProgressRing(width=18, height=18, stroke_width=2, color=ACCENT), ft.Text("Regelbasierte Qualitätsprüfung und optionale GDS-Läufe laufen …", size=12, color=MUTED)], spacing=10),
            padding=14, bgcolor=PANEL_ALT, border_radius=8,
        ))
        page.update()
        try:
            report = await asyncio.to_thread(_run_graph_analysis, bool(gds_analysis_checkbox.value))
            graph_intelligence_output.controls.clear()
            graph_intelligence_output.controls.extend(analysis_report_controls(report))
            graph_intelligence_status.value = (
                f"Analyse {report.get('status', 'beendet')} · Lauf-ID {report.get('run_id', '–')}"
            )
            graph_intelligence_status.color = GOOD if report.get("status") == "completed" else WARN
        except Exception as exc:
            graph_intelligence_status.value = f"Graphanalyse fehlgeschlagen: {exc}"
            graph_intelligence_status.color = BAD
            graph_intelligence_output.controls.clear()
            graph_intelligence_output.controls.append(_card(ft.Column(controls=[
                ft.Row(controls=[ft.Icon(_safe_icon("ERROR_OUTLINE"), color=BAD, size=20), ft.Text("Graphanalyse fehlgeschlagen", size=13, weight=ft.FontWeight.W_600, color=TEXT)], spacing=8),
                ft.Text(str(exc), size=11, color=MUTED, selectable=True),
                ft.Text("Prüfe zunächst die Neo4j-Verbindung und GDS-Capabilities. Externe LLM-Aufrufe sind für regelbasierte Prüfungen nicht erforderlich.", size=10, color=SUBTLE),
                ft.Button(content="Erneut versuchen", icon=_safe_icon("REFRESH_ROUNDED"), on_click=lambda e: page.run_task(graph_analysis_task)),
            ], spacing=8), bgcolor=PANEL_ALT, border_color=BORDER))
        page.update()

    async def graph_candidates_task() -> None:
        quality_status.value = "Lade offene Beziehungskandidaten …"
        quality_status.color = WARN
        quality_output.controls.clear()
        quality_output.controls.append(ft.Container(
            content=ft.Row(controls=[ft.ProgressRing(width=18, height=18, stroke_width=2, color=ACCENT), ft.Text("Beziehungsvorschläge aus Neo4j werden geladen …", size=12, color=MUTED)], spacing=10),
            padding=14, bgcolor=PANEL_ALT, border_radius=8,
        ))
        page.update()
        try:
            rows = await asyncio.to_thread(_run_graph_link_candidates, "proposed")
            quality_output.controls.clear()
            if not rows:
                quality_output.controls.append(
                    _card(ft.Text(
                        "Keine offenen Vorschläge vorhanden. Starte zuerst eine Graphanalyse; Vorschläge benötigen erkannte Entitäten oder GDS-Strukturinformationen.",
                        size=12, color=MUTED, selectable=True,
                    ))
                )
            else:
                for row in rows:
                    candidate_id = str(row.get("id", ""))
                    subject = str(row.get("subject_name") or row.get("subject_id") or "?")
                    predicate = str(row.get("predicate") or "RELATED_TO")
                    object_name = str(row.get("object_name") or row.get("object_id") or "?")
                    score = row.get("score")
                    evidence_count = int(row.get("evidence_count") or 0)
                    method = str(row.get("method") or "unknown")
                    method_reviewed = int(row.get("method_reviewed") or 0)
                    historical_rate = row.get("method_acceptance_rate")
                    if historical_rate is None:
                        calibration_text = f"noch nicht kalibriert (Bewertungen: {method_reviewed}/5)"
                    else:
                        calibration_text = f"bisher {float(historical_rate):.0%} akzeptiert aus {method_reviewed} Bewertungen"
                    candidate_heading = ft.Row(
                        controls=[
                            ft.Column(controls=[
                                _eyebrow("PROPOSED RELATIONSHIP", PURPLE),
                                ft.Text(subject, size=14, weight=ft.FontWeight.W_600, color=TEXT, selectable=True),
                            ], spacing=4, expand=True),
                            _status_pill(f"{evidence_count} Beleg(e)", GOOD if evidence_count else WARN),
                        ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    )
                    relation_line = ft.Container(
                        content=ft.Row(controls=[
                            ft.Container(content=ft.Text(subject, size=11, color=TEXT, max_lines=2), padding=10, bgcolor="#102747", border_radius=8, expand=True),
                            ft.Column(controls=[ft.Icon(_safe_icon("ARROW_FORWARD_ROUNDED"), color=ACCENT, size=18), ft.Text(predicate, size=9, color=ACCENT, text_align=ft.TextAlign.CENTER)], spacing=2, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
                            ft.Container(content=ft.Text(object_name, size=11, color=TEXT, max_lines=2), padding=10, bgcolor="#102747", border_radius=8, expand=True),
                        ], spacing=9, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                        padding=12, bgcolor="#071326", border_radius=11,
                    )
                    quality_output.controls.append(
                        _card(ft.Column(controls=[
                            candidate_heading,
                            relation_line,
                            ft.Row(controls=[
                                _status_pill(f"Methode: {method}", BLUE),
                                _status_pill(f"Score: {score if score is not None else '–'}", PURPLE),
                                _status_pill(f"Historie: {calibration_text}", MUTED),
                            ], spacing=8, wrap=True),
                            ft.Text(str(row.get('reason') or 'Keine Begründung hinterlegt.'), size=11, color=MUTED, selectable=True),
                            ft.ExpansionTile(title=ft.Text("Technische Metadaten", size=10, color=MUTED), controls=[ft.Container(content=ft.Text(f"Kandidaten-ID: {candidate_id}\nMethode: {method}\nGraph-Score: {score if score is not None else '–'}", size=10, color=SUBTLE, selectable=True), padding=10)], collapsed_text_color=MUTED, text_color=TEXT),
                            ft.Row(controls=[
                                ft.Button(
                                    content="Akzeptieren",
                                    on_click=lambda e, cid=candidate_id: page.run_task(
                                        review_graph_candidate_task, cid, "accepted"
                                    ),
                                    disabled=not candidate_id,
                                ),
                                ft.Button(
                                    content="Ablehnen",
                                    on_click=lambda e, cid=candidate_id: page.run_task(
                                        review_graph_candidate_task, cid, "rejected"
                                    ),
                                    disabled=not candidate_id,
                                ),
                                ft.Button(
                                    content="Zurückstellen",
                                    on_click=lambda e, cid=candidate_id: page.run_task(
                                        review_graph_candidate_task, cid, "deferred"
                                    ),
                                    disabled=not candidate_id,
                                ),
                            ], wrap=True, spacing=8),
                        ], spacing=8))
                    )
            quality_status.value = (
                f"{len(rows)} offene Vorschläge · Feedback wird in Neo4j gespeichert und beeinflusst weitere Analysen"
            )
            quality_status.color = GOOD if rows else MUTED
        except Exception as exc:
            quality_status.value = f"Kandidaten konnten nicht geladen werden: {exc}"
            quality_status.color = BAD
            quality_output.controls.clear()
            quality_output.controls.append(_card(ft.Column(controls=[
                ft.Row(controls=[ft.Icon(_safe_icon("ERROR_OUTLINE"), color=BAD, size=20), ft.Text("Beziehungsvorschläge konnten nicht geladen werden", size=13, weight=ft.FontWeight.W_600, color=TEXT)], spacing=8),
                ft.Text(str(exc), size=11, color=MUTED, selectable=True),
                ft.Text("Die übrigen Graph-Analysen sind davon unabhängig. Du kannst es erneut versuchen.", size=10, color=SUBTLE),
                ft.Button(content="Erneut laden", icon=_safe_icon("REFRESH_ROUNDED"), on_click=lambda e: page.run_task(graph_candidates_task)),
            ], spacing=8), bgcolor=PANEL_ALT, border_color=BORDER))
        page.update()

    async def review_graph_candidate_task(candidate_id: str, decision: str) -> None:
        quality_status.value = f"Speichere Entscheidung: {decision} …"
        quality_status.color = WARN
        page.update()
        try:
            result = await asyncio.to_thread(_run_review_graph_link_candidate, candidate_id, decision)
            quality_status.value = (
                f"Vorschlag {decision}: {result.get('subject_name', result.get('subject_id', '?'))} → "
                f"{result.get('object_name', result.get('object_id', '?'))}"
            )
            quality_status.color = GOOD
            await graph_candidates_task()
        except Exception as exc:
            quality_status.value = f"Entscheidung konnte nicht gespeichert werden: {exc}"
            quality_status.color = BAD
            page.update()

    def build_quality_view() -> ft.Control:
        candidates_panel = _card(ft.Column(controls=[
            ft.Row(controls=[_icon_tile(ft.Icons.LINK_OUTLINED, PURPLE, size=38), ft.Column(controls=[ft.Text("Offene Beziehungsvorschläge", size=16, weight=ft.FontWeight.W_600, color=TEXT), ft.Text("Belege, Ableitungsmethode und Akzeptanzhistorie sind getrennt sichtbar.", size=11, color=MUTED)], spacing=4, expand=True), ft.Button(content="Aktualisieren", icon=_safe_icon("REFRESH_ROUNDED"), on_click=lambda e: page.run_task(graph_candidates_task))], spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Divider(color=BORDER),
            quality_status,
        ], spacing=12), padding=17)
        return ft.Column(controls=[candidates_panel, quality_output], spacing=14, scroll=ft.ScrollMode.AUTO, expand=True)

    def build_graph_intelligence_view() -> ft.Control:
        config_panel = _card(ft.Column(controls=[
            ft.Row(controls=[_icon_tile(ft.Icons.ACCOUNT_TREE_OUTLINED, ACCENT, size=38), ft.Column(controls=[ft.Text("Analyse konfigurieren", size=16, weight=ft.FontWeight.W_600, color=TEXT), ft.Text("Regelbasierte Prüfungen laufen unabhängig von GDS; GDS ergänzt sie optional.", size=11, color=MUTED)], spacing=4, expand=True)], spacing=12),
            ft.Divider(color=BORDER),
            gds_analysis_checkbox,
            ft.Row(controls=[ft.Button(content="Capabilities prüfen", icon=_safe_icon("FACT_CHECK_OUTLINED"), on_click=lambda e: page.run_task(graph_capabilities_task)), ft.Button(content="Analyse ausführen", icon=_safe_icon("PLAY_ARROW_ROUNDED"), on_click=lambda e: page.run_task(graph_analysis_task))], wrap=True, spacing=10),
            ft.Row(controls=[ft.Icon(_safe_icon("INFO_OUTLINE"), color=BLUE, size=15), graph_intelligence_status], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        ], spacing=12), padding=18)
        results_panel = _card(ft.Column(controls=[
            ft.Row(controls=[ft.Column(controls=[ft.Text("Ergebnisse & Befunde", size=17, weight=ft.FontWeight.W_600, color=TEXT), ft.Text("Kennzahlen und verwertbare Befunde zuerst; Rohdaten nur bei Bedarf.", size=11, color=MUTED)], spacing=4, expand=True)], spacing=10),
            ft.Divider(color=BORDER),
            graph_intelligence_output,
        ], spacing=12), padding=18)
        return ft.Column(controls=[config_panel, results_panel], spacing=14, scroll=ft.ScrollMode.AUTO, expand=True)

    # ---------------- Connection view ----------------
    def open_env_file(_: Any) -> None:
        """Open the guided setup wizard instead of exposing raw configuration to users."""
        try:
            if IS_FROZEN:
                command = [sys.executable, "--configure"]
                subprocess.Popen(command, cwd=str(PROJECT_ROOT), close_fds=True)
            else:
                subprocess.Popen(
                    [sys.executable, "-m", "winkg.bootstrap", "--configure"],
                    cwd=str(PROJECT_ROOT), close_fds=True,
                )
            connection_status.value = "Konfigurationsassistent wurde geöffnet. Änderungen gelten nach einem Neustart."
            connection_status.color = GOOD
        except Exception as exc:
            connection_status.value = f"Konfigurationsassistent konnte nicht geöffnet werden: {exc}"
            connection_status.color = BAD
        page.update()

    async def doctor_task() -> None:
        reset_embedding_progress()
        refresh_embedding_progress_controls()
        global_status.value = "Prüfe Neo4j und Embeddings …"
        global_status.color = WARN
        connection_status.value = "Verbindung wird geprüft …"
        connection_status.color = WARN
        connection_details.controls.clear()
        connection_details.controls.append(ft.Container(
            content=ft.Row(controls=[ft.ProgressRing(width=18, height=18, stroke_width=2, color=ACCENT), ft.Text("Neo4j und Embedding-Endpunkt werden getestet …", size=12, color=MUTED)], spacing=10),
            padding=14, bgcolor=PANEL_ALT, border_radius=8,
        ))
        page.update()
        try:
            # The embedding constructor/download blocks, so run it off the Flet
            # event loop and poll the thread-safe progress state for live UI updates.
            doctor_future = asyncio.create_task(
                asyncio.to_thread(_run_doctor, True, publish_embedding_progress)
            )
            while not doctor_future.done():
                if refresh_embedding_progress_controls():
                    page.update()
                await asyncio.sleep(0.2)
            report = await doctor_future
            if refresh_embedding_progress_controls():
                page.update()
            connection_status.value = "Neo4j und Embeddings erreichbar"
            connection_status.color = GOOD
            connection_details.controls.clear()
            connection_details.controls.append(_card(ft.Text(json.dumps(report, ensure_ascii=False, indent=2, default=str), size=11, color=TEXT, selectable=True)))
            global_status.value = "Neo4j verbunden"
            global_status.color = GOOD
            try:
                header_status_dot.bgcolor = GOOD
            except NameError:
                pass
            stats = report.get("stats") or {}
            for key in stat_values:
                stat_values[key].value = str(stats.get(key, 0))
            for key, control in quality_stat_values.items():
                control.value = str(stats.get(key, 0))
        except Exception as exc:
            publish_embedding_progress({"phase": "error", "message": f"Embedding-/Verbindungstest fehlgeschlagen: {exc}"})
            refresh_embedding_progress_controls()
            connection_status.value = f"Verbindung fehlgeschlagen: {exc}"
            connection_status.color = BAD
            connection_details.controls.clear()
            connection_details.controls.append(_card(ft.Text(str(exc), color=BAD, size=12, selectable=True)))
            global_status.value = "Neo4j nicht erreichbar"
            global_status.color = BAD
            try:
                header_status_dot.bgcolor = BAD
            except NameError:
                pass
        page.update()

    def build_connection_view() -> ft.Control:
        rows: list[ft.Control] = []
        for key, value in _settings_preview().items():
            rows.append(ft.Container(content=ft.Row(controls=[ft.Text(key, size=11, color=MUTED, width=175), ft.Text(value, size=11, color=TEXT, selectable=True, expand=True)], vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=12), padding=ft.Padding.symmetric(vertical=7), border=ft.Border.only(bottom=ft.BorderSide(1, BORDER))))
        config_panel = _card(ft.Column(controls=[ft.Row(controls=[_icon_tile(_safe_icon("TUNE_OUTLINED"), BLUE, size=42), ft.Column(controls=[_eyebrow("CONFIGURATION"), ft.Text("Projektkonfiguration", size=16, weight=ft.FontWeight.W_600, color=TEXT), ft.Text("Datenbank, Embeddings und optionale KI unabhängig konfigurieren.", size=11, color=MUTED)], spacing=4, expand=True)], spacing=12), ft.Divider(color=BORDER), *rows, ft.Button(content="Setup-Assistent öffnen", icon=_safe_icon("EDIT_NOTE_OUTLINED"), on_click=open_env_file)], spacing=8), padding=18)
        mcp_panel = _card(ft.Column(controls=[ft.Row(controls=[_icon_tile(_safe_icon("CABLE_OUTLINED"), ACCENT, size=42), ft.Column(controls=[_eyebrow("EXTERNAL ACCESS"), ft.Text("MCP für externe LLMs", size=16, weight=ft.FontWeight.W_600, color=TEXT), ft.Text("Der MCP-Prozess wird vom jeweiligen MCP-Client gestartet.", size=11, color=MUTED)], spacing=4, expand=True)], spacing=12), ft.Divider(color=BORDER), ft.Row(controls=[_status_pill("Transport: stdio", BLUE), _status_pill("Clientstatus nicht direkt ermittelbar", WARN)], wrap=True, spacing=8), ft.Text("Die Werkzeuge stellen u. a. Such- und Kontextzugriff, Graph-Statistiken, Analysefunktionen, Beziehungsvorschläge und Quelleninformationen bereit. Die Desktop-GUI selbst enthält bewusst keine Chat-/RAG-Oberfläche.", size=11, color=MUTED, selectable=True)], spacing=12), padding=18)
        health_panel = _card(ft.Column(controls=[ft.Row(controls=[_icon_tile(_safe_icon("MONITOR_HEART_OUTLINED"), GOOD, size=42), ft.Column(controls=[_eyebrow("HEALTH CHECK"), ft.Text("Verbindung & Laufzeit", size=16, weight=ft.FontWeight.W_600, color=TEXT), ft.Text("Neo4j und das konfigurierte Embedding-Modell testen.", size=11, color=MUTED)], spacing=4, expand=True)], spacing=12), ft.Divider(color=BORDER), ft.Row(controls=[connection_status, ft.Container(width=8), ft.Button(content="Systemtest starten", icon=_safe_icon("PLAY_ARROW_ROUNDED"), on_click=lambda e: page.run_task(doctor_task))], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER, wrap=True), connection_embedding_progress_panel, connection_details], spacing=12), padding=18)
        return ft.Column(controls=[ft.Row(controls=[ft.Container(content=config_panel, expand=7), ft.Container(content=ft.Column(controls=[mcp_panel, health_panel], spacing=14), expand=5)], spacing=14, vertical_alignment=ft.CrossAxisAlignment.START)], spacing=14, scroll=ft.ScrollMode.AUTO, expand=True)

    # ---------------- Main navigation & desktop shell ----------------
    view_builders: dict[str, Callable[[], ft.Control]] = {
        "dashboard": build_dashboard_view,
        "scan": build_scan_view,
        "graph": build_graph_view,
        "quality": build_quality_view,
        "intelligence": build_graph_intelligence_view,
        "connection": build_connection_view,
    }
    # Each page owns several long-lived Flet controls (logs, statuses and result
    # columns). Rebuilding a page on every navigation re-parented those same
    # controls into new parents and could result in blank/gray native surfaces.
    view_cache: dict[str, ft.Control] = {}

    page_titles = {
        "dashboard": ("Übersicht", "Graph-Bestand, Datenabdeckung und Qualität"),
        "scan": ("Datenquellen", "Roots verwalten und Indexierung überwachen"),
        "graph": ("Graph Explorer", "Knoten, Kanten und Quellenbeziehungen inspizieren"),
        "quality": ("Qualität & Beziehungen", "Evidenz prüfen und Vorschläge kontrolliert entscheiden"),
        "intelligence": ("Graph-Analysen", "Graph-Qualität und Struktur mit Cypher und GDS untersuchen"),
        "connection": ("System & MCP", "Verbindungen, Konfiguration und externe LLM-Anbindung"),
    }
    nav_items = [
        ("dashboard", "Übersicht", "COMMAND", ft.Icons.DASHBOARD_OUTLINED),
        ("scan", "Datenquellen", "KNOWLEDGE BASE", ft.Icons.FOLDER_OPEN_OUTLINED),
        ("graph", "Graph Explorer", "KNOWLEDGE BASE", ft.Icons.HUB_OUTLINED),
        ("quality", "Qualität & Beziehungen", "INTELLIGENCE", ft.Icons.LINK_OUTLINED),
        ("intelligence", "Graph-Analysen", "INTELLIGENCE", ft.Icons.INSIGHTS_OUTLINED),
        ("connection", "System & MCP", "SYSTEM", ft.Icons.SETTINGS_OUTLINED),
    ]
    nav_tiles: dict[str, tuple[ft.Container, ft.Container, ft.Icon, ft.Text]] = {}

    def navigate(key: str) -> None:
        if key not in view_builders:
            return
        title, subtitle = page_titles[key]
        header_title.value = title
        header_subtitle.value = subtitle
        if key not in view_cache:
            view_cache[key] = view_builders[key]()
        view_host.content = view_cache[key]
        for nav_key, (tile, accent_bar, icon_control, label_control) in nav_tiles.items():
            active = nav_key == key
            tile.bgcolor = PANEL_ALT if active else SIDEBAR
            tile.border = ft.Border.all(1, "#1677FF" if active else SIDEBAR)
            accent_bar.bgcolor = ACCENT if active else SIDEBAR
            icon_control.color = ACCENT if active else MUTED
            label_control.color = TEXT if active else MUTED
            label_control.weight = ft.FontWeight.W_600 if active else ft.FontWeight.NORMAL
        page.update()
        if key == "quality":
            page.run_task(graph_candidates_task)
        elif key == "intelligence":
            page.run_task(graph_capabilities_task)

    nav_controls: list[ft.Control] = []
    section_for_key = {"dashboard": "COMMAND", "scan": "KNOWLEDGE BASE", "graph": "KNOWLEDGE BASE", "quality": "INTELLIGENCE", "intelligence": "INTELLIGENCE", "connection": "SYSTEM"}
    previous_section = None
    for nav_key, nav_label, section, nav_icon in nav_items:
        if section != previous_section:
            nav_controls.append(ft.Container(content=_eyebrow(section, SUBTLE), padding=ft.Padding.only(left=12, top=18 if previous_section else 4, bottom=7)))
            previous_section = section
        accent_bar = ft.Container(width=3, height=25, bgcolor=SIDEBAR, border_radius=3)
        icon_control = ft.Icon(nav_icon, size=19, color=MUTED)
        label_control = ft.Text(nav_label, size=12, color=MUTED, weight=ft.FontWeight.NORMAL)
        tile = ft.Container(
            content=ft.Row(controls=[accent_bar, icon_control, ft.Container(content=label_control, expand=True)], spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            padding=ft.Padding.symmetric(horizontal=10, vertical=11),
            bgcolor=SIDEBAR,
            border=ft.Border.all(1, SIDEBAR),
            border_radius=10,
            on_click=lambda e, k=nav_key: navigate(k),
            ink=True,
        )
        nav_tiles[nav_key] = (tile, accent_bar, icon_control, label_control)
        nav_controls.append(tile)

    sidebar = ft.Container(
        width=258,
        bgcolor=SIDEBAR,
        padding=ft.Padding.symmetric(horizontal=14, vertical=16),
        border=ft.Border.only(right=ft.BorderSide(1, BORDER)),
        content=ft.Column(
            controls=[
                ft.Container(
                    content=ft.Image(
                        src=image_as_base64("Enargeia_Logo_Dark_Background.png"),
                        width=220, height=68, fit=ft.BoxFit.CONTAIN,
                        semantics_label="Enargeia – Wissen verbindet",
                    ),
                    padding=ft.Padding.symmetric(horizontal=2, vertical=8),
                    alignment=ft.Alignment(-1, 0),
                ),
                ft.Divider(color=BORDER),
                ft.Column(controls=nav_controls, spacing=2),
                ft.Container(expand=True),
                _card(ft.Column(controls=[
                    ft.Row(controls=[ft.Container(width=7, height=7, bgcolor=ACCENT, border_radius=4), ft.Text("GRAPH ENGINE", size=9, weight=ft.FontWeight.BOLD, color=ACCENT)], spacing=7),
                    ft.Text("Neo4j-first architecture", size=11, weight=ft.FontWeight.W_600, color=TEXT),
                    ft.Text("Native graph · GDS · MCP", size=10, color=MUTED),
                    ft.Text("Keine Quelldateien werden verändert.", size=9, color=SUBTLE),
                ], spacing=7), padding=13, bgcolor="#0B1C36", border_color="#1A3554", radius=12),
                ft.Text(f"ENARGEIA  •  {__version__}", size=8, color=SUBTLE, text_align=ft.TextAlign.CENTER),
            ], expand=True, spacing=4,
        ),
    )

    header_status_dot = ft.Container(width=7, height=7, bgcolor=SUBTLE, border_radius=4)
    header = ft.Container(
        padding=ft.Padding.symmetric(horizontal=26, vertical=15),
        bgcolor=HEADER,
        border=ft.Border.only(bottom=ft.BorderSide(1, BORDER)),
        content=ft.Row(controls=[
            ft.Column(controls=[_eyebrow("ENARGEIA", SUBTLE), header_title, header_subtitle], spacing=3, expand=True),
            ft.Container(content=ft.Row(controls=[header_status_dot, global_status], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER), bgcolor=PANEL_ALT, border=ft.Border.all(1, BORDER), border_radius=20, padding=ft.Padding.symmetric(horizontal=12, vertical=8)),
            ft.IconButton(icon=_safe_icon("REFRESH_ROUNDED"), tooltip="Graph-Bestand aktualisieren", icon_color=ACCENT, on_click=lambda e: page.run_task(load_dashboard_task)),
            ft.IconButton(icon=ft.Icons.SETTINGS_OUTLINED, tooltip="System & MCP", icon_color=MUTED, on_click=lambda e: navigate("connection")),
        ], vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=14),
    )
    main_area = ft.Column(controls=[header, view_host], spacing=0, expand=True)
    page.add(ft.Row(controls=[sidebar, main_area], spacing=0, expand=True, vertical_alignment=ft.CrossAxisAlignment.STRETCH))
    navigate("dashboard")

    def root_entry_card(path: str, name: str, signature: str) -> ft.Control:
        return _card(
            ft.Row(
                controls=[
                    _icon_tile(_safe_icon("FOLDER_SPECIAL_OUTLINED"), BLUE, size=40),
                    ft.Column(
                        controls=[
                            ft.Text(name, size=13, weight=ft.FontWeight.W_600, color=TEXT),
                            ft.Text(path, size=10, color=MUTED, selectable=True),
                            _status_pill(signature, ACCENT),
                        ], spacing=5, expand=True,
                    ),
                    ft.Button(content="Aktualisieren", icon=_safe_icon("SYNC_ROUNDED"), on_click=lambda e, p=path: use_root_for_scan(p)),
                    ft.Button(content="Aus Index löschen", icon=_safe_icon("DELETE_OUTLINE"), on_click=lambda e, p=path, n=name: request_delete_root(p, n), color=BAD),
                ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ), padding=12, bgcolor=PANEL_ALT, border_color=BORDER, radius=12,
        )

    async def load_dashboard_data_into_controls() -> None:
        try:
            stats, roots = await asyncio.to_thread(_load_dashboard_data)
            for key in stat_values:
                stat_values[key].value = str(stats.get(key, 0))
            for key, control in quality_stat_values.items():
                control.value = str(stats.get(key, 0))
            overview_message.value = "Graph-Bestand aktualisiert."
            overview_message.color = GOOD
            global_status.value = "Neo4j verbunden"
            global_status.color = GOOD
            try:
                header_status_dot.bgcolor = GOOD
            except NameError:
                pass
            source_root_list.controls.clear()
            if not roots:
                empty_text = "Noch keine Roots indexiert. Wähle ein Verzeichnis und starte den ersten Scan."
                source_root_list.controls.append(_card(ft.Text(empty_text, size=12, color=MUTED), bgcolor=PANEL_ALT, border_color=BORDER, radius=10))
                source_registry_status.value = "Keine Roots registriert"
                source_registry_status.color = MUTED
            else:
                for root in roots:
                    path = str(root.get("path") or "")
                    name = str(root.get("name") or Path(path).name or path)
                    sig = str(root.get("embedding_signature") or "Embedding unbekannt")
                    source_root_list.controls.append(root_entry_card(path, name, sig))
                source_registry_status.value = f"{len(roots)} registrierte Roots"
                source_registry_status.color = GOOD
        except Exception as exc:
            overview_message.value = f"Neo4j nicht erreichbar oder Konfiguration unvollständig: {exc}"
            overview_message.color = BAD
            global_status.value = "Verbindung prüfen"
            global_status.color = BAD
            try:
                header_status_dot.bgcolor = BAD
            except NameError:
                pass
            for key in stat_values:
                stat_values[key].value = "–"
            for control in quality_stat_values.values():
                control.value = "–"
            source_registry_status.value = "Root-Registry konnte nicht geladen werden"
            source_registry_status.color = BAD
            if not source_root_list.controls:
                source_root_list.controls.append(_card(ft.Text("Die Root-Registry wird nach erfolgreicher Verbindung geladen.", size=12, color=MUTED), bgcolor=PANEL_ALT, border_color=BORDER, radius=10))
        page.update()

    async def load_dashboard_task() -> None:
        overview_message.value = "Lade Bestand aus Neo4j …"
        overview_message.color = WARN
        page.update()
        await load_dashboard_data_into_controls()

    def use_root_for_scan(path: str) -> None:
        directory_field.value = path
        navigate("scan")

    delete_state: dict[str, bool] = {"running": False}

    async def delete_root_task(path: str, name: str) -> None:
        if scan_state["running"]:
            source_registry_status.value = "Ordner kann während eines laufenden Scans nicht entfernt werden."
            source_registry_status.color = WARN
            page.update()
            return
        if delete_state["running"]:
            return
        delete_state["running"] = True
        source_registry_status.value = f"Entferne den Index für '{name}' aus Neo4j …"
        source_registry_status.color = WARN
        page.update()
        try:
            await asyncio.to_thread(_run_delete_root, path)
            await load_dashboard_data_into_controls()
            source_registry_status.value = (
                f"'{name}' wurde aus dem Knowledge Graph entfernt. Die Quelldateien blieben unverändert."
            )
            source_registry_status.color = GOOD
            logging.getLogger("winkg.gui").info("Root-Löschung in der GUI abgeschlossen: %s", path)
        except Exception as exc:
            source_registry_status.value = f"Ordner konnte nicht aus dem Index entfernt werden: {exc}"
            source_registry_status.color = BAD
            logging.getLogger("winkg.gui").exception("Entfernen eines indexierten Roots fehlgeschlagen: %s", path)
        finally:
            delete_state["running"] = False
            page.update()

    def request_delete_root(path: str, name: str) -> None:
        if scan_state["running"]:
            source_registry_status.value = "Bitte warte, bis der laufende Scan beendet ist."
            source_registry_status.color = WARN
            page.update()
            return
        dialog: ft.AlertDialog

        def cancel_delete(_: Any) -> None:
            _close_dialog_compat(page, dialog)

        def confirm_delete(_: Any) -> None:
            _close_dialog_compat(page, dialog)
            page.run_task(delete_root_task, path, name)

        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("Indexierten Ordner entfernen?", color=TEXT),
            content=ft.Text(
                f"Der Knowledge Graph für diesen Root wird aus Neo4j entfernt:\n\n{path}\n\n"
                "Das löscht keine Dateien und verändert den Ordner auf deinem Laufwerk nicht. "
                "Die Aktion lässt sich nicht rückgängig machen; du kannst den Ordner später erneut indexieren.",
                color=TEXT,
                selectable=True,
            ),
            actions=[
                ft.Button(content="Abbrechen", on_click=cancel_delete),
                ft.Button(content="Aus Index löschen", on_click=confirm_delete, color=BAD),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        _show_dialog_compat(page, dialog)

    # Start with a live database check; failures are presented in the dashboard,
    # not raised out of the UI event loop.
    page.run_task(load_dashboard_task)


if __name__ == "__main__":
    ft.run(main)
