# Second hidden client for the two-player tests (profile shadps4-mp3, account HDMp03, desktop HDPS4Mp3, state runs\current_mp3.txt).
# Copy of hd.ps1 so these runs never collide with other test runs using hd.ps1.
#   hd.ps1 start [-Tag name]      start the game (portable shadPS4) on the hidden desktop
#   hd.ps1 key <name> [-Hold ms]  press a key in the game window (cross, circle, up, down, left, right,
#                                 options, triangle, square, l1, r1, f12/shot, enter ...)
#   hd.ps1 shot [-Tag name]       take a screenshot with shadPS4's own hotkey (saved under runs\<tag>)
#   hd.ps1 status | stop
param(
    [Parameter(Position = 0)][string]$Cmd = "status",
    [Parameter(Position = 1)][string]$Arg = "",
    [string]$Tag = "",
    [int]$Hold = 120,
    [string]$Eboot = "D:\PS4 ROMS\CUSA09120\eboot.bin",
    [string]$Profile = "shadps4-mp3"   # emulator folder under ps4\ (shadps4 = main, shadps4-test = test account)
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$state = Join-Path $root "runs\current_mp3.txt"
$desk = "HDPS4Mp3"
Add-Type -Path (Join-Path $PSScriptRoot "HiddenDesk.cs") -ReferencedAssemblies System.Drawing

function Get-Run {
    if (-not (Test-Path $state)) { return $null }
    $p = (Get-Content $state -Raw).Trim() -split "\|"
    $proc = Get-Process -Id ([int]$p[0]) -ErrorAction SilentlyContinue
    if ($proc -and $proc.ProcessName -eq "shadPS4") {
        return @{ Pid = [int]$p[0]; Tag = $p[1]; Start = [datetime]$p[2]; Profile = $(if ($p.Count -gt 3) { $p[3] } else { "shadps4-mp3" }) }
    }
    return $null
}
$active = Get-Run
if ($active -and $Cmd -ne "start") { $Profile = $active.Profile }
$emu = Join-Path $root $Profile
$exe = Join-Path $emu "shadPS4.exe"

# key name -> virtual key, scan code, extended
$keys = @{
    "cross" = @(0x4E, 0x31, $false); "n" = @(0x4E, 0x31, $false)
    "circle" = @(0x42, 0x30, $false); "b" = @(0x42, 0x30, $false)
    "triangle" = @(0x43, 0x2E, $false); "c" = @(0x43, 0x2E, $false)
    "square" = @(0x56, 0x2F, $false); "v" = @(0x56, 0x2F, $false)
    "options" = @(0x0D, 0x1C, $false); "enter" = @(0x0D, 0x1C, $false)
    "touchpad" = @(0x20, 0x39, $false); "space" = @(0x20, 0x39, $false)
    "l1" = @(0x51, 0x10, $false); "r1" = @(0x55, 0x16, $false)
    "l2" = @(0x45, 0x12, $false); "r2" = @(0x4F, 0x18, $false)
    "up" = @(0x26, 0x48, $true); "down" = @(0x28, 0x50, $true)
    "left" = @(0x25, 0x4B, $true); "right" = @(0x27, 0x4D, $true)
    "lup" = @(0x57, 0x11, $false); "ldown" = @(0x53, 0x1F, $false)
    "lleft" = @(0x41, 0x1E, $false); "lright" = @(0x44, 0x20, $false)
    "f12" = @(0x7B, 0x58, $false); "shot" = @(0x7B, 0x58, $false)
    "esc" = @(0x1B, 0x01, $false)
}

switch ($Cmd) {
    "start" {
        if (Get-Run) { throw "a run is already active; stop it first" }
        if (-not $Tag) { $Tag = "run_" + (Get-Date -Format "yyyyMMdd_HHmmss") }
        $out = Join-Path $root ("runs\" + $Tag); New-Item -ItemType Directory -Force $out | Out-Null
        if ($Profile -eq "shadps4") { & py -3 -I (Join-Path $PSScriptRoot "set_volume.py") 0 }   # test runs stay silent
        $procId = [HiddenDesk]::Launch($desk, $exe, "-g `"$Eboot`"", $emu)
        Set-Content -Path $state -Value ("{0}|{1}|{2:o}|{3}" -f $procId, $Tag, (Get-Date), $Profile)
        "started pid=$procId tag=$Tag"
    }
    "key" {
        $r = Get-Run; if (-not $r) { throw "no active run" }
        $k = $keys[$Arg.ToLower()]; if (-not $k) { throw "unknown key $Arg" }
        [HiddenDesk]::SendKey($desk, $r.Pid, $k[0], $k[1], $k[2], $Hold)
    }
    "shot" {
        $r = Get-Run; if (-not $r) { throw "no active run" }
        $shots = Join-Path $emu "user\screenshots"
        $before = @(Get-ChildItem $shots -File -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName)
        [HiddenDesk]::SendKey($desk, $r.Pid, 0x7B, 0x58, $false, 80) | Out-Null
        $new = $null
        for ($i = 0; $i -lt 30 -and -not $new; $i++) {
            Start-Sleep -Milliseconds 300
            $new = Get-ChildItem $shots -File -ErrorAction SilentlyContinue | Where-Object { $before -notcontains $_.FullName } | Select-Object -First 1
        }
        if (-not $new) { "no screenshot appeared"; break }
        Start-Sleep -Milliseconds 500
        $t = ((Get-Date) - $r.Start).TotalSeconds
        $dest = Join-Path $root ("runs\{0}\{1}_{2:000}s{3}" -f $r.Tag, ($(if ($Tag) { $Tag } else { "shot" })), $t, $new.Extension)
        Copy-Item $new.FullName $dest
        $dest
    }
    "stop" {
        $r = Get-Run
        if ($r) {
            Stop-Process -Id $r.Pid -Force; Start-Sleep 2
            $log = Join-Path $emu "user\log\shad_log.txt"
            if (Test-Path $log) { Copy-Item $log (Join-Path $root ("runs\" + $r.Tag + "\shad_log.txt")) -Force }
            "stopped pid=$($r.Pid) tag=$($r.Tag)"
        } else { "no active run" }
        Remove-Item $state -ErrorAction SilentlyContinue
    }
    default {
        $r = Get-Run
        if ($r) { "running pid=$($r.Pid) tag=$($r.Tag) for {0:N0}s" -f ((Get-Date) - $r.Start).TotalSeconds } else { "no active run" }
    }
}
