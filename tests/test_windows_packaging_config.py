from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_local_embedding_runtime_is_pinned_as_a_compatible_pair():
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'version = "1.2.2"' in project
    assert '"fastembed==0.9.0"' in project
    assert '"onnxruntime==1.23.0"' in project
    assert '"huggingface-hub==0.36.2"' in project


def test_windows_build_uses_isolated_environment_and_checks_native_onnx_files():
    script = (ROOT / "packaging/windows/build_windows.ps1").read_text(encoding="utf-8")
    assert ".venv-packaging" in script
    assert "onnxruntime_pybind11_state.pyd" in script
    assert "onnxruntime_providers_shared.dll" in script
    assert "Installer will not be created" in script
    assert "Get-ChildItem -Path $AppBundle -Directory -Filter 'capi' -Recurse" in script
    assert "PyInstaller 6 places collected runtime files in the _internal directory" in script
    assert "https://aka.ms/vc14/vc_redist.x64.exe" in script
    assert "Get-AuthenticodeSignature" in script
    assert "check_embedding_build.py" in script
    assert "--check-embedding-runtime" not in script
    assert "--check-desktop-runtime" not in script


def test_windows_build_installs_and_bundles_docling():
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    script = (ROOT / "packaging/windows/build_windows.ps1").read_text(encoding="utf-8")
    helper = (ROOT / "packaging/windows/check_embedding_build.py").read_text(encoding="utf-8")
    assert '"docling>=2.0"' in project
    assert ".[gui,local-embeddings,package,docling,mcp]" in script
    for package in ("docling", "docling_core", "docling_parse", "docling_ibm_models"):
        assert f"'--collect-all','{package}'" in script
    assert "'--copy-metadata','docling-slim'" in script
    assert "from docling.document_converter import DocumentConverter" in helper
    assert 'version("docling")' in helper


def test_inno_setup_uses_enargeia_identity_and_integrated_brand_assets():
    installer = (ROOT / "packaging/windows/Enargeia.iss").read_text(encoding="utf-8")
    assert '#define MyAppName "Enargeia"' in installer
    assert '#define MyAppVersion "1.2.2"' in installer
    assert "DefaultDirName={localappdata}\\Programs\\Enargeia" in installer
    assert "OutputBaseFilename=Enargeia-Setup-{#MyAppVersion}-x64" in installer
    assert "SetupIconFile={#BrandRoot}\\icons\\Enargeia.ico" in installer
    assert "WizardImageFile={#BrandRoot}\\installer\\Enargeia_Installer_Sidebar.bmp" in installer
    assert "WizardSmallImageFile={#BrandRoot}\\installer\\Enargeia_Installer_Small_Icon.bmp" in installer
    assert "Enargeia_Splash_Screen.bmp" in installer
    assert "Enargeia_Setup_Complete.bmp" in installer
    assert "[InstallDelete]" in installer
    assert 'Name: "{app}\\*"' in installer
    assert "%LOCALAPPDATA%\\Enargeia" in installer
    assert 'Source: "redist\\vc_redist.x64.exe"; Flags: dontcopy' in installer
    assert "HasSuitableVCRuntime" in installer
    assert "ExtractTemporaryFile('vc_redist.x64.exe')" in installer


def test_enargeia_runtime_assets_are_declared_and_bundled():
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    script = (ROOT / "packaging/windows/build_windows.ps1").read_text(encoding="utf-8")
    assert 'name = "enargeia"' in project
    assert 'enargeia = "winkg.bootstrap:main"' in project
    assert 'enargeia-cli = "winkg.cli:main"' in project
    assert "--icon','assets/branding/Enargeia_Corporate_Design/icons/Enargeia.ico" in script
    assert "src/winkg/ui/assets;winkg/ui/assets" in script
    for filename in ("Enargeia.ico", "Enargeia_Logo_Dark_Background.png", "Enargeia_Loading_Indicator.png", "Enargeia_Welcome_Background.png", "Enargeia_Installer_Header.png", "Enargeia_Symbol_512.png"):
        assert (ROOT / "src/winkg/ui/assets" / filename).is_file(), filename
    assert (ROOT / "assets/branding/Enargeia_Corporate_Design/installer/Enargeia_Splash_Screen.bmp").is_file()
    assert (ROOT / "assets/branding/Enargeia_Corporate_Design/installer/Enargeia_Setup_Complete.bmp").is_file()


def test_windows_build_preflight_checks_pinned_runtime_dependencies():
    script_path = ROOT / "packaging/windows/build_windows.ps1"
    script_bytes = script_path.read_bytes()
    script = script_bytes.decode("ascii")
    assert all(byte < 128 for byte in script_bytes)
    assert "check_embedding_build.py" in script
    assert "-c \"import fastembed" not in script
    helper = (ROOT / "packaging/windows/check_embedding_build.py").read_text(encoding="utf-8")
    assert "onnxruntime.get_available_providers()" in helper
    assert 'version("huggingface-hub")' in helper
    assert 'version("fastembed")' in helper
    assert 'fastembed_version != "0.9.0"' in helper
    assert 'expected = "1.23.0"' in helper
    assert 'hub_version != "0.36.2"' in helper


