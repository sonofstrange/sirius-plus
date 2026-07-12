$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$archive = Join-Path $env:TEMP 'sirius_plus.zip'

Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue
& tar.exe -a -c -f $archive `
    --exclude=.git `
    --exclude=data `
    --exclude=__pycache__ `
    --exclude='*/__pycache__' `
    --exclude='*.pyc' `
    --exclude='*.sqlite3' `
    --exclude='*.sqlite3-journal' `
    --exclude=.env `
    -C $projectRoot .

if ($LASTEXITCODE -ne 0) {
    throw "tar.exe завершился с кодом $LASTEXITCODE"
}

Write-Host "Готово: $archive"
