# Happy Dungeons (PS4) - switch "keep saves on this PC" on or off. Started by "Saves on this PC - on or off.bat".
# Off (the default): your character is saved on the Happy Dungeons server, the same one the PC version uses.
# On: the game saves to files in this folder instead, and the server is not used for saves.
# -Set on / -Set off: no question asked. Changes only shadPS4\user\patches\HappyDungeonsSaves\HappyDungeons_saves.xml.
param([ValidateSet('', 'on', 'off')][string]$Set = '')
$ErrorActionPreference = 'Stop'
trap {
    Write-Host "Nothing changed: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
$Pkg = Split-Path -Parent $PSScriptRoot
$Patch = Join-Path $Pkg 'shadPS4\user\patches\HappyDungeonsSaves\HappyDungeons_saves.xml'
$SaveDir = Join-Path $Pkg 'shadPS4\user\download\CUSA09120'
$Utf8 = New-Object System.Text.UTF8Encoding $false

if (-not (Test-Path $Patch)) { throw "$Patch is missing (unzip the whole package again)." }
$xml = [IO.File]::ReadAllText($Patch)
$m = [regex]::Match($xml, 'isEnabled="(true|false)"')
if (-not $m.Success) { throw "$Patch has no on/off setting." }
$on = $m.Groups[1].Value -eq 'true'
$files = @(Get-ChildItem -Path $SaveDir -Filter 'hds_*' -ErrorAction SilentlyContinue)

Write-Host ''
if ($on) {
    Write-Host 'Saves on this PC: ON' -ForegroundColor Yellow
    Write-Host "  The game saves to $SaveDir ($($files.Count) save file(s) there now), not to the server."
    Write-Host '  Switching it OFF: the game uses your character on the Happy Dungeons server again (as it was when you'
    Write-Host '  switched this on). The files here stay; switch on again to go back to them.'
} else {
    Write-Host 'Saves on this PC: OFF (normal: your character is saved on the Happy Dungeons server)' -ForegroundColor Green
    Write-Host '  Switching it ON: the game saves to files on this PC instead.'
    Write-Host '    - That character is NOT shared with the PC version or your other computers, and the server keeps no'
    Write-Host '      copy: back up the hds_*.bin / hds_*.bak files yourself.'
    if ($files.Count -eq 0) {
        Write-Host '    - There is no save on this PC yet: the game starts with a NEW character. Your server character'
        Write-Host '      stays on the server, unchanged; switch off again to play it.'
    } else {
        Write-Host "    - $($files.Count) save file(s) already here: the game loads the character in them."
    }
    Write-Host '    - Online play (co-op, friends, events, shop) still uses the server.'
}
Write-Host ''
if (-not $Set) {
    $answer = Read-Host "Switch it $(if ($on) { 'OFF' } else { 'ON' })? (y/n)"
    if ($answer -notmatch '^\s*[yY]') { Write-Host 'Nothing changed.'; exit 0 }
    $Set = if ($on) { 'off' } else { 'on' }
}
$want = $Set -eq 'on'
if ($want -eq $on) { Write-Host "Already $($Set.ToUpper()); nothing changed."; exit 0 }
$value = if ($want) { 'true' } else { 'false' }
$new = $xml.Substring(0, $m.Index) + 'isEnabled="' + $value + '"' + $xml.Substring($m.Index + $m.Length)
[IO.File]::WriteAllText($Patch, $new, $Utf8)
if ($want) { New-Item -ItemType Directory -Force -Path $SaveDir | Out-Null }
Write-Host "Saves on this PC is now $($Set.ToUpper()). It takes effect the next time you start the game." -ForegroundColor Cyan
