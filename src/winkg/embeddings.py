from __future__ import annotations

import io
import logging
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

import requests

from .config import APP_DATA_DIR, Settings

log = logging.getLogger(__name__)
_FASTEMBED_SNAPSHOT_PATCH_LOCK = threading.Lock()

DEFAULT_FASTEMBED_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


def _prepare_huggingface_download_environment(cache_dir: Path) -> None:
    """Use a predictable per-user cache and avoid Xet-specific transfers on Windows.

    FastEmbed's model is public and around a few hundred MB. The regular Hub HTTP
    download path is more compatible with locked-down Windows networks and avoids
    silently staging data in the user's unrelated default Hugging Face cache.
    These variables must be set before importing FastEmbed/Hugging Face Hub.
    """
    models_root = APP_DATA_DIR / "models"
    hf_home = models_root / "huggingface"
    xet_cache = hf_home / "xet"
    hf_home.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)

    # This is an app-owned cache, so keep it deterministic instead of inheriting
    # a possible profile-level cache override from an unrelated Python install.
    os.environ["HF_HOME"] = str(hf_home)
    os.environ["HF_HUB_CACHE"] = str(cache_dir)
    os.environ["HF_XET_CACHE"] = str(xet_cache)
    os.environ["HF_HUB_DISABLE_XET"] = "1"
    # The packaged Windows GUI runs with sys.stdout/sys.stderr set to None.
    # Disable default terminal progress bars before importing the Hub. During a
    # model download we inject a custom tqdm class that captures byte progress for
    # Flet and never writes to stdout/stderr.
    os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
    os.environ["TQDM_DISABLE"] = "1"
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "60")
    os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "30")


def _make_huggingface_progress_class(
    callback: Callable[[dict[str, Any]], None] | None,
):
    """Build a tqdm-compatible bar that streams transfer bytes to the app UI.

    Hugging Face Hub accepts a ``tqdm_class`` for ``snapshot_download``. The
    default bar assumes a console in some releases, which does not exist for a
    PyInstaller ``--windowed`` application. This implementation captures only
    the aggregate transfer bar and never writes to any terminal stream.
    """
    from tqdm.auto import tqdm as BaseTqdm

    class FletDownloadProgress(BaseTqdm):
        def __init__(self, *args: Any, **kwargs: Any):
            self._winkg_callback = callback
            self._winkg_bar_name = str(kwargs.pop("name", ""))
            self._winkg_last_emit = 0.0
            self._winkg_last_signature: tuple[int, int] | None = None
            # Always use an in-memory stream and force updates even when Hub's
            # progress bars are globally disabled. display() below is a no-op.
            kwargs["file"] = io.StringIO()
            kwargs["disable"] = False
            kwargs["leave"] = False
            super().__init__(*args, **kwargs)
            self._winkg_emit(force=True)

        def display(self, msg: str | None = None, pos: int | None = None) -> None:
            # No terminal output: transfer updates are consumed by Flet instead.
            return None

        def refresh(self, *args: Any, **kwargs: Any) -> Any:
            result = super().refresh(*args, **kwargs)
            self._winkg_emit(force=True)
            return result

        def update(self, n: int | float = 1) -> Any:
            result = super().update(n)
            self._winkg_emit()
            return result

        def close(self) -> None:
            # Always flush the last byte count, even if a transfer finishes inside
            # the UI-throttling interval.
            self._winkg_emit(force=True)
            super().close()

        def _winkg_emit(self, *, force: bool = False) -> None:
            if not callable(self._winkg_callback):
                return
            description = str(getattr(self, "desc", "") or "")
            bar_name = self._winkg_bar_name.casefold()
            # snapshot_download owns two bars. Report the transfer bar (network
            # bytes), not the separate reconstruction bar, to avoid double-counting.
            if "transfer" not in bar_name and "downloading bytes" not in description.casefold():
                return
            current = max(0, int(getattr(self, "n", 0) or 0))
            total = max(0, int(getattr(self, "total", 0) or 0))
            signature = (current, total)
            now = time.monotonic()
            previous = self._winkg_last_signature
            if not force and previous is not None:
                if signature == previous:
                    return
                previous_ratio = previous[0] / previous[1] if previous[1] > 0 else 0.0
                current_ratio = current / total if total > 0 else 0.0
                if now - self._winkg_last_emit < 0.15 and total == previous[1] and abs(current_ratio - previous_ratio) < 0.01:
                    return
            self._winkg_last_emit = now
            self._winkg_last_signature = signature
            event = {
                "phase": "download",
                "current_bytes": current,
                "total_bytes": total,
                "description": "Modell-Dateien herunterladen",
            }
            try:
                self._winkg_callback(event)
            except Exception:
                pass

    return FletDownloadProgress


