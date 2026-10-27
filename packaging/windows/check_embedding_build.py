"""Build-time preflight for local embeddings, Docling and the Flet desktop client."""
from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    try:
        from importlib.metadata import version

        import flet as ft
        import flet_desktop
        import onnxruntime
        import fastembed
        import pymupdf
        import rapidocr
        import latex2mathml
        import docling
        from fastembed import TextEmbedding
        from docling.document_converter import DocumentConverter

        expected = "1.23.0"
        fastembed_version = version("fastembed")
        if fastembed_version != "0.9.0":
            raise RuntimeError(
                f"Expected FastEmbed 0.9.0, found {fastembed_version}"
            )
        if onnxruntime.__version__ != expected:
            raise RuntimeError(
                f"Expected ONNX Runtime {expected}, found {onnxruntime.__version__}"
            )
        providers = onnxruntime.get_available_providers()
        if "CPUExecutionProvider" not in providers:
            raise RuntimeError(f"ONNX Runtime CPU provider is unavailable: {providers}")
        _ = TextEmbedding
        _ = DocumentConverter
        layout_data = (
            Path(pymupdf.__file__).resolve().parent
            / "layout" / "resources" / "onnx" / "layout_rf2.4.1+imf1.yaml"
        )
        if not layout_data.is_file():
            raise RuntimeError(f"PyMuPDF layout data is missing: {layout_data}")
        latex2mathml_data = (
            Path(latex2mathml.__file__).resolve().parent / "unimathsymbols.txt"
        )
        if not latex2mathml_data.is_file():
            raise RuntimeError(f"latex2mathml data is missing: {latex2mathml_data}")
        rapidocr_models = (
            Path(rapidocr.__file__).resolve().parent / "default_models.yaml"
        )
        if not rapidocr_models.is_file():
            raise RuntimeError(f"RapidOCR model configuration is missing: {rapidocr_models}")
        flet_version = version("flet")
        if flet_version != "1.0.5":
            raise RuntimeError(
                f"Expected Flet 1.0.5 for the validated UI API, found {flet_version}"
            )
        if not hasattr(ft, "BoxFit") or not hasattr(ft.BoxFit, "CONTAIN"):
            raise RuntimeError("Flet Image.fit API is incompatible: ft.BoxFit.CONTAIN is unavailable")
        # Construct an Image so the build preflight catches API mismatches before PyInstaller.
        ft.Image(src="", width=1, height=1, fit=ft.BoxFit.CONTAIN)
        desktop_version = version("flet-desktop")
        docling_version = version("docling")
        if tuple(int(part) for part in docling_version.split(".")[:2]) < (2, 0):
            raise RuntimeError(f"Expected Docling >=2.0, found {docling_version}")
        hub_version = version("huggingface-hub")
        if hub_version != "0.36.2":
            raise RuntimeError(
                "Expected huggingface-hub 0.36.2 for reproducible model-cache/download behavior; "
                f"found {hub_version}. Rebuild with the pinned dependency set."
            )
        print(f"Flet: {flet_version}; Image.fit uses BoxFit.CONTAIN")
        print(f"Flet desktop: {desktop_version} ({flet_desktop.__file__})")
        print(f"Docling: {docling_version} ({docling.__file__})")
        print(f"Hugging Face Hub: {hub_version} (FastEmbed-compatible cache layout)")
        print(f"FastEmbed: {fastembed_version} ({fastembed.__file__})")
        print(f"ONNX Runtime: {onnxruntime.__version__}")
        print(f"Execution providers: {', '.join(providers)}")
        print(f"PyMuPDF layout data: {layout_data}")
        print(f"latex2mathml data: {latex2mathml_data}")
        print(f"RapidOCR model configuration: {rapidocr_models}")
        return 0
    except Exception as exc:
        print(f"Build runtime dependency preflight failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
