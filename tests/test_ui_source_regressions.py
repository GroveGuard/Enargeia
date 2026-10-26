from pathlib import Path


GUI_SOURCE = Path(__file__).parents[1] / "src" / "winkg" / "gui.py"


def test_graph_explorer_launches_a_native_animated_3d_viewer():
    source = GUI_SOURCE.read_text(encoding="utf-8")
    viewer = GUI_SOURCE.parents[0] / "graph3d_window.py"
    html = GUI_SOURCE.parents[0] / "ui" / "graph3d.html"
    viewer_source = viewer.read_text(encoding="utf-8")
    html_source = html.read_text(encoding="utf-8")
    assert 'open_graph3d_viewer({}, "")' in source
    assert "Die Wissensgalaxie live erleben" in source
    assert "graph3d_launch_panel()," not in source  # launcher is created once at the top of the Graph Explorer
    assert "winkg.graph3d_window" in source
    assert "pywebview" in viewer_source
    assert "build_database_payload(snapshot)" in viewer_source
    assert "get_graph_update" in viewer_source and "load_database_graph" in viewer_source
    assert "def load_graph(" not in viewer_source  # obsolete SVG-neighborhood bridge is removed
    assert "canvas.getContext('webgl'" in html_source
    assert "requestAnimationFrame(render)" in html_source
    assert "function simulate(dt)" in html_source
    assert "function labelPriority(n)" in html_source
    assert "labelCandidates.sort((a,b)=>b.priority-a.priority)" in html_source
    assert "const collides=placed.some" in html_source
    assert "pointermove" in html_source and "cameraDistance" in html_source
    assert all(color in html_source for color in ("#11C5E8", "#1677FF", "#F2B95D", "#8057F5"))


def test_navigation_caches_page_control_trees():
    source = GUI_SOURCE.read_text(encoding="utf-8")
    assert "view_cache: dict[str, ft.Control] = {}" in source
    assert "if key not in view_cache:" in source
    assert "view_host.content = view_cache[key]" in source


def test_dashboard_has_no_duplicate_next_actions_or_root_registry():
    source = GUI_SOURCE.read_text(encoding="utf-8")
    dashboard = source.split("    # ---------------- Scan view ----------------", 1)[0]
    assert "NEXT ACTIONS" not in dashboard
    assert "actions_panel" not in dashboard
    assert "roots_panel" not in dashboard
    assert "LLM-Zugriff über MCP" not in dashboard
    assert 'stat_card("roots", "Datenquellen"' in dashboard
    assert "coverage_panel," in dashboard
    assert "ft.Row([coverage_panel, actions_panel]" not in dashboard

def test_dynamic_pages_render_explicit_error_states():
    source = GUI_SOURCE.read_text(encoding="utf-8")
    assert "Beziehungsvorschläge konnten nicht geladen werden" in source
    assert "Graphanalyse fehlgeschlagen" in source
    assert "Fähigkeitsprüfung fehlgeschlagen" in source


def test_wrapping_rows_do_not_contain_expanding_direct_children():
    """Flutter Wrap cannot host Expanded children; this renders as a gray ErrorWidget."""
    import ast

    tree = ast.parse(GUI_SOURCE.read_text(encoding="utf-8"))
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "Row"):
            continue
        wrapped = any(
            kw.arg == "wrap" and isinstance(kw.value, ast.Constant) and kw.value.value is True
            for kw in node.keywords
        )
        controls_kw = next((kw for kw in node.keywords if kw.arg == "controls"), None)
        if not wrapped or controls_kw is None or not isinstance(controls_kw.value, (ast.List, ast.Tuple)):
            continue
        for child in controls_kw.value.elts:
            if isinstance(child, ast.Call) and any(
                kw.arg == "expand" and isinstance(kw.value, ast.Constant) and kw.value.value is True
                for kw in child.keywords
            ):
                violations.append((getattr(node, "lineno", 0), getattr(child, "lineno", 0)))
    assert not violations, f"Expanded child in wrapping Row: {violations}"


