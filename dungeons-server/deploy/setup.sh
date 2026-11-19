#!/usr/bin/env bash
# Happy Dungeons server (the WinDurango "Xbox Live" emulator) - setup for an Ubuntu 24.04 VPS.
#
# Run it as root. It is safe to run again: it updates the code, keeps the players' data, the join code
# and a domain you gave earlier. deploy.ps1 (on the Windows PC) uploads this folder and runs it for you.
#
#   sudo bash setup.sh                                        address = <public ip with dashes>.sslip.io
#   sudo DOMAIN=games.example.com JOIN_CODE=dungeon-crew bash setup.sh
#
# Settings (environment variables, all optional):
#   DOMAIN        name for the HTTPS certificate. Empty = the one given last time, else a-b-c-d.sslip.io
#                 built from this server's public IPv4. "auto" forgets a domain given earlier.
#   JOIN_CODE     code a NEW player must give to register. "none" removes it. Empty keeps the current one.
#   ACME_EMAIL    e-mail address Let's Encrypt may use for certificate warnings.
#   SRC_DIR       folder with experimentalserver.py, requirements.txt and web/ (default: the folder above
#                 this script). If it has no experimentalserver.py the installed code is kept.
#   DATA_ARCHIVE  .tar.gz of a server data folder to copy into /opt/happydungeons/data (deploy.ps1 -WithData).
#                 Files in it overwrite files of the same name; other files on the server are kept.
#   REPLACE_DATA  1 = move the server's data folder aside first instead of merging (deploy.ps1 -ReplaceData).
#   FORCE_DATA    1 = merge DATA_ARCHIVE even though the server's player list is newer than the archive's
#                 (deploy.ps1 -ForceData). Without it such an import stops: people have played on the server since.
#
# What goes where:
#   /opt/happydungeons/server    the code and server_config.json. server/data and server/discord_app_id.txt
#                                are links into /opt/happydungeons/data, because the server looks next to itself.
#   /opt/happydungeons/data      players, saves, friends, jewels, discord_app_id.txt (same layout as the
#                                data folder beside experimentalserver.py on Windows)
#   /opt/happydungeons/venv      Python with flask and websockets
#   /opt/happydungeons/backups   a snapshot of the data taken before every data import
#   /opt/happydungeons/deploy    a copy of this kit, so it can be re-run on the server
#
# The emulator listens on 127.0.0.1:5000 (HTTP) and 127.0.0.1:8765 (realtime websocket) only. Caddy is the
# only public door: HTTPS on 443 with a free Let's Encrypt certificate, /connect goes to the websocket, the
# rest to the HTTP server. Port 80 stays open for the certificate check and the redirect to HTTPS.
set -euo pipefail

APP_USER=happydungeons
SERVICE=happydungeons
BASE=/opt/happydungeons
SERVER_DIR=$BASE/server
DATA_DIR=$BASE/data
VENV=$BASE/venv
BACKUP_DIR=$BASE/backups
KIT_DIR=$BASE/deploy
DOMAIN_FILE=$BASE/domain.txt
HTTP_PORT=5000
RTA_PORT=8765
UNIT_FILE=/etc/systemd/system/$SERVICE.service
CADDYFILE=/etc/caddy/Caddyfile
CADDY_MARK="# Managed by Happy Dungeons setup.sh"
CADDY_KEYRING=/usr/share/keyrings/caddy-stable-archive-keyring.gpg
CADDY_LIST=/etc/apt/sources.list.d/caddy-stable.list

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SRC_DIR=${SRC_DIR:-$(dirname "$SCRIPT_DIR")}
DOMAIN=${DOMAIN:-}
JOIN_CODE=${JOIN_CODE:-}
ACME_EMAIL=${ACME_EMAIL:-}
DATA_ARCHIVE=${DATA_ARCHIVE:-}
REPLACE_DATA=${REPLACE_DATA:-0}
FORCE_DATA=${FORCE_DATA:-0}
STAMP=$(date -u +%Y%m%d-%H%M%S)

