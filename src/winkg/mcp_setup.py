from __future__ import annotations

import json
import importlib.util
import os
import shutil
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

MCP_SERVER_NAME = "enargeia"


@dataclass(frozen=True)
class ClientStatus:
    client_id: str
    label: str
    detected: bool
    config_path: Path
    configured: bool
    detail: str
    error: str = ""


def runtime_readiness() -> tuple[bool, str]:
    if bool(getattr(sys, "frozen", False)):
        executable = Path(sys.executable).resolve().with_name("EnargeiaMCP.exe")
        if executable.is_file():
            return True, f"MCP-Server bereit: {executable}"
        return False, f"MCP-Server-Binärdatei fehlt: {executable}"
    try:
        available = importlib.util.find_spec("mcp") is not None
    except (ImportError, ValueError):
        available = False
    if available:
        return True, "MCP-Abhängigkeit ist in dieser Python-Umgebung installiert."
    return False, "MCP-Abhängigkeit fehlt. Installiere sie mit: pip install -e '.[mcp]'"


def _user_path(env: dict[str, str], name: str, fallback: Path) -> Path:
    value = env.get(name)
    return Path(value).expanduser() if value else fallback


def server_launch() -> tuple[str, list[str], dict[str, str]]:
    """Return the command, arguments, and environment needed by an MCP host."""
    if bool(getattr(sys, "frozen", False)):
        executable = Path(sys.executable).resolve().with_name("EnargeiaMCP.exe")
        if not executable.is_file():
            raise FileNotFoundError(f"Der MCP-Server wurde nicht gefunden: {executable}")
        return str(executable), [], {}

    project_root = Path(__file__).resolve().parents[2]
    current_pythonpath = os.environ.get("PYTHONPATH", "")
    pythonpath = (
        str(project_root) + (os.pathsep + current_pythonpath if current_pythonpath else "")
    )
    return sys.executable, ["-m", "winkg.mcp_server"], {
        "PYTHONPATH": pythonpath,
    }


def client_config(client_id: str) -> dict[str, Any]:
    command, args, env = server_launch()
    if client_id == "codex":
        result: dict[str, Any] = {"command": command, "args": args}
        if env:
            result["env"] = env
        return result
    if client_id in {"claude", "copilot"}:
        return {"command": command, "args": args, **({"env": env} if env else {})}
    raise ValueError(f"Unbekannter MCP-Client: {client_id}")


def _config_state(path: Path, client_id: str) -> tuple[bool, str]:
    if not path.exists():
        return False, ""
    try:
        content = path.read_text(encoding="utf-8")
        if client_id == "codex":
            import tomlkit
            document = tomlkit.parse(content)
            servers = document.get("mcp_servers", {})
            return MCP_SERVER_NAME in servers, ""
        document = json.loads(content)
        servers = document.get("mcpServers", {})
        if not isinstance(servers, dict):
            return False, "Die vorhandene mcpServers-Konfiguration ist kein JSON-Objekt."
        return MCP_SERVER_NAME in servers, ""
    except (OSError, ValueError, TypeError, ImportError) as exc:
        return False, f"Konfigurationsdatei kann nicht sicher gelesen werden: {exc}"


