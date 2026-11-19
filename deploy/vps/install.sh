#!/usr/bin/env bash
# Happy Dungeons PS4 build on the Happy Dungeons VPS: shadNet (PSN stand-in) + the PS4 gateway.
# Run as root from the unpacked bundle folder:   bash install.sh <step> [--dry-run]
#
#   check     read-only report (changes nothing)
#   fetch     download the shadNet linux build (sha256-pinned) and the Qt 6.10.3 runtime it needs
#             (aqtinstall in its own venv); everything goes under /opt/happydungeons/ps4
#   setup     user hdps4, folders, gateway + configs + pictures, random secrets, systemd units (not started)
#   start     start shadNet, create the gateway's shadNet admin account, start the gateway, local checks
#   expose    Caddy: PS4 blocks into /etc/caddy/Caddyfile (backup, validate, reload);
#             ufw: 31313/tcp (gateway), 31314/udp (shadNet STUN), 31315/tcp (shadNet WebAPI)
#   website   the PS4 box on the players page (/windurango/): /opt/happydungeons/server/web/index.html, with a
#             backup next to it (players_page_ps4.py; static file, no restart). website-remove takes it out.
#   status    services, ports, gateway status
#   rollback  stop + disable the two services, take the Caddy blocks out, delete the three ufw rules
#             and the players page box (files, accounts and links stay in /opt/happydungeons/ps4)
#
# Every step can be run again and only changes what is not in place yet. --dry-run prints what it would do.
# Never touched: the Dungeons server (/opt/happydungeons/server, data, venv, happydungeons.service; the one
# exception is the "website" step's box on the players page web/index.html) and
# kytyNET (8763/8764). Saves reach the Dungeons data only through its normal HTTP save route.
#
# NOTE: /opt/happydungeons/deploy/setup.sh rewrites the whole Caddyfile. After running it again, run
#       "bash /opt/happydungeons/ps4/install.sh expose" again to put the PS4 blocks back.
set -euo pipefail

STEP=${1:-}
DRY=0
[[ ${2:-} == --dry-run ]] && DRY=1
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
P=/opt/happydungeons/ps4
STAMP=$(date +%Y%m%d-%H%M%S)
SHADNET_ZIP=shadnet-linux64-2026-11-16-a53dad5.zip
SHADNET_URL=https://github.com/shadps4-emu/shadNet/releases/download/pre-release-shadnet-2026-11-16-a53dad52ae04aba321e62bcff8dc3743ba3e4814/$SHADNET_ZIP
SHADNET_SHA256=a3bb6ea3a364d3a2d6f6980e684c9daa77b64b727e42b187c59653e8b562d899
QT_VERSION=6.10.3
QT_ARCH=linux_gcc_64
CADDYFILE=/etc/caddy/Caddyfile
CADDY_ANCHOR=$'\t# Everything else goes to the Xbox Live HTTP emulator.'
MARK_BEGIN="# >>> happydungeons-ps4"
MARK_END="# <<< happydungeons-ps4"
UFW_PORTS=("31313/tcp" "31314/udp" "31315/tcp")
UNITS=(hd-ps4-shadnet hd-ps4-gateway)

say() { printf '\n== %s\n' "$*"; }
die() {
	printf 'ERROR: %s\n' "$*" >&2
	exit 1
}
run() {
	if ((DRY)); then printf '[dry-run] %s\n' "$*"; else "$@"; fi
}
listening() { ss -Hlntu | awk '{print $5}' | grep -qE "[:.]$1\$"; }
wait_port() {
	local i
	for i in $(seq 1 30); do
		listening "$1" && return 0
		sleep 1
	done
	return 1
}
[[ $EUID -eq 0 ]] || die "run as root"

