<#
.SYNOPSIS
    Puts the Happy Dungeons server on your Ubuntu 24.04 VPS, or updates it.

.DESCRIPTION
    Uploads experimentalserver.py, requirements.txt, web\ and this deploy folder to the VPS with Windows'
    built-in OpenSSH (ssh/scp) and runs setup.sh there as root. setup.sh installs Python, Caddy (HTTPS with a
    free Let's Encrypt certificate), the happydungeons service and the firewall, then checks that it answers.

    Run it again whenever the server code changes: the players' data on the VPS is kept.

    -WithData also copies this PC's server data (players, saves, friends, jewels) and discord_app_id.txt.
    On the VPS the service is stopped, its current data is snapshotted to /opt/happydungeons/backups, your
    files are copied into /opt/happydungeons/data (a file with the same name is overwritten, every other file
    is kept), ownership is fixed and the service starts again. Nothing on the VPS is removed unless you also
    give -ReplaceData, and even then the old data folder is moved into /opt/happydungeons/backups.
    Once people have played on the VPS its player list is newer than yours, and -WithData stops without
    changing anything (it would roll their players and saves back); -ForceData imports anyway.

.EXAMPLE
    .\deploy.ps1 -NewKey
    Creates the SSH key your VPS provider asks for when you create the server, and shows the public half.

.EXAMPLE
    .\deploy.ps1 -Server 203.0.113.10 -JoinCode dungeon-crew -WithData
    The move: sets the VPS up and copies your players and saves over.

.EXAMPLE
    .\deploy.ps1 -Server 203.0.113.10
    Later: new server code, the data on the VPS stays as it is.
#>
[CmdletBinding()]
param(
    # IP address or host name of the VPS.
    [string]$Server,
    # SSH private key. Its .pub twin is what the VPS provider needs.
    [string]$Key = (Join-Path $HOME '.ssh\happydungeons_vps'),
    # SSH login. Anyone but root needs password-less sudo (the "ubuntu" user of cloud images has it).
    [string]$User = 'root',
    [int]$SshPort = 22,
    # Code new players must type once to register. "none" removes it. Left out = keep the server's current one.
    [string]$JoinCode,
    # Your own domain instead of <ip>.sslip.io (its DNS A record must point at the VPS). "auto" = sslip.io again.
    [string]$Domain,
    # Optional e-mail address for Let's Encrypt certificate notices.
    [string]$AcmeEmail,
    # Also copy the local server data (players, saves, friends, jewels) and discord_app_id.txt.
    [switch]$WithData,
    # With -WithData: move the VPS's data aside first instead of merging into it.
    [switch]$ReplaceData,
    # With -WithData: import even though the VPS's player list is newer (people played there since).
    [switch]$ForceData,
    [string]$DataDir = (Join-Path (Split-Path -Parent $PSScriptRoot) 'data'),
    [string]$DiscordAppIdFile = (Join-Path (Split-Path -Parent $PSScriptRoot) 'discord_app_id.txt'),
    # Do not ask before copying data while a local server still seems to be running.
    [switch]$Force,
    # Build the upload and show the ssh/scp commands, without connecting to anything.
    [switch]$DryRun,
    # Create the SSH key pair (unless it exists), show the public key, and stop.
    [switch]$NewKey
)

$ErrorActionPreference = 'Stop'

function Say([string]$Text) {
    Write-Host ''
    Write-Host "==> $Text" -ForegroundColor Cyan
}

function Fail([string]$Text) {
    Write-Host "ERROR: $Text" -ForegroundColor Red
    exit 1
}

function Find-Exe([string[]]$Candidates, [string]$Name) {
    foreach ($candidate in $Candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) { return $candidate }
    }
    if ($Name) {
        $command = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($command) { return $command.Source }
    }
    return $null
}

function Write-LfFile([string]$Path, [string]$Text) {
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, ($Text -replace "`r`n", "`n"), $utf8NoBom)
}

