[CmdletBinding()]
param(
    [string]$PythonExe = "python",
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ToolRoot = $PSScriptRoot
$ProjectRoot = (Resolve-Path (Join-Path $ToolRoot "..\..")).Path
$SourceRoot = Join-Path $ProjectRoot "src"
$OutputsRoot = Join-Path $ProjectRoot "outputs"
$ArtifactName = "DSS Mesh Doctor-v1.0.0 (windows x64)"
$PortableDirectory = Join-Path $OutputsRoot $ArtifactName
$WorkRoot = Join-Path ([System.IO.Path]::GetTempPath()) "DSSMeshDoctor-1.0.0-pyinstaller"
$BuildRoot = Join-Path $WorkRoot "build"
$DistRoot = Join-Path $WorkRoot "dist"
$Executable = Join-Path $DistRoot "DSSMeshDoctor.exe"

function Invoke-PythonChecked {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    & $PythonExe @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Python command failed with exit code $LASTEXITCODE."
    }
}

function Remove-OutputPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    $resolvedOutputs = [System.IO.Path]::GetFullPath($OutputsRoot).TrimEnd("\")
    $resolvedTarget = [System.IO.Path]::GetFullPath($Path)
    if (-not $resolvedTarget.StartsWith(
        $resolvedOutputs + "\",
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Refusing to remove a path outside outputs: $resolvedTarget"
    }
    Remove-Item -LiteralPath $resolvedTarget -Recurse -Force
}

if ($env:OS -ne "Windows_NT") {
    throw "The portable DLC build requires Windows."
}
$RuntimeIdentity = & $PythonExe -c "import struct,sys;print(f'{sys.version_info.major}.{sys.version_info.minor}|{struct.calcsize(chr(80))*8}')"
if ($LASTEXITCODE -ne 0 -or $RuntimeIdentity.Trim() -ne "3.13|64") {
    throw "Python 3.13 x64 is required; detected $RuntimeIdentity"
}
$PythonBase = (& $PythonExe -c "import sys;print(sys.base_prefix)").Trim()
if ($LASTEXITCODE -ne 0 -or -not $PythonBase) {
    throw "Unable to locate the Python base runtime."
}
$PythonLicense = Join-Path $PythonBase "LICENSE.txt"
if (-not (Test-Path -LiteralPath $PythonLicense -PathType Leaf)) {
    throw "Python runtime license and acknowledgements are missing: $PythonLicense"
}
if (-not $SkipInstall) {
    Invoke-PythonChecked -m pip install --disable-pip-version-check "pyinstaller==6.21.0"
}
Invoke-PythonChecked -c "import PyInstaller, tkinter"

New-Item -ItemType Directory -Path $OutputsRoot, $WorkRoot -Force | Out-Null
if (Test-Path -LiteralPath $BuildRoot) {
    Remove-Item -LiteralPath $BuildRoot -Recurse -Force
}
if (Test-Path -LiteralPath $DistRoot) {
    Remove-Item -LiteralPath $DistRoot -Recurse -Force
}

Invoke-PythonChecked -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name DSSMeshDoctor `
    --paths $ToolRoot `
    --paths $SourceRoot `
    --specpath $WorkRoot `
    --workpath $BuildRoot `
    --distpath $DistRoot `
    (Join-Path $ToolRoot "app.py")

if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
    throw "PyInstaller completed without producing DSSMeshDoctor.exe."
}
Remove-OutputPath -Path $PortableDirectory
New-Item -ItemType Directory -Path $PortableDirectory | Out-Null
Copy-Item -LiteralPath $Executable -Destination $PortableDirectory
Copy-Item -LiteralPath (Join-Path $ToolRoot "README.md") -Destination $PortableDirectory
Copy-Item -LiteralPath (Join-Path $ToolRoot "LICENSE") -Destination $PortableDirectory
Copy-Item -LiteralPath (Join-Path $ToolRoot "dss-extension.json") -Destination $PortableDirectory
Copy-Item -LiteralPath $PythonLicense -Destination (
    Join-Path $PortableDirectory "THIRD_PARTY_LICENSES.txt"
)

$ThirdPartyLicenses = Join-Path $PortableDirectory "THIRD_PARTY_LICENSES.txt"
foreach ($Marker in @(
    "PYTHON SOFTWARE FOUNDATION LICENSE VERSION 2",
    "bzip2/libbzip2 version 1.0.8",
    "libffi - Copyright",
    "copyrighted by the Regents"
)) {
    if (-not (Select-String -LiteralPath $ThirdPartyLicenses -SimpleMatch $Marker -Quiet)) {
        throw "Third-party license file is missing required marker: $Marker"
    }
}

& (Join-Path $PortableDirectory "DSSMeshDoctor.exe") --smoke-test
if ($LASTEXITCODE -ne 0) {
    throw "Frozen DLC smoke test failed with exit code $LASTEXITCODE."
}
Write-Host "Portable DLC folder: $PortableDirectory"
