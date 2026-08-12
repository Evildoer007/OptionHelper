$ErrorActionPreference = "SilentlyContinue"

$candidatePaths = [System.Collections.Generic.List[string]]::new()

function Add-PythonCandidate([string] $Path) {
    if (-not [string]::IsNullOrWhiteSpace($Path)) {
        $candidatePaths.Add($Path)
    }
}

if ($env:CONDA_PREFIX) {
    Add-PythonCandidate (Join-Path $env:CONDA_PREFIX "python.exe")
}

Get-Command python3, python -CommandType Application -All | ForEach-Object {
    Add-PythonCandidate $_.Source
}

$condaRoots = @(
    (Join-Path $env:ProgramData "anaconda3"),
    (Join-Path $env:USERPROFILE "anaconda3"),
    (Join-Path $env:USERPROFILE "miniconda3"),
    (Join-Path $env:USERPROFILE "mambaforge")
)
foreach ($root in $condaRoots) {
    Add-PythonCandidate (Join-Path $root "python.exe")
    Get-ChildItem (Join-Path $root "envs") -Directory | ForEach-Object {
        Add-PythonCandidate (Join-Path $_.FullName "python.exe")
    }
}

$condaEnvironmentFile = Join-Path $env:USERPROFILE ".conda\environments.txt"
if (Test-Path -LiteralPath $condaEnvironmentFile -PathType Leaf) {
    Get-Content -LiteralPath $condaEnvironmentFile | ForEach-Object {
        Add-PythonCandidate (Join-Path $_ "python.exe")
    }
}

$seen = @{}
foreach ($candidate in $candidatePaths) {
    $absolutePath = [System.IO.Path]::GetFullPath($candidate)
    if ($seen.ContainsKey($absolutePath) -or -not (Test-Path -LiteralPath $absolutePath -PathType Leaf)) {
        continue
    }
    $seen[$absolutePath] = $true
    $environmentRoot = Split-Path $absolutePath -Parent
    $environmentName = "独立解释器"
    $pythonVersion = "选择后确认"
    $condaMetadata = Join-Path $environmentRoot "conda-meta"
    $venvMetadata = Join-Path $environmentRoot "pyvenv.cfg"
    if (Test-Path -LiteralPath $condaMetadata -PathType Container) {
        $environmentName = Split-Path $environmentRoot -Leaf
        if ((Split-Path $environmentRoot -Parent | Split-Path -Leaf) -ne "envs") {
            $environmentName = "base ($environmentName)"
        }
        $pythonPackage = Get-ChildItem -LiteralPath $condaMetadata -Filter "python-*.json" -File | Select-Object -First 1
        if ($pythonPackage) {
            $metadata = Get-Content -LiteralPath $pythonPackage.FullName -Raw | ConvertFrom-Json
            if ($metadata.version) {
                $pythonVersion = [string] $metadata.version
            }
        }
    } elseif (Test-Path -LiteralPath $venvMetadata -PathType Leaf) {
        $environmentName = Split-Path $environmentRoot -Leaf
        $versionLine = Get-Content -LiteralPath $venvMetadata | Where-Object { $_ -match '^\s*version\s*=' } | Select-Object -First 1
        if ($versionLine) {
            $pythonVersion = ($versionLine -split '=', 2)[1].Trim()
        }
    }
    Write-Output "  环境名称=$environmentName；Python版本=$pythonVersion；解释器绝对路径=$absolutePath"
}
