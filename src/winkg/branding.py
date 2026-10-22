"""Brand resources shared by Enargeia desktop windows and Flet UI."""
from __future__ import annotations

import base64
from pathlib import Path

BRAND_ASSET_DIR = Path(__file__).resolve().parent / "ui" / "assets"

def brand_asset_path(name: str) -> Path:
    """Return a bundled branding resource path."""
    path = (BRAND_ASSET_DIR / name).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Enargeia-Asset fehlt: {path}")
    return path

def image_as_base64(name: str) -> str:
    """Read an image as base64 for Flet, avoiding dependency on working-directory paths."""
    return base64.b64encode(brand_asset_path(name).read_bytes()).decode("ascii")
