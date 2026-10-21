$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
python make_icon.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
python -m PyInstaller --noconfirm --clean --onefile --windowed --exclude-module numpy --name DeviceBatteryTray --icon app.ico --distpath dist --workpath build --specpath build app.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
    Pop-Location
}
