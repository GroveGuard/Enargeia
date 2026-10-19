from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

from .chunking import chunk_text
from .config import get_settings
from .embeddings import EmbeddingClient
from .extractors import extract_text


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Misst Jina-Embeddings mit echten Chunks aus einer lokalen Datei; schreibt nichts nach Neo4j."
    )
    parser.add_argument("--file", required=True, help="Pfad zu einer unterstützten lokalen Quelldatei, z. B. XLSX/PDF/DOCX")
    parser.add_argument("--chunks", type=int, default=200, help="Maximale Anzahl echter Chunks (Standard: 200)")
    parser.add_argument("--batch-size", type=int, default=32, help="Chunks pro API-Anfrage (Standard: 32)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    path = Path(args.file).expanduser().resolve()
    if not path.is_file():
        parser.error(f"Datei nicht gefunden: {path}")
    if args.chunks < 1 or args.batch_size < 1:
        parser.error("--chunks und --batch-size müssen mindestens 1 sein.")

    settings = get_settings()
    if settings.embedding_provider != "openai":
        parser.error("Für diesen Test bitte in .env EMBEDDING_PROVIDER=openai setzen.")
    if not settings.embedding_api_key:
        parser.error("EMBEDDING_API_KEY fehlt in der Projekt-.env.")

    raw = path.read_bytes()
    text = extract_text(path, raw, settings)
    if not text.strip():
        parser.error("Aus der Datei konnte kein Text extrahiert werden.")
    chunks = chunk_text(text, size=settings.chunk_size, overlap=settings.chunk_overlap)
    selected = chunks[: args.chunks]
    if not selected:
        parser.error("Die Datei enthält keine Chunks.")

    print(f"Datei: {path.name}")
    print(f"Extrahierter Text: {len(text):,} Zeichen")
    print(f"Chunks insgesamt: {len(chunks):,}; getestet: {len(selected):,}")
    print(f"Chunk-Zeichen: Ø {sum(len(c.text) for c in selected) / len(selected):.0f}; max. {max(len(c.text) for c in selected):,}")
    print(f"API: {settings.embedding_base_url}; Modell: {settings.embedding_model}; Batchgröße: {args.batch_size}")
    print("Es werden nur Embeddings angefragt; Neo4j wird nicht verändert. Textinhalte werden an Jina übertragen.", flush=True)

    client = EmbeddingClient(settings)
    embedded = 0
    started = time.monotonic()
    for start in range(0, len(selected), args.batch_size):
        batch = selected[start : start + args.batch_size]
        batch_started = time.monotonic()
        vectors = client.embed_many([chunk.text for chunk in batch])
        seconds = time.monotonic() - batch_started
        embedded += len(vectors)
        print(
            f"Batch {start // args.batch_size + 1}: {len(vectors)} Chunks in {seconds:.2f}s "
            f"({len(vectors) / max(seconds, 0.001):.2f} Chunks/s); Vektordimension={len(vectors[0])}",
            flush=True,
        )
    elapsed = time.monotonic() - started
    print("\nERGEBNIS")
    print(f"Erfolgreich: {embedded}/{len(selected)} Chunks")
    print(f"Gesamtzeit: {elapsed:.2f}s")
    print(f"Durchsatz: {embedded / max(elapsed, 0.001):.2f} Chunks/s")
    print(f"Hochrechnung auf {len(selected):,} Chunks: {elapsed / max(embedded, 1) * len(selected) / 60:.1f} Minuten (ohne Limits/Netzwerkschwankungen)")
    print("Hinweis: Der Durchsatz dieses Tests ist eine Momentaufnahme; API-Ratenlimits können die Vollindexierung begrenzen.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
