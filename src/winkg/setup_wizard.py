"""First-run configuration wizard; depends only on the Python standard library."""
from __future__ import annotations

import secrets
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any

from .config import ENV_FILE, get_settings, update_config
from .branding import brand_asset_path



DB_LABEL_TO_VALUE = {"Lokal installieren": "local", "Neo4j Aura": "aura", "Externe Neo4j-Instanz": "external"}
EMBED_LABEL_TO_VALUE = {"Lokal (FastEmbed / ONNX)": "fastembed", "Vorhandenes Ollama": "ollama", "Externe Embedding-API": "openai", "Embeddings deaktivieren": "none"}
LLM_LABEL_TO_VALUE = {"Keine LLM-Anbindung": "none", "Ollama lokal bereitstellen": "ollama-local", "Vorhandenes Ollama verwenden": "ollama", "Externer OpenAI-kompatibler Endpunkt": "openai-compatible"}
DB_VALUE_TO_LABEL = {value: label for label, value in DB_LABEL_TO_VALUE.items()}
EMBED_VALUE_TO_LABEL = {value: label for label, value in EMBED_LABEL_TO_VALUE.items()}
LLM_VALUE_TO_LABEL = {value: label for label, value in LLM_LABEL_TO_VALUE.items()}

PROFILE_VALUES = {
    "Vollständig lokal (ohne verpflichtendes LLM)": ("local", "fastembed", "none"),
    "Lokale Datenbank + externe KI": ("local", "fastembed", "openai-compatible"),
    "Alles lokal inklusive LLM": ("local", "fastembed", "ollama-local"),
    "Neo4j Aura + lokale Embeddings + externe KI": ("aura", "fastembed", "openai-compatible"),
    "Individuelle Konfiguration": ("custom", "fastembed", "none"),
}


