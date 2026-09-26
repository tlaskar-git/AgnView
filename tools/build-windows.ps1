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

# The exe's file and product version resource, from agent_relay.__version__.
$VersionFile = Join-Path $Build "agnview-version.txt"
& $Python tools/make-version-file.py $VersionFile
if ($LASTEXITCODE -ne 0) { throw "Could not write the version resource." }

$Entry = Join-Path $Build "agnview_desktop.py"
Set-Content -Encoding utf8 $Entry "import sys`nfrom agent_relay.desktop.app import main`nsys.exit(main())"

& $Python -m PyInstaller --noconfirm --clean --windowed `
    --name AgnView `
    --icon $Icon `
    --version-file $VersionFile `
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

# The zip attached to a GitHub release. It holds the AgnView folder, because
# the exe needs the files beside it.
$Zip = Join-Path $Root "dist\AgnView-windows-x64.zip"
if (Test-Path $Zip) { Remove-Item -Force $Zip }
Compress-Archive -Path (Join-Path $Root "dist\AgnView") -DestinationPath $Zip
Write-Host "Packaged $Zip"

if ($Install) {
    $Target = Join-Path $env:LOCALAPPDATA "Programs\AgnView"
    $Source = Join-Path $Root "dist\AgnView"

    # Stop the running copy and wait until it has exited, so its files are
    # no longer locked.
    $Running = Get-Process AgnView -ErrorAction SilentlyContinue
    $Running | Stop-Process -Force
    $Running | ForEach-Object { $_.WaitForExit(15000) | Out-Null }

    # Delete the old install, retrying while Windows releases the files. A
    # partial delete used to leave the folder behind, and copying into it
    # then nested the new build one level down as AgnView\AgnView.
    for ($Attempt = 1; (Test-Path $Target) -and $Attempt -le 10; $Attempt++) {
        Remove-Item -Recurse -Force $Target -ErrorAction SilentlyContinue
        if (Test-Path $Target) { Start-Sleep -Milliseconds 500 }
    }
    if (Test-Path $Target) { throw "Could not remove $Target. Close AgnView and try again." }

    # Copy the folder's contents, never the folder itself, so the exe always
    # lands directly in the target.
    New-Item -ItemType Directory -Force $Target | Out-Null
    Copy-Item -Recurse -Force (Join-Path $Source "*") $Target
    if (-not (Test-Path (Join-Path $Target "AgnView.exe"))) {
        throw "AgnView.exe is missing from $Target after the copy."
    }

    $Shortcut = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\AgnView.lnk"
    $Shell = New-Object -ComObject WScript.Shell
    $Link = $Shell.CreateShortcut($Shortcut)
    $Link.TargetPath = Join-Path $Target "AgnView.exe"
    $Link.WorkingDirectory = $Target
    $Link.Save()

    Write-Host "Installed to $Target with a Start menu shortcut."
}
