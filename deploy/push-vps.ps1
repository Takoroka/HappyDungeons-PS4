# Run ONE step of the Happy Dungeons PS4 install on the VPS (see DEPLOY-VPS.md). Needs the owner's OK per step.
#
#   powershell -ExecutionPolicy Bypass -File push-vps.ps1 -Step check
#   powershell -ExecutionPolicy Bypass -File push-vps.ps1 -Step fetch -DryRun
#
# Steps, in order: check, fetch, setup, start, expose   (then status; rollback undoes setup/start/expose)
# The bundle (deploy\hd-ps4-vps-bundle.tar.gz, built by tools\build_vps_bundle.py) is copied to
# /root/hd-ps4-bundle on the VPS and install.sh runs from there. Nothing else on the VPS is touched by this script.
param(
    [Parameter(Mandatory = $true)][ValidateSet('check', 'fetch', 'setup', 'start', 'expose', 'status', 'rollback')]
    [string]$Step,
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
$Key = Join-Path $env:USERPROFILE '.ssh\happydungeons_vps'
$Target = 'root@107.170.39.185'
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $Here
$Bundle = Join-Path $Here 'hd-ps4-vps-bundle.tar.gz'
$Ssh = @('-i', $Key, '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15')

& py -3 -I (Join-Path $Root 'tools\build_vps_bundle.py')
if ($LASTEXITCODE -ne 0) { throw 'bundle build failed' }

& scp @Ssh $Bundle "${Target}:/root/hd-ps4-vps-bundle.tar.gz"
if ($LASTEXITCODE -ne 0) { throw 'upload failed' }

$flag = ''
if ($DryRun) { $flag = ' --dry-run' }
$remote = "set -e; rm -rf /root/hd-ps4-bundle; tar -xzf /root/hd-ps4-vps-bundle.tar.gz -C /root; cd /root/hd-ps4-bundle; bash install.sh $Step$flag"
& ssh @Ssh $Target $remote
exit $LASTEXITCODE
