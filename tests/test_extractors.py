from pathlib import Path

import pytest

from winkg.config import get_settings
from winkg.extractors import _docling_extract, extract_text


def test_extract_plain_text():
    text = extract_text(Path("sample.py"), b"def hello():\n    return 'Hallo'\n", get_settings())
    assert "def hello" in text
    assert "Hallo" in text


def test_extract_json_formats_pretty():
    result = extract_text(Path("sample.json"), b'{"hello":"world","n":1}', get_settings())
    assert '"hello"' in result
    assert "world" in result


def test_unsupported_binary_is_empty():
    assert extract_text(Path("sample.exe"), b"MZ\x00\x01", get_settings()) == ""


def test_extract_extensionless_config_files():
    assert "FROM python" in extract_text(Path("Dockerfile"), b"FROM python:3.12\n", get_settings())
    assert "*.pyc" in extract_text(Path(".gitignore"), b"*.pyc\n", get_settings())


def test_enabled_docling_fails_explicitly_when_dependency_is_missing(monkeypatch, tmp_path):
    import builtins

    real_import = builtins.__import__

    def import_without_docling(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "docling.document_converter":
            raise ModuleNotFoundError("No module named 'docling'", name="docling")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", import_without_docling)

    with pytest.raises(RuntimeError, match="USE_DOCLING=true.*Docling"):
        _docling_extract(tmp_path / "document.pdf")


def test_docling_converter_is_reused_between_documents(monkeypatch, tmp_path):
    import builtins
    from types import SimpleNamespace

    import winkg.extractors as extractors

    converted = []
    constructions = []

    class FakeConverter:
        def __init__(self):
            constructions.append(1)

        def convert(self, path):
            converted.append(path)
            return SimpleNamespace(
                document=SimpleNamespace(export_to_markdown=lambda: path)
            )

    real_import = builtins.__import__

    def import_with_fake_docling(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "docling.document_converter":
            return SimpleNamespace(DocumentConverter=FakeConverter)
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", import_with_fake_docling)
    monkeypatch.setattr(extractors, "_docling_converter", None)

    first = str(tmp_path / "first.pdf")
    second = str(tmp_path / "second.pdf")
    assert _docling_extract(Path(first)) == first
    assert _docling_extract(Path(second)) == second
    assert len(constructions) == 1
    assert converted == [first, second]


def test_docling_initialization_failure_falls_back_to_standard_parser(monkeypatch, tmp_path):
    import builtins
    from dataclasses import replace
    from types import SimpleNamespace

    import winkg.extractors as extractors

    class BrokenConverter:
        def __init__(self):
            raise FileNotFoundError("latex2mathml/unimathsymbols.txt")

    real_import = builtins.__import__

    def import_with_broken_docling(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "docling.document_converter":
            return SimpleNamespace(DocumentConverter=BrokenConverter)
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", import_with_broken_docling)
    monkeypatch.setattr(extractors, "_docling_converter", None)
    monkeypatch.setattr(extractors, "_extract_xlsx", lambda raw: "Standard-Extraktion")

    settings = replace(get_settings(), use_docling=True)
    assert extractors.extract_text(tmp_path / "table.xlsx", b"workbook", settings) == "Standard-Extraktion"
