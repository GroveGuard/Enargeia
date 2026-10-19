from pathlib import Path

from winkg.config import get_settings
from winkg.scanner import scan_directory


def test_scanner_indexes_files_but_skips_common_secrets_and_generated_dirs(tmp_path: Path):
    (tmp_path / "readme.md").write_text("Wissen", encoding="utf-8")
    (tmp_path / ".env").write_text("PASSWORD=example", encoding="utf-8")
    generated = tmp_path / "node_modules" / "pkg"
    generated.mkdir(parents=True)
    (generated / "index.js").write_text("module.exports = 1", encoding="utf-8")

    snapshot = scan_directory(tmp_path, get_settings())
    assert [item.relative_path for item in snapshot.files] == ["readme.md"]
    assert snapshot.skipped_sensitive == 1
    assert snapshot.folders == ("",)


def test_scanner_can_explicitly_include_sensitive_files(tmp_path: Path):
    (tmp_path / ".env").write_text("KEY=value", encoding="utf-8")
    snapshot = scan_directory(tmp_path, get_settings(), include_sensitive=True)
    assert [item.relative_path for item in snapshot.files] == [".env"]
