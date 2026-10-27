$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Set-Location $ProjectRoot

Write-Host 'Building Enargeia for Windows x64' -ForegroundColor Cyan
Write-Host 'This build includes Docling; embedding/Docling model weights, Neo4j, Java and GDS are provisioned only when needed.'
Write-Host 'Using a dedicated project-local build environment to prevent unrelated packages/DLLs from leaking into the bundle.' -ForegroundColor Cyan

$BuildVenv = Join-Path $ProjectRoot '.venv-packaging'
$Python = Join-Path $BuildVenv 'Scripts\python.exe'
if (-not (Test-Path $Python)) {
  $BootstrapPython = (Get-Command python -ErrorAction Stop).Source
  & $BootstrapPython -m venv $BuildVenv
  if ($LASTEXITCODE -ne 0) { throw 'Could not create the isolated packaging virtual environment.' }
}
if (-not (Test-Path $Python)) { throw "Packaging Python was not created: $Python" }
$PythonVersion = & $Python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($LASTEXITCODE -ne 0) { throw 'Could not determine packaging Python version.' }
if ($PythonVersion -notin @('3.10','3.11','3.12','3.13')) {
  throw "Unsupported packaging Python $PythonVersion. Use Python 3.10-3.13 x64."
}

& $Python -m pip install --upgrade pip
& $Python -m pip install -e '.[gui,local-embeddings,package,docling,mcp]'
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }

Write-Host 'Checking local embedding, Docling and desktop dependencies (without downloading model weights)...' -ForegroundColor Cyan
$EmbeddingPreflight = Join-Path $PSScriptRoot 'check_embedding_build.py'
& $Python $EmbeddingPreflight
if ($LASTEXITCODE -ne 0) { throw 'FastEmbed/ONNX Runtime/Docling dependency preflight failed; see the error above.' }

$Dist = Join-Path $ProjectRoot 'dist'
$Build = Join-Path $ProjectRoot 'build\pyinstaller'
Remove-Item -Recurse -Force $Dist -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force $Build -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $Dist, $Build | Out-Null

$Common = @('--noconfirm','--clean','--onedir','--paths','src')
$GuiArgs = $Common + @(
  '--windowed','--name','Enargeia',
  '--icon','assets/branding/Enargeia_Corporate_Design/icons/Enargeia.ico',
  '--add-data','src/winkg/ui/assets;winkg/ui/assets',
  '--add-data','src/winkg/ui/graph3d.html;winkg/ui',
  '--collect-all','flet','--collect-all','flet_desktop',
  '--hidden-import','flet_desktop','--copy-metadata','flet','--copy-metadata','flet-desktop',
  '--collect-all','fastembed','--collect-binaries','onnxruntime','--collect-data','onnxruntime',
  '--collect-all','docling','--collect-all','docling_core','--collect-all','docling_parse','--collect-all','docling_ibm_models',
  '--hidden-import','transformers.models.auto.image_processing_auto',
  '--hidden-import','transformers.models.rt_detr.image_processing_rt_detr',
  '--hidden-import','transformers.models.auto.configuration_auto',
  '--hidden-import','transformers.models.rt_detr_v2.configuration_rt_detr_v2',
  '--hidden-import','transformers.models.rt_detr_v2.modeling_rt_detr_v2',
  '--collect-all','rapidocr','--copy-metadata','rapidocr',
  '--collect-all','latex2mathml',
  '--collect-all','huggingface_hub','--collect-all','tokenizers','--collect-all','tqdm','--collect-all','pymupdf',
  '--hidden-import','fastembed','--hidden-import','fastembed.text.text_embedding',
  '--hidden-import','onnxruntime','--hidden-import','onnxruntime.capi',
  '--hidden-import','onnxruntime.capi.onnxruntime_pybind11_state',
  '--hidden-import','onnxruntime.capi._pybind_state',
  '--copy-metadata','fastembed','--copy-metadata','onnxruntime',
  '--copy-metadata','docling','--copy-metadata','docling-slim','--copy-metadata','docling-core',
  '--copy-metadata','docling-parse','--copy-metadata','docling-ibm-models',
  '--hidden-import','winkg.graph3d_window','--hidden-import','keyring.backends.Windows',
  '--distpath',$Dist,'--workpath',(Join-Path $Build 'gui'),
  'packaging/windows/entry_gui.py'
)
& $Python -m PyInstaller @GuiArgs
if ($LASTEXITCODE -ne 0) { throw 'GUI build failed.' }

