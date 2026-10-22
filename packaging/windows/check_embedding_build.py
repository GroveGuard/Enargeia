"""Build-time preflight for local embeddings and the Flet desktop client; no model download."""
from __future__ import annotations

import sys


def main() -> int:
    try:
        from importlib.metadata import version

        import flet as ft
        import flet_desktop
        import onnxruntime
        import fastembed
        from fastembed import TextEmbedding

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
        hub_version = version("huggingface-hub")
        if hub_version != "0.36.2":
            raise RuntimeError(
                "Expected huggingface-hub 0.36.2 for reproducible model-cache/download behavior; "
                f"found {hub_version}. Rebuild with the pinned dependency set."
            )
        print(f"Flet: {flet_version}; Image.fit uses BoxFit.CONTAIN")
        print(f"Flet desktop: {desktop_version} ({flet_desktop.__file__})")
        print(f"Hugging Face Hub: {hub_version} (FastEmbed-compatible cache layout)")
        print(f"FastEmbed: {fastembed_version} ({fastembed.__file__})")
        print(f"ONNX Runtime: {onnxruntime.__version__}")
        print(f"Execution providers: {', '.join(providers)}")
        return 0
    except Exception as exc:
        print(f"Build runtime dependency preflight failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
