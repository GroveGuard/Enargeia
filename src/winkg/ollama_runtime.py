"""Optional, private Ollama runtime for users who select a local LLM.

Ollama is deliberately not part of the embedding requirement. The managed LLM
uses its own loopback port and private model directory so it never silently
reuses or modifies a separately installed Ollama instance.
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path

import requests

from .config import APP_DATA_DIR, LOG_DIR, Settings
from .neo4j_runtime import Neo4jRuntimeError, _download, _safe_extract

log = logging.getLogger(__name__)
OLLAMA_WINDOWS_ZIP_URL = "https://ollama.com/download/ollama-windows-amd64.zip"
OLLAMA_HOST = "127.0.0.1:11435"
OLLAMA_PORT = 11435
DEFAULT_LOCAL_LLM = "qwen2.5:3b"


class OllamaRuntimeError(RuntimeError):
    """Raised when the optional local LLM runtime cannot be provisioned."""


def _report(progress, message: str) -> None:
    if progress:
        progress(message)


class LocalOllamaManager:
    """Provision the standalone Windows Ollama CLI only when a local LLM is selected."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.runtime_root = APP_DATA_DIR / "runtime" / "ollama"
        self.models_dir = APP_DATA_DIR / "models" / "ollama"
        self.executable = self.runtime_root / "ollama.exe"
        self.marker = self.runtime_root / "ollama-managed.marker"

    def _environment(self) -> dict[str, str]:
        env = os.environ.copy()
        env["OLLAMA_HOST"] = OLLAMA_HOST
        env["OLLAMA_MODELS"] = str(self.models_dir)
        env["OLLAMA_KEEP_ALIVE"] = "30m"
        return env

    def _api_ready(self) -> bool:
        try:
            response = requests.get(f"http://{OLLAMA_HOST}/api/version", timeout=1.5)
            return response.ok and bool(response.json().get("version"))
        except (requests.RequestException, ValueError, TypeError):
            return False

    def _find_executable(self) -> Path | None:
        if self.executable.is_file():
            return self.executable
        if self.runtime_root.exists():
            candidate = next(self.runtime_root.rglob("ollama.exe"), None)
            if candidate:
                return candidate
        return None

    def _install_runtime(self, progress=None) -> Path:
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        exe = self._find_executable()
        if exe:
            self.executable = exe
            return exe
        archive = self.runtime_root / "ollama-windows-amd64.zip"
        extract_root = self.runtime_root / "extract"
        _report(progress, "Lade optionale lokale Ollama-Laufzeit herunter …")
        try:
            _download(OLLAMA_WINDOWS_ZIP_URL, archive, progress, "Ollama")
            _safe_extract(archive, extract_root)
            candidate = next(extract_root.rglob("ollama.exe"), None)
            if candidate is None:
                raise OllamaRuntimeError("Das Ollama-Archiv enthält keine ollama.exe.")
            for child in list(extract_root.iterdir()):
                destination = self.runtime_root / child.name
                if destination.exists():
                    if destination.is_dir():
                        import shutil
                        shutil.rmtree(destination)
                    else:
                        destination.unlink()
                child.replace(destination)
            archive.unlink(missing_ok=True)
            import shutil
            shutil.rmtree(extract_root, ignore_errors=True)
            exe = self._find_executable()
            if exe is None:
                raise OllamaRuntimeError("Die Ollama-Laufzeit konnte nicht bereitgestellt werden.")
            self.executable = exe
            self.marker.write_text("source=ollama.com standalone Windows CLI\n", encoding="utf-8")
            return exe
        except (OllamaRuntimeError, Neo4jRuntimeError):
            raise
        except Exception as exc:
            raise OllamaRuntimeError(f"Ollama konnte nicht heruntergeladen/eingerichtet werden: {exc}") from exc

    def _start_server(self, exe: Path, progress=None) -> None:
        if self._api_ready():
            return
        self.models_dir.mkdir(parents=True, exist_ok=True)
        _report(progress, "Starte die private lokale LLM-Laufzeit …")
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        try:
            subprocess.Popen(
                [str(exe), "serve"],
                cwd=str(exe.parent),
                env=self._environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise OllamaRuntimeError(f"Ollama konnte nicht gestartet werden: {exc}") from exc
        started = time.monotonic()
        while time.monotonic() - started < 120:
            if self._api_ready():
                _report(progress, "Lokale LLM-Laufzeit ist bereit.")
                return
            time.sleep(1)
        raise OllamaRuntimeError(
            f"Die private Ollama-API wurde unter http://{OLLAMA_HOST} nicht bereit. "
            f"Prüfe die Laufzeitdateien unter {self.runtime_root}."
        )

    def _model_present(self, model: str) -> bool:
        try:
            response = requests.post(
                f"http://{OLLAMA_HOST}/api/show", json={"model": model}, timeout=8
            )
            return response.ok
        except requests.RequestException:
            return False

    def _ensure_model(self, exe: Path, model: str, progress=None) -> None:
        if self._model_present(model):
            return
        _report(progress, f"Lade das lokale LLM-Modell {model} herunter (kann mehrere GB benötigen) …")
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        pull_log = LOG_DIR / "ollama-model-download.log"
        started = time.monotonic()
        try:
            with pull_log.open("w", encoding="utf-8") as output:
                process = subprocess.Popen(
                    [str(exe), "pull", model],
                    cwd=str(exe.parent),
                    env=self._environment(),
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    creationflags=creationflags,
                )
                while process.poll() is None:
                    if time.monotonic() - started > 3600:
                        process.terminate()
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            process.kill()
                        raise OllamaRuntimeError(
                            f"Der Download des Modells {model} dauerte länger als erwartet. "
                            "Starte die Anwendung erneut, um den Modellstatus erneut zu prüfen."
                        )
                    _report(progress, f"Lade lokales LLM-Modell {model} herunter …")
                    time.sleep(2)
                output.flush()
            if process.returncode != 0:
                details = pull_log.read_text(encoding="utf-8", errors="replace")[-2500:]
                raise OllamaRuntimeError(f"Das lokale LLM-Modell {model} konnte nicht heruntergeladen werden: {details or 'keine Details'}")
        except OllamaRuntimeError:
            raise
        except OSError as exc:
            raise OllamaRuntimeError(f"Das lokale LLM-Modell {model} konnte nicht heruntergeladen werden: {exc}") from exc
        if not self._model_present(model):
            raise OllamaRuntimeError(f"Ollama meldet den Download von {model} nicht als abgeschlossen.")
        _report(progress, f"Lokales LLM-Modell {model} ist bereit.")

    def ensure_ready(self, progress=None) -> None:
        if self.settings.llm_provider != "ollama-local":
            return
        if os.name != "nt":
            raise OllamaRuntimeError("Die verwaltete lokale Ollama-Laufzeit ist zunächst nur für Windows verfügbar.")
        if not self.settings.llm_model.strip():
            raise OllamaRuntimeError(f"Kein lokales LLM-Modell ausgewählt (Standard: {DEFAULT_LOCAL_LLM}).")
        # The managed process uses a dedicated port and model directory. We never
        # adopt an arbitrary service on the normal Ollama port 11434.
        exe = self._install_runtime(progress)
        self._start_server(exe, progress)
        self._ensure_model(exe, self.settings.llm_model.strip(), progress)
