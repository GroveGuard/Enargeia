param(
  [ValidateSet('setup', 'run', 'test', 'check')]
  [string]$Command = 'run',
  [Parameter(ValueFromRemainingArguments = $true)]
  [string[]]$Arguments = @()
)

$ErrorActionPreference = 'Stop'

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$DevVenv = Join-Path $ProjectRoot '.venv'
$Python = Join-Path $DevVenv 'Scripts\python.exe'

function Invoke-Checked {
  param(
    [string]$Executable,
    [string[]]$CommandArguments
  )
  & $Executable @CommandArguments
  if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
  }
}

function Install-DevelopmentEnvironment {
  if (-not (Test-Path -LiteralPath $Python)) {
    $PyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($PyLauncher) {
      Invoke-Checked $PyLauncher.Source @('-3', '-m', 'venv', $DevVenv)
    } else {
      $SystemPython = Get-Command python.exe -ErrorAction Stop
      Invoke-Checked $SystemPython.Source @('-m', 'venv', $DevVenv)
    }
  }

  if (-not (Test-Path -LiteralPath $Python)) {
    throw "Die Entwicklungsumgebung konnte nicht erstellt werden: $Python"
  }

  Write-Host 'Installiere/aktualisiere die editierbare Entwicklungsinstallation ...' -ForegroundColor Cyan
  Invoke-Checked $Python @('-m', 'pip', 'install', '--upgrade', 'pip')
  Invoke-Checked $Python @('-m', 'pip', 'install', '-e', '.[dev]')
}

function Assert-DevelopmentEnvironment {
  if (-not (Test-Path -LiteralPath $Python)) {
    throw "Die Entwicklungsumgebung fehlt. Einmal 'setup_windows.bat' oder 'scripts\dev.ps1 -Command setup' ausfuehren."
  }
}

Set-Location $ProjectRoot

switch ($Command) {
  'setup' {
    Install-DevelopmentEnvironment
    Write-Host 'Entwicklungsumgebung ist bereit.' -ForegroundColor Green
  }
  'run' {
    Assert-DevelopmentEnvironment
    $SourcePath = Join-Path $ProjectRoot 'src'
    if ($env:PYTHONPATH) {
      $env:PYTHONPATH = "$SourcePath;$env:PYTHONPATH"
    } else {
      $env:PYTHONPATH = $SourcePath
    }
    Write-Host 'Starte Enargeia direkt aus dem Quellcode. Aenderungen werden nach einem Neustart uebernommen.' -ForegroundColor Cyan
    Invoke-Checked $Python (@('-m', 'winkg.bootstrap') + $Arguments)
  }
  'test' {
    Assert-DevelopmentEnvironment
    Invoke-Checked $Python (@('-m', 'pytest') + $Arguments)
  }
  'check' {
    Assert-DevelopmentEnvironment
    Invoke-Checked $Python @('-m', 'compileall', '-q', 'src')
    Invoke-Checked $Python (@('-m', 'pytest') + $Arguments)
  }
}