@contextmanager
def _fastembed_download_progress_hook(
    callback: Callable[[dict[str, Any]], None] | None,
) -> Iterator[None]:
    """Temporarily route FastEmbed's Hugging Face snapshot progress to Flet.

    FastEmbed imports ``snapshot_download`` into its model-management module.
    Patching that local reference for the duration of model construction makes it
    possible to supply Hugging Face's documented ``tqdm_class`` hook without
    forking FastEmbed. A lock ensures parallel model initializations cannot swap
    each other's function reference.
    """
    if callback is None:
        yield
        return
    with _FASTEMBED_SNAPSHOT_PATCH_LOCK:
        try:
            from fastembed.common import model_management
            original_snapshot_download = model_management.snapshot_download
            progress_class = _make_huggingface_progress_class(callback)
        except Exception:
            # Model loading remains available; only byte-level GUI progress is optional.
            yield
            return

        def snapshot_download_with_progress(*args: Any, **kwargs: Any) -> Any:
            kwargs["tqdm_class"] = progress_class
            return original_snapshot_download(*args, **kwargs)

        model_management.snapshot_download = snapshot_download_with_progress
        try:
            yield
        finally:
            model_management.snapshot_download = original_snapshot_download


class EmbeddingError(RuntimeError):
    pass


class EmbeddingClient:
    """Provider-neutral embedding interface with lazy local-model loading."""

    def __init__(
        self,
        settings: Settings,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.settings = settings
        self.session = requests.Session()
        self._local_model = None
        self.progress_callback = progress_callback

    def _report_progress(self, phase: str, **details: Any) -> None:
        if not callable(self.progress_callback):
            return
        event = {"phase": phase, **details}
        try:
            self.progress_callback(event)
        except Exception:
            pass

    @property
    def enabled(self) -> bool:
        return self.settings.embedding_provider != "none"

    @property
    def signature(self) -> str:
        return self.settings.embedding_signature

    def _get_local_model(self):
        if self._local_model is None:
            cache_dir = APP_DATA_DIR / "models" / "fastembed"
            _prepare_huggingface_download_environment(cache_dir)

            # Keep third-party download logs quiet; progress is shown in the GUI and
            # failures are logged by the application with their original traceback.
            for logger_name in (
                "fastembed", "huggingface_hub", "huggingface_hub.file_download",
                "httpx", "httpcore",
            ):
                logging.getLogger(logger_name).setLevel(logging.WARNING)

            try:
                # Import only after cache and transfer settings have been established.
                from fastembed import TextEmbedding
                try:
                    from huggingface_hub.utils import disable_progress_bars
                    disable_progress_bars()
                except Exception:
                    # Environment flags above are the primary safeguard; this
                    # best-effort call also covers Hub versions already imported.
                    pass
            except ModuleNotFoundError as exc:
                if exc.name == "fastembed":
                    raise EmbeddingError(
                        "FastEmbed fehlt in der installierten Anwendung. Bitte die Anwendung "
                        "mit einem Build installieren, der FastEmbed und ONNX Runtime enthält. "
                        "Der aktuelle Installationsfehler wird im GUI-Log protokolliert."
                    ) from exc
                log.exception("Python-Abhaengigkeit beim FastEmbed-Import fehlt: %s", exc.name)
                raise EmbeddingError(
                    "FastEmbed ist vorhanden, aber ein benötigtes Python-Modul fehlt "
                    f"({exc.name or 'unbekannt'}). Details: {exc}. "
                    "Bitte die Installation mit den lokalen Embedding-Abhängigkeiten reparieren."
                ) from exc
            except (ImportError, OSError) as exc:
                log.exception("FastEmbed/ONNX Runtime konnte nicht importiert werden")
                raise EmbeddingError(
                    "FastEmbed ist vorhanden, aber eine Abhängigkeit oder native DLL von "
                    "ONNX Runtime konnte nicht geladen werden. Details: "
                    f"{type(exc).__name__}: {exc}. Bei Windows können fehlende ONNX-Runtime- "
                    "Dateien oder die Microsoft Visual C++ Runtime die Ursache sein."
                ) from exc

            model_name = self.settings.embedding_model or DEFAULT_FASTEMBED_MODEL
            log.info("Initialisiere lokales FastEmbed-Modell %s", model_name)
            result: dict[str, object] = {}
            self._report_progress(
                "preparing", message="Pruefe Modellcache und bereite den Download vor.",
                current_bytes=0, total_bytes=0,
            )

            def construct_model() -> None:
                try:
                    with _fastembed_download_progress_hook(self.progress_callback):
                        try:
                            result["model"] = TextEmbedding(
                                model_name=model_name,
                                cache_dir=str(cache_dir),
                                threads=self.settings.embedding_threads or None,
                            )
                        except ValueError as exc:
                            # FastEmbed can finish materializing the Hugging Face
                            # snapshot and still report its generic "from any
                            # source" error on the first process. A second
                            # process then succeeds from the cache, which used to
                            # force users to restart the application. Retry once
                            # against the cache that the first attempt populated.
                            if "Could not load model" not in str(exc):
                                raise
                            log.warning(
                                "FastEmbed-Download meldete einen Fehler; versuche den lokalen Cache erneut (%s)",
                                model_name,
                            )
                            result["model"] = TextEmbedding(
                                model_name=model_name,
                                cache_dir=str(cache_dir),
                                threads=self.settings.embedding_threads or None,
                                local_files_only=True,
                            )
                except Exception as exc:  # transfer errors originate in the worker thread
                    result["error"] = exc

            # Load in a worker so transfer progress is independent of console streams.
            worker = threading.Thread(
                target=construct_model, name="fastembed-model-loader", daemon=True
            )
            worker.start()
            worker.join()
            if "error" in result:
                exc = result["error"]
                self._report_progress("error", message=f"Modell-Download fehlgeschlagen: {type(exc).__name__}: {exc}")
                log.error(
                    "FastEmbed-Modellinitialisierung fehlgeschlagen (%s; Cache: %s)",
                    model_name, cache_dir,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
                raise EmbeddingError(
                    f"Lokales Embedding-Modell konnte nicht geladen werden ({model_name}; Cache: {cache_dir}). "
                    f"Ursache: {type(exc).__name__}: {exc}"
                ) from exc

            model = result.get("model")
            if model is None:
                raise EmbeddingError(
                    f"FastEmbed hat kein Modell geliefert ({model_name}; Cache: {cache_dir})."
                )

            self._report_progress(
                "testing", message="Modell-Dateien vorhanden; initialisiere ONNX und teste ein Embedding.",
                current_bytes=0, total_bytes=0,
            )

            # Constructor success alone is insufficient: run one real inference so
            # incomplete weights/ONNX sessions are detected before indexing proceeds.
            try:
                warmup = list(model.embed(["Enargeia local embedding self-test"], batch_size=1))
                if len(warmup) != 1 or not list(warmup[0]):
                    raise RuntimeError("FastEmbed lieferte beim Modell-Selbsttest keinen gueltigen Vektor.")
            except Exception as exc:
                self._report_progress("error", message=f"Modell-Selbsttest fehlgeschlagen: {type(exc).__name__}: {exc}")
                log.error(
                    "FastEmbed-Modell-Selbsttest fehlgeschlagen (%s; Cache: %s)",
                    model_name, cache_dir,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
                raise EmbeddingError(
                    f"Das Embedding-Modell wurde gefunden, konnte aber keine Embeddings erzeugen "
                    f"({model_name}; Cache: {cache_dir}). Ursache: {type(exc).__name__}: {exc}"
                ) from exc

            self._local_model = model
            self._report_progress(
                "complete", message="Lokales Embedding-Modell geladen und erfolgreich getestet.",
                current_bytes=0, total_bytes=0, dimensions=len(warmup[0]),
            )
            log.info("FastEmbed-Modell erfolgreich geladen: %s; Dimensionen=%s", model_name, len(warmup[0]))
        return self._local_model

    def embed_many(self, texts: Sequence[str]) -> list[list[float]]:
        if not self.enabled or not texts:
            return []
        try:
            if self.settings.embedding_provider == "fastembed":
                model = self._get_local_model()
                vectors = [row.tolist() if hasattr(row, "tolist") else list(row)
                           for row in model.embed(list(texts), batch_size=self.settings.embedding_batch_size)]
            elif self.settings.embedding_provider == "ollama":
                response = self.session.post(
                    f"{self.settings.embedding_base_url}/api/embed",
                    json={"model": self.settings.embedding_model, "input": list(texts), "keep_alive": "30m"},
                    timeout=self.settings.embedding_timeout_seconds,
                )
                response.raise_for_status()
                vectors = response.json().get("embeddings")
            elif self.settings.embedding_provider == "openai":
                headers = {}
                if self.settings.embedding_api_key:
                    headers["Authorization"] = f"Bearer {self.settings.embedding_api_key}"
                response = self.session.post(
                    f"{self.settings.embedding_base_url}/embeddings",
                    json={"model": self.settings.embedding_model, "input": list(texts)},
                    headers=headers,
                    timeout=self.settings.embedding_timeout_seconds,
                )
                response.raise_for_status()
                payload = response.json()
                vectors = [item["embedding"] for item in sorted(payload.get("data", []), key=lambda row: row.get("index", 0))]
            else:
                raise EmbeddingError(f"Unbekannter Embedding-Provider: {self.settings.embedding_provider}")
        except EmbeddingError:
            raise
        except (requests.RequestException, ValueError, KeyError, TypeError, RuntimeError) as exc:
            raise EmbeddingError(
                f"Embedding-Aufruf fehlgeschlagen ({self.settings.embedding_provider}, "
                f"Modell {self.settings.embedding_model}): {exc}"
            ) from exc
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise EmbeddingError("Embedding-Provider lieferte eine unerwartete Anzahl an Vektoren.")
        normalized: list[list[float]] = []
        dimension: int | None = None
        for vector in vectors:
            if not isinstance(vector, (list, tuple)) or not vector:
                raise EmbeddingError("Embedding-Provider lieferte einen leeren oder ungültigen Vektor.")
            row = [float(value) for value in vector]
            if dimension is None:
                dimension = len(row)
            if len(row) != dimension:
                raise EmbeddingError("Embedding-Provider lieferte Vektoren mit verschiedenen Dimensionen.")
            normalized.append(row)
        return normalized

    def embed_one(self, text: str) -> list[float]:
        vectors = self.embed_many([text])
        if not vectors:
            raise EmbeddingError("Embeddings sind deaktiviert.")
        return vectors[0]