def test_dynamic_output_columns_are_not_inside_plain_gray_placeholder_containers():
    source = GUI_SOURCE.read_text(encoding="utf-8")
    # The dynamic outputs are now attached directly to their owning page/card trees.
    # Extra wrappers were rendering as blank gray native error surfaces in Flet 1.x.
    assert "ft.Container(content=root_list, bgcolor=BG" not in source
    assert "content=source_root_list" not in source
    assert "content=quality_output" not in source
    assert "content=connection_details" not in source


def test_dashboard_layout_is_single_column_after_metrics():
    source = GUI_SOURCE.read_text(encoding="utf-8")
    dashboard = source.split("    # ---------------- Scan view ----------------", 1)[0]
    assert "coverage_panel.expand" not in dashboard
    assert "actions_panel.expand" not in dashboard
    assert "operation_status," in dashboard
    assert "                coverage_panel," in dashboard
    assert "roots_panel" not in dashboard


def test_root_registry_is_owned_only_by_data_sources_page():
    source = GUI_SOURCE.read_text(encoding="utf-8")
    dashboard = source.split("    def build_dashboard_view() -> ft.Control:", 1)[1].split("    # ---------------- Scan view ----------------", 1)[0]
    assert "source_root_list" not in dashboard
    scan_view = source.split("    def build_scan_view() -> ft.Control:", 1)[1].split("    def clear_scan_log()", 1)[0]
    assert "source_root_list" in scan_view
    assert "source_registry_status" in scan_view


def test_page_content_does_not_repeat_global_page_title_banners():
    source = GUI_SOURCE.read_text(encoding="utf-8")
    assert "def build_graph_view()" in source
    graph = source.split("    def build_graph_view() -> ft.Control:", 1)[1].split("    # ---------------- Graph Intelligence view ----------------", 1)[0]
    quality = source.split("    def build_quality_view() -> ft.Control:", 1)[1].split("    def build_graph_intelligence_view()", 1)[0]
    intelligence = source.split("    def build_graph_intelligence_view() -> ft.Control:", 1)[1].split("    # ---------------- Connection view ----------------", 1)[0]
    connection = source.split("    def build_connection_view() -> ft.Control:", 1)[1].split("    # ---------------- Main navigation", 1)[0]
    assert 'ft.Text("Graph Explorer", size=23' not in graph
    assert 'ft.Text("Qualität & Beziehungen", size=23' not in quality
    assert 'ft.Text("Graph-Analysen", size=23' not in intelligence
    assert 'ft.Text("System & MCP", size=23' not in connection


def test_mcp_has_a_dedicated_setup_page_and_tool_safety_overview():
    source = GUI_SOURCE.read_text(encoding="utf-8")
    assert '"mcp": build_mcp_view' in source
    assert '("mcp", "MCP-Integration"' in source
    assert "Serverstart und Werkzeuge prüfen" in source
    assert "Werkzeuge und Zugriffsarten" in source
    assert "Schreibt Analysebefunde" in source
    assert "Ändert Prüfstatus" in source
    assert "Einrichtung bestätigen" in source
    assert "Letzte Änderung zurücksetzen" in source
    assert "JSON-Fallback" in source
    for tool_name in (
        "search_knowledge",
        "global_search_knowledge",
        "get_file_context",
        "find_related_files",
        "get_graph_stats",
        "get_graph_capabilities",
        "analyze_knowledge_graph",
        "list_graph_link_candidates",
        "review_graph_link_candidate",
        "list_graph_assertions",
        "get_graph_insights",
        "get_files_needing_deep_analysis",
    ):
        assert f'"{tool_name}"' in source
    assert "ft.ResponsiveRow(" in source
    assert 'col={"sm": 12, "md": 6}' in source
    manual_setup = source.split("    def build_mcp_manual_section() -> ft.Control:", 1)[1].split(
        "    async def mcp_probe_task()", 1
    )[0]
    assert "ft.ExpansionTile(" in manual_setup
    assert '"Danach verbinden"' not in manual_setup
    assert "Konfiguration kopieren" in manual_setup
    assert 'font_family="Consolas"' in manual_setup
    assert "selectable=True" in manual_setup
    assert "height=190" in manual_setup
    assert "horizontal_alignment=ft.CrossAxisAlignment.STRETCH" in manual_setup
    assert "], alignment=ft.MainAxisAlignment.END, spacing=0)," in manual_setup
    assert "controls_padding=ft.Padding.symmetric(horizontal=12, vertical=2)" in manual_setup


