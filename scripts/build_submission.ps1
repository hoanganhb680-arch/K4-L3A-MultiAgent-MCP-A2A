$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $repoRoot

$python = Join-Path $repoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Repository .venv is missing. Create it before building.'
}

function Invoke-Checked {
    param([string]$Label, [string]$Executable, [string[]]$Arguments)
    Write-Host "==> $Label"
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Label failed with exit code $LASTEXITCODE" }
}

Invoke-Checked 'Install repository source' $python @('-m', 'pip', 'install', '-e', '.[dev]')
Invoke-Checked 'Ruff' $python @('-m', 'ruff', 'check', '.')
Invoke-Checked 'Pytest' $python @('-m', 'pytest', '-q')

$day09 = Join-Path $repoRoot '.venv\Scripts\day09.exe'
if (-not (Test-Path -LiteralPath $day09)) { throw 'day09.exe is missing from repository .venv' }
Invoke-Checked 'Validate inputs' $day09 @('validate-inputs')
Invoke-Checked 'Discover MCP tools' $day09 @('mcp-tools')
Invoke-Checked 'Fresh 100-case run' $day09 @('run')
Invoke-Checked 'Validate outputs and provenance' $day09 @('validate')
Invoke-Checked 'Package fresh submission' $day09 @('package', '--output', 'dist/submission.zip')
Invoke-Checked 'Audit submission' $python @('scripts/audit_run.py')