$AppBundle = Join-Path $Dist 'Enargeia'
$RequiredOnnxFiles = @('onnxruntime_pybind11_state.pyd','onnxruntime.dll','onnxruntime_providers_shared.dll')

# PyInstaller 6 places collected runtime files in the _internal directory by
# default. Older/custom builds may put them beside the executable instead.
# Discover the ONNX Runtime capi directory rather than assuming one layout.
$OnnxCapiCandidates = @(
  Get-ChildItem -Path $AppBundle -Directory -Filter 'capi' -Recurse -ErrorAction SilentlyContinue |
    Where-Object { $_.Parent.Name -eq 'onnxruntime' } |
    Select-Object -ExpandProperty FullName
)
$OnnxCapi = $null
$MissingOnnxFiles = $RequiredOnnxFiles
foreach ($Candidate in $OnnxCapiCandidates) {
  $CandidateMissing = @($RequiredOnnxFiles | Where-Object { -not (Test-Path (Join-Path $Candidate $_)) })
  if ($CandidateMissing.Count -eq 0) {
    $OnnxCapi = $Candidate
    $MissingOnnxFiles = @()
    break
  }
  $MissingOnnxFiles = $CandidateMissing
}
if (-not $OnnxCapi) {
  $FoundNativeFiles = @(
    Get-ChildItem -Path $AppBundle -File -Recurse -ErrorAction SilentlyContinue |
      Where-Object { $_.Name -in $RequiredOnnxFiles } |
      Select-Object -ExpandProperty FullName
  )
  $CandidateSummary = if ($OnnxCapiCandidates.Count -gt 0) { $OnnxCapiCandidates -join '; ' } else { 'no onnxruntime\capi directory found' }
  $FoundSummary = if ($FoundNativeFiles.Count -gt 0) { $FoundNativeFiles -join '; ' } else { 'none' }
  throw "Generated GUI bundle is missing required ONNX native files. Checked $CandidateSummary. Missing in a single capi directory: $($MissingOnnxFiles -join ', '). Files found anywhere in bundle: $FoundSummary. Installer will not be created."
}
Write-Host "ONNX native files verified in: $OnnxCapi" -ForegroundColor Green

# PyMuPDF's layout-aware PDF extraction reads versioned ONNX model YAML files
# at runtime. PyInstaller's import analysis alone does not collect this data.
$PyMuPDFLayoutData = @(
  Get-ChildItem -Path $AppBundle -File -Filter 'layout_rf2.4.1+imf1.yaml' -Recurse -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty FullName
)
if ($PyMuPDFLayoutData.Count -eq 0) {
  throw 'Generated GUI bundle is missing PyMuPDF layout data (layout_rf2.4.1+imf1.yaml). Installer will not be created.'
}
Write-Host "PyMuPDF layout data verified in: $($PyMuPDFLayoutData[0])" -ForegroundColor Green

$Latex2MathMLData = @(
  Get-ChildItem -Path $AppBundle -File -Filter 'unimathsymbols.txt' -Recurse -ErrorAction SilentlyContinue |
    Where-Object { $_.Directory.Name -eq 'latex2mathml' } |
    Select-Object -ExpandProperty FullName
)
if ($Latex2MathMLData.Count -eq 0) {
  throw 'Generated GUI bundle is missing latex2mathml data (unimathsymbols.txt). Installer will not be created.'
}
Write-Host "latex2mathml data verified in: $($Latex2MathMLData[0])" -ForegroundColor Green

$RapidOcrModels = @(
  Get-ChildItem -Path $AppBundle -File -Filter 'default_models.yaml' -Recurse -ErrorAction SilentlyContinue |
    Where-Object { $_.Directory.Name -eq 'rapidocr' } |
    Select-Object -ExpandProperty FullName
)
if ($RapidOcrModels.Count -eq 0) {
  throw 'Generated GUI bundle is missing RapidOCR model configuration (rapidocr/default_models.yaml). Installer will not be created.'
}
Write-Host "RapidOCR model configuration verified in: $($RapidOcrModels[0])" -ForegroundColor Green

