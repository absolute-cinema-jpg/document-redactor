# Builds "Sanitization Bureau" for Windows -- a standalone app with Python and
# every library bundled inside -- then self-tests it and zips it for sharing.
#
# Needs Python 3.10-3.13 from python.org. In PowerShell, from the project folder:
#
#   powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
#
# Must be run on Windows: PyInstaller cannot build a Windows app from a Mac.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$App  = "Sanitization Bureau"
$Venv = ".venv-win"
$Py   = Join-Path $Venv "Scripts\python.exe"

function Check($what) { if ($LASTEXITCODE -ne 0) { throw "$what failed (exit $LASTEXITCODE)" } }

if (-not (Test-Path $Py)) {
    Write-Host "Creating $Venv..."
    if ($env:PYTHON) { & $env:PYTHON -m venv $Venv }
    elseif (Get-Command py -ErrorAction SilentlyContinue) { & py -3 -m venv $Venv }
    else { & python -m venv $Venv }
    Check "creating the Python environment"
    & $Py -m pip install -q --upgrade pip; Check "pip upgrade"
}
& $Py -m pip install -q -r requirements.txt -r packaging\requirements-build.txt
Check "installing dependencies"

$Dist = "dist\windows"
$Work = "build\windows"
Write-Host "Building $App for Windows..."
if (Test-Path $Work) { Remove-Item -Recurse -Force $Work }
if (Test-Path "$Dist\$App") { Remove-Item -Recurse -Force "$Dist\$App" }
& $Py -m PyInstaller --noconfirm --clean --log-level WARN `
    --windowed --name $App `
    --icon "$PWD\packaging\icon.ico" `
    --paths "$PWD" `
    --add-data "$PWD\redactor\static;redactor\static" `
    --hidden-import fontTools.subset --hidden-import fontTools.ttLib `
    --distpath $Dist --workpath $Work --specpath $Work `
    packaging\bureau_app.py
Check "PyInstaller"

$Exe = Join-Path $Dist "$App\$App.exe"
if ($env:BUREAU_SKIP_SELFTEST -ne "1") {
    Write-Host "Self-testing the built app..."
    $Tmp = Join-Path $env:TEMP ("bureau-" + [guid]::NewGuid())
    New-Item -ItemType Directory -Path $Tmp | Out-Null
    & $Py tests\make_sample.py "$Tmp\sample.pdf"; Check "making the sample PDF"
    $proc = Start-Process -FilePath $Exe -PassThru `
        -ArgumentList @("--selftest", "`"$Tmp\sample.pdf`"", "`"$Tmp\out.pdf`"")
    if (-not $proc.WaitForExit(180000)) { $proc.Kill(); Write-Host "  (timed out)" }
    & $Py packaging\check_selftest.py "$Tmp\out.pdf.json"
    if ($LASTEXITCODE -ne 0) { throw "SELF-TEST FAILED -- not packaging" }
    Remove-Item -Recurse -Force $Tmp
}

# The zip unpacks to a folder holding the app plus plain-language instructions.
$Stage = Join-Path $Work "stage\$App"
New-Item -ItemType Directory -Path $Stage -Force | Out-Null
Copy-Item -Recurse "$Dist\$App\*" $Stage
Copy-Item packaging\READ_ME_FIRST_windows.txt (Join-Path $Stage "READ ME FIRST.txt")
$Zip = "dist\Sanitization-Bureau-windows-x64.zip"
if (Test-Path $Zip) { Remove-Item -Force $Zip }
Compress-Archive -Path (Join-Path $Work "stage\$App") -DestinationPath $Zip
Remove-Item -Recurse -Force (Join-Path $Work "stage")

Write-Host ""
Write-Host "Done (Windows):"
Write-Host "  $Dist\$App\$App.exe"
Write-Host "  $Zip   <- send this to people on Windows"
