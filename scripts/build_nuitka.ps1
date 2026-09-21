param(
    [ValidateSet('all', 'gui', 'cli')][string]$Frontend = 'all',
    [switch]$WithoutOcr,
    [ValidateSet('auto', 'msvc', 'mingw64')][string]$Compiler = 'auto',
    [int]$Jobs = 4,
    [string]$Python = 'python',
    [string]$OutputRoot,
    [switch]$Plan,
    [switch]$Analyze
)
$ErrorActionPreference = 'Stop'
$buildScript = Join-Path $PSScriptRoot 'build_nuitka.py'
$buildArguments = @($buildScript, '--frontend', $Frontend, '--compiler', $Compiler, '--jobs', $Jobs)
if ($WithoutOcr) { $buildArguments += '--without-ocr' }
if ($OutputRoot) { $buildArguments += @('--output-root', $OutputRoot) }
if ($Plan) { $buildArguments += '--plan' }
if ($Analyze) { $buildArguments += '--analyze' }
& $Python @buildArguments
exit $LASTEXITCODE
