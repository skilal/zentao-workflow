$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
& py -3 -X utf8 (Join-Path $PSScriptRoot 'app.py')