# ------------------------------------------------------------------------------------------------- check
do_check() {
	say "System"
	lsb_release -ds 2>/dev/null || head -1 /etc/os-release
	python3 --version
	df -h / | tail -1
	say "Dungeons server (not touched)"
	printf 'happydungeons.service: %s\n' "$(systemctl is-active happydungeons 2>/dev/null || true)"
	md5sum /opt/happydungeons/server/experimentalserver.py
	say "PS4 side"
	local f
	for f in "$P/shadnet/shadnet" "$P/qt/lib/libQt6Core.so.6" "$P/gateway/hd_ps4_gateway.py" "$P/gateway/gateway.json" \
		"$P/shadnet/shadnet.cfg"; do
		if [[ -e $f ]]; then echo "present: $f"; else echo "missing: $f"; fi
	done
	if id hdps4 >/dev/null 2>&1; then echo "user hdps4: present"; else echo "user hdps4: missing"; fi
	local u
	for u in "${UNITS[@]}"; do printf '%s: %s\n' "$u" "$(systemctl is-active "$u" 2>/dev/null || true)"; done
	say "Ports"
	ss -Hlntup | grep -E ':(31313|31314|31315|31350|31413|5101) ' || echo "no PS4 port is listening"
	say "Caddy"
	if grep -qF "$MARK_BEGIN" "$CADDYFILE"; then echo "PS4 blocks: present"; else echo "PS4 blocks: absent"; fi
	if grep -qF "$CADDY_ANCHOR" "$CADDYFILE"; then echo "anchor for the /ps4 route: found"; else echo "anchor for the /ps4 route: NOT FOUND"; fi
	say "ufw"
	ufw status | grep -E '^3131[345]/' || echo "no PS4 rules"
}

