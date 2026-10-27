"""First-run Flet configuration wizard for Enargeia."""
from __future__ import annotations

import secrets
from typing import Any

import flet as ft
from dotenv import dotenv_values

from .config import ENV_FILE, get_settings, update_config
from .branding import image_as_base64, brand_asset_path

DB_LABEL_TO_VALUE = {"Lokal installieren": "local", "Neo4j Aura": "aura", "Externe Neo4j-Instanz": "external"}
EMBED_LABEL_TO_VALUE = {"Lokal (FastEmbed / ONNX)": "fastembed", "Vorhandenes Ollama": "ollama", "Externe Embedding-API": "openai", "Embeddings deaktivieren": "none"}
LLM_LABEL_TO_VALUE = {"Keine LLM-Anbindung": "none", "Ollama lokal bereitstellen": "ollama-local", "Vorhandenes Ollama verwenden": "ollama", "Externer OpenAI-kompatibler Endpunkt": "openai-compatible"}
DB_VALUE_TO_LABEL = {v: k for k, v in DB_LABEL_TO_VALUE.items()}
EMBED_VALUE_TO_LABEL = {v: k for k, v in EMBED_LABEL_TO_VALUE.items()}
LLM_VALUE_TO_LABEL = {v: k for k, v in LLM_LABEL_TO_VALUE.items()}
PROFILE_VALUES = {
    "Vollständig lokal (ohne verpflichtendes LLM)": ("local", "fastembed", "none"),
    "Lokale Datenbank + externe KI": ("local", "fastembed", "openai-compatible"),
    "Alles lokal inklusive LLM": ("local", "fastembed", "ollama-local"),
    "Neo4j Aura + lokale Embeddings + externe KI": ("aura", "fastembed", "openai-compatible"),
    "Individuelle Konfiguration": ("custom", "fastembed", "none"),
}

# All tunable parameters from .env.example are represented in the wizard.
ADVANCED = {
    "EMBEDDING_TIMEOUT_SECONDS": ("Embedding-Zeitlimit (Sekunden)", "180", "int"),
    "EMBEDDING_THREADS": ("Embedding-Threads (0 = automatisch)", "0", "int"),
    "EMBEDDING_BATCH_SIZE": ("Embedding-Batchgröße", "32", "int"),
    "LLM_TIMEOUT_SECONDS": ("LLM-Zeitlimit (Sekunden)", "180", "int"),
    "ENTITY_EXTRACTION_ENABLED": ("LLM-Entitätsextraktion", "false", "bool"),
    "AUTONOMOUS_QUALITY_ENABLED": ("Autonome Qualitätsprüfung", "false", "bool"),
    "COMMUNITY_SUMMARIES_ENABLED": ("Community-Zusammenfassungen", "false", "bool"),
    "GLOBAL_SEARCH_ENABLED": ("Globale Suche", "false", "bool"),
    "QUALITY_MAX_CANDIDATES_PER_RUN": ("Max. Qualitätskandidaten je Lauf", "20", "int"),
    "QUALITY_BATCH_SIZE": ("Qualitäts-Batchgröße", "5", "int"),
    "QUALITY_MIN_CONFIDENCE": ("Minimale Konfidenz (0–1)", "0.82", "float"),
    "COMMUNITY_SUMMARY_MIN_MEMBERS": ("Min. Mitglieder je Community", "3", "int"),
    "COMMUNITY_SUMMARY_MAX_PER_RUN": ("Max. Zusammenfassungen je Lauf", "8", "int"),
    "COMMUNITY_SUMMARY_BATCH_SIZE": ("Community-Batchgröße", "2", "int"),
    "CHUNK_SIZE": ("Textabschnitt-Größe", "1800", "int"),
    "CHUNK_OVERLAP": ("Überlappung der Textabschnitte", "220", "int"),
    "MAX_CHUNKS_PER_FILE": ("Max. Textabschnitte je Datei", "500", "int"),
    "MAX_FILE_SIZE_MB": ("Max. Dateigröße (MB)", "80", "int"),
    "EXTRACTION_CACHE_RETENTION_DAYS": ("Aufbewahrung Extraktions-Cache (Tage, 0 = deaktiviert)", "90", "int"),
    "GDS_MAX_ENTITY_NODES": ("GDS-Limit: Entitätsknoten", "50000", "int"),
    "GDS_MAX_NODE_SIMILARITY_NODES": ("GDS-Limit: Similarity-Knoten", "15000", "int"),
    "GDS_MAX_KNN_CHUNKS": ("GDS-Limit: KNN-Textabschnitte", "10000", "int"),
    "LOG_LEVEL": ("Protokollstufe", "INFO", "choice:DEBUG,INFO,WARNING,ERROR"),
    "EXTRA_EXCLUDED_DIRS": ("Zusätzlich ausgeschlossene Ordner (mit ; trennen)", "", "text"),
    "INCLUDE_SENSITIVE_FILES": ("Sensible Dateien standardmäßig einbeziehen", "false", "bool"),
    "INCLUDE_IGNORED_DIRS": ("Ausgeschlossene Ordner standardm\u00e4\u00dfig einbeziehen", "false", "bool"),
    "USE_DOCLING": ("Docling für Dokumente verwenden", "false", "bool"),
}


