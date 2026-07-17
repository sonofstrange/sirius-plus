$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$archive = Join-Path $env:TEMP 'sirius_plus.zip'

Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue
$tarArgs = @(
    '-a', '-c', '-f', $archive,
    '--exclude=.git',
    '--exclude=data',
    '--exclude=__pycache__',
    '--exclude=*/__pycache__',
    '--exclude=*.pyc',
    '--exclude=*.sqlite3',
    '--exclude=*.sqlite3-journal',
    '--exclude=*.sqlite3-*',
    '--exclude=./sirius_web.sqlite3*',
    '--exclude=.env',
    '--exclude=vapid_private_key.pem',
    '--exclude=android',
    '-C', $projectRoot, '.'
)
& tar.exe @tarArgs

if ($LASTEXITCODE -ne 0) {
    throw "tar.exe exited with code $LASTEXITCODE"
}

Write-Host "Created: $archive"
