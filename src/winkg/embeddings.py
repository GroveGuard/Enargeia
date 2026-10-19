from __future__ import annotations

import logging
from typing import Sequence

import requests

from .config import Settings

log = logging.getLogger(__name__)


class EmbeddingError(RuntimeError):
    pass


class EmbeddingClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.session = requests.Session()

    @property
    def enabled(self) -> bool:
        return self.settings.embedding_provider != "none"

    @property
    def signature(self) -> str:
        return self.settings.embedding_signature

    def embed_many(self, texts: Sequence[str]) -> list[list[float]]:
        if not self.enabled:
            return []
        if not texts:
            return []
        try:
            if self.settings.embedding_provider == "ollama":
                response = self.session.post(
                    f"{self.settings.embedding_base_url}/api/embed",
                    json={"model": self.settings.embedding_model, "input": list(texts), "keep_alive": "30m"},
                    timeout=self.settings.embedding_timeout_seconds,
                )
                response.raise_for_status()
                payload = response.json()
                vectors = payload.get("embeddings")
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
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            raise EmbeddingError(
                f"Embedding-Aufruf fehlgeschlagen ({self.settings.embedding_provider}, "
                f"Modell {self.settings.embedding_model}): {exc}"
            ) from exc
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise EmbeddingError("Embedding-API lieferte eine unerwartete Anzahl an Vektoren.")
        normalized: list[list[float]] = []
        dimension: int | None = None
        for vector in vectors:
            if not isinstance(vector, list) or not vector:
                raise EmbeddingError("Embedding-API lieferte einen leeren oder ungültigen Vektor.")
            row = [float(value) for value in vector]
            if dimension is None:
                dimension = len(row)
            if len(row) != dimension:
                raise EmbeddingError("Embedding-API lieferte Vektoren mit verschiedenen Dimensionen.")
            normalized.append(row)
        return normalized

    def embed_one(self, text: str) -> list[float]:
        vectors = self.embed_many([text])
        if not vectors:
            raise EmbeddingError("Embeddings sind deaktiviert.")
        return vectors[0]
