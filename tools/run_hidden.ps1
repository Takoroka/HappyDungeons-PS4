# Runs Happy Dungeons (PS4, CUSA09120) in the portable shadPS4 on a hidden desktop,
# takes screenshots at the given seconds, then stops it and keeps the log.
# Example: powershell -File run_hidden.ps1 -Tag boot1 -Shots 20,40,60,90 -Seconds 95
param(
    [string]$Tag = ("run_" + (Get-Date -Format "yyyyMMdd_HHmmss")),
    [string]$Shots = "30,60,90",
    [int]$Seconds = 95,
    [string]$Eboot = "D:\PS4 ROMS\CUSA09120\eboot.bin",
    [string]$ExtraArgs = "",
    [switch]$Keep
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$emu = Join-Path $root "shadps4"
$exe = Join-Path $emu "shadPS4.exe"
$out = Join-Path $root ("runs\" + $Tag)
New-Item -ItemType Directory -Force $out | Out-Null
Add-Type -Path (Join-Path $PSScriptRoot "HiddenDesk.cs") -ReferencedAssemblies System.Drawing

$desk = "HDPS4Test"
$args = "-g `"$Eboot`" $ExtraArgs"
$procId = [HiddenDesk]::Launch($desk, $exe, $args, $emu)
"started pid=$procId desktop=$desk args=$args" | Tee-Object -FilePath (Join-Path $out "run.txt")
$start = Get-Date
$shotList = $Shots -split '[,; ]+' | Where-Object { $_ } | ForEach-Object { [int]$_ } | Sort-Object
foreach ($s in $shotList) {
    $wait = $s - ((Get-Date) - $start).TotalSeconds
    if ($wait -gt 0) { Start-Sleep -Seconds $wait }
    if (-not (Get-Process -Id $procId -ErrorAction SilentlyContinue)) { "process exited before ${s}s" | Tee-Object -Append -FilePath (Join-Path $out "run.txt"); break }
    $png = Join-Path $out ("shot_{0:D3}s.png" -f $s)
    $r = [HiddenDesk]::Capture($desk, $procId, $png)
    "--- ${s}s`n$r" | Tee-Object -Append -FilePath (Join-Path $out "run.txt")
}
$wait = $Seconds - ((Get-Date) - $start).TotalSeconds
if ($wait -gt 0 -and (Get-Process -Id $procId -ErrorAction SilentlyContinue)) { Start-Sleep -Seconds $wait }
if (-not $Keep) {
    Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}
$log = Join-Path $emu "user\log\shad_log.txt"
if (Test-Path $log) { Copy-Item $log (Join-Path $out "shad_log.txt") -Force }
"done after {0:N0}s" -f ((Get-Date) - $start).TotalSeconds | Tee-Object -Append -FilePath (Join-Path $out "run.txt")