function Format-Command([string]$Exe, [string[]]$Arguments) {
    $parts = (@($Exe) + $Arguments) | ForEach-Object { if ($_ -match '\s') { '"' + $_ + '"' } else { $_ } }
    return ($parts -join ' ')
}

$here = $PSScriptRoot
if (-not $here) { $here = Split-Path -Parent $MyInvocation.MyCommand.Path }
$serverDir = Split-Path -Parent $here
$openSsh = @((Join-Path $env:SystemRoot 'System32\OpenSSH'), (Join-Path $env:SystemRoot 'Sysnative\OpenSSH'))

# ---------------------------------------------------------------------------------------------------------------
# -NewKey: make the key pair the VPS provider asks for
# ---------------------------------------------------------------------------------------------------------------
if ($NewKey) {
    $keygen = Find-Exe ($openSsh | ForEach-Object { Join-Path $_ 'ssh-keygen.exe' }) 'ssh-keygen.exe'
    if (-not $keygen) { Fail 'OpenSSH is missing. Add it in Settings > System > Optional features > OpenSSH Client.' }
    if (Test-Path -LiteralPath $Key) {
        Say "The key already exists: $Key"
    } else {
        $keyFolder = Split-Path -Parent $Key
        if (-not (Test-Path -LiteralPath $keyFolder)) { New-Item -ItemType Directory -Path $keyFolder | Out-Null }
        Say "Creating an SSH key pair: $Key"
        Write-Host 'ssh-keygen asks for a passphrase: type one to protect the key, or press Enter twice for none.'
        & $keygen -t ed25519 -f $Key -C 'happydungeons-vps'
        if ($LASTEXITCODE -ne 0) { Fail 'ssh-keygen did not create the key.' }
    }
    if (-not (Test-Path -LiteralPath "$Key.pub")) { Fail "The public key $Key.pub is missing." }
    Write-Host ''
    Write-Host 'Public key - paste this line into your VPS provider''s "SSH key" box when you create the server:' -ForegroundColor Green
    Write-Host ''
    Get-Content -LiteralPath "$Key.pub" | ForEach-Object { Write-Host $_ }
    Write-Host ''
    Write-Host 'Keep the other file (without .pub) private: it is the key to your server.'
    exit 0
}

# ---------------------------------------------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------------------------------------------
if (-not $Server) { Fail 'Which server? Use -Server <IP address or host name>  (or -NewKey to create the SSH key first).' }
$Server = $Server.Trim()
if ($Server -notmatch '^[A-Za-z0-9.:\-]+$') { Fail "'$Server' is not an IP address or host name." }
if ($User -notmatch '^[a-z_][a-z0-9_.\-]*$') { Fail "'$User' is not a valid Linux user name." }
if ($SshPort -lt 1 -or $SshPort -gt 65535) { Fail "-SshPort $SshPort is not a port number." }
if ($JoinCode -and $JoinCode -ne 'none' -and $JoinCode -notmatch '^[A-Za-z0-9._~\-]{4,64}$') {
    Fail 'The join code must be 4-64 letters, digits, dots, dashes, underscores or tildes (no spaces).'
}
if ($Domain -and $Domain -ne 'auto' -and $Domain -notmatch '^([A-Za-z0-9]([A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$') {
    Fail "'$Domain' is not a valid domain name (example: games.example.com)."
}
if ($AcmeEmail -and $AcmeEmail -notmatch '^[^@\s''"]+@[^@\s''"]+\.[A-Za-z]{2,}$') { Fail "'$AcmeEmail' is not an e-mail address." }
if ($ReplaceData -and -not $WithData) { Fail '-ReplaceData only works together with -WithData.' }
if ($ForceData -and -not $WithData) { Fail '-ForceData only works together with -WithData.' }

foreach ($name in 'experimentalserver.py', 'requirements.txt', 'web\index.html', 'deploy\setup.sh') {
    if (-not (Test-Path -LiteralPath (Join-Path $serverDir $name))) { Fail "$name is missing in $serverDir" }
}

