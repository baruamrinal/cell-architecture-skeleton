<#
.SYNOPSIS
    Local development tasks:  .\make up | deploy-mgr | deploy-web-ui | down | clean | status | build | test | test-fast | setup | help

.DESCRIPTION
    Thin wrapper around scripts/dev.py (the same targets as the Makefile on macOS/Linux).
    Start with a dot to stay in the activated virtual environment afterwards:

        . .\make up
#>
param(
    [Parameter(Position = 0)] [string] $Target = "help",
    [Parameter(ValueFromRemainingArguments = $true)] [string[]] $Rest = @()
)

$root = $PSScriptRoot
$venvPython = Join-Path $root ".venv\Scripts\python.exe"
$activate = Join-Path $root ".venv\Scripts\Activate.ps1"
$dotSourced = $MyInvocation.InvocationName -eq "."

if (Test-Path $venvPython) {
    $python = $venvPython
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $python = "python"
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $python = "py"
} else {
    Write-Error "Python 3.12+ is required: install it from https://www.python.org/downloads/"
    return
}

Push-Location $root
try {
    & $python (Join-Path $root "scripts\dev.py") $Target @Rest
    $code = $LASTEXITCODE
} finally {
    Pop-Location
}

if ($code -eq 0 -and $Target -in @("up", "setup", "test", "status") -and (Test-Path $activate)) {
    if ($dotSourced) {
        . $activate
        # Same admin token the stack was started with (.env, or the development default).
        $token = & $venvPython (Join-Path $root "scripts\dev.py") token
        if ($LASTEXITCODE -eq 0 -and $token) { $env:CELLCTL_TOKEN = "$token".Trim() }
        Write-Host "Virtual environment activated and CELLCTL_TOKEN set: cellctl is ready in this terminal."
    } elseif (-not $env:VIRTUAL_ENV) {
        Write-Host "Tip: run '. .\make up' (with the leading dot) or '.venv\Scripts\Activate.ps1' to use cellctl in this terminal."
    }
}

if ($dotSourced) {
    $global:LASTEXITCODE = $code   # never `exit` a dot-sourced script: it would close the terminal
} else {
    exit $code
}
