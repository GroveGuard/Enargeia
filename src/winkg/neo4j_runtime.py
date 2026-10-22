"""Provision and start the per-user Neo4j Community runtime on Windows.

Runtime versions are intentionally pinned together. Downloads are fetched over HTTPS;
where the publisher exposes a SHA-256 checksum, it is verified before extraction.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

from .config import APP_DATA_DIR, Settings

log = logging.getLogger(__name__)
Progress = Callable[[str], None]

# Pin the database and its plugin as a tested compatibility pair. Update together.
NEO4J_VERSION = "2026.09.0"
GDS_VERSION = "2026.09.0"
JAVA_FEATURE_VERSION = "21"
NEO4J_ZIP_URL = f"https://dist.neo4j.org/neo4j-community-{NEO4J_VERSION}-windows.zip"
GDS_JAR_URL = f"https://graphdatascience.ninja/neo4j-graph-data-science-{GDS_VERSION}.jar"


class Neo4jRuntimeError(RuntimeError):
    """Raised when the app-managed local database cannot be provisioned or started."""


def _report(progress: Progress | None, text: str) -> None:
    log.info("%s", text)
    if progress:
        progress(text)


def _checksum_from_url(url: str) -> str | None:
    """Return the publisher's SHA-256 sidecar if one is available."""
    for suffix in (".sha256", ".sha256sum"):
        try:
            request = urllib.request.Request(url + suffix, headers={"User-Agent": "Enargeia/1.2.1"})
            with urllib.request.urlopen(request, timeout=15) as response:
                body = response.read(4096).decode("ascii", errors="ignore")
            match = re.search(r"\b([0-9a-fA-F]{64})\b", body)
            if match:
                return match.group(1).lower()
        except (OSError, urllib.error.URLError, TimeoutError):
            continue
    return None


