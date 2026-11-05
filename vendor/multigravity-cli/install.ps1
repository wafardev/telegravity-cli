# multigravity Windows installer
[CmdletBinding()]
param(
    [string]$InstallDir = $env:INSTALL_DIR
)

$RealHome = if ($env:REAL_HOME) { 
    $env:REAL_HOME 
} elseif ($env:USERPROFILE -and $env:USERPROFILE -like "*\.config\multigravity-profiles*") {
    $env:USERPROFILE.Substring(0, $env:USERPROFILE.IndexOf("\.config\multigravity-profiles"))
} elseif ($env:USERPROFILE) { 
    $env:USERPROFILE 
} else { 
    $env:HOME 
}

if (-not $InstallDir) {
    $InstallDir = Join-Path $RealHome ".local\bin"
}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "==> Installing mgy to $InstallDir..." -ForegroundColor Cyan

if (-not (Test-Path $InstallDir)) {
    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
}

$ProfilesDir = Join-Path $RealHome ".config\multigravity-profiles"
if (-not (Test-Path $ProfilesDir)) {
    New-Item -ItemType Directory -Path $ProfilesDir -Force | Out-Null
}

Copy-Item -Path (Join-Path $ScriptDir "bin\mgy.ps1") -Destination (Join-Path $InstallDir "mgy.ps1") -Force
Copy-Item -Path (Join-Path $ScriptDir "bin\mgy.cmd") -Destination (Join-Path $InstallDir "mgy.cmd") -Force
Copy-Item -Path (Join-Path $ScriptDir "bin\multigravity.cmd") -Destination (Join-Path $InstallDir "multigravity.cmd") -Force
Copy-Item -Path (Join-Path $ScriptDir "bin\mgy.ps1") -Destination (Join-Path $InstallDir "multigravity.ps1") -Force

$quotaSource = Join-Path $ScriptDir "bin\mgy-quota"
if (Test-Path $quotaSource) {
    Copy-Item -Path $quotaSource -Destination (Join-Path $InstallDir "mgy-quota") -Force
    Copy-Item -Path $quotaSource -Destination (Join-Path $InstallDir "mgy-quota.py") -Force
    @"
@echo off
python "%~dp0mgy-quota.py" %*
"@ | Set-Content -Path (Join-Path $InstallDir "mgy-quota.cmd") -Encoding ASCII
}

Write-Host "==> Successfully installed mgy, multigravity, and mgy-quota to $InstallDir" -ForegroundColor Green

# Check if InstallDir is in User PATH
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
$pathList = if ($userPath) { ($userPath -split ";") | Where-Object { $_ -ne "" } } else { @() }
$normalizedInstallDir = $InstallDir.TrimEnd("\/")

$inPath = $false
foreach ($p in $pathList) {
    if ($p.TrimEnd("\/") -eq $normalizedInstallDir) {
        $inPath = $true
        break
    }
}

if (-not $inPath) {
    Write-Host ""
    Write-Host "Notice: $InstallDir is not currently in your User PATH." -ForegroundColor Yellow
    Write-Host "Adding $InstallDir to your User PATH environment variable..." -ForegroundColor Cyan
    $newPath = if ($userPath) { "$userPath;$InstallDir" } else { $InstallDir }
    [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
    $env:PATH = "$env:PATH;$InstallDir"
    Write-Host "Added! Please restart your terminal window for PATH updates to take full effect." -ForegroundColor Green
}

Write-Host ""
Write-Host "Get started by running:" -ForegroundColor Cyan
Write-Host "  mgy list"
Write-Host "  mgy new <profile_name>"
