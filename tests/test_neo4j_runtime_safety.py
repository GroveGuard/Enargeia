from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from winkg.neo4j_runtime import Neo4jRuntimeError, _safe_extract


def test_safe_extract_rejects_zip_slip(tmp_path: Path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../escape.txt", "bad")
    with pytest.raises(Neo4jRuntimeError, match="ungültigen Dateipfad"):
        _safe_extract(archive, tmp_path / "out")
    assert not (tmp_path / "escape.txt").exists()


def test_safe_extract_extracts_valid_archive(tmp_path: Path):
    archive = tmp_path / "good.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("neo4j/bin/neo4j.bat", "@echo off")
    destination = tmp_path / "out"
    _safe_extract(archive, destination)
    assert (destination / "neo4j" / "bin" / "neo4j.bat").read_text(encoding="utf-8") == "@echo off"


def test_local_runtime_launches_console_mode_not_windows_service(tmp_path: Path, monkeypatch):
    """The ZIP distribution is started as a foreground process, not as a service."""
    from winkg.config import Settings
    from winkg.neo4j_runtime import LocalNeo4jManager
    import winkg.neo4j_runtime as runtime_module

    manager = LocalNeo4jManager(Settings())
    manager.server_home = tmp_path / "neo4j"
    manager.log_dir = tmp_path / "logs"
    manager.console_log = manager.log_dir / "neo4j-console.log"
    manager._environment = lambda java_home: {"JAVA_HOME": str(java_home)}

    captured = {}

    class FakeProcess:
        returncode = None

        def poll(self):
            return None

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(runtime_module.subprocess, "Popen", fake_popen)
    executable = tmp_path / "neo4j" / "bin" / "neo4j.bat"
    java_home = tmp_path / "java"
    manager._launch_console(executable, java_home)

    assert captured["command"].endswith('neo4j.bat" console')
    assert not captured["command"].endswith(" start")
    assert captured["kwargs"]["shell"] is True
    assert captured["kwargs"]["stdin"] == runtime_module.subprocess.DEVNULL
    assert captured["kwargs"]["stderr"] == runtime_module.subprocess.STDOUT
    assert manager.console_log.parent.is_dir()
    assert manager._neo4j_process is not None


def test_console_log_tail_is_included_for_runtime_diagnostics(tmp_path: Path):
    from winkg.config import Settings
    from winkg.neo4j_runtime import LocalNeo4jManager

    manager = LocalNeo4jManager(Settings())
    manager.log_dir = tmp_path / "logs"
    manager.console_log = manager.log_dir / "neo4j-console.log"
    manager.log_dir.mkdir(parents=True)
    manager.console_log.write_text("first line\nNeo4j failed to bind\n", encoding="utf-8")

    tail = manager._read_console_log_tail()
    assert "Neo4j failed to bind" in tail