$ssh = Find-Exe ($openSsh | ForEach-Object { Join-Path $_ 'ssh.exe' }) 'ssh.exe'
$scp = Find-Exe ($openSsh | ForEach-Object { Join-Path $_ 'scp.exe' }) 'scp.exe'
if (-not $DryRun) {
    if (-not $ssh -or -not $scp) { Fail 'OpenSSH is missing. Add it in Settings > System > Optional features > OpenSSH Client.' }
    if (-not (Test-Path -LiteralPath $Key -PathType Leaf)) {
        Fail "SSH key not found: $Key`n       Create one with:  .\deploy.ps1 -NewKey   (then give the .pub file to your VPS provider)"
    }
}
if (-not $ssh) { $ssh = 'ssh.exe' }
if (-not $scp) { $scp = 'scp.exe' }

# The package copy is what gets deployed; say so if the server running on this PC is a different version.
$liveServer = Join-Path (Split-Path -Parent $DataDir) 'experimentalserver.py'
if (Test-Path -LiteralPath $liveServer) {
    $packageHash = (Get-FileHash -LiteralPath (Join-Path $serverDir 'experimentalserver.py')).Hash
    if ((Get-FileHash -LiteralPath $liveServer).Hash -ne $packageHash) {
        Write-Warning "server\experimentalserver.py differs from $liveServer - deploying the package copy."
    }
}

$tar = $null
if ($WithData) {
    if (-not (Test-Path -LiteralPath $DataDir -PathType Container)) { Fail "Data folder not found: $DataDir  (use -DataDir)" }
    if (-not (Test-Path -LiteralPath (Join-Path $DataDir '_players.json'))) {
        Write-Warning "$DataDir has no _players.json - is it really the server's data folder?"
    }
    # Windows' own tar (bsdtar). A GNU tar from Git would read "C:\..." as a remote host, so no PATH lookup.
    $tar = Find-Exe @((Join-Path $env:SystemRoot 'System32\tar.exe'), (Join-Path $env:SystemRoot 'Sysnative\tar.exe')) $null
    if (-not $tar) { Fail 'tar.exe is missing from Windows\System32 (it ships with Windows 10 1803 and later).' }
    $listening = $null
    try { $listening = Get-NetTCPConnection -State Listen -LocalPort 5000 -ErrorAction Stop } catch { $listening = $null }
    if ($listening -and -not $Force -and -not $DryRun) {
        Write-Warning 'Something listens on port 5000 on this PC - probably the local server. Close it first:'
        Write-Warning 'progress saved on this PC after the copy will not reach the VPS.'
        $answer = Read-Host 'Copy the data anyway? (y/N)'
        if ($answer -notmatch '^(y|yes)$') { Fail 'Stopped. Close the local server and run this again.' }
    }
}

# ---------------------------------------------------------------------------------------------------------------
# Build the upload: code, web page, deploy kit, optional data archive, and run.sh with the chosen settings
# ---------------------------------------------------------------------------------------------------------------
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$stageName = "hd-deploy-$stamp"
$stage = Join-Path ([System.IO.Path]::GetTempPath()) $stageName
$scpHost = $Server
if ($Server.Contains(':')) { $scpHost = "[$Server]" }
$sshOptions = @('-i', $Key, '-o', 'StrictHostKeyChecking=accept-new', '-o', 'IdentitiesOnly=yes',
                '-o', 'ConnectTimeout=20', '-o', 'ServerAliveInterval=30')
# The upload goes to the SSH user's home folder (private), not /tmp: with -WithData it holds the players' secrets.
$scpArgs = $sshOptions + @('-P', "$SshPort", '-r', $stage, "$User@${scpHost}:$stageName")
$remoteCommand = "bash ~/$stageName/run.sh"
if ($User -ne 'root') { $remoteCommand = "sudo -n bash ~/$stageName/run.sh" }
$sshArgs = $sshOptions + @('-p', "$SshPort", "$User@$Server", $remoteCommand)