def run_setup_wizard() -> bool:
    """Show the user-facing setup wizard and save choices. Return False on cancel."""
    has_existing_configuration = ENV_FILE.is_file()
    current = get_settings()
    root = tk.Tk()
    root.title("Enargeia – Einrichtung")
    root.geometry("790x800")
    root.minsize(720, 680)
    root.configure(background="#071326")
    root.resizable(True, True)
    try:
        root.iconbitmap(str(brand_asset_path("Enargeia.ico")))
    except Exception:
        pass

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure("TFrame", background="#071326")
    style.configure("TLabel", background="#071326", foreground="#F4F8FF", font=("Segoe UI", 10))
    style.configure("Title.TLabel", font=("Segoe UI Semibold", 19), foreground="#F4F8FF")
    style.configure("Muted.TLabel", foreground="#91A9C8", font=("Segoe UI", 9))
    style.configure("TLabelFrame", background="#071326", foreground="#91A9C8")
    style.configure("TLabelFrame.Label", background="#071326", foreground="#11C5E8", font=("Segoe UI Semibold", 10))
    style.configure("TEntry", fieldbackground="#0B1C36", foreground="#F4F8FF")
    style.configure("TCombobox", fieldbackground="#0B1C36")
    style.configure("TButton", padding=(12, 7))
    style.configure("Accent.TButton", background="#1677FF", foreground="#F4F8FF")

    outer = ttk.Frame(root, padding=22)
    outer.pack(fill="both", expand=True)
    try:
        brand_header = tk.PhotoImage(file=str(brand_asset_path("Enargeia_Installer_Header.png")))
        brand_header_label = tk.Label(outer, image=brand_header, background="#071326", bd=0)
        brand_header_label.image = brand_header
        brand_header_label.pack(anchor="w", pady=(0, 10))
    except Exception:
        ttk.Label(outer, text="Enargeia", style="Title.TLabel").pack(anchor="w")
    ttk.Label(
        outer,
        text="Wähle für jede Komponente, ob sie lokal bereitgestellt oder extern verbunden werden soll.",
        wraplength=720,
        style="Muted.TLabel",
    ).pack(anchor="w", pady=(5, 16))

    # Keep the wizard usable on laptop displays and with Windows text scaling.
    scroll_container = ttk.Frame(outer)
    scroll_container.pack(fill="both", expand=True)
    canvas = tk.Canvas(scroll_container, background="#071326", highlightthickness=0, borderwidth=0)
    scrollbar = ttk.Scrollbar(scroll_container, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=scrollbar.set)
    scrollbar.pack(side="right", fill="y")
    canvas.pack(side="left", fill="both", expand=True)
    form = ttk.Frame(canvas)
    form_window = canvas.create_window((0, 0), window=form, anchor="nw")

    def _resize_form(event: tk.Event) -> None:
        canvas.itemconfigure(form_window, width=event.width)

    def _update_scroll_region(_: tk.Event | None = None) -> None:
        canvas.configure(scrollregion=canvas.bbox("all"))

    canvas.bind("<Configure>", _resize_form)
    form.bind("<Configure>", _update_scroll_region)

    def _mousewheel(event: tk.Event) -> str:
        canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        return "break"

    canvas.bind_all("<MouseWheel>", _mousewheel)
    vars_: dict[str, Any] = {}
    row = 0

    def section(title: str) -> None:
        nonlocal row
        frame = ttk.LabelFrame(form, text=title, padding=(12, 9))
        frame.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        frame.columnconfigure(1, weight=1)
        row += 1
        vars_[f"frame_{title}"] = frame

    def field(frame: ttk.LabelFrame, key: str, label: str, value: str = "", *, choices: list[str] | None = None, show: str | None = None) -> Any:
        idx = len([k for k in vars_ if k.startswith(f"row_{frame.winfo_name()}_")])
        ttk.Label(frame, text=label).grid(row=idx, column=0, sticky="w", padx=(0, 14), pady=4)
        if choices:
            var = tk.StringVar(value=value)
            widget = ttk.Combobox(frame, textvariable=var, values=choices, state="readonly")
            widget.grid(row=idx, column=1, sticky="ew", pady=4)
        else:
            var = tk.StringVar(value=value)
            widget = ttk.Entry(frame, textvariable=var, show=show or "")
            widget.grid(row=idx, column=1, sticky="ew", pady=4)
        vars_[key] = var
        vars_[f"row_{frame.winfo_name()}_{idx}"] = widget
        return var

    section("Installationsprofil")
    profile_frame = vars_["frame_Installationsprofil"]
    initial_profile = "Individuelle Konfiguration" if has_existing_configuration else "Vollständig lokal (ohne verpflichtendes LLM)"
    profile_var = field(profile_frame, "profile", "Profil", initial_profile, choices=list(PROFILE_VALUES))

    section("1 · Graph-Datenbank")
    db_frame = vars_["frame_1 · Graph-Datenbank"]
    initial_db_mode = current.neo4j_mode if has_existing_configuration else "local"
    db_mode = field(db_frame, "neo4j_mode", "Bereitstellung", DB_VALUE_TO_LABEL.get(initial_db_mode, "Lokal installieren"), choices=list(DB_LABEL_TO_VALUE))
    initial_uri = (f"neo4j://127.0.0.1:{current.neo4j_local_port or 17687}" if initial_db_mode == "local" else current.neo4j_uri)
    uri = field(db_frame, "neo4j_uri", "Bolt-URI", initial_uri)
    username = field(db_frame, "neo4j_username", "Benutzername", current.neo4j_username or "neo4j")
    password = field(db_frame, "neo4j_password", "Passwort", current.neo4j_password or secrets.token_urlsafe(24), show="•")
    database = field(db_frame, "neo4j_database", "Datenbank", current.neo4j_database or "neo4j")
    port = field(db_frame, "neo4j_local_port", "Lokaler Bolt-Port", str(current.neo4j_local_port or 17687))

    section("2 · Embeddings")
    emb_frame = vars_["frame_2 · Embeddings"]
    embed_provider = field(emb_frame, "embedding_provider", "Anbieter", EMBED_VALUE_TO_LABEL.get(current.embedding_provider, "Lokal (FastEmbed / ONNX)"), choices=list(EMBED_LABEL_TO_VALUE))
    embed_url = field(emb_frame, "embedding_base_url", "Endpunkt (API-Anbieter)", current.embedding_base_url)
    embed_model = field(emb_frame, "embedding_model", "Modell", current.embedding_model or "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
    embed_key = field(emb_frame, "embedding_api_key", "API-Schlüssel (optional)", current.embedding_api_key or "", show="•")

    section("3 · LLM für optionale KI-Aufgaben")
    llm_frame = vars_["frame_3 · LLM für optionale KI-Aufgaben"]
    llm_provider = field(llm_frame, "llm_provider", "Anbieter", LLM_VALUE_TO_LABEL.get(current.llm_provider, "Keine LLM-Anbindung"), choices=list(LLM_LABEL_TO_VALUE))
    llm_url = field(llm_frame, "llm_base_url", "OpenAI-kompatible Basis-URL", current.llm_base_url)
    llm_model = field(llm_frame, "llm_model", "Chat-Modell", current.llm_model)
    llm_key = field(llm_frame, "llm_api_key", "API-Schlüssel (optional)", current.llm_api_key, show="•")

    section("4 · Optionen")
    options_frame = vars_["frame_4 · Optionen"]
    gds_var = tk.BooleanVar(value=current.gds_enabled if has_existing_configuration else True)
    ttk.Checkbutton(options_frame, text="GDS-Graph-Analysen nutzen (bei lokaler Neo4j-Instanz Plugin bereitstellen)", variable=gds_var).grid(row=0, column=0, columnspan=2, sticky="w", pady=4)
    entity_var = tk.BooleanVar(value=current.entity_extraction_enabled if has_existing_configuration else False)
    quality_var = tk.BooleanVar(value=current.autonomous_quality_enabled if has_existing_configuration else False)
    community_var = tk.BooleanVar(value=current.community_summaries_enabled if has_existing_configuration else False)
    search_var = tk.BooleanVar(value=current.global_search_enabled if has_existing_configuration else False)
    ttk.Checkbutton(options_frame, text="LLM-gestützte Entitäts-/Beziehungsextraktion aktivieren", variable=entity_var).grid(row=1, column=0, columnspan=2, sticky="w", pady=3)
    ttk.Checkbutton(options_frame, text="Autonome Qualitätsprüfung von Beziehungsvorschlägen aktivieren", variable=quality_var).grid(row=2, column=0, columnspan=2, sticky="w", pady=3)
    ttk.Checkbutton(options_frame, text="Community-Zusammenfassungen aktivieren", variable=community_var).grid(row=3, column=0, columnspan=2, sticky="w", pady=3)
    ttk.Checkbutton(options_frame, text="LLM-gestützte globale Suche aktivieren", variable=search_var).grid(row=4, column=0, columnspan=2, sticky="w", pady=3)
    ttk.Label(options_frame, text="Embedding-Modell und optionales lokales LLM werden erst bei Bedarf heruntergeladen. Lokales LLM: Standardmodell qwen2.5:3b (~2 GB, je nach Modellversion).", style="Muted.TLabel", wraplength=650).grid(row=5, column=0, columnspan=2, sticky="w", pady=(4, 0))

    # Setup profiles are presets only; changing any provider switches to custom mode.
    manual_change = {"busy": False}
    def mark_custom(*_: object) -> None:
        # On a new setup, do not accidentally reuse the generated local password
        # for Aura/external connections; generate one again when returning to local.
        selected_db = DB_LABEL_TO_VALUE.get(db_mode.get(), db_mode.get())
        if not has_existing_configuration:
            if selected_db == "local" and not password.get():
                password.set(secrets.token_urlsafe(24))
            elif selected_db in {"aura", "external"} and password.get():
                password.set("")
        if embed_provider.get() == EMBED_VALUE_TO_LABEL["ollama"] and not embed_url.get().strip():
            embed_url.set("http://127.0.0.1:11434")
        elif embed_provider.get() == EMBED_VALUE_TO_LABEL["openai"] and not embed_url.get().strip():
            embed_url.set("https://api.openai.com/v1")
        if llm_provider.get() == LLM_VALUE_TO_LABEL["ollama-local"]:
            if not llm_url.get().strip():
                llm_url.set("http://127.0.0.1:11435/v1")
            if not llm_model.get().strip():
                llm_model.set("qwen2.5:3b")
        if not manual_change["busy"] and profile_var.get() in PROFILE_VALUES and profile_var.get() != "Individuelle Konfiguration":
            manual_change["busy"] = True
            profile_var.set("Individuelle Konfiguration")
            manual_change["busy"] = False

    for variable in (db_mode, embed_provider, llm_provider):
        variable.trace_add("write", mark_custom)

    def apply_profile(_: object = None) -> None:
        chosen = PROFILE_VALUES.get(profile_var.get())
        if not chosen or chosen[0] == "custom":
            return
        db_value, embed_value, llm_value = chosen
        manual_change["busy"] = True
        db_mode.set(DB_VALUE_TO_LABEL[db_value])
        embed_provider.set(EMBED_VALUE_TO_LABEL[embed_value])
        llm_provider.set(LLM_VALUE_TO_LABEL[llm_value])
        if db_value == "local":
            port.set("17687")
            uri.set("neo4j://127.0.0.1:17687")
            if not password.get():
                password.set(secrets.token_urlsafe(24))
        elif db_value == "aura":
            uri.set("neo4j+s://<Deine-Instanz>.databases.neo4j.io")
        if llm_value == "none":
            llm_url.set("")
            llm_model.set("")
            llm_key.set("")
        elif llm_value == "ollama-local":
            llm_url.set("http://127.0.0.1:11435/v1")
            llm_model.set("qwen2.5:3b")
        elif llm_value == "ollama":
            llm_url.set("http://127.0.0.1:11434/v1")
            if not llm_model.get():
                llm_model.set("qwen2.5:3b")
        elif llm_value == "openai-compatible":
            llm_url.set("https://api.openai.com/v1")
            llm_model.set("")
        manual_change["busy"] = False

    # Add profile behavior from the combobox itself.
    for widget in profile_frame.winfo_children():
        if isinstance(widget, ttk.Combobox):
            widget.bind("<<ComboboxSelected>>", apply_profile)
            break

    buttons = ttk.Frame(outer)
    buttons.pack(fill="x", pady=(12, 0))
    status = tk.StringVar(value=f"Konfiguration: {ENV_FILE}")
    ttk.Label(outer, textvariable=status, style="Muted.TLabel", wraplength=720).pack(anchor="w", pady=(8, 0))
    result = {"saved": False}

    def save() -> None:
        try:
            mode_value = DB_LABEL_TO_VALUE.get(db_mode.get(), db_mode.get()).strip().lower()
            embed_value = EMBED_LABEL_TO_VALUE.get(embed_provider.get(), embed_provider.get()).strip().lower()
            llm_value = LLM_LABEL_TO_VALUE.get(llm_provider.get(), llm_provider.get()).strip().lower()
            local_port = int(port.get().strip())
            if not 1024 <= local_port <= 64535:
                raise ValueError("Der lokale Bolt-Port muss zwischen 1024 und 64535 liegen, damit der zugehörige HTTP-Port ebenfalls gültig bleibt.")
            db_uri = uri.get().strip()
            if mode_value == "local":
                db_uri = f"neo4j://127.0.0.1:{local_port}"
            if mode_value in {"aura", "external"} and not db_uri:
                raise ValueError("Bitte eine Neo4j-Bolt-URI angeben.")
            if mode_value == "aura" and ("<" in db_uri or ">" in db_uri or "Deine-Instanz" in db_uri):
                raise ValueError("Bitte die tatsächliche Aura-Bolt-URI eintragen, zum Beispiel neo4j+s://xxxx.databases.neo4j.io.")
            if mode_value == "local" and (database.get().strip() or "neo4j").lower() != "neo4j":
                raise ValueError("Die verwaltete lokale Community-Instanz verwendet die Datenbank 'neo4j'. Andere Datenbanknamen sind für externe Verbindungen verfügbar.")
            if not username.get().strip() or not password.get():
                raise ValueError("Bitte Datenbank-Benutzername und Passwort angeben.")
            if mode_value == "local":
                # The initial password is passed to Neo4j's Windows .bat admin tool.
                # Restrict shell-sensitive characters to avoid cmd.exe parsing/injection.
                import re
                local_password = password.get()
                if len(local_password) < 16 or not re.fullmatch(r"[A-Za-z0-9_-]+", local_password):
                    raise ValueError(
                        "Für die lokale Neo4j-Instanz muss das Passwort mindestens 16 Zeichen lang sein "
                        "und nur Buchstaben, Zahlen, '_' oder '-' enthalten. Das automatisch erzeugte Passwort erfüllt diese Vorgabe."
                    )
            if embed_value in {"ollama", "openai"} and not embed_url.get().strip():
                raise ValueError("Bitte den Embedding-Endpunkt angeben.")
            llm_base = llm_url.get().strip().rstrip("/") if llm_value != "none" else ""
            llm_name = llm_model.get().strip() if llm_value != "none" else ""
            if llm_value == "ollama-local":
                llm_base = "http://127.0.0.1:11435/v1"
                llm_name = llm_name or "qwen2.5:3b"
            if llm_value != "none" and (not llm_base or not llm_name):
                raise ValueError("Für LLM-Aufgaben müssen Endpunkt und Modell angegeben werden.")

            updates = {
                "NEO4J_MODE": mode_value,
                "NEO4J_URI": db_uri,
                "NEO4J_USERNAME": username.get().strip(),
                "NEO4J_PASSWORD": password.get(),
                "NEO4J_DATABASE": database.get().strip() or "neo4j",
                "NEO4J_LOCAL_PORT": str(local_port),
                "GDS_ENABLED": str(bool(gds_var.get())).lower(),
                "EMBEDDING_PROVIDER": embed_value,
                "EMBEDDING_BASE_URL": embed_url.get().strip().rstrip("/"),
                "EMBEDDING_MODEL": embed_model.get().strip() or "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
                "EMBEDDING_API_KEY": embed_key.get(),
                "LLM_PROVIDER": llm_value,
                "LLM_BASE_URL": llm_base,
                "LLM_MODEL": llm_name,
                "LLM_API_KEY": llm_key.get() if llm_value != "none" else "",
                "ENTITY_EXTRACTION_ENABLED": str(bool(entity_var.get() and llm_value != "none")).lower(),
                "AUTONOMOUS_QUALITY_ENABLED": str(bool(quality_var.get() and llm_value != "none")).lower(),
                "COMMUNITY_SUMMARIES_ENABLED": str(bool(community_var.get() and llm_value != "none")).lower(),
                "GLOBAL_SEARCH_ENABLED": str(bool(search_var.get() and llm_value != "none")).lower(),
            }
            path = update_config(updates)
            result["saved"] = True
            messagebox.showinfo(
                "Einrichtung gespeichert",
                f"Die Konfiguration wurde gespeichert:\n\n{path}\n\n"
                + ("Neo4j wird beim Start automatisch bereitgestellt.\n" if mode_value == "local" else "Die externe Datenbank wird beim Start geprüft.\n")
                + ("Das Embedding-Modell wird beim ersten Einsatz heruntergeladen.\n" if embed_value == "fastembed" else "")
                + ("Ollama und das ausgewählte LLM werden beim ersten Start bereitgestellt/heruntergeladen.\n" if llm_value == "ollama-local" else "")
                + "Optionale LLM-Funktionen sind erst aktiv, wenn sie in den Einstellungen eingeschaltet werden.",
                parent=root,
            )
            canvas.unbind_all("<MouseWheel>")
            root.destroy()
        except Exception as exc:
            messagebox.showerror("Konfiguration prüfen", str(exc), parent=root)

    def cancel() -> None:
        if messagebox.askyesno("Einrichtung abbrechen?", "Ohne gespeicherte Konfiguration kann die Anwendung nicht gestartet werden.", parent=root):
            canvas.unbind_all("<MouseWheel>")
            root.destroy()

    ttk.Button(buttons, text="Abbrechen", command=cancel).pack(side="right")
    ttk.Button(buttons, text="Speichern und fortfahren", style="Accent.TButton", command=save).pack(side="right", padx=(0, 8))
    root.protocol("WM_DELETE_WINDOW", cancel)
    root.mainloop()
    return result["saved"]
