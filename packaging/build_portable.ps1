[CmdletBinding()]
param(
    [string]$PythonExe = "python",
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$SpecPath = Join-Path $PSScriptRoot "DualSPHysicsStudio.spec"
$LicenseCollector = Join-Path $PSScriptRoot "collect_licenses.py"
$RequirementsPath = Join-Path $ProjectRoot "requirements-lock.txt"
$DlcSource = Join-Path $ProjectRoot "DLC"
$GuideSource = Join-Path $ProjectRoot ([string]::Concat([char]0x5FEB, [char]0x901F, [char]0x8BF4, [char]0x660E, ".md"))
$ProjectLicense = Join-Path $ProjectRoot "LICENSE"
$LicenseSource = Join-Path $ProjectRoot "licenses"
$QtLicense = Join-Path $LicenseSource "LGPL-3.0-only.txt"
$QtGplLicense = Join-Path $LicenseSource "GPL-3.0-only.txt"
$QtThirdPartyNotices = Join-Path $LicenseSource "Qt-6.11.1-THIRD-PARTY-NOTICES.txt"
$VtkThirdPartyNotices = Join-Path $LicenseSource "VTK-9.6.2-THIRD-PARTY-NOTICES.txt"
$RustThirdPartyNotices = Join-Path $LicenseSource "Rust-Native-THIRD-PARTY-NOTICES.txt"
$NoticesSource = Join-Path $ProjectRoot "THIRD_PARTY_NOTICES.txt"
$WorkRoot = Join-Path ([System.IO.Path]::GetTempPath()) "DualSPHysicsStudio-1.0.1-pyinstaller"
$BuildRoot = Join-Path $WorkRoot "build"
$DistRoot = Join-Path $WorkRoot "dist"
$OutputsRoot = Join-Path $ProjectRoot "outputs"
$ArtifactName = "DualSPHysics Studio-v1.0.1 (windows x64)"
$PyInstallerDirectoryName = "DualSPHysicsStudio"
$BuiltDirectory = Join-Path $DistRoot $PyInstallerDirectoryName
$PortableDirectory = Join-Path $OutputsRoot $ArtifactName

function Invoke-PythonChecked {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    & $PythonExe @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Python command failed with exit code $LASTEXITCODE."
    }
}

