HAPPY DUNGEONS - RUNNING THE SERVER ON A CLOUD VPS
===================================================

What you get: the server runs all the time on a small rented Linux machine (a "VPS"). Players connect to
https://<address> - everything, including the key each player signs in with, is encrypted. Only players who
know the join code can register.

Any provider that gives you an Ubuntu 24.04 server with a public IPv4 address works (Hetzner, DigitalOcean,
Vultr, Linode, OVH, Oracle Cloud's free tier, ...). The smallest plan (1 CPU, 1 GB RAM) is plenty.

Every command below is typed in PowerShell, in the package folder (the one with "2 - Play Happy Dungeons").
In the examples 203.0.113.10 stands for your server's IP address.


1. MAKE YOUR SSH KEY (once)
   powershell -ExecutionPolicy Bypass -File .\server\deploy\deploy.ps1 -NewKey

   It asks for a passphrase (Enter twice = none) and prints one line starting with "ssh-ed25519". Copy it.
   The key itself stays in %USERPROFILE%\.ssh\happydungeons_vps - keep that file private.


2. CREATE THE VPS
   - Image: Ubuntu 24.04 LTS. Smallest plan.
   - Login / authentication: SSH key -> paste the "ssh-ed25519 ..." line.
   - If the provider has its own firewall ("security group", "VCN security list", ...), let TCP 22, 80 and
     443 in. The server's own firewall is set up for you.
   - Note the server's IPv4 address.


3. MOVE THE SERVER
   Close the server window on your PC first (so nothing changes while the data is copied), then:

   powershell -ExecutionPolicy Bypass -File .\server\deploy\deploy.ps1 -Server 203.0.113.10 -JoinCode pick-a-code -WithData

   It uploads the server, installs everything (Python, Caddy for HTTPS, a firewall, a service that starts
   by itself), copies your players, saves, friends and jewels, and ends with:

       Server address : 203-0-113-10.sslip.io

   The first run takes a few minutes. If it stops part way, run the same command again - but only until
   anyone has played on the VPS: after that, leave -WithData out (step 5). With it the setup refuses to
   copy your PC's older data over theirs anyway.
   If your key has a passphrase, it is asked twice per run (once to upload, once to run the setup).
   The join code: 4-64 letters, digits, dots, dashes or underscores.
   If your provider's login is not root (for example "ubuntu"), add:  -User ubuntu


4. CONNECT EVERYONE (you too)
   Everyone needs this package's current WinDurango files (they know how to talk HTTPS to the server).
   Each player runs "4 - Join a friend's server", types the address (203-0-113-10.sslip.io - or just the
   IP), then the join code. The join code is only needed once, the first time a new player registers.

   Send your friends the address and the join code. From now on the VPS has the real data: do not start
   the server on your PC any more.


5. UPDATE THE SERVER LATER
   When experimentalserver.py or server\web changes:

   powershell -ExecutionPolicy Bypass -File .\server\deploy\deploy.ps1 -Server 203.0.113.10

   Without -WithData the players' data on the VPS is left alone. Do not add -WithData again unless you
   really want your PC's (older) data copied over the VPS's: it stops when the VPS's player list is newer,
   and only -ForceData makes it go ahead. (Even then the VPS first saves a snapshot of its data in
   /opt/happydungeons/backups.)

   New join code:      ... -JoinCode new-code        No join code:  ... -JoinCode none
   Your own domain:    ... -Domain games.example.com (point its DNS A record at the VPS first)
   Back to sslip.io:   ... -Domain auto
   Try without sending anything:  ... -DryRun


6. BACK UP THE DATA
   Copy the whole data folder to your PC (a new folder named after today's date):

   scp -i $HOME\.ssh\happydungeons_vps -r root@203.0.113.10:/opt/happydungeons/data .\vps-backup-2026-09-18

   For a copy taken with the server paused (nothing half-written):

   ssh -i $HOME\.ssh\happydungeons_vps root@203.0.113.10 "systemctl stop happydungeons; tar -czf /root/hd-data.tar.gz -C /opt/happydungeons data; systemctl start happydungeons"
   scp -i $HOME\.ssh\happydungeons_vps root@203.0.113.10:/root/hd-data.tar.gz .

   Put a backup folder back on the server (replaces what is there; the old data is kept in backups):

   powershell -ExecutionPolicy Bypass -File .\server\deploy\deploy.ps1 -Server 203.0.113.10 -WithData -ReplaceData -DataDir .\vps-backup-2026-09-18

   The data holds every player's secret key (_players.json). Keep backups private.

   If your login is not root (for example "ubuntu"): the data folder is readable by the server only, so
   pack it with sudo first, then copy the archive and delete it on the VPS:

   ssh -i $HOME\.ssh\happydungeons_vps ubuntu@203.0.113.10 "sudo systemctl stop happydungeons; sudo tar -czf /tmp/hd-data.tar.gz -C /opt/happydungeons data; sudo chown ubuntu /tmp/hd-data.tar.gz; sudo chmod 600 /tmp/hd-data.tar.gz; sudo systemctl start happydungeons"
   scp -i $HOME\.ssh\happydungeons_vps ubuntu@203.0.113.10:/tmp/hd-data.tar.gz .
   ssh -i $HOME\.ssh\happydungeons_vps ubuntu@203.0.113.10 "rm /tmp/hd-data.tar.gz"


7. HANDY COMMANDS ON THE VPS
   Log in:   ssh -i $HOME\.ssh\happydungeons_vps root@203.0.113.10

   (Not root? Put sudo in front of each of these.)
   journalctl -u happydungeons -f        live server log (Ctrl+C to stop watching)
   systemctl restart happydungeons       restart the server
   journalctl -u caddy -n 50             HTTPS and certificate messages
   cat /opt/happydungeons/server/server_config.json      shows the join code
   ls /opt/happydungeons/backups         snapshots taken before each data copy


IF SOMETHING GOES WRONG
   - "REMOTE HOST IDENTIFICATION HAS CHANGED" after you rebuilt the VPS:   ssh-keygen -R 203.0.113.10
   - "UNPROTECTED PRIVATE KEY FILE": the key must be readable by you only. Make a new one with -NewKey.
   - The HTTPS check fails: usually the provider's firewall still blocks 80/443 (step 2). The certificate
     log (journalctl -u caddy -n 50) tells. If Let's Encrypt refuses sslip.io names, use a free name from
     a service such as DuckDNS, point it at the VPS, and run deploy.ps1 again with -Domain yourname.duckdns.org.
   - "sudo: a password is required": that login needs password-less sudo - use -User root if you can.
   - Anything else: run the same deploy.ps1 command again; it picks up where it stopped. Drop -WithData
     from it once anyone has played on the VPS.
   - Oracle Cloud: its Ubuntu images keep their own iptables rules; the setup opens 80/443 in those
     instead of turning on ufw. The VCN security list must still let 80 and 443 in (step 2).
