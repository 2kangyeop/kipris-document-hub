param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d+\.\d+\.\d+([.-][0-9A-Za-z.-]+)?$')]
    [string]$Version
)

$ErrorActionPreference = 'Stop'
$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$DistDir = Join-Path $AppDir 'dist'
$ReleaseDir = Join-Path $AppDir 'release'
$ExeName = "KIPRIS_Document_Hub_v$Version.exe"
$ReleaseExe = Join-Path $ReleaseDir $ExeName

if (Test-Path $DistDir) {
    Remove-Item -Recurse -Force $DistDir
}
if (Test-Path $ReleaseDir) {
    Remove-Item -Recurse -Force $ReleaseDir
}
New-Item -ItemType Directory -Path $ReleaseDir | Out-Null

python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name KIPRIS_Document_Hub `
    --add-data "$AppDir\assets;assets" `
    --add-data "$AppDir\web;web" `
    --collect-all playwright `
    --collect-all keyring `
    --collect-all pymupdf `
    --collect-all fastapi `
    --collect-all uvicorn `
    "$AppDir\launch_web.py"

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

$BuiltExe = Join-Path $DistDir 'KIPRIS_Document_Hub.exe'
if (-not (Test-Path $BuiltExe)) {
    throw "Expected executable was not created: $BuiltExe"
}

Copy-Item $BuiltExe $ReleaseExe

$Hash = (Get-FileHash -Algorithm SHA256 $ReleaseExe).Hash.ToLowerInvariant()
"$Hash  $ExeName" | Set-Content -Encoding ascii (Join-Path $ReleaseDir 'SHA256SUMS.txt')

$Manual = Join-Path $AppDir 'docs\KIPRIS_Document_Hub_Manual.pdf'
if (Test-Path $Manual) {
    Copy-Item $Manual (Join-Path $ReleaseDir "KIPRIS_Document_Hub_Manual_v$Version.pdf")
}

Write-Host "Release files created in $ReleaseDir"