$previousEncoding = $null
$exitCode = 0
try {
    Say "Preparing the upload in $stage"
    New-Item -ItemType Directory -Path $stage | Out-Null
    Copy-Item -LiteralPath (Join-Path $serverDir 'experimentalserver.py') -Destination $stage
    Copy-Item -LiteralPath (Join-Path $serverDir 'requirements.txt') -Destination $stage
    Copy-Item -LiteralPath (Join-Path $serverDir 'web') -Destination $stage -Recurse
    if (Test-Path -LiteralPath (Join-Path $serverDir 'discord_app_id.txt')) {
        # Only used when the VPS has no discord_app_id.txt yet.
        Copy-Item -LiteralPath (Join-Path $serverDir 'discord_app_id.txt') -Destination $stage
    }
    $stageDeploy = Join-Path $stage 'deploy'
    New-Item -ItemType Directory -Path $stageDeploy | Out-Null
    Write-LfFile (Join-Path $stageDeploy 'setup.sh') ([System.IO.File]::ReadAllText((Join-Path $here 'setup.sh')))
    foreach ($name in 'deploy.ps1', 'README-deploy.txt') {
        if (Test-Path -LiteralPath (Join-Path $here $name)) { Copy-Item -LiteralPath (Join-Path $here $name) -Destination $stageDeploy }
    }

    if ($WithData) {
        Say "Packing the data folder $DataDir"
        $archive = Join-Path $stage 'data.tar.gz'
        $tarArgs = @('-czf', $archive, '-C', $DataDir, '.')
        if (Test-Path -LiteralPath $DiscordAppIdFile -PathType Leaf) {
            # Lands as data/discord_app_id.txt on the VPS, whatever the local file is called.
            $extra = Join-Path $stage 'extra'
            New-Item -ItemType Directory -Path $extra | Out-Null
            Copy-Item -LiteralPath $DiscordAppIdFile -Destination (Join-Path $extra 'discord_app_id.txt')
            $tarArgs += @('-C', $extra, 'discord_app_id.txt')
        } else {
            Write-Warning "No $DiscordAppIdFile - the VPS keeps (or gets the package's) Discord application id."
        }
        & $tar @tarArgs
        if ($LASTEXITCODE -ne 0) { Fail 'tar could not pack the data folder.' }
        if ($extra) { Remove-Item -LiteralPath $extra -Recurse -Force }
        $fileCount = @(Get-ChildItem -LiteralPath $DataDir -Recurse -File).Count
        Write-Host ("{0} files, archive {1:N1} KB" -f $fileCount, ((Get-Item -LiteralPath $archive).Length / 1KB))
    }

    $run = @(
        '#!/usr/bin/env bash',
        '# Written by deploy.ps1: runs setup.sh with the options given on the PC, then deletes this upload.',
        'set -euo pipefail',
        'STAGE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)',
        'case $STAGE in */hd-deploy-*) trap ''rm -rf "$STAGE"'' EXIT ;; esac',
        'chmod -R go-rwx "$STAGE"',
        'sed -i ''s/\r$//'' "$STAGE/deploy/setup.sh"',
        'export SRC_DIR="$STAGE"'
    )
    if ($Domain) { $run += "export DOMAIN='$Domain'" }
    if ($JoinCode) { $run += "export JOIN_CODE='$JoinCode'" }
    if ($AcmeEmail) { $run += "export ACME_EMAIL='$AcmeEmail'" }
    if ($WithData) { $run += 'export DATA_ARCHIVE="$STAGE/data.tar.gz"' }
    if ($ReplaceData) { $run += 'export REPLACE_DATA=1' }
    if ($ForceData) { $run += 'export FORCE_DATA=1' }
    $run += 'bash "$STAGE/deploy/setup.sh"'
    Write-LfFile (Join-Path $stage 'run.sh') (($run -join "`n") + "`n")

    Write-Host 'Upload contents:'
    Get-ChildItem -LiteralPath $stage -Recurse -File | ForEach-Object {
        Write-Host ('  ' + $_.FullName.Substring($stage.Length + 1))
    }

    if ($DryRun) {
        Say 'Dry run - nothing is sent. These are the commands a real run uses:'
        Write-Host ('  ' + (Format-Command $scp $scpArgs))
        Write-Host ('  ' + (Format-Command $ssh $sshArgs))
        Write-Host ''
        Write-Host 'run.sh:'
        Get-Content -LiteralPath (Join-Path $stage 'run.sh') | ForEach-Object { Write-Host "  $_" }
        return
    }

    # -------------------------------------------------------------------------------------------------------------
    # Upload and run
    # -------------------------------------------------------------------------------------------------------------
    Say "Uploading to $User@${Server}:~/$stageName"
    & $scp @scpArgs
    if ($LASTEXITCODE -ne 0) {
        Fail ("The upload failed. Check the address, that the VPS was created with $Key.pub, and that port $SshPort is open.`n" +
              "       If you rebuilt the VPS, forget its old fingerprint with:  ssh-keygen -R $Server")
    }

    Say "Running setup.sh on $Server (a few minutes the first time: packages, Caddy, certificate)"
    try {
        $previousEncoding = [Console]::OutputEncoding
        [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
    } catch { $previousEncoding = $null }
    $publicUrl = $null
    & $ssh @sshArgs | ForEach-Object {
        $line = [string]$_
        Write-Host $line
        if ($line -match '^PUBLIC_URL=(https://\S+)') { $publicUrl = $Matches[1] }
    }
    $exitCode = $LASTEXITCODE

    if ($exitCode -eq 255) {
        Fail 'SSH could not connect or was cut off. Nothing may have changed; run the same command again.'
    }
    if ($exitCode -ne 0 -and $exitCode -ne 2) {
        if ($User -ne 'root') { Write-Warning "If sudo asked for a password: $User needs password-less sudo, or use -User root." }
        Fail "setup.sh stopped with exit code $exitCode - the lines above say why. Running this again is safe."
    }

    # -------------------------------------------------------------------------------------------------------------
    # Check from this PC and sum up
    # -------------------------------------------------------------------------------------------------------------
    if ($publicUrl) {
        Say "Checking $publicUrl from this PC"
        try {
            [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
            $response = Invoke-WebRequest -Uri "$publicUrl/windurango/players" -UseBasicParsing -TimeoutSec 20
            $players = $response.Content | ConvertFrom-Json
            Write-Host ("OK    {0} registered players, {1} online" -f @($players.players).Count, $players.online) -ForegroundColor Green
        } catch {
            Write-Warning "This PC cannot reach $publicUrl yet: $($_.Exception.Message)"
            $exitCode = 2
        }
    }

    Write-Host ''
    Write-Host '==========================================================================================' -ForegroundColor Green
    if ($publicUrl) {
        $address = $publicUrl -replace '^https://', ''
        Write-Host " Server address : $address"
    } else {
        Write-Host ' Server address : see setup.sh''s summary above'
    }
    if ($JoinCode -and $JoinCode -ne 'none') {
        Write-Host " Join code      : $JoinCode"
    } elseif ($JoinCode -eq 'none') {
        Write-Host ' Join code      : removed - anyone who finds the address can register'
    } else {
        Write-Host ' Join code      : unchanged (setup.sh''s summary above shows it)'
    }
    Write-Host ' Everyone (you too): run "4 - Join a friend''s server", type the address, then the join code.'
    Write-Host " Update later   : .\deploy.ps1 -Server $Server        (no -WithData: the VPS's data is the real copy now)"
    Write-Host '==========================================================================================' -ForegroundColor Green
    if ($exitCode -eq 2) {
        Write-Warning 'The server is installed, but HTTPS did not answer yet (see the warnings above).'
    }
} finally {
    if ($previousEncoding) {
        try { [Console]::OutputEncoding = $previousEncoding } catch { }
    }
    # The upload folder may hold a copy of the players' secrets: never leave it in %TEMP%.
    if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue }
}
exit $exitCode