function Remove-ArtifactPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    $resolvedOutputs = [System.IO.Path]::GetFullPath($OutputsRoot).TrimEnd('\')
    $resolvedTarget = [System.IO.Path]::GetFullPath($Path)
    $expectedPrefix = $resolvedOutputs + '\'
    if (-not $resolvedTarget.StartsWith($expectedPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove a path outside outputs: $resolvedTarget"
    }
    Remove-Item -LiteralPath $resolvedTarget -Recurse -Force
}

if ($env:OS -ne "Windows_NT") {
    throw "DualSPHysics Studio portable builds must be produced on Windows."
}
if (-not (Test-Path -LiteralPath $SpecPath -PathType Leaf)) {
    throw "PyInstaller specification not found: $SpecPath"
}
if (-not (Test-Path -LiteralPath $LicenseCollector -PathType Leaf)) {
    throw "Third-party license collector not found: $LicenseCollector"
}
if (-not (Test-Path -LiteralPath $ProjectLicense -PathType Leaf)) {
    throw "Project license not found: $ProjectLicense"
}
if (-not (Test-Path -LiteralPath $QtLicense -PathType Leaf)) {
    throw "Qt LGPL license text not found: $QtLicense"
}
if (-not (Test-Path -LiteralPath $QtGplLicense -PathType Leaf)) {
    throw "GNU GPL license text required by LGPLv3 was not found: $QtGplLicense"
}
if (-not (Test-Path -LiteralPath $QtThirdPartyNotices -PathType Leaf)) {
    throw "Qt third-party notices were not found: $QtThirdPartyNotices"
}
if (-not (Test-Path -LiteralPath $VtkThirdPartyNotices -PathType Leaf)) {
    throw "VTK third-party notices were not found: $VtkThirdPartyNotices"
}
if (-not (Test-Path -LiteralPath $RustThirdPartyNotices -PathType Leaf)) {
    throw "Rust crate notices were not found: $RustThirdPartyNotices"
}
if (-not (Test-Path -LiteralPath $NoticesSource -PathType Leaf)) {
    throw "Third-party notices not found: $NoticesSource"
}
if (-not (Test-Path -LiteralPath $GuideSource -PathType Leaf)) {
    throw "Chinese quick-start guide was not found: $GuideSource"
}

$RuntimeIdentity = & $PythonExe -c "import platform,struct,sys;print(str(sys.version_info.major)+chr(46)+str(sys.version_info.minor)+chr(124)+str(struct.calcsize(chr(80))*8)+chr(124)+platform.machine())"
if ($LASTEXITCODE -ne 0) {
    throw "Unable to execute Python: $PythonExe"
}
$RuntimeParts = $RuntimeIdentity.Trim().Split('|')
if ($RuntimeParts.Length -ne 3 -or $RuntimeParts[0] -ne "3.13") {
    throw "Python 3.13 is required; detected $RuntimeIdentity"
}
if ($RuntimeParts[1] -ne "64") {
    throw "A 64-bit Python runtime is required; detected $RuntimeIdentity"
}
$PythonBase = (& $PythonExe -c "import sys;print(sys.base_prefix)").Trim()
if ($LASTEXITCODE -ne 0 -or -not $PythonBase) {
    throw "Unable to locate the Python base runtime."
}
$PythonLicense = Join-Path $PythonBase "LICENSE.txt"
if (-not (Test-Path -LiteralPath $PythonLicense -PathType Leaf)) {
    throw "The Python runtime license/acknowledgements file is missing: $PythonLicense"
}

if (-not $SkipInstall) {
    Invoke-PythonChecked -m pip install --disable-pip-version-check -r $RequirementsPath
}

Invoke-PythonChecked -c "import PySide6, vtk, openai, keyring, pydantic, PyInstaller"

New-Item -ItemType Directory -Path $WorkRoot, $OutputsRoot -Force | Out-Null
if (Test-Path -LiteralPath $BuildRoot) {
    Remove-Item -LiteralPath $BuildRoot -Recurse -Force
}
if (Test-Path -LiteralPath $DistRoot) {
    Remove-Item -LiteralPath $DistRoot -Recurse -Force
}

Invoke-PythonChecked -m PyInstaller `
    --noconfirm `
    --clean `
    --log-level WARN `
    --workpath $BuildRoot `
    --distpath $DistRoot `
    $SpecPath

if (-not (Test-Path -LiteralPath (Join-Path $BuiltDirectory "DualSPHysicsStudio.exe") -PathType Leaf)) {
    throw "PyInstaller completed without producing DualSPHysicsStudio.exe."
}

Remove-ArtifactPath -Path $PortableDirectory
Copy-Item -LiteralPath $BuiltDirectory -Destination $PortableDirectory -Recurse
if (-not (Test-Path -LiteralPath (Join-Path $DlcSource "README.txt") -PathType Leaf)) {
    throw "DLC/README.txt is required so the portable archive retains the extension directory."
}
Copy-Item -LiteralPath $DlcSource -Destination (Join-Path $PortableDirectory "DLC") -Recurse
Copy-Item -LiteralPath $GuideSource -Destination $PortableDirectory
Copy-Item -LiteralPath $ProjectLicense -Destination $PortableDirectory
Invoke-PythonChecked $LicenseCollector `
    (Join-Path $PortableDirectory "THIRD_PARTY_LICENSES.txt") `
    --summary $NoticesSource `
    --python-license $PythonLicense `
    --qt-lgpl $QtLicense `
    --qt-gpl $QtGplLicense `
    --qt-third-party $QtThirdPartyNotices `
    --vtk-third-party $VtkThirdPartyNotices `
    --rust-third-party $RustThirdPartyNotices

$LicenseAppendix = Join-Path $PortableDirectory "THIRD_PARTY_LICENSES.txt"
$RequiredLicenseMarkers = @(
    "DE-AC04-94AL85000",
    "NVIDIA ownership",
    "Independent JPEG Group",
    "FreeType Project",
    "Audited registry components: 124",
    "Unique notice bodies after exact-byte deduplication: 60"
)
foreach ($Marker in $RequiredLicenseMarkers) {
    if (-not (Select-String -LiteralPath $LicenseAppendix -SimpleMatch $Marker -Quiet)) {
        throw "Third-party license appendix is missing required marker: $Marker"
    }
}

$MetadataDirectories = @(
    Get-ChildItem -LiteralPath $PortableDirectory -Recurse -Directory -Filter "*.dist-info"
)
if ($MetadataDirectories.Count -ne 0) {
    $UnexpectedMetadata = $MetadataDirectories.FullName -join "; "
    throw "Portable package contains unexpected distribution metadata: $UnexpectedMetadata"
}

$UnexpectedRuntimeFiles = @(
    Get-ChildItem -LiteralPath (Join-Path $PortableDirectory "_internal") -Recurse -File |
        Where-Object {
            $_.Name -like "api-ms-win-*.dll" -or
            $_.Name -eq "ucrtbase.dll" -or
            $_.Name -in @(
                "Qt6Network.dll",
                "QtNetwork.pyd",
                "Qt6OpenGL.dll",
                "Qt6OpenGLWidgets.dll",
                "QtOpenGL.pyd",
                "QtOpenGLWidgets.pyd",
                "Qt6Pdf.dll",
                "Qt6Qml.dll",
                "Qt6QmlMeta.dll",
                "Qt6QmlModels.dll",
                "Qt6QmlWorkerScript.dll",
                "Qt6Quick.dll",
                "Qt6VirtualKeyboard.dll",
                "opengl32sw.dll",
                "vtkParallelCore-9.6.2.dll"
            ) -or
            $_.FullName -match "[\\/]numpy(?:\.libs)?[\\/]" -or
            $_.Name -like "vtkParallelCore*.pyd" -or
            $_.FullName.EndsWith("\certifi\py.typed", [System.StringComparison]::OrdinalIgnoreCase) -or
            $_.FullName.EndsWith("\dualsphysics_studio\resources\app_icon.ico", [System.StringComparison]::OrdinalIgnoreCase) -or
            $_.Extension -eq ".qm"
        }
)
if ($UnexpectedRuntimeFiles.Count -ne 0) {
    $UnexpectedNames = $UnexpectedRuntimeFiles.FullName -join "; "
    throw "Portable package contains excluded runtime files: $UnexpectedNames"
}

& (Join-Path $PortableDirectory "DualSPHysicsStudio.exe") --smoke-test
if ($LASTEXITCODE -ne 0) {
    throw "Frozen smoke test failed with exit code $LASTEXITCODE."
}

Write-Host "Portable folder: $PortableDirectory"
