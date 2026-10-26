from __future__ import annotations

import json
import tomllib
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_development_extra_contains_runtime_and_test_dependencies():
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dev = project["project"]["optional-dependencies"]["dev"]

    assert any(item.startswith("flet[desktop]==1.0.5") for item in dev)
    assert any(item.startswith("fastembed==0.9.0") for item in dev)
    assert any(item.startswith("docling>=2.0") for item in dev)
    assert any(item.startswith("pytest>=8.0") for item in dev)


def test_source_workflow_keeps_release_build_separate():
    dev_script = (PROJECT_ROOT / "scripts" / "dev.ps1").read_text(encoding="utf-8")
    build_script = (PROJECT_ROOT / "packaging" / "windows" / "build_windows.ps1").read_text(encoding="utf-8")

    assert "pip', 'install', '-e', '.[dev]'" in dev_script
    assert "-Command run" not in build_script
    assert ".venv-packaging" in build_script


def test_vscode_launches_source_module_with_development_venv():
    launch = json.loads((PROJECT_ROOT / ".vscode" / "launch.json").read_text(encoding="utf-8"))
    gui = next(item for item in launch["configurations"] if item["name"] == "Enargeia GUI (Quellcode)")

    assert gui["module"] == "winkg.bootstrap"
    assert ".venv\\Scripts\\python.exe" in gui["python"]
    assert gui["env"]["PYTHONPATH"].endswith("\\src")