$GraphArgs = $Common + @(
  '--windowed','--name','Enargeia3D',
  '--icon','assets/branding/Enargeia_Corporate_Design/icons/Enargeia.ico',
  '--add-data','src/winkg/ui/assets;winkg/ui/assets',
  '--add-data','src/winkg/ui/graph3d.html;winkg/ui',
  '--collect-all','flet','--collect-all','flet_desktop',
  '--hidden-import','flet_desktop','--copy-metadata','flet','--copy-metadata','flet-desktop',
  '--collect-all','webview','--collect-all','clr_loader',
  '--hidden-import','webview.platforms.edgechromium',
  '--hidden-import','keyring','--hidden-import','keyring.backends.Windows',
  # The viewer uses Neo4j records/dicts, not Result.to_df(); this avoids
  # PyInstaller following Neo4j's type-only pandas import and its ML stack.
  '--exclude-module','pandas',
  '--distpath',$Dist,'--workpath',(Join-Path $Build 'graph3d'),
  'packaging/windows/entry_3d.py'
)
& $Python -m PyInstaller @GraphArgs
if ($LASTEXITCODE -ne 0) { throw '3D companion build failed.' }

$McpBuild = Join-Path $Build 'mcp'
New-Item -ItemType Directory -Force $McpBuild | Out-Null
# The MCP server searches the already-indexed graph; document conversion happens
# in the GUI/indexing app. FastEmbed uses ONNX Runtime, not the PyTorch stack.
$McpArgs = @(
  '--noconfirm','--clean','--onefile','--console','--name','EnargeiaMCP','--paths','src',
  '--specpath',$McpBuild,
  '--collect-all','mcp','--collect-all','anyio',
  '--exclude-module','torch','--exclude-module','torchvision','--exclude-module','torchaudio',
  '--exclude-module','transformers','--exclude-module','scipy','--exclude-module','pandas',
  '--exclude-module','sklearn','--exclude-module','docling','--exclude-module','docling_core',
  '--exclude-module','docling_parse','--exclude-module','docling_ibm_models',
  '--copy-metadata','mcp',
  '--distpath',$Dist,'--workpath',$McpBuild,
  'packaging/windows/entry_mcp.py'
)
& $Python -m PyInstaller @McpArgs
if ($LASTEXITCODE -ne 0) { throw 'MCP server build failed.' }

$McpBundle = Join-Path $Dist 'Enargeia-MCP.mcpb'
& $Python (Join-Path $PSScriptRoot 'build_mcpb.py') `
  --server (Join-Path $Dist 'EnargeiaMCP.exe') --output $McpBundle
if ($LASTEXITCODE -ne 0) { throw 'Claude Desktop MCPB bundle creation failed.' }
Write-Host "Claude Desktop MCP bundle created: $McpBundle" -ForegroundColor Green

$ISCC = Get-Command ISCC.exe -ErrorAction SilentlyContinue
if ($ISCC) {
  # Embed Microsoft's signed VC++ runtime redistributable in the installer;
  # the setup installs it only when an adequate x64 runtime is not present.
  $RedistDir = Join-Path $PSScriptRoot 'redist'
  $RedistExe = Join-Path $RedistDir 'vc_redist.x64.exe'
  New-Item -ItemType Directory -Force $RedistDir | Out-Null
  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
  Invoke-WebRequest -Uri 'https://aka.ms/vc14/vc_redist.x64.exe' -OutFile $RedistExe
  $Signature = Get-AuthenticodeSignature -FilePath $RedistExe
  if ($Signature.Status -ne 'Valid' -or $Signature.SignerCertificate.Subject -notmatch 'Microsoft Corporation') {
    Remove-Item -Force $RedistExe -ErrorAction SilentlyContinue
    throw 'Microsoft VC++ Redistributable download failed Authenticode signature validation.'
  }
  & $ISCC.Source (Join-Path $PSScriptRoot 'Enargeia.iss')
  if ($LASTEXITCODE -ne 0) { throw 'Inno Setup packaging failed.' }
} else {
  Write-Warning 'PyInstaller binaries were built, but ISCC.exe was not found. Install Inno Setup and rerun to create the single Setup.exe.'
}
Write-Host "Enargeia build artifacts: $Dist" -ForegroundColor Green
