"""Application bootstrap: Flet setup, optional local services, then main GUI."""
from __future__ import annotations

import argparse
import asyncio
import logging
import queue
import sys
import threading

from .config import ENV_FILE, LOG_DIR, get_settings
from .branding import image_as_base64, brand_asset_path
from .setup_wizard import run_setup_wizard


def _run_service_setup(settings) -> None:
    """Provision local services on a worker thread while Flet owns the progress UI."""
    messages: queue.Queue[tuple[str, object]] = queue.Queue()
    result: dict[str, object] = {"error": None}

    def worker() -> None:
        try:
            if settings.neo4j_mode == "local":
                from .neo4j_runtime import LocalNeo4jManager
                LocalNeo4jManager(settings).start(progress=lambda text: messages.put(("message", text)))
            if settings.llm_provider == "ollama-local":
                from .ollama_runtime import LocalOllamaManager
                LocalOllamaManager(settings).ensure_ready(progress=lambda text: messages.put(("message", text)))
        except Exception as exc:
            result["error"] = exc
        finally:
            messages.put(("done", None))

    thread = threading.Thread(target=worker, name="enargeia-service-setup", daemon=True)
    thread.start()

    def main(page) -> None:
        import flet as ft
        page.title = "Enargeia – Initialisierung"
        page.window.icon = str(brand_asset_path("Enargeia.ico"))
        page.theme_mode = ft.ThemeMode.DARK
        page.bgcolor = "#071326"
        page.padding = 28
        page.window.width = 680
        page.window.height = 300
        page.window.resizable = False
        message = ft.Text("Bereite die lokale Umgebung vor …", size=15, color="#F4F8FF", selectable=True)
        progress = ft.ProgressBar(value=None, color="#11C5E8", bgcolor="#1A3554")
        page.add(ft.Column(controls=[
            ft.Image(src="data:image/png;base64," + image_as_base64("Enargeia_Installer_Header.png"), width=390, height=72, fit=ft.BoxFit.CONTAIN),
            message, progress,
        ], spacing=18, expand=True))

        async def poll() -> None:
            while True:
                try:
                    kind, value = messages.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(0.15)
                    continue
                if kind == "message":
                    message.value = str(value)
                    page.update()
                else:
                    message.value = "Initialisierung abgeschlossen." if result["error"] is None else f"Initialisierung fehlgeschlagen: {result['error']}"
                    message.color = "#42D392" if result["error"] is None else "#FF777D"
                    progress.value = 1.0 if result["error"] is None else 0.0
                    page.update()
                    await asyncio.sleep(0.6)
                    try:
                        await page.window.close()
                    except RuntimeError:
                        # Flet may close the desktop session before the close
                        # method's response reaches this polling task.
                        pass
                    return

        page.run_task(poll)

    import flet as ft
    ft.run(main)
    thread.join(timeout=1)
    error = result["error"]
    if error is not None:
        raise error


def _show_start_error(error: Exception) -> None:
    """Display startup errors in the same Flet visual system as the application."""
    import flet as ft
    from .branding import image_as_base64, brand_asset_path

    def main(page) -> None:
        page.title = "Enargeia – Startfehler"
        page.window.icon = str(brand_asset_path("Enargeia.ico"))
        page.theme_mode = ft.ThemeMode.DARK
        page.bgcolor = "#071326"
        page.padding = 28
        page.window.width = 700
        page.window.height = 440
        async def close_error(_: object) -> None:
            await page.window.close()

        page.add(ft.Column(controls=[
            ft.Image(src="data:image/png;base64," + image_as_base64("Enargeia_Logo_Dark_Background.png"), width=320, height=70, fit=ft.BoxFit.CONTAIN),
            ft.Text("Enargeia konnte nicht gestartet werden", size=22, weight=ft.FontWeight.BOLD, color="#FF777D"),
            ft.Text(f"{error}\n\nKonfiguration: {ENV_FILE}\nProtokolle: {LOG_DIR}", selectable=True, color="#F4F8FF", expand=True),
            ft.Button(content="Schlie?en", on_click=close_error),
        ], spacing=18, expand=True))

    ft.run(main)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="Enargeia")
    parser.add_argument("--configure", action="store_true", help="Konfigurationsassistent erneut öffnen")
    args = parser.parse_args(argv)

    if args.configure or not ENV_FILE.is_file():
        if not run_setup_wizard():
            return 1 if not ENV_FILE.is_file() else 0
        if args.configure:
            return 0

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(LOG_DIR / "enargeia-bootstrap.log"),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        encoding="utf-8",
    )
    try:
        settings = get_settings()
        settings.validate_neo4j()
        if settings.neo4j_mode == "local" or settings.llm_provider == "ollama-local":
            _run_service_setup(settings)
        from . import gui
        import flet as ft
        ft.run(gui.main)
        return 0
    except Exception as exc:
        logging.exception("Enargeia konnte nicht gestartet werden")
        try:
            _show_start_error(exc)
        except Exception:
            print(f"Startfehler: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