def run_setup_wizard() -> bool:
    """Show the setup wizard in Flet and persist settings atomically."""
    saved = {"value": False}
    settings = get_settings()
    has_existing_configuration = ENV_FILE.is_file()
    env = {k: str(v) for k, v in dotenv_values(ENV_FILE, interpolate=False).items() if v is not None} if ENV_FILE.is_file() else {}
    controls: dict[str, Any] = {}

    def main(page: ft.Page) -> None:
        page.title = "Enargeia – Einrichtung"
        page.window.icon = str(brand_asset_path("Enargeia.ico"))
        page.theme_mode = ft.ThemeMode.DARK
        page.bgcolor = "#071326"
        page.padding = 24
        page.window.width = 900
        page.window.height = 860
        page.window.min_width = 900
        page.window.min_height = 680
        page.window.always_on_top = True
        page.window.focused = True
        page.window.resizable = True
        status = ft.Text("", color="#91A9C8", selectable=True)
        initial_mode = settings.neo4j_mode if has_existing_configuration else "local"
        initial_port = settings.neo4j_local_port or 17687
        initial_uri = env.get("NEO4J_URI", f"neo4j://127.0.0.1:{initial_port}" if initial_mode == "local" else settings.neo4j_uri)
        db = ft.Dropdown(label="Datenbank-Anbieter", value=DB_VALUE_TO_LABEL.get(initial_mode, "Lokal installieren"), options=[ft.dropdown.Option(x) for x in DB_LABEL_TO_VALUE])
        emb = ft.Dropdown(label="Embedding-Anbieter", value=EMBED_VALUE_TO_LABEL.get(settings.embedding_provider, "Lokal (FastEmbed / ONNX)"), options=[ft.dropdown.Option(x) for x in EMBED_LABEL_TO_VALUE])
        llm = ft.Dropdown(label="LLM-Anbieter", value=LLM_VALUE_TO_LABEL.get(settings.llm_provider, "Keine LLM-Anbindung"), options=[ft.dropdown.Option(x) for x in LLM_LABEL_TO_VALUE])
        basic = {
            "NEO4J_URI": ft.TextField(label="Neo4j Bolt-URI", value=initial_uri),
            "NEO4J_USERNAME": ft.TextField(label="Neo4j-Benutzername", value=settings.neo4j_username),
            "NEO4J_PASSWORD": ft.TextField(label="Neo4j-Passwort", value=settings.neo4j_password, password=True, can_reveal_password=True),
            "NEO4J_DATABASE": ft.TextField(label="Neo4j-Datenbank", value=settings.neo4j_database),
            "NEO4J_LOCAL_PORT": ft.TextField(label="Lokaler Neo4j-Port", value=str(initial_port), keyboard_type=ft.KeyboardType.NUMBER),
            "EMBEDDING_BASE_URL": ft.TextField(label="Embedding-Endpunkt", value=settings.embedding_base_url),
            "EMBEDDING_MODEL": ft.TextField(label="Embedding-Modell", value=settings.embedding_model),
            "EMBEDDING_API_KEY": ft.TextField(label="Embedding-API-Schlüssel", value=settings.embedding_api_key, password=True, can_reveal_password=True),
            "LLM_BASE_URL": ft.TextField(label="LLM-Basis-URL", value=settings.llm_base_url),
            "LLM_MODEL": ft.TextField(label="LLM-Modell", value=settings.llm_model),
            "LLM_API_KEY": ft.TextField(label="LLM-API-Schlüssel", value=settings.llm_api_key, password=True, can_reveal_password=True),
        }
        controls.update(basic)
        for key, (label, default, kind) in ADVANCED.items():
            value = env.get(key, default)
            if kind == "bool":
                control = ft.Checkbox(label=label, value=value.strip().lower() in {"1", "true", "yes", "on"})
            elif kind.startswith("choice:"):
                control = ft.Dropdown(label=label, value=value, options=[ft.dropdown.Option(x) for x in kind.split(":", 1)[1].split(",")])
            else:
                control = ft.TextField(label=label, value=value, keyboard_type=ft.KeyboardType.NUMBER if kind in {"int", "float"} else None)
            controls[key] = control

        form_field_width = 820
        for control in (db, emb, llm, *basic.values(), *controls.values()):
            if isinstance(control, (ft.TextField, ft.Dropdown)):
                control.width = form_field_width

        gds_enabled_control = ft.Checkbox(label="GDS-Graphanalysen aktivieren", value=settings.gds_enabled)

        def field_group(title: str, fields: list[ft.Control]) -> ft.Control:
            return ft.Container(
                content=ft.Column(
                    controls=[ft.Text(title, size=13, weight=ft.FontWeight.W_600, color="#11C5E8"), *fields],
                    spacing=9,
                    horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                ),
                padding=16,
                bgcolor="#0B1C36",
                border=ft.Border.all(1, "#1A3554"),
                border_radius=10,
            )

        advanced_groups = [
            field_group("Indexierung und Verarbeitung", [
                controls["CHUNK_SIZE"], controls["CHUNK_OVERLAP"],
                controls["MAX_CHUNKS_PER_FILE"], controls["MAX_FILE_SIZE_MB"],
                controls["EXTRACTION_CACHE_RETENTION_DAYS"],
            ]),
            field_group("Dateien und Datenschutz", [
                controls["EXTRA_EXCLUDED_DIRS"], controls["INCLUDE_SENSITIVE_FILES"],
                controls["INCLUDE_IGNORED_DIRS"], controls["USE_DOCLING"], controls["LOG_LEVEL"],
            ]),
            field_group("Embedding-Leistung", [
                controls["EMBEDDING_TIMEOUT_SECONDS"], controls["EMBEDDING_THREADS"],
                controls["EMBEDDING_BATCH_SIZE"],
            ]),
            field_group("LLM-Funktionen", [
                controls["LLM_TIMEOUT_SECONDS"], controls["ENTITY_EXTRACTION_ENABLED"],
                controls["AUTONOMOUS_QUALITY_ENABLED"], controls["COMMUNITY_SUMMARIES_ENABLED"],
                controls["GLOBAL_SEARCH_ENABLED"],
            ]),
            field_group("Qualit" + chr(0x00E4) + "t und Communitys", [
                controls["QUALITY_MAX_CANDIDATES_PER_RUN"], controls["QUALITY_BATCH_SIZE"],
                controls["QUALITY_MIN_CONFIDENCE"], controls["COMMUNITY_SUMMARY_MIN_MEMBERS"],
                controls["COMMUNITY_SUMMARY_MAX_PER_RUN"], controls["COMMUNITY_SUMMARY_BATCH_SIZE"],
            ]),
            field_group("Graph-Analysen", [
                gds_enabled_control, controls["GDS_MAX_ENTITY_NODES"],
                controls["GDS_MAX_NODE_SIMILARITY_NODES"], controls["GDS_MAX_KNN_CHUNKS"],
            ]),
        ]
        advanced = ft.ExpansionTile(
            title=ft.Text("Erweitert: Verarbeitung, Laufzeit und Analyse"),
            subtitle=ft.Text("Optionale Funktionen, Grenzen und technische Parameter", size=11),
            controls=[ft.Container(
                content=ft.Column(
                    controls=advanced_groups,
                    spacing=10,
                    horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                ),
                padding=12,
            )],
            expanded=False,
        )

        def form_section(title: str, description: str, fields: list[ft.Control]) -> ft.Control:
            return ft.Container(
                content=ft.Column(
                    controls=[
                        ft.Text(title, size=16, weight=ft.FontWeight.W_600, color="#F4F8FF"),
                        ft.Text(description, size=11, color="#91A9C8"),
                        *fields,
                    ],
                    spacing=9,
                    horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                ),
                padding=16,
                bgcolor="#0B1C36",
                border=ft.Border.all(1, "#1A3554"),
                border_radius=12,
            )

        def primary_selector(dropdown: ft.Dropdown, description: str, icon: Any) -> ft.Control:
            dropdown.filled = True
            dropdown.fill_color = "#102747"
            dropdown.border = ft.OutlineInputBorder(
                side=ft.BorderSide(color="#11C5E8", width=2),
                border_radius=10,
            )
            return ft.Column(
                controls=[
                    ft.Row(controls=[
                        ft.Icon(icon, size=15, color="#11C5E8"),
                        ft.Text("Prim" + chr(0x00E4) + "re Auswahl", size=10, weight=ft.FontWeight.W_600, color="#11C5E8"),
                    ], spacing=6),
                    dropdown,
                    ft.Text(description, size=10, color="#91A9C8"),
                ],
                spacing=5,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            )

        async def save(_: Any) -> None:
            try:
                db_mode = DB_LABEL_TO_VALUE[db.value]
                embed_provider = EMBED_LABEL_TO_VALUE[emb.value]
                llm_provider = LLM_LABEL_TO_VALUE[llm.value]
                port = int(basic["NEO4J_LOCAL_PORT"].value)
                if not 1024 <= port <= 64535:
                    raise ValueError("Der lokale Bolt-Port muss zwischen 1024 und 64535 liegen.")
                uri = str(basic["NEO4J_URI"].value or "").strip()
                if db_mode == "local":
                    uri = f"neo4j://127.0.0.1:{port}"
                if db_mode in {"aura", "external"} and not uri:
                    raise ValueError("Bitte eine Neo4j-Bolt-URI angeben.")
                password = str(basic["NEO4J_PASSWORD"].value or "")
                if db_mode == "local" and not password:
                    password = secrets.token_urlsafe(24)
                    basic["NEO4J_PASSWORD"].value = password
                if not basic["NEO4J_USERNAME"].value or not password:
                    raise ValueError("Neo4j-Benutzername und Passwort sind erforderlich.")
                if db_mode == "local" and (len(password) < 16 or not all(ch.isalnum() or ch in "_-" for ch in password)):
                    raise ValueError("Das lokale Neo4j-Passwort muss mindestens 16 Zeichen lang sein und nur Buchstaben, Zahlen, _ oder - enthalten.")
                if embed_provider in {"ollama", "openai"} and not basic["EMBEDDING_BASE_URL"].value.strip():
                    raise ValueError("Bitte den Embedding-Endpunkt angeben.")
                llm_url = str(basic["LLM_BASE_URL"].value or "").strip().rstrip("/")
                llm_model = str(basic["LLM_MODEL"].value or "").strip()
                if llm_provider == "ollama-local":
                    llm_url = llm_url or "http://127.0.0.1:11435/v1"
                    llm_model = llm_model or "qwen2.5:3b"
                if llm_provider != "none" and (not llm_url or not llm_model):
                    raise ValueError("Für ein LLM müssen Basis-URL und Modell angegeben werden.")
                updates = {
                    "NEO4J_MODE": db_mode, "NEO4J_URI": uri,
                    "NEO4J_USERNAME": str(basic["NEO4J_USERNAME"].value).strip(),
                    "NEO4J_PASSWORD": password, "NEO4J_DATABASE": str(basic["NEO4J_DATABASE"].value or "neo4j").strip(),
                    "NEO4J_LOCAL_PORT": str(port), "GDS_ENABLED": str(bool(gds_enabled_control.value)).lower(),
                    "EMBEDDING_PROVIDER": embed_provider,
                    "EMBEDDING_BASE_URL": str(basic["EMBEDDING_BASE_URL"].value or "").strip().rstrip("/"),
                    "EMBEDDING_MODEL": str(basic["EMBEDDING_MODEL"].value or "").strip(),
                    "EMBEDDING_API_KEY": str(basic["EMBEDDING_API_KEY"].value or ""),
                    "LLM_PROVIDER": llm_provider, "LLM_BASE_URL": llm_url if llm_provider != "none" else "",
                    "LLM_MODEL": llm_model if llm_provider != "none" else "",
                    "LLM_API_KEY": str(basic["LLM_API_KEY"].value or "") if llm_provider != "none" else "",
                }
                for key, control in controls.items():
                    if key in ADVANCED:
                        value = str(bool(control.value)).lower() if isinstance(control, ft.Checkbox) else str(control.value or "").strip()
                        kind = ADVANCED[key][2]
                        if kind == "int":
                            numeric = int(value)
                            minimum = 0 if key in {
                                "EMBEDDING_THREADS", "CHUNK_OVERLAP", "EXTRACTION_CACHE_RETENTION_DAYS"
                            } else 1
                            if numeric < minimum:
                                raise ValueError(f"{key} muss mindestens {minimum} sein.")
                        elif kind == "float":
                            numeric = float(value)
                            if not 0 <= numeric <= 1:
                                raise ValueError(f"{key} muss zwischen 0 und 1 liegen.")
                        if key == "CHUNK_OVERLAP" and int(value) >= int(str(controls["CHUNK_SIZE"].value)):
                            raise ValueError("CHUNK_OVERLAP muss kleiner als CHUNK_SIZE sein.")
                        if key in {"ENTITY_EXTRACTION_ENABLED", "AUTONOMOUS_QUALITY_ENABLED", "COMMUNITY_SUMMARIES_ENABLED", "GLOBAL_SEARCH_ENABLED"} and llm_provider == "none":
                            value = "false"
                        updates[key] = value
                path = update_config(updates)
                saved["value"] = True
                status.value = f"Konfiguration gespeichert: {path}\nÄnderungen gelten nach einem Neustart."
                status.color = "#42D392"
                page.update()
                try:
                    await page.window.close()
                except RuntimeError:
                    pass
            except Exception as exc:
                status.value = str(exc)
                status.color = "#FF777D"
                page.update()

        async def cancel(_: Any) -> None:
            try:
                await page.window.close()
            except RuntimeError:
                pass

        database_tab = ft.Column(
            controls=[form_section(
                "Graph-Datenbank",
                "Bereitstellung und Zugangsdaten f" + chr(0x00FC) + "r Neo4j.",
                [primary_selector(db, "Steuert Bereitstellung und Zugangsdaten in diesem Tab.", ft.Icons.STORAGE_OUTLINED), basic["NEO4J_URI"], basic["NEO4J_USERNAME"], basic["NEO4J_PASSWORD"], basic["NEO4J_DATABASE"], basic["NEO4J_LOCAL_PORT"]],
            )],
            spacing=12,
            scroll=ft.ScrollMode.AUTO,
            expand=True,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        )
        embeddings_tab = ft.Column(
            controls=[form_section(
                "Embeddings",
                "Anbieter und Modell f" + chr(0x00FC) + "r die semantische Suche.",
                [primary_selector(emb, "Steuert Anbieter, Endpunkt und Modell in diesem Tab.", ft.Icons.HUB_OUTLINED), basic["EMBEDDING_BASE_URL"], basic["EMBEDDING_MODEL"], basic["EMBEDDING_API_KEY"]],
            )],
            spacing=12,
            scroll=ft.ScrollMode.AUTO,
            expand=True,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        )
        llm_tab = ft.Column(
            controls=[form_section(
                "Chat-LLM",
                "Optionales Chat-Modell f" + chr(0x00FC) + "r Extraktion, Qualit" + chr(0x00E4) + "t und Zusammenfassungen.",
                [primary_selector(llm, "Steuert Bereitstellung, Endpunkt und Modell in diesem Tab.", ft.Icons.CHAT_OUTLINED), basic["LLM_BASE_URL"], basic["LLM_MODEL"], basic["LLM_API_KEY"]],
            )],
            spacing=12,
            scroll=ft.ScrollMode.AUTO,
            expand=True,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        )
        advanced_tab = ft.Column(
            controls=advanced_groups,
            spacing=12,
            scroll=ft.ScrollMode.AUTO,
            expand=True,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        )
        wizard_tabs = ft.Tabs(
            content=ft.Column(
                controls=[
                    ft.TabBar(tabs=[
                        ft.Tab(label="Datenbank"),
                        ft.Tab(label="Embeddings"),
                        ft.Tab(label="LLM"),
                        ft.Tab(label="Erweitert"),
                    ]),
                    ft.TabBarView(
                        controls=[database_tab, embeddings_tab, llm_tab, advanced_tab],
                        expand=True,
                    ),
                ],
                spacing=0,
                expand=True,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            ),
            length=4,
            selected_index=0,
            expand=True,
        )
        page.add(ft.Column(
            controls=[
                ft.Row(controls=[
                    ft.Image(src="data:image/png;base64," + image_as_base64("Enargeia_Logo_Dark_Background.png"), width=240, height=60, fit=ft.BoxFit.CONTAIN),
                    ft.Column(controls=[
                        ft.Text("Einrichtung", size=25, weight=ft.FontWeight.BOLD, text_align=ft.TextAlign.RIGHT),
                        ft.Text("Konfiguration nach Bereichen", color="#91A9C8", text_align=ft.TextAlign.RIGHT),
                    ], spacing=4, expand=True, horizontal_alignment=ft.CrossAxisAlignment.END),
                ], spacing=14),
                ft.Divider(color="#1A3554"),
                ft.Text("Einstellungen sind nach Komponenten geordnet. Weitere Parameter findest du im Tab Erweitert.", color="#91A9C8", size=12),
                wizard_tabs,
                status,
                ft.Row(controls=[
                    ft.Button(content="Abbrechen", on_click=cancel),
                    ft.Button(content="Konfiguration speichern", icon=ft.Icons.SAVE_OUTLINED, on_click=save),
                ], alignment=ft.MainAxisAlignment.END, spacing=10),
            ],
            spacing=10,
            expand=True,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        ))

    ft.run(main)
    return saved["value"]


