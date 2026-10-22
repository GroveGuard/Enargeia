from __future__ import annotations

import logging
import sys
import types

import pytest

from winkg.config import Settings
from winkg.embeddings import EmbeddingClient, EmbeddingError


def test_fastembed_provider_loads_lazily_and_returns_vectors(monkeypatch, tmp_path):
    calls = {"init": 0, "embed": 0}

    class FakeEmbedding:
        def __init__(self, model_name, cache_dir, threads=None):
            calls["init"] += 1
            assert "paraphrase-multilingual" in model_name
            assert cache_dir.endswith("models/fastembed") or cache_dir.endswith("models\\fastembed")

        def embed(self, texts, batch_size=256):
            calls["embed"] += 1
            return iter([[0.1, 0.2], [0.3, 0.4]][:len(texts)])

    monkeypatch.setattr("winkg.embeddings.APP_DATA_DIR", tmp_path)
    monkeypatch.setitem(sys.modules, "fastembed", types.SimpleNamespace(TextEmbedding=FakeEmbedding))
    settings = Settings(embedding_provider="fastembed", embedding_model="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
    client = EmbeddingClient(settings)

    assert calls["init"] == 0
    assert client.embed_many(["eins", "zwei"]) == [[0.1, 0.2], [0.3, 0.4]]
    assert calls["init"] == 1
    assert calls["embed"] == 2  # Warm-up plus requested embeddings.
    assert client.embed_many(["noch eins"]) == [[0.1, 0.2]]
    assert calls["init"] == 1
    assert calls["embed"] == 3


def test_missing_fastembed_reports_actionable_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "fastembed", None)
    client = EmbeddingClient(Settings(embedding_provider="fastembed"))

    with pytest.raises(EmbeddingError, match="FastEmbed"):
        client.embed_one("test")


def test_fastembed_dependency_import_error_includes_actual_details(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def broken_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "fastembed":
            raise ImportError("DLL load failed while importing onnxruntime_pybind11_state")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", broken_import)
    client = EmbeddingClient(Settings(embedding_provider="fastembed"))

    with pytest.raises(EmbeddingError, match="onnxruntime_pybind11_state"):
        client.embed_one("test")


def test_fastembed_download_uses_managed_cache_and_disables_xet(monkeypatch, tmp_path):
    import os
    import winkg.embeddings as embedding_module

    monkeypatch.setattr(embedding_module, "APP_DATA_DIR", tmp_path)
    for name in (
        "HF_HOME", "HF_HUB_CACHE", "HF_XET_CACHE", "HF_HUB_DISABLE_XET",
        "HF_HUB_DISABLE_PROGRESS_BARS", "TQDM_DISABLE",
    ):
        monkeypatch.delenv(name, raising=False)

    cache_dir = tmp_path / "models" / "fastembed"
    embedding_module._prepare_huggingface_download_environment(cache_dir)

    assert os.environ["HF_HOME"] == str(tmp_path / "models" / "huggingface")
    assert os.environ["HF_HUB_CACHE"] == str(cache_dir)
    assert os.environ["HF_XET_CACHE"] == str(tmp_path / "models" / "huggingface" / "xet")
    assert os.environ["HF_HUB_DISABLE_XET"] == "1"
    assert os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] == "1"
    assert os.environ["TQDM_DISABLE"] == "1"


def test_fastembed_download_failure_includes_cache_and_root_cause(monkeypatch, tmp_path):
    import winkg.embeddings as embedding_module

    class BrokenEmbedding:
        def __init__(self, model_name, cache_dir, threads=None):
            raise RuntimeError("simulated download failed")

    monkeypatch.setattr(embedding_module, "APP_DATA_DIR", tmp_path)
    monkeypatch.setitem(sys.modules, "fastembed", types.SimpleNamespace(TextEmbedding=BrokenEmbedding))
    client = EmbeddingClient(Settings(embedding_provider="fastembed"))

    with pytest.raises(EmbeddingError, match="simulated download failed") as caught:
        client.embed_one("test")
    assert str(tmp_path / "models" / "fastembed") in str(caught.value)


def test_fastembed_download_failure_is_reported_without_retry(monkeypatch, tmp_path):
    calls = []

    class DownloadFails:
        def __init__(self, model_name, cache_dir, threads=None, local_files_only=False):
            calls.append(local_files_only)
            raise ValueError(f"Could not load model {model_name} from any source.")

    monkeypatch.setattr("winkg.embeddings.APP_DATA_DIR", tmp_path)
    monkeypatch.setitem(sys.modules, "fastembed", types.SimpleNamespace(TextEmbedding=DownloadFails))
    client = EmbeddingClient(Settings(embedding_provider="fastembed"))

    with pytest.raises(EmbeddingError, match="Could not load model"):
        client.embed_one("test")
    assert calls == [False]


