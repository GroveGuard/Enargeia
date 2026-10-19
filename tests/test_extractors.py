from pathlib import Path

from winkg.config import get_settings
from winkg.extractors import extract_text


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
