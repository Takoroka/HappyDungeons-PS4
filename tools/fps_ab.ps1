# One timing run for the frame-rate decoupling test (profile shadps4-fps, hidden desktop).
# Boots, presses cross at the title (existing save -> intro cutscene), screenshots at fixed offsets after the press.
param(
    [Parameter(Mandatory = $true)][string]$Tag,
    [int]$Vblank = 60,                 # shadPS4 vblank_frequency; the game renders at half of it
    [switch]$NoDecouple,               # disable the delta-time patch for this run
    [string]$Offsets = "12,18,24,30,36,42",
    [int]$Burst = 0,                   # >0: one screenshot per second from BurstFrom to Burst seconds after the press
    [int]$BurstFrom = 14
)
$ErrorActionPreference = "Stop"
$tools = $PSScriptRoot
$prof = Join-Path (Split-Path -Parent $tools) "shadps4-fps"
$xml = Join-Path $prof "user\patches\HappyDungeonsFps\HappyDungeons_fps.xml"
$h = Join-Path $tools "hd_fps.ps1"
function hdf { & powershell -NoProfile -ExecutionPolicy Bypass -File $h @args }

& py -3 -I (Join-Path $tools "set_cfg.py") $prof "GPU.vblank_frequency=$Vblank" | Out-Null
$t = Get-Content $xml -Raw
$want = if ($NoDecouple) { 'false' } else { 'true' }
$t = [regex]::Replace($t, '(Name="Decouple gameplay from frame rate[^"]*"[^>]*isEnabled=")(true|false)(")', "`${1}$want`${3}")
Set-Content -Path $xml -Value $t -Encoding utf8

$o = hdf start -Tag $Tag
$procId = [int](($o -split "pid=")[1] -split " ")[0]
Start-Sleep 32
$log = Join-Path $prof "user\log\shad_log.txt"
$mon = & py -3 -I (Join-Path $tools "fpsmon.py") $procId $log 3 1
hdf key cross | Out-Null
$press = Get-Date
if ($Burst -gt 0) {
    # one process, one screenshot per second; files are renamed by their time after the press
    Add-Type -Path (Join-Path $tools "HiddenDesk.cs") -ReferencedAssemblies System.Drawing
    $shots = Join-Path $prof "user\screenshots"
    $before = @(Get-ChildItem $shots -File -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName)
    $out = Join-Path (Split-Path -Parent $tools) ("runs\" + $Tag)
    $times = @{}
    for ($s = $BurstFrom; $s -le $Burst; $s++) {
        $wait = $s - ((Get-Date) - $press).TotalSeconds
        if ($wait -gt 0) { Start-Sleep -Milliseconds ([int]($wait * 1000)) }
        $sent = ((Get-Date) - $press).TotalSeconds
        [HiddenDesk]::SendKey("HDPS4Fps", $procId, 0x7B, 0x58, $false, 40) | Out-Null
        $times[$s] = $sent
    }
    Start-Sleep 2
    $new = Get-ChildItem $shots -File | Where-Object { $before -notcontains $_.FullName } | Sort-Object LastWriteTime
    foreach ($f in $new) {
        $t = ($f.LastWriteTime - $press).TotalSeconds
        Copy-Item $f.FullName (Join-Path $out ("b{0:000.0}.png" -f $t))
    }
} else {
    foreach ($s in ($Offsets -split ',' | ForEach-Object { [int]$_ })) {
        $wait = $s - ((Get-Date) - $press).TotalSeconds
        if ($wait -gt 0) { Start-Sleep -Milliseconds ([int]($wait * 1000)) }
        hdf shot -Tag ("x{0:D2}" -f $s) | Out-Null
    }
}
$mon2 = & py -3 -I (Join-Path $tools "fpsmon.py") $procId $log 3 1
hdf stop | Out-Null
"$Tag vblank=$Vblank decouple=$(-not $NoDecouple)"
"title: " + ($mon | Select-Object -Last 1)
"cutscene: " + ($mon2 | Select-Object -Last 2 | Out-String).Trim()
