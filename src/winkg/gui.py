from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import subprocess
from logging.handlers import RotatingFileHandler
import threading
from pathlib import Path
from typing import Any, Callable

import flet as ft

from .config import PROJECT_ROOT, get_settings
from .embeddings import EmbeddingClient
from .graph_intelligence import GraphIntelligence
from .indexer import DirectoryIndexer
from .neo4j_store import Neo4jStore
from .search import KnowledgeSearch

APP_NAME = "Windows Knowledge Graph"
BG = "#0B1020"
SIDEBAR = "#111827"
PANEL = "#151F30"
PANEL_ALT = "#1B293D"
BORDER = "#29364B"
TEXT = "#E5E7EB"
MUTED = "#9CA3AF"
ACCENT = "#60A5FA"
GOOD = "#4ADE80"
WARN = "#FBBF24"
BAD = "#F87171"


def _configure_logging() -> Path:
    """Write application and scan diagnostics to a rotating project-local log."""
    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "winkg-gui.log"
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
        "Chat-Modell": settings.llm_model,
        "Chat-Endpunkt": settings.llm_base_url,
    }


def _root_id_from_path(store: Neo4jStore, value: str | None) -> str | None:
    if not value or not value.strip():
        return None
    return store.root_id_for(Path(value.strip()).expanduser().resolve())


def _run_search(query: str, limit: int, root_path: str) -> list[dict[str, Any]]:
    settings = get_settings()
    settings.validate_neo4j()
    searcher = KnowledgeSearch(settings)
    try:
        root_id = _root_id_from_path(searcher.store, root_path)
        return searcher.search(query, limit=limit, root_id=root_id)
    finally:
        searcher.close()


def _run_global_search(query: str, limit: int, root_path: str) -> dict[str, Any]:
    settings = get_settings()
    settings.validate_neo4j()
    searcher = KnowledgeSearch(settings)
    try:
        root_id = _root_id_from_path(searcher.store, root_path)
        return searcher.global_search(query, limit=limit, root_id=root_id)
    finally:
        searcher.close()


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


def _run_doctor(test_embeddings: bool) -> dict[str, Any]:
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
    if test_embeddings:
        client = EmbeddingClient(settings)
        vector = client.embed_one("Verbindungstest für den Knowledge Graph")
        result["embedding"] = {
            "status": "OK",
            "model": client.signature,
            "dimensions": len(vector),
        }
    return result


def _run_graph_query(query: str, root_path: str) -> dict[str, Any] | None:
    settings = get_settings()
    settings.validate_neo4j()
    store = Neo4jStore(settings)
    try:
        root_id = _root_id_from_path(store, root_path)
        return store.graph_neighborhood(query, root_id=root_id, limit=12)
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
        return GraphIntelligence(store).analyze(run_gds=run_gds)
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


def _card(content: ft.Control, *, padding: int = 18, bgcolor: str = PANEL, width: int | None = None) -> ft.Container:
    return ft.Container(
        content=content,
        padding=padding,
        bgcolor=bgcolor,
        border_radius=14,
        width=width,
    )


def _section_title(title: str, subtitle: str | None = None) -> ft.Column:
    controls: list[ft.Control] = [ft.Text(title, size=18, weight=ft.FontWeight.W_600, color=TEXT)]
    if subtitle:
        controls.append(ft.Text(subtitle, size=12, color=MUTED))
    return ft.Column(controls=controls, spacing=4)