def test_windows_build_preflight_is_valid_python():
    helper = (ROOT / "packaging/windows/check_embedding_build.py").read_text(encoding="utf-8")
    ast.parse(helper)


def test_desktop_runtime_is_declared_and_packaged():
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    script = (ROOT / "packaging/windows/build_windows.ps1").read_text(encoding="utf-8")
    assert 'flet[desktop]==1.0.5' in project
    assert script.count("--collect-all','flet_desktop") >= 2
    assert script.count("--hidden-import','flet_desktop") >= 2
    assert script.count("--copy-metadata','flet-desktop") >= 2
    assert "--check-desktop-runtime" not in script
    assert "--check-desktop-runtime" not in (ROOT / "src/winkg/bootstrap.py").read_text(encoding="utf-8")
    assert "--check-desktop-runtime" not in (ROOT / "packaging/windows/entry_3d.py").read_text(encoding="utf-8")


def test_3d_build_excludes_only_neo4j_dataframe_dependency():
    script = (ROOT / "packaging/windows/build_windows.ps1").read_text(encoding="utf-8")
    gui_args = script.split("$GuiArgs", 1)[1].split("$GraphArgs", 1)[0]
    graph_args = script.split("$GraphArgs", 1)[1].split("& $Python -m PyInstaller @GraphArgs", 1)[0]
    spec = (ROOT / "Enargeia3D.spec").read_text(encoding="utf-8")

    assert "'--exclude-module','pandas'" not in gui_args
    assert "'--exclude-module','pandas'" in graph_args
    assert "excludes=['pandas']" in spec


def test_mcp_server_and_claude_bundle_are_in_windows_distribution():
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    script = (ROOT / "packaging/windows/build_windows.ps1").read_text(encoding="utf-8")
    installer = (ROOT / "packaging/windows/Enargeia.iss").read_text(encoding="utf-8")
    assert '"mcp>=1.9,<2"' in project
    assert 'enargeia-mcp = "winkg.mcp_server:main"' in project
    assert "packaging/windows/entry_mcp.py" in script
    assert "'--onefile','--console','--name','EnargeiaMCP'" in script
    assert "'--collect-all','mcp'" in script
    for excluded_module in ("torch", "torchvision", "transformers", "scipy", "pandas", "docling"):
        assert f"'--exclude-module','{excluded_module}'" in script
    assert "build_mcpb.py" in script
    assert "Enargeia-MCP.mcpb" in installer


def test_fastembed_download_uses_managed_cache_and_gui_progress():
    source = (ROOT / "src/winkg/embeddings.py").read_text(encoding="utf-8")
    assert 'os.environ["HF_HUB_DISABLE_XET"] = "1"' in source
    assert 'os.environ["HF_HUB_CACHE"] = str(cache_dir)' in source
    assert 'model.embed(["Enargeia local embedding self-test"]' in source
    assert '"HF_XET_CACHE"' in source
    assert "_fastembed_download_progress_hook" in source
    assert "tqdm_class" in source
    assert "current_bytes" in source and "total_bytes" in source
    assert 'os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"' in source
    assert "_describe_cache" not in source
    assert "Embedding-Laufzeitversionen" not in source
    assert "setLevel(logging.INFO)" not in source


def test_user_facing_product_identity_is_consistently_enargeia():
    paths = [
        ROOT / "pyproject.toml",
        ROOT / "packaging/windows/build_windows.ps1",
        ROOT / "packaging/windows/Enargeia.iss",
        ROOT / "src/winkg/gui.py",
        ROOT / "src/winkg/bootstrap.py",
        ROOT / "src/winkg/setup_wizard.py",
        ROOT / "src/winkg/graph3d_window.py",
        ROOT / "src/winkg/ui/graph3d.html",
        ROOT / "src/winkg/ui/theme.py",
        ROOT / "run_gui_hidden.pyw",
        ROOT / "run_gui.vbs",
        ROOT / "setup_windows.bat",
        ROOT / "run_scan.bat",
    ]
    old_brand_tokens = (
        " ".join(("Company", "Knowledge", "Graph")),
        "Company" + "KnowledgeGraph",
        "Company" + "KnowledgeGraph3D",
        "win" + "-kg",
    )
    for path in paths:
        source = path.read_text(encoding="utf-8")
        for old in old_brand_tokens:
            assert old.casefold() not in source.casefold(), f"{old} remains in {path.relative_to(ROOT)}"
    assert '<div class="brandmark"><img src="assets/Enargeia_Symbol_512.png" alt="Enargeia"></div>' in (ROOT / "src/winkg/ui/graph3d.html").read_text(encoding="utf-8")


def test_flet_image_fit_uses_the_pinned_box_fit_api():
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    gui = (ROOT / "src/winkg/gui.py").read_text(encoding="utf-8")
    preflight = (ROOT / "packaging/windows/check_embedding_build.py").read_text(encoding="utf-8")
    assert 'flet[desktop]==1.0.5' in project
    assert "ft.ImageFit" not in gui
    assert gui.count("ft.BoxFit.CONTAIN") == 3
    assert "src_base64=" not in gui
    assert "src_base64=" not in preflight
    assert 'hasattr(ft.BoxFit, "CONTAIN")' in preflight
    assert 'ft.Image(src="", width=1, height=1, fit=ft.BoxFit.CONTAIN)' in preflight