say()  { printf '\n==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

valid_ipv4() {
	local octet
	[[ $1 =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]] || return 1
	for octet in "${BASH_REMATCH[@]:1}"; do
		if (( 10#$octet > 255 )); then return 1; fi
	done
	return 0
}

private_ipv4() {
	[[ $1 =~ ^(10\.|127\.|169\.254\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.|100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.) ]]
}

# The address the internet sees. Asks a few "what is my IP" services, then falls back to the address of the
# network card (only when that is a public one - behind cloud NAT it is not).
detect_public_ipv4() {
	local url ip
	for url in https://api.ipify.org https://ipv4.icanhazip.com https://checkip.amazonaws.com https://ifconfig.me/ip; do
		ip=$(curl -4 -fsS --max-time 8 "$url" 2>/dev/null | tr -d '[:space:]') || ip=
		if valid_ipv4 "$ip"; then
			printf '%s\n' "$ip"
			return 0
		fi
	done
	ip=$(ip -4 -o route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p') || ip=
	if valid_ipv4 "$ip" && ! private_ipv4 "$ip"; then
		printf '%s\n' "$ip"
		return 0
	fi
	return 1
}

export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a NEEDRESTART_SUSPEND=1

# If something fails after the data import stopped the service, start it again rather than leave it down.
service_stopped=0
restart_if_left_stopped() {
	local status=$?
	if [[ $status -ne 0 && $service_stopped == 1 ]]; then
		warn "setup did not finish; starting $SERVICE again with the data as it is now"
		systemctl start "$SERVICE" || true
	fi
}
trap restart_if_left_stopped EXIT

apt_get() { apt-get -o DPkg::Lock::Timeout=600 -y "$@"; }

apt_update() {
	local attempt
	for attempt in 1 2 3; do
		if apt_get update; then return 0; fi
		warn "apt-get update failed (attempt $attempt of 3), trying again in 15 seconds"
		sleep 15
	done
	die "apt-get update keeps failing - check the server's internet connection"
}

# ---------------------------------------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------------------------------------
[[ $EUID -eq 0 ]] || die "run this as root, for example: sudo bash $0"

os_id=$( . /etc/os-release 2>/dev/null && printf '%s' "${ID:-}" ) || os_id=
os_version=$( . /etc/os-release 2>/dev/null && printf '%s' "${VERSION_ID:-}" ) || os_version=
if [[ $os_id != ubuntu || $os_version != 24.04 ]]; then
	warn "this script is made for Ubuntu 24.04 (this is ${os_id:-an unknown system}${os_version:+ $os_version}); continuing anyway"
fi

DOMAIN=${DOMAIN,,}
if [[ -n $DOMAIN && $DOMAIN != auto ]] &&
	! [[ $DOMAIN =~ ^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$ ]]; then
	die "DOMAIN '$DOMAIN' is not a valid host name (example: games.example.com)"
fi
if [[ -n $JOIN_CODE && ${JOIN_CODE,,} != none ]] && ! [[ $JOIN_CODE =~ ^[A-Za-z0-9._~-]{4,64}$ ]]; then
	die "JOIN_CODE must be 4-64 letters, digits, dots, dashes, underscores or tildes (no spaces)"
fi
if [[ -n $ACME_EMAIL ]] && ! [[ $ACME_EMAIL =~ ^[^@[:space:]]+@[^@[:space:]]+\.[A-Za-z]{2,}$ ]]; then
	die "ACME_EMAIL '$ACME_EMAIL' does not look like an e-mail address"
fi
if [[ $REPLACE_DATA == 1 && -z $DATA_ARCHIVE ]]; then
	die "REPLACE_DATA=1 needs DATA_ARCHIVE (the data to replace the server's data with)"
fi
if [[ -n $DATA_ARCHIVE && ! -f $DATA_ARCHIVE ]]; then
	die "DATA_ARCHIVE $DATA_ARCHIVE does not exist"
fi

# ---------------------------------------------------------------------------------------------------------
# System packages and Caddy
# ---------------------------------------------------------------------------------------------------------
if command -v cloud-init >/dev/null 2>&1; then
	say "Waiting for the VPS's first-boot setup (cloud-init) to finish, if it still runs"
	timeout 600 cloud-init status --wait >/dev/null 2>&1 || true
fi

say "Installing system packages"
apt_update
# Caddy's install guide also lists debian-keyring, debian-archive-keyring and apt-transport-https. Ubuntu 24.04
# does not need them: apt speaks https itself and the repository key is installed as its own keyring below.
apt_get install python3 python3-venv ufw curl ca-certificates gnupg

if ! command -v caddy >/dev/null 2>&1 || [[ ! -s $CADDY_LIST || ! -s $CADDY_KEYRING ]]; then
	say "Adding Caddy's official apt repository"
	key_tmp=$(mktemp)
	curl -fsSL --retry 3 https://dl.cloudsmith.io/public/caddy/stable/gpg.key -o "$key_tmp"
	gpg --batch --yes --dearmor -o "$CADDY_KEYRING" "$key_tmp"
	rm -f "$key_tmp"
	curl -fsSL --retry 3 https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt -o "$CADDY_LIST"
	chmod o+r "$CADDY_KEYRING" "$CADDY_LIST"
	apt_update
fi
say "Installing Caddy"
apt_get install caddy
caddy version

# ---------------------------------------------------------------------------------------------------------
# Service user, folders, code
# ---------------------------------------------------------------------------------------------------------
say "Creating the service user '$APP_USER' and $BASE"
if ! id -u "$APP_USER" >/dev/null 2>&1; then
	useradd --system --user-group --home-dir "$BASE" --no-create-home --shell /usr/sbin/nologin "$APP_USER"
fi
install -d -o root -g root -m 0755 "$BASE"
install -d -o "$APP_USER" -g "$APP_USER" -m 0755 "$SERVER_DIR"
install -d -o "$APP_USER" -g "$APP_USER" -m 0700 "$DATA_DIR"
install -d -o root -g root -m 0700 "$BACKUP_DIR"

say "Installing the server code into $SERVER_DIR"
src_real=$(realpath -m "$SRC_DIR")
if [[ -f $SRC_DIR/experimentalserver.py && $src_real != "$(realpath -m "$SERVER_DIR")" ]]; then
	install -o "$APP_USER" -g "$APP_USER" -m 0644 "$SRC_DIR/experimentalserver.py" "$SERVER_DIR/experimentalserver.py"
	if [[ -f $SRC_DIR/requirements.txt ]]; then
		install -o "$APP_USER" -g "$APP_USER" -m 0644 "$SRC_DIR/requirements.txt" "$SERVER_DIR/requirements.txt"
	fi
	if [[ -d $SRC_DIR/web ]]; then
		install -d -o "$APP_USER" -g "$APP_USER" -m 0755 "$SERVER_DIR/web"
		cp -R "$SRC_DIR/web/." "$SERVER_DIR/web/"
		chown -R "$APP_USER:$APP_USER" "$SERVER_DIR/web"
	fi
	# The Discord application id that ships with the package, unless the server already has one.
	if [[ -f $SRC_DIR/discord_app_id.txt && ! -e $DATA_DIR/discord_app_id.txt ]]; then
		install -o "$APP_USER" -g "$APP_USER" -m 0644 "$SRC_DIR/discord_app_id.txt" "$DATA_DIR/discord_app_id.txt"
	fi
	echo "Installed experimentalserver.py ($(wc -c <"$SERVER_DIR/experimentalserver.py") bytes) from $SRC_DIR"
elif [[ -f $SERVER_DIR/experimentalserver.py ]]; then
	echo "No new code in $SRC_DIR - keeping the installed server."
else
	die "experimentalserver.py was not found in $SRC_DIR (put it, requirements.txt and web/ in the folder above deploy/)"
fi
if [[ ! -f $SERVER_DIR/requirements.txt ]]; then
	printf 'flask\nwebsockets\n' >"$SERVER_DIR/requirements.txt"
	chown "$APP_USER:$APP_USER" "$SERVER_DIR/requirements.txt"
fi

# Keep a copy of this kit on the server so "sudo bash /opt/happydungeons/deploy/setup.sh" works later.
if [[ $SCRIPT_DIR != "$KIT_DIR" ]]; then
	install -d -o root -g root -m 0755 "$KIT_DIR"
	cp -R "$SCRIPT_DIR/." "$KIT_DIR/"
	sed -i 's/\r$//' "$KIT_DIR"/*.sh
	chmod 0755 "$KIT_DIR"/*.sh
fi

# The server keeps its data in "data" and reads discord_app_id.txt next to experimentalserver.py.
if [[ -e $SERVER_DIR/data && ! -L $SERVER_DIR/data ]]; then
	die "$SERVER_DIR/data is a real folder; move what is in it to $DATA_DIR, delete it and run this again"
fi
ln -sfn "$DATA_DIR" "$SERVER_DIR/data"
chown -h "$APP_USER:$APP_USER" "$SERVER_DIR/data"
if [[ -f $SERVER_DIR/discord_app_id.txt && ! -L $SERVER_DIR/discord_app_id.txt ]]; then
	if [[ -e $DATA_DIR/discord_app_id.txt ]]; then
		mv "$SERVER_DIR/discord_app_id.txt" "$BACKUP_DIR/discord_app_id.txt.$STAMP"
	else
		mv "$SERVER_DIR/discord_app_id.txt" "$DATA_DIR/discord_app_id.txt"
	fi
fi
ln -sfn "$DATA_DIR/discord_app_id.txt" "$SERVER_DIR/discord_app_id.txt"
chown -h "$APP_USER:$APP_USER" "$SERVER_DIR/discord_app_id.txt"

# ---------------------------------------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------------------------------------
say "Setting up Python in $VENV"
if [[ ! -x $VENV/bin/python ]]; then
	python3 -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --disable-pip-version-check --no-input --quiet -r "$SERVER_DIR/requirements.txt"
if ! "$VENV/bin/python" -c 'import flask; from websockets.legacy.server import serve' 2>/dev/null; then
	die "flask/websockets are not usable in $VENV (the server needs websockets.legacy.server) - see pip's output above"
fi
"$VENV/bin/python" -c 'import importlib.metadata as m, sys
print("Python %s, flask %s, websockets %s" % (sys.version.split()[0], m.version("flask"), m.version("websockets")))'
if ! "$VENV/bin/python" -c 'import ast, sys; ast.parse(open(sys.argv[1], encoding="utf-8").read())' \
	"$SERVER_DIR/experimentalserver.py"; then
	die "experimentalserver.py does not parse with this server's Python"
fi

# ---------------------------------------------------------------------------------------------------------
# server_config.json: loopback only (Caddy is the public door), join code kept unless JOIN_CODE says otherwise
# ---------------------------------------------------------------------------------------------------------
say "Writing $SERVER_DIR/server_config.json"
CONFIG_FILE=$SERVER_DIR/server_config.json JOIN_CODE=$JOIN_CODE HTTP_PORT=$HTTP_PORT RTA_PORT=$RTA_PORT \
	python3 - <<'PY'
import json
import os

path = os.environ["CONFIG_FILE"]
join_code = os.environ.get("JOIN_CODE", "")
try:
    with open(path, encoding="utf-8") as handle:
        config = json.load(handle)
    if not isinstance(config, dict):
        config = {}
except (OSError, ValueError):
    config = {}
config.update(bind="127.0.0.1", httpPort=int(os.environ["HTTP_PORT"]), rtaPort=int(os.environ["RTA_PORT"]))
if join_code.lower() == "none":
    config.pop("joinCode", None)
elif join_code:
    config["joinCode"] = join_code
if not config.get("joinCode"):
    config.pop("joinCode", None)
temp = path + ".tmp"
with open(temp, "w", encoding="utf-8") as handle:
    json.dump(config, handle, indent=2)
    handle.write("\n")
os.replace(temp, path)
print("bind 127.0.0.1, HTTP %d, realtime %d, join code %s" % (
    config["httpPort"], config["rtaPort"], "set" if config.get("joinCode") else "NOT set"))
PY
chown "$APP_USER:$APP_USER" "$SERVER_DIR/server_config.json"
chmod 0600 "$SERVER_DIR/server_config.json"

# ---------------------------------------------------------------------------------------------------------
# systemd service
# ---------------------------------------------------------------------------------------------------------
say "Installing the systemd service '$SERVICE'"
unit_tmp=$(mktemp)
cat >"$unit_tmp" <<EOF
[Unit]
Description=Happy Dungeons Xbox Live emulator for WinDurango
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$SERVER_DIR
ExecStart=$VENV/bin/python -u experimentalserver.py
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=3
# Ctrl+C: the server prints "Stopping." and exits cleanly.
KillSignal=SIGINT
TimeoutStopSec=20
StandardOutput=journal
StandardError=journal
SyslogIdentifier=$SERVICE
# Files it creates (saves, the player registry with its secrets) are readable by the service user only.
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true

[Install]
WantedBy=multi-user.target
EOF
install -o root -g root -m 0644 "$unit_tmp" "$UNIT_FILE"
rm -f "$unit_tmp"
systemctl daemon-reload
systemctl enable --quiet "$SERVICE"

# ---------------------------------------------------------------------------------------------------------
# Data import (deploy.ps1 -WithData): with the service stopped, snapshot, then merge (or replace)
# ---------------------------------------------------------------------------------------------------------
if [[ -n $DATA_ARCHIVE ]]; then
	say "Importing player data from $(basename "$DATA_ARCHIVE")"
	listing=$(tar -tzf "$DATA_ARCHIVE") || die "the data archive $DATA_ARCHIVE is damaged"
	if grep -Eq '(^/|(^|/)\.\.(/|$))' <<<"$listing"; then
		die "the data archive contains absolute or '..' paths - not importing it"
	fi
	# A merge overwrites same-name files. Once people have played here, the server's _players.json (and their
	# saves) are newer than the PC's copy, and merging that copy would roll them back - stop instead.
	archived_players=$(grep -E '^(\./)?_players\.json$' <<<"$listing" | head -n 1) || archived_players=
	if [[ $REPLACE_DATA != 1 && $FORCE_DATA != 1 && -n $archived_players && -f $DATA_DIR/_players.json ]]; then
		peek=$(mktemp -d)
		tar -xzf "$DATA_ARCHIVE" -C "$peek" --no-same-owner --warning=no-unknown-keyword "$archived_players"
		if [[ $DATA_DIR/_players.json -nt $peek/$archived_players ]]; then
			rm -rf "$peek"
			die "the server's player list is newer than the uploaded one: people have played on this server since that
    copy was made, and importing it would overwrite their players and saves. Nothing was changed.
    To update the server's code, run deploy.ps1 again WITHOUT -WithData. To import anyway, add -ForceData."
		fi
		rm -rf "$peek"
	fi
	systemctl stop "$SERVICE" 2>/dev/null || true
	service_stopped=1
	snapshot=$BACKUP_DIR/data-$STAMP.tar.gz
	tar -czf "$snapshot" -C "$BASE" data
	chmod 0600 "$snapshot"
	echo "Snapshot of the data that was on the server before this import: $snapshot"
	if [[ $REPLACE_DATA == 1 ]]; then
		mv "$DATA_DIR" "$BACKUP_DIR/data-replaced-$STAMP"
		install -d -o "$APP_USER" -g "$APP_USER" -m 0700 "$DATA_DIR"
		echo "REPLACE: the previous data folder was moved to $BACKUP_DIR/data-replaced-$STAMP"
	else
		echo "MERGE: uploaded files overwrite files with the same name; nothing else is removed"
	fi
	tar -xzf "$DATA_ARCHIVE" -C "$DATA_DIR" --no-same-owner --no-same-permissions \
		--warning=no-unknown-keyword --warning=no-timestamp
	chown -R "$APP_USER:$APP_USER" "$DATA_DIR"
	chmod -R u+rwX,go-rwx "$DATA_DIR"
	echo "Imported $(grep -cv '/$' <<<"$listing") files; $DATA_DIR now holds $(find "$DATA_DIR" -type f | wc -l) files"
fi

# ---------------------------------------------------------------------------------------------------------
# Domain and Caddy
# ---------------------------------------------------------------------------------------------------------
say "Choosing the public address"
if [[ $DOMAIN == auto ]]; then
	rm -f "$DOMAIN_FILE"
	DOMAIN=
fi
if [[ -z $DOMAIN && -s $DOMAIN_FILE ]]; then
	DOMAIN=$(tr -d '[:space:]' <"$DOMAIN_FILE")
	echo "Using the domain given last time: $DOMAIN (DOMAIN=auto switches back to sslip.io)"
fi
public_ip=$(detect_public_ipv4) || public_ip=
if [[ -n $DOMAIN ]]; then
	printf '%s\n' "$DOMAIN" >"$DOMAIN_FILE"
	resolved=$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk 'NR == 1 { print $1 }') || resolved=
	if [[ -z $resolved ]]; then
		warn "$DOMAIN does not resolve yet - point its DNS A record at ${public_ip:-this server} or the certificate will fail"
	elif [[ -n $public_ip && $resolved != "$public_ip" ]]; then
		warn "$DOMAIN points at $resolved but this server is $public_ip - the certificate will fail until DNS is fixed"
	fi
else
	[[ -n $public_ip ]] || die "could not find this server's public IPv4 address; run again with DOMAIN=your.domain"
	DOMAIN="${public_ip//./-}.sslip.io"
fi
echo "Public address: $DOMAIN${public_ip:+ (server IP $public_ip)}"

say "Configuring Caddy (HTTPS for $DOMAIN)"
if [[ -f $CADDYFILE ]] && ! grep -qF "$CADDY_MARK" "$CADDYFILE"; then
	cp -p "$CADDYFILE" "$CADDYFILE.before-happydungeons-$STAMP"
	echo "The previous Caddyfile was kept as $CADDYFILE.before-happydungeons-$STAMP"
fi
caddy_tmp=$(mktemp)
{
	printf '%s - running setup.sh again rewrites this file.\n' "$CADDY_MARK"
	if [[ -n $ACME_EMAIL ]]; then
		printf '{\n\temail %s\n}\n\n' "$ACME_EMAIL"
	fi
	cat <<EOF
$DOMAIN {
	# Realtime activity websocket (the game's wss://rta.xboxlive.com/connect).
	@rta path /connect /connect/*
	handle @rta {
		reverse_proxy 127.0.0.1:$RTA_PORT {
			# a config reload keeps running game websockets open for up to 10 minutes
			stream_close_delay 10m
		}
	}

	# Everything else goes to the Xbox Live HTTP emulator. reverse_proxy sets X-Forwarded-For,
	# X-Forwarded-Proto and X-Forwarded-Host itself (and replaces any the client sent); the game's
	# X-Original-Host and Authorization headers pass through unchanged.
	handle {
		reverse_proxy 127.0.0.1:$HTTP_PORT
	}
}
EOF
} >"$caddy_tmp"
if ! validate_output=$(caddy validate --adapter caddyfile --config "$caddy_tmp" 2>&1); then
	printf '%s\n' "$validate_output" >&2
	die "Caddy rejected the generated configuration (kept in $caddy_tmp); the old Caddyfile is untouched"
fi
install -o root -g root -m 0644 "$caddy_tmp" "$CADDYFILE"
rm -f "$caddy_tmp"
systemctl enable --quiet caddy
systemctl reload-or-restart caddy

# ---------------------------------------------------------------------------------------------------------
# Firewall
# ---------------------------------------------------------------------------------------------------------
# Oracle Cloud's Ubuntu images come with their own iptables rules (saved by netfilter-persistent) whose INPUT
# chain ends in a REJECT. ufw would add its rules after that REJECT, where they never match, so there open 80 and
# 443 in those rules instead and leave ufw off.
if [[ -f /etc/iptables/rules.v4 ]] && iptables -S INPUT 2>/dev/null | grep -q -- '-j REJECT'; then
	say "Firewall: this image manages iptables itself (Oracle Cloud style) - opening 80 and 443 there"
	for tables in iptables ip6tables; do
		if "$tables" -S INPUT 2>/dev/null | grep -q -- '-j REJECT' &&
			! "$tables" -C INPUT -p tcp -m multiport --dports 80,443 -m state --state NEW -j ACCEPT 2>/dev/null; then
			"$tables" -I INPUT 1 -p tcp -m multiport --dports 80,443 -m state --state NEW -j ACCEPT
		fi
	done
	netfilter-persistent save >/dev/null 2>&1 || echo "WARNING: could not save the iptables rules; they may be lost on reboot"
	echo "Also open TCP 80 and 443 in the cloud provider's own firewall (Oracle: the VCN security list)."
else
say "Firewall: allowing SSH, 80 and 443, then turning ufw on"
if ! ufw allow OpenSSH >/dev/null 2>&1; then
	ufw allow 22/tcp >/dev/null
fi
# Never lock ourselves out: also allow the port this SSH session uses and any port sshd is set to.
ssh_connection=${SSH_CONNECTION:-}
ssh_ports=$( { printf '%s\n' "${ssh_connection##* }"; sshd -T 2>/dev/null | awk '$1 == "port" { print $2 }'; } |
	grep -E '^[0-9]+$' | sort -u ) || ssh_ports=
for port in $ssh_ports; do
	if [[ $port != 22 ]]; then
		ufw allow "$port/tcp" >/dev/null
	fi
done
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null
ufw status
fi

# ---------------------------------------------------------------------------------------------------------
# Start and check
# ---------------------------------------------------------------------------------------------------------
say "Starting the $SERVICE service"
systemctl restart "$SERVICE"
service_stopped=0

say "Health check"
local_ok=0
for _ in $(seq 1 30); do
	if curl -fsS --max-time 3 "http://127.0.0.1:$HTTP_PORT/windurango/players" >/dev/null 2>&1; then
		local_ok=1
		break
	fi
	sleep 1
done
if [[ $local_ok == 1 ]]; then
	echo "OK    the emulator answers on 127.0.0.1:$HTTP_PORT"
else
	warn "the emulator does not answer on 127.0.0.1:$HTTP_PORT. Its last log lines:"
	journalctl -u "$SERVICE" -n 40 --no-pager || true
fi

echo "Checking https://$DOMAIN/windurango/players (the first certificate can take a minute)..."
public_ok=0
players_json=
for _ in $(seq 1 24); do
	if players_json=$(curl -fsS --max-time 10 "https://$DOMAIN/windurango/players" 2>/dev/null); then
		public_ok=1
		break
	fi
	sleep 5
done
if [[ $public_ok == 1 ]]; then
	summary=$(python3 -c 'import json, sys
d = json.load(sys.stdin)
print("%d registered players, %d online" % (len(d.get("players", [])), d.get("online", 0)))' <<<"$players_json" 2>/dev/null) ||
		summary="answered"
	echo "OK    https://$DOMAIN/windurango/players: $summary"
	ws_code=$(curl -sS -o /dev/null -w '%{http_code}' --http1.1 --max-time 4 \
		-H 'Connection: Upgrade' -H 'Upgrade: websocket' -H 'Sec-WebSocket-Version: 13' \
		-H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' "https://$DOMAIN/connect" 2>/dev/null) || true
	if [[ $ws_code == 101 ]]; then
		echo "OK    wss://$DOMAIN/connect accepts websockets"
	else
		warn "wss://$DOMAIN/connect answered HTTP ${ws_code:-nothing} instead of 101 (websocket upgrade)"
	fi
else
	warn "https://$DOMAIN is not reachable from here yet. Usual causes: the VPS provider's own firewall blocks"
	warn "ports 80/443, DNS for $DOMAIN is wrong, or the certificate is still being issued."
	warn "Certificate log: journalctl -u caddy -n 50 --no-pager"
fi

current_join=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1])).get("joinCode") or "")' \
	"$SERVER_DIR/server_config.json" 2>/dev/null) || current_join=

echo
echo "=========================================================================================="
echo " Happy Dungeons server"
echo "   Address for players : $DOMAIN   (HTTPS, port 443)"
echo "   Players list        : https://$DOMAIN/windurango/players"
if [[ -n $current_join ]]; then
	echo "   Join code           : $current_join   (new players need it once, to register)"
else
	echo "   Join code           : none - ANYONE who finds this address can register."
	echo "                         Set one with JOIN_CODE=... (deploy.ps1 -JoinCode ...)."
fi
echo "   Server log          : journalctl -u $SERVICE -f"
echo "   Data                : $DATA_DIR   (snapshots in $BACKUP_DIR)"
echo "=========================================================================================="
echo "PUBLIC_URL=https://$DOMAIN"

if [[ $local_ok != 1 ]]; then
	exit 1
fi
if [[ $public_ok != 1 ]]; then
	exit 2
fi
exit 0