def _download(url: str, target: Path, progress: Progress | None, label: str,
              expected_sha256: str | None = None) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".part")
    digest = hashlib.sha256()
    request = urllib.request.Request(url, headers={"User-Agent": "Enargeia/1.2.1"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as output:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if total and progress:
                    progress(f"{label}: {done / total:.0%} heruntergeladen")
        actual = digest.hexdigest()
        if expected_sha256 and actual.lower() != expected_sha256.lower():
            raise Neo4jRuntimeError(f"SHA-256-Prüfung für {label} fehlgeschlagen.")
        if temporary.stat().st_size < 1024:
            raise Neo4jRuntimeError(f"Der Download für {label} ist unerwartet klein oder leer.")
        temporary.replace(target)
        return actual
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _safe_extract(archive: Path, target: Path) -> None:
    target = target.resolve()
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            candidate = (target / info.filename).resolve()
            if candidate != target and target not in candidate.parents:
                raise Neo4jRuntimeError("Das heruntergeladene ZIP enthält einen ungültigen Dateipfad.")
        bad = zf.testzip()
        if bad:
            raise Neo4jRuntimeError(f"Das ZIP-Archiv ist beschädigt: {bad}")
        zf.extractall(target)


def _adoptium_jre_package() -> tuple[str, str]:
    url = (
        "https://api.adoptium.net/v3/assets/latest/21/hotspot"
        "?architecture=x64&image_type=jre&os=windows&vendor=eclipse"
    )
    request = urllib.request.Request(url, headers={"User-Agent": "Enargeia/1.2.1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        assets = json.loads(response.read().decode("utf-8"))
    if not assets:
        raise Neo4jRuntimeError("Adoptium hat kein passendes Windows-JRE-Paket geliefert.")
    package = assets[0]["binary"]["package"]
    link, checksum = package.get("link"), package.get("checksum")
    if not link or not checksum:
        raise Neo4jRuntimeError("Die Download-Metadaten des Java-Runtimes enthalten keinen Link oder SHA-256.")
    return str(link), str(checksum)


class LocalNeo4jManager:
    """Install a private database runtime and launch it without creating a Windows service."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.runtime_root = APP_DATA_DIR / "runtime" / "neo4j"
        self.java_root = APP_DATA_DIR / "runtime" / "java"
        self.server_home = self.runtime_root / "server"
        self.conf_dir = self.runtime_root / "conf"
        self.data_dir = APP_DATA_DIR / "data" / "neo4j"
        self.log_dir = APP_DATA_DIR / "logs" / "neo4j"
        self.plugins_dir = self.runtime_root / "plugins"
        self.marker = self.runtime_root / f"neo4j-{NEO4J_VERSION}.installed"
        self.console_log = self.log_dir / "neo4j-console.log"
        self._neo4j_process: subprocess.Popen | None = None
        self._console_log_handle = None

    @property
    def bolt_port(self) -> int:
        return self.settings.neo4j_local_port

    @property
    def bolt_address(self) -> tuple[str, int]:
        return ("127.0.0.1", self.bolt_port)

    def _java_home(self) -> Path | None:
        matches = list(self.java_root.rglob("java.exe")) if self.java_root.exists() else []
        return matches[0].parent.parent if matches else None

    def _find_neo4j_home(self, search_root: Path) -> Path | None:
        if not search_root.exists():
            return None
        scripts = list(search_root.rglob("neo4j.bat"))
        return scripts[0].parent.parent if scripts else None

    def _environment(self, java_home: Path) -> dict[str, str]:
        env = os.environ.copy()
        env["NEO4J_HOME"] = str(self.server_home)
        env["NEO4J_CONF"] = str(self.conf_dir)
        env["JAVA_HOME"] = str(java_home)
        env["PATH"] = str(java_home / "bin") + os.pathsep + env.get("PATH", "")
        return env

    def _write_config(self) -> None:
        self.conf_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        port = self.settings.neo4j_local_port
        config = {
            "server.default_listen_address": "127.0.0.1",
            "server.bolt.listen_address": f"127.0.0.1:{port}",
            "server.http.listen_address": f"127.0.0.1:{port + 1000 if port <= 64535 else 17474}",
            "server.directories.data": self.data_dir.as_posix(),
            "server.directories.logs": self.log_dir.as_posix(),
            "server.directories.plugins": self.plugins_dir.as_posix(),
            "server.memory.heap.initial_size": "512m",
            "server.memory.heap.max_size": "2G",
        }
        if self.settings.gds_enabled:
            config["dbms.security.procedures.allowlist"] = "gds.*"
            config["dbms.security.procedures.unrestricted"] = "gds.*"
        lines = ["# Managed by Enargeia; local loopback-only instance."]
        lines.extend(f"{key}={value}" for key, value in config.items())
        (self.conf_dir / "neo4j.conf").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _download_neo4j(self, progress: Progress | None) -> None:
        archive = self.runtime_root / f"neo4j-community-{NEO4J_VERSION}-windows.zip"
        _report(progress, f"Lade Neo4j Community {NEO4J_VERSION} herunter …")
        expected = _checksum_from_url(NEO4J_ZIP_URL)
        if expected is None:
            log.warning("Für das Neo4j-Archiv wurde kein SHA-256-Sidecar gefunden; HTTPS und ZIP-Integritätsprüfung werden verwendet.")
        digest = _download(NEO4J_ZIP_URL, archive, progress, "Neo4j", expected)
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        extract_root = self.runtime_root / "neo4j-extract"
        shutil.rmtree(extract_root, ignore_errors=True)
        _safe_extract(archive, extract_root)
        source_home = self._find_neo4j_home(extract_root)
        if source_home is None:
            raise Neo4jRuntimeError("Das Neo4j-Archiv enthält keine erkennbare Windows-Installation.")
        if self.server_home.exists():
            shutil.rmtree(self.server_home)
        shutil.copytree(source_home, self.server_home)
        shutil.rmtree(extract_root, ignore_errors=True)
        archive.unlink(missing_ok=True)
        (self.runtime_root / "neo4j.sha256").write_text(digest + "\n", encoding="ascii")

    def _download_java(self, progress: Progress | None) -> Path:
        existing = self._java_home()
        if existing:
            return existing
        _report(progress, "Ermittle kompatible Java-21-Laufzeit …")
        url, checksum = _adoptium_jre_package()
        package_path = self.java_root.parent / "temurin-jre-21-windows-x64.zip"
        _report(progress, "Lade Java-Laufzeit herunter …")
        _download(url, package_path, progress, "Java", checksum)
        extract_root = self.java_root.parent / "java-extract"
        shutil.rmtree(extract_root, ignore_errors=True)
        _safe_extract(package_path, extract_root)
        java_exe = next(iter(extract_root.rglob("java.exe")), None)
        if java_exe is None:
            raise Neo4jRuntimeError("Das Java-Archiv enthält keine java.exe.")
        jre_home = java_exe.parent.parent
        self.java_root.parent.mkdir(parents=True, exist_ok=True)
        if self.java_root.exists():
            shutil.rmtree(self.java_root)
        shutil.copytree(jre_home, self.java_root)
        shutil.rmtree(extract_root, ignore_errors=True)
        package_path.unlink(missing_ok=True)
        result = self._java_home()
        if result is None:
            raise Neo4jRuntimeError("Die Java-Laufzeit konnte nicht bereitgestellt werden.")
        return result

    def _download_gds(self, progress: Progress | None) -> None:
        if not self.settings.gds_enabled:
            return
        expected_jar = self.plugins_dir / f"neo4j-graph-data-science-{GDS_VERSION}.jar"
        if expected_jar.is_file() and expected_jar.stat().st_size > 1024 * 1024:
            return
        _report(progress, f"Lade kompatible GDS-Version {GDS_VERSION} herunter …")
        digest = _checksum_from_url(GDS_JAR_URL)
        if digest is None:
            log.warning("Für die GDS-JAR wurde kein SHA-256-Sidecar gefunden; Download erfolgt über HTTPS.")
        _download(GDS_JAR_URL, expected_jar, progress, "Neo4j GDS", digest)
        try:
            if not zipfile.is_zipfile(expected_jar):
                raise Neo4jRuntimeError("Der GDS-Download ist keine gültige JAR-Datei.")
        except Exception:
            expected_jar.unlink(missing_ok=True)
            raise

    def ensure_installed(self, progress: Progress | None = None) -> None:
        if os.name != "nt":
            raise Neo4jRuntimeError("Die automatische lokale Neo4j-Bereitstellung ist zunächst nur für Windows x64 verfügbar.")
        if self.settings.neo4j_mode != "local":
            return
        if not self.marker.is_file() or self._find_neo4j_home(self.server_home) is None:
            self._download_neo4j(progress)
        java_home = self._download_java(progress)
        self._write_config()
        self._download_gds(progress)
        # Re-write after optional GDS installation so the allowlist matches the selection.
        self._write_config()
        if not self.marker.exists():
            if not self.settings.neo4j_password:
                raise Neo4jRuntimeError("Das Passwort für die lokale Neo4j-Instanz fehlt. Bitte Setup erneut öffnen.")
            if len(self.settings.neo4j_password) < 16 or not re.fullmatch(r"[A-Za-z0-9_-]+", self.settings.neo4j_password):
                raise Neo4jRuntimeError(
                    "Das lokale Neo4j-Passwort muss mindestens 16 Zeichen lang sein und nur Buchstaben, Zahlen, '_' oder '-' enthalten. "
                    "Bitte öffne den Setup-Assistenten und speichere ein geeignetes Passwort."
                )
            _report(progress, "Initialisiere die lokale Datenbank und Zugangsdaten …")
            admin = self.server_home / "bin" / "neo4j-admin.bat"
            if not admin.is_file():
                raise Neo4jRuntimeError(f"Neo4j-Admin wurde nicht gefunden: {admin}")
            result = subprocess.run(
                f'"{admin}" dbms set-initial-password "{self.settings.neo4j_password}"',
                shell=True,
                cwd=str(self.server_home),
                env=self._environment(java_home),
                capture_output=True,
                text=True,
                timeout=180,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode != 0:
                details = (result.stderr or result.stdout or "keine Details").strip()[-3000:]
                raise Neo4jRuntimeError(f"Initiales Neo4j-Passwort konnte nicht gesetzt werden: {details}")
            self.marker.write_text(
                f"neo4j={NEO4J_VERSION}\ngds={GDS_VERSION if self.settings.gds_enabled else 'disabled'}\n",
                encoding="utf-8",
            )

    def _verify_existing_managed_instance(self) -> None:
        """Refuse to silently adopt an unrelated service already using our port."""
        try:
            from neo4j import GraphDatabase

            uri = f"neo4j://127.0.0.1:{self.bolt_port}"
            with GraphDatabase.driver(
                uri,
                auth=(self.settings.neo4j_username, self.settings.neo4j_password),
                connection_timeout=4,
            ) as driver:
                driver.verify_connectivity()
                with driver.session(database="system") as session:
                    record = session.run(
                        "CALL dbms.components() YIELD versions RETURN versions[0] AS version"
                    ).single()
            version = str(record["version"]) if record else ""
        except Exception as exc:
            raise Neo4jRuntimeError(
                f"Der lokale Bolt-Port 127.0.0.1:{self.bolt_port} ist belegt, aber die verwaltete Neo4j-Instanz "
                "konnte nicht mit den gespeicherten Zugangsdaten verifiziert werden. "
                "Es wurde keine fremde Datenbank übernommen. Prüfe den Port in den Einstellungen."
            ) from exc
        if version != NEO4J_VERSION:
            raise Neo4jRuntimeError(
                f"Am lokalen Bolt-Port läuft Neo4j {version or 'mit unbekannter Version'}, erwartet wird "
                f"die verwaltete Version {NEO4J_VERSION}. Aus Sicherheitsgründen wird diese Instanz nicht übernommen."
            )

    def start(self, progress: Progress | None = None, timeout_seconds: int = 150) -> None:
        if self.settings.neo4j_mode != "local":
            return
        if os.name != "nt":
            raise Neo4jRuntimeError("Der verwaltete lokale Neo4j-Prozess wird zunächst nur unter Windows unterstützt.")
        self.ensure_installed(progress)
        try:
            with socket.create_connection(self.bolt_address, timeout=2):
                self._verify_existing_managed_instance()
                _report(progress, "Lokale Neo4j-Instanz läuft und wurde verifiziert.")
                return
        except Neo4jRuntimeError:
            raise
        except OSError:
            pass
        java_home = self._java_home()
        if java_home is None:
            raise Neo4jRuntimeError("Die lokale Java-Laufzeit ist nicht installiert.")
        executable = self.server_home / "bin" / "neo4j.bat"
        if not executable.is_file():
            raise Neo4jRuntimeError(f"Neo4j-Startprogramm wurde nicht gefunden: {executable}")
        _report(progress, "Starte lokale Neo4j-Instanz …")
        self._launch_console(executable, java_home)
        started = time.monotonic()
        while time.monotonic() - started < timeout_seconds:
            # Unlike `neo4j start`, `neo4j console` does not require a Windows
            # service installation. Keep it as an app-managed foreground process,
            # redirecting its output so it can run without a visible console window.
            process = self._neo4j_process
            if process is not None and process.poll() is not None:
                details = self._read_console_log_tail()
                raise Neo4jRuntimeError(
                    f"Neo4j wurde während des Starts beendet (Exit-Code {process.returncode}). "
                    f"Letzte Neo4j-Ausgaben:\n{details or 'Keine Ausgabe vorhanden.'}"
                )
            try:
                with socket.create_connection(self.bolt_address, timeout=2):
                    self._verify_existing_managed_instance()
                    _report(progress, "Neo4j ist bereit und wurde verifiziert.")
                    return
            except OSError:
                time.sleep(1.0)
        details = self._read_console_log_tail()
        raise Neo4jRuntimeError(
            f"Neo4j hat den Bolt-Port 127.0.0.1:{self.bolt_port} nicht innerhalb von {timeout_seconds} Sekunden geöffnet. "
            f"Letzte Neo4j-Ausgaben:\n{details or 'Keine Ausgabe vorhanden.'}\n"
            f"Weitere Logs: {self.log_dir}"
        )

    def _launch_console(self, executable: Path, java_home: Path) -> None:
        """Launch Neo4j in console mode without requiring a Windows service."""
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._console_log_handle = self.console_log.open("ab")
        try:
            self._neo4j_process = subprocess.Popen(
                f'"{executable}" console',
                shell=True,
                cwd=str(self.server_home),
                env=self._environment(java_home),
                stdin=subprocess.DEVNULL,
                stdout=self._console_log_handle,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            self._console_log_handle.close()
            self._console_log_handle = None
            raise

    def _read_console_log_tail(self, max_bytes: int = 12000) -> str:
        """Read recent managed Neo4j output for actionable startup errors."""
        try:
            with self.console_log.open("rb") as handle:
                handle.seek(max(0, self.console_log.stat().st_size - max_bytes))
                return handle.read().decode("utf-8", errors="replace").strip()[-6000:]
        except OSError:
            return ""