def test_windows_fastembed_cache_symlinks_are_materialized(monkeypatch, tmp_path):
    import os
    import winkg.embeddings as embedding_module

    cache_dir = tmp_path / "models" / "fastembed"
    repo_dir = cache_dir / "models--Qdrant--example"
    blob = repo_dir / "blobs" / "model-hash"
    snapshot_file = repo_dir / "snapshots" / "revision" / "model.onnx"
    blob.parent.mkdir(parents=True)
    snapshot_file.parent.mkdir(parents=True)
    blob.write_bytes(b"model data")
    try:
        os.symlink(os.path.relpath(blob, snapshot_file.parent), snapshot_file)
    except OSError as exc:
        pytest.skip(f"Symbolic links are unavailable: {exc}")

    assert embedding_module._materialize_huggingface_cache_symlinks(cache_dir) == 1
    assert not snapshot_file.is_symlink()
    assert snapshot_file.read_bytes() == b"model data"


def test_huggingface_hub_wont_create_new_cache_symlinks(monkeypatch, tmp_path):
    import os
    import winkg.embeddings as embedding_module
    from huggingface_hub import file_download

    if os.name != "nt":
        pytest.skip("Windows cache compatibility only")
    cache_root = tmp_path.resolve()
    monkeypatch.setitem(file_download._are_symlinks_supported_in_dir, str(cache_root), True)

    embedding_module._disable_huggingface_cache_symlinks(cache_root)

    assert file_download._are_symlinks_supported_in_dir[str(cache_root)] is False


def test_fastembed_loguru_diagnostics_reach_app_log_without_url_query_secrets(caplog):
    from loguru import logger as loguru_logger
    from winkg.embeddings import _fastembed_log_hook

    diagnostic_logger = loguru_logger.patch(
        lambda record: record.update(name="fastembed.common.model_management")
    )
    with caplog.at_level(logging.WARNING):
        with _fastembed_log_hook():
            diagnostic_logger.error(
                "Could not download model: https://huggingface.co/model?token=secret"
            )

    assert "Could not download model" in caplog.text
    assert "https://huggingface.co/model?<query redacted>" in caplog.text
    assert "secret" not in caplog.text


def test_huggingface_transfer_progress_is_reported_without_a_console():
    from winkg.embeddings import _make_huggingface_progress_class

    events = []
    progress_type = _make_huggingface_progress_class(events.append)
    bar = progress_type(
        total=100,
        desc="Downloading bytes",
        name="huggingface_hub.snapshot_download.transfer",
    )
    bar.update(25)
    bar.update(25)
    bar.close()

    transfer_events = [event for event in events if event["phase"] == "download"]
    assert transfer_events
    assert any(event["current_bytes"] == 50 and event["total_bytes"] == 100 for event in transfer_events)
    assert all(event["description"] == "Modell-Dateien herunterladen" for event in transfer_events)


def test_huggingface_reconstruction_bar_is_not_double_counted():
    from winkg.embeddings import _make_huggingface_progress_class

    events = []
    progress_type = _make_huggingface_progress_class(events.append)
    bar = progress_type(
        total=100,
        desc="Reconstructing",
        name="huggingface_hub.snapshot_download",
    )
    bar.update(50)
    bar.close()

    assert events == []


def test_fastembed_download_hook_supplies_tqdm_class_and_restores_original(monkeypatch):
    import types
    import winkg.embeddings as embedding_module

    calls = {}

    def snapshot_download(*args, **kwargs):
        calls["kwargs"] = kwargs
        return "snapshot"

    management = types.ModuleType("fastembed.common.model_management")
    management.snapshot_download = snapshot_download
    common = types.ModuleType("fastembed.common")
    common.model_management = management
    fastembed = types.ModuleType("fastembed")
    fastembed.common = common
    monkeypatch.setitem(sys.modules, "fastembed", fastembed)
    monkeypatch.setitem(sys.modules, "fastembed.common", common)
    monkeypatch.setitem(sys.modules, "fastembed.common.model_management", management)

    events = []
    with embedding_module._fastembed_download_progress_hook(events.append):
        assert management.snapshot_download is not snapshot_download
        assert management.snapshot_download("model", allow_patterns=["*.onnx"]) == "snapshot"
        assert calls["kwargs"]["tqdm_class"] is not None
    assert management.snapshot_download is snapshot_download
