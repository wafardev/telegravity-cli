# multigravity Windows uninstaller
[CmdletBinding()]
param(
    [string]$InstallDir = $env:INSTALL_DIR
)

if (-not $InstallDir) {
    $InstallDir = Join-Path $env:USERPROFILE ".local\bin"
}

Write-Host "==> Uninstalling mgy from $InstallDir..." -ForegroundColor Yellow

$files = @(
    "mgy.ps1",
    "mgy.cmd",
    "multigravity.ps1",
    "multigravity.cmd",
    "mgy-quota.py",
    "mgy-quota.cmd",
    "mgy-quota"
)

foreach ($f in $files) {
    $p = Join-Path $InstallDir $f
    if (Test-Path $p) {
        Remove-Item -Path $p -Force -ErrorAction SilentlyContinue
    }
}

Write-Host "==> multigravity binaries removed successfully." -ForegroundColor Green

$ProfilesDir = Join-Path $env:USERPROFILE ".config\multigravity-profiles"
if (Test-Path $ProfilesDir) {
    Write-Host ""
    Write-Host "Notice: Your profile workspaces and credentials remain at:" -ForegroundColor Yellow
    Write-Host "  $ProfilesDir"
    $resp = Read-Host "Do you want to delete all saved profiles and credentials as well? [y/N]"
    if ($resp -match "^[yY]") {
        Remove-Item -Path $ProfilesDir -Recurse -Force
        Write-Host "Profiles directory deleted." -ForegroundColor Green
    } else {
        Write-Host "Profiles preserved."
    }
}
