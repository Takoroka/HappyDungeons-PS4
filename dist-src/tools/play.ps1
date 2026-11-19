# Happy Dungeons (PS4) - start the game. Used by the "Play Happy Dungeons" .bat files.
# -Vblank: the emulator's vblank rate; the game shows one frame every two vblanks (120 = 60 fps, 240 = 120 fps).
param([int]$Vblank = 120)
$ErrorActionPreference = 'Stop'
trap {
    Write-Host "Could not start: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
$Pkg = Split-Path -Parent $PSScriptRoot
$Emu = Join-Path $Pkg 'shadPS4'
$UserDir = Join-Path $Emu 'user'
$SettingsPath = Join-Path $Pkg 'settings.json'
$Utf8 = New-Object System.Text.UTF8Encoding $false

$users = Get-Content (Join-Path $UserDir 'users.json') -Raw | ConvertFrom-Json
$me = $users.Users.user | Where-Object { $_.user_id -eq 1000 }
if (-not (Test-Path $SettingsPath) -or -not $me.shadnet_npid) {
    Write-Host 'Happy Dungeons is not set up yet: starting setup first.' -ForegroundColor Yellow
    $global:LASTEXITCODE = 0
    & (Join-Path $PSScriptRoot 'setup.ps1')
    if ($LASTEXITCODE -ne 0) { exit 1 }
}
$settings = Get-Content $SettingsPath -Raw | ConvertFrom-Json
$eboot = Join-Path $settings.gameFolder 'eboot.bin'
if (-not (Test-Path $eboot)) { throw "The game is not at $($settings.gameFolder) any more: run Setup Happy Dungeons.bat again." }

$cfgPath = Join-Path $UserDir 'config.json'
$cfg = Get-Content $cfgPath -Raw
$new = [regex]::Replace($cfg, '("vblank_frequency"\s*:\s*)\d+', '${1}' + $Vblank)
if ($new -ne $cfg) { [IO.File]::WriteAllText($cfgPath, $new, $Utf8) }

Write-Host "Starting Happy Dungeons as $($me.shadnet_npid) at $([int]($Vblank / 2)) fps..."
Start-Process -FilePath (Join-Path $Emu 'shadPS4.exe') -ArgumentList ('-g "' + $eboot + '"') -WorkingDirectory $Emu
