from __future__ import annotations

import asyncio
import json
import runpy
import zipfile
from pathlib import Path

import pytest
import tomlkit

from winkg import mcp_setup
from winkg.mcp_server import mcp


def test_client_discovery_uses_personal_config_paths_and_copilot_home(tmp_path: Path):
    home = tmp_path / "home"
    appdata = tmp_path / "roaming"
    copilot_home = tmp_path / "custom-copilot"
    codex_home = tmp_path / "custom-codex"
    codex_config = codex_home / "config.toml"
    codex_config.parent.mkdir(parents=True)
    codex_config.write_text("[mcp_servers.enargeia]\ncommand = 'test'\n", encoding="utf-8")

    statuses = mcp_setup.discover_clients(
        env={
            "USERPROFILE": str(home),
            "CODEX_HOME": str(codex_home),
            "APPDATA": str(appdata),
            "LOCALAPPDATA": str(tmp_path / "local"),
            "COPILOT_HOME": str(copilot_home),
        },
        which=lambda _: None,
    )

    assert [status.client_id for status in statuses] == ["codex", "claude", "copilot"]
    assert statuses[0].detected and statuses[0].configured
    assert statuses[0].config_path == codex_config
    assert statuses[1].config_path == appdata / "Claude" / "claude_desktop_config.json"
    assert statuses[2].config_path == copilot_home / "mcp-config.json"


def test_runtime_readiness_reports_missing_optional_mcp_dependency(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(mcp_setup.importlib.util, "find_spec", lambda _: None)

    ready, detail = mcp_setup.runtime_readiness()

    assert ready is False
    assert "pip install -e '.[mcp]'" in detail


def test_frozen_runtime_requires_sibling_mcp_executable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    gui_executable = tmp_path / "Enargeia.exe"
    mcp_executable = tmp_path / "EnargeiaMCP.exe"
    gui_executable.touch()
    monkeypatch.setattr(mcp_setup.sys, "frozen", True, raising=False)
    monkeypatch.setattr(mcp_setup.sys, "executable", str(gui_executable))

    ready, detail = mcp_setup.runtime_readiness()
    assert ready is False and str(mcp_executable) in detail

    mcp_executable.touch()
    assert mcp_setup.runtime_readiness()[0] is True


def test_json_configuration_preserves_other_servers_and_backs_up_existing_file(tmp_path: Path):
    config_path = tmp_path / "client.json"
    config_path.write_text(json.dumps({
        "otherSetting": {"enabled": True},
        "mcpServers": {"existing": {"command": "other-server"}},
    }), encoding="utf-8")

    backup = mcp_setup.configure_json_client("claude", config_path)

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert backup is not None and backup.is_file()
    assert saved["otherSetting"] == {"enabled": True}
    assert saved["mcpServers"]["existing"] == {"command": "other-server"}
    assert saved["mcpServers"]["enargeia"] == mcp_setup.client_config("claude")


def test_json_configuration_is_idempotent_and_refuses_invalid_files(tmp_path: Path):
    config_path = tmp_path / "client.json"
    config_path.write_text('{"mcpServers":{}}', encoding="utf-8")
    mcp_setup.configure_json_client("copilot", config_path)
    assert mcp_setup.configure_json_client("copilot", config_path) is None

    config_path.write_text("{invalid json", encoding="utf-8")
    with pytest.raises(ValueError, match="ungültig"):
        mcp_setup.configure_json_client("copilot", config_path)
    assert config_path.read_text(encoding="utf-8") == "{invalid json"


def test_codex_configuration_preserves_comments_and_existing_entries(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "# Keep this comment\nmodel = 'test-model'\n\n"
        "[mcp_servers.existing]\ncommand = 'other'\n",
        encoding="utf-8",
    )

    backup = mcp_setup.configure_codex(config_path)
    parsed = tomlkit.parse(config_path.read_text(encoding="utf-8"))

    assert backup is not None and backup.is_file()
    assert "# Keep this comment" in config_path.read_text(encoding="utf-8")
    assert parsed["model"] == "test-model"
    assert parsed["mcp_servers"]["existing"]["command"] == "other"
    assert dict(parsed["mcp_servers"]["enargeia"]) == mcp_setup.client_config("codex")
    assert mcp_setup.configure_codex(config_path) is None


def test_codex_configuration_refuses_malformed_toml(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text("[broken\n", encoding="utf-8")
    with pytest.raises(ValueError, match="ungültig"):
        mcp_setup.configure_codex(config_path)
    assert config_path.read_text(encoding="utf-8") == "[broken\n"


def test_configuration_backup_can_be_restored_without_losing_current_file(tmp_path: Path):
    config_path = tmp_path / "claude.json"
    config_path.write_text('{"before":true}', encoding="utf-8")
    backup = mcp_setup.configure_json_client("claude", config_path)
    assert backup is not None
    configured_contents = config_path.read_text(encoding="utf-8")

    current_backup = mcp_setup.restore_configuration_backup(config_path, backup)

    assert json.loads(config_path.read_text(encoding="utf-8")) == {"before": True}
    assert current_backup is not None
    assert current_backup.read_text(encoding="utf-8") == configured_contents


def test_stdio_probe_lists_tools_and_server_metadata():
    count, server_name = asyncio.run(mcp_setup.probe_server())
    assert count == 12
    assert server_name == "Enargeia Knowledge Graph"


def test_tool_annotations_distinguish_reads_from_graph_changes():
    tools = {tool.name: tool for tool in mcp._tool_manager.list_tools()}

    assert tools["search_knowledge"].annotations.readOnlyHint is True
    assert tools["get_graph_stats"].annotations.readOnlyHint is True
    assert tools["analyze_knowledge_graph"].annotations.readOnlyHint is False
    assert tools["review_graph_link_candidate"].annotations.destructiveHint is True


def test_mcpb_bundle_contains_windows_binary_manifest(tmp_path: Path):
    source = tmp_path / "EnargeiaMCP.exe"
    output = tmp_path / "Enargeia-MCP.mcpb"
    source.write_bytes(b"test binary")
    script = Path(__file__).resolve().parents[1] / "packaging" / "windows" / "build_mcpb.py"
    build = runpy.run_path(str(script))["build"]

    build(source, output)
    with zipfile.ZipFile(output) as bundle:
        manifest = json.loads(bundle.read("manifest.json"))
        assert manifest["server"]["type"] == "binary"
        assert manifest["server"]["entry_point"] == "server/EnargeiaMCP.exe"
        assert bundle.read("server/EnargeiaMCP.exe") == b"test binary"