def main(page: ft.Page) -> None:
    _configure_logging()
    page.title = APP_NAME
    page.theme_mode = ft.ThemeMode.DARK
    page.bgcolor = BG
    page.padding = 0
    page.spacing = 0

    scan_log_queue: queue.Queue[str] = queue.Queue()
    scan_state: dict[str, Any] = {"running": False, "result": None, "error": None, "cancel_event": None}

    # ---------------- Shared controls ----------------
    header_title = ft.Text("Dashboard", size=23, weight=ft.FontWeight.BOLD, color=TEXT)
    header_subtitle = ft.Text("Unternehmenswissen in Neo4j indexieren, verknüpfen und analysieren", size=12, color=MUTED)
    global_status = ft.Text("Verbindung noch nicht geprüft", size=12, color=MUTED)
    view_host = ft.Container(expand=True, padding=22, bgcolor=BG)

    stat_values: dict[str, ft.Text] = {}
    for key in ("roots", "folders", "files", "chunks", "entities", "assertions", "truncated_files", "insights", "link_candidates", "communities", "components", "semantic_similarity_edges"):
        stat_values[key] = ft.Text("–", size=26, weight=ft.FontWeight.BOLD, color=TEXT)

    overview_message = ft.Text("Verbinde dich mit Neo4j, um den aktuellen Bestand zu laden.", size=12, color=MUTED)
    root_list = ft.Column(spacing=8, controls=[])

    directory_field = ft.TextField(
        label="Windows-Verzeichnis",
        hint_text=r"z. B. C:\MS_VSCode_Workspace\Fletapps_HA",
        value="",
        expand=True,
        text_size=13,
        border=ft.OutlineInputBorder(border_radius=10),
    )
    root_filter_search = ft.TextField(label="Root-Pfad (optional)", hint_text="Leer lassen für alle Roots", expand=True, text_size=12)
    search_query = ft.TextField(
        label="Was möchtest du wissen?",
        hint_text="z. B. Welche Module schreiben in die SQLite-Datenbank?",
        expand=True,
        on_submit=lambda e: page.run_task(search_task),
    )
    graph_query = ft.TextField(
        label="Dateiname oder Pfad",
        hint_text="z. B. main.py oder src/winkg/indexer.py",
        expand=True,
        on_submit=lambda e: page.run_task(graph_task),
    )
    result_limit = ft.TextField(label="Treffer (5–20)", value="8", width=135, text_size=12)
    global_search_checkbox = ft.Checkbox(
        label="Global Search: Zusammenfassung über Communities + Originalquellen (LLM)", value=False
    )
    force_checkbox = ft.Checkbox(label="Alle Dateien erneut verarbeiten", value=False)
    extract_entities_checkbox = ft.Checkbox(label="Entitäten und Beziehungen per Chat-LLM extrahieren", value=False)
    no_embeddings_checkbox = ft.Checkbox(label="Ohne Embeddings (nur Graph/Volltext)", value=False)
    include_ignored_checkbox = ft.Checkbox(label="Standardmäßig ausgeschlossene Ordner einbeziehen", value=False)
    include_sensitive_checkbox = ft.Checkbox(label="Sensible Dateien ausdrücklich einschließen", value=False)
    rebuild_confirm_checkbox = ft.Checkbox(label="Ich bestätige, dass der Graph dieses Roots gelöscht und neu aufgebaut wird.", value=False)

    scan_status = ft.Text("Bereit für den Scan.", size=12, color=MUTED)
    scan_progress = ft.ProgressRing(width=18, height=18, visible=False, stroke_width=2)
    scan_start_button: ft.Button
    scan_rebuild_button: ft.Button
    scan_log = ft.ListView(expand=True, spacing=4, auto_scroll=True)

    search_status = ft.Text("Die Ergebnisse enthalten immer Quellenpfade, soweit verfügbar.", size=12, color=MUTED)
    search_results = ft.Column(spacing=10, controls=[])
    graph_status = ft.Text("Suche eine Datei, um ihre direkten Beziehungen zu erkunden.", size=12, color=MUTED)
    graph_results = ft.Column(spacing=12, controls=[])
    connection_status = ft.Text("Noch nicht geprüft", size=13, color=MUTED)
    connection_details = ft.Column(spacing=7, controls=[])
    graph_intelligence_status = ft.Text("Graphanalyse wurde noch nicht ausgeführt.", size=12, color=MUTED)
    graph_intelligence_output = ft.Column(spacing=9, controls=[])
    gds_analysis_checkbox = ft.Checkbox(label="GDS-Algorithmen zusätzlich ausführen (sofern installiert)", value=True)

    # ---------------- Utility rendering ----------------
    def add_notice(target: ft.Column, message: str, color: str = MUTED) -> None:
        target.controls.clear()
        target.controls.append(_card(ft.Text(message, color=color, size=13)))

    def stat_card(key: str, label: str, note: str) -> ft.Container:
        return _card(
            ft.Column(
                controls=[
                    ft.Text(label, size=12, color=MUTED),
                    stat_values[key],
                    ft.Text(note, size=11, color=MUTED),
                ],
                spacing=7,
            ),
            padding=16,
            bgcolor=PANEL_ALT,
            width=170,
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

    def result_card(row: dict[str, Any], number: int, citation_label: str | None = None) -> ft.Container:
        filename = str(row.get("filename") or Path(str(row.get("path") or "Unbekannte Datei")).name)
        path = str(row.get("path") or "")
        page_text = f" · Seite {row['page']}" if row.get("page") else ""
        section_text = f" · {row['section']}" if row.get("section") else ""
        score_text = f"RRF-Score: {row.get('score', 0)}"
        snippet = str(row.get("text") or "").strip()
        entities = row.get("entities") or []
        subtitle = f"{path}{page_text}{section_text}"
        controls: list[ft.Control] = [
            ft.Row(
                controls=[
                    ft.Text(citation_label or f"{number:02d}", color=ACCENT, weight=ft.FontWeight.BOLD, size=13),
                    ft.Column(
                        controls=[
                            ft.Text(filename, size=14, weight=ft.FontWeight.W_600, color=TEXT),
                            ft.Text(subtitle, size=11, color=MUTED, selectable=True),
                        ],
                        spacing=4,
                        expand=True,
                    ),
                    ft.Text(score_text, size=10, color=MUTED),
                ],
                vertical_alignment=ft.CrossAxisAlignment.START,
                spacing=10,
            ),
            ft.Text(snippet[:1800] or "Für diese Datei wurde kein lesbarer Text gespeichert.", size=12, color=TEXT, selectable=True),
        ]
        if row.get("needs_deep_analysis"):
            controls.append(ft.Text(
                f"Quelle gekürzt: {row.get('indexed_chunks', 0)} von {row.get('total_chunks', 0)} Chunks indexiert; "
                f"{row.get('omitted_chunks', 0)} ausgelassen. Gesonderte Analyse empfohlen.",
                size=11, color=WARN,
            ))
        if entities:
            entity_names = ", ".join(f"{item.get('name')} ({item.get('type')})" for item in entities[:8])
            controls.append(ft.Text(f"Entitäten: {entity_names}", size=11, color=ACCENT))
        controls.append(
            ft.Row(
                controls=[
                    ft.Button(
                        content="Im Graph erkunden",
                        on_click=lambda e, p=path: open_in_graph(p),
                    ),
                ],
            )
        )
        return _card(ft.Column(controls=controls, spacing=10), padding=15)

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
        stats_row = ft.Row(
            controls=[
                stat_card("roots", "Quell-Roots", "Indexierte Verzeichnisse"),
                stat_card("folders", "Ordner", "Strukturknoten"),
                stat_card("files", "Dateien", "Mit Dateimetadaten"),
                stat_card("chunks", "Textabschnitte", "Suchbare Inhalte"),
                stat_card("entities", "Entitäten", "Semantische Knoten"),
                stat_card("assertions", "Aussagen", "Mit Herkunft belegte Beziehungen"),
                stat_card("truncated_files", "Analyse erforderlich", "Zu groß oder gekürzt"),
                stat_card("insights", "Graph-Befunde", "Gespeicherte Analyseergebnisse"),
                stat_card("link_candidates", "Hypothesen", "Prüfbare Beziehungsvorschläge"),
                stat_card("communities", "Communities", "GDS-Gruppierungen"),
                stat_card("components", "Komponenten", "Zusammenhängende Graphinseln (GDS WCC)"),
                stat_card("semantic_similarity_edges", "Ähnlichkeitskanten", "GDS-KNN zwischen Textabschnitten"),
            ],
            wrap=True,
            spacing=12,
            run_spacing=12,
        )
        # Use an intrinsically sized vertical layout here. An expanding Column
        # inside a Row can occupy the whole available height in Flet 1.x and
        # render as a large empty grey panel in the desktop window.
        connection_panel = _card(
            ft.Column(
                controls=[
                    ft.Text("Datenbankstatus", size=15, weight=ft.FontWeight.W_600, color=TEXT),
                    overview_message,
                    ft.Row(
                        controls=[
                            ft.Button(content="Bestand aktualisieren", on_click=lambda e: page.run_task(load_dashboard_task)),
                            ft.Button(content="Verbindung prüfen", on_click=lambda e: page.run_task(doctor_task)),
                        ],
                        wrap=True,
                        spacing=12,
                    ),
                ],
                spacing=10,
            )
        )
        roots_panel = _card(
            ft.Column(
                controls=[
                    _section_title("Indexierte Verzeichnisse", "Ein Root auswählen, um es im Scan-Bereich zu aktualisieren."),
                    root_list,
                ],
                spacing=13,
            )
        )
        quick_panel = _card(
            ft.Column(
                controls=[
                    _section_title("Schnellstart", "Indexiere zunächst ein überschaubares Projektverzeichnis."),
                    ft.Text("1. Verzeichnis im Bereich Scan auswählen.  2. Inkrementellen Scan starten.  3. Inhalte unter Suche und Graph erkunden.", size=12, color=MUTED),
                    ft.Button(content="Zum Scan wechseln", on_click=lambda e: navigate("scan")),
                ],
                spacing=10,
            )
        )
        return ft.Column(
            controls=[
                _section_title("Überblick", "Status deiner lokalen oder entfernten Neo4j-Wissensbasis"),
                stats_row,
                connection_panel,
                roots_panel,
                quick_panel,
            ],
            spacing=16,
            scroll=ft.ScrollMode.AUTO,
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
                indexer = DirectoryIndexer(settings)
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

        threading.Thread(target=worker, name="win-kg-scan", daemon=True).start()
        page.run_task(monitor_scan_task)
        page.update()

    async def monitor_scan_task() -> None:
        while scan_state["running"]:
            drain_scan_logs()
            await asyncio.sleep(0.3)
        drain_scan_logs(max_count=5000)
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

    scan_start_button = ft.Button(content="Inkrementell indexieren", on_click=lambda e: start_scan(False))
    scan_rebuild_button = ft.Button(content="Root neu aufbauen", on_click=lambda e: start_scan(True))
    def request_scan_cancel(_: Any) -> None:
        event = scan_state.get("cancel_event")
        if scan_state.get("running") and event is not None:
            event.set()
            scan_cancel_button.disabled = True
            scan_status.value = "Abbruch angefordert … Die aktuelle Datei wird noch sicher abgeschlossen."
            scan_status.color = WARN
            page.update()
    scan_cancel_button = ft.Button(content="Scan abbrechen / pausieren", on_click=request_scan_cancel, disabled=True)

    def build_scan_view() -> ft.Control:
        selection_panel = _card(
            ft.Column(
                controls=[
                    _section_title("Quelle auswählen", "Ein kompletter Scan liest keine ausgeschlossenen Ordner und verändert keine Quelldateien."),
                    ft.Row(controls=[directory_field, ft.Button(content="Ordner auswählen …", on_click=pick_directory)], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    ft.Row(controls=[force_checkbox, no_embeddings_checkbox], wrap=True, spacing=18, run_spacing=6),
                    ft.Row(controls=[extract_entities_checkbox], wrap=True),
                    ft.Text("Sensible Dateien können Zugangsdaten enthalten. Nur bei bewusstem Bedarf einschalten.", size=11, color=WARN),
                    ft.Row(controls=[include_ignored_checkbox, include_sensitive_checkbox], wrap=True, spacing=18, run_spacing=6),
                    ft.Row(controls=[scan_start_button, scan_rebuild_button, scan_cancel_button, scan_progress, scan_status], wrap=True, spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    rebuild_confirm_checkbox,
                ],
                spacing=13,
            )
        )
        logs_panel = _card(
            ft.Column(
                controls=[
                    ft.Row(controls=[_section_title("Scan-Protokoll", "Live-Logs und abschließende Importstatistik"), ft.Button(content="Protokoll leeren", on_click=lambda e: clear_scan_log())], alignment=ft.MainAxisAlignment.SPACE_BETWEEN, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    ft.Container(content=scan_log, height=300, padding=8, bgcolor=BG, border_radius=8),
                ],
                spacing=12,
            )
        )
        return ft.Column(controls=[_section_title("Indexierung", "Dateien synchronisieren und für GraphRAG vorbereiten"), selection_panel, logs_panel], spacing=16, scroll=ft.ScrollMode.AUTO, expand=True)

    def clear_scan_log() -> None:
        scan_log.controls.clear()
        page.update()

    # ---------------- Search view ----------------
    def search_click(_: Any) -> None:
        if not (search_query.value or "").strip():
            search_status.value = "Bitte eine Frage oder Suchanfrage eingeben."
            search_status.color = WARN
            page.update()
            return
        search_status.value = "Suche läuft …"
        search_status.color = WARN
        search_results.controls.clear()
        page.update()
        page.run_task(search_task)

    async def search_task() -> None:
        query = (search_query.value or "").strip()
        root_filter = (root_filter_search.value or "").strip()
        try:
            limit = max(5, min(20, int(result_limit.value or "8")))
            search_results.controls.clear()
            if global_search_checkbox.value:
                result = await asyncio.to_thread(_run_global_search, query, limit, root_filter)
                answer = str(result.get("answer") or "Keine Antwort verfügbar.")
                search_results.controls.append(_card(
                    ft.Column(
                        controls=[
                            ft.Text("Global Search · Antwort", size=15, weight=ft.FontWeight.W_600, color=TEXT),
                            ft.Text(answer, size=13, color=TEXT, selectable=True),
                            *([ft.Text("Offene Punkte: " + " · ".join(str(x) for x in result.get("uncertainties", [])[:5]),
                                      size=11, color=WARN, selectable=True)] if result.get("uncertainties") else []),
                            ft.Text(
                                f"Status: {result.get('status', 'unknown')} · "
                                f"Community-Zusammenfassungen: {result.get('community_summaries_used', len(result.get('communities') or []))} · "
                                f"Quellabschnitte: {len(result.get('sources') or [])}",
                                size=10, color=MUTED,
                            ),
                        ], spacing=9,
                    ), padding=16,
                ))
                for community_index, community in enumerate(result.get("communities") or [], start=1):
                    details = community.get("details") or {}
                    summary = str(details.get("summary") or community.get("description") or "")
                    key_points = details.get("key_points") or []
                    caveats = details.get("caveats") or []
                    source_paths = details.get("source_paths") or []
                    community_controls: list[ft.Control] = [
                        ft.Text(f"C{community_index} · {community.get('title') or 'Community'}", size=13, weight=ft.FontWeight.W_600, color=TEXT),
                        ft.Text(summary, size=12, color=TEXT, selectable=True),
                    ]
                    if key_points:
                        community_controls.append(ft.Text("Kernaussagen: " + " · ".join(str(x) for x in key_points[:4]), size=11, color=ACCENT, selectable=True))
                    if caveats:
                        community_controls.append(ft.Text("Einschränkungen: " + " · ".join(str(x) for x in caveats[:3]), size=11, color=WARN, selectable=True))
                    if source_paths:
                        community_controls.append(ft.Text("Stützende Quellen: " + " · ".join(str(x) for x in source_paths[:4]), size=10, color=MUTED, selectable=True))
                    search_results.controls.append(_card(ft.Column(controls=community_controls, spacing=7), padding=14, bgcolor=PANEL_ALT))
                sources = result.get("sources") or []
                if sources:
                    search_results.controls.append(_section_title("Zugehörige Originalquellen", "Die Quellabschnitte sind unabhängig von der LLM-Zusammenfassung abrufbar."))
                    search_results.controls.extend(
                        result_card(row, number, citation_label=f"S{number}")
                        for number, row in enumerate(sources, start=1)
                    )
                elif not result.get("communities"):
                    search_results.controls.append(ft.Text("Keine passenden Community-Zusammenfassungen oder Originalquellen gefunden.", size=12, color=MUTED))
                summary_count = len(result.get("communities") or [])
                source_count = len(sources)
                search_status.value = f"Global Search abgeschlossen · {summary_count} Communities · {source_count} Quellabschnitte · Status {result.get('status', 'unknown')}"
                search_status.color = GOOD if result.get("status") == "completed" else WARN
            else:
                results = await asyncio.to_thread(_run_search, query, limit, root_filter)
                if not results:
                    add_notice(search_results, "Keine Treffer. Prüfe, ob der Root indexiert wurde und ob die Begriffe im Volltext vorkommen.")
                    search_status.value = "Suche abgeschlossen · keine Treffer"
                    search_status.color = MUTED
                else:
                    search_results.controls.extend(result_card(row, number) for number, row in enumerate(results, start=1))
                    search_status.value = f"{len(results)} Treffer · Hybrid Search kombiniert Volltext- und Vektortreffer."
                    search_status.color = GOOD
        except Exception as exc:
            add_notice(search_results, f"Suche fehlgeschlagen: {exc}", BAD)
            search_status.value = "Fehler bei der Suche"
            search_status.color = BAD
        page.update()

    def open_in_graph(path: str) -> None:
        graph_query.value = path
        navigate("graph")
        page.run_task(graph_task)

    def build_search_view() -> ft.Control:
        controls = ft.Column(
            controls=[
                _section_title("Knowledge Search", "Semantische Suche und Volltextsuche mit überprüfbaren Quellen"),
                _card(
                    ft.Column(
                        controls=[
                            ft.Row(controls=[search_query, result_limit], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                            ft.Row(controls=[root_filter_search, ft.Button(content="Suchen", on_click=search_click)], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                            global_search_checkbox,
                            search_status,
                        ],
                        spacing=12,
                    )
                ),
                search_results,
            ],
            spacing=15,
            scroll=ft.ScrollMode.AUTO,
            expand=True,
        )
        return controls

    # ---------------- Graph explorer ----------------
    def graph_click(_: Any) -> None:
        if not (graph_query.value or "").strip():
            graph_status.value = "Bitte Dateiname oder Pfad eingeben."
            graph_status.color = WARN
            page.update()
            return
        graph_status.value = "Graph-Nachbarschaft wird geladen …"
        graph_status.color = WARN
        graph_results.controls.clear()
        page.update()
        page.run_task(graph_task)

    async def graph_task() -> None:
        query = (graph_query.value or "").strip()
        root_filter = (root_filter_search.value or "").strip()
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
                    bgcolor="#20324B",
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
        return ft.Column(
            controls=[
                _section_title("Graph Explorer", "Dateiknoten und ihre gespeicherten Beziehungen nachvollziehen"),
                _card(
                    ft.Column(
                        controls=[
                            ft.Row(controls=[graph_query, ft.Button(content="Graph erkunden", on_click=graph_click)], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                            graph_status,
                        ],
                        spacing=11,
                    )
                ),
                graph_results,
            ],
            spacing=15,
            scroll=ft.ScrollMode.AUTO,
            expand=True,
        )

    # ---------------- Graph Intelligence view ----------------
    async def graph_capabilities_task() -> None:
        graph_intelligence_status.value = "Prüfe Neo4j- und GDS-Fähigkeiten …"
        graph_intelligence_status.color = WARN
        page.update()
        try:
            report = await asyncio.to_thread(_run_graph_capabilities)
            graph_intelligence_output.controls.clear()
            graph_intelligence_output.controls.append(
                _card(ft.Text(json.dumps(report, ensure_ascii=False, indent=2, default=str),
                              size=11, color=TEXT, selectable=True))
            )
            graph_intelligence_status.value = (
                "GDS verfügbar" if report.get("gds", {}).get("available")
                else "Neo4j erreichbar · GDS nicht erkannt; Cypher-Analysen bleiben verfügbar"
            )
            graph_intelligence_status.color = GOOD if report.get("gds", {}).get("available") else WARN
        except Exception as exc:
            graph_intelligence_status.value = f"Fähigkeitsprüfung fehlgeschlagen: {exc}"
            graph_intelligence_status.color = BAD
        page.update()

    async def graph_analysis_task() -> None:
        graph_intelligence_status.value = "Graph wird analysiert. Bei großen Datenbeständen kann das dauern …"
        graph_intelligence_status.color = WARN
        graph_intelligence_output.controls.clear()
        page.update()
        try:
            report = await asyncio.to_thread(_run_graph_analysis, bool(gds_analysis_checkbox.value))
            graph_intelligence_output.controls.clear()
            graph_intelligence_output.controls.append(
                _card(ft.Text(json.dumps(report, ensure_ascii=False, indent=2, default=str),
                              size=11, color=TEXT, selectable=True))
            )
            graph_intelligence_status.value = (
                f"Analyse {report.get('status', 'beendet')} · Lauf-ID {report.get('run_id', '–')}"
            )
            graph_intelligence_status.color = GOOD if report.get("status") == "completed" else WARN
        except Exception as exc:
            graph_intelligence_status.value = f"Graphanalyse fehlgeschlagen: {exc}"
            graph_intelligence_status.color = BAD
            graph_intelligence_output.controls.append(_card(ft.Text(str(exc), size=12, color=BAD, selectable=True)))
        page.update()

    async def graph_candidates_task() -> None:
        graph_intelligence_status.value = "Lade offene Beziehungskandidaten …"
        graph_intelligence_status.color = WARN
        page.update()
        try:
            rows = await asyncio.to_thread(_run_graph_link_candidates, "proposed")
            graph_intelligence_output.controls.clear()
            if not rows:
                graph_intelligence_output.controls.append(
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
                    details = (
                        f"{subject}  —[{predicate}]→  {object_name}\n"
                        f"Verfahren: {method} · Historische Annahmequote: {calibration_text}\n"
                        f"Belege: {evidence_count} · Graph-Score: {score if score is not None else '–'}\n"
                        f"Begründung: {row.get('reason') or '–'}\n"
                        f"Kandidaten-ID: {candidate_id}"
                    )
                    graph_intelligence_output.controls.append(
                        _card(ft.Column(controls=[
                            ft.Text(details, size=12, color=TEXT, selectable=True),
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
            graph_intelligence_status.value = (
                f"{len(rows)} offene Vorschläge · Feedback wird in Neo4j gespeichert und beeinflusst weitere Analysen"
            )
            graph_intelligence_status.color = GOOD if rows else MUTED
        except Exception as exc:
            graph_intelligence_status.value = f"Kandidaten konnten nicht geladen werden: {exc}"
            graph_intelligence_status.color = BAD
        page.update()

    async def review_graph_candidate_task(candidate_id: str, decision: str) -> None:
        graph_intelligence_status.value = f"Speichere Entscheidung: {decision} …"
        graph_intelligence_status.color = WARN
        page.update()
        try:
            result = await asyncio.to_thread(_run_review_graph_link_candidate, candidate_id, decision)
            graph_intelligence_status.value = (
                f"Vorschlag {decision}: {result.get('subject_name', result.get('subject_id', '?'))} → "
                f"{result.get('object_name', result.get('object_id', '?'))}"
            )
            graph_intelligence_status.color = GOOD
            await graph_candidates_task()
        except Exception as exc:
            graph_intelligence_status.value = f"Entscheidung konnte nicht gespeichert werden: {exc}"
            graph_intelligence_status.color = BAD
            page.update()

    def build_graph_intelligence_view() -> ft.Control:
        return ft.Column(
            controls=[
                _section_title("Graph Intelligence", "Neo4j analysiert gespeichertes Wissen und hält Analyseergebnisse im Graphen fest."),
                _card(ft.Column(controls=[
                    ft.Text("Regelbasierte Graphanalyse", size=15, weight=ft.FontWeight.W_600, color=TEXT),
                    ft.Text("Nutzt Cypher und optional Neo4j GDS für Duplikate, Abdeckungslücken, Communities, Zentralität, strukturelle Ähnlichkeit und prüfbare Beziehungskandidaten. Entscheidungen werden in Neo4j gespeichert.", size=12, color=MUTED),
                    gds_analysis_checkbox,
                    ft.Row(controls=[
                        ft.Button(content="Neo4j / GDS prüfen", on_click=lambda e: page.run_task(graph_capabilities_task)),
                        ft.Button(content="Graph analysieren", on_click=lambda e: page.run_task(graph_analysis_task)),
                        ft.Button(content="Beziehungsvorschläge anzeigen", on_click=lambda e: page.run_task(graph_candidates_task)),
                    ], wrap=True, spacing=10),
                    graph_intelligence_status,
                ], spacing=10)),
                _card(ft.Column(controls=[
                    _section_title("Analyseergebnisse", "Ergebnisse sind nachvollziehbar, wiederholbar und in Neo4j gespeichert."),
                    graph_intelligence_output,
                ], spacing=10)),
            ], spacing=15, scroll=ft.ScrollMode.AUTO, expand=True
        )

    # ---------------- Connection view ----------------
    def open_env_file(_: Any) -> None:
        env_file = PROJECT_ROOT / ".env"
        if not env_file.exists():
            env_file = PROJECT_ROOT / ".env.example"
        try:
            if os.name == "nt":
                subprocess.Popen(["notepad.exe", str(env_file)])
                connection_status.value = f"In Notepad geöffnet: {env_file}"
                connection_status.color = GOOD
            else:
                connection_status.value = f"Konfigurationsdatei: {env_file}"
                connection_status.color = MUTED
        except Exception as exc:
            connection_status.value = f"Datei konnte nicht geöffnet werden: {exc}"
            connection_status.color = BAD
        page.update()

    async def doctor_task() -> None:
        global_status.value = "Prüfe Neo4j und Embeddings …"
        global_status.color = WARN
        connection_status.value = "Verbindung wird geprüft …"
        connection_status.color = WARN
        page.update()
        try:
            report = await asyncio.to_thread(_run_doctor, True)
            connection_status.value = "Neo4j und Embeddings erreichbar"
            connection_status.color = GOOD
            connection_details.controls.clear()
            connection_details.controls.append(_card(ft.Text(json.dumps(report, ensure_ascii=False, indent=2, default=str), size=11, color=TEXT, selectable=True)))
            global_status.value = "Neo4j verbunden"
            global_status.color = GOOD
            stats = report.get("stats") or {}
            for key in stat_values:
                stat_values[key].value = str(stats.get(key, 0))
        except Exception as exc:
            connection_status.value = f"Verbindung fehlgeschlagen: {exc}"
            connection_status.color = BAD
            connection_details.controls.clear()
            connection_details.controls.append(_card(ft.Text(str(exc), color=BAD, size=12, selectable=True)))
            global_status.value = "Neo4j nicht erreichbar"
            global_status.color = BAD
        page.update()

    def build_connection_view() -> ft.Control:
        rows: list[ft.Control] = []
        for key, value in _settings_preview().items():
            rows.append(
                ft.Row(
                    controls=[ft.Text(key, size=12, color=MUTED, width=170), ft.Text(value, size=12, color=TEXT, selectable=True, expand=True)],
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=12,
                )
            )
        config_panel = _card(
            ft.Column(
                controls=[
                    _section_title("Konfiguration", "Werte werden aus der Projektdatei .env eingelesen; Passwörter werden nicht angezeigt."),
                    *rows,
                    ft.Button(content=".env / Beispielkonfiguration öffnen", on_click=open_env_file),
                ],
                spacing=11,
            )
        )
        health_panel = _card(
            ft.Column(
                controls=[
                    _section_title("Systemtest", "Testet Neo4j und das konfigurierte Embedding-Modell."),
                    connection_status,
                    ft.Button(content="Neo4j + Embeddings testen", on_click=lambda e: page.run_task(doctor_task)),
                    connection_details,
                ],
                spacing=12,
            )
        )
        return ft.Column(controls=[_section_title("Verbindung & Einstellungen", "Zugangsdaten verbleiben lokal in .env"), config_panel, health_panel], spacing=16, scroll=ft.ScrollMode.AUTO, expand=True)

    # ---------------- Main navigation ----------------
    view_builders: dict[str, Callable[[], ft.Control]] = {
        "dashboard": build_dashboard_view,
        "scan": build_scan_view,
        "search": build_search_view,
        "graph": build_graph_view,
        "intelligence": build_graph_intelligence_view,
        "connection": build_connection_view,
    }
    page_titles = {
        "dashboard": "Dashboard",
        "scan": "Indexierung",
        "search": "Knowledge Search",
        "graph": "Graph Explorer",
        "intelligence": "Graph Intelligence",
        "connection": "Verbindung & Einstellungen",
    }

    def navigate(key: str) -> None:
        header_title.value = page_titles[key]
        view_host.content = view_builders[key]()
        page.update()

    nav_items = [
        ("dashboard", "01  Dashboard"),
        ("scan", "02  Scan & Import"),
        ("search", "03  Knowledge Search"),
        ("graph", "04  Graph Explorer"),
        ("intelligence", "05  Graph Intelligence"),
        ("connection", "06  Verbindung"),
    ]
    nav_buttons = ft.Column(
        controls=[
            ft.Button(content=label, on_click=lambda e, k=key: navigate(k), width=205)
            for key, label in nav_items
        ],
        spacing=7,
    )
    sidebar = ft.Container(
        width=245,
        bgcolor=SIDEBAR,
        padding=18,
        content=ft.Column(
            controls=[
                ft.Container(
                    content=ft.Column(
                        controls=[
                            ft.Text("WIN / KG", size=22, weight=ft.FontWeight.BOLD, color=ACCENT),
                            ft.Text("WINDOWS KNOWLEDGE GRAPH", size=9, color=MUTED, weight=ft.FontWeight.BOLD),
                        ],
                        spacing=3,
                    ),
                    padding=ft.Padding.only(left=5, top=6, bottom=25),
                ),
                nav_buttons,
                ft.Container(expand=True),
                ft.Divider(color=BORDER),
                ft.Text("Company Knowledge Graph", size=11, color=TEXT),
                ft.Text("Neo4j · GDS · Hybrid Search · MCP", size=10, color=MUTED),
                ft.Text("Keine Quelldateien werden verändert.", size=10, color=MUTED),
            ],
            expand=True,
        ),
    )

    header = ft.Container(
        padding=ft.Padding.symmetric(horizontal=24, vertical=16),
        bgcolor=SIDEBAR,
        content=ft.Row(
            controls=[
                ft.Column(controls=[header_title, header_subtitle], spacing=3, expand=True),
                global_status,
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=14,
        ),
    )
    main_area = ft.Column(controls=[header, view_host], spacing=0, expand=True)
    page.add(ft.Row(controls=[sidebar, main_area], spacing=0, expand=True, vertical_alignment=ft.CrossAxisAlignment.STRETCH))
    view_host.content = build_dashboard_view()
    page.update()

    async def load_dashboard_data_into_controls() -> None:
        try:
            stats, roots = await asyncio.to_thread(_load_dashboard_data)
            for key in stat_values:
                stat_values[key].value = str(stats.get(key, 0))
            overview_message.value = "Neo4j erreichbar · Der Graph-Bestand wurde aktualisiert."
            overview_message.color = GOOD
            global_status.value = "Neo4j verbunden"
            global_status.color = GOOD
            root_list.controls.clear()
            if not roots:
                root_list.controls.append(ft.Text("Noch keine Roots indexiert. Öffne Scan & Import und starte den ersten Scan.", size=12, color=MUTED))
            else:
                for root in roots:
                    path = str(root.get("path") or "")
                    name = str(root.get("name") or Path(path).name or path)
                    sig = str(root.get("embedding_signature") or "Embedding unbekannt")
                    root_list.controls.append(
                        _card(
                            ft.Row(
                                controls=[
                                    ft.Column(
                                        controls=[ft.Text(name, size=13, weight=ft.FontWeight.W_600, color=TEXT), ft.Text(path, size=10, color=MUTED, selectable=True), ft.Text(sig, size=10, color=ACCENT)],
                                        spacing=4,
                                        expand=True,
                                    ),
                                    ft.Button(content="Aktualisieren", on_click=lambda e, p=path: use_root_for_scan(p)),
                                    ft.Button(
                                        content="Aus Index löschen",
                                        on_click=lambda e, p=path, n=name: request_delete_root(p, n),
                                        color=BAD,
                                    ),
                                ],
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                spacing=10,
                            ),
                            padding=12,
                            bgcolor=PANEL_ALT,
                        )
                    )
        except Exception as exc:
            overview_message.value = f"Neo4j nicht erreichbar oder Konfiguration unvollständig: {exc}"
            overview_message.color = BAD
            global_status.value = "Verbindung prüfen"
            global_status.color = BAD
            for key in stat_values:
                stat_values[key].value = "–"
            if not root_list.controls:
                root_list.controls.append(ft.Text("Nach erfolgreicher Verbindung erscheinen hier die indexierten Roots.", size=12, color=MUTED))
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
            overview_message.value = "Ordner kann während eines laufenden Scans nicht entfernt werden."
            overview_message.color = WARN
            page.update()
            return
        if delete_state["running"]:
            return
        delete_state["running"] = True
        overview_message.value = f"Entferne den Index für '{name}' aus Neo4j …"
        overview_message.color = WARN
        page.update()
        try:
            await asyncio.to_thread(_run_delete_root, path)
            await load_dashboard_data_into_controls()
            overview_message.value = (
                f"'{name}' wurde aus dem Knowledge Graph entfernt. "
                "Die Dateien auf dem Datenträger wurden nicht verändert."
            )
            overview_message.color = GOOD
            logging.getLogger("winkg.gui").info("Root-Löschung in der GUI abgeschlossen: %s", path)
        except Exception as exc:
            overview_message.value = f"Ordner konnte nicht aus dem Index entfernt werden: {exc}"
            overview_message.color = BAD
            logging.getLogger("winkg.gui").exception("Entfernen eines indexierten Roots fehlgeschlagen: %s", path)
        finally:
            delete_state["running"] = False
            page.update()

    def request_delete_root(path: str, name: str) -> None:
        if scan_state["running"]:
            overview_message.value = "Bitte warte, bis der laufende Scan beendet ist."
            overview_message.color = WARN
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
