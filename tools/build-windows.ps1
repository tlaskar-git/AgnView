<#
Builds the AgnView desktop app for Windows on this machine, with no CI runner.

  .\tools\build-windows.ps1            builds dist\AgnView\AgnView.exe
  .\tools\build-windows.ps1 -Install   also installs it for the current user

The install copies the app to %LOCALAPPDATA%\Programs\AgnView and adds a
Start menu shortcut. Start with Windows is a toggle in the tray menu.
#>
param(
    [switch]$Install
)

# Native tools write progress to stderr, which Windows PowerShell treats as
# an error under "Stop". Exit codes decide failure instead.
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
$Venv = Join-Path $Root ".venv-desktop"
$Python = Join-Path $Venv "Scripts\python.exe"
$Build = Join-Path $Root "build\desktop"

Set-Location $Root

if (-not (Test-Path $Python)) {
    python -m venv $Venv
    if ($LASTEXITCODE -ne 0) { throw "Could not create the build virtual environment." }
}
& $Python -m pip install --quiet --upgrade pip
& $Python -m pip install --quiet -e ".[desktop]" "pyinstaller==6.22.3"
if ($LASTEXITCODE -ne 0) { throw "Dependency install failed." }

New-Item -ItemType Directory -Force $Build | Out-Null
$Icon = Join-Path $Build "agnview.ico"
& $Python -c "from PIL import Image; Image.open(r'agent_relay\web\static\agnview-app-icon-dark.png').save(r'$Icon', sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])"

$Entry = Join-Path $Build "agnview_desktop.py"
Set-Content -Encoding utf8 $Entry "import sys`nfrom agent_relay.desktop.app import main`nsys.exit(main())"

& $Python -m PyInstaller --noconfirm --clean --windowed `
    --name AgnView `
    --icon $Icon `
    --distpath (Join-Path $Root "dist") `
    --workpath (Join-Path $Build "work") `
    --specpath $Build `
    --collect-data agent_relay `
    --collect-submodules agent_relay `
    --collect-submodules uvicorn `
    --collect-all iroh `
    --collect-all sse_starlette `
    --hidden-import pystray._win32 `
    $Entry
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }

$Exe = Join-Path $Root "dist\AgnView\AgnView.exe"
Write-Host "Built $Exe"

if ($Install) {
    $Target = Join-Path $env:LOCALAPPDATA "Programs\AgnView"
    Get-Process AgnView -ErrorAction SilentlyContinue | Stop-Process -Force
    if (Test-Path $Target) { Remove-Item -Recurse -Force $Target }
    Copy-Item -Recurse (Join-Path $Root "dist\AgnView") $Target

    $Shortcut = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\AgnView.lnk"
    $Shell = New-Object -ComObject WScript.Shell
    $Link = $Shell.CreateShortcut($Shortcut)
    $Link.TargetPath = Join-Path $Target "AgnView.exe"
    $Link.WorkingDirectory = $Target
    $Link.Save()

    Write-Host "Installed to $Target with a Start menu shortcut."
}