def test_dashboard_advanced_statistics_have_an_explicit_visible_toggle():
    source = GUI_SOURCE.read_text(encoding="utf-8")
    dashboard = source.split("    # ---------------- Scan view ----------------", 1)[0]
    assert '"Erweiterte Statistik anzeigen"' in dashboard
    assert "graph_stats.visible = False" in dashboard
    assert "graph_stats.visible = not bool(graph_stats.visible)" in dashboard
    assert "on_click=toggle_graph_stats" in dashboard
    # Avoid a framework-native ExpansionTile that was not discoverable in the user's UI.
    assert "ft.ExpansionTile(" not in dashboard


def test_dashboard_statistics_toggle_is_in_the_top_section_not_below_status():
    source = GUI_SOURCE.read_text(encoding="utf-8")
    dashboard = source.split("    def build_dashboard_view() -> ft.Control:", 1)[1].split("    # ---------------- Scan view ----------------", 1)[0]
    assert "return ft.Column(" in dashboard
    assert 'ft.Text("GRAPH AT A GLANCE"' in dashboard
    assert "graph_stats_toggle," in dashboard
    assert dashboard.index("graph_stats_toggle,") < dashboard.index("primary_stats,")
    assert dashboard.index("graph_stats,") < dashboard.index("operation_status,")
    assert '"Erweiterte Statistik anzeigen"' in dashboard
    assert "ft.ExpansionTile(" not in dashboard


def test_graph_explorer_text_summary_is_independent_from_full_database_galaxy():
    source = GUI_SOURCE.read_text(encoding="utf-8")
    graph = source.split("    def build_graph_view() -> ft.Control:", 1)[1].split("    # ---------------- Graph Intelligence view ----------------", 1)[0]
    task = source.split("    async def graph_task() -> None:", 1)[1].split("    def build_graph_view()", 1)[0]
    assert "galaxy_panel = graph3d_launch_panel()" in graph
    assert "return ft.Column(controls=[galaxy_panel, explorer_controls, graph_results]" in graph
    assert "open_graph3d_viewer" not in task
    assert "graph_query" in graph
    assert "SVG" not in graph


def test_dashboard_is_not_a_second_unbounded_scroll_surface():
    source = GUI_SOURCE.read_text(encoding="utf-8")
    # Do not add a page-wide scroll override; the Overview itself is intentionally
    # a non-scrollable Column so the mouse wheel cannot scroll into blank space.
    assert "page.scroll = None" not in source
    dashboard = source.split("    def build_dashboard_view() -> ft.Control:", 1)[1].split("    # ---------------- Scan view ----------------", 1)[0]
    assert "return ft.Column(" in dashboard
    assert "return ft.ListView(" not in dashboard
    assert "scroll=ft.ScrollMode.AUTO" not in dashboard



def test_data_source_cards_have_equal_bounded_heights_without_stretch_constraints():
    source = GUI_SOURCE.read_text(encoding="utf-8")
    scan_view = source.split("    def build_scan_view() -> ft.Control:", 1)[1].split("    def clear_scan_log()", 1)[0]
    assert "ft.Container(content=selection_panel, expand=7, height=620)" in scan_view
    assert "ft.Container(content=logs_panel, expand=5, height=620)" in scan_view
    assert "vertical_alignment=ft.CrossAxisAlignment.START" in scan_view
    assert "vertical_alignment=ft.CrossAxisAlignment.STRETCH" not in scan_view
    assert "spacing=13, scroll=ft.ScrollMode.AUTO, expand=True" in scan_view


def test_fastembed_download_progress_is_visible_in_connection_and_scan_views():
    source = GUI_SOURCE.read_text(encoding="utf-8")
    assert "connection_embedding_progress_panel" in source
    assert "scan_embedding_progress_panel" in source
    assert "ft.ProgressBar(value=None" in source
    assert "refresh_embedding_progress_controls()" in source
    assert "asyncio.to_thread(_run_doctor, True, publish_embedding_progress)" in source
    assert "DirectoryIndexer(settings, embedding_progress_callback=publish_embedding_progress)" in source
    assert "Modell herunterladen" in source or "Modell-Dateien werden heruntergeladen" in source
