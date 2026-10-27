from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path


def materialize_huggingface_cache_symlinks(cache_dir: Path) -> int:
    """Replace model snapshot links with local files for Windows-safe traversal."""
    cache_root = Path(cache_dir).resolve()
    materialized = 0
    for snapshots in cache_root.glob("models--*/snapshots"):
        if not snapshots.is_dir():
            continue
        for snapshot in snapshots.iterdir():
            if not snapshot.is_dir():
                continue
            for current, directories, filenames in os.walk(snapshot, followlinks=False):
                directories[:] = [
                    name for name in directories
                    if not (Path(current) / name).is_symlink()
                ]
                for name in filenames:
                    link_path = Path(current) / name
                    if not link_path.is_symlink():
                        continue
                    link_target = os.readlink(link_path)
                    target_path = Path(os.path.abspath(link_path.parent / link_target))
                    try:
                        common_path = os.path.commonpath((str(cache_root), str(target_path)))
                    except ValueError as exc:
                        raise OSError(f"Hugging Face-Cachelink liegt auf einem anderen Laufwerk: {link_path}") from exc
                    if os.path.normcase(common_path) != os.path.normcase(str(cache_root)):
                        raise OSError(f"Hugging Face-Cachelink zeigt außerhalb des Modellcaches: {link_path}")
                    if target_path.is_symlink() or not target_path.is_file():
                        raise OSError(f"Hugging Face-Cachelink hat kein reguläres Blob-Ziel: {link_path}")

                    temporary_path: Path | None = None
                    try:
                        with tempfile.NamedTemporaryFile(
                            dir=link_path.parent, prefix=f".{link_path.name}.", suffix=".tmp", delete=False
                        ) as temporary_file:
                            temporary_path = Path(temporary_file.name)
                        shutil.copyfile(target_path, temporary_path)
                        if temporary_path.stat().st_size != target_path.stat().st_size:
                            raise OSError(f"Hugging Face-Cachekopie ist unvollständig: {link_path}")
                        os.replace(temporary_path, link_path)
                        materialized += 1
                    finally:
                        if temporary_path is not None:
                            temporary_path.unlink(missing_ok=True)
    return materialized


def disable_huggingface_cache_symlinks(cache_dir: Path) -> None:
    """Prevent new Hugging Face snapshot links that Windows may reject as untrusted."""
    if os.name != "nt":
        return
    from huggingface_hub import file_download

    # Hub 0.36.2 caches symlink capability per resolved cache directory.
    # Pinning it off avoids reintroducing links after old snapshots are copied.
    cache_root = str(Path(cache_dir).expanduser().resolve())
    symlink_cache = getattr(file_download, "_are_symlinks_supported_in_dir", None)
    if not isinstance(symlink_cache, dict):
        raise RuntimeError("Hugging Face Hub unterstützt die Windows-symlinkfreie Cachekonfiguration nicht.")
    symlink_cache[cache_root] = False
