"""Source-checkout launcher; the end-user build uses the packaged executable."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    project_root = Path(__file__).resolve().parent
    python_exe = project_root / ".venv" / "Scripts" / "python.exe"
    if not python_exe.is_file():
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None,
                "Die Entwicklungsumgebung fehlt. Bitte setup_windows.bat ausführen.",
                "Enargeia",
                0x10,
            )
        except Exception:
            pass
        return 1
    command = [str(python_exe), "-m", "winkg.bootstrap"]
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    source_path = str(project_root / "src")
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = source_path + (os.pathsep + existing_pythonpath if existing_pythonpath else "")
    return subprocess.call(command, cwd=str(project_root), env=env,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


if __name__ == "__main__":
    raise SystemExit(main())