# ------------------------------------------------------------------------------------------------- fetch
do_fetch() {
	run install -d -m 0755 "$P" "$P/download"
	local zip=$P/download/$SHADNET_ZIP
	if [[ -f $zip ]] && echo "$SHADNET_SHA256  $zip" | sha256sum -c --quiet - >/dev/null 2>&1; then
		echo "shadNet zip already here (checksum ok)"
	else
		run curl -fL --retry 3 -o "$zip.part" "$SHADNET_URL"
		if ((!DRY)); then
			echo "$SHADNET_SHA256  $zip.part" | sha256sum -c - || die "shadNet zip checksum mismatch (kept as $zip.part)"
			mv "$zip.part" "$zip"
		fi
	fi
	if [[ -x $P/shadnet/shadnet ]]; then
		echo "shadNet binary already installed"
	else
		run install -d -m 0755 "$P/shadnet"
		run rm -rf "$P/shadnet-unpack"
		run python3 -m zipfile -e "$zip" "$P/shadnet-unpack"
		if ((!DRY)); then
			local bin tgz
			# the release zip holds shadnet-linux64.tar.gz, which holds the binary
			for tgz in "$P"/shadnet-unpack/*.tar.gz; do
				[[ -f $tgz ]] && tar -xzf "$tgz" -C "$P/shadnet-unpack"
			done
			bin=$(find "$P/shadnet-unpack" -type f -name shadnet | head -1)
			[[ -n $bin ]] || die "no shadnet binary in $zip"
			install -m 0755 "$bin" "$P/shadnet/shadnet"
			rm -rf "$P/shadnet-unpack"
		fi
	fi
	if [[ -e $P/qt/lib/libQt6Core.so.6 ]]; then
		echo "Qt $QT_VERSION runtime already installed"
	else
		if [[ ! -x $P/aqt-venv/bin/aqt ]]; then
			run python3 -m venv "$P/aqt-venv"
			run "$P/aqt-venv/bin/pip" install --disable-pip-version-check -q aqtinstall
		fi
		run "$P/aqt-venv/bin/aqt" install-qt linux desktop "$QT_VERSION" "$QT_ARCH" -m qthttpserver qtwebsockets -O "$P/qt-kit" ||
			die "aqt failed; module names: $P/aqt-venv/bin/aqt list-qt linux desktop --modules $QT_VERSION $QT_ARCH"
		if ((!DRY)); then
			local kit
			kit=$(find "$P/qt-kit/$QT_VERSION" -mindepth 1 -maxdepth 1 -type d | head -1)
			[[ -e $kit/lib/libQt6Core.so.6 ]] || die "no Qt libraries under $P/qt-kit/$QT_VERSION"
			ln -sfn "$kit" "$P/qt"
		fi
	fi
	if ((!DRY)); then
		local missing
		missing=$(LD_LIBRARY_PATH=$P/qt/lib ldd "$P/shadnet/shadnet" | grep 'not found' || true)
		[[ -z $missing ]] || die "shadnet still misses libraries: $missing"
		echo "ldd: every library of shadnet resolves"
		[[ -e $P/qt/plugins/sqldrivers/libqsqlite.so ]] || die "Qt SQLite driver missing ($P/qt/plugins/sqldrivers)"
	fi
}

# ------------------------------------------------------------------------------------------------- setup
secret() { python3 -c 'import secrets; print(secrets.token_urlsafe(24))'; }

# write $2 (content) to $1 only when different; back up the old file first
put_file() {
	local dest=$1 content=$2 mode=$3 owner=$4
	if [[ -f $dest ]] && [[ "$(cat "$dest")" == "$content" ]]; then
		echo "unchanged: $dest"
		return
	fi
	if ((DRY)); then
		echo "[dry-run] write $dest"
		return
	fi
	[[ -f $dest ]] && cp -p "$dest" "$dest.before-$STAMP"
	printf '%s\n' "$content" >"$dest.new"
	chmod "$mode" "$dest.new"
	chown "$owner" "$dest.new"
	mv "$dest.new" "$dest"
	echo "written: $dest"
}

do_setup() {
	[[ -x $P/shadnet/shadnet ]] || ((DRY)) || die "run 'fetch' first"
	id hdps4 >/dev/null 2>&1 || run useradd --system --home-dir "$P" --no-create-home --shell /usr/sbin/nologin hdps4
	run install -d -m 0755 "$P/gateway" "$P/base_serverdata" "$P/overrides"
	run install -d -m 0750 -o hdps4 -g hdps4 "$P/state"
	run chown hdps4:hdps4 "$P/shadnet" # shadNet keeps db/, logs and worlds.cfg.running next to itself
	run install -m 0644 "$HERE/hd_ps4_gateway.py" "$P/gateway/hd_ps4_gateway.py"
	if [[ $HERE != "$P" ]]; then # keep this script + its Caddy blocks for later runs (expose after setup.sh)
		run install -m 0755 "$HERE/install.sh" "$P/install.sh"
		run install -m 0644 "$HERE/Caddyfile.ps4-site" "$HERE/Caddyfile.ps4-route" "$P/"
	fi
	run install -m 0644 -o hdps4 -g hdps4 "$HERE/worlds.cfg" "$P/shadnet/worlds.cfg"
	run install -m 0644 -o hdps4 -g hdps4 "$HERE/clientversions.cfg" "$P/shadnet/clientversions.cfg"
	run cp -f "$HERE"/base_serverdata/* "$P/base_serverdata/"
	run cp -f "$HERE"/overrides/* "$P/overrides/"

	# secrets: reuse what an earlier run made (gateway.json), else new random ones
	local key="" admin=""
	if [[ -f $P/gateway/gateway.json ]]; then
		key=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["shadnetRegistrationKey"])' "$P/gateway/gateway.json")
		admin=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["shadnetAdminPassword"])' "$P/gateway/gateway.json")
	fi
	[[ -n $key && $key != __REGISTRATION_KEY__ ]] || key=$(secret)
	[[ -n $admin && $admin != __ADMIN_PASSWORD__ ]] || admin=$(secret)

	local cfg gw
	cfg=$(sed "s|__REGISTRATION_KEY__|$key|" "$HERE/shadnet.cfg")
	gw=$(sed -e "s|__REGISTRATION_KEY__|$key|" -e "s|__ADMIN_PASSWORD__|$admin|" "$HERE/gateway.json")
	# shadNet adds its default keys to shadnet.cfg when it starts, so only write it the first time
	# (or when the template's own lines are missing from it).
	if [[ -f $P/shadnet/shadnet.cfg ]] && grep -qxF "RegistrationSecretKey=$key" "$P/shadnet/shadnet.cfg" &&
		grep -qxF "UnsecuredPort=31413" "$P/shadnet/shadnet.cfg" && grep -qxF "MatchingUdpPort=31314" "$P/shadnet/shadnet.cfg"; then
		echo "unchanged: $P/shadnet/shadnet.cfg"
	else
		put_file "$P/shadnet/shadnet.cfg" "$cfg" 0600 hdps4:hdps4
	fi
	put_file "$P/gateway/gateway.json" "$gw" 0640 root:hdps4

	local u
	for u in "${UNITS[@]}"; do
		if cmp -s "$HERE/$u.service" "/etc/systemd/system/$u.service"; then
			echo "unchanged: /etc/systemd/system/$u.service"
		else
			run install -m 0644 "$HERE/$u.service" "/etc/systemd/system/$u.service"
		fi
	done
	run systemctl daemon-reload
	run systemctl enable "${UNITS[@]}"
	echo "Set up. Services are enabled but not started: run 'start'."
}

# ------------------------------------------------------------------------------------------------- start
do_start() {
	[[ -f $P/gateway/gateway.json ]] || ((DRY)) || die "run 'setup' first"
	run systemctl start hd-ps4-shadnet
	if ((!DRY)); then
		wait_port 31413 || die "shadNet did not open 31413 (journalctl -u hd-ps4-shadnet)"
		echo "shadNet is up"
	fi
	# the gateway's admin account (friends sync); 0 = created now, 11 = existed already
	local out=""
	if ((DRY)); then
		echo "[dry-run] create the shadNet admin account (hd_ps4_gateway.py --create-admin)"
	else
		out=$(runuser -u hdps4 -- python3 "$P/gateway/hd_ps4_gateway.py" "$P/gateway/gateway.json" --create-admin) ||
			die "could not create the admin account: $out"
		echo "$out"
		if [[ $out == *": 0" ]]; then
			# shadNet reads AdminsList against existing accounts at start; nobody is connected yet
			systemctl restart hd-ps4-shadnet
			wait_port 31413 || die "shadNet did not come back"
		fi
	fi
	run systemctl restart hd-ps4-gateway
	if ((!DRY)); then
		wait_port 5101 || die "the gateway did not open 5101 (journalctl -u hd-ps4-gateway)"
		wait_port 31313 || die "the gateway did not open 31313"
		curl -fsS http://127.0.0.1:5101/ps4/status
		echo
		local n
		n=$(curl -fsS 'http://127.0.0.1:5101/v12/api' | python3 -c 'import json,sys; d=json.load(sys.stdin); print(len(d.get("File", {})), "files, list", d["List"]["Name"])')
		echo "Vulcan list: $n"
	fi
}

# ------------------------------------------------------------------------------------------------- expose
caddy_without() {
	# prints the Caddyfile without the PS4 blocks
	python3 - "$CADDYFILE" "$MARK_BEGIN" "$MARK_END" <<'EOF'
import re, sys
path, begin, end = sys.argv[1:4]
text = open(path, encoding="utf-8").read()
text = re.sub(r"[ \t]*%s[^\n]*\n.*?[ \t]*%s[^\n]*\n\n?" % (re.escape(begin), re.escape(end)), "", text, flags=re.S)
sys.stdout.write(text.rstrip("\n") + "\n")
EOF
}

caddy_new() {
	# prints the Caddyfile with the PS4 blocks added (from the current file without any old PS4 blocks)
	python3 - "$CADDYFILE" "$HERE/Caddyfile.ps4-site" "$HERE/Caddyfile.ps4-route" "$MARK_BEGIN" "$MARK_END" "$CADDY_ANCHOR" <<'EOF'
import re, sys
path, site, route, begin, end, anchor = sys.argv[1:7]
text = open(path, encoding="utf-8").read()
text = re.sub(r"[ \t]*%s[^\n]*\n.*?[ \t]*%s[^\n]*\n\n?" % (re.escape(begin), re.escape(end)), "", text, flags=re.S)
if text.count(anchor) != 1:
    sys.exit("anchor found %d times (want exactly 1): %r" % (text.count(anchor), anchor))
route_block = "\t%s (PS4 account linking)\n%s\t%s\n\n" % (begin, open(route, encoding="utf-8").read(), end)
text = text.replace(anchor, route_block + anchor, 1)
site_block = "\n%s (PS4 build: Vulcan delivery, telemetry; added by /opt/happydungeons/ps4/install.sh)\n%s%s\n" % (
    begin, open(site, encoding="utf-8").read(), end)
sys.stdout.write(text.rstrip("\n") + "\n" + site_block)
EOF
}

do_expose() {
	listening 5101 || ((DRY)) || die "the gateway is not running: run 'start' first"
	local tmp
	tmp=$(mktemp)
	caddy_new >"$tmp" || die "could not build the new Caddyfile"
	if cmp -s "$tmp" "$CADDYFILE"; then
		echo "Caddyfile: PS4 blocks already in place"
	else
		diff -u "$CADDYFILE" "$tmp" || true
		caddy validate --adapter caddyfile --config "$tmp" >/dev/null 2>&1 ||
			die "Caddy rejects the new file (kept in $tmp); the Caddyfile is untouched"
		run cp -p "$CADDYFILE" "$CADDYFILE.before-ps4-$STAMP"
		run install -o root -g root -m 0644 "$tmp" "$CADDYFILE"
		run systemctl reload caddy
	fi
	rm -f "$tmp"
	local port
	for port in "${UFW_PORTS[@]}"; do
		if ufw status | grep -qE "^$port +ALLOW +Anywhere"; then
			echo "ufw: $port already open"
		else
			run ufw allow "$port" comment "Happy Dungeons PS4"
		fi
	done
	if ((!DRY)); then
		local code
		code=$(curl -s -o /dev/null -w '%{http_code}' http://107.170.39.185/v12/api || true)
		echo "http://107.170.39.185/v12/api -> HTTP $code"
		code=$(curl -s -o /dev/null -w '%{http_code}' https://107-170-39-185.sslip.io/ps4/account || true)
		echo "https://107-170-39-185.sslip.io/ps4/account (player sign-in page) -> HTTP $code"
	fi
}

# ------------------------------------------------------------------------------------------------- website
WEB_INDEX=/opt/happydungeons/server/web/index.html
do_website() {
	local args=("$WEB_INDEX" "$@")
	((DRY)) && args+=(--dry-run)
	python3 "$HERE/players_page_ps4.py" "${args[@]}"
}

# ------------------------------------------------------------------------------------------------- status
do_status() {
	local u
	for u in "${UNITS[@]}"; do printf '%s: %s\n' "$u" "$(systemctl is-active "$u" 2>/dev/null || true)"; done
	ss -Hlntup | grep -E ':(31313|31314|31315|31350|31413|5101) ' || true
	curl -fsS http://127.0.0.1:5101/ps4/status 2>/dev/null && echo || echo "gateway not answering"
	tail -n 20 "$P/state/gateway.log" 2>/dev/null || true
}

# ------------------------------------------------------------------------------------------------- rollback
do_rollback() {
	run systemctl disable --now "${UNITS[@]}" || true
	if grep -qF "$MARK_BEGIN" "$CADDYFILE"; then
		local tmp
		tmp=$(mktemp)
		caddy_without >"$tmp"
		diff -u "$CADDYFILE" "$tmp" || true
		caddy validate --adapter caddyfile --config "$tmp" >/dev/null 2>&1 || die "Caddy rejects the cleaned file ($tmp)"
		run cp -p "$CADDYFILE" "$CADDYFILE.before-ps4-rollback-$STAMP"
		run install -o root -g root -m 0644 "$tmp" "$CADDYFILE"
		run systemctl reload caddy
		rm -f "$tmp"
	fi
	local port
	for port in "${UFW_PORTS[@]}"; do
		ufw status | grep -qE "^$port " && run ufw delete allow "$port"
	done
	do_website --remove || true
	echo "Rolled back. Files, shadNet accounts and links are still in $P."
}

case $STEP in
check) do_check ;;
fetch) do_fetch ;;
setup) do_setup ;;
start) do_start ;;
expose) do_expose ;;
website) do_website ;;
website-remove) do_website --remove ;;
status) do_status ;;
rollback) do_rollback ;;
*)
	sed -n '2,25p' "$0"
	exit 2
	;;
esac
