from __future__ import annotations

import argparse
import json
import zipfile
from importlib.metadata import version
from pathlib import Path


def build(source: Path, output: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(f"MCP server executable not found: {source}")
    manifest = {
        "manifest_version": "0.3",
        "name": "enargeia-knowledge-graph",
        "display_name": "Enargeia Knowledge Graph",
        "version": version("enargeia"),
        "description": (
            "Connects Claude Desktop to the local Enargeia knowledge graph "
            "for search, file context, and graph analysis."
        ),
        "author": {"name": "Enargeia"},
        "server": {
            "type": "binary",
            "entry_point": "server/EnargeiaMCP.exe",
            "mcp_config": {
                "command": "${__dirname}/server/EnargeiaMCP.exe",
                "args": [],
            },
        },
        "compatibility": {"platforms": ["win32"]},
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(
            "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        )
        bundle.write(source, "server/EnargeiaMCP.exe")


def main() -> None:
    parser = argparse.ArgumentParser(description="Package Enargeia as a Claude Desktop MCPB.")
    parser.add_argument("--server", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    build(args.server, args.output)


if __name__ == "__main__":
    main()
