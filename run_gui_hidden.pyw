"""Start the GUI without a console while keeping a fallback output log."""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def _rotate_fallback_log(path: Path, max_bytes: int = 2 * 1024 * 1024) -> None:
    """Keep the redirected stdout/stderr log from growing without bound."""
    try:
        if path.exists() and path.stat().st_size >= max_bytes:
            backup = path.with_suffix(path.suffix + ".1")
            try:
                backup.unlink(missing_ok=True)
            except OSError:
                pass
            path.replace(backup)
    except OSError:
        # Logging must not prevent the GUI from starting.
        pass


def main() -> int:
    project_root = Path(__file__).resolve().parent
    logs_dir = project_root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    python_exe = project_root / ".venv" / "Scripts" / "python.exe"
    fallback_log = logs_dir / "winkg-gui-console.log"
    _rotate_fallback_log(fallback_log)

    with fallback_log.open("a", encoding="utf-8", buffering=1) as log:
        log.write(f"\n--- GUI launcher started {datetime.now().isoformat(timespec='seconds')} ---\n")
        if not python_exe.is_file():
            message = f"Python environment not found: {python_exe}"
            log.write(message + "\n")
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(
                    None,
                    "Die Projekt-Python-Umgebung wurde nicht gefunden.\nBitte zuerst setup_windows.bat ausführen.\n\nDetails stehen in logs\\winkg-gui-console.log.",
                    "Windows Knowledge Graph",
                    0x10,
                )
            except Exception:
                pass
            return 1

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        try:
            result = subprocess.run(
                [str(python_exe), "-m", "winkg.gui"],
                cwd=str(project_root / "src"),
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
            log.write(
                f"--- GUI process ended {datetime.now().isoformat(timespec='seconds')}; "
                f"exit_code={result.returncode} ---\n"
            )
            return result.returncode
        except Exception as exc:
            log.write(f"Launcher exception: {exc!r}\n")
            return 1


if __name__ == "__main__":
    sys.exit(main())
