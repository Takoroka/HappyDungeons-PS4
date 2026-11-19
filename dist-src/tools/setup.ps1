# Happy Dungeons (PS4) - setup. Started by "Setup Happy Dungeons.bat"; run it again any time to change something.
# Windows PowerShell 5.1, no other software needed. Changes only files inside this folder.
param(
    [string]$GameFolder,     # folder with eboot.bin of CUSA09120
    [string]$IdentityFile,   # winDurango.json (PC player) or happydungeons_identity.json (kept from an earlier setup)
    [string]$NewPlayer,      # name for a new player
    [int]$P2PPort = 0,       # UDP port for co-op (default 3658)
    [switch]$Unattended      # no questions: only what the parameters say
)
$ErrorActionPreference = 'Stop'
trap {
    Write-Host ''
    Write-Host "Setup stopped: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
$Pkg = Split-Path -Parent $PSScriptRoot
$Emu = Join-Path $Pkg 'shadPS4'
$UserDir = Join-Path $Emu 'user'
$SettingsPath = Join-Path $Pkg 'settings.json'
$IdentityKeep = Join-Path $Pkg 'happydungeons_identity.json'
$Server = Get-Content (Join-Path $PSScriptRoot 'server.json') -Raw | ConvertFrom-Json
$Utf8 = New-Object System.Text.UTF8Encoding $false
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Write-Text($Path, $Text) { [IO.File]::WriteAllText($Path, $Text, $Utf8) }
function Read-Settings {
    if (Test-Path $SettingsPath) { return Get-Content $SettingsPath -Raw | ConvertFrom-Json }
    return New-Object PSObject
}
function Set-Prop($Obj, $Name, $Value) { $Obj | Add-Member -NotePropertyName $Name -NotePropertyValue $Value -Force }
function ConvertTo-JsonString([string]$s) {
    $sb = New-Object Text.StringBuilder '"'
    foreach ($c in $s.ToCharArray()) {
        switch ($c) {
            '"' { [void]$sb.Append('\"') } '\' { [void]$sb.Append('\\') }
            default { if ([int]$c -lt 32) { [void]$sb.AppendFormat('\u{0:x4}', [int]$c) } else { [void]$sb.Append($c) } }
        }
    }
    [void]$sb.Append('"'); return $sb.ToString()
}
# Replace the value of one "key": value pair in config.json text (keys used here are unique in the file).
function Set-JsonValue([string]$Text, [string]$Key, [string]$Literal) {
    $pattern = '("' + [regex]::Escape($Key) + '"\s*:\s*)("(?:[^"\\]|\\.)*"|-?\d+(?:\.\d+)?|true|false|null|\[[^\]]*\])'
    $re = New-Object Text.RegularExpressions.Regex $pattern
    $m = $re.Match($Text)
    if (-not $m.Success) { throw "no '$Key' in the settings file" }
    return $Text.Substring(0, $m.Index) + $m.Groups[1].Value + $Literal + $Text.Substring($m.Index + $m.Length)
}
function Ask([string]$Question, [string]$Default = '') {
    if ($Unattended) { return $Default }
    $answer = Read-Host $Question
    if ([string]::IsNullOrWhiteSpace($answer)) { return $Default }
    return $answer.Trim()
}
function Pick-Folder([string]$Title) {
    if ($Unattended) { return $null }
    try {
        Add-Type -AssemblyName System.Windows.Forms
        $d = New-Object System.Windows.Forms.FolderBrowserDialog
        $d.Description = $Title; $d.ShowNewFolderButton = $false
        if ($d.ShowDialog() -eq 'OK') { return $d.SelectedPath }
    } catch { }
    return (Read-Host "$Title (paste the folder path)").Trim('"', ' ')
}
function Pick-File([string]$Title, [string]$Filter) {
    if ($Unattended) { return $null }
    try {
        Add-Type -AssemblyName System.Windows.Forms
        $d = New-Object System.Windows.Forms.OpenFileDialog
        $d.Title = $Title; $d.Filter = $Filter
        if ($d.ShowDialog() -eq 'OK') { return $d.FileName }
    } catch { }
    return (Read-Host "$Title (paste the file path)").Trim('"', ' ')
}
function Post-Json([string]$Url, $Body) {
    $json = $Body | ConvertTo-Json -Compress
    try {
        return Invoke-RestMethod -Uri $Url -Method Post -ContentType 'application/json' -Body ([Text.Encoding]::UTF8.GetBytes($json)) -TimeoutSec 60
    } catch {
        $reason = $_.Exception.Message
        try {
            $parsed = $_.ErrorDetails.Message | ConvertFrom-Json   # the server's JSON answer
            if ($parsed.reason) { $reason = $parsed.reason }
        } catch { }
        throw "The Happy Dungeons server said no: $reason"
    }
}

Write-Host ''
Write-Host '=== Happy Dungeons (PS4) setup ===' -ForegroundColor Cyan
$settings = Read-Settings

# ---------------------------------------------------------------------------------------------- 1. the game
function Test-Game([string]$Folder) {
    if (-not $Folder) { return 'no folder chosen' }
    $eboot = Join-Path $Folder 'eboot.bin'
    if (-not (Test-Path $eboot)) { return "there is no eboot.bin in $Folder" }
    $head = New-Object byte[] 4
    $fs = [IO.File]::OpenRead($eboot); [void]$fs.Read($head, 0, 4); $fs.Close()
    if (-not ($head[0] -eq 0x7F -and $head[1] -eq 0x45 -and $head[2] -eq 0x4C -and $head[3] -eq 0x46)) {
        return 'eboot.bin is still encrypted (a decrypted dump of the game is needed)'
    }
    $sfo = Join-Path $Folder 'sce_sys\param.sfo'
    if (Test-Path $sfo) {
        $text = [Text.Encoding]::ASCII.GetString([IO.File]::ReadAllBytes($sfo))
        if ($text -notmatch 'CUSA09120') { return 'this is not Happy Dungeons CUSA09120 (European release), which the patches are made for' }
        if ($text -notmatch '01\.11') { Write-Host '  Note: the patches are made for version 01.11 of the game.' -ForegroundColor Yellow }
    }
    return $null
}
$game = $GameFolder
if (-not $game -and $settings.gameFolder -and -not (Test-Game $settings.gameFolder)) {
    $game = $settings.gameFolder
    Write-Host "Game: $game"
    if ((Ask 'Keep this game folder? (Y/n)' 'y') -match '^[nN]') { $game = $null }
}
while ($true) {
    if (-not $game) {
        Write-Host 'Choose the folder of your Happy Dungeons dump (CUSA09120, the one with eboot.bin and sce_sys).'
        $game = Pick-Folder 'Happy Dungeons game folder (CUSA09120)'
    }
    $problem = Test-Game $game
    if (-not $problem) { break }
    if ($Unattended) { throw "Game folder: $problem" }
    Write-Host "  $problem" -ForegroundColor Red
    $game = $null
}
Set-Prop $settings 'gameFolder' ([IO.Path]::GetFullPath($game))
Write-Host "Game folder: $($settings.gameFolder)" -ForegroundColor Green

# ---------------------------------------------------------------------------------------------- 2. firmware modules
$needed = 'libSceAudiodec.sprx', 'libSceJpegDec.sprx', 'libSceJson2.sprx', 'libSceLibcInternal.sprx', 'libScePngDec.sprx', 'libSceRtc.sprx'
$candidates = @((Join-Path $UserDir 'sys_modules'), (Join-Path $env:APPDATA 'shadPS4\sys_modules'))
$modules = $null
foreach ($dir in $candidates) {
    if ((Test-Path $dir) -and @($needed | Where-Object { Test-Path (Join-Path $dir $_) }).Count -eq $needed.Count) { $modules = $dir; break }
}
if (-not $modules) {
    $modules = $candidates[0]
    Write-Host ''
    Write-Host 'PS4 firmware modules are missing. Copy them from your own PS4 (system/common/lib) into:' -ForegroundColor Yellow
    Write-Host "  $modules" -ForegroundColor Yellow
    Write-Host "  needed: $($needed -join ', ')" -ForegroundColor Yellow
    Write-Host '  (If you already use shadPS4, setup finds them in %APPDATA%\shadPS4\sys_modules by itself.)'
} else {
    Write-Host "Firmware modules: $modules" -ForegroundColor Green
}

# ---------------------------------------------------------------------------------------------- 3. the player
$usersPath = Join-Path $UserDir 'users.json'
$users = Get-Content $usersPath -Raw | ConvertFrom-Json
$me = $users.Users.user | Where-Object { $_.user_id -eq 1000 }
$linked = [bool]($me.shadnet_npid -and $me.shadnet_password)
$doLink = -not $linked -or $IdentityFile -or $NewPlayer
if ($linked -and -not $doLink) {
    Write-Host "Player: $($me.shadnet_npid) (already set up)" -ForegroundColor Green
    if ((Ask 'Keep this player? (Y/n)' 'y') -match '^[nN]') { $doLink = $true }
}
if ($doLink) {
    $answer = $null
    $file = $IdentityFile
    $name = $NewPlayer
    if (-not $file -and -not $name) {
        if (Test-Path $IdentityKeep) {
            Write-Host "Found $IdentityKeep from an earlier setup: using that player."
            $file = $IdentityKeep
        } else {
            Write-Host ''
            Write-Host 'Who are you on the Happy Dungeons server?'
            Write-Host '  1  I already play Happy Dungeons on PC (WinDurango): same player, character and friends'
            Write-Host '  2  I am new: make a new player'
            Write-Host '  3  I have a happydungeons_identity.json from an earlier PS4 setup'
            $choice = Ask 'Choose 1, 2 or 3' '2'
            if ($choice -eq '1') {
                Write-Host 'Pick winDurango.json from your PC game folder (Mount\UserData\LocalState\WinDurango\winDurango.json).'
                $file = Pick-File 'winDurango.json of your PC game' 'winDurango.json|winDurango.json|JSON files|*.json'
            } elseif ($choice -eq '3') {
                $file = Pick-File 'happydungeons_identity.json' 'happydungeons_identity.json|happydungeons_identity.json|JSON files|*.json'
            } else {
                $name = Ask 'Player name (1-15 letters, digits or spaces, starting with a letter)' ''
            }
        }
    }
    if ($file) {
        $id = Get-Content $file -Raw | ConvertFrom-Json
        if (-not $id.xuidBase -or -not $id.secret) { throw "$file has no xuidBase/secret (has that game signed in to the server yet?)" }
        $answer = Post-Json ($Server.linkApi.TrimEnd('/') + '/ps4/link') @{ xuidBase = [string]$id.xuidBase; secret = [string]$id.secret }
        if ($file -ne $IdentityKeep -and -not (Test-Path $IdentityKeep)) {
            # keep the key next to the game too, so a later setup (or a reinstall) can link again
            Write-Text $IdentityKeep ((@{ server = $Server.linkApi; gamertag = $answer.gamertag; xuidBase = [string]$id.xuidBase; secret = [string]$id.secret } | ConvertTo-Json))
        }
    } elseif ($name) {
        $answer = Post-Json ($Server.linkApi.TrimEnd('/') + '/ps4/register') @{ gamertag = $name }
        Write-Text $IdentityKeep ((@{ server = $Server.linkApi; gamertag = $answer.gamertag; xuidBase = [string]$answer.xuidBase; secret = [string]$answer.secret } | ConvertTo-Json))
        Write-Host "Your player's key is in $IdentityKeep - keep a copy of that file, it is what signs you in." -ForegroundColor Yellow
    } else {
        throw 'No player chosen.'
    }
    if (-not $answer.ok) { throw "The Happy Dungeons server said no: $($answer.reason)" }
    if ($answer.ownPassword -and -not $answer.password) {
        # this player chose their own shadNet password (account page / happydungeons_account.py --same-login):
        # the server does not know it, so ask for it
        if ($Unattended) { throw "Player $($answer.npid) signs in with a password they chose; run setup without -Unattended to type it." }
        $secure = Read-Host "Player $($answer.npid) signs in with the shadNet password you chose for it. Type it" -AsSecureString
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        try { $typed = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
        if (-not $typed) { throw 'No password typed.' }
        $answer | Add-Member -NotePropertyName password -NotePropertyValue $typed -Force
    }
    $me.shadnet_enabled = $true
    $me.shadnet_npid = [string]$answer.npid
    $me.shadnet_password = [string]$answer.password
    $me.shadnet_email = ''
    $me.shadnet_token = ''
    $me.user_name = [string]$answer.npid
    # users.json: rewrite only user 1000's fields in the original text (keeps the emulator's own formatting)
    $text = Get-Content $usersPath -Raw
    $block = [regex]::Match($text, '\{[^{}]*"user_id"\s*:\s*1000[^{}]*\}')
    if (-not $block.Success) { throw 'users.json has no user 1000' }
    $b = $block.Value
    $b = Set-JsonValue $b 'shadnet_enabled' 'true'
    $b = Set-JsonValue $b 'shadnet_npid' (ConvertTo-JsonString $me.shadnet_npid)
    $b = Set-JsonValue $b 'shadnet_password' (ConvertTo-JsonString $me.shadnet_password)
    $b = Set-JsonValue $b 'shadnet_email' '""'
    $b = Set-JsonValue $b 'shadnet_token' '""'
    $b = Set-JsonValue $b 'user_name' (ConvertTo-JsonString $me.shadnet_npid)
    Write-Text $usersPath ($text.Substring(0, $block.Index) + $b + $text.Substring($block.Index + $block.Length))
    Write-Host "Player: $($answer.npid) = Happy Dungeons player '$($answer.gamertag)'" -ForegroundColor Green
}

# ---------------------------------------------------------------------------------------------- 4. emulator settings
$port = $P2PPort
if (-not $port) { $port = if ($settings.p2pPort) { [int]$settings.p2pPort } else { 3658 } }
Set-Prop $settings 'p2pPort' $port
$cfgPath = Join-Path $UserDir 'config.json'
$cfg = Get-Content $cfgPath -Raw
$fwd = { param($p) (ConvertTo-JsonString ($p -replace '\\', '/')) }
$cfg = Set-JsonValue $cfg 'install_dirs' ('[{"enabled": true, "path": ' + (& $fwd (Split-Path -Parent $settings.gameFolder)) + '}]')
$cfg = Set-JsonValue $cfg 'addon_install_dir' (& $fwd (Join-Path $UserDir 'addcont'))
$cfg = Set-JsonValue $cfg 'sys_modules_dir' (& $fwd $modules)
$cfg = Set-JsonValue $cfg 'connected_to_network' 'true'
$cfg = Set-JsonValue $cfg 'shad_net_enabled' 'true'
$cfg = Set-JsonValue $cfg 'shadnet_server' (ConvertTo-JsonString $Server.shadnet)
$cfg = Set-JsonValue $cfg 'shadnet_webapi_server' (ConvertTo-JsonString $Server.webapi)
$cfg = Set-JsonValue $cfg 'p2p_port' ([string]$port)
Write-Text $cfgPath $cfg
Write-Text $SettingsPath ($settings | ConvertTo-Json)

Write-Host ''
Write-Host 'Done. Start the game with "Play Happy Dungeons.bat" (or the 120/240 fps one).' -ForegroundColor Cyan
Write-Host "Co-op uses UDP port $port on this PC: allow shadPS4 when Windows Firewall asks, and if friends cannot"
Write-Host 'join your games, forward that UDP port on your router (see README.txt).'
