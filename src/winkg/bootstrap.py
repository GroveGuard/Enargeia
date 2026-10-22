"""Application bootstrap: first-run setup, optional local Neo4j provisioning, then GUI."""
from __future__ import annotations

import argparse
import logging
import sys
import tkinter as tk
from tkinter import messagebox, ttk

from .config import ENV_FILE, LOG_DIR, get_settings
from .branding import brand_asset_path
from .setup_wizard import run_setup_wizard


def _progress_window():
    root = tk.Tk()
    root.title("Enargeia – Initialisierung")
    root.geometry("700x394")
    root.resizable(False, False)
    root.configure(background="#071326")
    try:
        root.iconbitmap(str(brand_asset_path("Enargeia.ico")))
    except Exception:
        pass
    # The supplied welcome artwork gives the short first-run setup window the same
    # visual language as the installer. It is pre-sized, so no runtime imaging library is needed.
    background_image = tk.PhotoImage(file=str(brand_asset_path("Enargeia_Welcome_Background.png")))
    background = tk.Label(root, image=background_image, bd=0, highlightthickness=0)
    background.place(x=0, y=0, relwidth=1, relheight=1)
    root._enargeia_background_image = background_image
    header_image = tk.PhotoImage(file=str(brand_asset_path("Enargeia_Installer_Header.png")))
    header = tk.Label(root, image=header_image, bd=0, background="#071326")
    header.place(x=20, y=22)
    root._enargeia_header_image = header_image
    message = tk.StringVar(value="Bereite die lokale Umgebung vor …")
    label = tk.Label(root, textvariable=message, wraplength=460, anchor="w", justify="left",
                     background="#071326", foreground="#F4F8FF", font=("Segoe UI", 10))
    label.place(x=22, y=112, width=440, height=42)
    bar = ttk.Progressbar(root, mode="indeterminate")
    bar.place(x=22, y=166, width=440, height=12)
    bar.start(12)
    root.update_idletasks()
    root.update()

    def report(text: str) -> None:
        try:
            message.set(text)
            root.update_idletasks()
            root.update()
        except tk.TclError:
            pass

    return root, report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="Enargeia")
    parser.add_argument("--configure", action="store_true", help="Konfigurationsassistent erneut öffnen")
    args = parser.parse_args(argv)

    if args.configure or not ENV_FILE.is_file():
        if not run_setup_wizard():
            return 1 if not ENV_FILE.is_file() else 0
        if args.configure:
            return 0

    progress_root = None
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(LOG_DIR / "enargeia-bootstrap.log"),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        settings = get_settings()
        settings.validate_neo4j()
        needs_local_setup = settings.neo4j_mode == "local" or settings.llm_provider == "ollama-local"
        report = None
        if needs_local_setup:
            progress_root, report = _progress_window()
        if settings.neo4j_mode == "local":
            from .neo4j_runtime import LocalNeo4jManager
            LocalNeo4jManager(settings).start(progress=report)
        if settings.llm_provider == "ollama-local":
            from .ollama_runtime import LocalOllamaManager
            LocalOllamaManager(settings).ensure_ready(progress=report)
        if progress_root is not None:
            progress_root.destroy()
            progress_root = None

        from . import gui
        import flet as ft

        ft.run(gui.main)
        return 0
    except Exception as exc:
        logging.exception("Enargeia konnte nicht gestartet werden")
        if progress_root is not None:
            try:
                progress_root.destroy()
            except tk.TclError:
                pass
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(
                "Enargeia – Startfehler",
                f"Die Anwendung konnte nicht gestartet werden.\n\n{exc}\n\n"
                f"Konfiguration: {ENV_FILE}\nProtokolle: {LOG_DIR}",
                parent=root,
            )
            root.destroy()
        except Exception:
            print(f"Startfehler: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