def discover_clients(
    env: dict[str, str] | None = None,
    which: Any = shutil.which,
) -> list[ClientStatus]:
    environment = dict(os.environ if env is None else env)
    home = _user_path(environment, "USERPROFILE", Path.home())
    appdata = _user_path(environment, "APPDATA", home / "AppData" / "Roaming")
    local = _user_path(environment, "LOCALAPPDATA", home / "AppData" / "Local")

    codex_home = _user_path(environment, "CODEX_HOME", home / ".codex")
    copilot_home = _user_path(environment, "COPILOT_HOME", home / ".copilot")
    client_paths = {
        "codex": codex_home / "config.toml",
        "claude": appdata / "Claude" / "claude_desktop_config.json",
        "copilot": copilot_home / "mcp-config.json",
    }
    installed = {
        "codex": bool(which("codex")) or any(path.is_file() for path in (
            local / "Programs" / "Codex" / "Codex.exe",
            local / "Codex" / "Codex.exe",
        )),
        "claude": any(path.is_file() for path in (
            local / "Programs" / "Claude" / "Claude.exe",
            local / "AnthropicClaude" / "Claude.exe",
            Path(environment.get("ProgramFiles", "C:\\Program Files")) / "Claude" / "Claude.exe",
        )),
        "copilot": bool(which("code")) or any(path.is_file() for path in (
            local / "Programs" / "Microsoft VS Code" / "Code.exe",
            Path(environment.get("ProgramFiles", "C:\\Program Files"))
            / "Microsoft VS Code" / "Code.exe",
        )),
    }
    labels = {
        "codex": "Codex / ChatGPT Desktop",
        "claude": "Claude Desktop",
        "copilot": "VS Code / Copilot",
    }
    results = []
    for client_id, path in client_paths.items():
        configured, error = _config_state(path, client_id)
        detected = installed[client_id] or path.exists()
        if error:
            detail = error
        elif configured:
            detail = f"Enargeia ist in {path} eingetragen."
        elif detected:
            detail = f"Konfigurationsziel: {path}"
        else:
            detail = "Client nicht erkannt. Die manuelle Einrichtung ist weiterhin verfügbar."
        results.append(ClientStatus(
            client_id, labels[client_id], detected, path, configured, detail, error
        ))
    return results


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    suffix = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    backup_path = path.with_name(f"{path.name}.enargeia-{suffix}.bak")
    shutil.copy2(path, backup_path)
    return backup_path


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        temporary_path.replace(path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def restore_configuration_backup(target: Path, backup: Path) -> Path | None:
    if (
        backup.parent.resolve() != target.parent.resolve()
        or not backup.name.startswith(target.name + ".enargeia-")
        or not backup.name.endswith(".bak")
        or not backup.is_file()
    ):
        raise ValueError("Die Sicherungskopie passt nicht zur MCP-Konfigurationsdatei.")
    current_backup = _backup(target)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=target.parent, prefix=f".{target.name}.",
            suffix=".restore", delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(backup.read_bytes())
            temporary.flush()
            os.fsync(temporary.fileno())
        temporary_path.replace(target)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
    return current_backup


def configure_json_client(client_id: str, path: Path | None = None) -> Path | None:
    if client_id not in {"claude", "copilot"}:
        raise ValueError(f"JSON-Konfiguration wird für {client_id!r} nicht unterstützt.")
    target = path or next(
        status.config_path for status in discover_clients()
        if status.client_id == client_id
    )
    if target.exists():
        try:
            document = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Die vorhandene JSON-Konfiguration ist ungültig: {exc}") from exc
    else:
        document = {}
    if not isinstance(document, dict):
        raise ValueError("Die vorhandene Client-Konfiguration muss ein JSON-Objekt sein.")
    servers = document.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError("Die vorhandene mcpServers-Konfiguration ist kein JSON-Objekt.")
    desired = client_config(client_id)
    if servers.get(MCP_SERVER_NAME) == desired:
        return None
    backup = _backup(target)
    servers[MCP_SERVER_NAME] = desired
    _atomic_write(target, json.dumps(document, ensure_ascii=False, indent=2) + "\n")
    return backup


def configure_codex(path: Path | None = None) -> Path | None:
    target = path or next(
        status.config_path for status in discover_clients()
        if status.client_id == "codex"
    )
    try:
        import tomlkit
    except ImportError as exc:
        raise RuntimeError(
            "Für die sichere Codex-Konfiguration fehlt tomlkit. Installiere Enargeia mit dem MCP-Extra."
        ) from exc
    if target.exists():
        try:
            document = tomlkit.parse(target.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError) as exc:
            raise ValueError(f"Die vorhandene Codex-TOML-Datei ist ungültig: {exc}") from exc
    else:
        document = tomlkit.document()
    servers = document.get("mcp_servers")
    if servers is None:
        servers = tomlkit.table()
        document["mcp_servers"] = servers
    if not hasattr(servers, "get"):
        raise ValueError("Der mcp_servers-Abschnitt in der Codex-Konfiguration ist ungültig.")
    desired = client_config("codex")
    existing = servers.get(MCP_SERVER_NAME)
    if existing is not None and dict(existing) == desired:
        return None
    backup = _backup(target)
    server_table = tomlkit.table()
    for key, value in desired.items():
        if key == "env":
            env_table = tomlkit.inline_table()
            for env_key, env_value in value.items():
                env_table[env_key] = env_value
            server_table[key] = env_table
        else:
            server_table[key] = value
    servers[MCP_SERVER_NAME] = server_table
    _atomic_write(target, tomlkit.dumps(document))
    return backup


async def probe_server() -> tuple[int, str]:
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError as exc:
        raise RuntimeError(
            "Die MCP-Abhängigkeit fehlt. Installiere Enargeia mit der MCP-Komponente."
        ) from exc

    command, args, env = server_launch()
    parameters = StdioServerParameters(
        command=command,
        args=args,
        env={**os.environ, **env} if env else None,
    )
    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            initialized = await session.initialize()
            tools = await session.list_tools()
            return len(tools.tools), initialized.serverInfo.name


def mcpb_bundle_path(project_root: Path, frozen: bool) -> Path:
    if frozen:
        return project_root / "extensions" / "Enargeia-MCP.mcpb"
    return project_root / "dist" / "Enargeia-MCP.mcpb"
