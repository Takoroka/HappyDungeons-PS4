from flask import Flask, request, Response, jsonify, send_file, g
from werkzeug.utils import safe_join
from werkzeug.middleware.proxy_fix import ProxyFix
from urllib.parse import unquote
from datetime import datetime, timezone
import asyncio
import base64
import copy
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import struct
import threading
import time
import uuid

try:
    from websockets.legacy.server import serve
except ImportError:
    raise SystemExit(
        "Missing dependency: websockets\n"
        "Install with: pip install flask websockets"
    )

app = Flask(__name__)


# hardening_patch v1 - release 2's review fixes (release2\hardening\hardening_patch.py). Request bodies are limited here;
# the rest of it is in the "Hardening" section below caller_xuid().
from flask import Request as _FlaskRequest   # noqa: E402

REQUEST_BODY_MAX_BYTES = 16 << 20              # every route but the Setup publish (saves and JSON are well under 1 MB)
CLIENT_PUBLISH_PATH = "/windurango/client/publish"


class HardenedRequest(_FlaskRequest):
    """Werkzeug reads max_content_length before reading a body: over it, 413 at once (Content-Length) or as soon as a
    chunked body passes it. The owner's Setup publish may carry the Setup (client_publish checks its own sizes)."""

    @property
    def max_content_length(self):
        if self.path == CLIENT_PUBLISH_PATH:
            return CLIENT_MAX_FILE_BYTES + CLIENT_MAX_RELEASE_BYTES + (1 << 20)
        return REQUEST_BODY_MAX_BYTES

    @max_content_length.setter
    def max_content_length(self, value):   # Flask 3.1 lets a view set it; this server never does
        pass


app.request_class = HardenedRequest

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
SESSION_DATA = os.path.join(DATA, "_sessions")
HANDLE_DATA = os.path.join(DATA, "_handles")


# ---------------------------------------------------------------------------------------------------------------------
# tuning_patch v1 - launch-day load tuning (loadtest\tuning\tuning_patch.py, applied after release 2's chain). Three
# switches in server_config.json, all off unless set; read again within TUNING_RECHECK_SECONDS of a change to the file
# (no restart needed - a restart drops every player's realtime connection):
#   "quietLogs": true            one access line per request (werkzeug's format + milliseconds, one write to stderr)
#                                instead of the request banner and the per-request dumps; status lines stay
#   "fastPaths": true            the same answers for less CPU and less time under STATE_LOCK (read-only session and
#                                handle reads through the parse cache, compact JSON writes, one event-loop wake-up per
#                                session change, base_of remembered, config files looked at every 2 s, /players built
#                                at most every 2 s)
#   "mmSkipFullLobbyPass": true  a host ticket from a lobby that cannot take anyone does not re-place every waiting
#                                seeker (their own tickets and timers still do)
import builtins    # noqa: E402
import functools   # noqa: E402
import sys         # noqa: E402
from werkzeug.serving import WSGIRequestHandler   # noqa: E402

TUNING_FILE = os.path.join(ROOT, "server_config.json")
TUNING_RECHECK_SECONDS = 2.0
#   "quietRoutineRequests": true (privacy_patch v2 quietlogs) with quietLogs: no access line for a 2xx/304 answered
#                                in under ACCESS_SLOW_MS, one "[HTTP] last minute: ..." line a minute instead. 4xx/5xx,
#                                slow ones, app lines and reads of what the server says about players by a caller
#                                without a verified key (access_keep) keep their line. Off (the default): every access
#                                line, as before. Switch it on only once the watcher counts requests from [HTTP] lines.
TUNING_SWITCHES = ("quietLogs", "fastPaths", "mmSkipFullLobbyPass", "quietRoutineRequests")
TUNING = dict.fromkeys(TUNING_SWITCHES, False)
TUNING_STATE = {"checked": None, "key": None}   # when the file was last looked at (monotonic), its (mtime_ns, size)
TUNING_LOCK = threading.Lock()
# quietLogs: in these functions every print is left out ...
TUNING_QUIET_EVERY_LINE = frozenset(('guard_host_write', 'log_members', 'log_request', 'notify_session_changed'))
# ... and in these the dumps (a line that does not start with "[", e.g. "SCID: ...", "PUT BODY: ...") are.
TUNING_QUIET_DUMPS = frozenset(('create_multiplayer_ticket', 'current_title_presence', 'eds_details', 'marketplace_inventory', 'momatch_create_ticket', 'mpsd_create_handle', 'mpsd_query_handles', 'mpsd_sessions', 'mpsd_specific_session', 'mpsd_template_sessions', 'player_profile', 'privacy_mute', 'social_people', 'title_storage', 'user_stats'))


def tuning(name):
    """One switch's current value (server_config.json, looked at again every TUNING_RECHECK_SECONDS)."""
    checked = TUNING_STATE["checked"]
    if checked is None or time.monotonic() - checked >= TUNING_RECHECK_SECONDS:
        tuning_reload()
    return TUNING[name]


def tuning_reload():
    with TUNING_LOCK:
        now = time.monotonic()
        checked = TUNING_STATE["checked"]
        if checked is not None and now - checked < TUNING_RECHECK_SECONDS:
            return
        TUNING_STATE["checked"] = now
        try:
            info = os.stat(TUNING_FILE)
            key = (info.st_mtime_ns, info.st_size)
        except OSError:
            key = None
        if checked is not None and key == TUNING_STATE["key"]:
            return
        TUNING_STATE["key"] = key
        doc = {}
        if key is not None:
            try:
                with open(TUNING_FILE, encoding="utf-8-sig") as handle:
                    doc = json.load(handle)
                if not isinstance(doc, dict):
                    raise ValueError("it is not a JSON object")
            except (OSError, ValueError) as error:
                builtins.print("[TUNING] server_config.json cannot be read (%s): the switches stay as they were (%s)"
                               % (error, tuning_text()))
                return
        new = {}
        for name in TUNING_SWITCHES:
            value = doc.get(name, False)
            if value not in (True, False) or isinstance(value, int) and not isinstance(value, bool):
                builtins.print('[TUNING] server_config.json "%s" is %r - only true or false; taken as false'
                               % (name, value))
            new[name] = value is True
        tuning_values_read(doc, checked is not None)   # (lobby_patch v1) the matchmaking settings, see below
        if new != TUNING:
            TUNING.update(new)
            if checked is not None:
                builtins.print("[TUNING] now " + tuning_text())


def tuning_text():
    return ", ".join("%s %s" % (name, "on" if TUNING[name] else "off") for name in TUNING_SWITCHES)


def tuning_summary():
    tuning("quietLogs")   # reads server_config.json the first time
    return "Tuning: %s (server_config.json; read again within %g s of a change)" % (
        tuning_text(), TUNING_RECHECK_SECONDS)


# lobby_patch v1 - matchmaking settings in server_config.json, read again with the switches above (within
# TUNING_RECHECK_SECONDS of a change to the file; no restart). Unlike the switches each has a default in the code (the
# SmartMatch section), used while the file leaves it out; a value it does not know is logged and the default used.
#   "mmStagePolicy": "strict"          where a seeker who picked a mission may be put (default MM_STAGE_POLICY):
#                                        "strict"  a lobby for that mission, or one with no mission;
#                                        "exact"   a lobby for that mission only;
#                                        "prefer"  that mission first, else ANY open lobby (before lobby_patch: the game
#                                                  keeps its own mission, so such a guest leaves when the host starts);
#                                        "any"     the same as "prefer".
#                                      An "any dungeon" seeker may go into every lobby under all four. The players
#                                      page's lobby Join and a friend's Join follow it too.
#   "mmDropPlacedSeekerLobby": true    a seeker put into someone else's lobby stops being a lobby that others are put
#                                      into (default MM_DROP_PLACED_SEEKER_LOBBY); false: the "ghost" lobbies of before.
# Setting "mmStagePolicy": "prefer" and "mmDropPlacedSeekerLobby": false places players exactly as before lobby_patch.
# (fps_split v3) 30 and 60 fps players are matched separately (X-WinDurango-Fps, see fps_split below):
#   "mmFpsSplit": true                 (default MM_FPS_SPLIT) false is the kill switch: every rate is put together again.
#   "mmFpsUnknown": "own"              (default MM_FPS_UNKNOWN) an older build, which sends no rate: "own" - older builds
#                                      only meet each other (how they are matched before the new build is out); "30" -
#                                      they join the 30 fps pool; "solo" - they are matched with nobody.
TUNING_VALUES = {"mmStagePolicy": ("strict", "exact", "prefer", "any"), "mmDropPlacedSeekerLobby": (True, False),
                 "mmFpsSplit": (True, False), "mmFpsUnknown": ("own", "30", "solo")}
TUNING_VALUE_DEFAULTS = {"mmStagePolicy": "MM_STAGE_POLICY", "mmDropPlacedSeekerLobby": "MM_DROP_PLACED_SEEKER_LOBBY",
                         "mmFpsSplit": "MM_FPS_SPLIT", "mmFpsUnknown": "MM_FPS_UNKNOWN"}
TUNING_VALUE = {}   # name -> the value server_config.json sets (a valid one); a name left out: the code's default


def tuning_value(name):
    """A matchmaking setting's current value: server_config.json's, else the code's default (see above)."""
    checked = TUNING_STATE["checked"]
    if checked is None or time.monotonic() - checked >= TUNING_RECHECK_SECONDS:
        tuning_reload()
    value = TUNING_VALUE.get(name)
    return globals().get(TUNING_VALUE_DEFAULTS[name]) if value is None else value


def tuning_values_read(doc, announce):
    """(tuning_reload, under TUNING_LOCK) The matchmaking settings in server_config.json's document. The dict is
    replaced, not changed, so a request reading it meanwhile sees the old settings or the new ones."""
    global TUNING_VALUE
    new = {}
    for name, allowed in TUNING_VALUES.items():
        value = doc.get(name)
        if value is None:
            continue
        if isinstance(value, str):
            value = value.strip().lower()
        if any(type(value) is type(ok) and value == ok for ok in allowed):
            new[name] = value
        else:
            builtins.print('[TUNING] server_config.json "%s" is %r - only %s; the default (%s) is used' % (
                name, doc.get(name), " or ".join(json.dumps(ok) for ok in allowed),
                json.dumps(globals().get(TUNING_VALUE_DEFAULTS[name]))))
    if new != TUNING_VALUE:
        TUNING_VALUE = new
        if announce:
            builtins.print("[TUNING] matchmaking now " + tuning_values_text())


def tuning_values_text():
    shown = []
    for name in TUNING_VALUES:
        value = TUNING_VALUE.get(name)
        shown.append("%s %s%s" % (name, json.dumps(globals().get(TUNING_VALUE_DEFAULTS[name]) if value is None
                                                   else value), " (default)" if value is None else ""))
    return ", ".join(shown)


def matchmaking_summary():
    tuning_value("mmStagePolicy")   # reads server_config.json the first time
    return "Matchmaking: %s (lobby_patch v1; server_config.json can set them, read again within %g s of a change)" % (
        tuning_values_text(), TUNING_RECHECK_SECONDS)


def print(*args, **kwargs):   # noqa: A001 - this module's print (quietLogs); everything else goes to builtins.print
    """With quietLogs, every line of the TUNING_QUIET_EVERY_LINE functions and the dumps of the TUNING_QUIET_DUMPS
    functions are left out. Anything else, and everything without quietLogs, is builtins.print as before."""
    if tuning("quietLogs"):
        where = sys._getframe(1).f_code.co_name
        if where in TUNING_QUIET_EVERY_LINE or (where in TUNING_QUIET_DUMPS and not (
                args and isinstance(args[0], str) and args[0].startswith("["))):
            return
    builtins.print(*args, **kwargs)


TUNING_CONTROL_CHARS = {c: "\\x%02x" % c for c in (*range(0x20), *range(0x7F, 0xA0))}
TUNING_CONTROL_CHARS[ord("\\")] = "\\\\"


class TunedRequestHandler(WSGIRequestHandler):
    """Werkzeug's request handler (run_http gives it to app.run). With quietLogs its access line - werkzeug's own
    format, plus the milliseconds from the request line to the response head - is one write to stderr instead of a
    trip through the logging module and colorama. Without quietLogs it is werkzeug's line, unchanged."""

    def run_wsgi(self):
        self.tuning_started = time.monotonic()
        return super().run_wsgi()

    def log_request(self, code="-", size="-"):
        if not tuning("quietLogs"):
            return super().log_request(code, size)
        started, self.tuning_started = getattr(self, "tuning_started", None), None
        try:
            ms = None if started is None else (time.monotonic() - started) * 1000
            took = "" if ms is None else " %dms" % ms
            if tuning("quietRoutineRequests"):   # (privacy_patch v2 quietlogs) every request counted; routine
                status = access_status(code)     # ones not written
                keep = (getattr(self, "environ", None) or {}).get(ACCESS_KEEP) is True
                access_count(self.address_string(), self.requestline, status, ms)
                if not keep and access_routine(status, ms):
                    return
            sys.stderr.write('%s - - [%s] "%s" %s %s%s\n' % (
                self.address_string(), self.log_date_time_string(),
                str(self.requestline).translate(TUNING_CONTROL_CHARS), code, size, took))
        except Exception:   # an access line never fails a request
            pass


# privacy_patch v2 quietlogs - with quietLogs AND "quietRoutineRequests" on, a 2xx/304 answered in under ACCESS_SLOW_MS
# gets no access line (the polls were 86-95% of the journal). Every request is still counted, and one line a minute
# (on the minute) says what the lines left out would have:
#   [HTTP] last minute: N requests, 4xx=a (404=b 401=c 403=d 426=e), 5xx=f, slow=g, ips=h, top=/route:count,...
# 4xx/5xx and slow requests keep their own access line (the same format as before), so a watcher counting those
# lines still counts them; N and ips come from the summary. Routes: ids -> <id>, long numbers -> <n>, "," -> %2C.
# A routine request keeps its line anyway when it reads what the server says about players (profile, people,
# whoami, achievements, a session or handle by name) without a verified key, or names another player's XUID, or
# reads a handle by id (the game never does): the per-address record for spotting scraping. The game signs every
# request, so these are few (the in-game profile card, which sends no key, is most of them).
# "quietRoutineRequests" off (the default): every access line exactly as before, no counting, no summary.
ACCESS_SLOW_MS = 1000
ACCESS_SUMMARY_SECONDS = 60
ACCESS_TOP_ROUTES = 5
ACCESS_MAX_ROUTES = 2000     # distinct routes kept a minute (a scanner's paths beyond it count as "(other)")
ACCESS_MAX_IPS = 20000
ACCESS_LOCK = threading.Lock()
ACCESS_STATS = {"n": 0, "codes": {}, "slow": 0, "ips": set(), "routes": {}}
ACCESS_THREAD = {"started": False}
ACCESS_ID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
ACCESS_NUMBER = re.compile(r"\d{6,}")
ACCESS_KEEP = "privacy_patch2.keep_access_line"   # set in the request's environ by access_mark (after_request)
ACCESS_WATCHED = re.compile(r"^/(?:users/[^/]+/(?:profile|people)(?:/|$)|windurango/identity/whoami$"
                            r"|windurango/achievements/(?!catalog$)[^/]+$"
                            r"|serviceconfigs/[^/]+/session[Tt]emplates/[^/]+/sessions/[^/]+$"
                            r"|handles/(?!query$)[^/]+(?:/session)?$)")
ACCESS_NAMED_XUID = re.compile(r"^/(?:users/xuid\((\d{1,20})\)/|windurango/achievements/(\d{1,20})$)")
# Routes that answer an unsigned caller with counts only - unless their group was left out of this server (the mark
# near the end of the file lists the groups it has): then they are watched too. (/windurango/players has answered
# unsigned callers with a count since v1; the launcher polls it and /relay once a minute without a key.)
ACCESS_WATCHED_UNLESS = {"relay": "/windurango/relay", "lobbies": "/windurango/lobbies"}


def access_keep(method, path, verified):
    """Whether a routine request keeps its access line: see above."""
    if not ACCESS_WATCHED.match(path):
        applied = globals().get("PRIVACY_PATCH2_GROUPS", ())
        return not verified and any(path == route for group, route in ACCESS_WATCHED_UNLESS.items()
                                    if group not in applied)
    if not verified or (method == "GET" and path.startswith("/handles/")):
        return True
    named = ACCESS_NAMED_XUID.match(path)
    xuid = named and (named.group(1) or named.group(2))
    return bool(xuid) and not in_caller_block(xuid)


@app.after_request
def access_mark(response):
    """(privacy_patch v2 quietlogs) Marks the requests access_keep keeps (only while routine lines are left out)."""
    try:
        if (tuning("quietLogs") and tuning("quietRoutineRequests")
                and access_keep(request.method, request.path, g.get("caller_verified") is True)):
            request.environ[ACCESS_KEEP] = True
    except Exception:   # never fails a request
        pass
    return response


def access_status(code):
    try:
        return int(getattr(code, "value", code))
    except (TypeError, ValueError):
        return 0


def access_routine(status, ms):
    """A request whose access line is left out: answered 2xx or 304, in under ACCESS_SLOW_MS."""
    return (200 <= status < 300 or status == 304) and (ms is None or ms < ACCESS_SLOW_MS)


def access_route(requestline):
    parts = str(requestline).split(" ")
    path = parts[1].split("?", 1)[0] if len(parts) >= 2 else "?"
    path = ACCESS_NUMBER.sub("<n>", ACCESS_ID.sub("<id>", path))[:96]
    return path.translate(TUNING_CONTROL_CHARS).replace(",", "%2C")


def access_count(address, requestline, status, ms):
    route = access_route(requestline)
    with ACCESS_LOCK:
        stats = ACCESS_STATS
        stats["n"] += 1
        stats["codes"][status] = stats["codes"].get(status, 0) + 1
        if ms is not None and ms >= ACCESS_SLOW_MS:
            stats["slow"] += 1
        if len(stats["ips"]) < ACCESS_MAX_IPS:
            stats["ips"].add(address)
        routes = stats["routes"]
        if route not in routes and len(routes) >= ACCESS_MAX_ROUTES:
            route = "(other)"
        routes[route] = routes.get(route, 0) + 1
        if not ACCESS_THREAD["started"]:
            ACCESS_THREAD["started"] = True
            threading.Thread(target=access_summaries, name="access-summary", daemon=True).start()


def access_summary_line(stats):
    codes = stats["codes"]
    c4 = sum(n for code, n in codes.items() if 400 <= code < 500)
    c5 = sum(n for code, n in codes.items() if 500 <= code < 600)
    top = sorted(stats["routes"].items(), key=lambda item: (-item[1], item[0]))[:ACCESS_TOP_ROUTES]
    return ("[HTTP] last minute: %d requests, 4xx=%d (404=%d 401=%d 403=%d 426=%d), 5xx=%d, slow=%d, ips=%d, top=%s"
            % (stats["n"], c4, codes.get(404, 0), codes.get(401, 0), codes.get(403, 0), codes.get(426, 0), c5,
               stats["slow"], len(stats["ips"]), ",".join("%s:%d" % item for item in top)))


def access_flush():
    """Writes the last minute's line and starts the next minute (quiet mode on, or anything counted)."""
    with ACCESS_LOCK:
        stats = dict(ACCESS_STATS)
        ACCESS_STATS.update(n=0, codes={}, slow=0, ips=set(), routes={})
    quiet = tuning("quietLogs") and tuning("quietRoutineRequests")
    if quiet or stats["n"]:
        builtins.print(access_summary_line(stats), flush=True)


def access_summaries():
    while True:
        time.sleep(ACCESS_SUMMARY_SECONDS - time.time() % ACCESS_SUMMARY_SECONDS + 0.05)
        try:
            access_flush()
        except Exception as error:   # the summary never stops the server
            builtins.print("[HTTP] summary failed: %r" % (error,), flush=True)


def tuning_save_json(original):
    """fastPaths: save_json with one C-encoded json.dumps (compact) and one write - json.dump(indent=2) is the
    pure-Python encoder and writes piece by piece. The same steps otherwise: temporary file, os.replace, the parse
    cache told. Without fastPaths: the original save_json."""
    @functools.wraps(original)
    def save(path, data):
        if not tuning("fastPaths"):
            return original(path, data)
        if not path:
            return False
        text = json.dumps(data, separators=(",", ":"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
        session_file_changed(path)
        return True
    return save


TUNING_TTL_HITS = {}   # function name -> (monotonic time, its last answer)


def tuning_ttl(function):
    """fastPaths: a reader of a config file (which reads it again whenever it changes) is asked at most once every
    TUNING_RECHECK_SECONDS and its last answer used in between. Without fastPaths: function() on every call."""
    @functools.wraps(function)
    def cached():
        if not tuning("fastPaths"):
            return function()
        now = time.monotonic()
        hit = TUNING_TTL_HITS.get(function.__name__)
        if hit is not None and now - hit[0] < TUNING_RECHECK_SECONDS:
            return hit[1]
        value = function()
        TUNING_TTL_HITS[function.__name__] = (now, value)
        return value
    return cached


def tuning_ttl_resetting(function):
    """function changes server_config.json: after it, every tuning_ttl reader asks its file again."""
    @functools.wraps(function)
    def resetting(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        finally:
            TUNING_TTL_HITS.clear()
    return resetting


TUNING_SHARED_ANSWERS = {}   # route function name -> (monotonic time, body, status, headers)


def tuning_shared_answer(view):
    """fastPaths: a GET route whose answer is the same for every caller is built at most once every
    TUNING_RECHECK_SECONDS; the callers in between get the same body. Without fastPaths: view() every time."""
    @functools.wraps(view)
    def answer(*args, **kwargs):
        if not tuning("fastPaths"):
            return view(*args, **kwargs)
        now = time.monotonic()
        hit = TUNING_SHARED_ANSWERS.get(view.__name__)
        if hit is None or now - hit[0] >= TUNING_RECHECK_SECONDS:
            response = view(*args, **kwargs)
            if response.status_code != 200 or response.is_streamed:
                return response
            hit = (now, response.get_data(), response.status_code, list(response.headers.items()))
            TUNING_SHARED_ANSWERS[view.__name__] = hit
        return Response(hit[1], status=hit[2], headers=hit[3])
    return answer


def session_shared(path):
    """fastPaths: a stored session through the parse cache (session_parsed: parsed again only when the file changed) -
    the dict is SHARED, so only for code that never changes it. Without fastPaths: load_json(path), as before."""
    if not tuning("fastPaths"):
        return load_json(path)
    return session_parsed(path)[0] if path else None


def session_shared_from_ref(session_ref):
    """load_session_from_ref through session_shared (for read-only callers)."""
    if not tuning("fastPaths"):
        return load_session_from_ref(session_ref)
    if not isinstance(session_ref, dict):
        return None
    scid, template_name = session_ref.get("scid"), session_ref.get("templateName")
    session_name = session_ref.get("name")
    if not scid or not template_name or not session_name:
        return None
    return session_shared(session_file(scid, template_name, session_name))


TUNING_HANDLES = {}          # handle file -> ((mtime_ns, size, this server's writes), parsed handle)
TUNING_HANDLES_MAX = 4096    # past this many (entries of removed handles pile up) the cache starts over


def handle_shared(path):
    """fastPaths: an MPSD handle file, parsed again only when it changed - SHARED, for code that only reads it.
    Without fastPaths: load_json(path), as before."""
    if not tuning("fastPaths"):
        return load_json(path)
    key = session_key(path)
    try:
        info = os.stat(path)
    except OSError:
        TUNING_HANDLES.pop(key, None)
        return None
    stamp = (info.st_mtime_ns, info.st_size, SESSION_WRITES.get(key, 0))
    cached = TUNING_HANDLES.get(key)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    handle = load_json(path)
    if len(TUNING_HANDLES) >= TUNING_HANDLES_MAX:
        TUNING_HANDLES.clear()
    TUNING_HANDLES[key] = (stamp, handle)
    return handle


def tuning_send_taps(batch):
    """fastPaths: one session change's RTA taps, sent from one event-loop callback - the frames and websockets the
    per-member callbacks used to send, in the same order."""
    for websocket, frame in batch:
        asyncio.ensure_future(rta_send(websocket, frame))


BASE_OF_KNOWN = {}   # fastPaths: int(XUID) -> the xuidBase that owned it when base_of last walked the registry

TITLE_GROUP = "be554af1-2afa-4168-b64f-bdcb9ce7ac7b"
REPUTATION_SCID = "7492baca-c1b4-440d-a391-b7ef364a8d40"

RESET_TRANSIENT_MULTIPLAYER_ON_START = True

STATE_LOCK = threading.RLock()

# RTA: event loop of the websocket server and live MPSD connections,
# connection id -> (websocket, subscription id of the connections subscription).
RTA_LOOP = None
RTA_CONNECTIONS = {}
# connection id -> xuidBase of the game that took it (None: no verified identity), until its members are let go.
RTA_CONNECTION_OWNERS = {}

# Gamertag reported for session members (matches WinDurango's winDurango.json).
LOCAL_GAMERTAG = "Durangler"

# SmartMatch: seconds before a ticket nothing matched is answered (a host's with its own lobby, a seeker's with a
# session of its own).
MATCH_FOUND_AFTER_SECONDS = 3


# This is a Happy Dungeons server. Its sessions hold several real players (up to the template's size, 4), matched
# by the multi-player SmartMatch below, and its MPSD and matchmaking routes answer 404 for any other game's service
# config (not_happy_dungeons).
HD_SCID = "b32e0100-8014-44fb-a4c5-eb943751a2ca"


def is_hd_scid(scid):
    return str(scid or "").lower() == HD_SCID

# Which game a player is in, from the service config id the title puts in its requests (and in its presence).
GAME_NAMES = {HD_SCID: "Happy Dungeons"}
SCID_IN_TEXT = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
LAST_GAME = {}   # str(xuidBase) -> the game that player's requests last came from


def template_max_members(scid, template_name):
    # Template names carry their size ("107_MatchTicketSession4", "..._08", "..._16").
    match = re.search(r"(\d+)$", template_name or "")
    if match and not (template_name or "").startswith(match.group(1)):
        return int(match.group(1))

    # Happy Dungeons sessions without a size in the name (e.g. 107_PrivateMatchSession) hold 4 players.
    if is_hd_scid(scid):
        return 4

    return None


def device_token_for(xuid):
    return str(uuid.uuid5(uuid.NAMESPACE_OID, "windurango-device-" + str(xuid)))


def session_etag(session):
    return '"%s-%s"' % (session.get("branch", "local"), session.get("changeNumber", 0))


def reset_transient_multiplayer_state():
    if not RESET_TRANSIENT_MULTIPLAYER_ON_START:
        return

    for path in (SESSION_DATA, HANDLE_DATA):
        shutil.rmtree(path, ignore_errors=True)


reset_transient_multiplayer_state()

for path in (DATA, SESSION_DATA, HANDLE_DATA):
    os.makedirs(path, exist_ok=True)


# Helpers

def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def log_request(service):
    if tuning("quietLogs"):   # (tuning_patch: the access line says what was asked)
        return
    print("\n" + "=" * 76)
    print(f"[{service}] {request.method} {request.path}")

    if request.query_string:
        print("QUERY:", request.query_string.decode("utf-8", errors="replace"))

    original_host = request.headers.get("X-Original-Host")
    if original_host:
        print("ORIGINAL HOST:", original_host)
    if request.headers.get("X-WinDurango-Fps"):   # (fps_split v3)
        print("FPS HEADER:", request.headers.get("X-WinDurango-Fps")[:8])

    # The token is "XBL3.0 x=<n>;WD1.<xuidBase>.<secret>". Show who it claims to be; never the secret.
    auth = request.headers.get("Authorization") or ""
    claimed = re.search(r"WD1\.(\d+)\.", auth)
    print("AUTH HEADER:", "missing" if not auth else
          ("player %s (secret %s)" % (claimed.group(1), "set" if auth.rstrip().split(".")[-1] else "empty")
           if claimed else "present, no WinDurango identity"))
    if request.headers.get("X-WinDurango-Player"):
        print("PLAYER HEADER:", request.headers.get("X-WinDurango-Player")[:24])   # (hardening: not a 1 MB line)
    print("CONTRACT:", request.headers.get("X-Xbl-Contract-Version"))
    for header in ("If-Match", "If-None-Match"):
        if request.headers.get(header):
            print(header.upper() + ":", request.headers.get(header))
    print("=" * 76)


def digits_of(value):
    """Accepts "xuid(123)", "123" or "me" and returns the bare id."""
    found = re.findall(r"\d+", str(value))
    return found[0] if found else str(caller_xuid() or "")


def json_response(data, status=200):
    response = jsonify(data)
    response.status_code = status
    response.headers["Cache-Control"] = "no-cache, no-store"
    return response


# ---------------------------------------------------------------------------
# Players.
#
# Each player owns four consecutive XUIDs, xuidBase .. xuidBase+3 (the title always asks about four local
# users), and proves who they are with the token WinDurango sends on every request:
#     Authorization: XBL3.0 x=<n>;WD1.<xuidBase>.<secret>
# The registry maps xuidBase -> gamertag + secret. A player gets a secret by claiming a gamertag
# (/windurango/identity/claim, done by the play script before the game starts).
PLAYER_FILE = os.path.join(DATA, "_players.json")
XUIDS_PER_PLAYER = 4
# XUIDs are made the way Xenia makes profile XUIDs - random, generated on the player's own PC, so identities do not
# depend on the order people joined and stay unique across servers - but in the Xbox One range this title uses:
# 0x0009 followed by 46 random bits, 4-aligned for the four local users. (Xenia's own are Xbox 360 offline XUIDs,
# 0xE03 << 52, which are 20 digits, past 2^53 and outside what an Xbox One title expects.)
XUID_PREFIX = 0x0009 << 48
XUID_LIMIT = 0x000A << 48
# Before random XUIDs, blocks were handed out in order from this one (and it was WinDurango's built-in default).
# That window is reserved now: identities found in it are moved to random blocks at startup
# (migrate_sequential_identities) and nothing new is given one.
FIRST_XUID_BASE = 2535437789298188
SEQUENTIAL_WINDOW = 4 * 4096
MOVED_FILE = os.path.join(DATA, "_moved_xuids.json")


def in_sequential_window(value):
    return FIRST_XUID_BASE <= int(value) < FIRST_XUID_BASE + SEQUENTIAL_WINDOW


def random_xuid_base():
    while True:
        base = XUID_PREFIX | (secrets.randbits(46) << 2)
        if (not in_sequential_window(base) and base_of(base) is None
                and base_of(base + XUIDS_PER_PLAYER - 1) is None):
            return str(base)


def valid_xuid_base(value):
    return (str(value).isdigit() and XUID_PREFIX <= int(value) <= XUID_LIMIT - XUIDS_PER_PLAYER
            and int(value) % XUIDS_PER_PLAYER == 0 and not in_sequential_window(value))

PLAYER_REGISTRY = {}   # str(xuidBase) -> {"gamertag", "secret", "created"}

# str(xuidBase) -> time of that player's last request. The game talks to the server constantly (session
# polls, presence, saves), so this is a better "is this person here" signal than presence: a game that
# crashes never sends the presence DELETE and would otherwise look online forever.
LAST_SEEN = {}
ONLINE_WITHIN_SECONDS = 90
# (fps_split v3) str(xuidBase) -> {"fps": 30 | 60 | "old", "at": time [, "header": time]}: the frame rate the player's
# game last said (X-WinDurango-Fps), or "old" for an older build's session traffic. In memory only (fps_note_caller).
PLAYER_FPS = {}

# str(xuidBase) -> number of open realtime (RTA) websockets. The game keeps one open for its whole session,
# including while it sits quietly in the menus, so an open socket is the real "this person is playing".
RTA_PLAYERS = {}


def is_online(base):
    base = str(base)
    return RTA_PLAYERS.get(base, 0) > 0 or time.time() - LAST_SEEN.get(base, 0) < ONLINE_WITHIN_SECONDS


def load_players():
    try:
        with open(PLAYER_FILE, encoding="utf-8") as handle:
            return {str(k): v for k, v in json.load(handle).items()}
    except (OSError, ValueError):
        return {}


def save_players():
    try:
        os.makedirs(DATA, exist_ok=True)
        temp = PLAYER_FILE + ".tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(PLAYER_REGISTRY, handle, indent=1, sort_keys=True)
        os.replace(temp, PLAYER_FILE)
    except OSError as error:
        print("COULD NOT SAVE PLAYERS:", error)


PLAYER_REGISTRY.update(load_players())


# solo_patch v1 - solo play: "a mode where people can matchmake with just themselves and not find anyone else".
#
# A player's own switch, OFF until they turn it on, kept in their registry record ("solo": true) and saved with the
# rest of the record, so it survives a restart and follows them through the sequential-XUID migration. While it is
# on the server never puts anyone into their lobby (open_lobby) and never puts them into anyone else's
# (place_seeker); their search is answered with their own session, exactly as it is when nobody else is online, and
# the game starts the dungeon alone. Nothing else about the player changes.
#
# SOLO_COUNT is only a guard so the matchmaking paths cost nothing while nobody plays solo; it counts records, so
# re-keying a record (migrate_sequential_identities) does not make it wrong.
SOLO_API = 1
SOLO_COUNT = [sum(1 for r in PLAYER_REGISTRY.values() if isinstance(r, dict) and r.get("solo") is True)]
SOLO_TEXT = {True: "Solo play is on - nobody will be put into your dungeons.",
             False: "Solo play is off - other players can be matched into your dungeons."}


def solo_play(base):
    """True when this player asked to play alone (their registry record's "solo")."""
    record = PLAYER_REGISTRY.get(str(base)) if base is not None else None
    return isinstance(record, dict) and record.get("solo") is True


def solo_xuid(xuid):
    """solo_play for any of a player's four XUIDs (or for a lobby's owner / a session's host). Costs one integer
    comparison while nobody on this server plays solo."""
    return SOLO_COUNT[0] > 0 and solo_play(base_of(xuid))


def solo_count():
    return sum(1 for r in PLAYER_REGISTRY.values() if isinstance(r, dict) and r.get("solo") is True)


def set_solo_play(base, value):
    """Turns solo play on or off for a player and saves it; returns the new state. The caller holds STATE_LOCK."""
    base = str(base)
    record = PLAYER_REGISTRY.get(base)
    if not isinstance(record, dict):
        return False
    value = bool(value)
    was = record.get("solo") is True
    if value:
        record["solo"] = True
    else:
        record.pop("solo", None)
    SOLO_COUNT[0] = solo_count()
    if was != value:
        save_players()
        print("[SOLO] %s turned solo play %s (%d player%s on this server play%s solo now)" % (
            gamertag_for(primary_xuid(base)),
            "on - nobody will be put into their dungeons" if value else "off - normal matchmaking again",
            SOLO_COUNT[0], "" if SOLO_COUNT[0] == 1 else "s", "s" if SOLO_COUNT[0] == 1 else ""))
    return value


def solo_startup_line():
    return "Solo play: %d of %d player%s (solo_patch v1; the players page's Solo play box switches it)" % (
        SOLO_COUNT[0], len(PLAYER_REGISTRY), "" if len(PLAYER_REGISTRY) == 1 else "s")


def base_of(xuid):
    """The registered xuidBase that owns this XUID, or None."""
    try:
        n = int(str(xuid))
    except (TypeError, ValueError):
        return None
    fast = tuning("fastPaths")
    if fast:   # (tuning_patch) the owner found before, while it is still registered (4-aligned blocks never overlap)
        known = BASE_OF_KNOWN.get(n)
        if known is not None and known in PLAYER_REGISTRY:
            return known
    for base in list(PLAYER_REGISTRY):  # a copy: identity_claim may add a player meanwhile
        if int(base) <= n < int(base) + XUIDS_PER_PLAYER:
            if fast:
                BASE_OF_KNOWN[n] = base
            return base
    return None


def gamertag_for(xuid):
    base = base_of(xuid)
    if base and PLAYER_REGISTRY[base].get("gamertag"):
        # (localusers_patch S2) The player's other local users: "<gamertag> (2)", "(3)", "(4)" (xuidBase+2, +3, +0).
        offset = int(str(xuid)) - int(base)
        name = PLAYER_REGISTRY[base]["gamertag"]
        return name if offset == 1 else "%s (%d)" % (name, 4 if offset == 0 else offset)
    return LOCAL_GAMERTAG


# localusers_patch v1 - local multiplayer on one PC: ".u<id>" at the end of a local player's token (next3 localmp).
LOCAL_USER_MARK = re.compile(r"WD1\.\d{1,20}\.[0-9a-fA-F]*\.u([0-3])(?![0-9A-Za-z])")


def local_user_offset(auth):
    """0..3 when the token is a local player's own (";WD1.<base>.<secret>.u<id>"), else None."""
    match = LOCAL_USER_MARK.search(auth or "")
    return int(match.group(1)) if match else None


def parse_token(auth):
    """(xuidBase, secret) from "XBL3.0 x=<n>;WD1.<base>.<secret>", or (None, None)."""
    match = re.search(r"WD1\.(\d{1,20})\.([0-9a-fA-F]*)", auth or "")   # (hardening: at most 20 digits)
    return (match.group(1), match.group(2)) if match else (None, None)


def verify_player(base, secret):
    """True when this base has a secret and the caller knows it; None when the base has no secret yet."""
    record = PLAYER_REGISTRY.get(str(base)) if base else None
    if not record or not record.get("secret"):
        return None
    return hmac.compare_digest(record["secret"], secret or "")


@app.before_request
def identify_caller():
    g.caller_base = None
    g.caller_xuid = None
    g.caller_verified = None
    g.caller_fps = None   # (fps_split v3) a verified caller's X-WinDurango-Fps: 30, 60 or None (fps_note_caller)

    base, secret = parse_token(request.headers.get("Authorization"))
    if base is None:
        header = request.headers.get("X-WinDurango-Player", "")
        base = header if header.isdigit() and len(header) <= 20 else None   # (hardening: at most 20 digits)
    if base is None:
        return None

    g.caller_base = base
    seen_before = LAST_SEEN.get(base)   # (hardening, below)
    g.caller_verified = verify_player(base, secret)
    if not request.path.startswith("/windurango/client/"):   # the launcher (client updater) is not playing
        LAST_SEEN[base] = time.time()
    # (hardening) Online and "in which game" only for a request that proves who it is: a bare X-WinDurango-Player
    # header or a wrong secret used to mark that player online (and every made-up id stayed in LAST_SEEN for good).
    # The updater's lines just above stay as they are (its patch checks them); an unproven request is taken back here.
    if g.caller_verified is not True:
        if seen_before is None:
            LAST_SEEN.pop(base, None)
        else:
            LAST_SEEN[base] = seen_before
    if g.caller_verified is True:
        fps_note_caller(base)   # (fps_split v3) the rate this request says, or an older build's session traffic
    for scid in (SCID_IN_TEXT.findall(request.path) if g.caller_verified is True else ()):
        if scid.lower() in GAME_NAMES:
            LAST_GAME[base] = GAME_NAMES[scid.lower()]
            break

    # The XUID this request is about: the one in the path when it belongs to the caller, otherwise the
    # caller's signed-in user (local user 1, which is the one the title plays as).
    low = int(base)
    for found in re.findall(r"\d{8,}", request.path):
        if low <= int(found) < low + XUIDS_PER_PLAYER:
            g.caller_xuid = found
            break
    else:
        # (localusers_patch S1) A local player's own token names its local user: "me" is xuidBase+id, not player 1.
        offset = local_user_offset(request.headers.get("Authorization"))
        g.caller_xuid = str(low + (1 if offset is None else offset))
    return None


def caller_xuid():
    """The XUID of whoever is making this request (identify_caller), or None when the request carries no
    WinDurango identity or this is not a request at all (the RTA socket, timers)."""
    try:
        return g.get("caller_xuid")
    except RuntimeError:
        return None


# ---------------------------------------------------------------------------------------------------------------------
# Hardening (hardening_patch v1, release 2's review): title storage writes, the relay-mode address rule, the session
# walk, the per-player rate limit and the startup line. Request bodies: HardenedRequest near the top.
TITLE_STORAGE_MAX_BYTES = REQUEST_BODY_MAX_BYTES   # one save file
TITLE_STORAGE_QUOTA_BYTES = 64 << 20               # everything under a player's four data/<xuid> folders
TITLE_STORAGE_QUOTA_ENTRIES = 20000                 # files and folders looked at while adding them up
TITLE_STORAGE_LOCK = threading.Lock()              # one save write at a time: the quota check and the write together
SESSIONS_PER_PLAYER = 16                           # stored sessions a player may be in (joined or reserved) at once
RATE_BURST = 30                                    # /me, /friends/join, /news/seen: this many at once per player ...
RATE_PER_SECOND = 2.0                              # ... then this many a second (the Guide polls /me every 5 s)
RATE_LIMITED_PATHS = frozenset({"/windurango/me", "/windurango/friends/join", "/windurango/news/seen",
                                "/windurango/me/solo",   # (solo_patch v1)
                                "/windurango/friends/invite", "/windurango/friends/invite/accept",   # (invite_patch v1)
                                "/windurango/friends/invite/decline", "/windurango/friends/invite/cancel"})
RATE_BUCKETS = {}                                  # str(xuidBase) -> [tokens, time]
RATE_LOCK = threading.Lock()
KEYED_DOWNLOADS_PER_HOUR = 6                       # Setup downloads a player starts per hour (the launcher needs one)
ADDRESS_DOWNLOADS_PER_HOUR = 60                    # all Setup downloads from one address per hour, key or not
KEYED_DOWNLOADS = {}                               # str(xuidBase) -> times
ADDRESS_DOWNLOADS = {}                             # address -> times
LOOPBACK_ADDRESSES = ("127.0.0.1", "::1", "::ffff:127.0.0.1")
PEER_ADDRESS_CACHE = {"key": None, "allow": False}  # server_config.json "allowRealPeerAddresses", read again on change
SESSION_PARSED = {}                                # session file -> ((mtime_ns, size), this server's writes, session)
SESSION_WRITES = {}                                # session file -> how often this server wrote or removed it


def from_this_machine():
    """The request comes from this computer itself (not through the reverse proxy: Caddy's X-Forwarded-For names the
    player's own address, and ProxyFix makes that remote_addr)."""
    try:
        return (request.remote_addr or "") in LOOPBACK_ADDRESSES
    except RuntimeError:
        return False


def session_key(path):
    """One spelling per file: safe_join gives "_sessions/scid/..." and os.walk gives backslashes on Windows."""
    return os.path.normcase(os.path.abspath(path))


def session_file_changed(path):
    """This server wrote or removed a session file: its parsed copy is stale (mtime alone may not show it: two writes
    in one clock tick with the same size)."""
    if path:
        key = session_key(path)
        SESSION_WRITES[key] = SESSION_WRITES.get(key, 0) + 1
        SESSION_PARSED.pop(key, None)


def session_parsed(path):
    """(session dict or None, mtime) of a stored session, parsed again only when it changed. The dict is shared by
    every walk: callers only read it (iter_sessions' callers never change what it returns)."""
    key = session_key(path)
    writes = SESSION_WRITES.get(key, 0)
    try:
        info = os.stat(path)
    except OSError:
        return None, 0
    stamp = (info.st_mtime_ns, info.st_size)
    cached = SESSION_PARSED.get(key)
    if cached is not None and cached[0] == stamp and cached[1] == writes:
        return cached[2], info.st_mtime
    session = load_json(path)
    SESSION_PARSED[key] = (stamp, writes, session)
    return session, info.st_mtime


def rate_refusal():
    """A 429 when a verified player asks /me, /friends/join or /news/seen faster than RATE_PER_SECOND after a burst of
    RATE_BURST; None otherwise. (Each of them walks every stored session under the global lock.)"""
    if request.path not in RATE_LIMITED_PATHS or g.get("caller_verified") is not True:
        return None
    base, now = g.get("caller_base"), time.monotonic()
    with RATE_LOCK:
        tokens, then = RATE_BUCKETS.get(base, (float(RATE_BURST), now))
        tokens = min(float(RATE_BURST), tokens + (now - then) * RATE_PER_SECOND)
        if tokens < 1.0:
            RATE_BUCKETS[base] = (tokens, now)
            wait = max(1, int((1.0 - tokens) / RATE_PER_SECOND + 0.999))
        else:
            RATE_BUCKETS[base] = (tokens - 1.0, now)
            return None
    response = json_response({"ok": False, "reason": "too many requests - try again in a moment"}, 429)
    response.headers["Retry-After"] = str(wait)
    return response


app.before_request(rate_refusal)   # after identify_caller (registered first, above)


def download_refused(base, address, starting):
    """(Setup downloads) A reason to refuse this download, or None (and it is counted). Only downloads that start at
    the beginning count: a launcher resuming with Range is the same download."""
    if not starting:
        return None
    now = time.time()
    with CLIENT_CACHE_LOCK:
        for table in (KEYED_DOWNLOADS, ADDRESS_DOWNLOADS):
            if len(table) > 4096:
                for known in [k for k, times in table.items() if not times or now - times[-1] >= 3600]:
                    del table[known]
        by_address = [t for t in ADDRESS_DOWNLOADS.get(address, []) if now - t < 3600]
        by_player = [t for t in KEYED_DOWNLOADS.get(base, []) if now - t < 3600] if base else []
        if len(by_address) >= ADDRESS_DOWNLOADS_PER_HOUR:
            return "too many downloads from this address - try again later"
        if base and len(by_player) >= KEYED_DOWNLOADS_PER_HOUR:
            return "too many downloads for this player - try again later"
        ADDRESS_DOWNLOADS[address] = by_address + [now]
        if base:
            KEYED_DOWNLOADS[base] = by_player + [now]
    return None


def title_storage_used(base, limit):
    """Bytes under the data/<xuid> folders of this player's block, stopping once past limit (or after
    TITLE_STORAGE_QUOTA_ENTRIES entries: then it answers limit + 1)."""
    total, seen = 0, 0
    for i in range(XUIDS_PER_PLAYER):
        folder = os.path.join(DATA, str(int(base) + i))
        for root, dirs, files in os.walk(folder):
            seen += len(dirs) + len(files)
            if seen > TITLE_STORAGE_QUOTA_ENTRIES:
                return limit + 1
            for name in files:
                try:
                    total += os.lstat(os.path.join(root, name)).st_size
                except OSError:
                    pass
                if total > limit:
                    return total
    return total


def title_storage_put(xuid, file_path):
    """PUT of one save: only the verified owner of this XUID's block (or, with no identity, a caller on this machine,
    for a folder no registered player owns), within TITLE_STORAGE_QUOTA_BYTES; streamed into a temporary file next to
    the save, then renamed over it."""
    owner = base_of(xuid)
    caller = g.get("caller_base")
    if g.get("caller_verified") is True:
        if owner != caller:
            return json_response({"error": "not your save"}, 403)
    elif not from_this_machine() or owner is not None:
        print("[STORAGE] refused PUT %s: no player key%s" % (xuid, "" if from_this_machine() else " (not from this machine)"))
        return json_response({"error": "sign in to save (no player key)"}, 403)
    folder = os.path.dirname(file_path)
    temp = "%s.%s.upload" % (file_path, secrets.token_hex(4))
    size = 0
    try:
        # The body first, without the lock (a slow sender must not hold up everyone's saves); the quota check and the
        # rename under it. The temporary file sits in the player's own folder, so the walk counts it: after the rename
        # the player keeps what the walk found less the file it replaces.
        os.makedirs(folder, exist_ok=True)
        with open(temp, "wb") as handle:
            for chunk in iter(lambda: request.stream.read(1 << 16), b""):
                size += len(chunk)
                if size > TITLE_STORAGE_MAX_BYTES:
                    break
                handle.write(chunk)
        if size > TITLE_STORAGE_MAX_BYTES:
            print("[STORAGE] refused PUT %s: over %d MB" % (xuid, TITLE_STORAGE_MAX_BYTES >> 20))
            return json_response({"error": "too big: one save may be %d MB" % (TITLE_STORAGE_MAX_BYTES >> 20)}, 413)
        with TITLE_STORAGE_LOCK:
            if owner:
                used = title_storage_used(owner, TITLE_STORAGE_QUOTA_BYTES + TITLE_STORAGE_MAX_BYTES)
                try:
                    current = os.path.getsize(file_path) if os.path.isfile(file_path) else 0
                except OSError:
                    current = 0
                if used - current > TITLE_STORAGE_QUOTA_BYTES:
                    print("[STORAGE] refused PUT %s: player %s is over %d MB of saves" % (
                        xuid, owner, TITLE_STORAGE_QUOTA_BYTES >> 20))
                    return json_response({"error": "too big: a player may keep %d MB of saves" % (
                        TITLE_STORAGE_QUOTA_BYTES >> 20)}, 413)
            for attempt in range(20):
                try:
                    os.replace(temp, file_path)
                    break
                except PermissionError:   # Windows: a GET of this save still has it open - it is closed in a moment
                    if attempt == 19:
                        raise
                    time.sleep(0.05)
    except OSError as error:
        print("[STORAGE] could not write %s: %s" % (file_path, error))
        return json_response({"error": "could not save"}, 500)
    finally:
        if os.path.exists(temp):
            try:
                os.remove(temp)
            except OSError:
                pass
    return Response("{}", status=201, mimetype="application/json")


def peer_real_addresses_allowed():
    """server_config.json "allowRealPeerAddresses": true (a LAN / VPN group that does not use the relay). Read again
    when the file changes."""
    path = os.path.join(ROOT, "server_config.json")
    try:
        info = os.stat(path)
        key = (info.st_mtime_ns, info.st_size)
    except OSError:
        return False
    if PEER_ADDRESS_CACHE["key"] == key:
        return PEER_ADDRESS_CACHE["allow"]
    allow = False
    try:
        with open(path, encoding="utf-8-sig") as handle:
            allow = json.load(handle).get("allowRealPeerAddresses") is True
    except (OSError, ValueError, AttributeError):
        pass
    PEER_ADDRESS_CACHE.update(key=key, allow=allow)
    return allow


def peer_address_refusal(value, xuid):
    """Why this member's secureDeviceAddress may not be stored, or None. In relay mode a player is reached only at their
    own relay address; a real IP:port would make the relayed players' games send there (their public address to
    whoever wrote it) and let its packets past their stray filter."""
    system = ((value.get("properties") or {}).get("system") if isinstance(value.get("properties"), dict) else None)
    if not isinstance(system, dict) or "secureDeviceAddress" not in system:
        return None
    if not SERVER_CONFIG.get("relayPort") or from_this_machine() or peer_real_addresses_allowed():
        return None
    raw = system.get("secureDeviceAddress")
    try:
        decoded = base64.b64decode(str(raw), validate=True) if raw else b""
    except (ValueError, TypeError):
        return None   # not an address the games can use at all
    if len(decoded) != 18:
        return None   # (the games only use 18-byte addresses: 16-byte IPv6 + port)
    base = base_of(xuid)
    if base is not None and decoded[:16] == relay_virtual_address(base):
        return None
    return "not their relay address"


def peer_address_checked(value, xuid):
    """value without a secureDeviceAddress peer_address_refusal refuses (logged); value itself otherwise."""
    why = peer_address_refusal(value, xuid) if isinstance(value, dict) else None
    if why is None:
        return value
    value = copy.deepcopy(value)
    value["properties"]["system"].pop("secureDeviceAddress", None)
    print("[MPSD] %s's secureDeviceAddress left out: %s (relay mode; \"allowRealPeerAddresses\" is not set)" % (
        gamertag_for(xuid), why))
    return value


def membership_refusal(xuid):
    """Why this player may not take a place in one more session, or None: at most SESSIONS_PER_PLAYER stored sessions
    (joined or reserved) at once. Sessions persist without a realtime connection, so without a limit one player could
    make thousands, and every walk of them (/me, the Guide's Join, matchmaking) grew with them."""
    base = base_of(xuid)
    if base is None:
        return None
    low, count = int(base), 0
    for _path, session in iter_sessions(HD_SCID):
        members = session.get("members") if isinstance(session.get("members"), dict) else {}
        if any((lambda x: x and x.isdigit() and low <= int(x) < low + XUIDS_PER_PLAYER)(member_xuid(members[k]))
               for k in member_keys(members)):
            count += 1
            if count >= SESSIONS_PER_PLAYER:
                return "you are in %d sessions already - leave one first" % count
    return None


def hardening_summary():
    """The startup line."""
    return ("Hardening: request bodies up to %d MB; saves need the player key (%d MB per player); a player in at most %d "
            "sessions; /me, /friends/join, /news/seen %d at once then %g a second per player; relay-mode peer addresses "
            "%s; Setup downloads %d per player and %d per address an hour" % (
                REQUEST_BODY_MAX_BYTES >> 20, TITLE_STORAGE_QUOTA_BYTES >> 20, SESSIONS_PER_PLAYER, RATE_BURST,
                RATE_PER_SECOND, "real ones allowed (allowRealPeerAddresses)" if peer_real_addresses_allowed()
                else "own relay address only", KEYED_DOWNLOADS_PER_HOUR, ADDRESS_DOWNLOADS_PER_HOUR))


def title_storage_file(xuid, subpath):
    # Saves live in data/<xuid>/. Only a real XUID may name that folder: safe_join stops "..", but "." or a
    # folder name like "_handles" would still land in data/ itself (and _players.json holds every secret).
    if not str(xuid).isdigit():
        return None
    return safe_join(DATA, str(xuid), unquote(subpath))


def session_file(scid, template_name, session_name):
    return safe_join(
        SESSION_DATA,
        scid,
        template_name,
        f"{session_name}.json"
    )


def handle_file(handle_id):
    return safe_join(HANDLE_DATA, f"{handle_id}.json")


def load_json(path, default=None):
    if not path or not os.path.isfile(path):
        return default

    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path, data):
    if not path:
        return False

    os.makedirs(os.path.dirname(path), exist_ok=True)

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    os.replace(tmp, path)
    session_file_changed(path)   # (hardening: the session walk's parsed copy; any other file is not in it)
    return True


def deep_merge(base, patch):
    # Merge nested dictionaries; null removes keys and other values replace them.
    if not isinstance(base, dict):
        base = {}

    if not isinstance(patch, dict):
        return copy.deepcopy(patch)

    result = copy.deepcopy(base)

    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)

    return result


def member_xuid(member):
    if not isinstance(member, dict):
        return None

    return str(
        member.get("constants", {})
        .get("system", {})
        .get("xuid", "")
    ) or None


def delete_handles_for_session(scid, template_name, session_name):
    if not os.path.isdir(HANDLE_DATA):
        return

    for filename in list(os.listdir(HANDLE_DATA)):
        if not filename.endswith(".json"):
            continue

        path = os.path.join(HANDLE_DATA, filename)
        handle = handle_shared(path)   # (tuning_patch: read-only here)

        if not isinstance(handle, dict):
            continue

        ref = handle.get("sessionRef", {})

        if (
            ref.get("scid") == scid
            and ref.get("templateName") == template_name
            and ref.get("name") == session_name
        ):
            try:
                os.remove(path)
            except OSError:
                pass


def iter_sessions(scid, template_name=None):
    root = safe_join(SESSION_DATA, scid)

    if not root or not os.path.isdir(root):
        return []

    records = []

    # (hardening) Each file is parsed again only when it changed (session_parsed): a walk was a full read and parse of
    # every stored session, several times per /windurango/me. The sessions are shared: callers only read them.
    for current_root, _, files in os.walk(root):
        for filename in files:
            if not filename.endswith(".json"):
                continue

            path = os.path.join(current_root, filename)
            session, mtime = session_parsed(path)

            if not isinstance(session, dict):
                continue

            ref = session.get("sessionRef", {})
            found_template = ref.get("templateName")

            if template_name and found_template != template_name:
                continue

            records.append((mtime, path, session))

    # Return newest sessions first to reduce stale-state reuse.
    records.sort(key=lambda item: item[0], reverse=True)

    return [(path, session) for _mtime, path, session in records]


def build_session_query_result(session, query_xuid=None):
    ref = session.get("sessionRef", {})

    constants_system = (
        session.get("constants", {})
        .get("system", {})
    )

    properties_system = (
        session.get("properties", {})
        .get("system", {})
    )

    info = session.get("membersInfo", {})

    result = {
        "startTime": session.get("startTime", utc_now()),
        "sessionRef": ref,
        "accepted": int(info.get("accepted", info.get("count", 0))),
        "visibility": constants_system.get("visibility", "open"),
        "keywords": properties_system.get("keywords", [])
    }

    if query_xuid is not None:
        result["xuid"] = str(query_xuid)
        result["status"] = "active"

    join_restriction = properties_system.get("joinRestriction")
    if join_restriction:
        result["joinRestriction"] = join_restriction

    return result


def load_session_from_ref(session_ref):
    if not isinstance(session_ref, dict):
        return None

    scid = session_ref.get("scid")
    template_name = session_ref.get("templateName")
    session_name = session_ref.get("name")

    if not scid or not template_name or not session_name:
        return None

    return load_json(session_file(scid, template_name, session_name))


def save_handle(handle):
    return save_json(handle_file(handle["id"]), handle)


def load_handle(handle_id):
    return load_json(handle_file(handle_id))


def replace_activity_handle(handle):
    # Replace the existing activity handle for the same local user.
    owner_xuid = str(handle.get("ownerXuid", ""))

    for filename in list(os.listdir(HANDLE_DATA)):
        if not filename.endswith(".json"):
            continue

        path = os.path.join(HANDLE_DATA, filename)
        old = handle_shared(path)   # (tuning_patch: read-only here)

        if not isinstance(old, dict):
            continue

        if (
            old.get("type") == "activity"
            and str(old.get("ownerXuid", "")) == owner_xuid
        ):
            try:
                os.remove(path)
            except OSError:
                pass

    save_handle(handle)


def stamp_session_change(session, previous):
    # MPSD versions every write; RTA shoulder taps carry the branch and change number.
    previous = previous if isinstance(previous, dict) else {}
    session["branch"] = previous.get("branch") or session.get("branch") or str(uuid.uuid4())
    session["changeNumber"] = int(previous.get("changeNumber", 0) or 0) + 1


def session_connection_ids(session):
    ids = set()

    members = session.get("members", {}) if isinstance(session, dict) else {}
    if not isinstance(members, dict):
        return ids

    for member in members.values():
        if not isinstance(member, dict):
            continue

        connection = (
            member.get("properties", {})
            .get("system", {})
            .get("connection")
        )
        if connection:
            ids.add(str(connection).lower())

    return ids


async def rta_send(websocket, frame):
    try:
        await websocket.send(frame)
    except Exception as exc:
        print("[RTA] SEND FAILED:", repr(exc))


def notify_session_changed(session, previous=None):
    # Tell every subscribed member (before and after the write) that the session changed.
    # Titles wait for these taps before continuing lobby and matchmaking flows.
    ref = session.get("sessionRef", {})
    tap = {
        "resource": "%s~%s~%s" % (ref.get("scid"), ref.get("templateName"), ref.get("name")),
        "changeNumber": session.get("changeNumber", 0),
        "branch": session.get("branch", "")
    }

    targets = session_connection_ids(session) | session_connection_ids(previous)
    batch = [] if tuning("fastPaths") else None   # (tuning_patch: one event-loop wake-up for this change's taps)
    quiet = tuning("quietLogs")

    for connection_id in sorted(targets):
        entry = RTA_CONNECTIONS.get(connection_id)

        if entry is None or RTA_LOOP is None:
            print("[RTA] NO LIVE CONNECTION FOR TAP:", connection_id)
            continue

        websocket, subscription_id = entry
        frame = json.dumps(
            [3, subscription_id, {"shoulderTaps": [tap]}],
            separators=(",", ":")
        )
        if not quiet:   # (tuning_patch)
            print(f"[RTA] {utc_now()} TAP -> {connection_id} {frame}")

        if batch is not None:   # (tuning_patch: sent below, all from one callback)
            batch.append((websocket, frame))
            continue

        # Send shortly after the HTTP response so the title has applied its own write first.
        RTA_LOOP.call_soon_threadsafe(
            lambda ws=websocket, f=frame: RTA_LOOP.call_later(
                0.25,
                lambda: asyncio.ensure_future(rta_send(ws, f))
            )
        )
    if batch:   # (tuning_patch) the same frames, 0.25 s after the event loop takes them, one wake-up for all
        RTA_LOOP.call_soon_threadsafe(RTA_LOOP.call_later, 0.25, tuning_send_taps, batch)


def update_matchmaking_state(session_ref, status, typical_wait=1, target_ref=None):
    with STATE_LOCK:
        session = load_session_from_ref(session_ref)

        if not isinstance(session, dict):
            return

        previous = copy.deepcopy(session)

        servers = session.setdefault("servers", {})
        mm = servers.setdefault("matchmaking", {})
        props = mm.setdefault("properties", {})
        system = props.setdefault("system", {})

        system["status"] = status
        system["statusDetails"] = ""
        system["typicalWait"] = typical_wait

        if target_ref:
            system["targetSessionRef"] = target_ref
        else:
            system.pop("targetSessionRef", None)

        stamp_session_change(session, previous)
        save_json(
            session_file(
                session_ref["scid"],
                session_ref["templateName"],
                session_ref["name"]
            ),
            session
        )
    notify_session_changed(session, previous)


# ---------------------------------------------------------------------------
# Multi-player sessions.
#
# A session holds up to maxMembersCount players. XSAPI finds its own member by comparing every member's
# constants.system.xuid with its signed-in user, and reads the members as a list: from membersInfo.first it takes
# membersInfo.count members, each member's "next" naming the key of the one after it. So member keys are handed
# out once from membersInfo.next and never reused, and every response carries the whole chain, the same for
# whoever asks. A player only ever writes its own member ("me"); the members matchmaking adds for other players
# are reservations (reserved: true) that each player claims by writing "me" into the session.

def member_keys(members):
    if not isinstance(members, dict):
        return []
    return sorted((k for k, v in members.items() if str(k).isdigit() and isinstance(v, dict)), key=int)


def find_member_key(members, xuid):
    for key in member_keys(members):
        if member_xuid(members[key]) == str(xuid):
            return key
    return None


def accepted_member_keys(members):
    return [k for k in member_keys(members) if members[k].get("reserved") is not True]


def next_member_key(session):
    members = session.get("members") if isinstance(session.get("members"), dict) else {}
    info = session.get("membersInfo") if isinstance(session.get("membersInfo"), dict) else {}
    try:
        stored = int(info.get("next", 0) or 0)
    except (TypeError, ValueError):
        stored = 0
    return max([stored] + [int(k) + 1 for k in member_keys(members)])


def in_caller_block(xuid):
    """xuid is one of the calling player's own four XUIDs."""
    base = g.get("caller_base")
    return bool(base and xuid and str(xuid).isdigit() and int(base) <= int(xuid) < int(base) + XUIDS_PER_PLAYER)


def session_host_xuid(session):
    """The xuid of the joined member whose deviceToken is properties.system.host, or None."""
    host = ((session.get("properties") or {}).get("system") or {}).get("host")
    members = session.get("members") if isinstance(session.get("members"), dict) else {}
    if not host:
        return None
    for key in accepted_member_keys(members):
        xuid = member_xuid(members[key])
        if xuid and device_token_for(xuid) == host:
            return xuid
    return None


def decorate_session(session, scid, template_name, session_name):
    """The response shape of a stored multi-player session. Never changes who is in it and never depends on who
    asks, so every member reads the same document."""
    session = copy.deepcopy(session) if isinstance(session, dict) else {}
    session["contractVersion"] = 107
    if "correlationId" not in session or not tuning("fastPaths"):   # (tuning_patch: no uuid4 for one that is there)
        session.setdefault("correlationId", str(uuid.uuid4()))
    session["sessionRef"] = {"scid": scid, "templateName": template_name, "name": session_name}
    constants = session.setdefault("constants", {})
    constants.setdefault("system", {})
    constants.setdefault("custom", {})
    max_members = template_max_members(scid, template_name)
    if max_members:
        constants["system"]["maxMembersCount"] = max_members
    properties = session.setdefault("properties", {})
    properties.setdefault("system", {})
    properties.setdefault("custom", {})

    next_key = next_member_key(session)
    raw = session.get("members") if isinstance(session.get("members"), dict) else {}
    keys = member_keys(raw)
    members = {k: raw[k] for k in keys}
    for position, key in enumerate(keys):
        member = members[key]
        member.setdefault("constants", {}).setdefault("system", {})["index"] = int(key)
        member["constants"].setdefault("custom", {})
        member.setdefault("properties", {}).setdefault("system", {})
        member["properties"].setdefault("custom", {})
        xuid = member_xuid(member)
        if xuid:
            member["deviceToken"] = device_token_for(xuid)
            member["gamertag"] = gamertag_for(xuid)
        member.setdefault("nat", "open")
        if member.get("reserved") is not True:
            member.pop("reserved", None)
        member["next"] = int(keys[position + 1]) if position + 1 < len(keys) else next_key

    accepted = accepted_member_keys(members)
    session["members"] = members
    session["membersInfo"] = {
        "first": int(keys[0]) if keys else 0,
        "next": next_key,
        "count": len(keys),
        "accepted": len(accepted),
        "active": sum(1 for k in accepted if members[k]["properties"]["system"].get("active") is True),
    }
    session.setdefault("startTime", utc_now())
    session.setdefault("servers", {})
    return session


def verified_caller_xuid():
    """The caller's XUID when the request proves it (the WD1 secret), else None."""
    return caller_xuid() if g.get("caller_verified") is True else None


def caller_member_key(session):
    """The key of the verified caller's joined (not reserved) member in this session, or None."""
    if not verified_caller_xuid() or not isinstance(session, dict):
        return None
    members = session.get("members") if isinstance(session.get("members"), dict) else {}
    for key in accepted_member_keys(members):
        if in_caller_block(member_xuid(members[key])):
            return key
    return None


def join_refusal(session, members, xuid):
    """Why this player may not take a new place (one it holds no reservation for) in the session, or None.
    Session membership is what lets players reach each other through the peer relay, so this is MPSD's own rule:
    nobody joins a closed session; joinRestriction "local" lets in only the members' own device (the same
    player's other local users), "followed" also a player a joined member has as a friend. The first member of
    a new session is always let in, and a reservation (matchmaking's way in) is always honoured."""
    if not member_keys(members):
        return None
    system = ((session.get("properties") or {}).get("system") or {}) if isinstance(session, dict) else {}
    if system.get("closed") is True:
        return "the session is closed"
    fps_refused = fps_join_refusal(session, members, xuid)   # (fps_split v3) 30 and 60 fps play separately
    if fps_refused:
        return fps_refused
    restriction = system.get("joinRestriction")
    if restriction not in ("local", "followed"):
        return None
    base = base_of(xuid)
    member_bases = {base_of(member_xuid(members[k])) for k in accepted_member_keys(members)} - {None}
    if base in member_bases:
        return None
    if restriction == "followed" and base:
        with STATE_LOCK:
            if any(primary_xuid(base) in FRIENDS.get(primary_xuid(other), ()) for other in member_bases):
                return None
    return "joinRestriction is %s" % restriction


def apply_member_writes(existing, incoming, caller, max_members):
    """The members part of a session PUT, done for the caller only: "me" (or a key holding one of the caller's
    own XUIDs) is merged into that player's member, which is created at membersInfo.next when it has none (if
    join_refusal lets it in), or taken over from its reservation. Writes to other players' members are ignored.
    Returns (session with the member changes, the rest of the body, a refusal response or None)."""
    session = copy.deepcopy(existing) if isinstance(existing, dict) else {}
    rest = copy.deepcopy(incoming) if isinstance(incoming, dict) else {}
    patch = rest.pop("members", None)
    next_key = next_member_key(session)
    raw = session.get("members") if isinstance(session.get("members"), dict) else {}
    members = {k: raw[k] for k in member_keys(raw)}

    for key, value in (patch.items() if isinstance(patch, dict) else []):
        if not caller:
            # in_caller_block goes by the claimed xuidBase: without the secret nobody writes any member.
            return session, rest, json_response({"code": 401, "description": "sign in to join sessions"}, 401)
        if key == "me":
            named = member_xuid(value)
            xuid = named if in_caller_block(named) else caller
        elif key in members and in_caller_block(member_xuid(members[key])):
            xuid = member_xuid(members[key])
        else:
            print("[MPSD] %s may not write member %s - ignored" % (gamertag_for(caller) if caller else "?", key))
            continue
        if not xuid:
            return session, rest, json_response({"code": 401, "description": "sign in to join sessions"}, 401)

        own = find_member_key(members, xuid)
        if value is None:
            if own is not None:
                members.pop(own)
                print("[MPSD] %s left (member %s)" % (gamertag_for(xuid), own))
            continue
        if not isinstance(value, dict):
            continue

        was_reserved = own is not None and members[own].get("reserved") is True
        if was_reserved:
            # (fps_split v3) The rate this claim says, checked against the JOINED players' as join_refusal checks a
            # new member: a switch after the place was written gets the same 403, and the place is freed at once.
            # Other players' reserved places do not count here: each is checked when its own player claims it.
            fps_refused = fps_claim_refusal(existing, members, own, xuid)
            if fps_refused:
                return session, rest, fps_claim_refused(existing, xuid, fps_refused)
        if own is None:
            too_many = membership_refusal(xuid)   # (hardening)
            if too_many:
                print("[MPSD] %s cannot join: %s" % (gamertag_for(xuid), too_many))
                return session, rest, json_response({"code": 409, "description": too_many}, 409)
            refused = join_refusal(existing, members, xuid)
            if refused:
                print("[MPSD] %s cannot join %s: %s" % (gamertag_for(xuid),
                                                         (session.get("sessionRef") or {}).get("name"), refused))
                return session, rest, json_response({"code": 403, "description": refused}, 403)
            if max_members and len(members) >= max_members:
                print("[MPSD] %s cannot join: the session is full (%d)" % (gamertag_for(xuid), len(members)))
                return session, rest, json_response({"code": 409, "description": "the session is full"}, 409)
            own = str(next_key)
            next_key += 1
            members[own] = {}
            print("[MPSD] %s joined as member %s" % (gamertag_for(xuid), own))
        elif was_reserved:
            print("[MPSD] %s took its reserved place (member %s)" % (gamertag_for(xuid), own))

        member = deep_merge(members[own], peer_address_checked(value, xuid))   # (hardening: relay-mode addresses)
        member.setdefault("constants", {}).setdefault("system", {})["xuid"] = xuid
        member.pop("reserved", None)
        if was_reserved or not member.get("joinTime"):
            member["joinTime"] = utc_now()
        member.setdefault("properties", {}).setdefault("system", {}).setdefault("active", True)
        members[own] = member

    session["members"] = members
    info = session.get("membersInfo") if isinstance(session.get("membersInfo"), dict) else {}
    session["membersInfo"] = dict(info, next=next_key)
    return session, rest, None


def guard_host_write(existing, rest):
    """The game writes properties.system.host with If-Match: *, so the last writer would win. Keep a host that
    is still in the session: only it may change the host; anyone may take it once that player has left."""
    system = (rest.get("properties") or {}).get("system") if isinstance(rest.get("properties"), dict) else None
    if not isinstance(system, dict) or "host" not in system:
        return
    current = session_host_xuid(existing)
    if current is None or in_caller_block(current):
        return
    print("[MPSD] %s tried to take over as host from %s - kept %s" % (
        gamertag_for(caller_xuid()), gamertag_for(current), gamertag_for(current)))
    system.pop("host")


def log_members(session):
    if tuning("quietLogs"):   # (tuning_patch)
        return
    members = session.get("members") or {}
    host = session_host_xuid(session)
    parts = []
    for key in member_keys(members):
        member = members[key]
        xuid = member_xuid(member)
        flags = [flag for flag, on in (("reserved", member.get("reserved") is True), ("host", xuid == host),
                                       ("no connection", not member.get("properties", {}).get("system", {})
                                        .get("connection") and member.get("reserved") is not True)) if on]
        parts.append("%s %s%s" % (key, gamertag_for(xuid), " (%s)" % ", ".join(flags) if flags else ""))
    print("[MPSD] members:", "; ".join(parts) or "none")


def remove_session(scid, template_name, session_name, path):
    if path and os.path.isfile(path):
        os.remove(path)
    session_file_changed(path)   # (hardening)
    delete_handles_for_session(scid, template_name, session_name)
    MM_LOBBIES.pop("%s~%s~%s" % (scid, template_name, session_name), None)
    relay_session_removed(scid, template_name, session_name)


def write_session_multi(scid, template_name, session_name, incoming):
    """PUT of a multi-player session (and of one reached through a handle)."""
    path = session_file(scid, template_name, session_name)
    if not path:
        return Response(status=400)
    caller = verified_caller_xuid()
    with STATE_LOCK:
        if tuning("fastPaths"):   # (tuning_patch) the old copy is only read here (apply_member_writes copies it)
            existing = session_shared(path)
            if existing is None:
                existing = {}
        else:
            existing = load_json(path, {})
        was_new = not os.path.isfile(path)
        session, rest, refusal = apply_member_writes(existing, incoming, caller,
                                                     template_max_members(scid, template_name))
        if refusal is not None:
            return refusal
        if (rest and isinstance(existing, dict) and member_keys(existing.get("members"))
                and caller_member_key(existing) is None
                and caller_member_key(session) is None):
            # Only a member changes a session that has members (a joining player may write in the same PUT):
            # otherwise anyone could open a closed session (closed, joinRestriction) and then join it.
            print("[MPSD] %s is not in %s and may not change it - refused" % (
                gamertag_for(caller) if caller else "a caller with no identity", session_name))
            return json_response({"code": 403, "description": "only the session's members may change it"}, 403)
        guard_host_write(existing, rest)
        merged = decorate_session(deep_merge(session, rest), scid, template_name, session_name)
        stamp_session_change(merged, existing)
        if not merged["members"]:
            remove_session(scid, template_name, session_name, path)
            print("[MPSD] Session has no members; deleted:", session_name)
            deleted = True
        else:
            save_json(path, merged)
            relay_session_changed(merged)
            deleted = False
    notify_session_changed(merged, existing)
    if deleted:
        return Response(status=204)
    log_members(merged)
    response = json_response(merged, 201 if was_new else 200)
    response.headers["ETag"] = session_etag(merged)
    return response


def remove_members_of_connection(connection_id):
    """A game's realtime connection closed and did not come back: take its players out of every session (as MPSD
    does for members whose connection goes away), so nobody waits for them."""
    connection_id = str(connection_id).lower()
    if connection_id in RTA_CONNECTIONS:
        return   # it reconnected with the same id
    RTA_CONNECTION_OWNERS.pop(connection_id, None)
    for path, _ in iter_sessions(HD_SCID):
        with STATE_LOCK:
            session = load_json(path)
            if not isinstance(session, dict):
                continue
            ref = session.get("sessionRef") or {}
            members = session.get("members") if isinstance(session.get("members"), dict) else {}
            gone = [k for k in accepted_member_keys(members) if str(members[k].get("properties", {})
                    .get("system", {}).get("connection", "")).lower() == connection_id]
            if not gone:
                continue
            previous = copy.deepcopy(session)
            for key in gone:
                print("[MPSD] %s disconnected; removed from %s" % (
                    gamertag_for(member_xuid(members[key])), ref.get("name")))
                members.pop(key)
            session["members"] = members
            session = decorate_session(session, ref.get("scid"), ref.get("templateName"), ref.get("name"))
            stamp_session_change(session, previous)
            if session["members"]:
                save_json(path, session)
                relay_session_changed(session)
            else:
                remove_session(ref.get("scid"), ref.get("templateName"), ref.get("name"), path)
        notify_session_changed(session, previous)


RTA_GONE_GRACE_SECONDS = 20


def connection_released(connection_id):
    timer = threading.Timer(RTA_GONE_GRACE_SECONDS, remove_members_of_connection, args=(connection_id,))
    timer.daemon = True
    timer.start()


# ---------------------------------------------------------------------------
# Multi-player SmartMatch.
#
# Happy Dungeons matchmakes in two roles (ticketAttributes.ToBeHost). A host (preserveSession "always", ToBeHost
# 1000) keeps its own session as the lobby and posts a new ticket from it every few seconds while it waits for
# players. A seeker (preserveSession "never", ToBeHost 0) looks for a lobby: its players are put into an open host
# lobby as reservations and its ticket is answered "found" with that lobby as the target; the game then claims
# the reservation (members.me). Its game stays member 0 of its own ticket session until it leaves that lobby (it
# leaves both at once), so that session stops being a lobby of its own the moment the seeker is placed (lobby_patch
# v1, "mmDropPlacedSeekerLobby"). A host's ticket always resolves to its own session.
#
# A small group rarely searches for the same dungeon at the same moment (MM2, from the friends' first evening: 155
# searches, every one for a specific mission, and the only host/seeker overlap was for different missions), so:
# - a seeker who picked a mission only goes into a lobby for that mission (or one with no mission yet); an "any
#   dungeon" seeker goes into any lobby and its reservation carries the lobby's Stage (MM_STAGE_POLICY "strict",
#   lobby_patch v1). Until then a seeker went into ANY open lobby when none played its mission ("prefer"), but the
#   game keeps playing the mission its player picked (it takes the lobby's only for "any dungeon", exe 0x12d9e50):
#   on the public launch day 224 of 365 placements were into another mission's lobby, 2% of them played, and they
#   held the places a real partner could have used. The players page's Join and a friend's Join follow the same rule;
# - a seeker's own ticket session is itself a joinable lobby (kind "seeker"), registered the moment it searches;
#   with nobody to join it is answered "found" with that same session - the game then stays where it is instead of
#   hopping to a new session every few seconds (it only leaves a session when the target has another name), takes
#   the host role there and keeps searching from it, so the next seeker can be reserved into it;
# - a first search is held SEEKER_HOLD_SECONDS before that answer, so a host who is opening a lobby gets a chance;
# - placement order: the lobby picked on the players page, then host lobbies, then the same mission, then more
#   players, then the oldest; a seeker alone in its own lobby only moves to a host lobby or a strictly better seeker
#   lobby (more players, or as many and older), so two seekers never swap places;
# - only players with the same game data (the version value, see game_version below) are put together.
MM_TICKETS = {}              # ticket id -> ticket record (momatch_create_ticket)
MM_LOBBIES = {}              # "scid~template~name" -> {"ref", "kind", "stage", "hopper", "owner", "since", "refreshed",
                             #                          "version"}
MM_STAGE_POLICY = "strict"   # (lobby_patch v1; was "prefer") the default of server_config.json "mmStagePolicy" (see
                             # there): "strict" that mission or none, "exact" that mission, "prefer"/"any" else any lobby
MM_DROP_PLACED_SEEKER_LOBBY = True   # (lobby_patch v1) the default of server_config.json "mmDropPlacedSeekerLobby"
MM_FPS_SPLIT = True          # (fps_split v3) the default of server_config.json "mmFpsSplit" (false: the kill switch)
MM_FPS_UNKNOWN = "own"       # (fps_split v3) the default of server_config.json "mmFpsUnknown": "own" / "30" / "solo"
SEEKER_HOLD_SECONDS = 10     # a first search waits this long for a lobby (the game gives up after 25-30 s)
LOBBY_FRESH_SECONDS = 12     # a host re-tickets about every 4 s; a lobby not refreshed for this long is gone
RESERVATION_SECONDS = 30     # a reserved place nobody takes within this long is freed again
JOIN_WISHES = {}             # str(xuidBase) -> {"lobby": key, "until": time}, from the players page
JOIN_WISH_SECONDS = 300
# A seeker that already sits in another player's lobby and asks again (the game re-tickets from the session it
# joined): "hold" keeps its ticket searching, so it stays where it is while the host gets ready.
BACKFILL_POLICY = "hold"


def ref_key(ref):
    return "%s~%s~%s" % (ref.get("scid"), ref.get("templateName"), ref.get("name"))


def ticket_role(body):
    attrs = body.get("ticketAttributes") if isinstance(body.get("ticketAttributes"), dict) else {}
    try:
        to_be_host = int(attrs.get("ToBeHost", 0) or 0)
    except (TypeError, ValueError):
        to_be_host = 0
    return "host" if str(body.get("preserveSession", "")).lower() == "always" or to_be_host >= 1000 else "seeker"


def matchmaking_status(session):
    return (((session.get("servers") or {}).get("matchmaking") or {}).get("properties") or {}) \
        .get("system", {}).get("status")


def lobby_size(ref):
    return template_max_members(ref["scid"], ref["templateName"]) or 4


def concrete_stage(stage):
    """"mission_3_1" for a player who picked a dungeon; None for "any dungeon" (the game sends negative numbers)."""
    return str(stage) if re.fullmatch(r"mission_\d+_\d+", str(stage or "")) else None


def stages_match(lobby_stage, seeker_stage):
    """The lobby is for the dungeon the seeker picked (or one of them takes any dungeon, and the game then reads the
    dungeon from the lobby's own League/Difficulty properties)."""
    wanted = concrete_stage(seeker_stage)
    return wanted is None or concrete_stage(lobby_stage) in (None, wanted)


def mm_stage_policy():
    """(lobby_patch v1) server_config.json "mmStagePolicy", else MM_STAGE_POLICY."""
    return tuning_value("mmStagePolicy")


def stage_allowed(lobby_stage, seeker_stage):
    """May a seeker be put into a lobby for this stage at all ("mmStagePolicy")? "strict": a lobby for the seeker's
    mission or one with no mission; "exact": the seeker's mission only; "prefer" / "any": any lobby. A seeker for "any
    dungeon" may go into every lobby."""
    policy = mm_stage_policy()
    if policy == "exact":
        wanted = concrete_stage(seeker_stage)
        return wanted is None or concrete_stage(lobby_stage) == wanted
    return policy != "strict" or stages_match(lobby_stage, seeker_stage)


def mm_stage_hint(lobby_stage):
    """(lobby_patch v1) What a Join's instructions add while the policy keeps a player out of another mission's lobby:
    " for Mission 2-0 (or any dungeon)"; "" for a lobby with no mission, and under "prefer" / "any"."""
    if concrete_stage(lobby_stage) and mm_stage_policy() in ("strict", "exact"):
        return " for %s (or any dungeon)" % stage_text(lobby_stage)
    return ""


def lobby_kind(lobby):
    return lobby.get("kind") or "host"


def own_seeker_lobby(ticket):
    """The seeker's ticket session is its own (nobody else hosts it): the lobby other seekers can be put into."""
    return not seeker_in_someones_lobby(ticket)


def register_seeker_lobby(ticket, now):
    """Makes (or refreshes) the seeker's own ticket session a joinable lobby. Returns True when it was new."""
    key = ref_key(ticket["ref"])
    lobby = MM_LOBBIES.get(key)
    new = lobby is None or lobby_kind(lobby) != "seeker"
    if new:
        lobby = MM_LOBBIES[key] = {"ref": copy.deepcopy(ticket["ref"]), "kind": "seeker", "since": now}
    lobby.update(stage=ticket["stage"], hopper=ticket["hopper"], owner=ticket["owner"], refreshed=now,
                 version=ticket.get("version"), fps=ticket.get("fps"))   # (fps_split v3)
    return new


def session_has_others(ticket):
    """Another player is (or is about to be) in the ticket's session with its owner: the group stays together
    there and is never moved to another lobby (the other players' games would not follow)."""
    session = session_shared_from_ref(ticket["ref"])   # (tuning_patch: read-only)
    if not isinstance(session, dict):
        return False
    owner = str(base_of(ticket["owner"]))
    members = session.get("members") if isinstance(session.get("members"), dict) else {}
    return any(str(base_of(member_xuid(members[k]))) != owner for k in member_keys(members))


def open_lobby(key, now=None):
    """(lobby record, its session, free places) for a host lobby that can take players now, else None. A closed
    lobby takes nobody: the host closes it while setting up and for good when the dungeon starts."""
    lobby = MM_LOBBIES.get(key)
    if not lobby or (now or time.time()) - lobby["refreshed"] > LOBBY_FRESH_SECONDS:
        return None
    if solo_xuid(lobby.get("owner")):   # (solo_patch v1) solo play: this lobby takes nobody, whoever asks
        return None
    session = session_shared_from_ref(lobby["ref"])   # (tuning_patch: callers only read it; reserve_into_lobby copies)
    if not isinstance(session, dict):
        return None
    if solo_xuid(session_host_xuid(session)):   # (solo_patch v1) the same for whoever hosts it now
        return None
    if ((session.get("properties") or {}).get("system") or {}).get("closed") is True:
        return None
    members = session.get("members") if isinstance(session.get("members"), dict) else {}
    free = lobby_size(lobby["ref"]) - len(member_keys(members))
    if free <= 0 or not accepted_member_keys(members):
        return None
    return lobby, session, free


def lobby_fits(lobby, ticket):
    return (lobby["ref"]["scid"] == ticket["ref"]["scid"] and lobby["hopper"] == ticket["hopper"]
            and lobby["ref"]["templateName"] == ticket["ref"]["templateName"])


# Game data. Happy Dungeons only plays with players whose game data (DungeonPatch, JsonDiff, EventExtra and
# EventCampaign - the event) is the same: it hashes them into one "version value", sends it in every ticket
# (ticketAttributes.Division, the 6th "_" field: "1_4_3_0_0_<value>_0") and writes it into its session
# (properties.custom.InviteVersion), and it refuses to join a session whose InviteVersion is not its own (exe
# 0x11C7E93) - after the player waited for that match. So matchmaking only puts a player into a lobby with the same
# value. A value that cannot be read (no Division, an odd one) matches anything: the game still checks for itself.
# Everyone on the same ServerData rev of this server (/windurango/serverdata) has the same value.
LAST_TICKET_VERSION = {}     # str(xuidBase) -> the version value in that player's last ticket (for the players page)
VERSION_SKIPS_LOGGED = {}    # (seeker's xuidBase, lobby key) -> when "not put there: other game data" was last logged


def game_version(value):
    """The version value as an unsigned 32-bit number (taken signed or unsigned, as a number or a string), or None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, str) and re.fullmatch(r"\s*-?\d{1,10}\s*", value):
        value = int(value)
    if isinstance(value, int) and -2 ** 31 <= value < 2 ** 32:
        return value & 0xFFFFFFFF
    return None


def division_version(division):
    """"1_4_3_0_0_1486770622_0" -> 1486770622; None when there is no readable 6th field."""
    parts = str(division).split("_") if isinstance(division, (str, int)) else []
    return game_version(parts[5]) if len(parts) >= 6 else None


def session_version(session):
    """The session's properties.custom.InviteVersion - what a joining game compares with its own - or None."""
    properties = session.get("properties") if isinstance(session, dict) else None
    custom = properties.get("custom") if isinstance(properties, dict) else None
    return game_version(custom.get("InviteVersion")) if isinstance(custom, dict) else None


def lobby_version(lobby, session=None):
    """A lobby's version value: its session's InviteVersion, else the one in its owner's last ticket."""
    if session is None:
        session = session_shared_from_ref(lobby["ref"])   # (tuning_patch: read-only)
    version = session_version(session)
    return version if version is not None else lobby.get("version")


def toylogic_hash(data):
    """The title's string hash (exe 0x12BF8E0), for naming version values."""
    current, previous = 0x12A3FE2D, 0x37ABE8F9
    for c in data:
        mixed = (((c * 0x11763) ^ current) + previous) & 0xFFFFFFFF
        previous = current
        current = (mixed - 0x7FFFFFFF) & 0xFFFFFFFF if mixed & 0x80000000 else mixed
    return current


GAME_DATA_NAMES = {}


def game_data_text(version):
    """"no event" / "event build 1" / "game data 12345" for a version value (None for None). The value is the hash of
    the v1.15 builds of the DungeonPatch and event package a game loaded (-1 for none; slot hashes "0"):
    1486770622 is no event (every install before the v1.15 EventCampaign.pak), 128140489 is event build 1."""
    if version is None:
        return None
    if not GAME_DATA_NAMES:
        def value(event_build):
            return toylogic_hash(b"%04d%02d%03d%03d%03d%08x%08x" % (15, 0, -1, event_build, -1,
                                                                   toylogic_hash(b"0000"), 0))
        names = {value(build): "event build %d" % build for build in range(999, -1, -1)}
        names[value(-1)] = "no event"
        GAME_DATA_NAMES.update(names)
    return GAME_DATA_NAMES.get(version, "game data %d" % version)


def same_game_data(lobby, session, ticket, now=None):
    """The ticket's players have the lobby's game data (or one of the two is not known). Logs a lobby it rules out,
    once a minute per seeker and lobby."""
    if session is None:
        session = session_shared_from_ref(lobby["ref"])   # (tuning_patch: read-only)
    mine, theirs = ticket.get("version"), lobby_version(lobby, session)
    if mine is None or theirs is None or mine == theirs:
        return True
    now = now or time.time()
    key = (str(base_of(ticket["owner"])), ref_key(lobby["ref"]))
    if now - VERSION_SKIPS_LOGGED.get(key, 0) >= 60:
        for old in [k for k, at in VERSION_SKIPS_LOGGED.items() if now - at >= 60]:
            del VERSION_SKIPS_LOGGED[old]
        VERSION_SKIPS_LOGGED[key] = now
        host = session_host_xuid(session) if isinstance(session, dict) else None
        print("[MM] %s has other game data (%s) than %s's lobby (%s): not put there" % (
            gamertag_for(ticket["owner"]), game_data_text(mine), gamertag_for(host or lobby["owner"]),
            game_data_text(theirs)))
    return False


# ---------------------------------------------------------------------------------------------------------------------
# fps_split v3 - 30 and 60 fps players are matched separately.
#
# WHY. The game is strictly frame-locked (one logic step per rendered frame), and WinDurango can run it at 60 fps
# instead of 30 (the Guide's Frame rate row). Step-counted timers then run on different clocks in one session - the
# rush-attack window alone is 14 steps - so a 30 and a 60 fps player do not belong in one lobby or dungeon.
#
# THE SIGNAL. A new WinDurango build adds "X-WinDurango-Fps: 30|60" to every request it redirects here, and to the
# Guide's own calls, built per request from the game's live frame rate, so the server always sees the rate the game
# runs at now (the Guide can switch it while the player is not in a group). identify_caller keeps it per player in
# PLAYER_FPS, for verified callers only. A request WITHOUT it that came to the session directory or SmartMatch
# (X-Original-Host) is an older build and is recorded as "old" - unless that player's game sent the header within
# FPS_HEADER_STICKY_SECONDS (a new build is never taken for an old one). Other requests say nothing about the rate.
#
# THE POOLS (fps_key). 30 and 60 are pools of their own. An older build, by server_config.json "mmFpsUnknown":
#   "own"   (default) older builds are a pool of their own: they meet each other exactly as before this patch;
#   "30"    older builds count as 30 fps;
#   "solo"  older builds are matched with nobody (their search ends in their own lobby, played alone).
# A player not seen since the server started passes (the way same_game_data lets an unknown version pass).
# "mmFpsSplit": false is the kill switch: every gate below passes and players are matched exactly as before.
#
# THE GATES sit where same_game_data already does: place_seeker (the players page's lobby wish and the candidate walk;
# the host pass, lobbies_join and friends_join place through it), place_friend_seeker (a friend Join waits, and says
# why), join_friend_state (friends[].join and POST /windurango/friends/join: "otherFps"), invite_friend_state and
# invite_landable (Invite and accept: "otherFps"; no reservation is written), lobbies_join (409 sameFps false) and
# join_refusal (a direct PUT of members/me into a session with the other rate's players: 403). Matchmaking and invites
# decide before they write a reservation, and (v2) apply_member_writes checks it again when its player claims it
# (fps_claim_refusal): the rate that claim says may be another than the one the place was written for - the Guide
# refuses a switch while /me says inGroup, but its last /me poll may be older than the place. Such a claim gets
# join_refusal's 403 and the place is freed at once (fps_release_claim), so the lobby does not keep it for
# RESERVATION_SECONDS; the player's game then searches again, and at the new rate. (v3) That claim is checked against
# the players who have JOINED only: another player's reserved place is not in the lobby yet, and is checked when THAT
# player claims it (v2 checked every member, so one reserved player's switch cost a same-rate player its claim).
#
# (v3) A SWITCH THAT RACED A JOIN. When the server sees a player's rate change (fps_note_caller) or a host's lobby take
# a new rate at its re-ticket, fps_rate_changed looks at every session that player is in with other players. Joined
# players of the other rate in it with them: "[FPS] MIXED <session>: <player> now N fps, others M fps (...)", at most
# once a minute per session, player and rate (FPS_MIXED_SECONDS) - and nothing is refused or changed for the joined
# players, whose own writes go through as before (it may be a running dungeon, which a refusal would break). And every
# reserved place in it that could no longer be claimed (fps_claim_refusal) is freed at once - the switching player's
# own too (its claim would get the 403 anyway; freed now, its /me says inGroup false again at once).
#
# THE ACCEPTED RESIDUAL RACE. The server learns of a switch from the first request that carries the new rate. A place
# claimed in the moment between a player's switch in the Guide and that request is checked against the rate the server
# knows then and is let in (a 30 fps host switches to 60 while its last /me poll predates a seeker's reservation, and
# the 30 fps seeker takes its place before the host's game has sent anything at 60): 30 and 60 then share that lobby.
# Closing this sub-second window needs an atomic check-and-set - the switch and the claim decided in one place, under
# one lock - which the Guide on the player's PC and this server do not share. It is detected and logged afterwards
# instead: the switching player's next request prints the MIXED line and frees the places nobody has taken yet.
#
# VISIBLE, additively: /windurango/lobbies (fps, fpsText, sameFps per lobby - other-rate lobbies stay listed, marked;
# fps per searching player; myFps) and /windurango/me (fpsApi, fpsSplit, inGroup, you.fps, you.inGroup,
# friends[].fps, fpsPool). "inGroup" is true while the caller is in a lobby, session or dungeon with at least one other
# player (joined, or holding a place): the Guide refuses a frame-rate switch then. player_summary is not changed.
FPS_SPLIT_VERSION = "fps_split v3"
FPS_API = 1
FPS_HEADER = "X-WinDurango-Fps"
FPS_RATES = {"30": 30, "60": 60}
FPS_OLD_BUILD_HOSTS = ("sessiondirectory", "smartmatch")   # X-Original-Host of an older build's telling requests
FPS_HEADER_STICKY_SECONDS = 120   # a game that sent the header this recently is never taken for an older build
FPS_SEEN_SECONDS = 600            # the "[FPS] seen in the last 10 min" line: how often, and what it counts
FPS_SEEN_STATE = {"at": time.time()}
FPS_SEEN_LOCK = threading.Lock()
FPS_SKIP_SECONDS = 60             # "[FPS] X is not put into Y's lobby" lines are counted in windows this long:
FPS_SKIP_LINES = 20               # at most one per seeker (whatever the lobby) and this many in all in one window
# The current window: when it began, the seekers that had their line in it, the (seeker, lobby) pairs ruled out in it
# (each counted once, as v1 logged each pair once a minute), and how many of those pairs had no line of their own.
FPS_SKIP_WINDOW = {"at": 0.0, "seekers": set(), "pairs": set(), "left_out": 0}
FPS_STICKY_LOGGED = {}            # xuidBase -> when "a request without the header, still taken as N fps" was logged
FPS_POOL_CACHE = {"at": 0.0, "value": None}   # /windurango/me's fpsPool, built at most every TUNING_RECHECK_SECONDS
FPS_MIXED_SECONDS = 60            # (v3) an "[FPS] MIXED" line at most this often per session, player and rate
FPS_MIXED_LOGGED = {}             # (v3) (session key, xuidBase, rate) -> when its "[FPS] MIXED" line was printed


def fps_split_on():
    """server_config.json "mmFpsSplit" (default MM_FPS_SPLIT, true): false puts every rate together, as before."""
    return tuning_value("mmFpsSplit") is not False


def fps_label(value):
    """"30 fps" / "60 fps" / "an older build" / "a rate not known yet"."""
    if value in (30, 60):
        return "%d fps" % value
    return "an older build" if value == "old" else "a rate not known yet"


def fps_mm_text(value):
    """The rate in the "[MM] ... is hosting / is looking for a lobby (<stage>, <rate>)" lines."""
    if value in (30, 60):
        return "%d fps" % value
    return "older build" if value == "old" else "fps not known"


def fps_players_text(value):
    if value in (30, 60):
        return "%d fps players" % value
    return "players on older WinDurango builds" if value == "old" else "players at another frame rate"


def fps_note_caller(base):
    """(identify_caller, for a verified caller only) What this request says about the player's frame rate: sets
    g.caller_fps and PLAYER_FPS[base]; (v3) a change of it goes to fps_rate_changed. Never fails a request."""
    try:
        raw = request.headers.get(FPS_HEADER)
        now = time.time()
        before = PLAYER_FPS.get(base)
        if raw is not None:
            fps = FPS_RATES.get(raw.strip())
            if fps is not None:   # any other value: a rate there is no pool for - nothing is recorded
                g.caller_fps = fps
                PLAYER_FPS[base] = {"fps": fps, "at": now, "header": now}
                if before is not None and before["fps"] != fps:
                    print("[FPS] %s plays at %d fps now (was %s)" % (gamertag_for(primary_xuid(base)), fps,
                                                                    fps_label(before["fps"])))
                    fps_rate_changed(base, fps)   # (v3) a MIXED session, reserved places it can no longer take
        else:
            host = (request.headers.get("X-Original-Host") or "").lower()
            if any(word in host for word in FPS_OLD_BUILD_HOSTS):
                if before is not None and now - before.get("header", 0) < FPS_HEADER_STICKY_SECONDS:
                    if now - FPS_STICKY_LOGGED.get(base, 0) >= FPS_SEEN_SECONDS:
                        FPS_STICKY_LOGGED[base] = now
                        print("[FPS] %s's game sent a %s request without %s - still taken as %s (it sent the header "
                              "%d s ago)" % (gamertag_for(primary_xuid(base)), host, FPS_HEADER,
                                             fps_label(before["fps"]), now - before["header"]))
                else:
                    PLAYER_FPS[base] = {"fps": "old", "at": now}
                    if before is not None and before["fps"] != "old":
                        print("[FPS] %s is on an older WinDurango build now (was %s)" % (
                            gamertag_for(primary_xuid(base)), fps_label(before["fps"])))
                        fps_rate_changed(base, "old")   # (v3)
        fps_seen_line(now)
    except Exception as error:   # noqa: BLE001 - the rate is a hint; a request never fails over it
        builtins.print("[FPS] could not read the frame rate of %s's request: %r" % (base, error))


def fps_seen_line(now):
    """Every FPS_SEEN_SECONDS: how many players each pool saw, to judge the new build's adoption."""
    if now - FPS_SEEN_STATE["at"] < FPS_SEEN_SECONDS:
        return
    with FPS_SEEN_LOCK:
        if now - FPS_SEEN_STATE["at"] < FPS_SEEN_SECONDS:
            return
        FPS_SEEN_STATE["at"] = now
    counts = {30: 0, 60: 0, "old": 0}
    for record in list(PLAYER_FPS.values()):
        if now - record["at"] < FPS_SEEN_SECONDS and record["fps"] in counts:
            counts[record["fps"]] += 1
    print("[FPS] seen in the last %d min: 30=%d 60=%d old=%d (split %s, old builds: %s)" % (
        FPS_SEEN_SECONDS // 60, counts[30], counts[60], counts["old"], "on" if fps_split_on() else "off",
        tuning_value("mmFpsUnknown")))


def request_fps():
    """This request's X-WinDurango-Fps (30 or 60) from a verified caller, else None: no header (an older build, a
    browser), or not in a request at all (a timer)."""
    try:
        return g.get("caller_fps")
    except RuntimeError:
        return None


def fps_caller_base():
    try:
        return g.get("caller_base") if g.get("caller_verified") is True else None
    except RuntimeError:
        return None


def fps_of(base):
    """A player's frame rate as far as this server knows: 30, 60, "old" (an older build) or None (not seen since the
    server started). For this request's own caller, the rate this very request says."""
    if base is None:
        return None
    base = str(base)
    if base == fps_caller_base():
        mine = request_fps()
        if mine is not None:
            return mine
    record = PLAYER_FPS.get(base)
    return record["fps"] if record is not None else None


def ticket_fps(ticket):
    """A ticket's rate: its owner's as known now, else what its POST said."""
    live = fps_of(base_of(ticket.get("owner")))
    return live if live is not None else ticket.get("fps")


def lobby_fps(lobby, session=None):
    """A lobby's rate: whoever hosts its session now (else its owner) as known now, else the owner's last ticket's."""
    host = session_host_xuid(session) if isinstance(session, dict) else None
    live = fps_of(base_of(host or lobby.get("owner")))
    return live if live is not None else lobby.get("fps")


def fps_key(value):
    """The pool a rate plays in: "all" while "mmFpsSplit" is false; 30 / 60; for an older build "old", 30 or None
    (matched with nobody) by "mmFpsUnknown"; "unknown" for a rate not known yet (it passes)."""
    if not fps_split_on():
        return "all"
    if value in (30, 60) and not isinstance(value, bool):
        return value
    if value == "old":
        policy = tuning_value("mmFpsUnknown")
        return "old" if policy == "own" else 30 if policy == "30" else None
    return "unknown"


def fps_ok(a, b):
    """May players at these two rates be put together? Both in one pool, or one of them not known yet."""
    ka, kb = fps_key(a), fps_key(b)
    if ka == "unknown" or kb == "unknown":
        return True
    return ka is not None and ka == kb


def fps_clash(session, base, fps, fallback=None):
    """(their xuidBase or None, their rate) for the first player in the session - joined or holding a place, not
    base's own local users - who may not be put together with a player at fps; None when nobody clashes. A member
    whose rate is not known counts as fallback (the lobby's recorded rate)."""
    if not fps_split_on():
        return None
    members = session.get("members") if isinstance(session, dict) and isinstance(session.get("members"), dict) else {}
    base = None if base is None else str(base)
    others = False
    for key in member_keys(members):
        other = base_of(member_xuid(members[key]))
        if other is None or other == base:
            continue
        others = True
        theirs = fps_of(other)
        if theirs is None:
            theirs = fallback
        if not fps_ok(fps, theirs):
            return other, theirs
    if not others and fallback is not None and not fps_ok(fps, fallback):
        return None, fallback
    return None


def fps_clash_text(name, theirs, mine, goal="join them"):
    """The sentence for a Join or Invite refused over the rate, e.g. "Mako plays at 60 fps and you at 30 - change
    Frame rate in the Guide (Settings) to join them." (name plays at theirs, the reader at mine)."""
    older_30 = fps_key("old") == 30
    if theirs in (30, 60) and mine in (30, 60):
        return "%s plays at %d fps and you at %d - change Frame rate in the Guide (Settings) to %s." % (
            name, theirs, mine, goal)
    if mine == "old" and theirs == "old":
        return ("Older WinDurango builds are not matched with other players now - update WinDurango to %s."
                % goal)
    if mine == "old" and theirs in (30, 60):
        return "%s plays at %d fps on the new WinDurango build and you on an older one - update WinDurango to %s." % (
            name, theirs, goal)
    if theirs == "old" and mine in (30, 60):
        if older_30 and mine == 60:
            return ("%s plays on an older WinDurango build, which plays with 30 fps players, and you at 60 - change "
                    "Frame rate in the Guide (Settings) to %s." % (name, goal))
        return ("%s plays on an older WinDurango build and you at %d fps - they need the new build before you can "
                "play together." % (name, mine))
    return ("%s plays at another frame rate - 30 and 60 fps players play separately. Change Frame rate in the Guide "
            "(Settings) to %s." % (name, goal))


def same_fps(lobby, session, ticket, now=None):
    """(place_seeker, place_friend_seeker) The ticket's players may be put into this lobby as far as the frame rate
    goes. A lobby it rules out is logged by fps_skip_line (once a minute per seeker, FPS_SKIP_LINES in all)."""
    if not fps_split_on():
        return True
    if session is None:
        session = session_shared_from_ref(lobby["ref"])   # read-only
    owner = base_of(ticket["owner"])
    mine = ticket_fps(ticket)
    clash = fps_clash(session, owner, mine, lobby.get("fps"))
    if clash is None:
        return True
    fps_skip_line(ticket, lobby, session, mine, clash[1], now or time.time())
    return False


def fps_skip_line(ticket, lobby, session, mine, theirs, now):
    """(same_fps; the caller holds STATE_LOCK) "[FPS] <seeker> (<rate>) is not put into <host>'s lobby (<rate>)" - in
    each FPS_SKIP_SECONDS window at most one line per seeker, whatever the lobby, and FPS_SKIP_LINES lines in all.
    v1 logged once a minute per seeker AND lobby, as same_game_data does; but every pass rules every seeker out of
    every open lobby of the other pool, so while older builds and new ones are both about that came to seekers x
    lobbies lines a minute - hundreds at a busy hour. A pair is still counted once per window (a seeker asking again
    and again about the same lobby adds nothing), and the first line of the next window says how many pairs had no
    line of their own."""
    window = FPS_SKIP_WINDOW
    if now - window["at"] >= FPS_SKIP_SECONDS:
        if window["left_out"]:
            print('[FPS] %d more "is not put into ... lobby" lines were left out in the minute from %s (%d printed: at '
                  'most one per seeker, %d in all)' % (window["left_out"], time.strftime(
                      "%H:%M:%S", time.localtime(window["at"])), len(window["seekers"]), FPS_SKIP_LINES))
        window.update(at=now, seekers=set(), pairs=set(), left_out=0)
    owner = str(base_of(ticket["owner"]))
    pair = (owner, ref_key(lobby["ref"]))
    if pair in window["pairs"]:
        return
    window["pairs"].add(pair)
    if owner in window["seekers"] or len(window["seekers"]) >= FPS_SKIP_LINES:
        window["left_out"] += 1
        return
    window["seekers"].add(owner)
    host = session_host_xuid(session) if isinstance(session, dict) else None
    print("[FPS] %s (%s) is not put into %s's lobby (%s)" % (
        gamertag_for(ticket["owner"]), fps_mm_text(mine), gamertag_for(host or lobby["owner"]), fps_mm_text(theirs)))


def fps_friend_seeker_text(lobby, session, ticket, name):
    """(place_friend_seeker) Why a friend Join waits over the rate - the wish stays armed and says this."""
    mine = ticket_fps(ticket)
    clash = fps_clash(session, base_of(ticket["owner"]), mine, lobby.get("fps"))
    return fps_clash_text(name, clash[1] if clash else lobby_fps(lobby, session), mine, "join them")


def fps_join_refusal(session, members, xuid):
    """(join_refusal) Why a player may not take a NEW place - one it holds no reservation for - among these members
    over the frame rate, or None. The same player's other local users always may; the first member of a new session
    never comes here. The message becomes apply_member_writes' 403."""
    if not fps_split_on() or not member_keys(members):
        return None
    base = base_of(xuid)
    if base is None:
        return None
    if base in {base_of(member_xuid(members[k])) for k in accepted_member_keys(members)} - {None}:
        return None
    ref = session.get("sessionRef") if isinstance(session, dict) else None
    fallback = (MM_LOBBIES.get(ref_key(ref)) or {}).get("fps") if isinstance(ref, dict) else None
    clash = fps_clash({"members": members}, base, fps_of(base), fallback)
    if clash is None:
        return None
    return "this lobby is for %s" % fps_players_text(clash[1])


def fps_claim_refusal(session, members, own, xuid):
    """(apply_member_writes and fps_session_rate_changed, under STATE_LOCK) Why this player may not TAKE the place
    reserved for them (member own: matchmaking's or an invite's) over the frame rate, or None - fps_join_refusal's
    test, made again at the claim, with the rate this very request says, against the players who have JOINED the
    session (v3). The place was written for the rate the player had then; a switch in between (the Guide refuses one
    while /me says inGroup, but its last /me poll can be older than the place) would otherwise put a 30 and a 60 fps
    player together. Other players' reserved places do not count: they are not in the lobby yet, and each is checked
    when its own player claims it (v2 counted them, so one reserved player's switch cost a same-rate player its claim).
    None while "mmFpsSplit" is false, for the same player's other local users, and when no other player has joined.
    An older build keeps v1's pools: "own" lets it claim among older builds, and a rate not known yet passes."""
    if not fps_split_on():
        return None
    base = base_of(xuid)
    if base is None:
        return None
    joined = {k: members[k] for k in accepted_member_keys(members) if k != own}
    if not any(base_of(member_xuid(joined[k])) not in (None, base) for k in joined):
        return None
    return fps_join_refusal(session, joined, xuid)


def fps_rate_changed(base, fps, ref=None):
    """(v3) A player's rate changed (fps_note_caller: base's request says fps and the last one said another), or a
    host's lobby took a new rate at its re-ticket (create_multiplayer_ticket: ref is that lobby's session). Every
    session the player is in with other players (only ref's when it is given) goes to fps_session_rate_changed: a
    MIXED line when joined players of the other rate are in it with them, and the reserved places in it that could no
    longer be claimed are freed. Nothing when "mmFpsSplit" is false or the rate is not known. Takes STATE_LOCK (an
    RLock: the re-ticket holds it already). Never fails a request."""
    if base is None or fps is None or not fps_split_on():
        return
    base = str(base)
    try:
        now = time.time()
        with STATE_LOCK:   # sessions are written and removed under it
            if ref is not None:
                session = session_shared_from_ref(ref)   # read-only; fps_release_claim reads the file again
                found = [session] if isinstance(session, dict) else []
            else:
                found = [session for _path, session in iter_sessions(HD_SCID) if base in fps_member_bases(session)]
            for session in found:
                fps_session_rate_changed(session, base, fps, now)
    except Exception as error:   # noqa: BLE001 - this only looks after the fact; a request never fails over it
        builtins.print("[FPS] could not look at the sessions of %s after its rate changed: %r" % (base, error))


def fps_member_bases(session):
    """The xuidBases of everyone in a stored session, joined or holding a place."""
    members = session.get("members") if isinstance(session, dict) else None
    if not isinstance(members, dict):
        return set()
    return {base_of(member_xuid(members[k])) for k in member_keys(members)} - {None}


def fps_lobby_text(session, ref):
    """"Mako's lobby S9_H30" (or "dungeon" once it is closed); "lobby S9_H30" while nobody hosts it."""
    host = session_host_xuid(session)
    closed = ((session.get("properties") or {}).get("system") or {}).get("closed") is True
    kind = "dungeon" if closed else "lobby"
    return "%s's %s %s" % (gamertag_for(host), kind, ref.get("name")) if host else "%s %s" % (kind, ref.get("name"))


def fps_session_rate_changed(session, base, fps, now):
    """(fps_rate_changed, under STATE_LOCK) One session base's player is in, now that it plays at fps. The session is
    only read here (it may be the parse cache's own copy): fps_release_claim reads the file again to free a place."""
    raw = session.get("members") if isinstance(session.get("members"), dict) else {}
    keys = member_keys(raw)
    bases = {k: base_of(member_xuid(raw[k])) for k in keys}
    if not any(other not in (None, base) for other in bases.values()):
        return   # nobody else in it
    ref = session.get("sessionRef") if isinstance(session.get("sessionRef"), dict) else {}
    joined = accepted_member_keys(raw)
    if any(bases[k] == base for k in joined):
        clashing = [(bases[k], fps_of(bases[k])) for k in joined
                    if bases[k] not in (None, base) and not fps_ok(fps, fps_of(bases[k]))]
        if clashing:
            fps_mixed_line(session, ref, base, fps, clashing, now)
    for key in keys:
        xuid = member_xuid(raw[key])
        if raw[key].get("reserved") is not True or not xuid:
            continue
        refused = fps_claim_refusal(session, raw, key, xuid)   # what its claim would get
        if refused and fps_release_claim(ref, xuid):
            print("[FPS] %s (%s) loses its reserved place in %s: %s - the place is freed (%s)" % (
                gamertag_for(xuid), fps_mm_text(fps_of(bases[key])), fps_lobby_text(session, ref), refused,
                "it switched to %s before taking it" % fps_mm_text(fps) if bases[key] == base else
                "%s plays at %s now" % (gamertag_for(primary_xuid(base)), fps_mm_text(fps))))


def fps_mixed_line(session, ref, base, fps, clashing, now):
    """(fps_session_rate_changed) "[FPS] MIXED <session>: <player> now 60 fps, others 30 fps (...)" - joined players of
    both rates in one session: a switch raced a join (the accepted residual race, see above). At most once per
    FPS_MIXED_SECONDS per session, player and rate. Only logged: nothing is refused or changed for them."""
    key = (ref_key(ref), base, fps)
    if now - FPS_MIXED_LOGGED.get(key, 0) < FPS_MIXED_SECONDS:
        return
    for old in [k for k, at in FPS_MIXED_LOGGED.items() if now - at >= FPS_MIXED_SECONDS]:
        del FPS_MIXED_LOGGED[old]
    FPS_MIXED_LOGGED[key] = now
    rates = []
    for _other, theirs in clashing:
        if fps_mm_text(theirs) not in rates:
            rates.append(fps_mm_text(theirs))
    print("[FPS] MIXED %s: %s now %s, others %s (%s; %s) - a switch raced a join; the joined players are left as they "
          "are" % (ref.get("name"), gamertag_for(primary_xuid(base)), fps_mm_text(fps), " / ".join(rates),
                   ", ".join(gamertag_for(primary_xuid(other)) for other, _theirs in clashing),
                   fps_lobby_text(session, ref)))


def fps_release_claim(ref, xuid):
    """(fps_claim_refused and fps_session_rate_changed, under STATE_LOCK) Frees the place reserved for xuid in the
    session ref names, the way expire_reservation frees one nobody takes: read from the file, the member row dropped,
    saved (the session removed if nobody is left), the relay and the members' games told. True when a reserved place
    was freed."""
    if not isinstance(ref, dict) or not all(ref.get(k) for k in ("scid", "templateName", "name")):
        return False
    path = session_file(ref["scid"], ref["templateName"], ref["name"])
    session = load_json(path)
    if not isinstance(session, dict):
        return False
    members = session.get("members") if isinstance(session.get("members"), dict) else {}
    key = find_member_key(members, xuid)
    if key is None or members[key].get("reserved") is not True:
        return False
    previous = copy.deepcopy(session)
    members.pop(key)
    session = decorate_session(session, ref["scid"], ref["templateName"], ref["name"])
    stamp_session_change(session, previous)
    if session["members"]:
        save_json(path, session)
        relay_session_changed(session)
    else:
        remove_session(ref["scid"], ref["templateName"], ref["name"], path)
    notify_session_changed(session, previous)
    return True


def fps_claim_refused(existing, xuid, refused):
    """(apply_member_writes) The answer to a claim fps_claim_refusal refused: the place is freed, an [FPS] line says
    so, and the refusal is join_refusal's 403 {"code": 403, "description": "this lobby is for 30 fps players"}."""
    ref = existing.get("sessionRef") if isinstance(existing, dict) else None
    freed = fps_release_claim(ref, xuid)
    print("[FPS] %s (%s) may not take its reserved place in %s's lobby %s: %s - refused (403), %s" % (
        gamertag_for(xuid), fps_mm_text(fps_of(base_of(xuid))), gamertag_for(session_host_xuid(existing)),
        ref.get("name") if isinstance(ref, dict) else "?", refused,
        "the place is freed" if freed else "the place could not be freed (it lapses on its own)"))
    return json_response({"code": 403, "description": refused}, 403)


def fps_friend_join_refusal(me, friend, record, view):
    """(join_friend_state) The sentence refusing me a Join of this friend's lobby over the rate, or None."""
    fallback = (MM_LOBBIES.get(view["key"]) or {}).get("fps") if view and view.get("key") else None
    if fallback is None:
        fallback = fps_of(friend)
    mine = fps_of(me)
    clash = fps_clash(record["session"] if record else None, me, mine, fallback)
    if clash is None:
        return None
    return fps_clash_text(gamertag_for(primary_xuid(friend)), clash[1], mine, "join them")


def fps_lobby_refusal(base, lobby, session):
    """(lobbies_join) The sentence refusing the players page's Join of this lobby over the rate, or None. Only when
    both rates are known (a player not seen yet is checked again when their search comes)."""
    mine = fps_of(base)
    clash = fps_clash(session, base, mine, lobby.get("fps"))
    if clash is None:
        return None
    return fps_clash_text(gamertag_for(session_host_xuid(session) or lobby["owner"]), clash[1], mine, "join them")


def fps_invite_refusal(me, friend, index):
    """(invite_friend_state) The sentence refusing me an Invite of this friend into my lobby over the rate, or None."""
    record = index["players"].get(me)
    theirs = fps_of(friend)
    clash = fps_clash(record["session"] if record else None, friend, theirs, fps_of(me))
    if clash is None:
        return None
    return fps_clash_text(gamertag_for(primary_xuid(friend)), theirs, clash[1], "play together")


def fps_invite_landable(invitee, host, record):
    """(invite_landable) The invitee may go into the host's lobby as far as the rate goes."""
    return fps_clash(record["session"], invitee, fps_of(invitee), fps_of(host)) is None


def fps_invite_text(reason, name, invitee, host, index):
    """invite_reason_text, with the rates in it for "otherFps" (the invitee reads it: name is the host)."""
    if reason != "otherFps":
        return invite_reason_text(reason, name)
    record = index["players"].get(host)
    mine = fps_of(invitee)
    clash = fps_clash(record["session"] if record else None, invitee, mine, fps_of(host))
    return fps_clash_text(name, clash[1] if clash else fps_of(host), mine, "join them")


def fps_lobby_fields(lobby, session, mine, base):
    """(lobbies_list) A lobby's rate, and whether the caller may join it as far as the rate goes (None: not known).
    Lobbies of the other rate stay in the list; sameFps false marks them."""
    theirs = lobby_fps(lobby, session)
    same = None
    if mine is not None and theirs is not None:
        same = fps_clash(session, base, mine, lobby.get("fps")) is None
    return {"fps": theirs, "fpsText": fps_label(theirs) if theirs is not None else None, "sameFps": same}


def fps_stats_ok(ticket):
    """(momatch_hopper_stats) Count this ticket in the population the caller sees: it is in the caller's pool."""
    return not fps_split_on() or fps_ok(fps_of(fps_caller_base()), ticket_fps(ticket))


def fps_pool_name(value):
    return str(value) if value in (30, 60) else "old" if value == "old" else None


def fps_pool(now):
    """/windurango/me's fpsPool: players searching and open lobbies per rate. Built at most every
    TUNING_RECHECK_SECONDS. The caller holds STATE_LOCK."""
    at = time.monotonic()
    if FPS_POOL_CACHE["value"] is not None and at - FPS_POOL_CACHE["at"] < TUNING_RECHECK_SECONDS:
        return copy.deepcopy(FPS_POOL_CACHE["value"])
    pool = {name: {"searching": 0, "openLobbies": 0} for name in ("30", "60", "old")}
    for ticket in list(MM_TICKETS.values()):
        name = fps_pool_name(ticket_fps(ticket)) if ticket["role"] == "seeker" else None
        if name:
            pool[name]["searching"] += len(ticket["players"])
    for key in list(MM_LOBBIES):
        found = open_lobby(key, now)
        name = fps_pool_name(lobby_fps(found[0], found[1])) if found else None
        if name:
            pool[name]["openLobbies"] += 1
    FPS_POOL_CACHE.update(at=at, value=pool)
    return copy.deepcopy(pool)


def fps_in_group(base, index):
    """The player is in a lobby, session or dungeon with at least one other player, joined or holding a place."""
    base = str(base)
    seen = set()
    for record in index["players"].values():
        if id(record) in seen:
            continue
        seen.add(id(record))
        if base in record["members"] and len(record["members"]) > 1:
            return True
    return False


def fps_me_fields(base, doc, index, now):
    """/windurango/me's frame-rate half (additive). The caller holds STATE_LOCK."""
    in_group = fps_in_group(base, index)
    doc["fpsApi"] = FPS_API
    doc["fpsSplit"] = fps_split_on()
    doc["inGroup"] = in_group
    if isinstance(doc.get("you"), dict):
        doc["you"]["fps"] = fps_of(base)
        doc["you"]["inGroup"] = in_group
    for friend in doc.get("friends") or []:
        other = base_of(friend.get("xuid"))
        friend["fps"] = fps_of(other) if other is not None and friend.get("online") else None
    doc["fpsPool"] = fps_pool(now)
    return doc


def fps_startup_line():
    if not fps_split_on():
        return ('[FPS] split off - 30 and 60 fps players are matched together (%s; server_config.json "mmFpsSplit": '
                'false)' % FPS_SPLIT_VERSION)
    return ('[FPS] split on, old builds: %s (%s: 30 and 60 fps players are matched separately by the %s header; '
            'server_config.json "mmFpsSplit" / "mmFpsUnknown", read again within %g s)' % (
                tuning_value("mmFpsUnknown"), FPS_SPLIT_VERSION, FPS_HEADER, TUNING_RECHECK_SECONDS))


def note_ticket_version(owner, version):
    """Remember the version value in this player's latest ticket (the players page compares lobbies with it)."""
    base = base_of(owner)
    if base is None:
        return
    before = LAST_TICKET_VERSION.get(base)
    if version is None:
        LAST_TICKET_VERSION.pop(base, None)
        return
    LAST_TICKET_VERSION[base] = version
    if before != version:
        print("[MM] %s's game data: %s (%d)" % (gamertag_for(owner), game_data_text(version), version))


def expire_reservation(lobby_ref, xuid):
    with STATE_LOCK:
        path = session_file(lobby_ref["scid"], lobby_ref["templateName"], lobby_ref["name"])
        session = load_json(path)
        if not isinstance(session, dict):
            return
        members = session.get("members") if isinstance(session.get("members"), dict) else {}
        key = find_member_key(members, xuid)
        if key is None or members[key].get("reserved") is not True:
            return
        previous = copy.deepcopy(session)
        members.pop(key)
        print("[MM] %s never came into %s's lobby; freed its place" % (
            gamertag_for(xuid), gamertag_for(session_host_xuid(session))))
        session = decorate_session(session, lobby_ref["scid"], lobby_ref["templateName"], lobby_ref["name"])
        stamp_session_change(session, previous)
        if session["members"]:
            save_json(path, session)
            relay_session_changed(session)
        else:
            remove_session(lobby_ref["scid"], lobby_ref["templateName"], lobby_ref["name"], path)
    notify_session_changed(session, previous)


def reserve_into_lobby(lobby, session, ticket):
    """Puts the ticket's players into the lobby session as reservations and answers the ticket "found" with the
    lobby as its target. The caller holds STATE_LOCK."""
    ref = lobby["ref"]
    session = copy.deepcopy(session)   # (tuning_patch) with fastPaths, open_lobby's session is the parse cache's own
    previous = copy.deepcopy(session)
    members = session.setdefault("members", {})
    next_key = next_member_key(session)
    added = []
    # The game reads the dungeon from matchmakingResult.ticketAttrs.Stage: give the joiners the lobby's.
    attrs = copy.deepcopy(ticket["attrs"])
    if concrete_stage(lobby.get("stage")):
        attrs["Stage"] = lobby["stage"]
    for xuid, initialize in ticket["players"]:
        if find_member_key(members, xuid) is not None:
            continue
        members[str(next_key)] = {
            "reserved": True,
            "constants": {
                "system": {"xuid": xuid, "initialize": bool(initialize)},
                "custom": {"matchmakingResult": {"ticketAttrs": copy.deepcopy(attrs), "playerAttrs": {}}},
            },
            "properties": {"system": {}, "custom": {}},
        }
        next_key += 1
        added.append(xuid)
    session.setdefault("membersInfo", {})["next"] = next_key
    session = decorate_session(session, ref["scid"], ref["templateName"], ref["name"])
    stamp_session_change(session, previous)
    save_json(session_file(ref["scid"], ref["templateName"], ref["name"]), session)
    relay_session_changed(session)   # reserved players' games may talk to the lobby's before they claim their places
    print("[MM] %s -> %s's lobby %s (%d of %d places taken)" % (
        ", ".join(gamertag_for(x) for x in added) or gamertag_for(ticket["owner"]),
        gamertag_for(session_host_xuid(session)), ref["name"], len(session["members"]), lobby_size(ref)))
    log_members(session)
    notify_session_changed(session, previous)
    for xuid in added:
        timer = threading.Timer(RESERVATION_SECONDS, expire_reservation, args=(copy.deepcopy(ref), xuid))
        timer.daemon = True
        timer.start()
    MM_TICKETS.pop(ticket["id"], None)
    # (lobby_patch v1) Its game stays member 0 of its own ticket session until it leaves this lobby, so that session
    # is no lobby to put anyone into any more: nobody there would start it (a "ghost"). place_seeker never moves a
    # seeker whose session has anyone else in it, so the record holds nobody. The player searches again from this
    # lobby, not from its own session; a new search from there lists it again (register_seeker_lobby).
    own = ref_key(ticket["ref"])
    if (own != ref_key(ref) and lobby_kind(MM_LOBBIES.get(own) or {}) == "seeker"
            and tuning_value("mmDropPlacedSeekerLobby")):
        MM_LOBBIES.pop(own, None)
        print("[MM] %s's own lobby %s is off the list: it is in %s's lobby now" % (
            gamertag_for(ticket["owner"]), ticket["ref"].get("name"), gamertag_for(session_host_xuid(session))))
    update_matchmaking_state(ticket["ref"], "found", typical_wait=0, target_ref=copy.deepcopy(ref))


def seeker_in_someones_lobby(ticket):
    """The seeker's ticket session is a lobby another player hosts (it re-ticketed from a lobby it joined)."""
    session = session_shared_from_ref(ticket["ref"])   # (tuning_patch: read-only)
    if not isinstance(session, dict):
        return False
    host = session_host_xuid(session)
    return bool(host and base_of(host) != base_of(ticket["owner"]))


def place_seeker(ticket, now=None):
    """Tries to put a waiting seeker into a lobby: the one its player picked on the players page, else the open
    lobby with the most players (then the oldest). The caller holds STATE_LOCK. True when it was placed."""
    now = now or time.time()
    if ticket["id"] not in MM_TICKETS:
        return False
    if solo_xuid(ticket["owner"]):
        # (solo_patch v1) solo play: never into anyone else's lobby - before the Join wish, the friend Join and the
        # candidate walk. The ticket stays in MM_TICKETS and resolves to the seeker's own session, as it does when
        # nobody else is online ("stays in its own lobby"), and the game starts the dungeon there alone.
        return False
    if seeker_in_someones_lobby(ticket) or session_has_others(ticket):
        return False
    candidates = []
    wish = JOIN_WISHES.get(str(base_of(ticket["owner"])))
    if wish and wish.get("friend") and wish["until"] > now:
        # friends join: a Join pressed on a friend puts the seeker into that friend's lobby or nowhere
        placed = place_friend_seeker(ticket, wish, now)
        if placed is not None:
            return placed
        wish = JOIN_WISHES.get(str(base_of(ticket["owner"])))
    if wish and wish["until"] > now:
        found = open_lobby(wish["lobby"], now)
        if found and not same_game_data(found[0], found[1], ticket, now):
            # The game would refuse that lobby: forget the wish, look for one with the same game data.
            print("[MM] %s wanted to join %s's lobby, but it has other game data" % (
                gamertag_for(ticket["owner"]), gamertag_for(session_host_xuid(found[1]) or found[0]["owner"])))
            JOIN_WISHES.pop(str(base_of(ticket["owner"])), None)
            wish = None
        elif found and not same_fps(found[0], found[1], ticket, now):
            # (fps_split v3) That lobby plays at another frame rate: forget the wish, look for one at this rate.
            print("[FPS] %s (%s) wanted to join %s's lobby (%s) - the wish is dropped" % (
                gamertag_for(ticket["owner"]), fps_mm_text(ticket_fps(ticket)),
                gamertag_for(session_host_xuid(found[1]) or found[0]["owner"]),
                fps_mm_text(lobby_fps(found[0], found[1]))))
            JOIN_WISHES.pop(str(base_of(ticket["owner"])), None)
            wish = None
        elif found and lobby_fits(found[0], ticket) and found[2] >= len(ticket["players"]):
            if stage_allowed(found[0]["stage"], ticket["stage"]):
                candidates.append(found)
            elif wish.get("stageNote") != (wish["lobby"], ticket["stage"]):
                # (lobby_patch v1) the game would keep its own mission there: the wish stays for a search for the
                # lobby's mission (or any dungeon); this search goes on like any other. Logged once per mission.
                wish["stageNote"] = (wish["lobby"], ticket["stage"])
                print("[MM] %s wanted to join %s's lobby, but it plays %s and %s searched for %s - the Join waits "
                      "for a search for that mission (or any dungeon)" % (
                          gamertag_for(ticket["owner"]),
                          gamertag_for(session_host_xuid(found[1]) or found[0]["owner"]),
                          stage_text(found[0]["stage"]), gamertag_for(ticket["owner"]), stage_text(ticket["stage"])))
        elif found is None:
            print("[MM] %s wanted to join %s, but that lobby is not open now" % (
                gamertag_for(ticket["owner"]), wish["lobby"].split("~")[-1]))
    if not candidates:
        # Where the seeker sits now: alone in its own seeker lobby, it only moves for something strictly better.
        mine = MM_LOBBIES.get(ref_key(ticket["ref"]))
        mine = mine if mine and lobby_kind(mine) == "seeker" else None
        mine_count = len(ticket["players"])
        for key in list(MM_LOBBIES):
            found = open_lobby(key, now)
            if not found or ref_key(found[0]["ref"]) == ref_key(ticket["ref"]):
                continue
            lobby, session, free = found
            if not lobby_fits(lobby, ticket) or free < len(ticket["players"]):
                continue
            if not stage_allowed(lobby["stage"], ticket["stage"]):
                continue
            if not same_game_data(lobby, session, ticket, now):
                continue
            if not same_fps(lobby, session, ticket, now):   # (fps_split v3)
                continue
            count = len(accepted_member_keys(session.get("members") or {}))
            if mine and lobby_kind(lobby) == "seeker" and not (
                    count > mine_count or (count == mine_count and lobby["since"] < mine["since"])):
                continue
            candidates.append(found)
        candidates.sort(key=lambda c: (lobby_kind(c[0]) != "host",
                                       not stages_match(c[0]["stage"], ticket["stage"]),
                                       -len(accepted_member_keys(c[1].get("members") or {})),
                                       c[0]["since"]))
    if not candidates:
        return False
    lobby, session, _free = candidates[0]
    if not stages_match(lobby["stage"], ticket["stage"]):
        print("[MM] no lobby for %s's %s; %s's lobby plays %s instead" % (
            gamertag_for(ticket["owner"]), stage_text(ticket["stage"]),
            gamertag_for(session_host_xuid(session) or lobby["owner"]), stage_text(lobby["stage"])))
    if wish and ref_key(lobby["ref"]) == wish["lobby"]:
        JOIN_WISHES.pop(str(base_of(ticket["owner"])), None)
    reserve_into_lobby(lobby, session, ticket)
    return True


def resolve_multiplayer_ticket(ticket_id):
    """MATCH_FOUND_AFTER_SECONDS after a ticket: a host's resolves to its own lobby; a seeker still without a
    lobby gets a session of its own for now and finds one with its next ticket."""
    with STATE_LOCK:
        ticket = MM_TICKETS.get(ticket_id)
        if ticket is None:
            return
        ticket_session = load_session_from_ref(ticket["ref"])
        if not isinstance(ticket_session, dict) or matchmaking_status(ticket_session) != "searching":
            MM_TICKETS.pop(ticket_id, None)
            return
        if ticket["role"] == "host":
            MM_TICKETS.pop(ticket_id, None)
            target_ref = copy.deepcopy(ticket["ref"])
        elif place_seeker(ticket):
            return
        elif seeker_in_someones_lobby(ticket) and BACKFILL_POLICY == "hold":
            return   # stays searching in the lobby it is in; the game gives up on its own after a while
        else:
            # Nobody to join: the seeker stays in its own session (same name = the game does not leave it), which
            # remains a lobby other seekers can be put into. Its players get the matchmakingResult the old
            # "session of its own" carried, so the game reads the same Stage from them as before.
            MM_TICKETS.pop(ticket_id, None)
            target_ref = copy.deepcopy(ticket["ref"])
            register_seeker_lobby(ticket, time.time())
            give_matchmaking_result(ticket, ticket_session)
            others = session_has_others(ticket)
            print("[MM] %s stays in its own lobby%s" % (
                gamertag_for(ticket["owner"]),
                " with the players who joined it" if others else " (nobody to join yet; others can join it)"))
    print("[MATCHMAKING] Ticket", ticket_id, "found ->", target_ref)
    update_matchmaking_state(ticket["ref"], "found", typical_wait=0, target_ref=target_ref)


def give_matchmaking_result(ticket, session):
    """Adds constants.custom.matchmakingResult (the ticket's attributes) to the ticket players' members in their own
    session where it is missing. The caller holds STATE_LOCK."""
    path = session_file(ticket["ref"]["scid"], ticket["ref"]["templateName"], ticket["ref"]["name"])
    members = session.get("members") if isinstance(session.get("members"), dict) else {}
    previous = copy.deepcopy(session)
    changed = False
    for xuid, _initialize in ticket["players"]:
        key = find_member_key(members, xuid)
        if key is None:
            continue
        custom = members[key].setdefault("constants", {}).setdefault("custom", {})
        if not isinstance(custom, dict):
            custom = members[key]["constants"]["custom"] = {}
        if "matchmakingResult" not in custom:
            custom["matchmakingResult"] = {"ticketAttrs": copy.deepcopy(ticket["attrs"]), "playerAttrs": {}}
            changed = True
    if changed:
        ref = ticket["ref"]
        session = decorate_session(session, ref["scid"], ref["templateName"], ref["name"])
        stamp_session_change(session, previous)
        save_json(path, session)


def create_multiplayer_ticket(scid, hopper_name, body, session_ref, session):
    """POST hoppers/<hopper>: a new host or seeker ticket from the ticket session."""
    members = session.get("members") if isinstance(session.get("members"), dict) else {}
    players = [(member_xuid(members[k]), (members[k].get("constants", {}).get("system", {}) or {})
                .get("initialize", False)) for k in accepted_member_keys(members) if member_xuid(members[k])]
    owner = str(caller_xuid() or "")
    if not players and owner:
        players = [(owner, False)]
    attrs = body.get("ticketAttributes") if isinstance(body.get("ticketAttributes"), dict) else {}
    # Its game data (see game_version): the Division's, else what the game wrote into its ticket session.
    version = division_version(attrs.get("Division"))
    if version is None:
        version = session_version(session)
    ticket = {
        "id": str(uuid.uuid4()),
        "scid": scid,
        "hopper": hopper_name,
        "ref": {"scid": session_ref.get("scid"), "templateName": session_ref.get("templateName"),
                "name": session_ref.get("name")},
        "role": ticket_role(body),
        "attrs": attrs,
        "stage": str(attrs.get("Stage", "")),
        "owner": owner,
        "players": players,
        "created": time.time(),
        "version": version,
        "fps": fps_of(base_of(owner)),   # (fps_split v3) this request's X-WinDurango-Fps, else the owner's known rate
    }
    update_matchmaking_state(ticket["ref"], "searching", typical_wait=1)
    with STATE_LOCK:
        MM_TICKETS[ticket["id"]] = ticket
        note_ticket_version(owner, ticket["version"])
        now = time.time()
        if ticket["role"] == "host":
            key = ref_key(ticket["ref"])
            lobby = MM_LOBBIES.get(key)
            if lobby is None or lobby_kind(lobby) != "host":
                print("[MM] %s is hosting a lobby (%s, %s)%s" % (   # (solo_patch v1: says so while it is on)
                    gamertag_for(owner), ticket["stage"] or "no stage", fps_mm_text(ticket["fps"]),   # (fps_split v3)
                    " - solo play: nobody will be put into it" if solo_xuid(owner) else ""))
                lobby = MM_LOBBIES[key] = {"ref": copy.deepcopy(ticket["ref"]), "kind": "host", "since": now}
            fps_switched = lobby.get("fps", ticket["fps"]) != ticket["fps"]   # (fps_split v3) its host switched
            if fps_switched:
                print("[FPS] %s's lobby: %s now (was %s)" % (
                    gamertag_for(owner), fps_mm_text(ticket["fps"]), fps_mm_text(lobby["fps"])))
            lobby.update(stage=ticket["stage"], hopper=hopper_name, owner=owner, refreshed=now,
                         version=ticket["version"], fps=ticket["fps"])
            if fps_switched:   # (fps_split v3) joined players of the other rate: MIXED; places nobody can take: freed
                fps_rate_changed(base_of(owner), ticket["fps"], ticket["ref"])
            waiting = sorted((t for t in MM_TICKETS.values() if t["role"] == "seeker"), key=lambda t: t["created"])
            if waiting and tuning("mmSkipFullLobbyPass") and open_lobby(key, now) is None:   # (tuning_patch)
                waiting = []   # this lobby takes nobody now: the seekers' own tickets and timers still place them
            for seeker in waiting:
                place_seeker(seeker, now)
        else:
            print("[MM] %s is looking for a lobby (%s, %s)%s" % (   # (solo_patch v1: says so while it is on)
                gamertag_for(owner), ticket["stage"] or "no stage", fps_mm_text(ticket["fps"]),   # (fps_split v3)
                " - solo play: it will play alone" if solo_xuid(owner) else ""))
            first_search = False
            if own_seeker_lobby(ticket):
                first_search = register_seeker_lobby(ticket, now)
            place_seeker(ticket, now)
        placed = ticket["id"] not in MM_TICKETS
        # How long before the solo answer: a first search waits for a host; a lobby that others already joined is
        # answered at once (they are waiting in it); a re-ticket from an established seeker lobby as before.
        wait = MATCH_FOUND_AFTER_SECONDS
        if ticket["role"] == "seeker" and not placed:
            if session_has_others(ticket):
                wait = 0.2
            elif first_search and not solo_xuid(owner):   # (solo_patch v1) nobody to wait for
                wait = SEEKER_HOLD_SECONDS
    if not placed:
        timer = threading.Timer(wait, resolve_multiplayer_ticket, args=(ticket["id"],))
        timer.daemon = True
        timer.start()
    print("MATCH TICKET:", ticket["id"], "role", ticket["role"], "players", [p[0] for p in players])
    return json_response({"ticketId": ticket["id"], "waitTime": MATCH_FOUND_AFTER_SECONDS}, 201)


# Xbox title storage (Happy Dungeons keeps its saves in container hd201607)

TITLE_STORAGE_ROUTE = (
    "/users/xuid(<xuid>)/storage/titlestorage/titlegroups/"
    + TITLE_GROUP
    + "/data/<container_name>/<path:subpath>"
)


def storage_refusal(xuid):
    """Why the caller may not touch this player's saves, or None when they may.

    A save belongs to the player whose XUID block it is under. A caller may use their own; nobody may use
    someone else's; and a caller claiming to be a registered player with the wrong secret gets nothing.
    Requests with no WinDurango identity at all are still served, so an install that has not claimed an
    identity yet keeps working on its own PC.
    """
    owner = base_of(xuid)
    caller = g.get("caller_base")
    if caller is None:
        return None if owner is None or not PLAYER_REGISTRY[owner].get("secret") else "no identity"
    if g.get("caller_verified") is False:
        return "wrong secret for player %s" % caller
    if owner is not None and owner != caller:
        return "player %s is not player %s" % (caller, owner)
    return None


@app.route(TITLE_STORAGE_ROUTE, methods=["GET", "PUT"])
def title_storage(xuid, container_name, subpath):
    log_request("TITLE STORAGE")
    if not str(xuid).isdigit():
        return json_response({"error": "not a xuid"}, 400)

    refusal = storage_refusal(xuid)
    if refusal:
        print("[STORAGE] refused %s %s: %s" % (request.method, xuid, refusal))
        return json_response({"error": refusal}, 403)

    file_path = title_storage_file(xuid, subpath)
    if not file_path:
        return Response(status=400)

    print("XUID:", xuid)
    print("CONTAINER:", container_name)
    print("SUBPATH:", subpath)
    print("LOCAL FILE:", file_path)

    if request.method == "PUT":
        return title_storage_put(xuid, file_path)   # (hardening: the key, the quota, streamed)

    if os.path.isfile(file_path):
        return send_file(
            file_path,
            mimetype="application/octet-stream"
        )

    return Response(status=404)


# ---------------------------------------------------------------------------
# Save export: a player downloads a copy of their own saves (the players page's "Export my save").
#
#   GET /windurango/save/export     Authorization: XBL3.0 x=1;WD1.<xuidBase>.<secret> (the players page sends it)
#
# Only for a verified player: 401 without an identity (or one this server does not know), 403 with the wrong secret.
# Only their own saves: the title storage folders data/<xuid>/ of the XUIDs of their block that have one - and only
# a XUID that title storage itself gives to this player (base_of), so a hand-edited player list with overlapping
# blocks cannot hand out someone else's folder. Nothing else is read - not _players.json (it holds every secret), not
# another player's folder, not the server's own files. Inside those folders links (symbolic links, junctions, other
# reparse points) are skipped, never followed, and so is anything that is not a regular file, and any name a zip
# cannot carry safely (a backslash, a colon or a control character - Linux allows them, a zip tool on Windows would
# read "..\" in one as a way out of the folder); a file is opened without following links and must still be the file
# that was listed. (data itself may be a link: on the VPS server/data points at /opt/happydungeons/data.)
#
# The answer is a ZIP (deflate), streamed while it is made:
#   save/<xuid>/<path under data/<xuid>/>   every save file, byte for byte as title storage keeps it
#   export.json   {"product", "format", "gamertag", "xuidBase", "xuids", "exportedAt", "serverVersion",
#                  "serverDataRev", "gameWasRunning", "skipped", "note", "files": [{"path", "size", "sha256"}, ...]}
#                  - written last, so size and sha256 are those of the bytes in this zip
# named HappyDungeons-save-<gamertag, else xuidBase>-<UTC yyyymmdd-HHMM>.zip, Content-Disposition attachment,
# Cache-Control no-store. 404 when nothing is saved yet; 413 when the saves are over SAVE_EXPORT_MAX_BYTES (or
# SAVE_EXPORT_MAX_FILES files, or the listing met more than SAVE_EXPORT_MAX_ENTRIES files and folders - it stops
# there, so a save folder full of empty folders cannot keep it busy); 429 (Retry-After) after SAVE_EXPORT_PER_WINDOW
# exports in SAVE_EXPORT_WINDOW_SECONDS - counted only for a verified player, so nobody else can use up theirs.
# A file that grows past the limit while it is being sent breaks the download off (the page says so) rather than
# end it with a zip that is not whole. A save the game writes while it is being exported can be caught half written:
# export.json says whether the player's game had its realtime connection open ("gameWasRunning").
# One [SAVE EXPORT] line per request: who, how many files, how many bytes, and the sha256 of the export.json that was
# sent. Every export that was sent whole also gets one line in data/save_export_log.jsonl (JSON, written only here):
# with it the owner checks that a zip sent back for a restore is one this server made (RESTORE.md's check_export.py
# --journal). Not the console log for that: any request can put a line of its choosing there (a %0A in its path).
# There is no import route: the owner restores by hand.
import stat      # (imported here: save_export_patch.py adds this section and touches nothing else)
import zipfile

SAVE_EXPORT_JOURNAL = os.path.join(DATA, "save_export_log.jsonl")
SAVE_EXPORT_MAX_BYTES = 200 * 1024 * 1024   # of saves in one export (a player's are well under 1 MB)
SAVE_EXPORT_MAX_FILES = 20000
SAVE_EXPORT_MAX_ENTRIES = 50000             # files, folders and left-out things looked at while listing
SAVE_EXPORT_PER_WINDOW = 3                 # exports per player ...
SAVE_EXPORT_WINDOW_SECONDS = 600            # ... in this many seconds
SAVE_EXPORT_READ = 256 * 1024               # bytes read from a save file at a time
SAVE_EXPORT_SEND = 64 * 1024                # the response sends the zip in pieces of about this size
SAVE_EXPORT_FORMAT = "windurango-save-export-1"
SAVE_EXPORT_REPARSE_POINT = 0x400           # FILE_ATTRIBUTE_REPARSE_POINT (Windows: junctions, symbolic links)
SAVE_EXPORT_ODD_NAME = re.compile(r"[\x00-\x1f\x7f\\:]")   # names a zip cannot carry safely (left out)
SAVE_EXPORT_TIMES = {}                      # str(xuidBase) -> times (time.monotonic) of that player's recent exports
SAVE_EXPORT_LOCK = threading.Lock()


def save_export_server_version():
    """A fingerprint of this server's code for export.json: the start of the sha256 of experimentalserver.py."""
    try:
        with open(os.path.abspath(__file__), "rb") as handle:
            return "sha256:" + hashlib.sha256(handle.read()).hexdigest()[:16]
    except OSError:
        return "unknown"


SAVE_EXPORT_SERVER_VERSION = save_export_server_version()


class SaveExportRefused(Exception):
    """The saves are over the limits: found while listing them (413), or while sending when a file grew."""


def save_export_plain(info):
    """True when this lstat result is not a link: no symbolic link, junction or other reparse point."""
    return not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & SAVE_EXPORT_REPARSE_POINT


def save_export_listing(base):
    """(files, xuids, skipped) of the player with this xuidBase.

    files: [(xuid, path inside data/<xuid>/ with "/", full path, lstat result)], sorted by XUID and path; xuids: the
    XUIDs of the block that have a plain save folder (empty ones too); skipped: links, things that are not regular
    files or folders, names a zip cannot carry safely and folders title storage gives to another player, left out.
    Walks without following links. Raises SaveExportRefused past the limits."""
    files, xuids, skipped, total, seen = [], [], 0, 0, 0
    for i in range(XUIDS_PER_PLAYER):
        xuid = str(int(base) + i)
        top = os.path.join(DATA, xuid)
        try:
            info = os.lstat(top)
        except OSError:
            continue   # nothing saved for this XUID
        if base_of(xuid) != base:   # (only when the player list has overlapping blocks) not this player's saves
            skipped += 1
            continue
        if not (save_export_plain(info) and stat.S_ISDIR(info.st_mode)):
            skipped += 1
            continue
        xuids.append(xuid)
        pending = [(top, "")]
        while pending:
            folder, prefix = pending.pop()
            names = []
            try:
                with os.scandir(folder) as entries:
                    for entry in entries:   # counted as they come: a folder can hold any number of names
                        seen += 1
                        if seen > SAVE_EXPORT_MAX_ENTRIES:
                            raise SaveExportRefused("over %d files and folders" % SAVE_EXPORT_MAX_ENTRIES)
                        names.append(entry.name)
            except OSError:
                skipped += 1
                continue
            for name in names:
                path = os.path.join(folder, name)
                if SAVE_EXPORT_ODD_NAME.search(name):   # a backslash, a colon or a control character (Linux)
                    skipped += 1
                    continue
                try:
                    name.encode("utf-8")   # a name that is not text (Linux allows any bytes) cannot go in the zip
                    info = os.lstat(path)
                except (OSError, UnicodeError):
                    skipped += 1
                    continue
                if not save_export_plain(info):
                    skipped += 1
                elif stat.S_ISDIR(info.st_mode):
                    pending.append((path, prefix + name + "/"))
                elif stat.S_ISREG(info.st_mode):
                    files.append((xuid, prefix + name, path, info))
                    total += info.st_size
                    if total > SAVE_EXPORT_MAX_BYTES:
                        raise SaveExportRefused("over %d MB" % (SAVE_EXPORT_MAX_BYTES // (1024 * 1024)))
                    if len(files) > SAVE_EXPORT_MAX_FILES:
                        raise SaveExportRefused("over %d files" % SAVE_EXPORT_MAX_FILES)
                else:
                    skipped += 1   # a pipe, a socket, a device
    files.sort(key=lambda item: (int(item[0]), item[1]))
    return files, xuids, skipped


def save_export_open(path, listed):
    """The file at path opened for reading (binary) without following a link - when it is still the regular file
    that was listed (same device and inode); else None."""
    flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_NONBLOCK", 0))   # O_NONBLOCK: a pipe put in its place cannot hang the open
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) == (listed.st_dev, listed.st_ino):
            return os.fdopen(fd, "rb")
    except OSError:
        pass
    os.close(fd)
    return None


def save_export_serverdata_rev():
    """The ServerData rev this server hands out now (for export.json), or None."""
    listing = globals().get("serverdata_listing")
    try:
        found = listing() if callable(listing) else None
    except Exception:   # noqa - export.json can do without it
        found = None
    return found[0] if isinstance(found, tuple) and found else None


def save_export_zip_time(seconds):
    # A zip holds local times from 1980 to 2107.
    return time.localtime(min(max(seconds, 315619200), 4102444800))[:6]


class SaveExportSink:
    """Where zipfile writes: the bytes for the next piece of the response. It cannot seek, so zipfile streams (the
    sizes and CRC of each file follow its data)."""

    def __init__(self):
        self.parts, self.size = [], 0

    def write(self, data):
        self.parts.append(bytes(data))
        self.size += len(data)
        return len(data)

    def flush(self):
        pass

    def take(self):
        out, self.parts, self.size = b"".join(self.parts), [], 0
        return out


def save_export_journal(record):
    """One JSON line in data/save_export_log.jsonl for an export that was sent whole. Only this function writes that
    file, and json.dumps leaves no line break in a line, so no request can put a line of its own in it (unlike the
    console log, where a %0A in a request's path starts a new line). True when written."""
    line = (json.dumps(record, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")
    flags = (os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        with SAVE_EXPORT_LOCK:
            fd = os.open(SAVE_EXPORT_JOURNAL, flags, 0o600)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
        return True
    except OSError as error:
        print("[SAVE EXPORT] could not add to %s: %s" % (os.path.basename(SAVE_EXPORT_JOURNAL), error))
        return False


def save_export_stream(base, gamertag, files, xuids, skipped, when):
    """The zip, piece by piece; then the [SAVE EXPORT] line (and, when it was sent whole, the journal line)."""
    who = "%s (%s)" % (gamertag or "?", base)
    sink = SaveExportSink()
    listed, saves, zipped, digest = [], 0, 0, None
    outcome = "the download was cancelled"   # what is left when the generator is closed early (GeneratorExit)
    try:
        with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for xuid, relative, path, info in files:
                source = save_export_open(path, info)
                if source is None:   # gone, or no longer the file that was listed
                    skipped += 1
                    continue
                entry = zipfile.ZipInfo("save/%s/%s" % (xuid, relative), date_time=save_export_zip_time(info.st_mtime))
                entry.compress_type = zipfile.ZIP_DEFLATED
                entry.external_attr = 0o600 << 16
                entry.file_size = info.st_size
                hasher, size = hashlib.sha256(), 0
                with source, archive.open(entry, "w") as target:
                    while True:
                        block = source.read(SAVE_EXPORT_READ)
                        if not block:
                            break
                        size += len(block)
                        saves += len(block)
                        if saves > SAVE_EXPORT_MAX_BYTES:
                            raise SaveExportRefused("a save grew past %d MB while it was being sent"
                                                    % (SAVE_EXPORT_MAX_BYTES // (1024 * 1024)))
                        hasher.update(block)
                        target.write(block)
                        if sink.size >= SAVE_EXPORT_SEND:
                            piece = sink.take()
                            zipped += len(piece)
                            yield piece
                listed.append({"path": entry.filename, "size": size, "sha256": hasher.hexdigest()})
                if sink.size >= SAVE_EXPORT_SEND:
                    piece = sink.take()
                    zipped += len(piece)
                    yield piece
            doc = {
                "product": "Happy Dungeons",
                "format": SAVE_EXPORT_FORMAT,
                "gamertag": gamertag,
                "xuidBase": str(base),
                "xuids": xuids,
                "exportedAt": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "serverVersion": SAVE_EXPORT_SERVER_VERSION,
                "serverDataRev": save_export_serverdata_rev(),
                "gameWasRunning": RTA_PLAYERS.get(str(base), 0) > 0,
                "skipped": skipped,
                "note": "A backup copy of this player's Happy Dungeons saves on this server. To restore it, send this "
                        "file to the server's owner: they put it back by hand.",
                "files": listed,
            }
            manifest = (json.dumps(doc, indent=1, ensure_ascii=False) + "\n").encode("utf-8")
            digest = hashlib.sha256(manifest).hexdigest()
            entry = zipfile.ZipInfo("export.json", date_time=save_export_zip_time(when.timestamp()))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o600 << 16
            archive.writestr(entry, manifest)
        piece = sink.take()   # the rest, and the zip's central directory
        zipped += len(piece)
        yield piece
        outcome = None
    except SaveExportRefused as error:
        outcome = "broken off: %s" % error
        raise
    except Exception as error:   # a save that could not be read (locked, gone bad): not a cancelled download
        outcome = "failed: %s: %s" % (type(error).__name__, error)
        raise
    finally:
        if outcome is None:
            print("[SAVE EXPORT] %s: %d file%s, %d bytes of saves, zip %d bytes%s, export.json sha256 %s" % (
                who, len(listed), "" if len(listed) == 1 else "s", saves, zipped,
                ", %d skipped (links, odd names, not regular files, or changed while listed)" % skipped
                if skipped else "", digest))
            save_export_journal({"at": when.strftime("%Y-%m-%dT%H:%M:%SZ"), "xuidBase": str(base),
                                 "gamertag": gamertag, "files": len(listed), "bytes": saves, "zipBytes": zipped,
                                 "skipped": skipped, "exportJsonSha256": digest})
        else:
            print("[SAVE EXPORT] %s: NOT FINISHED (%s) after %d file%s, %d bytes of saves" % (
                who, outcome, len(listed), "" if len(listed) == 1 else "s", saves))


@app.route("/windurango/save/export", methods=["GET"])
def save_export():
    """The caller's own saves as a zip (see above)."""
    base = g.get("caller_base")
    gamertag = PLAYER_REGISTRY.get(base, {}).get("gamertag") or "" if base else ""
    who = "%s (%s)" % (gamertag or "?", base) if base else "a caller without a player key"
    if request.method != "GET":   # HEAD (Flask adds it to GET routes) would count as an export and send nothing
        return json_response({"ok": False, "reason": "use GET"}, 405)
    if base is None or base not in PLAYER_REGISTRY:
        print("[SAVE EXPORT] refused %s: not signed in (401)" % who)
        return json_response({"ok": False, "reason": "sign in first"}, 401)
    if g.get("caller_verified") is not True:
        print("[SAVE EXPORT] refused %s: wrong key (403)" % who)
        return json_response({"ok": False, "reason": "that is not your identity"}, 403)

    now = time.monotonic()   # (not the wall clock: a clock set back would hold a player up for longer)
    with SAVE_EXPORT_LOCK:
        for known in list(SAVE_EXPORT_TIMES):
            SAVE_EXPORT_TIMES[known] = [t for t in SAVE_EXPORT_TIMES[known] if now - t < SAVE_EXPORT_WINDOW_SECONDS]
            if not SAVE_EXPORT_TIMES[known]:
                del SAVE_EXPORT_TIMES[known]
        recent = SAVE_EXPORT_TIMES.get(base, [])
        wait = int(SAVE_EXPORT_WINDOW_SECONDS - (now - recent[0])) + 1 if len(recent) >= SAVE_EXPORT_PER_WINDOW else 0
        if not wait:
            SAVE_EXPORT_TIMES.setdefault(base, []).append(now)
    if wait:
        print("[SAVE EXPORT] refused %s: %d exports in %d minutes already (429, %d s to wait)" % (
            who, SAVE_EXPORT_PER_WINDOW, SAVE_EXPORT_WINDOW_SECONDS // 60, wait))
        response = json_response({"ok": False, "reason": "%d exports in %d minutes at most - try again in %d s" % (
            SAVE_EXPORT_PER_WINDOW, SAVE_EXPORT_WINDOW_SECONDS // 60, wait), "retryAfter": wait}, 429)
        response.headers["Retry-After"] = str(wait)
        return response

    try:
        files, xuids, skipped = save_export_listing(base)
    except SaveExportRefused as error:
        print("[SAVE EXPORT] refused %s: the saves are %s (413)" % (who, error))
        answer = {"ok": False, "reason": "your saves are %s, more than this server exports - ask the server's owner "
                                         "for a copy" % error,
                  "maxFiles": SAVE_EXPORT_MAX_FILES, "maxEntries": SAVE_EXPORT_MAX_ENTRIES}
        if str(error).endswith(" MB"):   # the page names the size limit only when that is the one they are over
            answer["limit"] = SAVE_EXPORT_MAX_BYTES
        return json_response(answer, 413)
    if not files:
        print("[SAVE EXPORT] %s: nothing saved yet (404)%s" % (who, ", %d skipped" % skipped if skipped else ""))
        return json_response({"ok": False, "reason": "nothing is saved on this server for you yet"}, 404)

    when = datetime.now(timezone.utc)
    label = re.sub(r"[^A-Za-z0-9]+", "_", gamertag).strip("_")[:32] or str(base)
    name = "HappyDungeons-save-%s-%s.zip" % (label, when.strftime("%Y%m%d-%H%M"))
    response = Response(save_export_stream(base, gamertag, files, xuids, skipped, when), mimetype="application/zip")
    response.headers["Content-Disposition"] = 'attachment; filename="%s"' % name
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


# Xbox presence

@app.route(
    "/users/xuid(<xuid>)/devices/current/titles/current",
    methods=["POST", "DELETE"]
)
def current_title_presence(xuid):
    log_request("USER PRESENCE")
    # Only a player may say what they are doing (it ends up on the friends page and on their Discord).
    owner = base_of(xuid)
    if owner is not None and (g.get("caller_base") != owner or g.get("caller_verified") is not True):
        return json_response({"error": "not your presence"}, 403)

    print("XUID:", xuid)

    if request.method == "POST":
        body = request.get_data(as_text=True)

        if body:
            print("BODY:", body)

        payload = request.get_json(silent=True) or {}
        activity = (payload.get("activity") or {}).get("richPresence") or {}
        with STATE_LOCK:
            rec = PLAYERS.setdefault(str(xuid), {})
            rec["activity"] = activity.get("id", "")
            rec["game"] = GAME_NAMES.get(str(activity.get("scid", "")).lower(), rec.get("game", ""))
            rec["state"] = payload.get("state", "active")
            rec["updated"] = time.time()
        print("ACTIVITY:", rec["activity"], "->", presence_text(rec["activity"]))

        return Response(status=201)

    with STATE_LOCK:
        rec = PLAYERS.setdefault(str(xuid), {})
        rec["state"] = "offline"
        rec["activity"] = ""
        rec["updated"] = time.time()

    return Response(status=204)


# Player profiles and presence for the closed ecosystem.
#
# The Xbox shell that drew profile cards and kept a friends list is gone, so WinDurango's
# SystemUI::ShowProfileCardAsync asks this server for the player instead (see HDProfileCard.cpp).
# PRESENCE is what the game itself reports to /users/xuid(N)/devices/current/titles/current.

# The title's rich-presence ids: a fixed table of ten (exe .data 0x1cbcc70).
PRESENCE_TEXT = {
    "Menu": "In the menus",
    "Lobby": "In a lobby",
    "Waiting": "Waiting for players",
    "Story_Dungeon": "Playing a story dungeon",
    "Extra_Dungeon": "Playing an extra dungeon",
    "Event_Dungeon": "Playing an event dungeon",
    "Infinity_Dungeon": "Exploring the Infinity Dungeon",
    "Customize_Menu": "Customising their character",
    "Shop": "Browsing the shop",
    "HappyCards": "Opening Happy Cards",
}

# xuid -> {"gamertag": str, "activity": str, "state": str, "updated": float}
PLAYERS = {}


def presence_text(activity):
    if not activity:
        return "Online"
    return PRESENCE_TEXT.get(activity, activity.replace("_", " "))


def game_of(base, rec):
    """The game this player is in: what their presence said, else where their requests came from."""
    return rec.get("game") or LAST_GAME.get(str(base), "")


def player_record(xuid):
    base = base_of(xuid)
    with STATE_LOCK:
        rec = dict(PLAYERS.get(str(xuid), {}))
    if base is not None:
        online = is_online(base)
    else:
        online = bool(rec) and rec.get("state") != "offline"
    activity = rec.get("activity", "") if online else ""
    return {
        "xuid": str(xuid),
        "gamertag": gamertag_for(xuid),
        "online": online,
        "activity": activity,
        "presenceText": presence_text(activity) if online else "Offline",
        "game": game_of(base, rec) if online else "",
    }


@app.route("/users/xuid(<xuid>)/profile", methods=["GET"])
def player_profile(xuid):
    log_request("PROFILE")
    print("XUID:", xuid)
    return json_response(player_record(xuid))


# Xbox social

# The closed-ecosystem friends list.
#
# XSAPI's parser for this route (Microsoft.Xbox.Services.dll 0x9a98b0 / 0x9a9e10 / 0x9a8eb0)
# requires "people" to be present AND a JSON array, and each entry to carry "xuid" as a STRING;
# a JSON number will not parse. totalCount, isFavorite, isFollowingCaller and socialNetworks are
# optional. Nothing else on this route is read - the game has no friends roster of its own, it
# uses this set to mark friends in the lobby, to filter friends-only lobbies and for the friend
# bonus - so the visible list is drawn by WinDurango instead.
FRIENDS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "friends.json")

# xuid -> sorted list of friend xuids (a friendship is stored on both sides)
FRIENDS = {}


def load_friends():
    try:
        with open(FRIENDS_FILE, encoding="utf-8") as handle:
            return {str(k): sorted({str(x) for x in v}) for k, v in json.load(handle).items()}
    except (OSError, ValueError):
        return {}


def save_friends():
    try:
        os.makedirs(os.path.dirname(FRIENDS_FILE), exist_ok=True)
        with open(FRIENDS_FILE, "w", encoding="utf-8") as handle:
            json.dump(FRIENDS, handle, indent=1, sort_keys=True)
    except OSError as error:
        print("COULD NOT SAVE FRIENDS:", error)


FRIENDS.update(load_friends())


def friends_of(xuid):
    with STATE_LOCK:
        return list(FRIENDS.get(str(xuid), []))


def set_friendship(a, b, make_friends):
    a, b = str(a), str(b)
    if a == b:
        return False
    with STATE_LOCK:
        for owner, other in ((a, b), (b, a)):
            current = set(FRIENDS.get(owner, []))
            if make_friends:
                current.add(other)
            else:
                current.discard(other)
            FRIENDS[owner] = sorted(current)
        save_friends()
    return True


@app.route("/users/<owner_id>/people", methods=["GET"])
def social_people(owner_id):
    log_request("SOCIAL / PEOPLE")

    owner = digits_of(owner_id)
    max_items = int(request.args.get("maxItems", 100) or 100)
    people = []
    # privacy_patch v2: a friends list only for its own player (the game asks for its own, signed in); anyone else
    # gets an empty one.
    own = verified_caller_xuid() is not None and in_caller_block(owner)
    for friend in (friends_of(owner)[:max_items] if own else []):
        people.append({
            "xuid": str(friend),                    # MUST be a string
            "isFavorite": False,
            "isFollowingCaller": True,
            "socialNetworks": ["LegacyXboxLiveFriends"],
        })

    print("OWNER:", owner, "FRIENDS:", len(people))
    return json_response({"people": people, "totalCount": len(people)})


# Claiming an identity. The play script calls this before the game starts, the first time on each PC, and
# writes the answer into winDurango.json; from then on every request carries ";WD1.<xuidBase>.<secret>".
# A gamertag is only the name a player goes by: two players may pick the same one, so everything that has to tell
# players apart (friends, sessions, saves) goes by xuid.
GAMERTAG_OK = re.compile(r"^[A-Za-z][A-Za-z0-9 ]{0,14}$")


@app.route("/windurango/identity/claim", methods=["POST"])
def identity_claim():
    """Register this player, or check back in.

    {gamertag, xuidBase, secret}: xuidBase is the random block the player's PC generated (tools/identity.py).
    With the matching secret this is a check-in (and may change the gamertag). Without one it registers that
    block if it is valid and free; a block someone else already has gets 409, and the PC makes a new one. A new
    player needs nothing but a gamertag, and it does not have to be one nobody else uses.
    """
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        body = {}
    gamertag = str(body.get("gamertag") or "").strip()
    wanted = str(body.get("xuidBase") or "").strip()
    secret = str(body.get("secret") or "")

    if not GAMERTAG_OK.match(gamertag):
        return json_response({"ok": False, "reason": "gamertags are 1-15 letters, digits or spaces "
                                                     "and start with a letter"}, 400)

    with STATE_LOCK:
        wanted = MOVED_XUIDS.get(wanted, wanted)   # moved off the old sequential XUIDs: answer with the new

        is_new = wanted not in PLAYER_REGISTRY
        if is_new:
            refusal = registration_refusal()
            if refusal is not None:
                return refusal

        if wanted in PLAYER_REGISTRY:
            record = PLAYER_REGISTRY[wanted]
            if not secret or not hmac.compare_digest(record.get("secret", ""), secret):
                return json_response({"ok": False, "reason": "that xuid belongs to someone else"}, 409)
            base = wanted
        elif wanted:
            if not valid_xuid_base(wanted):
                return json_response({"ok": False, "reason": "that is not a valid xuid block"}, 400)
            if client_claim_refused(wanted):   # client updater: owner rights come with an owner's block
                return json_response({"ok": False, "reason": "that xuid block is reserved for this server's "
                                                             "owner"}, 403)
            if base_of(wanted) is not None or base_of(int(wanted) + XUIDS_PER_PLAYER - 1) is not None:
                return json_response({"ok": False, "reason": "that xuid overlaps another player"}, 409)
            base = wanted
            PLAYER_REGISTRY[base] = {"created": utc_now()}
            # A PC that already has a secret for this block (this server lost its player list, or the player
            # moved servers) keeps it: handing out a new one would lock that PC out on its next check-in.
            if re.fullmatch(r"[0-9a-f]{32}", secret):
                PLAYER_REGISTRY[base]["secret"] = secret
        else:
            base = random_xuid_base()
            PLAYER_REGISTRY[base] = {"created": utc_now()}

        record = PLAYER_REGISTRY[base]
        record["gamertag"] = gamertag
        record.setdefault("secret", secrets.token_hex(16))
        save_players()

        # The play script checks in right before the game starts: whatever the last session said it was doing
        # is over (it would otherwise show until the new session reports something). So is the game data its last
        # search had: the game may have fetched new ServerData since.
        for i in range(XUIDS_PER_PLAYER):
            PLAYERS.pop(str(int(base) + i), None)
        LAST_TICKET_VERSION.pop(base, None)

    print("[IDENTITY] %s is xuidBase %s" % (gamertag, base))
    return json_response({"ok": True, "xuidBase": base, "secret": record["secret"],
                          "gamertag": gamertag, "xuid": str(int(base) + 1)})


# New players per address: plenty for friends sharing one connection, but a script cannot fill the player list.
REGISTRATIONS_PER_HOUR = 10
REGISTRATIONS = {}   # player's address -> times of its recent registrations


def registration_refusal():
    """None when this address may register another new player now, else the 429 response. Holds STATE_LOCK."""
    address = request.remote_addr or "?"
    now = time.time()
    for known in list(REGISTRATIONS):
        REGISTRATIONS[known] = [t for t in REGISTRATIONS[known] if now - t < 3600]
        if not REGISTRATIONS[known]:
            del REGISTRATIONS[known]
    if len(REGISTRATIONS.get(address, [])) >= REGISTRATIONS_PER_HOUR:
        print("[IDENTITY] refused a new player from %s: too many new players this hour" % address)
        return json_response({"ok": False, "reason": "too many new players from this address - try again later"},
                             429)
    REGISTRATIONS.setdefault(address, []).append(now)
    return None


@app.route("/windurango/identity/whoami", methods=["GET"])
def identity_whoami():
    # privacy_patch v2: who you are only for a request that proves it. A bare X-WinDurango-Player header (or a wrong
    # secret) got that base's gamertag, and "verified" false/null told whether the base was registered. No shipped
    # client calls this route.
    if g.get("caller_verified") is not True:
        return json_response({"xuidBase": None, "xuid": None, "gamertag": None, "verified": False})
    base = g.get("caller_base")
    return json_response({
        "xuidBase": base,
        "xuid": g.get("caller_xuid"),
        "gamertag": PLAYER_REGISTRY.get(base, {}).get("gamertag") if base else None,
        "verified": g.get("caller_verified"),
    })


def player_summary(base):
    """What the lobby list and the friends page show about one player. Never includes the secret."""
    record = PLAYER_REGISTRY.get(str(base), {})
    xuid = str(int(base) + 1)
    online = is_online(base)
    if tuning("fastPaths"):   # (tuning_patch) one dict copy is atomic under the GIL: no global lock per player
        rec = dict(PLAYERS.get(xuid, {}))
    else:
        with STATE_LOCK:
            rec = dict(PLAYERS.get(xuid, {}))
    activity = rec.get("activity", "")
    return {
        "xuid": xuid,
        "gamertag": record.get("gamertag", "Player " + xuid),
        "online": online,
        "activity": activity if online else "",
        "presenceText": presence_text(activity) if online else "Offline",
        "game": game_of(base, rec) if online else "",
    }


PRIVACY_EVERYONE = {"at": 0.0, "value": None}   # privacy_patch v1: every player's summary, fastPaths-cached 2 s


def privacy_everyone():
    """Every registered player's summary, online first. With fastPaths built at most once every
    TUNING_RECHECK_SECONDS (the old shared-answer cache of this route, now below the per-caller filter)."""
    now = time.monotonic()
    hit = PRIVACY_EVERYONE
    if tuning("fastPaths") and hit["value"] is not None and now - hit["at"] < TUNING_RECHECK_SECONDS:
        return hit["value"]
    everyone = sorted((player_summary(b) for b in PLAYER_REGISTRY),
                      key=lambda p: (not p["online"], p["gamertag"].lower()))
    PRIVACY_EVERYONE["at"], PRIVACY_EVERYONE["value"] = now, everyone
    return everyone


@app.route("/windurango/players", methods=["GET"])
def players_list():
    """Who is online (privacy_patch v1): the owners see every registered player; a verified player sees only
    themselves and their friends; anyone else only the number online."""
    everyone = privacy_everyone()
    online = sum(1 for p in everyone if p["online"])
    base = g.get("caller_base") if g.get("caller_verified") is True else None
    if base and client_is_owner(base):
        shown = everyone
    elif base:
        mine = primary_xuid(base)
        allowed = set(friends_of(mine)) | {mine}
        shown = [p for p in everyone if p["xuid"] in allowed]
    else:
        shown = []
    visible = {p["xuid"] for p in shown}
    versions = client_versions_by_xuid()
    return json_response({"players": shown, "online": online,
                          "clientVersions": {x: v for x, v in versions.items() if x in visible}})


def privacy_viewer():
    """(privacy_patch v2) The caller when the request proves who it is (the WD1 secret): (xuidBase, owner?, the
    primary XUIDs of themselves and their friends); else (None, False, frozenset())."""
    base = g.get("caller_base") if g.get("caller_verified") is True else None
    if not base:
        return None, False, frozenset()
    mine = primary_xuid(base)
    return base, client_is_owner(base), frozenset(friends_of(mine)) | {mine}


def privacy_may_see(xuid):
    """(privacy_patch v2) Whether the verified caller may see this player's presence and achievements: the XUID is
    one of the caller's own four, or one of a friend's four, or the caller is one of this server's owners."""
    base, owner, allowed = privacy_viewer()
    if not base:
        return False
    if owner or in_caller_block(xuid):
        return True
    target = base_of(xuid)
    return target is not None and primary_xuid(target) in allowed


def privacy_hidden(summary):
    """(privacy_patch v2) A player summary / profile with the name kept and what they are doing left out."""
    return dict(summary, online=False, activity="", presenceText="Offline", game="")


def stage_text(stage):
    """"mission_3_1" -> "Mission 3-1" (the dungeon a Happy Dungeons ticket is for)."""
    match = re.fullmatch(r"mission_(\d+)_(\d+)", str(stage or ""))
    return "Mission %s-%s" % match.groups() if match else (str(stage or "") or "Any")


# Lobbies: Happy Dungeons players hosting a lobby (matchmaking as host) and players looking for one, for the
# players page. Joining from there works through matchmaking: the page records which lobby the player wants
# (/windurango/lobbies/join), and the player's next search in the game ("join a game") is put into that lobby.
@app.route("/windurango/lobbies", methods=["GET"])
def lobbies_list():
    now = time.time()
    lobbies, searching = [], []
    base = g.get("caller_base") if g.get("caller_verified") is True else None
    with STATE_LOCK:
        # sameVersion: the lobby has the game data of the caller's last ticket (null: one of the two is not known).
        my_version = LAST_TICKET_VERSION.get(base) if base else None
        my_fps = fps_of(base) if base else None   # (fps_split v3)
        for key in list(MM_LOBBIES):
            lobby = MM_LOBBIES.get(key)
            if not lobby or now - lobby["refreshed"] > LOBBY_FRESH_SECONDS:
                continue
            found = open_lobby(key, now)
            session = found[1] if found else session_shared_from_ref(lobby["ref"])   # (tuning_patch: read-only)
            if not isinstance(session, dict):
                continue
            members = session.get("members") if isinstance(session.get("members"), dict) else {}
            host = session_host_xuid(session) or lobby["owner"]
            max_members = lobby_size(lobby["ref"])
            version = lobby_version(lobby, session)
            lobbies.append({
                "id": key,
                "host": gamertag_for(host),
                "hostXuid": host,
                "stage": lobby["stage"],
                "stageText": stage_text(lobby["stage"]),
                "players": [{"gamertag": gamertag_for(member_xuid(members[k])), "xuid": member_xuid(members[k]),
                             "host": member_xuid(members[k]) == host,
                             "reserved": members[k].get("reserved") is True} for k in member_keys(members)],
                "maxPlayers": max_members,
                "kind": lobby_kind(lobby),
                "open": found is not None,
                "solo": solo_xuid(host),   # (solo_patch v1) nobody is put into this one; "open" is false for it
                "since": datetime.fromtimestamp(lobby["since"], timezone.utc).isoformat().replace("+00:00", "Z"),
                "version": version,
                "versionText": game_data_text(version),
                "sameVersion": None if my_version is None or version is None else my_version == version,
                **fps_lobby_fields(lobby, session, my_fps, base),   # (fps_split v3) listed, marked
            })
        for ticket in MM_TICKETS.values():
            if ticket["role"] == "seeker":
                searching.append({"gamertag": gamertag_for(ticket["owner"]), "xuid": ticket["owner"],
                                  "stage": ticket["stage"],
                                  "stageText": stage_text(ticket["stage"]),
                                  "fps": ticket_fps(ticket)})   # (fps_split v3)
        wish = None
        if base and JOIN_WISHES.get(base, {}).get("until", 0) > now:
            wish = JOIN_WISHES[base]["lobby"]
    lobbies.sort(key=lambda l: (not l["open"], l["kind"] != "host", l["since"]))
    counts = {"lobbyCount": len(lobbies), "openLobbies": sum(1 for l in lobbies if l["open"]),
              "searchingCount": len(searching)}
    if not base:   # privacy_patch v2: the list is for signed-in players (every one of them); anyone else: how many
        return json_response({"game": GAME_NAMES[HD_SCID], "lobbies": [], "searching": [], "joining": None,
                              "myVersion": None, "myVersionText": game_data_text(None), "signIn": True, **counts})
    return json_response({"game": GAME_NAMES[HD_SCID], "lobbies": lobbies, "searching": searching,
                          "joining": wish, "myVersion": my_version, "myVersionText": game_data_text(my_version),
                          "myFps": my_fps,   # (fps_split v3)
                          **counts})


@app.route("/windurango/lobbies/join", methods=["POST"])
def lobbies_join():
    """{"lobby": "<id from /windurango/lobbies>"}: put me into that lobby the next time I search for a game to
    join in Happy Dungeons (within JOIN_WISH_SECONDS). {"lobby": null} forgets it."""
    base, refusal = require_player()
    if refusal:
        return refusal
    body = request.get_json(silent=True) or {}
    key = body.get("lobby")
    with STATE_LOCK:
        if not key:
            JOIN_WISHES.pop(base, None)
            return json_response({"ok": True, "joining": None})
        if solo_play(base):   # (solo_patch v1) you play alone: turn it off first
            return json_response({"ok": False, "solo": True, "reason": "solo play is on - turn it off in the Solo "
                                                                      "play box above to join other players"}, 409)
        picked = MM_LOBBIES.get(str(key))
        if picked is not None and solo_xuid(picked.get("owner")):   # (solo_patch v1) they play alone
            return json_response({"ok": False, "reason": "that player is playing solo right now"}, 409)
        found = open_lobby(str(key))
        if found is None:
            return json_response({"ok": False, "reason": "that lobby is full or gone"}, 409)
        lobby, session, _free = found
        if base_of(lobby["owner"]) == base:
            return json_response({"ok": False, "reason": "that is your own lobby"}, 409)
        # Only into a lobby with your game data (as far as it is known: your last search in the game says yours;
        # without one, the search you are about to start is checked when it comes).
        mine, theirs = LAST_TICKET_VERSION.get(base), lobby_version(lobby, session)
        if mine is not None and theirs is not None and mine != theirs:
            return json_response({"ok": False, "sameVersion": False,
                                  "reason": "different game data - whoever is behind must update (press Play in Happy Dungeons Online) or restart the game"}, 409)
        fps_refused = fps_lobby_refusal(base, lobby, session)   # (fps_split v3) only when both rates are known
        if fps_refused:
            return json_response({"ok": False, "sameFps": False, "reason": fps_refused}, 409)
        JOIN_WISHES[base] = {"lobby": str(key), "until": time.time() + JOIN_WISH_SECONDS}
        host = gamertag_for(session_host_xuid(session) or lobby["owner"])
        print("[MM] %s wants to join %s's lobby" % (PLAYER_REGISTRY[base].get("gamertag"), host))
        for ticket in sorted(MM_TICKETS.values(), key=lambda t: t["created"]):
            if ticket["role"] == "seeker" and base_of(ticket["owner"]) == base:
                place_seeker(ticket)   # already searching: straight in
                break
    return json_response({"ok": True, "joining": str(key), "host": host, "stageText": stage_text(lobby["stage"]),
                          "howTo": "In Happy Dungeons, search for a game to join (not host)%s within %d minutes: "
                                   "you will be put into %s's lobby." % (mm_stage_hint(lobby["stage"]),   # lobby_patch
                                                                          JOIN_WISH_SECONDS // 60, host)})


# solo_patch v1 - the solo play switch.
#
#   POST /windurango/me/solo {"solo": true}    Authorization: XBL3.0 x=1;WD1.<xuidBase>.<secret>
#   GET  /windurango/me/solo                   reads it without changing it
#   -> 200 {"ok": true, "solo": <the new state>, "soloApi": 1, "text": "<what to tell the player>"}
#      401 signed out, 403 with the wrong secret (require_player), 400 for a body that is not {"solo": true|false}.
#
# Turning it on also forgets a Join this player had armed (they are about to play alone), and takes their lobby out
# of everyone's placement paths at once - within one lobby-list refresh nobody can pick it any more. Players who are
# already in their lobby stay there: leaving a session is the game's own (Leave Session).
@app.route("/windurango/me/solo", methods=["GET", "POST"])
def me_solo():
    """The caller's own solo play switch (see above). Only for a verified player, and only their own."""
    base, refusal = require_player()
    if refusal:
        return refusal
    if request.method == "POST":
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not isinstance(body.get("solo"), bool):
            return json_response({"ok": False, "reason": "badRequest",
                                  "text": 'Send {"solo": true} to play alone, or {"solo": false} to play with '
                                          'others again.'}, 400)
        with STATE_LOCK:
            value = set_solo_play(base, body["solo"])
            if value:   # nothing of yours can be waiting to join someone while you play alone
                JOIN_WISHES.pop(base, None)
                FRIEND_JOIN_RESULTS.pop(base, None)
    else:
        value = solo_play(base)
    return json_response({"ok": True, "solo": value, "soloApi": SOLO_API, "text": SOLO_TEXT[value]})


# Discord Rich Presence. Whoever runs the server creates one Discord application (its name is the "Playing ..."
# line) and puts its Application ID in discord_app_id.txt next to this file. Every player's WinDurango asks for it
# here, so friends get Rich Presence without setting anything up themselves.
DISCORD_APP_ID_FILE = os.path.join(ROOT, "discord_app_id.txt")


@app.route("/windurango/discord", methods=["GET"])
def discord_config():
    try:
        with open(DISCORD_APP_ID_FILE, encoding="utf-8") as handle:
            app_id = handle.readline().strip()
    except OSError:
        app_id = ""
    ok = app_id.isdigit() and 17 <= len(app_id) <= 20
    return json_response({"applicationId": app_id if ok else None})


# Server-delivered ServerData: the event (and any other delivery file) comes from this server.
#
# The game reads its delivery files - the event package EventCampaign.pak, happy_card.pak, Dictionary.pak, the
# Pic_*.jpg banners and so on - from its ServerData slots. WinDurango mirrors this server's set into
# <LocalState>\WinDurango\ServerData\ when the game starts: it downloads what is missing or changed, checks each
# file's sha256 before it swaps it in, and removes the files it got from here that the set no longer lists. A file
# from here wins over the copy Setup put in <game>\ServerData, and for the four files the game's version value is
# made from (DungeonPatch, JsonDiff, EventExtra, EventCampaign) only a file from here counts - so everyone on the same
# rev has the same game data and can play together (see game_version).
#
# To roll out or change an event: put the files in data/serverdata/ (copy each in under another name, then rename
# it, so nobody downloads half a file), list them in data/serverdata/manifest.json and raise its rev:
#     {"rev": 2, "files": ["EventCampaign.pak"]}
# Only files listed there are served, by plain file name ([A-Za-z0-9_.-]). Taking a file off the list (or deleting
# manifest.json) takes it off every player's PC at their next start.
#   GET /windurango/serverdata/manifest       {"rev": n, "files": [{"name", "size", "sha256"}, ...]}
#   GET /windurango/serverdata/files/<name>   the file; ETag "<sha256>" (If-None-Match -> 304); 404 when not listed
# Both are for signed-in players only (401 / 403, like the players page's own routes).
SERVERDATA_DIR = os.path.join(DATA, "serverdata")
SERVERDATA_MANIFEST = os.path.join(SERVERDATA_DIR, "manifest.json")
SERVERDATA_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}")
SERVERDATA_MAX_BYTES = 64 << 20   # delivery files are well under 1 MB; one this big is a mistake and is left out
SERVERDATA_LOCK = threading.Lock()
SERVERDATA_LISTING = {"key": None, "rev": 0, "names": [], "good": False}   # manifest.json as last read
SERVERDATA_HASHES = {}            # name -> ((mtime_ns, size), sha256)
SERVERDATA_SERVED = {"rev": None, "files": None}   # the set the manifest route last answered with


def serverdata_name_ok(name):
    return (isinstance(name, str) and SERVERDATA_NAME.fullmatch(name) is not None and not name.startswith(".")
            and name.lower() != "manifest.json")


def serverdata_listing():
    """(rev, [names]) from data/serverdata/manifest.json, read again whenever the file changed; (0, []) without one.
    A manifest.json that cannot be read keeps the last good listing (None when there never was one)."""
    try:
        info = os.stat(SERVERDATA_MANIFEST)
        key = (info.st_mtime_ns, info.st_size)
    except FileNotFoundError:
        key = "none"
    except OSError:
        key = "unreadable"
    with SERVERDATA_LOCK:
        if SERVERDATA_LISTING["key"] == key and key != "unreadable":
            return (SERVERDATA_LISTING["rev"], list(SERVERDATA_LISTING["names"])) if SERVERDATA_LISTING["good"] \
                else None
    if key == "none":
        with SERVERDATA_LOCK:
            SERVERDATA_LISTING.update(key=key, rev=0, names=[], good=True)
        return 0, []

    doc, problem = None, None
    try:
        with open(SERVERDATA_MANIFEST, encoding="utf-8-sig") as handle:
            doc = json.load(handle)
    except (OSError, ValueError) as error:
        problem = "cannot be read (%s)" % error
    rev = doc.get("rev") if isinstance(doc, dict) else None
    listed = doc.get("files") if isinstance(doc, dict) else None
    if problem is None and (not isinstance(rev, int) or isinstance(rev, bool) or rev < 0
                            or not isinstance(listed, list)):
        problem = 'is not {"rev": <number>, "files": ["<file name>", ...]}'
    if problem:
        with SERVERDATA_LOCK:
            SERVERDATA_LISTING["key"] = key
            good, rev = SERVERDATA_LISTING["good"], SERVERDATA_LISTING["rev"]
            names = list(SERVERDATA_LISTING["names"])
        print("[SERVERDATA] data/serverdata/manifest.json %s - %s" % (
            problem, "still serving rev %d" % rev if good else "serving nothing until it is fixed"))
        return (rev, names) if good else None

    names, seen = [], set()
    for name in listed:
        if not serverdata_name_ok(name):
            print("[SERVERDATA] manifest.json lists %r, which is not a plain file name - left out" % (name,))
        elif name.lower() in seen:
            print("[SERVERDATA] manifest.json lists %s twice - once is enough" % name)
        else:
            seen.add(name.lower())
            names.append(name)
    with SERVERDATA_LOCK:
        SERVERDATA_LISTING.update(key=key, rev=rev, names=names, good=True)
    return rev, names


def serverdata_file(name, want_data=False):
    """(size, sha256, bytes or None) of a listed file as it is on disk now. None when it is not there (or is not a
    file, or is over SERVERDATA_MAX_BYTES); "busy" while it is being written. The hash is kept per (mtime, size)."""
    path = os.path.join(SERVERDATA_DIR, name)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as handle:
            before = os.fstat(handle.fileno())
            key = (before.st_mtime_ns, before.st_size)
            if before.st_size > SERVERDATA_MAX_BYTES:
                return None
            with SERVERDATA_LOCK:
                cached = SERVERDATA_HASHES.get(name)
            data = None
            if want_data or not cached or cached[0] != key:
                data = handle.read(SERVERDATA_MAX_BYTES + 1)
                after = os.fstat(handle.fileno())
                if (after.st_mtime_ns, after.st_size) != key or len(data) != before.st_size:
                    return "busy"
    except OSError:
        return None
    if cached and cached[0] == key:
        digest = cached[1]
    else:
        digest = hashlib.sha256(data).hexdigest()
        with SERVERDATA_LOCK:
            SERVERDATA_HASHES[name] = (key, digest)
    return before.st_size, digest, data if want_data else None


def serverdata_set():
    """(rev, [{"name", "size", "sha256"}]) as served now; None when there is no good manifest.json, "busy" while
    a listed file is being written (players keep what they have and ask again next time)."""
    listing = serverdata_listing()
    if listing is None:
        return None
    rev, names = listing
    files = []
    for name in names:
        found = serverdata_file(name)
        if found == "busy":
            return "busy"
        if found is None:
            print("[SERVERDATA] manifest.json lists %s, but data/serverdata has no such file (or it is over %d MB)"
                  " - left out" % (name, SERVERDATA_MAX_BYTES >> 20))
            continue
        files.append({"name": name, "size": found[0], "sha256": found[1]})
    fingerprint = {entry["name"]: entry["sha256"] for entry in files}
    with SERVERDATA_LOCK:
        last = dict(SERVERDATA_SERVED)
        SERVERDATA_SERVED.update(rev=rev, files=fingerprint)
    if last["files"] is not None and last["rev"] == rev and last["files"] != fingerprint:
        print("[SERVERDATA] WARNING: the ServerData files changed but manifest.json still says rev %d - raise rev "
              "whenever you change them" % rev)
    if last["rev"] != rev or last["files"] != fingerprint:
        print("[SERVERDATA] serving rev %d: %s" % (rev, ", ".join("%s (%d bytes)" % (e["name"], e["size"])
                                                                  for e in files) or "no files"))
    return rev, files


def serverdata_unavailable(reason):
    response = json_response({"error": "serverdata_unavailable", "reason": reason}, 503)
    response.headers["Retry-After"] = "10"
    return response


@app.route("/windurango/serverdata/manifest", methods=["GET"])
def serverdata_manifest():
    base, refusal = require_player()
    if refusal:
        return refusal
    served = serverdata_set()
    if served is None:
        return serverdata_unavailable("this server's data/serverdata/manifest.json is broken")
    if served == "busy":
        return serverdata_unavailable("the ServerData files are being updated")
    rev, files = served
    print("[SERVERDATA] %s asked for the ServerData list (rev %d, %d file%s)" % (
        PLAYER_REGISTRY[base].get("gamertag"), rev, len(files), "" if len(files) == 1 else "s"))
    return json_response({"rev": rev, "files": files})


@app.route("/windurango/serverdata/files/<path:name>", methods=["GET"])
def serverdata_download(name):
    base, refusal = require_player()
    if refusal:
        return refusal
    # Only a name manifest.json lists (a plain file name, so nothing outside data/serverdata can be named).
    listing = serverdata_listing()
    if not serverdata_name_ok(name) or name not in (listing[1] if listing else []):
        return json_response({"error": "not_in_serverdata"}, 404)
    found = serverdata_file(name, want_data=True)
    if found == "busy":
        return serverdata_unavailable("%s is being updated" % name)
    if found is None:
        return json_response({"error": "not_in_serverdata"}, 404)
    size, digest, data = found
    response = Response(data, mimetype="application/octet-stream")
    response.set_etag(digest)
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response = response.make_conditional(request)
    print("[SERVERDATA] %s %s %s (%d bytes)" % (
        PLAYER_REGISTRY[base].get("gamertag"), "already has" if response.status_code == 304 else "downloaded",
        name, size))
    return response


def serverdata_summary():
    """The startup line about the ServerData this server hands out."""
    served = serverdata_set()
    if served is None:
        return "ServerData: data/serverdata/manifest.json is broken - players get no ServerData until it is fixed"
    if served == "busy":
        return "ServerData: files are being written right now"
    rev, files = served
    return "ServerData: rev %d, %s" % (rev, ", ".join(e["name"] for e in files) or "no files (none delivered)")


# ---------------------------------------------------------------------------
# Event switcher (owners only).
#
# The owners of this server switch the Happy Dungeons event for everyone - from the players page or with curl:
#   GET  /windurango/admin/events                       the library, which event is on, the ServerData rev, and which
#                                                       online players still have other game data
#   POST /windurango/admin/events/activate {"event": id}   serve that event's files from the next game start on
#   POST /windurango/admin/events/deactivate               serve no event
# Owners: server_config.json "owners": ["<xuidBase>", ...] (any of the player's four XUIDs works). It is read again
# whenever the file changes, so adding an owner needs no restart; without "owners" nobody is one. A request without
# a player key gets 401, a player who is not an owner (or whose key is wrong) 403.
#
# The library is data/event_library/: catalog.json (build_library.py) and <id>/<file> for every file an event
# serves - its EventCampaign.pak (restamped v1.15.0 build <rev>), for some events also event_gacha.pak or Pic_*.
# Activating copies those files into data/serverdata/ (each under a temporary name, then renamed over it), then
# rewrites data/serverdata/manifest.json in one step (a temporary file renamed over it) with "rev" one higher and an
# "event" record of what it did. Files the previous event added that this one does not use are taken off the list
# and deleted. A file manifest.json lists that the switcher did not add is left alone - only EventCampaign.pak is
# always the switcher's. Every change is appended to data/event_switch_log.jsonl.
#
# Players get the new event at their next game start (WinDurango mirrors the ServerData set then). An event's build
# number is part of the game's version value, so a player who has not restarted yet is only matched with players
# who have the same event (same_game_data), and the game itself refuses the others' sessions.
EVENT_LIBRARY_DIR = os.path.join(DATA, "event_library")
EVENT_CATALOG_FILE = os.path.join(EVENT_LIBRARY_DIR, "catalog.json")
EVENT_SWITCH_LOG = os.path.join(DATA, "event_switch_log.jsonl")
EVENT_REMOVED_DIR = os.path.join(DATA, "event_switch_removed")   # hand-placed files the switcher took off the list
EVENT_OWNERS_FILE = os.path.join(ROOT, "server_config.json")
EVENT_SLOT_FILE = "EventCampaign.pak"            # slot 7: the event itself, always the switcher's
EVENT_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
EVENT_SHA256 = re.compile(r"[0-9a-f]{64}")
EVENT_SWITCH_LOCK = threading.Lock()             # one switch at a time (the server is one process)
EVENT_CACHE_LOCK = threading.Lock()
EVENT_CATALOG_CACHE = {"key": None, "events": None, "problem": None}
EVENT_OWNERS_CACHE = {"key": None, "owners": frozenset()}
EVENT_FILE_HASHES = {}                           # path -> ((mtime_ns, size), sha256)


def event_version_value(build):
    """The game's version value with no DungeonPatch and event build `build` loaded (-1: no event). Same formula as
    game_data_text: 128140489 for build 1, 1486770622 for none."""
    return toylogic_hash(b"%04d%02d%03d%03d%03d%08x%08x" % (15, 0, -1, build, -1, toylogic_hash(b"0000"), 0))


def event_owners():
    """The owners' XUIDs (str) from server_config.json "owners", read again when the file changes."""
    try:
        info = os.stat(EVENT_OWNERS_FILE)
        key = (info.st_mtime_ns, info.st_size)
    except OSError:
        return frozenset()
    with EVENT_CACHE_LOCK:
        if EVENT_OWNERS_CACHE["key"] == key:
            return EVENT_OWNERS_CACHE["owners"]
    owners = set()
    try:
        with open(EVENT_OWNERS_FILE, encoding="utf-8-sig") as handle:
            listed = json.load(handle).get("owners") or []
        if not isinstance(listed, list):
            raise ValueError('"owners" is not a list')
        for value in listed:
            text = str(value).strip()
            if text.isdigit():
                owners.add(str(int(text)))
            else:
                print('[EVENTS] server_config.json "owners" has %r, which is not a XUID - left out' % (value,))
    except (OSError, ValueError, AttributeError) as error:
        print('[EVENTS] server_config.json "owners" cannot be read (%s): nobody is an owner' % error)
        owners = set()
    with EVENT_CACHE_LOCK:
        EVENT_OWNERS_CACHE.update(key=key, owners=frozenset(owners))
    return frozenset(owners)


def require_owner():
    """(the caller's xuidBase, None) for an owner, else (None, the 401 / 403 response)."""
    base, refusal = require_player()
    if refusal:
        return None, refusal
    low = int(base)
    if not any(low <= int(owner) < low + XUIDS_PER_PLAYER for owner in event_owners()):
        return None, json_response({"ok": False, "reason": "only this server's owners can switch events"}, 403)
    return base, None


def event_file_sha256(path):
    """sha256 of a file, kept per (mtime, size); None when it is not there."""
    try:
        info = os.stat(path)
    except OSError:
        return None
    key = (info.st_mtime_ns, info.st_size)
    with EVENT_CACHE_LOCK:
        cached = EVENT_FILE_HASHES.get(path)
    if cached and cached[0] == key:
        return cached[1]
    try:
        with open(path, "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return None
    with EVENT_CACHE_LOCK:
        EVENT_FILE_HASHES[path] = (key, digest)
    return digest


def event_catalog():
    """([event], None) from data/event_library/catalog.json - read again when it changes - or (None, problem).
    Each event: id, name, description, status, switchable, blockedReason, caveat, rev, versionValue, archiveId,
    eventNumbers, originalStart, originalEnd, missions, files [{name, size, sha256}], problem (None or why it
    cannot be switched to)."""
    try:
        info = os.stat(EVENT_CATALOG_FILE)
        key = (info.st_mtime_ns, info.st_size)
    except FileNotFoundError:
        return None, "there is no event library on this server (data/event_library/catalog.json)"
    except OSError as error:
        return None, "data/event_library/catalog.json cannot be read (%s)" % error
    with EVENT_CACHE_LOCK:
        if EVENT_CATALOG_CACHE["key"] == key:
            return EVENT_CATALOG_CACHE["events"], EVENT_CATALOG_CACHE["problem"]
    events, problem = None, None
    try:
        with open(EVENT_CATALOG_FILE, encoding="utf-8-sig") as handle:
            doc = json.load(handle)
        listed = doc.get("events") if isinstance(doc, dict) else None
        if not isinstance(listed, list):
            raise ValueError('it has no "events" list')
        events, seen_ids, seen_revs = [], set(), set()
        for raw in listed:
            if not isinstance(raw, dict):
                raise ValueError("an event is not an object")
            event_id, rev = raw.get("id"), raw.get("rev")
            if not isinstance(event_id, str) or not EVENT_ID.fullmatch(event_id) or event_id in seen_ids:
                raise ValueError("event id %r is not a unique lower-case name" % (event_id,))
            if not isinstance(rev, int) or isinstance(rev, bool) or not 0 <= rev <= 999 or rev in seen_revs:
                raise ValueError("%s: build number %r is not a unique 0..999" % (event_id, rev))
            seen_ids.add(event_id)
            seen_revs.add(rev)
            files, bad = [], None
            for entry in raw.get("files") or []:
                name = entry.get("name") if isinstance(entry, dict) else None
                if not serverdata_name_ok(name) or not EVENT_SHA256.fullmatch(str(entry.get("sha256"))) \
                        or not isinstance(entry.get("size"), int) or any(f["name"] == name for f in files):
                    bad = "catalog.json lists a file it cannot serve (%r)" % (name,)
                    break
                files.append({"name": name, "size": entry["size"], "sha256": entry["sha256"]})
            if bad is None and not any(f["name"] == EVENT_SLOT_FILE for f in files):
                bad = "catalog.json lists no %s for it" % EVENT_SLOT_FILE
            value = event_version_value(rev)
            if bad is None and raw.get("multiplayer_version_value") not in (None, value):
                bad = "its version value in catalog.json is not the one build %d makes" % rev
            window = raw.get("original_window_utc") if isinstance(raw.get("original_window_utc"), dict) else {}
            events.append({
                "id": event_id, "name": str(raw.get("name") or event_id),
                "description": str(raw.get("description") or ""), "status": str(raw.get("status") or ""),
                "switchable": raw.get("switchable") is True, "blockedReason": raw.get("blocked_reason"),
                "caveat": raw.get("caveat"), "rev": rev, "versionValue": value,
                "archiveId": raw.get("archive_id"), "eventNumbers": raw.get("event_numbers") or [],
                "originalStart": window.get("start"), "originalEnd": window.get("end"),
                "missions": [m.get("name") or m.get("file") for m in raw.get("missions") or [] if isinstance(m, dict)],
                "files": files, "problem": bad,
            })
    except (OSError, ValueError, AttributeError, TypeError) as error:
        events, problem = None, "data/event_library/catalog.json is not a usable catalogue (%s)" % error
        print("[EVENTS] %s" % problem)
    with EVENT_CACHE_LOCK:
        EVENT_CATALOG_CACHE.update(key=key, events=events, problem=problem)
    return events, problem


def event_library_problem(event):
    """None when every file of the event is in the library as catalog.json describes it, else what is wrong."""
    if event["problem"]:
        return event["problem"]
    for entry in event["files"]:
        path = os.path.join(EVENT_LIBRARY_DIR, event["id"], entry["name"])
        digest = event_file_sha256(path)
        if digest is None:
            return "event_library/%s/%s is missing" % (event["id"], entry["name"])
        if digest != entry["sha256"] or os.path.getsize(path) != entry["size"]:
            return "event_library/%s/%s is not the file catalog.json describes" % (event["id"], entry["name"])
        if entry["name"] == EVENT_SLOT_FILE:
            with open(path, "rb") as handle:
                head = handle.read(12)
            version = struct.unpack_from("<I", head, 8)[0] if len(head) == 12 else None
            if head[:2] != b"KP" or version != 1500000 + event["rev"]:
                return "event_library/%s/%s is not a v1.15.0 build %d event package" % (
                    event["id"], entry["name"], event["rev"])
    return None


def event_manifest():
    """(manifest.json as a dict, None) - {"rev": 0, "files": []} when there is none - or (None, problem)."""
    try:
        with open(SERVERDATA_MANIFEST, encoding="utf-8-sig") as handle:
            doc = json.load(handle)
    except FileNotFoundError:
        return {"rev": 0, "files": []}, None
    except (OSError, ValueError) as error:
        return None, "data/serverdata/manifest.json cannot be read (%s)" % error
    rev = doc.get("rev") if isinstance(doc, dict) else None
    listed = doc.get("files") if isinstance(doc, dict) else None
    if not isinstance(rev, int) or isinstance(rev, bool) or rev < 0 or not isinstance(listed, list) \
            or not all(isinstance(name, str) for name in listed):
        return None, 'data/serverdata/manifest.json is not {"rev": <number>, "files": ["<file name>", ...]}'
    return doc, None


def event_record(doc):
    record = doc.get("event") if isinstance(doc, dict) else None
    return record if isinstance(record, dict) else {}


def active_event(doc, events):
    """What data/serverdata serves as the event now: None (no EventCampaign.pak listed) or a dict with the library
    event it is (id None: an EventCampaign.pak that is not from the library, e.g. one put there by hand)."""
    if EVENT_SLOT_FILE not in (doc.get("files") or []):
        return None
    digest = event_file_sha256(os.path.join(SERVERDATA_DIR, EVENT_SLOT_FILE))
    record = event_record(doc)
    for event in events or []:
        if any(f["name"] == EVENT_SLOT_FILE and f["sha256"] == digest for f in event["files"]):
            mine = record.get("id") == event["id"]
            return {"id": event["id"], "name": event["name"], "rev": event["rev"], "versionValue": event["versionValue"],
                    "since": record.get("activated") if mine else None,
                    "by": (record.get("by") or {}).get("gamertag") if mine else "put there by hand",
                    "files": [n for n in doc.get("files") if n == EVENT_SLOT_FILE or n in (record.get("files") or [])]}
    return {"id": None, "name": "an EventCampaign.pak that is not in the library" if digest else
            "EventCampaign.pak is listed but not in data/serverdata", "rev": None, "versionValue": None,
            "since": None, "by": None, "files": [EVENT_SLOT_FILE], "sha256": digest}


def event_replace(temp, path):
    """os.replace, retried for a moment on Windows while someone is reading the file."""
    for attempt in range(100):
        try:
            os.replace(temp, path)
            return
        except PermissionError:
            if attempt == 99:
                raise
            time.sleep(0.02)


def event_write_file(path, data):
    """Write a whole file under a temporary name next to it, then rename it over `path`: a reader sees the old file
    or the new one, never part of one."""
    temp = "%s.%s.tmp" % (path, secrets.token_hex(4))
    try:
        with open(temp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        event_replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.remove(temp)


def event_write_json(path, doc):
    event_write_file(path, json.dumps(doc, indent=1).encode("utf-8"))


def event_log(entry):
    try:
        with open(EVENT_SWITCH_LOG, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
    except OSError as error:
        print("[EVENTS] could not write data/event_switch_log.jsonl: %s" % error)


def switch_event(event, base):
    """Serve `event`'s files (None: no event) from the next game start on. Returns (reply, HTTP status)."""
    who = {"xuidBase": base, "gamertag": PLAYER_REGISTRY.get(base, {}).get("gamertag")}
    with EVENT_SWITCH_LOCK:
        doc, problem = event_manifest()
        if problem:
            return {"ok": False, "reason": problem + " - fix it by hand first"}, 409
        rev, listed = doc["rev"], list(doc["files"])
        record = event_record(doc)
        managed = {EVENT_SLOT_FILE} | {n for n in record.get("files") or [] if serverdata_name_ok(n)}
        by_hand = [n for n in listed if n not in managed]
        wanted, skipped = [], []
        for entry in event["files"] if event else []:
            if entry["name"] != EVENT_SLOT_FILE and entry["name"] in by_hand:
                skipped.append(entry["name"])     # the owner's own copy stays
            else:
                wanted.append(entry)
        names = [entry["name"] for entry in wanted]
        new_files = by_hand + names
        on_disk = {n: event_file_sha256(os.path.join(SERVERDATA_DIR, n)) for n in managed | set(names)}
        if sorted(n for n in listed if n in managed) == sorted(names) and record.get("id") == (event or {}).get("id") \
                and all(on_disk.get(entry["name"]) == entry["sha256"] for entry in wanted):
            return {"ok": True, "changed": False, "rev": rev, "skipped": skipped,
                    "note": "that is already what this server serves"}, 200

        # 1. The event's files, each checked against catalog.json, then put in place under a temporary name.
        staged = []
        for entry in wanted:
            path = os.path.join(EVENT_LIBRARY_DIR, event["id"], entry["name"])
            try:
                with open(path, "rb") as handle:
                    data = handle.read()
            except OSError as error:
                return {"ok": False, "reason": "event_library/%s/%s cannot be read (%s)" % (
                    event["id"], entry["name"], error)}, 409
            if hashlib.sha256(data).hexdigest() != entry["sha256"]:
                return {"ok": False, "reason": "event_library/%s/%s is not the file catalog.json describes" % (
                    event["id"], entry["name"])}, 409
            staged.append((entry, data))
        previous = {}   # name -> what it held before it was replaced (None: not there), put back on failure
        try:
            os.makedirs(SERVERDATA_DIR, exist_ok=True)
            for entry, data in staged:
                if on_disk.get(entry["name"]) == entry["sha256"]:
                    continue
                target = os.path.join(SERVERDATA_DIR, entry["name"])
                try:
                    with open(target, "rb") as handle:
                        old = handle.read()
                except FileNotFoundError:
                    old = None
                event_write_file(target, data)
                previous[entry["name"]] = old

            # 2. manifest.json in one step.
            new_doc = dict(doc)
            new_doc["rev"] = rev + 1
            new_doc["files"] = new_files
            new_doc["event"] = {
                "id": event["id"] if event else None, "name": event["name"] if event else None,
                "build": event["rev"] if event else None,
                "versionValue": event["versionValue"] if event else event_version_value(-1),
                "files": names, "activated": utc_now(), "by": who}
            event_write_json(SERVERDATA_MANIFEST, new_doc)
        except OSError as error:
            for name, old in previous.items():
                target = os.path.join(SERVERDATA_DIR, name)
                try:
                    if old is None:
                        os.remove(target)
                    else:
                        event_write_file(target, old)
                except OSError as undo_error:
                    print("[EVENTS] could not put data/serverdata/%s back: %s" % (name, undo_error))
            print("[EVENTS] switching failed, the served set is unchanged: %s" % error)
            return {"ok": False, "reason": "could not write data/serverdata (%s); nothing was changed" % error}, 500

        # 3. What the previous event added and this one does not use: off the list already, now off the disk. A file
        #    that is not from the library (an EventCampaign.pak put there by hand) is moved aside, not deleted - and
        #    so is one the new event's file just replaced.
        library_hashes = {f["sha256"] for e in (event_catalog()[0] or []) for f in e["files"]}
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
        for name, old in previous.items():
            if old is not None and hashlib.sha256(old).hexdigest() not in library_hashes:
                try:
                    os.makedirs(EVENT_REMOVED_DIR, exist_ok=True)
                    event_write_file(os.path.join(EVENT_REMOVED_DIR, "%s-%s" % (stamp, name)), old)
                    print("[EVENTS] data/serverdata/%s was not from the library: a copy is in data/event_switch_removed"
                          % name)
                except OSError as error:
                    print("[EVENTS] could not keep a copy of the replaced data/serverdata/%s: %s" % (name, error))
        for name in sorted(managed - set(new_files)):
            path = os.path.join(SERVERDATA_DIR, name)
            if not os.path.exists(path):
                continue
            try:
                if event_file_sha256(path) in library_hashes:
                    os.remove(path)
                else:
                    kept = os.path.join(EVENT_REMOVED_DIR, "%s-%s" % (stamp, name))
                    os.makedirs(EVENT_REMOVED_DIR, exist_ok=True)
                    event_replace(path, kept)
                    print("[EVENTS] data/serverdata/%s was not from the library: moved to %s" % (
                        name, os.path.relpath(kept, ROOT)))
            except OSError as error:
                print("[EVENTS] could not remove data/serverdata/%s (%s); it is no longer listed, so nobody gets it"
                      % (name, error))

        action = "activate" if event else "deactivate"
        event_log({"time": utc_now(), "action": action, "event": event["id"] if event else None,
                   "build": event["rev"] if event else None, "revFrom": rev, "revTo": rev + 1,
                   "files": new_files, "by": who})
        print("[EVENTS] %s (%s) %s: ServerData rev %d -> %d (%s)" % (
            who["gamertag"], base, "switched to %s \"%s\" (event build %d, game data %d)" % (
                event["id"], event["name"], event["rev"], event["versionValue"]) if event else "turned the event off",
            rev, rev + 1, ", ".join(new_files) or "no files"))
        if skipped:
            print("[EVENTS] kept the owner's own %s (listed in manifest.json by hand)" % ", ".join(skipped))
    serverdata_set()   # log the new set now, not at the next player's request
    return {"ok": True, "changed": True, "rev": rev + 1, "files": new_files, "skipped": skipped,
            "note": "Players get it when they restart Happy Dungeons. Until then they keep the old event and are "
                    "only matched with players who have the same one."}, 200


def event_players(active):
    """Online players and whether the game data in their last ticket is the one the served event makes."""
    want = active["versionValue"] if active and active.get("versionValue") is not None else (
        None if active else event_version_value(-1))
    rows = []
    for base in sorted(PLAYER_REGISTRY):
        if not is_online(base):
            continue
        value = LAST_TICKET_VERSION.get(base)
        rows.append({"gamertag": PLAYER_REGISTRY[base].get("gamertag"), "xuid": primary_xuid(base),
                     "gameData": game_data_text(value) if value is not None else None,
                     "upToDate": None if value is None or want is None else value == want})
    return rows


@app.route("/windurango/admin/events", methods=["GET"])
def admin_events():
    base, refusal = require_owner()
    if refusal:
        return refusal
    events, problem = event_catalog()
    doc, manifest_problem = event_manifest()
    active = active_event(doc, events) if doc is not None else None
    listing = []
    for event in events or []:
        item = dict(event)
        item["problem"] = event_library_problem(event)
        item["active"] = bool(active and active.get("id") == event["id"])
        item["available"] = item["problem"] is None and event["switchable"]
        item["files"] = [{"name": f["name"], "size": f["size"]} for f in event["files"]]
        listing.append(item)
    return json_response({
        "ok": True, "you": player_summary(base),
        "serverDataRev": doc["rev"] if doc is not None else None, "manifestProblem": manifest_problem,
        "library": {"ok": problem is None, "problem": problem, "count": len(listing)},
        "active": active, "noEventVersionValue": event_version_value(-1),
        "events": listing, "players": event_players(active),
        "note": "Players get a new event when they restart Happy Dungeons. Until they do they keep the old one, "
                "and players on different events are kept apart.",
    })


@app.route("/windurango/admin/events/activate", methods=["POST"])
def admin_events_activate():
    base, refusal = require_owner()
    if refusal:
        return refusal
    body = request.get_json(force=True, silent=True)   # force: curl users forget the Content-Type
    wanted = body.get("event") if isinstance(body, dict) else None
    if not isinstance(wanted, str) or not EVENT_ID.fullmatch(wanted):
        return json_response({"ok": False, "reason": 'send {"event": "<id from GET /windurango/admin/events>"}'}, 400)
    events, problem = event_catalog()
    if events is None:
        return json_response({"ok": False, "reason": problem}, 503)
    event = next((e for e in events if e["id"] == wanted), None)
    if event is None:
        return json_response({"ok": False, "reason": "there is no event %r in the library" % wanted}, 404)
    if not event["switchable"]:
        return json_response({"ok": False, "reason": "%s cannot be switched to: %s" % (
            event["name"], event["blockedReason"] or "the catalogue marks it not switchable")}, 409)
    library_problem = event_library_problem(event)
    if library_problem:
        return json_response({"ok": False, "reason": library_problem}, 409)
    reply, status = switch_event(event, base)
    reply["event"] = event["id"]
    return json_response(reply, status)


@app.route("/windurango/admin/events/deactivate", methods=["POST"])
def admin_events_deactivate():
    base, refusal = require_owner()
    if refusal:
        return refusal
    reply, status = switch_event(None, base)
    reply["event"] = None
    return json_response(reply, status)


def event_switcher_summary():
    """The startup line about the event switcher."""
    events, problem = event_catalog()
    owners = event_owners()
    doc, _problem = event_manifest()
    active = active_event(doc, events) if doc is not None else None
    return "Event switcher: %s; %s; serving %s" % (
        problem or "%d events in data/event_library" % len(events),
        "%d owner%s" % (len(owners), "" if len(owners) == 1 else "s") if owners else
        'no owners (add "owners" to server_config.json)',
        ('"%s"' % active["name"]) if active else "no event")


# ---------------------------------------------------------------------------
# Client updates (the launcher's auto-updater) and the minimum client version.
#
# The launcher (Setup.exe /launch) keeps every player's Setup up to date. At each start it asks
#   GET  /windurango/client/latest   (also /windurango/client/release)   the release manifest,
#        data/client/release.json, byte for byte as the owner's PC signed it - this server never signs or changes
#        it. 404 when none is published, 503 while the one there does not check out (bad signature, no usable
#        clientUpdateKey, its file missing or different).
# and when that is newer than what it has installed, downloads
#   GET  /windurango/client/files/<name>   the Setup exe release.json names, nothing else (Range, If-None-Match),
# checks the signature (its own built-in public key), size and sha256, installs it and starts the game. It reports
# what is installed:
#   POST /windurango/client/hello {"version": "2026.9.18.1920"}   ("clientVersion" works too; signed-in players;
#        kept in data/client_versions.json; answers updateRequired / latest / minClientVersion)
#
# The download does NOT need a player key. The file is public in effect (release.json names it and anyone may
# register), it carries no secret, and its integrity comes from the signature and sha256 the launcher checks, never
# from who asked. What a key would add is a way to break updating: a PC whose identity broke (a lost or changed
# secret, a server that lost its player list) could then not fetch the very Setup that repairs it - and the updater
# is the recovery path of last resort, so it must depend on as little as possible. Launchers already out there send
# no key with the download either. What stays limited: a caller without a verified player key gets at most
# CLIENT_OPEN_DOWNLOADS_PER_HOUR downloads per address per hour (429 after), and every download is logged.
#
# server_config.json - read again whenever it changes, no restart:
#   "minClientVersion": "2026.9.19.1200"   a verified player whose last reported version is older - or who never
#          reported one, i.e. a Setup from before the updater - gets 426 {"error": "update_required", "minVersion",
#          "yourVersion", "latest", "reason"} (header X-WinDurango-Min-Client-Version) from every route except
#          /windurango/client/*, /windurango/identity/* (claim, whoami), /windurango/players (Setup's sign-in reads
#          it) and the players page itself. Owners (the event switcher's "owners") are never blocked. A request with
#          no verified player key is not checked: it cannot be tied to a version, and the routes' own rules refuse it
#          anything that belongs to a player. One [UPDATE] line per blocked player per 10 minutes. Not set (the
#          default), empty or not a version: nobody is blocked. The version is what the player's launcher says - it
#          keeps honest old builds out until they update; it is not a lock against a player who lies.
#   "clientUpdateKey": the PUBLIC half of the owner's release signing key, or a list of them (a new key next to the
#          old one while launchers move over, or the offline recovery key). RSA only, 2048-8192 bits, an odd public
#          exponent from 3 to 2^32-1 - exactly what the launcher (ReleaseManifest.cs) checks: .NET <RSAKeyValue>
#          (what the release tool, ReleaseTool.exe, prints), PEM / base64 DER (SubjectPublicKeyInfo or PKCS#1), or a
#          JWK. Without one nothing is served or published. A private key there is refused (and must never be on
#          this server), and so is any other kind of key (P-256, say): no launcher could check what it signs.
#
# Two things outside this section (the patch changes them too):
#   - identify_caller: requests to /windurango/client/* do not count as being online (LAST_SEEN). The launcher says
#     hello at every start and downloads with the player's key; neither is playing.
#   - identity_claim: a block that holds one of the owners' XUIDs is never registered for anyone (403). Owner rights
#     come with the block, so on a server whose player list lacks the owner (a new or restored server) the first
#     caller would otherwise become an owner. Only the block's holder checks in, with its secret; the startup lines
#     warn about an owner with no player here and say how to put the owner back.
#
# Owners only (the event switcher's owner check):
#   GET  /windurango/client/admin/status                  release, keys, minimum, and every player's version
#   POST /windurango/client/admin/min-version {"minVersion": "a.b.c.d" | null, "preview": true}
#          preview: who would be blocked (online now / in all); without it: writes server_config.json (everything
#          else in it kept). Refused (409) above the published release - nobody could update to it - unless "force".
#   POST /windurango/client/publish   multipart/form-data: release=@release.json file=@<the Setup exe>
#          checks the signature with clientUpdateKey, the product, that the version is newer than the published one
#          (the same release again changes nothing; another release with the same version is refused), and the
#          file's size and sha256; then puts the exe, and after it release.json, in place (each under a temporary
#          name, then renamed over), so a launcher never sees a release.json whose file is not there yet. The old
#          release.json is kept in data/client/history/, older exes are deleted. Every change is appended to
#          data/client_update_log.jsonl.
#   Instead of the POST, release.json and the exe may be copied into data/client/ by hand (exe first): they are
#   checked the same way before anything is served.
#
# release.json - exactly what the launcher (ReleaseManifest.cs) and ReleaseTool.exe make and accept, nothing else, so
# nothing is ever served (or made required through minClientVersion) that no launcher would install:
#   {"format": "wd-release-1", "product": "hd-private-test", "version", "file", "size", "sha256", "minVersion",
#    "issuedAt", "keyId"?, "signature"}
# "version" and "minVersion" are four numbers 0-65535 without leading zeros (minVersion <= version); "file" is
# [A-Za-z0-9][A-Za-z0-9._-]{0,95}.exe without ".."; "size" 1 byte to 256 MB; "sha256" 64 lower-case hex;
# "issuedAt" looks like 2026-09-19T12:00:00Z; the optional "keyId" (16 lower-case hex: the first 8 bytes of SHA-256
# over the RSA modulus, big-endian without leading zeros) only says which key signed - it is not signed, and nothing
# may trust it, nor any other key in the file. "signature" is plain base64 (no spaces inside) of RSASSA-PKCS1-v1_5 /
# SHA-256 (.NET RSA.SignData(bytes, SHA256, Pkcs1)) over these lines, each ending in "\n" (also the last), values
# exactly as in release.json:
#     wd-release-1 / product=hd-private-test / version=<version> / file=<file> / size=<size> / sha256=<sha256> /
#     minVersion=<minVersion> / issuedAt=<issuedAt>
CLIENT_DIR = os.path.join(DATA, "client")
CLIENT_RELEASE_FILE = os.path.join(CLIENT_DIR, "release.json")
CLIENT_HISTORY_DIR = os.path.join(CLIENT_DIR, "history")
CLIENT_VERSIONS_FILE = os.path.join(DATA, "client_versions.json")
CLIENT_LOG_FILE = os.path.join(DATA, "client_update_log.jsonl")
CLIENT_CONFIG_FILE = os.path.join(ROOT, "server_config.json")
CLIENT_PRODUCT = "hd-private-test"      # ReleaseManifest.ExpectedProduct: the only product a launcher installs
CLIENT_WD_FORMAT = "wd-release-1"       # ReleaseManifest.Format: the only format a launcher reads
CLIENT_VERSION = re.compile(r"[0-9]{1,5}(?:\.[0-9]{1,5}){0,3}")     # hello / minClientVersion: missing parts are .0
CLIENT_RELEASE_VERSION = re.compile(r"(?:0|[1-9][0-9]{0,4})(?:\.(?:0|[1-9][0-9]{0,4})){3}")
CLIENT_FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}\.exe")
CLIENT_ISSUED = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
CLIENT_SHA256 = re.compile(r"[0-9a-f]{64}")
CLIENT_KEY_ID = re.compile(r"[0-9a-f]{16}")
CLIENT_SIGNATURE = re.compile(r"[A-Za-z0-9+/]+={0,2}")   # ReleaseManifest's Base64Shape: no spaces inside
CLIENT_KIND = re.compile(r"[a-z][a-z0-9-]{0,23}")
CLIENT_MAX_RELEASE_BYTES = 64 << 10
CLIENT_MAX_FILE_BYTES = 256 << 20
CLIENT_EXE_TYPE = "application/vnd.microsoft.portable-executable"
CLIENT_EXEMPT_PREFIXES = ("/windurango/client/", "/windurango/identity/")
CLIENT_EXEMPT_PATHS = frozenset({"/windurango", "/windurango/", "/windurango/players"})
CLIENT_BLOCK_LOG_SECONDS = 600
CLIENT_SAVE_SECONDS = 60           # a hello with the same version rewrites data/client_versions.json at most this often
CLIENT_OPEN_DOWNLOADS_PER_HOUR = 30   # downloads per address per hour without a verified player key
CLIENT_RSA_SHA256 = bytes.fromhex("3031300d060960864801650304020105000420")   # DigestInfo prefix for SHA-256
CLIENT_RSA_OID = bytes.fromhex("2a864886f70d010101")                          # rsaEncryption
CLIENT_EC_OID = bytes.fromhex("2a8648ce3d0201")                               # id-ecPublicKey (only to name it)
CLIENT_LOCK = threading.Lock()           # CLIENT_VERSIONS and data/client_versions.json
CLIENT_PUBLISH_LOCK = threading.Lock()   # one publish or minimum change at a time
CLIENT_CACHE_LOCK = threading.Lock()
CLIENT_CONFIG_CACHE = {"key": None, "value": None}
CLIENT_RELEASE_CACHE = {"key": None, "value": None}
CLIENT_FILE_HASHES = {}                  # path -> ((mtime_ns, size), sha256)
CLIENT_BLOCK_LOGGED = {}                 # xuidBase -> when its last "blocked" line was printed
CLIENT_SAVED_AT = {}                     # xuidBase -> when its hello was last written to disk
CLIENT_OPEN_DOWNLOADS = {}               # address -> times of its recent downloads without a player key
CLIENT_VERSIONS = {}                     # str(xuidBase) -> {"version", "seen", "since", "first", "client"}


def client_version(text, release=False):
    """(a, b, c, d) from "a.b.c.d" (each 0-65535) - for hello and minClientVersion fewer parts count as .0 and
    leading zeros are allowed - or None."""
    if not isinstance(text, str):
        return None
    if not release:
        text = text.strip()
    if not (CLIENT_RELEASE_VERSION if release else CLIENT_VERSION).fullmatch(text):
        return None
    parts = [int(part) for part in text.split(".")]
    if any(part > 65535 for part in parts):
        return None
    return tuple(parts + [0] * (4 - len(parts)))


def client_version_text(version):
    return ".".join(str(part) for part in version) if version else None


def client_b64(text, url=False):
    """Bytes of standard (or base64url) base64 text, whitespace ignored. Raises ValueError."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("a base64 value is missing")
    compact = "".join(text.split())
    if url:
        compact = compact.replace("-", "+").replace("_", "/")
        compact += "=" * (-len(compact) % 4)
    return base64.b64decode(compact, validate=True)


def client_rsa_verify(key, message, signature):
    """True when signature is RSASSA-PKCS1-v1_5 SHA-256 of message by key's owner (the encoding is rebuilt and
    compared whole, never parsed)."""
    n, e = key["n"], key["e"]
    size = (n.bit_length() + 7) // 8
    if len(signature) != size:
        return False
    number = int.from_bytes(signature, "big")
    if number >= n:
        return False
    digest = CLIENT_RSA_SHA256 + hashlib.sha256(message).digest()
    expected = b"\x00\x01" + b"\xff" * (size - len(digest) - 3) + b"\x00" + digest
    return hmac.compare_digest(pow(number, e, n).to_bytes(size, "big"), expected)


def client_der(data, pos):
    """(tag, content, position after it) of the DER element at pos. Raises ValueError."""
    if pos + 2 > len(data):
        raise ValueError("the key is cut short")
    tag, length = data[pos], data[pos + 1]
    pos += 2
    if length & 0x80:
        count = length & 0x7F
        if not 1 <= count <= 4 or pos + count > len(data):
            raise ValueError("the key has a bad length")
        length = int.from_bytes(data[pos:pos + count], "big")
        pos += count
    if pos + length > len(data):
        raise ValueError("the key is cut short")
    return tag, data[pos:pos + length], pos + length


CLIENT_PRIVATE_KEY = "that is a PRIVATE key - it must never be on the server: put only the public key here"
CLIENT_NOT_RSA = ("only an RSA public key works here (what ReleaseTool.exe makes): the launcher checks RSA signatures "
                  "and nothing else, so a release signed with this key could never be installed")


def client_rsa_key(n, e):
    """An RSA public key as the launcher's ReleaseSignature.ParsePublicKey takes it: 2048-8192 bits, an odd modulus,
    an odd exponent from 3 to 2^32 - 1. Raises ValueError."""
    if not 2048 <= n.bit_length() <= 8192 or n % 2 == 0:
        raise ValueError("the RSA key is %d bits; it must be 2048 to 8192" % n.bit_length())
    if e < 3 or e % 2 == 0 or e >= n or e.bit_length() > 32:
        raise ValueError("the RSA key's public exponent is not usable (the launcher takes an odd one from 3 to 2^32-1)")
    return {"type": "rsa", "n": n, "e": e, "bits": n.bit_length(),
            "id": hashlib.sha256(n.to_bytes((n.bit_length() + 7) // 8, "big")).hexdigest()[:16]}


def client_der_key(der):
    """An RSA key from DER SubjectPublicKeyInfo or PKCS#1 RSAPublicKey. Raises ValueError."""
    tag, body, end = client_der(der, 0)
    if tag != 0x30 or end != len(der):
        raise ValueError("it is not a public key (<RSAKeyValue>, PEM or base64 DER)")
    first_tag, first, pos = client_der(body, 0)
    if first_tag == 0x30:   # SubjectPublicKeyInfo: AlgorithmIdentifier, then the key in a BIT STRING
        oid_tag, oid, after = client_der(first, 0)
        bits_tag, bits, pos = client_der(body, pos)
        if oid_tag != 0x06 or bits_tag != 0x03 or pos != len(body) or not bits or bits[0] != 0:
            raise ValueError("it is not a DER public key")
        if oid == CLIENT_EC_OID:
            raise ValueError("that is an EC (P-256?) key - " + CLIENT_NOT_RSA)
        if oid != CLIENT_RSA_OID:
            raise ValueError("it is not an RSA key - " + CLIENT_NOT_RSA)
        tag, body, end = client_der(bits, 1)
        if tag != 0x30 or end != len(bits):
            raise ValueError("it is not a DER public key")
        first_tag, first, pos = client_der(body, 0)
    if first_tag != 0x02:
        raise ValueError("it is not a public key")
    e_tag, e_bytes, pos = client_der(body, pos)
    if e_tag != 0x02 or pos != len(body):   # PKCS#1 / SEC1 / PKCS#8 private keys have more after these
        raise ValueError("it is not a public key (a private key?)")
    return client_rsa_key(int.from_bytes(first, "big"), int.from_bytes(e_bytes, "big"))


def client_parse_key(raw):
    """One public key from server_config.json "clientUpdateKey" (see above). Raises ValueError."""
    if isinstance(raw, dict):   # JWK
        if any(field in raw for field in ("d", "p", "q", "dp", "dq", "qi")):
            raise ValueError(CLIENT_PRIVATE_KEY)
        if raw.get("kty") == "RSA":
            return client_rsa_key(int.from_bytes(client_b64(raw.get("n"), url=True), "big"),
                                  int.from_bytes(client_b64(raw.get("e"), url=True), "big"))
        raise ValueError('a JWK here must be "kty": "RSA" - ' + CLIENT_NOT_RSA)
    if not isinstance(raw, str):
        raise ValueError("it is not a string")
    text = raw.strip()
    if "<RSAKeyValue" in text:   # .NET RSA.ToXmlString(false)
        if re.search(r"<(D|P|Q|DP|DQ|InverseQ)>", text):
            raise ValueError(CLIENT_PRIVATE_KEY)
        modulus = re.search(r"<Modulus>([^<]*)</Modulus>", text)
        exponent = re.search(r"<Exponent>([^<]*)</Exponent>", text)
        if not modulus or not exponent:
            raise ValueError("the <RSAKeyValue> has no <Modulus> or no <Exponent>")
        return client_rsa_key(int.from_bytes(client_b64(modulus.group(1)), "big"),
                              int.from_bytes(client_b64(exponent.group(1)), "big"))
    if "PRIVATE KEY" in text.upper():
        raise ValueError(CLIENT_PRIVATE_KEY)
    body = re.sub(r"-----(BEGIN|END) [A-Z0-9 ]+-----", "", text) if text.startswith("-----BEGIN") else text
    data = client_b64(body)
    if data[:3] in (b"ECS", b"ECK"):   # a CNG EC blob: ECS1/ECK1 (P-256) ... public, ECS2/ECK2 ... private
        if data[3:4] in (b"2", b"4", b"6"):
            raise ValueError(CLIENT_PRIVATE_KEY)
        raise ValueError("that is a CNG EC (P-256?) key - " + CLIENT_NOT_RSA)
    if len(data) == 64 or (len(data) == 65 and data[0] == 4):   # P-256 X || Y, or SEC1 04 || X || Y
        raise ValueError("that looks like a P-256 key - " + CLIENT_NOT_RSA)
    return client_der_key(data)


def client_parse_keys(raw):
    """([key], [problem]) from "clientUpdateKey": one key or a list of them; a bad entry is left out and named."""
    keys, problems = [], []
    listed = raw if isinstance(raw, list) else [raw]
    for index, item in enumerate(listed):
        try:
            key = client_parse_key(item)
        except ValueError as error:
            problems.append(("key %d: %s" % (index + 1, error)) if len(listed) > 1 else str(error))
            continue
        if all(known["id"] != key["id"] for known in keys):
            keys.append(key)
    return keys, problems


def client_config():
    """From server_config.json, read again whenever it changes: min (version tuple or None), minText, minProblem,
    keys ([key]), keyIds, keyProblem, problem."""
    try:
        info = os.stat(CLIENT_CONFIG_FILE)
        key = (info.st_mtime_ns, info.st_size)
    except OSError:
        key = None
    with CLIENT_CACHE_LOCK:
        if CLIENT_CONFIG_CACHE["value"] is not None and CLIENT_CONFIG_CACHE["key"] == key:
            return CLIENT_CONFIG_CACHE["value"]
    value = {"min": None, "minText": None, "minProblem": None, "keys": [], "keyIds": (), "keyProblem": None,
             "problem": None}
    doc = {}
    if key is not None:
        try:
            with open(CLIENT_CONFIG_FILE, encoding="utf-8-sig") as handle:
                doc = json.load(handle)
            if not isinstance(doc, dict):
                raise ValueError("it is not a JSON object")
        except (OSError, ValueError) as error:
            doc = {}
            value["problem"] = "server_config.json cannot be read (%s): nobody is blocked" % error
    wanted = doc.get("minClientVersion")
    if wanted not in (None, ""):
        parsed = client_version(wanted)
        if parsed is None:
            value["minProblem"] = ('server_config.json "minClientVersion" is %r, not a version like 2026.9.19.1200 - '
                                   'nobody is blocked until it is fixed' % (wanted,))
        else:
            value["min"], value["minText"] = parsed, client_version_text(parsed)
    raw_key = doc.get("clientUpdateKey")
    if raw_key not in (None, "", []):
        keys, problems = client_parse_keys(raw_key)
        value["keys"], value["keyIds"] = keys, tuple(k["id"] for k in keys)
        if problems:
            value["keyProblem"] = 'server_config.json "clientUpdateKey" %s: %s' % (
                "cannot be used" if not keys else "has entries that cannot be used", "; ".join(problems))
    with CLIENT_CACHE_LOCK:
        CLIENT_CONFIG_CACHE.update(key=key, value=value)
    for problem in (value["problem"], value["minProblem"], value["keyProblem"]):
        if problem:
            print("[UPDATE] %s" % problem)
    return value


def client_signed_text(release):
    """The bytes a release's signature covers (see above) - ReleaseManifest.SignedBytes() on the launcher."""
    return ("wd-release-1\nproduct=%s\nversion=%s\nfile=%s\nsize=%d\nsha256=%s\nminVersion=%s\nissuedAt=%s\n" % (
        release["product"], release["version"], release["file"], release["size"], release["sha256"],
        release["minVersion"], release["issuedAt"])).encode("ascii")


def client_check_release(raw, keys):
    """(release, None) when raw is a well-formed release.json signed with one of keys - one every launcher would
    install - else (None, why). release: format, product, version, minVersion, file, size, sha256, issuedAt, keyId
    (of the key that signed it)."""
    try:
        doc = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as error:
        return None, "is not JSON (%s)" % error
    if not isinstance(doc, dict):
        return None, "is not a JSON object"
    fmt = doc.get("format")
    if not isinstance(fmt, str) or fmt != CLIENT_WD_FORMAT:
        return None, 'has "format" %r - the launcher only reads "%s" (what ReleaseTool.exe makes)' % (
            fmt, CLIENT_WD_FORMAT)
    time_field = "issuedAt"
    if doc.get("product") != CLIENT_PRODUCT:
        return None, 'is for product %r - the launcher only installs "%s"' % (doc.get("product"), CLIENT_PRODUCT)
    for field in ("version", "minVersion"):
        if client_version(doc.get(field), release=True) is None:
            return None, 'has "%s" %r, not a version like 2026.9.19.1200' % (field, doc.get(field))
    name = doc.get("file")
    if not isinstance(name, str) or not CLIENT_FILE_NAME.fullmatch(name) or ".." in name:
        return None, 'has "file" %r, not a plain name ending in .exe' % (name,)
    size = doc.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= CLIENT_MAX_FILE_BYTES:
        return None, 'has "size" %r, not a number of bytes (at most %d MB)' % (size, CLIENT_MAX_FILE_BYTES >> 20)
    if not isinstance(doc.get("sha256"), str) or not CLIENT_SHA256.fullmatch(doc["sha256"]):
        return None, 'has no "sha256" of 64 lower-case hex digits'
    if not isinstance(doc.get(time_field), str) or not CLIENT_ISSUED.fullmatch(doc[time_field]):
        return None, 'has "%s" %r, not a time like 2026-09-19T12:00:00Z' % (time_field, doc.get(time_field))
    if client_version(doc["minVersion"], release=True) > client_version(doc["version"], release=True):
        return None, "has a minVersion newer than its version"
    claimed = doc.get("keyId")
    if claimed is not None and (not isinstance(claimed, str) or not CLIENT_KEY_ID.fullmatch(claimed)):
        return None, 'has "keyId" %r, not 16 lower-case hex digits' % (claimed,)
    signed = doc.get("signature")
    try:
        if not isinstance(signed, str) or not CLIENT_SIGNATURE.fullmatch(signed.strip()):
            raise ValueError("not plain base64")
        signature = client_b64(signed)
    except ValueError:
        return None, "is not signed (no plain base64 \"signature\")"
    if not keys:
        return None, "cannot be checked: there is no usable clientUpdateKey in server_config.json"
    usable = [k for k in keys if k["type"] == "rsa" and claimed in (None, k["id"])]
    if not usable:
        return None, "was signed with key %s, which is not in this server's clientUpdateKey" % claimed
    release = {"format": fmt, "product": doc["product"], "version": doc["version"], "minVersion": doc["minVersion"],
               "file": name, "size": size, "sha256": doc["sha256"], "issuedAt": doc[time_field], "keyId": None}
    message = client_signed_text(release)
    for key in usable:
        if client_rsa_verify(key, message, signature):
            release["keyId"] = key["id"]
            return release, None
    return None, ("does not verify with this server's clientUpdateKey (signed with another key, or changed after it "
                  "was signed)")


def client_file_sha256(path):
    """sha256 of a file, kept per (mtime, size); None when it is not there."""
    try:
        info = os.stat(path)
    except OSError:
        return None
    key = (info.st_mtime_ns, info.st_size)
    with CLIENT_CACHE_LOCK:
        cached = CLIENT_FILE_HASHES.get(path)
    if cached and cached[0] == key:
        return cached[1]
    try:
        with open(path, "rb") as handle:
            digest = hashlib.sha256()
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
            after = os.fstat(handle.fileno())
    except OSError:
        return None
    if (after.st_mtime_ns, after.st_size) != key:
        return None   # being written right now
    with CLIENT_CACHE_LOCK:
        CLIENT_FILE_HASHES[path] = (key, digest.hexdigest())
    return digest.hexdigest()


def client_release():
    """The published release: {"state": "none"} (no data/client/release.json), or {"state": "ok" | "bad", "release":
    the signed release or None, "raw": release.json's bytes, "path": the exe, "problem": why it is not served}.
    Checked again whenever release.json, the exe or clientUpdateKey changes."""
    config = client_config()
    try:
        info = os.stat(CLIENT_RELEASE_FILE)
    except FileNotFoundError:
        return {"state": "none", "release": None, "raw": None, "path": None, "problem": None}
    except OSError as error:
        return {"state": "bad", "release": None, "raw": None, "path": None,
                "problem": "data/client/release.json cannot be read (%s)" % error}
    key = ((info.st_mtime_ns, info.st_size), config["keyIds"])
    with CLIENT_CACHE_LOCK:
        cached = CLIENT_RELEASE_CACHE["value"] if CLIENT_RELEASE_CACHE["key"] == key else None
    if cached is None:
        try:
            with open(CLIENT_RELEASE_FILE, "rb") as handle:
                raw = handle.read(CLIENT_MAX_RELEASE_BYTES + 1)
        except OSError as error:
            return {"state": "bad", "release": None, "raw": None, "path": None,
                    "problem": "data/client/release.json cannot be read (%s)" % error}
        if len(raw) > CLIENT_MAX_RELEASE_BYTES:
            release, problem = None, "is over %d KB" % (CLIENT_MAX_RELEASE_BYTES >> 10)
        else:
            release, problem = client_check_release(raw, config["keys"])
        cached = {"raw": raw, "release": release, "problem": problem and "data/client/release.json " + problem}
        with CLIENT_CACHE_LOCK:
            CLIENT_RELEASE_CACHE.update(key=key, value=cached)
        if problem:
            print("[UPDATE] no client update is served: %s" % cached["problem"])
    found = {"state": "bad", "release": cached["release"], "raw": cached["raw"], "path": None,
             "problem": cached["problem"]}
    if cached["release"] is None:
        return found
    release = cached["release"]
    path = os.path.join(CLIENT_DIR, release["file"])
    digest = client_file_sha256(path)
    if digest is None:
        found["problem"] = "data/client/%s, the file release.json names, is not there" % release["file"]
    elif digest != release["sha256"] or os.path.getsize(path) != release["size"]:
        found["problem"] = "data/client/%s is not the file release.json describes (size or sha256)" % release["file"]
    else:
        found.update(state="ok", path=path, problem=None)
    return found


def client_release_info(found):
    """What the players page and the admin routes show about a release."""
    release = found["release"] or {}
    return {"state": found["state"], "problem": found["problem"], "version": release.get("version"),
            "product": release.get("product"), "format": release.get("format"), "file": release.get("file"),
            "size": release.get("size"), "sha256": release.get("sha256"), "minVersion": release.get("minVersion"),
            "issuedAt": release.get("issuedAt"), "keyId": release.get("keyId")}


def client_latest_version(found=None):
    found = found or client_release()
    return found["release"]["version"] if found["state"] == "ok" else None


def client_is_owner(base):
    """Whether this xuidBase is one of the server's owners (server_config.json "owners", any of the four XUIDs)."""
    try:
        low = int(base)
    except (TypeError, ValueError):
        return False
    return any(low <= int(owner) < low + XUIDS_PER_PLAYER for owner in event_owners())


def client_claim_refused(base):
    """identity_claim asks this before it registers a block the server does not have yet: True (and a log line) when
    the block holds one of the owners' XUIDs. Owner rights come with the block, so it is never handed out - on a
    server whose player list lacks the owner, the first caller would otherwise become an owner. Its holder checks in
    with its secret; see client_owner_warnings for putting a missing owner back."""
    if not client_is_owner(base):
        return False
    print("[UPDATE] refused to register xuidBase %s for %s: that block holds an owner's XUID (server_config.json "
          "\"owners\") and this server has no player there - see the startup warning" % (
              base, request.remote_addr or "?"))
    return True


def client_owner_warnings():
    """Startup lines about owners no registered player holds (their block is reserved: nobody can register it)."""
    lines = []
    for owner in sorted(event_owners(), key=int):
        if base_of(owner) is None:
            lines.append('[UPDATE] WARNING: owner XUID %s (server_config.json "owners") has no player on this server. '
                         'Its block is reserved - /windurango/identity/claim hands it to nobody, the owner included. '
                         'Put the owner\'s entry back into data/_players.json ("xuidBase" and "secret" from the '
                         'owner\'s winDurango.json), or take the XUID out of "owners", let the owner\'s game sign in '
                         'once, and put it back.' % owner)
    return lines


def client_needs_update(base, minimum):
    """Whether minimum (a version tuple, or None: no minimum) keeps this player out of the game routes."""
    if minimum is None or client_is_owner(base):
        return False
    have = client_version((CLIENT_VERSIONS.get(str(base)) or {}).get("version"))
    return have is None or have < minimum


def client_versions_by_xuid():
    """/windurango/players' "clientVersions": {signed-in xuid: {"clientVersion", "needsUpdate"}} for every player -
    the Setup version their launcher last reported (None: never), and whether minClientVersion keeps them out until
    they update. A map of its own, so the player summaries keep exactly their six keys (the in-game friends list and
    the Discord bridge read those)."""
    minimum = client_config()["min"]
    return {primary_xuid(base): {"clientVersion": (CLIENT_VERSIONS.get(base) or {}).get("version"),
                                 "needsUpdate": client_needs_update(base, minimum)}
            for base in list(PLAYER_REGISTRY)}


def client_load_versions():
    try:
        with open(CLIENT_VERSIONS_FILE, encoding="utf-8") as handle:
            doc = json.load(handle)
        if not isinstance(doc, dict):
            raise ValueError("it is not a JSON object")
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        kept = "%s.unreadable-%s" % (CLIENT_VERSIONS_FILE, datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"))
        try:
            shutil.copyfile(CLIENT_VERSIONS_FILE, kept)
        except OSError:
            kept = None
        print("[UPDATE] data/client_versions.json cannot be read (%s)%s - every player counts as not reported until "
              "their launcher says hello again" % (error, ", kept as " + os.path.basename(kept) if kept else ""))
        return {}
    return {str(base): record for base, record in doc.items()
            if isinstance(record, dict) and client_version(record.get("version")) is not None}


CLIENT_VERSIONS.update(client_load_versions())


def client_save_versions():
    """data/client_versions.json in one step (a temporary file renamed over it). Holds CLIENT_LOCK."""
    try:
        os.makedirs(DATA, exist_ok=True)
        event_write_json(CLIENT_VERSIONS_FILE, CLIENT_VERSIONS)
        return True
    except OSError as error:
        print("[UPDATE] could not save data/client_versions.json: %s" % error)
        return False


def client_log(entry):
    try:
        with open(CLIENT_LOG_FILE, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
    except OSError as error:
        print("[UPDATE] could not write data/client_update_log.jsonl: %s" % error)


def client_require_owner():
    """(the caller's xuidBase, None) for an owner, else (None, the 401 / 403 response)."""
    base, refusal = require_owner()
    if refusal is not None and refusal.status_code == 403 and g.get("caller_verified") is True:
        refusal = json_response({"ok": False, "reason": "only this server's owners can do that"}, 403)
    return base, refusal


def client_name(base):
    return PLAYER_REGISTRY.get(str(base), {}).get("gamertag") or "?"


@app.before_request
def client_update_gate():
    """426 update_required for a verified player whose Setup is older than minClientVersion (see above). Registered
    after identify_caller, so the caller is known."""
    if g.get("caller_verified") is not True:
        return None
    path = request.path
    if path in CLIENT_EXEMPT_PATHS or path.startswith(CLIENT_EXEMPT_PREFIXES):
        return None
    config = client_config()
    base = g.get("caller_base")
    if not client_needs_update(base, config["min"]):
        return None
    have = (CLIENT_VERSIONS.get(base) or {}).get("version")
    now = time.time()
    with CLIENT_CACHE_LOCK:
        due = now - CLIENT_BLOCK_LOGGED.get(base, 0) >= CLIENT_BLOCK_LOG_SECONDS
        if due:
            if len(CLIENT_BLOCK_LOGGED) > 4096:
                for known in [b for b, t in CLIENT_BLOCK_LOGGED.items() if now - t >= CLIENT_BLOCK_LOG_SECONDS]:
                    del CLIENT_BLOCK_LOGGED[known]
            CLIENT_BLOCK_LOGGED[base] = now
    if due:
        print("[UPDATE] blocked %s (%s): %s, minClientVersion is %s (%s %s) - until they update" % (
            client_name(base), base, "Setup " + have if have else "never reported a version (a Setup from before the "
                                                                     "updater)",
            config["minText"], request.method, path))
    response = json_response({
        "error": "update_required", "minVersion": config["minText"], "yourVersion": have,
        "latest": client_latest_version(),
        "reason": "This server needs Happy Dungeons Online %s or newer (%s). Download the new Happy Dungeons zip "
                  "and drag its files into your game folder." % (config["minText"], "you have " + have if have else
                                                           "yours is older")}, 426)
    response.headers["X-WinDurango-Min-Client-Version"] = config["minText"]
    return response


@app.route("/windurango/client/latest", methods=["GET"])
@app.route("/windurango/client/release", methods=["GET"])
def client_latest():
    found = client_release()
    if found["state"] == "none":
        return json_response({"error": "no_release", "reason": "no client release is published on this server"}, 404)
    if found["state"] != "ok":
        response = json_response({"error": "release_unavailable", "reason": found["problem"]}, 503)
        response.headers["Retry-After"] = "300"
        return response
    response = Response(found["raw"], mimetype="application/json")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.set_etag(hashlib.sha256(found["raw"]).hexdigest())
    return response.make_conditional(request)


def client_open_download_refused(address):
    """True when this address already had CLIENT_OPEN_DOWNLOADS_PER_HOUR downloads without a player key this hour;
    otherwise counts this one."""
    now = time.time()
    with CLIENT_CACHE_LOCK:
        if len(CLIENT_OPEN_DOWNLOADS) > 4096:
            for known in [a for a, times in CLIENT_OPEN_DOWNLOADS.items() if not times or now - times[-1] >= 3600]:
                del CLIENT_OPEN_DOWNLOADS[known]
        recent = [t for t in CLIENT_OPEN_DOWNLOADS.get(address, []) if now - t < 3600]
        refused = len(recent) >= CLIENT_OPEN_DOWNLOADS_PER_HOUR
        if not refused:
            recent.append(now)
        CLIENT_OPEN_DOWNLOADS[address] = recent
    return refused


@app.route("/windurango/client/files/<name>", methods=["GET"])
def client_download(name):
    base = g.get("caller_base")
    if g.get("caller_verified") is not True or base not in PLAYER_REGISTRY:
        base = None
    found = client_release() if CLIENT_FILE_NAME.fullmatch(name) else None
    if found is None or found["state"] != "ok" or found["release"]["file"] != name:
        return json_response({"error": "not_published", "reason": "this server publishes no client file of that name"},
                             404)
    address = request.remote_addr or "?"
    if base is None and client_open_download_refused(address):
        print("[UPDATE] refused a download of %s to %s: %d without a player key this hour" % (
            name, address, CLIENT_OPEN_DOWNLOADS_PER_HOUR))
        response = json_response({"error": "too_many_downloads", "reason": "too many downloads from this address - "
                                  "try again later"}, 429)
        response.headers["Retry-After"] = "600"
        return response
    wanted = request.headers.get("Range", "").replace(" ", "")
    too_many = download_refused(base, address, not wanted or wanted.startswith("bytes=0-"))   # (hardening: a player
    # key no longer lifts the limit; a Range resume is the same download and is not counted again)
    if too_many:
        print("[UPDATE] refused a download of %s to %s: %s" % (
            name, "%s (%s)" % (client_name(base), base) if base else address, too_many))
        response = json_response({"error": "too_many_downloads", "reason": too_many}, 429)
        response.headers["Retry-After"] = "600"
        return response
    release = found["release"]
    response = send_file(found["path"], mimetype=CLIENT_EXE_TYPE, as_attachment=True, download_name=name,
                         conditional=True, etag=release["sha256"], max_age=0)
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    if response.status_code == 200 or (response.status_code == 206 and
                                       request.headers.get("Range", "").replace(" ", "").startswith("bytes=0-")):
        print("[UPDATE] %s downloads %s (Setup %s, %d bytes)" % (
            "%s (%s)" % (client_name(base), base) if base else "a PC without a player key (%s)" % address, name,
            release["version"], release["size"]))
    return response


@app.route("/windurango/client/hello", methods=["POST"])
def client_hello():
    base, refusal = require_player()
    if refusal:
        return refusal
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        body = {}
    version = client_version(body.get("version") if body.get("version") not in (None, "") else body.get("clientVersion"))
    if version is None:
        return json_response({"ok": False, "reason": 'send {"version": "<the installed Setup, like 2026.9.18.1920>"}'},
                             400)
    kind = body.get("client") if isinstance(body.get("client"), str) and CLIENT_KIND.fullmatch(body["client"]) else None
    text = client_version_text(version)
    now_text, now = utc_now(), time.time()
    with CLIENT_LOCK:
        previous = CLIENT_VERSIONS.get(base) or {}
        changed = previous.get("version") != text
        CLIENT_VERSIONS[base] = {"version": text, "seen": now_text,
                                 "since": now_text if changed else previous.get("since") or now_text,
                                 "first": previous.get("first") or now_text, "client": kind or previous.get("client")}
        saved = None
        if changed or now - CLIENT_SAVED_AT.get(base, 0) >= CLIENT_SAVE_SECONDS:
            saved = client_save_versions()
            if saved:
                CLIENT_SAVED_AT[base] = now
    if changed:
        print("[UPDATE] %s (%s) runs Setup %s%s" % (client_name(base), base, text,
                                                    " (was %s)" % previous["version"] if previous.get("version") else ""))
    config = client_config()
    latest = client_latest_version()
    return json_response({
        "ok": True, "version": text, "saved": saved is not False, "minClientVersion": config["minText"],
        "updateRequired": client_needs_update(base, config["min"]), "latest": latest, "latestVersion": latest,
        "updateAvailable": latest is not None and client_version(latest) > version})


def client_player_rows(minimum):
    """Every registered player with the Setup version they last reported, online first."""
    rows = []
    for base in sorted(PLAYER_REGISTRY):
        record = CLIENT_VERSIONS.get(base) or {}
        rows.append({"gamertag": client_name(base), "xuid": primary_xuid(base), "online": is_online(base),
                     "owner": client_is_owner(base), "clientVersion": record.get("version"),
                     "clientSeen": record.get("seen"), "clientSince": record.get("since"),
                     "needsUpdate": client_needs_update(base, minimum)})
    rows.sort(key=lambda row: (not row["online"], not row["needsUpdate"], row["gamertag"].lower()))
    return rows


def client_blocked_summary(rows):
    blocked = [row for row in rows if row["needsUpdate"]]
    online = [row for row in blocked if row["online"]]
    return {"online": [{"gamertag": row["gamertag"], "xuid": row["xuid"], "clientVersion": row["clientVersion"]}
                       for row in online],
            "onlineCount": len(online), "total": len(blocked)}


@app.route("/windurango/client/admin/status", methods=["GET"])
def client_admin_status():
    base, refusal = client_require_owner()
    if refusal:
        return refusal
    config = client_config()
    rows = client_player_rows(config["min"])
    return json_response({
        "ok": True, "you": player_summary(base),
        "minClientVersion": config["minText"], "minProblem": config["minProblem"] or config["problem"],
        "key": {"configured": bool(config["keys"]), "problem": config["keyProblem"],
                "keys": [{"id": k["id"], "type": "RSA-%d" % k["bits"]} for k in config["keys"]]},
        "release": client_release_info(client_release()),
        "players": rows, "blocked": client_blocked_summary(rows),
        "reported": sum(1 for row in rows if row["clientVersion"]),
    })


def client_write_min_version(text):
    """Set (text) or remove (None) "minClientVersion" in server_config.json, everything else kept.
    (changed, problem)."""
    try:
        with open(CLIENT_CONFIG_FILE, encoding="utf-8-sig") as handle:
            doc = json.load(handle)
    except FileNotFoundError:
        doc = {}
    except (OSError, ValueError) as error:
        return False, "server_config.json cannot be read (%s) - fix it by hand first" % error
    if not isinstance(doc, dict):
        return False, "server_config.json is not a JSON object - fix it by hand first"
    if doc.get("minClientVersion") == text or (text is None and "minClientVersion" not in doc):
        return False, None
    if text is None:
        doc.pop("minClientVersion", None)
    else:
        doc["minClientVersion"] = text
    data = (json.dumps(doc, indent=2) + "\n").encode("utf-8")
    try:
        mode = os.stat(CLIENT_CONFIG_FILE).st_mode & 0o7777
    except OSError:
        mode = None
    temp = "%s.%s.tmp" % (CLIENT_CONFIG_FILE, secrets.token_hex(4))
    try:
        try:
            with open(temp, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if mode is not None:
                os.chmod(temp, mode)   # the VPS keeps it 0600
            event_replace(temp, CLIENT_CONFIG_FILE)
        except PermissionError:
            # The service may be allowed to write the file but not its folder: rewrite it in place.
            with open(CLIENT_CONFIG_FILE, "r+b") as handle:
                handle.write(data)
                handle.truncate()
                handle.flush()
                os.fsync(handle.fileno())
    except OSError as error:
        return False, "server_config.json could not be written (%s)" % error
    finally:
        if os.path.exists(temp):
            try:
                os.remove(temp)
            except OSError:
                pass
    return True, None


@app.route("/windurango/client/admin/min-version", methods=["POST"])
def client_admin_min_version():
    base, refusal = client_require_owner()
    if refusal:
        return refusal
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict) or "minVersion" not in body:
        return json_response({"ok": False, "reason": 'send {"minVersion": "2026.9.19.1200"} (or null to turn it off), '
                                                     'with "preview": true to see who it would block'}, 400)
    wanted, text = None, None
    if body["minVersion"] not in (None, ""):
        wanted = client_version(body["minVersion"])
        if wanted is None:
            return json_response({"ok": False, "reason": "%r is not a version like 2026.9.19.1200" % (
                body["minVersion"],)}, 400)
        text = client_version_text(wanted)
        found = client_release()
        if body.get("force") is not True:
            if found["state"] != "ok":
                return json_response({"ok": False, "reason": "no client release is served (%s): players could not "
                                      "update to %s - publish it first" % (found["problem"] or "none published", text)},
                                     409)
            if wanted > client_version(found["release"]["version"], release=True):
                return json_response({"ok": False, "reason": "the published release is only %s: nobody could update "
                                      "to %s" % (found["release"]["version"], text)}, 409)
    summary = client_blocked_summary(client_player_rows(wanted))
    config = client_config()
    if body.get("preview") is True:
        return json_response({"ok": True, "preview": True, "minVersion": text, "current": config["minText"],
                              "wouldBlock": summary})
    with CLIENT_PUBLISH_LOCK:
        changed, problem = client_write_min_version(text)
    if problem:
        return json_response({"ok": False, "reason": problem}, 500)
    if changed:
        who = {"xuidBase": base, "gamertag": client_name(base)}
        client_log({"time": utc_now(), "action": "minClientVersion", "from": config["minText"], "to": text,
                    "blockedOnline": summary["onlineCount"], "blockedTotal": summary["total"], "by": who})
        print("[UPDATE] %s (%s) %s: %d online and %d in all are below it" % (
            who["gamertag"], base, "set minClientVersion to %s" % text if text else "turned minClientVersion off",
            summary["onlineCount"], summary["total"]))
    return json_response({"ok": True, "changed": changed, "minVersion": text, "blocked": summary})


@app.route("/windurango/client/publish", methods=["POST"])
def client_publish():
    base, refusal = client_require_owner()
    if refusal:
        return refusal
    if (request.content_length or 0) > CLIENT_MAX_FILE_BYTES + CLIENT_MAX_RELEASE_BYTES + (1 << 20):
        return json_response({"ok": False, "reason": "that is more than a Setup can be (%d MB)" % (
            CLIENT_MAX_FILE_BYTES >> 20)}, 413)
    config = client_config()
    if not config["keys"]:
        return json_response({"ok": False, "reason": config["keyProblem"] or 'there is no "clientUpdateKey" in '
                              'server_config.json: put the public key there first'}, 409)
    part = request.files.get("release")
    raw = part.read(CLIENT_MAX_RELEASE_BYTES + 1) if part else (request.form.get("release") or "").encode("utf-8")
    upload = request.files.get("file")
    if not raw or upload is None:
        return json_response({"ok": False, "reason": 'send multipart/form-data with the signed release.json as '
                                                     '"release" and the Setup exe as "file"'}, 400)
    if len(raw) > CLIENT_MAX_RELEASE_BYTES:
        return json_response({"ok": False, "reason": "release.json is over %d KB" % (CLIENT_MAX_RELEASE_BYTES >> 10)},
                             413)
    release, problem = client_check_release(raw, config["keys"])
    if problem:
        return json_response({"ok": False, "reason": "release.json " + problem}, 422)
    version = client_version(release["version"], release=True)
    with CLIENT_PUBLISH_LOCK:
        current = client_release()
        before = current["release"]
        if before is not None:
            have = client_version(before["version"], release=True)
            if version < have:
                return json_response({"ok": False, "reason": "%s is older than the published %s - launchers never "
                                      "install an older Setup: give the new one a newer version" % (
                                          release["version"], before["version"])}, 409)
            if version == have and current["raw"] != raw:
                return json_response({"ok": False, "reason": "%s is already published with other contents - give the "
                                      "new Setup a new version" % release["version"]}, 409)
            if version == have and current["state"] == "ok":
                return json_response({"ok": True, "changed": False, "version": release["version"],
                                      "note": "that release is already published"})
        target = os.path.join(CLIENT_DIR, release["file"])
        temp = "%s.%s.upload" % (target, secrets.token_hex(4))
        try:
            os.makedirs(CLIENT_DIR, exist_ok=True)
            digest, size = hashlib.sha256(), 0
            with open(temp, "wb") as handle:
                for chunk in iter(lambda: upload.stream.read(1 << 20), b""):
                    size += len(chunk)
                    if size > release["size"]:
                        break
                    digest.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if size != release["size"] or digest.hexdigest() != release["sha256"]:
                return json_response({"ok": False, "reason": "the uploaded file is not the one release.json describes "
                                      "(%s bytes, sha256 %s...)" % (release["size"], release["sha256"][:16])}, 422)
            event_replace(temp, target)
            if current["raw"] is not None:
                os.makedirs(CLIENT_HISTORY_DIR, exist_ok=True)
                event_write_file(os.path.join(CLIENT_HISTORY_DIR, "release-%s-%s.json" % (
                    (before or {}).get("version") or "unverified",
                    datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"))), current["raw"])
            event_write_file(CLIENT_RELEASE_FILE, raw)
        except OSError as error:
            print("[UPDATE] publishing %s failed: %s" % (release["version"], error))
            return json_response({"ok": False, "reason": "could not write data/client (%s)" % error}, 500)
        finally:
            if os.path.exists(temp):
                try:
                    os.remove(temp)
                except OSError:
                    pass
        for name in os.listdir(CLIENT_DIR):   # older Setups: nothing serves them any more
            if name.lower().endswith(".exe") and name != release["file"]:
                try:
                    os.remove(os.path.join(CLIENT_DIR, name))
                except OSError as error:
                    print("[UPDATE] could not remove the old data/client/%s: %s" % (name, error))
    who = {"xuidBase": base, "gamertag": client_name(base)}
    client_log({"time": utc_now(), "action": "publish", "version": release["version"], "file": release["file"],
                "size": release["size"], "sha256": release["sha256"], "minVersion": release["minVersion"],
                "product": release["product"], "keyId": release["keyId"],
                "previous": (before or {}).get("version"), "by": who})
    print("[UPDATE] %s (%s) published Setup %s (%s, %d bytes, sha256 %s..., key %s)%s" % (
        who["gamertag"], base, release["version"], release["file"], release["size"], release["sha256"][:16],
        release["keyId"], " - was %s" % before["version"] if before else ""))
    return json_response({"ok": True, "changed": True, "version": release["version"], "file": release["file"],
                          "size": release["size"], "sha256": release["sha256"], "product": release["product"],
                          "keyId": release["keyId"], "previous": (before or {}).get("version")})


def client_update_summary():
    """The startup line about client updates (and, below it, a warning for each owner with no player here)."""
    config = client_config()
    found = client_release()
    if found["state"] == "none":
        release = "no release published"
    elif found["state"] == "ok":
        release = "serving Setup %s (%s, %d bytes, key %s)" % (
            found["release"]["version"], found["release"]["file"], found["release"]["size"], found["release"]["keyId"])
    else:
        release = "nothing served (%s)" % found["problem"]
    rows = client_player_rows(config["min"])
    below = client_blocked_summary(rows)["total"]
    minimum = ("minimum %s (%d player%s below it)" % (config["minText"], below, "" if below == 1 else "s")
               if config["min"] else "no minimum version")
    keys = ("signing key%s %s" % ("" if len(config["keys"]) == 1 else "s", ", ".join(
        "%s (RSA-%d)" % (k["id"], k["bits"]) for k in config["keys"]))
            if config["keys"] else "no signing key" + (" (%s)" % config["keyProblem"] if config["keyProblem"] else ""))
    return "\n".join(["Client updates: %s; %s; %s; %d of %d players reported a version" % (
        release, minimum, keys, sum(1 for row in rows if row["clientVersion"]), len(rows))] + client_owner_warnings())


# The players page: who is online and what they are doing, friends, requests and lobbies. It signs in with
# the same WD1 key the game uses, passed once in the link the "Friends and Players" shortcut opens.
WEB_DIR = os.path.join(ROOT, "web")


@app.route("/windurango/", methods=["GET"])
@app.route("/windurango", methods=["GET"])
def players_page():
    return send_file(os.path.join(WEB_DIR, "index.html"))


# Friends: requests, then friendship.
#
# One player asks, the other accepts. Two players asking each other is an acceptance too, which is what lets
# the in-game "add friend" work: the game has no screen for accepting, so each side presses add. Everything
# here acts as the CALLER (the token on the request), never as whatever XUID appears in a URL.
REQUESTS_FILE = os.path.join(DATA, "friend_requests.json")
FRIEND_REQUESTS = {}   # target xuid -> sorted list of xuids that asked to be their friend


def load_requests():
    try:
        with open(REQUESTS_FILE, encoding="utf-8") as handle:
            return {str(k): sorted({str(x) for x in v}) for k, v in json.load(handle).items()}
    except (OSError, ValueError):
        return {}


def save_requests():
    try:
        os.makedirs(DATA, exist_ok=True)
        with open(REQUESTS_FILE, "w", encoding="utf-8") as handle:
            json.dump(FRIEND_REQUESTS, handle, indent=1, sort_keys=True)
    except OSError as error:
        print("COULD NOT SAVE FRIEND REQUESTS:", error)


FRIEND_REQUESTS.update(load_requests())


def primary_xuid(base):
    return str(int(base) + 1)


def require_player():
    """The verified caller's xuidBase, or an error response to return."""
    base = g.get("caller_base")
    if base is None or base not in PLAYER_REGISTRY:
        return None, json_response({"ok": False, "reason": "sign in first"}, 401)
    if g.get("caller_verified") is not True:
        return None, json_response({"ok": False, "reason": "that is not your identity"}, 403)
    return base, None


def other_player(body):
    """(xuidBase, None) for the player a friends request names by {"xuid"} (any of their four XUIDs), or
    (None, the response refusing it). Never by gamertag: two players may have the same one."""
    if not isinstance(body, dict):
        body = {}
    if body.get("xuid") in (None, "") and str(body.get("gamertag") or "").strip():
        return None, json_response({"ok": False, "reason": "add friends by XUID - gamertags are not unique"}, 400)
    base = base_of(body.get("xuid"))
    if base is None:
        return None, json_response({"ok": False, "reason": "no player with that XUID"}, 404)
    return base, None


def summary_for_xuid(xuid):
    base = base_of(xuid)
    return player_summary(base) if base else None


# Discord Rich Presence's party line. WinDurango (and tools/discord_presence.py for the installed game) shows the
# player's group as "In a party (2 of 4)" under what they are doing, from "party" in /windurango/me: the Happy
# Dungeons session the caller has joined - their lobby, which stays their session through the dungeon - with the
# most joined players (then the newest: for a moment matchmaking has a player in its own ticket session and in the
# lobby it moves them to). size counts joined members only: a place matchmaking reserved for someone is not taken
# yet. id is the same for everyone in the session (Discord shows them as one party) and does not give its name away.
# None: the caller is in no session.
PARTY_ID_SALT = "windurango-party|"


def caller_party(base):
    low = int(base)
    best = None
    with STATE_LOCK:   # sessions are written and removed under it: no file goes away while this reads them
        for path, session in iter_sessions(HD_SCID):   # newest first
            members = session.get("members") if isinstance(session.get("members"), dict) else {}
            joined = [member_xuid(members[k]) for k in accepted_member_keys(members)]
            if not any(x and x.isdigit() and low <= int(x) < low + XUIDS_PER_PLAYER for x in joined):
                continue
            if best is None or len(joined) > best[0]:
                best = (len(joined), path, session)
    if best is None:
        return None
    size, path, session = best
    ref = session.get("sessionRef") if isinstance(session.get("sessionRef"), dict) else {}
    template_name = ref.get("templateName") or os.path.basename(os.path.dirname(path))
    name = ref.get("name") or os.path.splitext(os.path.basename(path))[0]
    system = (session.get("constants") or {}).get("system") if isinstance(session.get("constants"), dict) else None
    try:
        most = int((system or {}).get("maxMembersCount") or 0)
    except (TypeError, ValueError):
        most = 0
    most = most or template_max_members(HD_SCID, template_name) or 4
    # MPSD session names are case-insensitive (the game compares them that way).
    key = "%s~%s~%s" % (HD_SCID, str(template_name).lower(), str(name).lower())
    return {"size": size, "max": max(most, size),
            "id": hashlib.sha256((PARTY_ID_SALT + key).encode("utf-8")).hexdigest()[:32]}


@app.route("/windurango/me", methods=["GET"])
def my_page_data():
    """Everything the friends page shows for the signed-in player."""
    base, refusal = require_player()
    if refusal:
        return refusal
    me = primary_xuid(base)
    friends = friends_of(me)
    with STATE_LOCK:
        incoming = list(FRIEND_REQUESTS.get(me, []))
        outgoing = [target for target, askers in FRIEND_REQUESTS.items() if me in askers]
    known = set(friends) | set(incoming) | set(outgoing) | {me}
    g.lobby_index = lobby_index()   # friends join: "party" and the Join fields come from this one walk
    return json_response(join_me_fields(base, {
        "you": dict(player_summary(base), solo=solo_play(base)),   # (solo_patch v1)
        "soloApi": SOLO_API,                                       # (solo_patch v1)
        "party": caller_party(base),
        "friends": [s for s in (summary_for_xuid(x) for x in friends) if s],
        "incoming": [s for s in (summary_for_xuid(x) for x in incoming) if s],
        "outgoing": [s if privacy_may_see(s["xuid"]) else privacy_hidden(s)   # privacy_patch v2: not until
                     for s in (summary_for_xuid(x) for x in outgoing) if s],      # they accept
        "others": [],   # privacy_patch v1: no directory of every player - friends are added by XUID
    }))


# ---------------------------------------------------------------------------
# friends_join_patch v1 - Join a friend (the Guide menu's per-friend Join button, phase 1) and the news item.
#
# The Guide menu (WinDurango's in-game friends list) shows a Join button on every friend who is in an open lobby.
#   POST /windurango/friends/join {"xuid": "<the friend's XUID>"}      ({"xuid": null} cancels)
# arms a join for FRIEND_JOIN_SECONDS: the player's next search for a game to join (Story Quest > Quest Selection,
# or Quick Match for a Quick Match lobby) is put into that friend's lobby as it is then - the join follows the friend
# if they host again - and never into anyone else's (the player asked for their friend). A player who is already
# searching alone is moved at once. Hosts, players in someone else's lobby and players whose own lobby has others in
# it are never moved (place_seeker's rule); the banner tells them what to leave first. Phase 1 never starts a search
# by itself: the game only matchmakes when the player searches. The players page's lobby-list Join
# (/windurango/lobbies/join) shares JOIN_WISHES: a new wish of either kind replaces the old one.
#
# /windurango/me grows, additively (older clients ignore it):
#   "joinApi": 1, "joinMode": "wish" | "activation"   (server_config.json "joinMode"; "activation" is phase 2: the
#        Guide raises the game's own join-a-friend launch - switched on only after its two-copy test)
#   you.lobby    {"role": "host"|"guest"|"searching"|"dungeon", "host", "stageText", "players", "max"} | null
#   you.joining  {"friendXuid", "host", "stageText", "players", "max", "menu", "state": "armed"|"placed"|"failed",
#                 "secondsLeft", "howTo", "reason", "text"} | null         (placed / failed: for FRIEND_JOIN_RESULT_SECONDS)
#   friends[].lobby  {"kind": "host"|"seeker", "host", "stageText", "players", "max", "open", "sameVersion",
#                     "menu": "questSelection"|"quickMatch" [, "activityHandle": joinMode "activation" only]} | null
#   friends[].join   {"can", "reason", "text"}: reason ok | joining (can: the button is Cancel) | offline | noLobby |
#                    dungeon | full | otherData | together | joiningYou - text is the sentence to show
#   "news": {"id", "title", "lines"}   only while data/news.json has an item this player has not marked seen with
#        POST /windurango/news/seen {"id"} (the Guide shows it once as a banner, then sends that)
# players = places taken (reserved ones too), max = the lobby's size; "menu" names where to search.
#
# lobby_index() decides every player's lobby with one walk: the session they have joined with the most joined
# players, then the newest - the rule of the Discord party line ("In a party (2 of 4)"), with the same key, size, max
# and party id. /windurango/me builds the index once per request and checks it against the party it reports: when a
# session changed between the two walks, the index is walked again, so the party and the Join fields in one answer
# always name the same session. (The party function itself is neither changed nor called here: its patch's test,
# ev3\discord\party_test.py, refuses any other code that uses it.)
FRIEND_JOIN_SECONDS = 600          # a Join pressed in the Guide menu stays ready this long
FRIEND_JOIN_RESULT_SECONDS = 60    # how long you.joining still says "placed" or "failed" (and why)
FRIEND_JOIN_RESULTS = {}           # str(xuidBase) -> the last placed / failed friend join (see join_result)
JOIN_API = 1
DUNGEON_ACTIVITIES = frozenset({"Story_Dungeon", "Extra_Dungeon", "Event_Dungeon", "Infinity_Dungeon"})
JOIN_MENU_STEP = {"questSelection": "open Story Quest > Quest Selection and pick any quest (not Create Session)",
                  "quickMatch": "open Story Quest > Quick Match"}
JOIN_MENU_NAME = {"questSelection": "Quest Selection", "quickMatch": "Quick Match"}
JOIN_CONFIG_FILE = os.path.join(ROOT, "server_config.json")
JOIN_LOCK = threading.Lock()       # the two caches below; never held while taking STATE_LOCK
JOIN_CONFIG_CACHE = {"key": None, "mode": "wish"}
NEWS_FILE = os.path.join(DATA, "news.json")
NEWS_SEEN_FILE = os.path.join(DATA, "news_seen.json")
NEWS_CACHE = {"key": None, "item": None}
NEWS_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
NEWS_SEEN_KEEP = 32                # ids remembered per player
NEWS_SEEN = {}                     # str(xuidBase) -> [news ids the player's game marked seen]


def join_mode():
    """server_config.json "joinMode": "wish" (phase 1, the default) or "activation" (phase 2). Read again when the
    file changes; anything else is logged and taken as "wish"."""
    try:
        info = os.stat(JOIN_CONFIG_FILE)
        key = (info.st_mtime_ns, info.st_size)
    except OSError:
        return "wish"
    with JOIN_LOCK:
        if JOIN_CONFIG_CACHE["key"] == key:
            return JOIN_CONFIG_CACHE["mode"]
    mode = "wish"
    try:
        with open(JOIN_CONFIG_FILE, encoding="utf-8-sig") as handle:
            value = json.load(handle).get("joinMode")
        if value in ("wish", "activation"):
            mode = value
        elif value is not None:
            print('[JOIN] server_config.json "joinMode" is %r - only "wish" or "activation"; using "wish"' % (value,))
    except (OSError, ValueError, AttributeError) as error:
        print('[JOIN] server_config.json cannot be read (%s): joinMode "wish"' % error)
    with JOIN_LOCK:
        JOIN_CONFIG_CACHE.update(key=key, mode=mode)
    return mode


def lobby_session_key(path, session):
    """(the party line's lower-case "scid~template~name" key of a stored Happy Dungeons session, template, name)."""
    ref = session.get("sessionRef") if isinstance(session.get("sessionRef"), dict) else {}
    template_name = ref.get("templateName") or os.path.basename(os.path.dirname(path))
    name = ref.get("name") or os.path.splitext(os.path.basename(path))[0]
    return "%s~%s~%s" % (HD_SCID, str(template_name).lower(), str(name).lower()), template_name, name


def lobby_index(now=None):
    """One walk over the stored Happy Dungeons sessions. Returns {"players": {xuidBase: record}, "at": now}: every
    registered player's lobby - the session they have joined (not only reserved a place in) with the most joined
    players, then the newest - which is exactly the session the Discord party line reports. The players of one
    session share one record: {"key", "size" (joined), "max", "id" (the party id), "session", "host" (xuid or None),
    "closed",
    "joined" / "members" (xuidBases: joined / joined or reserved), "mm" (its MM_LOBBIES key while that lobby is
    fresh, else None), "mmAny" (its MM_LOBBIES key, fresh or not)}. Takes STATE_LOCK."""
    now = now or time.time()
    players = {}
    with STATE_LOCK:   # sessions are written and removed under it: no file goes away while this reads them
        fresh, known = {}, {}
        for key, lobby in list(MM_LOBBIES.items()):
            known[key.lower()] = key
            if now - lobby.get("refreshed", 0) <= LOBBY_FRESH_SECONDS:
                fresh[key.lower()] = key
        for path, session in iter_sessions(HD_SCID):   # newest first
            members = session.get("members") if isinstance(session.get("members"), dict) else {}
            joined = [member_xuid(members[k]) for k in accepted_member_keys(members)]
            joined_bases = {base_of(x) for x in joined if x and x.isdigit()} - {None}
            if not joined_bases:
                continue
            key, template_name, _name = lobby_session_key(path, session)
            system = (session.get("constants") or {}).get("system") if isinstance(session.get("constants"),
                                                                                 dict) else None
            try:
                most = int((system or {}).get("maxMembersCount") or 0)
            except (TypeError, ValueError):
                most = 0
            most = most or template_max_members(HD_SCID, template_name) or 4
            record = {
                "key": key, "size": len(joined), "max": max(most, len(joined)),
                "id": hashlib.sha256((PARTY_ID_SALT + key).encode("utf-8")).hexdigest()[:32],
                "session": session, "host": session_host_xuid(session),
                "closed": ((session.get("properties") or {}).get("system") or {}).get("closed") is True,
                "joined": joined_bases,
                "members": {base_of(member_xuid(members[k])) for k in member_keys(members)} - {None},
                "mm": fresh.get(key), "mmAny": known.get(key),
            }
            for base in joined_bases:
                best = players.get(base)
                if best is None or record["size"] > best["size"]:
                    players[base] = record
    return {"players": players, "at": now}


def join_index():
    """The index /windurango/me built for this request (g.lobby_index), else a fresh walk (timers, other routes)."""
    try:
        index = g.get("lobby_index")
    except RuntimeError:   # not in a request
        index = None
    return index if index is not None else lobby_index()


def lobby_party(base, index=None):
    """The Discord party line as lobby_index sees it: {"size", "max", "id"} of the player's lobby, or None - what
    /windurango/me's party says, from the same rule."""
    record = (index or join_index())["players"].get(str(base))
    return None if record is None else {"size": record["size"], "max": record["max"], "id": record["id"]}


def join_menu(hopper):
    """Where a lobby's players searched: Quick Match (V2_GeneralHopper4) or Quest Selection (GeneralHopper4)."""
    return "quickMatch" if str(hopper or "").startswith("V2_") else "questSelection"


def join_activity(base):
    """The player's rich-presence id while online ("Story_Dungeon", "Lobby", ...), else ""."""
    if not is_online(base):
        return ""
    with STATE_LOCK:
        return (PLAYERS.get(primary_xuid(base)) or {}).get("activity", "") or ""


def join_record_view(record):
    """Host, stage and places of a lobby record, whether or not it is a fresh matchmaking lobby."""
    session = record["session"]
    members = session.get("members") if isinstance(session.get("members"), dict) else {}
    lobby = MM_LOBBIES.get(record["mmAny"]) if record["mmAny"] else None
    host = record["host"] or (lobby or {}).get("owner")
    taken = len(member_keys(members))
    return {"host": gamertag_for(host) if host else None, "hostXuid": host,
            "stageText": stage_text(lobby.get("stage")) if lobby else None,
            "players": taken, "max": max(record["max"], taken)}


def join_lobby_view(record, now, my_version):
    """What Join needs about a player's lobby when it is a fresh matchmaking lobby, else None."""
    if not record or not record["mm"]:
        return None
    lobby = MM_LOBBIES.get(record["mm"])
    if lobby is None:
        return None
    found = open_lobby(record["mm"], now)
    session = found[1] if found else record["session"]
    members = session.get("members") if isinstance(session.get("members"), dict) else {}
    version = lobby_version(lobby, session)
    view = join_record_view(record)
    view.update(key=record["mm"], kind=lobby_kind(lobby), open=found is not None, closed=record["closed"],
                free=found[2] if found else lobby_size(lobby["ref"]) - len(member_keys(members)),
                sameVersion=None if my_version is None or version is None else my_version == version,
                menu=join_menu(lobby.get("hopper")), stageText=stage_text(lobby.get("stage")))
    return view


def join_together(me, friend, index):
    """The two players are in one lobby (joined, or one of them holds a reserved place in the other's)."""
    mine, theirs = index["players"].get(me), index["players"].get(friend)
    return bool((theirs is not None and (theirs is mine or me in theirs["members"]))
                or (mine is not None and friend in mine["members"]))


def join_step(menu, stage=None):
    """(lobby_patch v1) JOIN_MENU_STEP[menu] - for a Quest Selection lobby of a mission, while the policy keeps a player
    out of another mission's lobby ("mmStagePolicy" strict / exact), it names the quest to pick instead of "any quest".
    stage: the lobby's Stage ("mission_2_0") or its stageText ("Mission 2-0")."""
    step = JOIN_MENU_STEP[menu]
    text = stage_text(stage) if concrete_stage(stage) else str(stage or "")
    if (menu == "questSelection" and re.fullmatch(r"Mission \d+-\d+", text)
            and mm_stage_policy() in ("strict", "exact")):
        step = step.replace("pick any quest", "pick %s, the quest that lobby plays" % text)
    return step


def join_how_to(menu, role, host, stage=None):
    """What the joiner has to do now, for the lobby's menu and the joiner's own situation (you.lobby)."""
    step = join_step(menu, stage)   # (lobby_patch v1: the mission to pick)
    kind = (role or {}).get("role")
    if kind == "dungeon":
        return "Finish or leave your dungeon, then %s. The join stays ready for %d minutes." % (
            step, FRIEND_JOIN_SECONDS // 60)
    if kind == "host":
        return "Leave your session first (Leave Session), then %s." % step
    if kind == "guest":
        return "Leave %s's lobby first (Leave Session), then %s." % (role.get("host") or "that", step)
    return "%s%s - you will be put into %s's lobby." % (step[0].upper(), step[1:], host)


def join_my_role(base, index):
    """you.lobby: where the caller is - "dungeon", "guest" (in someone else's lobby), "host" (their own lobby, or
    their own seeker lobby with others in it: never moved), "searching" - or None (town, menus)."""
    record = index["players"].get(base)

    def role(name):
        out = {"role": name, "host": None, "stageText": None, "players": 0, "max": 0}
        if record is not None:
            view = join_record_view(record)
            out.update(host=view["host"], stageText=view["stageText"], players=view["players"], max=view["max"])
        return out

    if join_activity(base) in DUNGEON_ACTIVITIES or (record is not None and record["closed"]):
        return role("dungeon")
    if record is not None:
        if record["host"] and base_of(record["host"]) != base:
            return role("guest")
        lobby = MM_LOBBIES.get(record["mm"]) if record["mm"] else None
        if (lobby is not None and lobby_kind(lobby) == "host") or record["size"] > 1:
            return role("host")
    with STATE_LOCK:
        searching = any(t["role"] == "seeker" and base_of(t["owner"]) == base for t in MM_TICKETS.values())
    if searching or (record is not None and record["mm"]):
        return role("searching")
    return None


def join_friend_state(me, friend, index, now, my_version, handles):
    """(friends[].lobby, friends[].join) of one friend, for the caller me. The caller holds STATE_LOCK."""
    record = index["players"].get(friend)
    view = join_lobby_view(record, now, my_version)
    lobby = None
    if view is not None:
        lobby = {k: view[k] for k in ("kind", "host", "stageText", "players", "max", "open", "sameVersion", "menu")}
        handle = handles.get(friend)
        if handle and handle[1] == view["key"].lower():
            lobby["activityHandle"] = handle[0]
    name = gamertag_for(primary_xuid(friend))

    def join(reason, text, can=False):
        return lobby, {"can": can, "reason": reason, "text": text}

    if not is_online(friend):
        return join("offline", "Offline")
    if join_together(me, friend, index):
        return join("together", "In your lobby")
    if solo_play(me):   # (solo_patch v1) these two also refuse POST /windurango/friends/join (it reads join["can"])
        return join("youSolo", "Solo play is on - turn it off on the players page to join friends")
    if solo_play(friend):
        return join("solo", "%s is playing solo right now" % name)
    theirs = JOIN_WISHES.get(friend)
    if theirs and theirs.get("friend") == me and theirs["until"] > now:
        return join("joiningYou", "%s is on the way to your lobby" % name)
    wish = JOIN_WISHES.get(me)
    if wish and wish.get("friend") == friend and wish["until"] > now:
        menu = view["menu"] if view else "questSelection"
        return join("joining", "Joining %s's lobby - %s" % (view["host"] if view else name,   # (lobby_patch v1)
                                                             join_step(menu, view["stageText"] if view else None)),
                    True)
    if view is not None and view["open"]:
        if view["sameVersion"] is False:
            return join("otherData", "Different game data - whoever is behind must update (press Play in Happy Dungeons Online) or restart the game")
        fps_refused = fps_friend_join_refusal(me, friend, record, view)   # (fps_split v3) friends_join: 409 too
        if fps_refused:
            return join("otherFps", fps_refused)
        return join("ok", "In %s's lobby \u00b7 %s \u00b7 %d/%d" % (view["host"], view["stageText"], view["players"],
                                                                   view["max"]), True)
    if view is not None and view["free"] <= 0 and not view["closed"]:
        return join("full", "Their lobby is full (%d/%d)" % (view["players"], view["max"]))
    if join_activity(friend) in DUNGEON_ACTIVITIES or (record is not None and record["closed"]):
        return join("dungeon", "In a dungeon - you can join when they are back in a lobby")
    return join("noLobby", "Not in a lobby - ask them to Create Session")


def join_result(base, state, friend, view, reason, now, text=None):
    """Remembers a friend join that was placed or failed, for you.joining (FRIEND_JOIN_RESULT_SECONDS). The caller
    holds STATE_LOCK."""
    host = (view or {}).get("host") or gamertag_for(primary_xuid(friend))
    FRIEND_JOIN_RESULTS[base] = {
        "state": state, "friend": friend, "host": host, "stageText": (view or {}).get("stageText"),
        "players": (view or {}).get("players"), "max": (view or {}).get("max"),
        "menu": (view or {}).get("menu") or "questSelection", "reason": reason, "at": now,
        "text": text or ("You are in %s's lobby." % host if state == "placed" else "Could not join %s: %s." % (
            host, reason))}


def join_my_joining(base, index, now, role, my_version):
    """you.joining, and its housekeeping: an armed friend join that has come true (the two are in one lobby) becomes
    "placed"; one whose time ran out "failed". The caller holds STATE_LOCK."""
    wish = JOIN_WISHES.get(base)
    if wish and wish.get("friend"):
        friend = wish["friend"]
        record = index["players"].get(friend)
        view = join_lobby_view(record, now, my_version)
        if join_together(base, friend, index):
            JOIN_WISHES.pop(base, None)
            join_result(base, "placed", friend, view or (join_record_view(record) if record is not None else None),
                        None, now, "You and %s are in one lobby now." % gamertag_for(primary_xuid(friend)))
            print("[JOIN] %s is in %s's lobby now" % (gamertag_for(primary_xuid(base)),
                                                     gamertag_for(primary_xuid(friend))))
        elif wish["until"] <= now:
            JOIN_WISHES.pop(base, None)
            join_result(base, "failed", friend, view, "the %d minutes ran out - press Join again"
                        % (FRIEND_JOIN_SECONDS // 60), now)
        else:
            menu = view["menu"] if view else "questSelection"
            host = view["host"] if view else gamertag_for(primary_xuid(friend))
            return {"friendXuid": primary_xuid(friend), "host": host,
                    "stageText": view["stageText"] if view else None, "players": view["players"] if view else None,
                    "max": view["max"] if view else None, "menu": menu, "state": "armed",
                    "secondsLeft": max(0, int(wish["until"] - now + 0.5)),
                    "howTo": join_how_to(menu, role, host, view["stageText"] if view else None),   # (lobby_patch v1)
                    "reason": wish.get("reason"),
                    "text": "Joining %s's lobby%s" % (host, " \u00b7 %s \u00b7 %d/%d" % (
                        view["stageText"], view["players"], view["max"]) if view else "")}
    result = FRIEND_JOIN_RESULTS.get(base)
    if result is None:
        return None
    if now - result["at"] >= FRIEND_JOIN_RESULT_SECONDS:
        FRIEND_JOIN_RESULTS.pop(base, None)
        return None
    return {"friendXuid": primary_xuid(result["friend"]), "host": result["host"], "stageText": result["stageText"],
            "players": result["players"], "max": result["max"], "menu": result["menu"], "state": result["state"],
            "secondsLeft": 0, "howTo": None, "reason": result["reason"], "text": result["text"]}


def join_activity_handles():
    """{xuidBase: (activity handle id, its session's lower-case key)} - phase 2 only ("activityHandle")."""
    found = {}
    try:
        names = os.listdir(HANDLE_DATA)
    except OSError:
        return found
    for filename in names:
        if not filename.endswith(".json"):
            continue
        handle = load_json(os.path.join(HANDLE_DATA, filename))
        if not isinstance(handle, dict) or handle.get("type") != "activity" or not handle.get("id"):
            continue
        owner = base_of(handle.get("ownerXuid"))
        ref = handle.get("sessionRef") if isinstance(handle.get("sessionRef"), dict) else {}
        if owner:
            found[owner] = (str(handle["id"]), "%s~%s~%s" % (str(ref.get("scid", "")).lower(),
                                                             str(ref.get("templateName", "")).lower(),
                                                             str(ref.get("name", "")).lower()))
    return found


def join_me_fields(base, doc):
    """Adds the Join fields and the news item to /windurango/me's answer (see the top of this section)."""
    now = time.time()
    index = join_index()
    if doc.get("party") != lobby_party(base, index):
        # The party line in doc was walked on its own, a moment after the index, by the same rule: a session was
        # written in between. Walk again and take that for both, so this answer's party and Join fields always name
        # the same session.
        index = lobby_index()
        doc["party"] = lobby_party(base, index)
    mode = join_mode()
    # (invite_patch v1) an invite carries the host's activity handle, so these are wanted in "wish" mode too
    invites = invite_on()
    handles = join_activity_handles() if mode == "activation" or invites else {}
    invite_note_client(base)   # (invite_patch v1) ?invite=1: this client can take an invite
    with STATE_LOCK:
        my_version = LAST_TICKET_VERSION.get(base)
        role = join_my_role(base, index)
        doc["joinApi"] = JOIN_API
        doc["joinMode"] = mode
        if isinstance(doc.get("you"), dict):
            doc["you"]["lobby"] = role
            doc["you"]["joining"] = join_my_joining(base, index, now, role, my_version)
        for friend in doc.get("friends") or []:
            other = base_of(friend.get("xuid"))
            if other is None:
                friend["lobby"], friend["join"] = None, {"can": False, "reason": "offline", "text": "Offline"}
                continue
            friend["lobby"], friend["join"] = join_friend_state(base, other, index, now, my_version, handles)
        fps_me_fields(base, doc, index, now)   # (fps_split v3) additive: old clients ignore it
        if invites:
            invite_me_fields(base, doc, index, now, handles)   # (invite_patch v1) additive: old clients ignore it
    news = news_for(base, now)
    if news is not None:
        doc["news"] = news
    return doc


def place_friend_seeker(ticket, wish, now):
    """place_seeker for a Join pressed on a friend: into that friend's lobby as it is NOW (the join follows the friend
    when they host again), never into anyone else's. True: placed. False: not now - the seeker keeps its own lobby
    and wish["reason"] says why (their lobby started / is full / is a lobby of the other menu). None: the join is
    dropped (other game data, as place_seeker does for a lobby wish) and the search goes on without it. The caller
    holds STATE_LOCK."""
    owner = str(base_of(ticket["owner"]))
    friend = wish["friend"]
    record = lobby_index(now)["players"].get(friend)
    if record is not None and record["mm"]:
        wish["lobby"] = record["mm"]
    key = wish.get("lobby")
    found = open_lobby(key, now) if key else None
    name = gamertag_for(primary_xuid(friend))

    def wait(reason):
        if wish.get("reason") != reason:
            print("[JOIN] %s's join on %s: not now (%s)" % (gamertag_for(ticket["owner"]), name, reason))
        wish["reason"] = reason
        return False

    if solo_play(friend):   # (solo_patch v1) they turned it on after the Join was armed: the wish waits, it says why
        return wait("%s is playing solo right now" % name)
    if found is None:
        lobby = MM_LOBBIES.get(key) if key else None
        session = load_session_from_ref(lobby["ref"]) if lobby else None
        if not isinstance(session, dict):
            return wait("%s is not in an open lobby now" % name)
        if ((session.get("properties") or {}).get("system") or {}).get("closed") is True:
            return wait("their lobby started")
        members = session.get("members") if isinstance(session.get("members"), dict) else {}
        if lobby_size(lobby["ref"]) - len(member_keys(members)) <= 0:
            return wait("their lobby is full")
        return wait("%s is not in an open lobby now" % name)
    lobby, session, free = found
    if ref_key(lobby["ref"]).lower() == ref_key(ticket["ref"]).lower():
        return False   # the friend is in the seeker's own lobby: they are together already
    view = join_lobby_view(record, now, ticket.get("version")) if record is not None and record["mm"] == key else None
    if not same_game_data(lobby, session, ticket, now):
        JOIN_WISHES.pop(owner, None)
        join_result(owner, "failed", friend, view, "different game data - whoever is behind must update (press Play in Happy Dungeons Online) or restart the game",
                    now)
        print("[JOIN] %s's join on %s: dropped, other game data" % (gamertag_for(ticket["owner"]), name))
        return None
    if not same_fps(lobby, session, ticket, now):   # (fps_split v3) the Join stays armed: switch, and it goes through
        return wait(fps_friend_seeker_text(lobby, session, ticket, name))
    if not lobby_fits(lobby, ticket):
        menu = join_menu(lobby.get("hopper"))
        return wait("%s's lobby is a %s lobby - %s" % (name, JOIN_MENU_NAME[menu],
                                                       join_step(menu, lobby.get("stage"))))
    if not stage_allowed(lobby.get("stage"), ticket["stage"]):   # (lobby_patch v1) the game keeps its own mission
        return wait("%s's lobby plays %s - search for that mission (or any dungeon) to join"
                    % (name, stage_text(lobby.get("stage"))))
    if free < len(ticket["players"]):
        return wait("their lobby is full")
    JOIN_WISHES.pop(owner, None)
    join_result(owner, "placed", friend, view, None, now)
    # They are together now: a Join of anyone in that lobby that points at the joiner is done too.
    members = session.get("members") if isinstance(session.get("members"), dict) else {}
    for other in {base_of(member_xuid(members[k])) for k in accepted_member_keys(members)} - {None, owner}:
        theirs = JOIN_WISHES.get(other)
        if theirs and theirs.get("friend") == owner:
            JOIN_WISHES.pop(other, None)
            join_result(other, "placed", owner, None, None, now,
                        "%s came into your lobby." % gamertag_for(ticket["owner"]))
    print("[JOIN] %s -> %s's lobby (Join)" % (gamertag_for(ticket["owner"]), name))
    reserve_into_lobby(lobby, session, ticket)
    return True


# ---------------------------------------------------------------------------------------------------------------------
# invite_patch v1 - invite a friend into your lobby.
#
# WHY THIS IS NOT THE JOIN WISH. POST /windurango/friends/join writes JOIN_WISHES[me] and then waits: the wish is
# only ever read by place_seeker, which runs when the joiner's OWN game posts a matchmaking ticket. Nothing here can
# move a game, so the answer even carries instructions for the human ("open Story Quest > Quest Selection"). On
# 2026-09-20 a player pressed Join at 07:10:43 and nothing at all happened until 07:10:52, when they walked back
# into the menu themselves. Those nine seconds are the feature gap.
#
# An invite goes the other way. The host offers; the friend's Guide shows it; the friend accepts; the server keeps
# them a place and hands back the host's activity handle id; and the friend's WinDurango raises the game's own
# protocol activation (ms-xbl-<titleid>://activityHandleJoin/?handle=...) - the path the exe has always had and
# nothing has ever raised. XSAPI then resolves that handle against this server and the game joins by itself.
#
# THE RESERVED PLACE, AND WHY IT IS BOTH THE FEATURE AND THE RISK. apply_member_writes runs join_refusal,
# membership_refusal and the capacity check only when it has never seen this member (own is None). A player who
# already holds a reserved place is not new, so their PUT of members/me walks past all three. That is how matchmaking
# gets a seeker into a lobby, and it is how an invite gets a friend in - but it means invite_accept has to do those
# three checks itself, before it writes the reservation, or an invite could put a fifth player in a four-place lobby
# or a player into a session they may not be in. It does, in invite_landable() and invite_accept(), and it re-does
# them at accept time rather than trusting what was true when the invite was sent.
#
# HELD. An invite may be sent from anywhere, a dungeon included. It arms itself only while the host's lobby is open
# and has room; until then you.invite says state "held" and why. Nothing expires on the way: the same invite arms
# the moment the host is back in a lobby, within INVITE_SECONDS of being sent.
#
# server_config.json (read again when the file changes, no restart):
#   "invites"   false turns the four routes off and drops the /me fields; true or absent, on.
INVITE_VERSION = "invite_patch v1"
INVITE_API = 1
INVITE_SECONDS = 600               # an offer stays open this long, held or not
INVITE_RESERVE_SECONDS = 90        # a place kept for an invitee (matchmaking's own is RESERVATION_SECONDS, 30): the
                                   # invitee's game may have a scene to leave before it joins
INVITE_CLIENT_SECONDS = 120        # a client that asked /windurango/me?invite=1 this recently can take an invite
INVITES = {}                       # str(inviteeBase) -> {"from", "host", "at", "until", "seenKey"}
INVITE_CLIENTS = {}                # str(xuidBase) -> monotonic time its Guide last said it knows about invites
INVITE_LOCK = threading.Lock()     # INVITE_CONFIG_CACHE only; never held while taking STATE_LOCK
INVITE_CONFIG_CACHE = {"key": None, "on": True}


def invite_on():
    """server_config.json "invites": on unless it says false. Read again when the file changes."""
    try:
        info = os.stat(JOIN_CONFIG_FILE)
        key = (info.st_mtime_ns, info.st_size)
    except OSError:
        return True
    with INVITE_LOCK:
        if INVITE_CONFIG_CACHE["key"] == key:
            return INVITE_CONFIG_CACHE["on"]
    on = True
    try:
        with open(JOIN_CONFIG_FILE, encoding="utf-8-sig") as handle:
            value = json.load(handle).get("invites")
        if value in (True, False):
            on = value
        elif value is not None:
            print('[INVITE] server_config.json "invites" is %r - only true or false; taken as true' % (value,))
    except (OSError, ValueError, AttributeError) as error:
        print("[INVITE] server_config.json cannot be read (%s): invites stay on" % error)
    with INVITE_LOCK:
        INVITE_CONFIG_CACHE.update(key=key, on=on)
    return on


def invite_note_client(base):
    """/windurango/me?invite=1 - this player's Guide knows what an invite is. Without it a host is never offered an
    Invite button for them, so a new build never fires an invite into an old build's void."""
    try:
        wanted = request.args.get("invite") in ("1", "true", "yes")
    except RuntimeError:   # not in a request
        return
    if wanted:
        INVITE_CLIENTS[str(base)] = time.monotonic()


def invite_client_ready(base):
    then = INVITE_CLIENTS.get(str(base))
    return then is not None and time.monotonic() - then <= INVITE_CLIENT_SECONDS


def invite_ticket_attrs(session, lobby):
    """The matchmakingResult.ticketAttrs to give an invited member: the ones the session's other members were
    matched with (the game reads its dungeon from Stage), with the lobby's own Stage put back the way
    reserve_into_lobby does. An empty dict when the session has none to copy."""
    members = session.get("members") if isinstance(session.get("members"), dict) else {}
    attrs = {}
    for key in member_keys(members):
        found = (((members[key].get("constants") or {}).get("custom") or {})
                 .get("matchmakingResult") or {}).get("ticketAttrs")
        if isinstance(found, dict) and found:
            attrs = copy.deepcopy(found)
            break
    if concrete_stage(lobby.get("stage")):
        attrs["Stage"] = lobby["stage"]
    return attrs


# ---------------------------------------------------------------------------------------------------------------------
# invite_private v1 - let an invite land in a lobby nobody is matchmaking from.
#
# WHY IT COULD NOT. join_lobby_view starts with "if not record or not record["mm"]: return None", and record["mm"] is
# only ever set for a session a matchmaking ticket was posted from: MM_LOBBIES has exactly two write sites,
# register_seeker_lobby and create_multiplayer_ticket, and both are inside a hopper POST. A host who simply sits in a
# room posts no ticket, so their lobby has no MM_LOBBIES entry - not because it went stale, but by definition - and
# invite_landable turned that into "noLobby". Being invitable therefore meant leaving the room open to matchmaking.
#
# WHAT THIS ADDS. A second view builder, reached only from the invite path. Everything it needs is already in the
# stored session: decorate_session stamps sessionRef on every save, so the three fields a ticket's ref carries are
# there; the members, the closed flag and InviteVersion are in the same document; and lobby_size works off any ref.
# Join is untouched - join_lobby_view still answers None for a non-matchmaking lobby, so friends[].join, you.joining,
# POST /windurango/friends/join and place_friend_seeker behave exactly as they do today.
#
# THE GUARDS ARE KEPT, BY HAND. open_lobby was doing five checks on the way past, and none of them survive the loss of
# the MM_LOBBIES entry, so invite_private_view does them itself: solo play on whoever hosts the session, the closed
# flag, at least one player actually in it (not only reservations), and a free place counted from the template's size
# via lobby_size - never from record["max"], which grows to match a session that is already over its size. The dungeon
# guard and friendship are unaffected: invite_landable reads record["closed"] and join_activity, and invite_refused
# still requires friendship, and none of those ever looked at MM_LOBBIES.
#
# THE ONE THING A PRIVATE LOBBY CANNOT SUPPLY IS LIVENESS. A matchmaking lobby proves its host's game is still running
# by re-ticketing every few seconds (LOBBY_FRESH_SECONDS); a private one has no heartbeat. Two signals stand in and
# both must hold: the host is online (is_online), and the host's game has published this very session as its activity
# - invite_landable already refuses with "noHandle" unless the handle names view["key"], which for this view is the
# session the invite would land in. So a session file a crashed game left behind is never invited into: its owner's
# activity handle names another session, or none. Nothing here is faked to fill a gap; a private lobby that cannot
# supply what an invite needs is refused with the reason naming the missing piece, which the Guide already has
# wording for.
#
# server_config.json (read again when the file changes, no restart):
#   "invitePrivateLobbies"   false keeps invites to matchmaking lobbies, as before this patch; true or absent, on.
INVITE_PRIVATE_VERSION = "invite_private v1"
INVITE_PRIVATE_CACHE = {"key": None, "on": True}   # under INVITE_LOCK, beside INVITE_CONFIG_CACHE


def invite_private():
    """server_config.json "invitePrivateLobbies": may an invite land in a lobby nobody is matchmaking from? On
    unless it says false. Read again when the file changes, the way invite_on does."""
    try:
        info = os.stat(JOIN_CONFIG_FILE)
        key = (info.st_mtime_ns, info.st_size)
    except OSError:
        return True
    with INVITE_LOCK:
        if INVITE_PRIVATE_CACHE["key"] == key:
            return INVITE_PRIVATE_CACHE["on"]
    on = True
    try:
        with open(JOIN_CONFIG_FILE, encoding="utf-8-sig") as handle:
            value = json.load(handle).get("invitePrivateLobbies")
        if value in (True, False):
            on = value
        elif value is not None:
            print('[INVITE] server_config.json "invitePrivateLobbies" is %r - only true or false; taken as true'
                  % (value,))
    except (OSError, ValueError, AttributeError) as error:
        print("[INVITE] server_config.json cannot be read (%s): private lobbies stay invitable" % error)
    with INVITE_LOCK:
        INVITE_PRIVATE_CACHE.update(key=key, on=on)
    return on


def invite_session_ref(session):
    """The sessionRef a stored session carries: what a matchmaking lobby takes from its host's ticket and a private
    one has nowhere else to get. decorate_session writes it on every save, so every session lobby_index walked has
    one. Taken verbatim - the record's own key is lower-cased and this server's files are not. None when the three
    fields are not all there, or it is not this game's."""
    ref = session.get("sessionRef") if isinstance(session, dict) else None
    if not isinstance(ref, dict):
        return None
    scid, template_name, name = ref.get("scid"), ref.get("templateName"), ref.get("name")
    if not scid or not template_name or not name or not is_hd_scid(scid):
        return None
    return {"scid": scid, "templateName": template_name, "name": name}


def invite_private_version(record):
    """A private lobby's game data value: its session's InviteVersion, else the host's last ticket - the same two
    places lobby_version looks, minus the MM_LOBBIES entry this lobby does not have. None when neither knows, which
    is what a lobby whose host has not searched since logging in looks like; invite_landable then lets it through,
    exactly as it does for a matchmade lobby whose invitee has posted no ticket, and the game checks for itself."""
    version = session_version(record["session"])
    return version if version is not None else LAST_TICKET_VERSION.get(base_of(record["host"]))


def invite_private_view(record, host, my_version):
    """join_lobby_view's shape for a lobby nobody is matchmaking from: the private room a host is simply sitting in.
    Everything comes from the stored session, because that is all such a lobby has. The caller holds STATE_LOCK and
    only reads the session - it is the parse cache's own object. None when there is nothing to land in."""
    if not is_online(host):
        # The stand-in for LOBBY_FRESH_SECONDS: a matchmaking lobby proves its host's game is still there by
        # re-ticketing, this one cannot. invite_landable's handle check then proves it is the session they are in.
        return None
    ref = invite_session_ref(record["session"])
    if ref is None:
        return None
    if solo_xuid(record["host"]):   # (solo_patch v1) open_lobby's check on whoever hosts it, done here by hand
        return None
    members = record["session"].get("members") if isinstance(record["session"].get("members"), dict) else {}
    if not accepted_member_keys(members):   # nobody has actually joined it: open_lobby refuses these too
        return None
    free = lobby_size(ref) - len(member_keys(members))   # the template's size, never record["max"]
    version = invite_private_version(record)
    view = join_record_view(record)   # it already copes with a record that has no MM entry: stageText stays None
    view.update(key=record["key"], kind="private", private=True, ref=copy.deepcopy(ref),
                open=not record["closed"] and free > 0, closed=record["closed"], free=free,
                sameVersion=None if my_version is None or version is None else my_version == version,
                menu="questSelection")
    return view


def invite_lobby_view(record, host, now, my_version):
    """What an invite needs about a host's lobby: join_lobby_view's view while the host is matchmaking, else - and
    this is the point - the same shape built from the session alone, so a friend can be invited into a private room
    without the host leaving an open matchmaking room. Only the invite path calls this; Join never does."""
    if not record:
        return None
    view = join_lobby_view(record, now, my_version)
    if view is not None or not invite_private():
        return view
    return invite_private_view(record, host, my_version)


def invite_landable(invitee, host, index, now, handles):
    """(the host's lobby view with "activityHandle", None) when an invite to this host could be taken RIGHT NOW,
    else (view or None, reason). The reasons are friends[].join's, so the Guide already has sentences for them.
    The caller holds STATE_LOCK."""
    record = index["players"].get(host)
    if record is None:
        return None, "noLobby"
    my_version = LAST_TICKET_VERSION.get(invitee)
    # (invite_private v1) the matchmaking view while the host is matchmaking, else the same shape from the session
    # alone. join_friend_state, join_my_joining, place_friend_seeker and friends_join still call join_lobby_view.
    view = invite_lobby_view(record, host, now, my_version)
    if view is None:
        return None, "noLobby"
    if join_activity(host) in DUNGEON_ACTIVITIES or record["closed"] or not view["open"]:
        return view, "dungeon"
    if view["sameVersion"] is False:
        return view, "otherData"
    if not fps_invite_landable(invitee, host, record):   # (fps_split v3) invite_accept then writes no reservation
        return view, "otherFps"
    if view["free"] <= 0:
        return view, "full"
    handle = handles.get(host)
    if not (handle and handle[1] == view["key"].lower()):
        return view, "noHandle"
    view["activityHandle"] = handle[0]
    return view, None


def invite_reason_text(reason, name):
    """One sentence for a reason code, for a client that has no wording of its own."""
    return {"noLobby": "%s is not in a lobby yet" % name,
            "dungeon": "%s is in a dungeon - you will be pulled in when they are back in a lobby" % name,
            "full": "%s's lobby is full right now" % name,
            "otherData": "Different game data - whoever is behind must update (press Play in Happy Dungeons Online)"
                         " or restart the game",
            "otherFps": "%s plays at another frame rate - 30 and 60 fps players play separately. Change Frame rate "
                        "in the Guide (Settings) to play together" % name,   # (fps_split v3)
            "noHandle": "%s's game has not published its lobby yet - a moment" % name,
            "offline": "%s went offline" % name,
            "together": "You are already in one lobby",
            "solo": "%s is playing solo right now" % name,
            "youSolo": "Solo play is on - turn it off on the players page to invite friends",
            "noInviteClient": "%s needs the newer WinDurango build before they can be invited" % name,
            "invited": "Invited - waiting for %s" % name,
            "busy": "%s has another invite open" % name,
            "notFriend": "Only friends can be invited here",
            "self": "That is you"}.get(reason, "Not right now")


def invite_friend_state(me, friend, index, now, handles):
    """friends[].invite: may I invite this friend into my lobby? The caller holds STATE_LOCK."""
    name = gamertag_for(primary_xuid(friend))

    def no(reason):
        return {"can": False, "reason": reason, "text": invite_reason_text(reason, name)}

    if index["players"].get(me) is None:
        return {"can": False, "reason": "noLobby", "text": "You are not in a lobby - Create Session first."}
    if not is_online(friend):
        return no("offline")
    if join_together(me, friend, index):
        return no("together")
    if solo_play(me):
        return no("youSolo")
    if solo_play(friend):
        return no("solo")
    fps_refused = fps_invite_refusal(me, friend, index)   # (fps_split v3) 30 and 60 fps play separately
    if fps_refused:
        return {"can": False, "reason": "otherFps", "text": fps_refused}
    if not invite_client_ready(friend):
        return no("noInviteClient")
    open_now = INVITES.get(str(friend))
    if open_now and open_now["until"] > now:
        return no("invited" if open_now["from"] == me else "busy")
    return {"can": True, "reason": "ok", "text": "Invite %s into your lobby" % name}


def invite_my_incoming(base, index, now, handles):
    """you.invite: the invite waiting for this player, or None. Drops one whose time is up, or whose host is gone.
    The caller holds STATE_LOCK."""
    record = INVITES.get(str(base))
    if record is None:
        return None
    if record["until"] <= now or not is_online(record["from"]):
        INVITES.pop(str(base), None)
        return None
    host = record["from"]
    view, reason = invite_landable(base, host, index, now, handles)
    name = gamertag_for(primary_xuid(host))
    ready = reason is None
    mine = index["players"].get(base)
    if ready and mine is not None and (mine["closed"] or join_activity(base) in DUNGEON_ACTIVITIES):
        # The invitee is in a dungeon of their own. Raising the activation now would only park it inside the game
        # until it is joinable again, so the Guide holds it and says so instead.
        ready, reason = False, "youDungeon"
    return {"fromXuid": primary_xuid(host), "host": name,
            "stageText": (view or {}).get("stageText"), "players": (view or {}).get("players"),
            "max": (view or {}).get("max"), "menu": (view or {}).get("menu") or "questSelection",
            "state": "ready" if ready else "held", "reason": None if ready else reason,
            "secondsLeft": max(0, int(record["until"] - now + 0.5)),
            "text": ("%s invited you into their lobby" % name) if ready else
            ("You are in a dungeon - finish or leave it and %s's invite takes you there" % name)
            if reason == "youDungeon" else fps_invite_text(reason, name, base, host, index)}


def invite_me_fields(base, doc, index, now, handles):
    """/windurango/me's invite half. The caller holds STATE_LOCK."""
    doc["inviteApi"] = INVITE_API
    if isinstance(doc.get("you"), dict):
        doc["you"]["invite"] = invite_my_incoming(base, index, now, handles)
    for friend in doc.get("friends") or []:
        other = base_of(friend.get("xuid"))
        friend["invite"] = ({"can": False, "reason": "offline", "text": "Offline"} if other is None
                            else invite_friend_state(base, other, index, now, handles))
    return doc


def invite_other(body):
    """(the other player's xuidBase, None), or (None, the response refusing it)."""
    if not isinstance(body, dict) or "xuid" not in body:
        return None, json_response({"ok": False, "reason": "badRequest",
                                    "text": 'Send {"xuid": "<your friend\'s XUID>"}.'}, 400)
    other = base_of(body.get("xuid"))
    if other is None:
        return None, json_response({"ok": False, "reason": "noPlayer",
                                    "text": "Nobody on this server has that XUID."}, 404)
    return other, None


def invite_refused(base, other):
    """A refusal that has nothing to do with lobbies (not a friend, is you, invites off), or None."""
    if not invite_on():
        return json_response({"ok": False, "reason": "off",
                              "text": "This server has invites turned off."}, 404)
    if other == base:
        return json_response({"ok": False, "reason": "self", "text": "That is you."}, 409)
    if primary_xuid(other) not in friends_of(primary_xuid(base)):
        return json_response({"ok": False, "reason": "notFriend",
                              "text": "Only friends can be invited here - add them first."}, 409)
    return None


def invite_reserve(view, xuid, now):
    """Keeps the invitee a place in the host's session: the reserved member row reserve_into_lobby writes for a
    matchmade seeker, and freed the same way by expire_reservation. The caller holds STATE_LOCK and has ALREADY
    checked friendship, versions, membership_refusal and the free place - a reservation is not checked again when
    the game writes itself in (apply_member_writes). The member key, or None when the lobby went away underneath."""
    if view.get("private"):
        # (invite_private v1) a private lobby has no MM_LOBBIES entry and no ticket to re-read: its ref is the one
        # the session carries, and the file is read again here so the place is never written from the index's older
        # copy (which is the parse cache's own object). The guards open_lobby would have done are redone on what
        # was just read, because the lobby can have closed or emptied since invite_landable looked.
        ref = view["ref"]
        session = load_session_from_ref(ref)
        if not isinstance(session, dict):
            return None
        members_now = session.get("members") if isinstance(session.get("members"), dict) else {}
        if (((session.get("properties") or {}).get("system") or {}).get("closed") is True
                or solo_xuid(session_host_xuid(session)) or not accepted_member_keys(members_now)):
            return None
        lobby = {}   # no MM record: invite_ticket_attrs then copies only what the members already carry
        print("[INVITE] %s's lobby %s has no matchmaking ticket - the invite reads its private session instead"
              % (gamertag_for(session_host_xuid(session)), ref["name"]))
    else:
        lobby = MM_LOBBIES.get(view["key"])
        found = open_lobby(view["key"], now)
        if lobby is None or found is None:
            return None
        ref = lobby["ref"]
        session = copy.deepcopy(found[1])   # (tuning_patch) open_lobby's session may be the parse cache's own
    previous = copy.deepcopy(session)
    members = session.setdefault("members", {})
    if find_member_key(members, xuid) is not None:
        return None
    if lobby_size(ref) - len(member_keys(members)) <= 0:
        return None
    key = str(next_member_key(session))
    members[key] = {
        "reserved": True,
        "constants": {
            "system": {"xuid": xuid, "initialize": True},
            "custom": {"matchmakingResult": {"ticketAttrs": invite_ticket_attrs(session, lobby),
                                             "playerAttrs": {}}},
        },
        "properties": {"system": {}, "custom": {}},
    }
    session.setdefault("membersInfo", {})["next"] = int(key) + 1
    session = decorate_session(session, ref["scid"], ref["templateName"], ref["name"])
    stamp_session_change(session, previous)
    save_json(session_file(ref["scid"], ref["templateName"], ref["name"]), session)
    relay_session_changed(session)   # the invitee's game may talk to the lobby's before it claims the place
    print("[INVITE] %s has a place kept in %s's lobby %s (%d of %d places taken)" % (
        gamertag_for(xuid), gamertag_for(session_host_xuid(session)), ref["name"],
        len(session["members"]), lobby_size(ref)))
    log_members(session)
    notify_session_changed(session, previous)
    timer = threading.Timer(INVITE_RESERVE_SECONDS, expire_reservation, args=(copy.deepcopy(ref), xuid))
    timer.daemon = True
    timer.start()
    return key


@app.route("/windurango/friends/invite", methods=["POST"])
def invite_send():
    """{"xuid": "<a friend's XUID>"}: offer that friend a place in my lobby. It waits in their Guide for
    INVITE_SECONDS, held while my lobby cannot take them. 200 {"ok": true, "state": "sent" | "held", ...};
    409 {"ok": false, "reason", "text"}; 404 no such player, or invites are off."""
    base, refusal = require_player()
    if refusal:
        return refusal
    other, bad = invite_other(request.get_json(silent=True))
    if bad:
        return bad
    refused = invite_refused(base, other)
    if refused:
        return refused
    now = time.time()
    index = lobby_index(now)
    handles = join_activity_handles()
    with STATE_LOCK:
        state = invite_friend_state(base, other, index, now, handles)
        if not state["can"]:
            return json_response({"ok": False, "reason": state["reason"], "text": state["text"]}, 409)
        view, reason = invite_landable(other, base, index, now, handles)
        INVITES[str(other)] = {"from": base, "host": gamertag_for(primary_xuid(base)), "at": now,
                               "until": now + INVITE_SECONDS,
                               "seenKey": (view or {}).get("key")}
        name = gamertag_for(primary_xuid(other))
        print("[INVITE] %s invited %s into their lobby%s" % (
            gamertag_for(primary_xuid(base)), name, "" if reason is None else " (held: %s)" % reason))
    return json_response({"ok": True, "state": "sent" if reason is None else "held",
                          "friendXuid": primary_xuid(other), "reason": reason,
                          "secondsLeft": INVITE_SECONDS,
                          "text": ("%s has been invited into your lobby." % name) if reason is None
                          else "%s has been invited - %s." % (name, invite_reason_text(reason, "your lobby"))})


@app.route("/windurango/friends/invite/cancel", methods=["POST"])
def invite_cancel():
    """{"xuid": "<the friend I invited>"}: take the offer back. 200 always when it was mine."""
    base, refusal = require_player()
    if refusal:
        return refusal
    other, bad = invite_other(request.get_json(silent=True))
    if bad:
        return bad
    with STATE_LOCK:
        record = INVITES.get(str(other))
        if record is not None and record["from"] == base:
            INVITES.pop(str(other), None)
            print("[INVITE] %s took back their invite to %s" % (gamertag_for(primary_xuid(base)),
                                                                gamertag_for(primary_xuid(other))))
    return json_response({"ok": True, "state": "cancelled", "friendXuid": primary_xuid(other)})


@app.route("/windurango/friends/invite/decline", methods=["POST"])
def invite_decline():
    """{"xuid": "<the host who invited me>"}: no thanks. 200 always when it was for me."""
    base, refusal = require_player()
    if refusal:
        return refusal
    other, bad = invite_other(request.get_json(silent=True))
    if bad:
        return bad
    with STATE_LOCK:
        record = INVITES.get(str(base))
        if record is not None and record["from"] == other:
            INVITES.pop(str(base), None)
            print("[INVITE] %s turned down %s's invite" % (gamertag_for(primary_xuid(base)),
                                                           gamertag_for(primary_xuid(other))))
    return json_response({"ok": True, "state": "declined", "hostXuid": primary_xuid(other)})


@app.route("/windurango/friends/invite/accept", methods=["POST"])
def invite_accept():
    """{"xuid": "<the host who invited me>"}: take the place. Everything is checked again here, because the
    reservation this writes is NOT checked again when the game writes itself in (apply_member_writes skips
    join_refusal, membership_refusal and the capacity check for a member that already holds a place).
    200 {"ok": true, "handle": "<the host's activity handle id>", "joineeXuid", "joinerXuid", ...} - the client
    raises the game's protocol activation with those. 409 {"ok": false, "reason", "text"}: not now, and no
    reservation was written."""
    base, refusal = require_player()
    if refusal:
        return refusal
    other, bad = invite_other(request.get_json(silent=True))
    if bad:
        return bad
    refused = invite_refused(base, other)
    if refused:
        return refused
    now = time.time()
    index = lobby_index(now)
    handles = join_activity_handles()
    with STATE_LOCK:
        record = INVITES.get(str(base))
        if record is None or record["from"] != other or record["until"] <= now:
            return json_response({"ok": False, "reason": "gone",
                                  "text": "That invite is no longer open - ask them to invite you again."}, 409)
        name = gamertag_for(primary_xuid(other))
        view, reason = invite_landable(base, other, index, now, handles)
        if reason is not None:
            return json_response({"ok": False, "reason": reason, "text": fps_invite_text(reason, name, base, other,
                                                                                       index),   # (fps_split v3)
                                  "state": "held"}, 409)
        if join_together(base, other, index):
            INVITES.pop(str(base), None)
            return json_response({"ok": False, "reason": "together",
                                  "text": "You are already in %s's lobby." % name}, 409)
        stop = membership_refusal(primary_xuid(base))   # the reservation walks past this later: check it now
        if stop:
            return json_response({"ok": False, "reason": "tooMany", "text": "Can't join: %s." % stop}, 409)
        key = invite_reserve(view, primary_xuid(base), now)
        if key is None:
            return json_response({"ok": False, "reason": "full",
                                  "text": "%s's lobby filled up - ask them to invite you again." % name}, 409)
        INVITES.pop(str(base), None)
        JOIN_WISHES.pop(base, None)   # an invite supersedes a wish for the same player; it never writes one
        print("[INVITE] %s accepted %s's invite (handle %s, member %s)" % (
            gamertag_for(primary_xuid(base)), name, view["activityHandle"], key))
    return json_response({"ok": True, "state": "accepted", "handle": view["activityHandle"],
                          "joineeXuid": primary_xuid(other), "joinerXuid": primary_xuid(base),
                          "hostXuid": primary_xuid(other), "host": name,
                          "stageText": view["stageText"], "players": view["players"], "max": view["max"],
                          "secondsLeft": INVITE_RESERVE_SECONDS,
                          "text": "A place is being kept in %s's lobby." % name})


def invite_summary():
    """The startup line."""
    return ("Invites: %s (invite a friend into your lobby; an offer stays open %d min, a place is kept %d s; "
            "server_config.json \"invites\"). Private lobbies: %s (server_config.json \"invitePrivateLobbies\")"
            % ("on" if invite_on() else "off - server_config.json \"invites\"",
               INVITE_SECONDS // 60, INVITE_RESERVE_SECONDS,
               "invitable - a host need not leave an open room" if invite_private()   # (invite_private v1)
               else "not invitable - matchmaking lobbies only"))


@app.route("/windurango/friends/join", methods=["POST"])
def friends_join():
    """{"xuid": "<a friend's XUID>"}: put me into that friend's lobby the next time I search for a game to join
    (within FRIEND_JOIN_SECONDS; at once when I am searching alone now). {"xuid": null} cancels it.
    200 {"ok": true, "state": "armed" | "placed" | "cancelled", ...}; 409 {"ok": false, "reason", "text"} with the
    friends[].join reason codes (plus self, notFriend); 404 no such player; 400 no "xuid" in the body."""
    base, refusal = require_player()
    if refusal:
        return refusal
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or "xuid" not in body:
        return json_response({"ok": False, "reason": "badRequest",
                              "text": 'Send {"xuid": "<your friend\'s XUID>"}, or {"xuid": null} to cancel.'}, 400)
    now = time.time()
    if body.get("xuid") in (None, ""):
        with STATE_LOCK:
            wish = JOIN_WISHES.pop(base, None)
            FRIEND_JOIN_RESULTS.pop(base, None)
        if wish:
            print("[JOIN] %s cancelled their join" % gamertag_for(primary_xuid(base)))
        return json_response({"ok": True, "state": "cancelled"})
    friend = base_of(body.get("xuid"))
    if friend is None:
        return json_response({"ok": False, "reason": "noPlayer", "text": "Nobody on this server has that XUID."}, 404)
    if friend == base:
        return json_response({"ok": False, "reason": "self", "text": "That is you."}, 409)
    if primary_xuid(friend) not in friends_of(primary_xuid(base)):
        return json_response({"ok": False, "reason": "notFriend",
                              "text": "Only friends can be joined here - add them first, or use the lobby list on "
                                      "the players page."}, 409)
    index = lobby_index(now)
    with STATE_LOCK:
        my_version = LAST_TICKET_VERSION.get(base)
        previous = JOIN_WISHES.pop(base, None)   # Join pressed again re-arms it: judge the friend's lobby afresh
        _lobby, join = join_friend_state(base, friend, index, now, my_version, {})
        if not join["can"]:   # (without a wish of the caller's, only "ok" can join: an open lobby)
            if previous is not None:
                JOIN_WISHES[base] = previous
            return json_response({"ok": False, "reason": join["reason"], "text": join["text"]}, 409)
        view = join_lobby_view(index["players"].get(friend), now, my_version)
        role = join_my_role(base, index)
        JOIN_WISHES[base] = {"lobby": view["key"], "until": now + FRIEND_JOIN_SECONDS, "friend": friend,
                             "since": now}
        FRIEND_JOIN_RESULTS.pop(base, None)
        print("[JOIN] %s pressed Join on %s (%s's lobby, %s)" % (
            gamertag_for(primary_xuid(base)), gamertag_for(primary_xuid(friend)), view["host"], view["stageText"]))
        placed = False
        for ticket in sorted(MM_TICKETS.values(), key=lambda t: t["created"]):
            if ticket["role"] == "seeker" and base_of(ticket["owner"]) == base:
                placed = place_seeker(ticket, now)   # already searching: straight in
                break
        wish = JOIN_WISHES.get(base) or {}
    return json_response({
        "ok": True, "state": "placed" if placed else "armed", "friendXuid": primary_xuid(friend),
        "host": view["host"], "stageText": view["stageText"], "players": view["players"], "max": view["max"],
        "menu": view["menu"], "secondsLeft": 0 if placed else FRIEND_JOIN_SECONDS,
        "howTo": ("Your game is joining %s's lobby now." % view["host"]) if placed
        else join_how_to(view["menu"], role, view["host"], view["stageText"]),
        "reason": None if placed else wish.get("reason"),
        "text": ("You are in %s's lobby." % view["host"]) if placed
        else "Joining %s's lobby \u00b7 %s \u00b7 %d/%d" % (view["host"], view["stageText"], view["players"],
                                                          view["max"])})


def news_check(doc):
    """data/news.json -> {"id", "title", "lines", "until"} or ValueError."""
    if not isinstance(doc, dict):
        raise ValueError("it is not a JSON object")
    news_id, title, lines = doc.get("id"), doc.get("title", "What's new"), doc.get("lines")
    if not isinstance(news_id, str) or not NEWS_ID.fullmatch(news_id):
        raise ValueError('"id" must be 1 to 64 letters, digits, ".", "_" or "-"')
    if not isinstance(title, str) or not 0 < len(title) <= 80:
        raise ValueError('"title" must be text of 1 to 80 characters')
    if (not isinstance(lines, list) or not 0 < len(lines) <= 8
            or not all(isinstance(line, str) and 0 < len(line) <= 160 for line in lines)):
        raise ValueError('"lines" must be a list of 1 to 8 lines of text (up to 160 characters each)')
    until = doc.get("until")
    if until is not None:
        try:
            until = datetime.strptime(str(until), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            raise ValueError('"until" must look like 2026-10-31T00:00:00Z') from None
    return {"id": news_id, "title": title, "lines": list(lines), "until": until}


def news_item():
    """data/news.json as last read (read again when it changes), or None: no file, or one that is not right (logged
    once per change)."""
    try:
        info = os.stat(NEWS_FILE)
        key = (info.st_mtime_ns, info.st_size)
    except OSError:
        return None
    with JOIN_LOCK:
        if NEWS_CACHE["key"] == key:
            return NEWS_CACHE["item"]
    item = None
    try:
        with open(NEWS_FILE, encoding="utf-8-sig") as handle:
            item = news_check(json.load(handle))
    except (OSError, ValueError) as error:
        print("[NEWS] data/news.json is left out: %s" % error)
    with JOIN_LOCK:
        NEWS_CACHE.update(key=key, item=item)
    return item


def news_for(base, now):
    """/windurango/me's "news": the news item while it runs and this player has not marked it seen, else None."""
    item = news_item()
    if item is None or (item["until"] is not None and now >= item["until"]):
        return None
    with STATE_LOCK:
        if item["id"] in NEWS_SEEN.get(base, ()):
            return None
    return {"id": item["id"], "title": item["title"], "lines": item["lines"]}


def news_load_seen():
    try:
        with open(NEWS_SEEN_FILE, encoding="utf-8") as handle:
            loaded = json.load(handle)
        return {str(k): [str(x) for x in v][-NEWS_SEEN_KEEP:] for k, v in loaded.items() if isinstance(v, list)}
    except (OSError, ValueError, AttributeError):
        return {}


NEWS_SEEN.update(news_load_seen())


@app.route("/windurango/news/seen", methods=["POST"])
def news_seen():
    """{"id": "<news id>"}: the player's game showed that news item; /windurango/me leaves it out from now on."""
    base, refusal = require_player()
    if refusal:
        return refusal
    body = request.get_json(silent=True)
    news_id = body.get("id") if isinstance(body, dict) else None
    if not isinstance(news_id, str) or not NEWS_ID.fullmatch(news_id):
        return json_response({"ok": False, "reason": 'send {"id": "<the news id>"}'}, 400)
    item = news_item()   # only the current item: each new made-up id used to rewrite news_seen.json (release2 review)
    if item is None or item["id"] != news_id:
        return json_response({"ok": False, "reason": "that is not the current news item"}, 404)
    with STATE_LOCK:
        if news_id in NEWS_SEEN.get(base, ()):
            return json_response({"ok": True, "id": news_id})   # already marked: nothing to write
        seen = NEWS_SEEN.get(base, []) + [news_id]
        NEWS_SEEN[base] = seen[-NEWS_SEEN_KEEP:]
        try:
            save_json(NEWS_SEEN_FILE, NEWS_SEEN)
        except OSError as error:
            print("COULD NOT SAVE NEWS SEEN:", error)
    return json_response({"ok": True, "id": news_id})


def friends_join_summary():
    """The startup line."""
    mode = join_mode()
    item = news_item()
    if item is None:
        news = "no news item (data/news.json)"
    else:
        with STATE_LOCK:
            seen = sum(1 for ids in NEWS_SEEN.values() if item["id"] in ids)
        news = "news %r, seen by %d player%s%s" % (item["id"], seen, "" if seen == 1 else "s",
                                                  " (its time is over)" if item["until"] is not None
                                                  and time.time() >= item["until"] else "")
    return "Friends join: joinMode %s (%s); a Join stays ready %d minutes; %s" % (
        mode, "phase 1 - the join wish" if mode == "wish" else "phase 2 - the game's join-a-friend launch",
        FRIEND_JOIN_SECONDS // 60, news)


@app.route("/windurango/friends/add", methods=["POST"])
def friends_add():
    """{"xuid"}: ask that player to be friends, or accept their request."""
    base, refusal = require_player()
    if refusal:
        return refusal
    target_base, refusal = other_player(request.get_json(silent=True))
    if refusal:
        return refusal
    me, them = primary_xuid(base), primary_xuid(target_base)
    if me == them:
        return json_response({"ok": False, "reason": "that is you"}, 400)

    with STATE_LOCK:
        if them in FRIENDS.get(me, []):
            state = "already friends"
        elif me in FRIEND_REQUESTS.get(them, []) and them in FRIEND_REQUESTS.get(me, []):
            state = "already friends"
        elif them in FRIEND_REQUESTS.get(me, []):
            # They asked first: this is an acceptance.
            FRIEND_REQUESTS[me] = [x for x in FRIEND_REQUESTS[me] if x != them]
            save_requests()
            set_friendship(me, them, True)
            state = "friends"
        else:
            FRIEND_REQUESTS[them] = sorted(set(FRIEND_REQUESTS.get(them, [])) | {me})
            save_requests()
            state = "requested"

    print("[FRIENDS] %s -> %s: %s" % (gamertag_for(me), gamertag_for(them), state))
    summary = player_summary(target_base)   # privacy_patch v2: no presence until they accept
    return json_response({"ok": True, "state": state,
                          "player": summary if privacy_may_see(them) else privacy_hidden(summary)})


@app.route("/windurango/friends/remove", methods=["POST"])
def friends_remove():
    """{"xuid"}: unfriend, decline a request, or cancel one you sent - whichever applies."""
    base, refusal = require_player()
    if refusal:
        return refusal
    target_base, refusal = other_player(request.get_json(silent=True))
    if refusal:
        return refusal
    me, them = primary_xuid(base), primary_xuid(target_base)
    with STATE_LOCK:
        FRIEND_REQUESTS[me] = [x for x in FRIEND_REQUESTS.get(me, []) if x != them]
        FRIEND_REQUESTS[them] = [x for x in FRIEND_REQUESTS.get(them, []) if x != me]
        save_requests()
    set_friendship(me, them, False)
    print("[FRIENDS] %s removed %s" % (gamertag_for(me), gamertag_for(them)))
    return json_response({"ok": True})


# Xbox privacy

@app.route("/users/<owner_id>/people/mute", methods=["GET"])
def privacy_mute(owner_id):
    log_request("PRIVACY / MUTE")

    print("OWNER:", owner_id)
    return json_response({"users": []})


# Xbox user statistics / reputation

@app.route(
    "/users/xuid(<xuid>)/scids/<scid>/stats/<path:stat_names>",
    methods=["GET"]
)
def user_stats(xuid, scid, stat_names):
    log_request("USER STATS")

    names = [
        x.strip()
        for x in stat_names.split(",")
        if x.strip()
    ]

    print("XUID:", xuid)
    print("SCID:", scid)
    print("STATS:", names)

    stats = []

    if scid.lower() == REPUTATION_SCID.lower():
        supported = {
            "OverallReputationIsBad",
            "FairplayReputationIsBad",
            "CommsReputationIsBad",
            "UserContentReputationIsBad",
        }

        for name in names:
            if name in supported:
                stats.append({
                    "statname": name,
                    "type": "Integer",
                    "value": 0
                })

    return json_response({
        "user": {
            "xuid": str(xuid),
            "stats": stats
        }
    })


# Xbox EDS / marketplace

# Happy Jewels.
#
# The jewel packs the game offers are listed in its own data file,
# Mount\wars\dungeon\json\happy_jewel\happy_jewel.bres -> HappyJewel.json. The game hands
# those product ids to XSAPI (Microsoft.Xbox.Services.dll), which asks the Xbox catalogue
# service for their details; with the service gone that came back empty and the shop said
# "Information could not be retrieved from the Xbox Store."
#
# The field names below are the UTF-16 literals in that DLL's EDS parser (.rdata 0xec7900..
# 0xec7cc0). Only Items[].Name and Items[].ID are read with the parser's "required" flag set,
# but every key it knows about is filled in anyway so nothing depends on that.
#
# Nothing is charged for any of this: the storefront these packs were sold on has been shut
# down since the service ended, so the price is 0 and the purchase is granted locally.

JEWEL_PACKS = [
    # product id                              jewels  name
    ("6629995d-7d8a-4bbf-a7ee-289fa67e5c63",    10),
    ("697e537f-b129-4e6e-9262-35ffe97e57cf",    21),
    ("18c9dffa-2888-4f84-9c14-bec969fb8122",    55),
    ("5de62d18-2a0b-4e84-98fe-e3cbd2c38952",   120),
    ("3b889656-d0f4-4ba4-b4e1-3d4b3f81ac0b",   150),
    ("da1f78ed-8d0f-4d6b-8b4f-ba00735ee109",   250),
    ("bf581d72-eda0-48d9-bfcc-e7c04307a43e",   390),
    ("07bc95b0-fd26-4ee6-877a-f7f6ed9a8b79",   600),
    ("72d022fc-9eb4-49a9-b7a1-cff203c7d928",   675),
    ("97cc63a6-8b81-40e5-9b00-cf26eb63ea02",  1400),
]

JEWEL_BY_ID = {product_id.lower(): jewels for product_id, jewels in JEWEL_PACKS}


def jewel_name(product_id):
    jewels = JEWEL_BY_ID.get(product_id.lower())
    if jewels is None:
        return "Happy Dungeons item"
    return f"{jewels} Happy Jewels"


def catalog_availability(product_id):
    name = jewel_name(product_id)
    jewels = JEWEL_BY_ID.get(product_id.lower(), 1)

    # Only ContentId, offerId and SignedOffer are read off the availability object itself
    # (Microsoft.Xbox.Services.dll 0x43c21f / 0x43c24d: "mov rdx, rsi"). Everything that
    # decides whether the pack can be sold - price, isPurchasable, quantity,
    # distributionType and the display strings - is read out of OfferDisplayData AFTER it has
    # been re-parsed from its JSON string into [rbp-0x80] (0x43ba2e, 0x43baf9: "lea rdx,
    # [rbp - 0x80]"). Put those at the availability level instead and XSAPI never sees them:
    # every offer then comes out isPurchasable=false, price=0, quantity=0, and the shop says
    # Happy Jewels cannot be used.
    display = {
        # offerId is the ONE required field inside OfferDisplayData: XSAPI extracts it from
        # the RE-PARSED document with its required flag set (Microsoft.Xbox.Services.dll
        # 0x43b7ac "mov byte ptr [rsp+0x20], 1", key slot [rbp+0x4c8] = L"offerId" bound at
        # 0x43b2a2, source rdx = [rbp-0x80] = the re-parsed OfferDisplayData). Leave it out
        # and every availability fails to deserialize, every item is dropped, and the game
        # gets a well-formed but EMPTY catalogue - which is what it calls
        # "Information could not be retrieved from the Xbox Store".
        "offerId": product_id,
        "displayPrice": "$0.00",
        "displayListPrice": "$0.00",
        "price": 0.0,
        "listPrice": 0.0,
        "currencyCode": "USD",
        "quantity": jewels,
        "distributionType": "Full",
        "isPurchasable": True,
        "promotionalText": "",
        "availabilityTitle": name,
        "availabilityDescription": name,
        "acceptablePaymentInstrumentTypes": ["CreditCard", "Tokens"],
    }

    return {
        "ContentId": product_id,
        "offerId": product_id,
        "SignedOffer": product_id,
        "OfferDisplayData": json.dumps(display),
    }


def catalog_item(product_id):
    name = jewel_name(product_id)

    return {
        "ID": product_id,
        "Name": name,
        "ReducedName": name,
        "Description": name,
        "MediaItemType": "DConsumable",
        "TitleId": HD_TITLE_ID,
        "SandboxId": "RETAIL",
        "ContentId": product_id,
        "IsBundle": False,
        "IsPartOfAnyBundle": False,
        "ReleaseDate": "2016-07-01T00:00:00Z",
        "DigitalReleaseDate": "2016-07-01T00:00:00Z",
        "Availabilities": [catalog_availability(product_id)],
    }


@app.route("/media/<marketplace_id>/details", methods=["GET"])
def eds_details(marketplace_id):
    log_request("EDS")

    requested = [
        part.strip()
        for part in (request.args.get("ids") or "").split(".")
        if part.strip()
    ]
    print("MARKETPLACE ID:", marketplace_id)
    print("REQUESTED IDS:", len(requested), requested)
    print("DESIRED TYPES:", request.args.get("desiredMediaItemTypes"))

    items = [catalog_item(product_id) for product_id in requested]
    known = sum(1 for product_id in requested if product_id.lower() in JEWEL_BY_ID)
    print(f"RETURNING {len(items)} items ({known} known Happy Jewel packs)")

    return json_response({
        "Items": items,
        "Totals": [{"Name": "DConsumable", "Count": len(items)}],
        "ImpressionGuid": str(uuid.uuid4())
    })


# Xbox Multiplayer Session Directory (MPSD)
#
# Happy Dungeons only: a request about any other game's service config (in the path, or in the sessionRef of a
# handle) gets not_happy_dungeons, and nothing is stored for it.

def not_happy_dungeons(scid):
    print("[MPSD] not Happy Dungeons (scid %s) - 404" % scid)
    return json_response({"error": "not_happy_dungeons", "scid": scid}, 404)


def handle_scid(handle):
    ref = handle.get("sessionRef") if isinstance(handle, dict) else None
    return ref.get("scid") if isinstance(ref, dict) else None


@app.route("/serviceconfigs/<scid>/sessions", methods=["GET"])
def mpsd_sessions(scid):
    log_request("MPSD SESSION QUERY")

    query_xuid = request.args.get("xuid")

    print("SCID:", scid)
    print("XUID FILTER:", query_xuid)
    print("CALLER XUID:", caller_xuid())

    if not is_hd_scid(scid):
        return not_happy_dungeons(scid)

    results = []

    # Only the caller's own sessions (the game asks for its own four xuids): session names are not handed to
    # anyone who asks, the players page lists the lobbies that take players.
    if query_xuid and verified_caller_xuid() and in_caller_block(query_xuid):
        for _, session in iter_sessions(scid):
            # Every session the player has joined (a reserved place is not "in" it yet).
            members = session.get("members") if isinstance(session.get("members"), dict) else {}
            key = find_member_key(members, query_xuid)
            if key is None or members[key].get("reserved") is True:
                continue
            results.append(
                build_session_query_result(
                    session,
                    query_xuid=query_xuid
                )
            )

    print("SESSION QUERY RESULTS:", len(results))
    return json_response({"results": results})


@app.route(
    "/serviceconfigs/<scid>/sessionTemplates/<template_name>/sessions",
    methods=["GET"]
)
@app.route(
    "/serviceconfigs/<scid>/sessiontemplates/<template_name>/sessions",
    methods=["GET"]
)
def mpsd_template_sessions(scid, template_name):
    log_request("MPSD TEMPLATE SESSION QUERY")

    print("SCID:", scid)
    print("TEMPLATE:", template_name)

    if not is_hd_scid(scid):
        return not_happy_dungeons(scid)

    # The game never asks this; it answers with the caller's own sessions only, as the xuid query does.
    results = [
        build_session_query_result(session)
        for _, session in iter_sessions(scid, template_name)
        if caller_member_key(session) is not None
    ]

    return json_response({"results": results})


@app.route(
    "/serviceconfigs/<scid>/sessionTemplates/<template_name>/sessions/<session_name>",
    methods=["GET", "PUT", "DELETE"]
)
@app.route(
    "/serviceconfigs/<scid>/sessiontemplates/<template_name>/sessions/<session_name>",
    methods=["GET", "PUT", "DELETE"]
)
def mpsd_specific_session(scid, template_name, session_name):
    log_request("MPSD SESSION")

    print("SCID:", scid)
    print("TEMPLATE:", template_name)
    print("SESSION:", session_name)

    if not is_hd_scid(scid):
        return not_happy_dungeons(scid)

    path = session_file(scid, template_name, session_name)
    if not path:
        return Response(status=400)

    if request.method == "DELETE":
        # The game never deletes a session (it leaves with members.me = null), and deleting one ends its players'
        # relay link to each other: only one of its joined members may.
        if not verified_caller_xuid():
            return json_response({"code": 401, "description": "sign in to delete sessions"}, 401)
        with STATE_LOCK:
            session = load_json(path)
            if (isinstance(session, dict) and member_keys(session.get("members"))
                    and caller_member_key(session) is None):
                print("[MPSD] %s is not in %s and may not delete it - refused" % (
                    gamertag_for(caller_xuid()), session_name))
                return json_response({"code": 403, "description": "only the session's members may delete it"}, 403)
            remove_session(scid, template_name, session_name, path)

        return Response(status=204)

    if request.method == "GET":
        # privacy_patch v2 sessions: a session document (every member's XUID, gamertag, device token, connection and
        # subscription ids, relay addresses, the next session's name) only for a caller who proves who they are.
        # The game signs every request (3150 of 3150 session GETs in the logs carried its key).
        if not verified_caller_xuid():
            return json_response({"code": 401, "description": "sign in to read sessions"}, 401)
        # Read-only and the same for every signed-in caller: XSAPI finds its own member by xuid.
        if tuning("fastPaths"):   # (tuning_patch) the parse cache: a write's os.replace is atomic, so no lock needed
            session = session_shared(path)
        else:
            with STATE_LOCK:
                session = load_json(path)
        if not isinstance(session, dict):
            return Response(status=404)
        session = decorate_session(session, scid, template_name, session_name)
        response = json_response(session)
        response.headers["ETag"] = session_etag(session)
        return response

    incoming = request.get_json(silent=True)

    if incoming is None:
        raw = request.get_data(as_text=True)

        try:
            incoming = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            incoming = {}

    print(
        "PUT BODY:",
        json.dumps(incoming, separators=(",", ":"))
    )

    return write_session_multi(scid, template_name, session_name, incoming)


@app.route(
    "/serviceconfigs/<scid>/sessionTemplates/<template_name>/sessions/"
    "<session_name>/members/me",
    methods=["DELETE"]
)
@app.route(
    "/serviceconfigs/<scid>/sessiontemplates/<template_name>/sessions/"
    "<session_name>/members/me",
    methods=["DELETE"]
)
def mpsd_leave_session(scid, template_name, session_name):
    log_request("MPSD LEAVE SESSION")

    if not is_hd_scid(scid):
        return not_happy_dungeons(scid)

    response = write_session_multi(scid, template_name, session_name, {"members": {"me": None}})
    return response if response.status_code >= 400 else Response(status=204)


# MPSD activity / invite handles

@app.route("/handles", methods=["POST"])
def mpsd_create_handle():
    log_request("MPSD HANDLE CREATE")

    # privacy_patch v2: a handle is made by the verified player it belongs to (the game signs every POST /handles).
    # A bare X-WinDurango-Player header made handles as anyone - an activity handle replaced that player's real one,
    # and an invite showed up in the invitee's /handles/query.
    if not verified_caller_xuid():
        return json_response({"error": "sign in to make handles"}, 401)

    body = request.get_json(silent=True)

    if not isinstance(body, dict):
        return json_response({
            "error": "invalid_handle_body"
        }, 400)

    handle_type = body.get("type")
    session_ref = body.get("sessionRef")
    version = body.get("version", 1)

    print("HANDLE TYPE:", handle_type)
    print("VERSION:", version)
    print("SESSION REF:", session_ref)

    if not isinstance(session_ref, dict):
        return json_response({
            "error": "missing_session_ref"
        }, 400)

    if not is_hd_scid(session_ref.get("scid")):
        return not_happy_dungeons(session_ref.get("scid"))

    if not session_ref.get("name"):
        session_ref = copy.deepcopy(session_ref)
        session_ref["name"] = str(uuid.uuid4())

    handle = {
        "id": str(uuid.uuid4()),
        "version": version,
        "type": handle_type,
        "sessionRef": session_ref,
        "ownerXuid": str(caller_xuid() or "")
    }

    for key in (
        "inviteAttributes",
        "invitedXuid",
        "titleId"
    ):
        if key in body:
            handle[key] = body[key]

    if handle_type == "activity":
        replace_activity_handle(handle)

        print("CURRENT ACTIVITY SESSION:", session_ref)

        # SetActivity returns no response body.
        response = Response(status=201)
        response.headers["Cache-Control"] = "no-cache, no-store"
        return response

    if handle_type == "invite":
        save_handle(handle)
        return json_response(handle, 201)

    return json_response({
        "error": "unsupported_handle_type",
        "type": handle_type
    }, 400)


@app.route("/handles/query", methods=["POST"])
def mpsd_query_handles():
    log_request("MPSD HANDLE QUERY")

    body = request.get_json(silent=True) or {}
    wanted_type = body.get("type")
    wanted_scid = body.get("scid")

    if wanted_scid and not is_hd_scid(wanted_scid):
        return not_happy_dungeons(wanted_scid)

    results = []
    # A handle names its session, so a caller gets its own handles, invites for it and its friends' activity.
    caller = verified_caller_xuid()
    friends = set(friends_of(primary_xuid(g.caller_base))) if caller else set()

    for filename in os.listdir(HANDLE_DATA) if caller else []:
        if not filename.endswith(".json"):
            continue

        handle = handle_shared(   # (tuning_patch: read-only here; json_response only serializes it)
            os.path.join(HANDLE_DATA, filename)
        )

        if not isinstance(handle, dict) or not is_hd_scid(handle_scid(handle)):
            continue

        if wanted_type and handle.get("type") != wanted_type:
            continue

        if wanted_scid and handle_scid(handle) != wanted_scid:
            continue

        owner = base_of(handle.get("ownerXuid"))
        if not (in_caller_block(handle.get("ownerXuid")) or in_caller_block(handle.get("invitedXuid"))
                or (handle.get("type") == "activity" and owner and primary_xuid(owner) in friends)):
            continue

        results.append(handle)

    print("HANDLE QUERY RESULTS:", len(results))
    return json_response({"results": results})


def privacy_handle_refusal(handle):
    """(privacy_patch v2 sessions) None when the caller may read this handle and the session it names - the verified
    player who made it, the one it invites, or (an activity handle) a friend of its maker: the /handles/query rule.
    Else the 401/403 answer. The game never reads a handle by id (it queries); a handle that is not there: None."""
    if not verified_caller_xuid():
        return json_response({"error": "sign in to read handles"}, 401)
    if not isinstance(handle, dict):
        return None
    owner = base_of(handle.get("ownerXuid"))
    if (in_caller_block(handle.get("ownerXuid")) or in_caller_block(handle.get("invitedXuid"))
            or (handle.get("type") == "activity" and owner
                and primary_xuid(owner) in set(friends_of(primary_xuid(g.caller_base))))):
        return None
    return json_response({"error": "not your handle"}, 403)


@app.route("/handles/<handle_id>", methods=["GET", "DELETE"])
def mpsd_handle_by_id(handle_id):
    log_request("MPSD HANDLE")

    path = handle_file(handle_id)
    handle = load_json(path)

    if isinstance(handle, dict) and not is_hd_scid(handle_scid(handle)):
        return not_happy_dungeons(handle_scid(handle))

    if request.method == "DELETE":
        # privacy_patch v2: only the verified player whose XUID made the handle removes it (the game clears its own
        # activity, signed in). A handle that is not there: 204 as before.
        if isinstance(handle, dict) and not (verified_caller_xuid() and in_caller_block(handle.get("ownerXuid"))):
            return json_response({"error": "not your handle"}, 403)
        if path and os.path.isfile(path):
            os.remove(path)

        return Response(status=204)

    refusal = privacy_handle_refusal(handle)   # privacy_patch v2 sessions
    if refusal is not None:
        return refusal

    if not isinstance(handle, dict):
        return Response(status=404)

    session = load_session_from_ref(
        handle.get("sessionRef")
    )

    if session is not None:
        return json_response(session)

    return json_response(handle)


@app.route("/handles/<handle_id>/session", methods=["GET", "PUT"])
def mpsd_handle_session(handle_id):
    log_request("MPSD HANDLE SESSION")

    handle = load_handle(handle_id)
    if request.method == "GET":   # privacy_patch v2 sessions: the GET /handles/<id> rule (PUT checks the key itself)
        refusal = privacy_handle_refusal(handle)
        if refusal is not None:
            return refusal

    if not isinstance(handle, dict):
        return Response(status=404)

    ref = handle.get("sessionRef")
    if not isinstance(ref, dict):
        return Response(status=404)

    scid = ref.get("scid")
    template_name = ref.get("templateName")
    session_name = ref.get("name")

    if not scid or not template_name or not session_name:
        return Response(status=404)

    if not is_hd_scid(scid):
        return not_happy_dungeons(scid)

    # Update the same session file used by normal session handling.
    path = session_file(scid, template_name, session_name)

    if request.method == "GET":
        session = load_json(path)

        if not isinstance(session, dict):
            return Response(status=404)

        return json_response(session)

    incoming = request.get_json(silent=True) or {}
    return write_session_multi(scid, template_name, session_name, incoming)


# SmartMatch (momatch) tickets: see the multi-player SmartMatch above

@app.route(
    "/serviceconfigs/<scid>/hoppers/<hopper_name>",
    methods=["POST"]
)
def momatch_create_ticket(scid, hopper_name):
    log_request("MATCHMAKING CREATE TICKET")

    body = request.get_json(silent=True) or {}
    session_ref = body.get("ticketSessionRef")

    print("HOPPER:", hopper_name)
    print(
        "TICKET BODY:",
        json.dumps(body, separators=(",", ":"))
    )

    if not is_hd_scid(scid):
        return not_happy_dungeons(scid)

    if not isinstance(session_ref, dict):
        return json_response({
            "error": "missing_ticketSessionRef"
        }, 400)

    session = session_shared_from_ref(session_ref)   # (tuning_patch: create_multiplayer_ticket only reads it)

    if not isinstance(session, dict):
        return json_response({
            "error": "ticket_session_not_found",
            "ticketSessionRef": session_ref
        }, 404)

    # A ticket changes its session's matchmaking state and can move its players: only one of them may post it.
    if caller_member_key(session) is None:
        print("[MM] %s is not in %s and may not search from it - refused" % (
            gamertag_for(caller_xuid()) if verified_caller_xuid() else "a caller with no identity",
            session_ref.get("name")))
        return json_response({"error": "not_a_member_of_ticket_session"}, 403)

    return create_multiplayer_ticket(scid, hopper_name, body, session_ref, session)


@app.route(
    "/serviceconfigs/<scid>/hoppers/<hopper_name>/stats",
    methods=["GET"]
)
def momatch_hopper_stats(scid, hopper_name):
    log_request("MATCHMAKING HOPPER STATS")

    if not is_hd_scid(scid):
        return not_happy_dungeons(scid)

    with STATE_LOCK:
        population = sum(len(t["players"]) for t in MM_TICKETS.values()
                         if t["hopper"] == hopper_name and fps_stats_ok(t))   # (fps_split v3) the caller's pool
    return json_response({"name": hopper_name, "hopperName": hopper_name, "waitTime": 1,
                          "population": population})


@app.route(
    "/serviceconfigs/<scid>/hoppers/<hopper_name>/tickets/<ticket_id>",
    methods=["DELETE"]
)
def momatch_delete_ticket(scid, hopper_name, ticket_id):
    log_request("MATCHMAKING DELETE TICKET")

    if not is_hd_scid(scid):
        return not_happy_dungeons(scid)

    with STATE_LOCK:
        ticket = MM_TICKETS.get(ticket_id)
        if ticket is not None and not (verified_caller_xuid() and in_caller_block(ticket["owner"])):
            # Cancelling a search changes the ticket session's matchmaking state: only the ticket's owner may.
            print("[MM] %s may not cancel %s's search - refused" % (
                gamertag_for(caller_xuid()) if verified_caller_xuid() else "a caller with no identity",
                gamertag_for(ticket["owner"])))
            return json_response({"error": "not_your_ticket"}, 403)
        MM_TICKETS.pop(ticket_id, None)
        if ticket is not None and ticket["role"] == "host" and MM_LOBBIES.pop(ref_key(ticket["ref"]), None):
            print("[MM] %s stopped recruiting; lobby closed" % gamertag_for(ticket["owner"]))
        elif ticket is not None and lobby_kind(MM_LOBBIES.get(ref_key(ticket["ref"])) or {}) == "seeker" \
                and MM_LOBBIES.pop(ref_key(ticket["ref"]), None):
            print("[MM] %s stopped searching; its lobby is closed" % gamertag_for(ticket["owner"]))
    if ticket is not None:
        print("[MM] %s stopped searching" % gamertag_for(ticket["owner"]))
        update_matchmaking_state(ticket["ref"], "canceled", typical_wait=0)
    return Response(status=204)


# Happy Dungeons: Xbox marketplace inventory

# Happy Dungeons (title storage container hd201607) checks that the consumable
# inventory is non-empty before it marks the marketplace as available.
HD_TITLE_ID = 0x3751A2CA  # AppxManifest TitleId

# Consumable balances, one set per PLAYER and one entry per Happy Jewel pack (see JEWEL_PACKS above). A pack
# sits at 0 until it is bought; the game then sees a quantity of 1, credits the jewels to that player's save and
# consumes it back down to 0. Kept on disk so a purchase survives a restart of this server.
JEWEL_BALANCE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "data", "jewel_balances.json")
JEWEL_BALANCES = {}   # str(xuidBase) -> {product_id: quantity}


def load_jewel_balances():
    try:
        with open(JEWEL_BALANCE_FILE, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    # The file used to hold one shared set; it was the first PC's, so it becomes that player's (and moves
    # with them to their random XUIDs).
    if data and all(not isinstance(v, dict) for v in data.values()):
        data = {str(FIRST_XUID_BASE): data}
    known = {product_id for product_id, _ in JEWEL_PACKS}
    return {str(owner): {pid: int(q) for pid, q in packs.items() if pid in known}
            for owner, packs in data.items() if isinstance(packs, dict)}


def save_jewel_balances():
    try:
        os.makedirs(os.path.dirname(JEWEL_BALANCE_FILE), exist_ok=True)
        temp = JEWEL_BALANCE_FILE + ".tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(JEWEL_BALANCES, handle, indent=1, sort_keys=True)
        os.replace(temp, JEWEL_BALANCE_FILE)
    except OSError as error:
        print("COULD NOT SAVE JEWEL BALANCES:", error)


def jewel_owner():
    """Whose jewels this request is about: the caller, when they proved who they are. Installs with no identity
    yet (and callers whose secret does not match) share one purse, so nobody can spend another player's."""
    try:
        base = g.get("caller_base")
        # privacy_patch v2: only a caller who proved who they are has a purse of their own - a bare
        # X-WinDurango-Player header with a made-up base used to add a bucket (and rewrite the file) every call.
        if base and g.get("caller_verified") is True:
            return base
        return "anonymous"
    except RuntimeError:
        return "anonymous"


def balances_for(owner):
    """This player's balances, filled in with every pack at 0. Call with STATE_LOCK held."""
    balances = JEWEL_BALANCES.setdefault(str(owner), {})
    for product_id, _ in JEWEL_PACKS:
        balances.setdefault(product_id, 0)
    return balances


JEWEL_BALANCES.update(load_jewel_balances())


def load_moved():
    try:
        with open(MOVED_FILE, encoding="utf-8") as handle:
            return {str(k): str(v) for k, v in json.load(handle).items()}
    except (OSError, ValueError):
        return {}


MOVED_XUIDS = load_moved()   # old sequential xuidBase -> the random one it became


def migrate_sequential_identities():
    """Give every player still on an old sequential XUID block a random one, carrying their things over.

    Saves (data/<xuid>) are copied to the new XUIDs and the originals moved to data/_migrated; friends, friend
    requests and jewels are re-keyed. The player's PC learns the new block the next time it checks in
    (identity.py sends the old base with its secret and the claim answers with the new one).
    """
    moving = sorted(base for base in PLAYER_REGISTRY if in_sequential_window(base))
    if not moving:
        return
    renames = {}
    for old_base in moving:
        new_base = random_xuid_base()
        record = PLAYER_REGISTRY.pop(old_base)
        record["previousXuidBase"] = old_base
        PLAYER_REGISTRY[new_base] = record
        MOVED_XUIDS[old_base] = new_base
        for i in range(XUIDS_PER_PLAYER):
            renames[str(int(old_base) + i)] = str(int(new_base) + i)
        JEWEL_BALANCES[new_base] = JEWEL_BALANCES.pop(old_base, {})
        print("[IDENTITY] %s moved from sequential xuidBase %s to random xuidBase %s"
              % (record.get("gamertag"), old_base, new_base))

    for old_xuid, new_xuid in renames.items():
        source = os.path.join(DATA, old_xuid)
        if os.path.isdir(source):
            target = os.path.join(DATA, new_xuid)
            if not os.path.exists(target):
                shutil.copytree(source, target)
            os.makedirs(os.path.join(DATA, "_migrated"), exist_ok=True)
            shutil.move(source, os.path.join(DATA, "_migrated", old_xuid))

    def rekey(table):
        moved = {}
        for owner, others in table.items():
            moved[renames.get(owner, owner)] = sorted({renames.get(x, x) for x in others})
        table.clear()
        table.update(moved)

    rekey(FRIENDS)
    rekey(FRIEND_REQUESTS)
    save_friends()
    save_requests()
    save_jewel_balances()
    save_players()
    with open(MOVED_FILE, "w", encoding="utf-8") as handle:
        json.dump(MOVED_XUIDS, handle, indent=1, sort_keys=True)


migrate_sequential_identities()


def inventory_item(product_id, item_type, quantity=None):
    # XSAPI's InventoryItem deserializer (Microsoft.Xbox.Services.dll, .text 0x45a5d0) reads
    # url / state / itemType / productId / obtained / beginDate / endDate with its "required"
    # flag set, and obtained / beginDate / endDate through the datetime extractor. Leaving any
    # of them out makes the whole inventory listing fail to parse, so keep them all.
    item = {
        "url": f"https://inventory.xboxlive.com/users/me/inventory/{product_id}",
        "itemType": item_type,
        "productId": product_id,
        "titleId": HD_TITLE_ID,
        "containers": [],
        "rightsDuration": "Perpetual",
        "state": "Enabled",
        "obtained": "2016-07-01T00:00:00Z",
        "beginDate": "2016-07-01T00:00:00Z",
        "endDate": "9999-12-31T23:59:59Z",
        "purchasedDate": "2016-07-01T00:00:00Z",
        "startDate": "2016-07-01T00:00:00Z",
        "trial": False,
        "trialTimeRemaining": "00:00:00",
    }

    if quantity is not None:
        item["consumable"] = {
            "url": f"https://inventory.xboxlive.com/users/me/consumables/{product_id}",
            "quantity": quantity,
            "lastUpdated": utc_now(),
        }

    return item


@app.route("/users/me/inventory", methods=["GET"])
@app.route("/users/xuid(<xuid>)/inventory", methods=["GET"])
def marketplace_inventory(xuid=None):
    log_request("INVENTORY")

    item_type = request.args.get("itemType", "")
    print("ITEM TYPE:", item_type)

    items = []
    if item_type in ("", "GameConsumable"):
        with STATE_LOCK:
            for product_id, _ in JEWEL_PACKS:
                items.append(inventory_item(
                    product_id,
                    "GameConsumable",
                    balances_for(jewel_owner()).get(product_id, 0)
                ))

    owned = [i["productId"] for i in items if i["consumable"]["quantity"]]
    print(f"RETURNING {len(items)} consumables, unclaimed: {owned or 'none'}")

    return json_response({
        "pagingInfo": {
            "continuationToken": None,
            "totalItems": len(items)
        },
        "items": items
    })


@app.route("/users/me/inventory/<product_id>", methods=["GET"])
def marketplace_inventory_item(product_id):
    log_request("INVENTORY ITEM")
    with STATE_LOCK:
        quantity = balances_for(jewel_owner()).get(product_id, 0)
    return json_response(inventory_item(product_id, "GameConsumable", quantity))


@app.route("/users/me/consumables/<product_id>", methods=["GET", "POST"])
def marketplace_consume(product_id):
    log_request("CONSUME")

    body = request.get_json(silent=True) or {}
    remove = int(body.get("removeQuantity", body.get("RemoveQuantity", 0)) or 0)
    print("REMOVE QUANTITY:", remove, "BODY:", body)

    with STATE_LOCK:
        current = balances_for(jewel_owner()).get(product_id, 0)
        new_quantity = max(0, current - remove)
        balances_for(jewel_owner())[product_id] = new_quantity
        save_jewel_balances()

    jewels = JEWEL_BY_ID.get(product_id.lower())
    if jewels and remove:
        print(f"CLAIMED {jewels} Happy Jewels ({product_id})")

    transaction = body.get("transactionId") or body.get("TransactionId") or str(uuid.uuid4())
    return json_response({
        "url": f"https://inventory.xboxlive.com/users/me/inventory/{product_id}",
        "itemUrl": f"https://inventory.xboxlive.com/users/me/inventory/{product_id}",
        "newQuantity": new_quantity,
        "transactionId": transaction,
        "consumeTrackingId": transaction
    })


# Local helper, not an Xbox Live route: WinDurango calls this when the game asks the Store to
# purchase a jewel pack, since the real purchase UI is gone. It puts one unclaimed copy of the
# pack in the inventory; the game picks it up on its next inventory query.
@app.route("/windurango/purchase/<product_id>", methods=["POST", "GET"])
def windurango_purchase(product_id):
    log_request("PURCHASE")

    product_id = product_id.strip("{}").lower()
    jewels = JEWEL_BY_ID.get(product_id)
    if jewels is None:
        print("UNKNOWN PRODUCT:", product_id)
        return json_response({"granted": False, "reason": "unknown product"}, 404)

    with STATE_LOCK:
        for known, _ in JEWEL_PACKS:
            if known.lower() == product_id:
                product_id = known
                break
        balances_for(jewel_owner())[product_id] = balances_for(jewel_owner()).get(product_id, 0) + 1
        quantity = balances_for(jewel_owner())[product_id]
        save_jewel_balances()

    print(f"GRANTED {jewels} Happy Jewels ({product_id}), unclaimed copies: {quantity}")
    return json_response({"granted": True, "productId": product_id,
                          "jewels": jewels, "quantity": quantity})


# Leaderboards (empty boards so league/ranking views don't hit real Xbox Live)

def empty_leaderboard():
    return {
        "pagingInfo": {
            "continuationToken": None,
            "totalItems": 0
        },
        "leaderboardInfo": {
            "totalCount": 0,
            "columnDefinition": {
                "displayName": "",
                "statName": "",
                "type": "Integer"
            },
            "columns": []
        },
        "userList": []
    }


@app.route("/scids/<scid>/leaderboards/<path:name>", methods=["GET"])
@app.route("/users/xuid(<xuid>)/scids/<scid>/stats/<stat>/people/<path:rest>", methods=["GET"])
def leaderboards(scid, name=None, xuid=None, stat=None, rest=None):
    log_request("LEADERBOARD")
    print("SCID:", scid, "BOARD:", name or stat)
    return json_response(empty_leaderboard())


# Diagnostic catch-all

@app.route(
    "/",
    defaults={"path": ""},
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"]
)
@app.route(
    "/<path:path>",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"]
)
def catch_all(path):
    log_request("UNHANDLED")

    print("UNHANDLED PATH:", request.path)

    body = request.get_data(as_text=True)
    if body:
        print("BODY:", body)

    # Keep a persistent list of endpoints that still need emulating.
    try:
        with open(os.path.join(ROOT, "unhandled_requests.log"), "a", encoding="utf-8") as f:
            f.write("%s %s %s %s%s\n" % (
                utc_now(),
                request.headers.get("X-Original-Host", "?"),
                request.method,
                request.full_path.rstrip("?"),
                (" BODY=" + body[:500].replace("\n", " ")) if body else ""
            ))
    except OSError:
        pass

    return json_response({
        "error": "unhandled_local_xbox_endpoint",
        "path": request.path,
        "method": request.method
    }, 404)


# ---------------------------------------------------------------------------
# Peer relay ("WDR1").
#
# Lobbies go through this server, but the dungeon itself is peer to peer: every game sends UDP straight to the other
# players' secureDeviceAddress, which only works when the players can reach each other (a VPN such as Radmin).
# With the relay, WinDurango advertises a virtual address instead - fd57:4452:0:0 followed by the player's xuidBase
# - and sends whatever the game sends to such an address here, to this UDP port, which passes it on to that player.
# Caddy only carries HTTP, so the relay listens on the server's own public address (relayBind / relayPort).
#
# Packets, all integers big-endian:
#   HELLO  game -> relay   "WDR1" 01 | xuidBase (8) | unix time (8) | game port (2) | mac (16)          39 bytes
#          mac = HMAC-SHA256(key = the player's secret as its 32 hex characters,
#                            "WDR1-hello" | xuidBase | time | game port), first 16 bytes
#          Sent from the game's own peer socket (so its address is the NAT mapping replies come back through),
#          every 10 s while that socket is open. Never answered.
#   DATA   game -> relay   "WDR1" 03 | destination virtual address (16) | destination port (2) | payload
#          relay -> game   "WDR1" 04 | source virtual address (16) | source game port (2) | payload
# A DATA packet goes out the size it came in (only the header is rewritten), to a player who proved who they are
# with a HELLO in the last minute, and only when it came from such a player: the relay cannot be used to send more
# than it was sent, or to send anything to an address that did not ask for it.
# And only between players who share a session (a lobby, the dungeon's session; a reserved place counts): anyone can
# make an account and every xuid is on the players page, so without that any stranger could feed packets into any
# player's game - which only ever had to deal with the players it matched with.
RELAY_MAGIC = b"WDR1"
RELAY_HELLO = 0x01
RELAY_DATA_IN = 0x03             # a game's packet for another player
RELAY_DATA_OUT = 0x04            # the same packet, passed on to that player
RELAY_PREFIX = bytes.fromhex("fd57445200000000")   # the first 8 bytes of every virtual address
RELAY_HELLO_SIZE = 39
RELAY_HEADER_SIZE = 23
RELAY_MAX_PAYLOAD = 1400
RELAY_CLOCK_SKEW = 300           # seconds a HELLO's time may differ from this server's clock
RELAY_REGISTERED_SECONDS = 60    # a player is reachable this long after their last HELLO
RELAY_LISTED_SECONDS = 300       # GET /windurango/relay lists players heard from within this long
RELAY_SUMMARY_SECONDS = 60
RELAY_SEND_BUFFER = 1 << 20      # bytes waiting to go out before the relay drops instead of queueing more

# The relay runs on the RTA event loop; RELAY_LOCK guards its tables, which Flask threads read for the players page.
RELAY_LOCK = threading.Lock()
RELAY_PLAYERS = {}       # str(xuidBase) -> {"source": (ip, port) or None, "gamePort", "helloTime", "registered", "seen"}
RELAY_SOURCES = {}       # (ip, port) -> str(xuidBase) whose accepted HELLO last came from there
RELAY_TRAFFIC = {}       # (from xuidBase, to xuidBase) -> [packets, payload bytes] since the last summary
RELAY_DROPS = {}         # reason -> packets dropped since the last summary
RELAY_DROP_LOGGED = {}   # sender's ip -> when a drop from it was last logged
RELAY_DROP_LOG_LIMIT = 4096
RELAY_SESSIONS = {}      # "scid~template~name" -> frozenset of the xuidBases in it (reserved places too), 2 or more
RELAY_PEERS = {}         # str(xuidBase) -> frozenset of the other xuidBases it shares a session with
RELAY_TRANSPORT = None   # the relay's socket once it listens
RELAY_SUMMARY_TASK = None


# ---------------------------------------------------------------------------
# p2p_server_patch v2 - direct connections (Happy Dungeons private test build): the rendezvous on the relay socket.
#
# A private-test WinDurango (built with WD_PEER_DIRECT) asks this relay who it may talk to directly: PEERQ, sent from
# the game's own peer socket after each HELLO and every 2 s while it is in a session. The answer, PEERS, lists only
# the players who share a session with it: their public address as this relay sees it (the NAT mapping of their game
# socket), their LAN addresses only when both sit behind the same public IP, their VPN and IPv6 addresses when they
# share them, whether they host a session the two share (or the recipient hosts one), and a 32-byte key for the pair,
# encrypted with the recipient's own secret. A player who holds only a reserved place (matchmaking put it there, it
# has not come in yet) is listed without key or address, and sees the others that way, until it claims the place.
# PEERS-SAME says "nothing changed" (and echoes the game's clock for its round-trip measure). When a lobby changes,
# the relay pushes a new PEERS at once. The games then try the paths themselves and keep the relay as their fallback.
# Older games never send PEERQ and see no difference: nobody learns their address.
#
# Nothing larger than a PEERQ goes to an address the game has not proved it receives at: a HELLO's source is only
# what the packet says, and anyone with an account can sign one. So the first PEERQ of a run from an address gets a
# PEERS-SAME whose version has bit 63 set - a random challenge - and the address counts as proved when a later PEERQ
# of that run, from there, sends it back as its haveVersion (the game does that by itself: a PEERS-SAME for a version
# it does not have makes it ask again with that version). Only then does it get PEERS, and pushes. A game quiet for a
# minute, or one that moved, proves its address again. Pushes carry echoTxMs 0: they answer no PEERQ.
#
# Packets, all integers big-endian; cand = type (1) | address (16, IPv4 as ::ffff:a.b.c.d) | port (2), 19 bytes:
#   PEERQ       game -> relay  "WDR1" 10 | xuidBase (8) | unix ms (8) | instance (8) | have version (8) | tx ms (4)
#                              | flags (1) | n (1, <= 6) | cand * n | m (1, <= 8)
#                              | report * m (peer xuidBase (8), path (1), rtt ms (2), candidate type (1)) | mac (16)
#               mac = HMAC-SHA256(secret, "WDR1-peerq" | packet[5:-16]), first 16 bytes; 60 + 19n + 12m bytes
#   PEERS       relay -> game  "WDR1" 11 | version (8) | echo tx ms (4) | server flags (1) | your address (16)
#                              | your port (2) | n (1, <= 8) | per peer: xuidBase (8), game port (2), flags (1),
#                              key id (4), encrypted key (32), n (1, <= 6), cand * n | mac (16)
#               mac = HMAC-SHA256(secret, "WDR1-peers" | recipient xuidBase | recipient instance | packet[5:-16])
#               encrypted key = key XOR HMAC-SHA256(secret, "WDR1-pkey" | recipient xuidBase | recipient instance
#                                                   | version | peer xuidBase | key id)
#   PEERS-SAME  relay -> game  "WDR1" 12 | version (8) | echo tx ms (4) | mac (16, "WDR1-same", as PEERS)  33 bytes
# Peer flags: b0 takes direct connections, b1 hosts a session you share, b2 forwards, b3 same public IP as you, b4 its
# NAT changes ports, b5 YOU host a session you share with it (how a host's own game learns that it hosts); all 0 for a
# player listed without direct connections. Candidates other than the public one are never loopback or link-local;
# LAN ones are RFC 1918, IPv6 ones global unicast (the games' parser refuses anything else).
# The secret is the player's 32 hex characters, as for HELLO. A PEERQ is answered only when its mac is right, its
# time is within RELAY_CLOCK_SKEW of this server's and newer than the last one from that run of the game (a run is
# its instance; a new one may start over), and it comes from the address the player's last HELLO came from: a PEERQ
# never moves a player, and a copy of one gets nothing. At most 4 answers and pushes a second per player. A PEERS may
# be larger than the PEERQ it answers, which is why only a proved address gets one; PEERS-SAME is smaller than any
# PEERQ. server_config.json "peerDirect": false is the owner's kill switch - every game is told at once to use the
# relay only - and "peerDirectLan": false leaves out LAN and VPN addresses. Both are read again while the server runs.
# Nothing here logs a player's address: "[P2P]" lines mask it (73.12.x.x) and never show candidates, and a PEERS the
# relay cannot send (its send queue is full) is only counted.
import ipaddress   # (imported here: p2p_server_patch.py adds this part and touches nothing else)

RELAY_P2P_QUERY = 0x10           # PEERQ       game -> relay
RELAY_P2P_LIST = 0x11            # PEERS       relay -> game
RELAY_P2P_SAME = 0x12            # PEERS-SAME  relay -> game
RELAY_P2P_TAG_SIZE = 16
RELAY_P2P_QUERY_MIN = 60         # a PEERQ without candidates or reports
RELAY_P2P_QUERY_MAX = 270        # 6 candidates and 8 reports
RELAY_P2P_CANDIDATE_SIZE = 19
RELAY_P2P_MAX_CANDIDATES = 6
RELAY_P2P_REPORT_SIZE = 12
RELAY_P2P_MAX_REPORTS = 8
RELAY_P2P_MAX_PEERS = 8          # 8 peers with 6 candidates each: 1349 bytes, under RELAY_MAX_PAYLOAD
RELAY_P2P_KEY_SIZE = 32
RELAY_P2P_UNKNOWN_RTT = 0xFFFF
RELAY_P2P_CAPABLE_SECONDS = 60   # a game takes direct connections this long after its last PEERQ
RELAY_P2P_QUERY_GAP = 0.15       # a PEERQ this soon after the last one from the same player is dropped
RELAY_P2P_SENDS_PER_SECOND = 4   # PEERS and PEERS-SAME per player, answers and pushes together
RELAY_P2P_KEY_GRACE = 60         # a pair key is forgotten this long after the pair stops sharing a session
RELAY_P2P_RUN_SECONDS = 2 * RELAY_CLOCK_SKEW + 60   # a run (instance) is remembered this long: no replays
RELAY_P2P_RUNS_KEPT = 16
RELAY_P2P_LINES = 5              # "[P2P]" lines a minute per pair (path changes) and per player (its game)
RELAY_P2P_CONFIG_SECONDS = 1     # how often server_config.json is looked at for the switches
# PEERQ flags
RELAY_P2P_SHARE_DIRECT = 0x01    # b0 direct connections on: share my public address
RELAY_P2P_SHARE_LAN = 0x02       # b1 share my LAN and VPN addresses
RELAY_P2P_SHARE_IPV6 = 0x04      # b2 share my IPv6 address
RELAY_P2P_FORWARDS = 0x08        # b3 I pass packets between guests when I host
RELAY_P2P_NAT_PORTS = 0x10       # b4 my NAT changes the port per destination
RELAY_P2P_QUERY_FLAGS = 0x1F
# PEERS server flags, and flags per peer
RELAY_P2P_SERVER_DIRECT = 0x01   # b0 direct connections allowed (off: the kill switch)
RELAY_P2P_SERVER_LAN = 0x02      # b1 LAN addresses included for players behind the same public IP
RELAY_P2P_PEER_DIRECT = 0x01     # b0 takes direct connections (else: use the relay for this player)
RELAY_P2P_PEER_HOST = 0x02       # b1 hosts a session the two share
RELAY_P2P_PEER_FORWARDS = 0x04   # b2 passes packets between guests when hosting
RELAY_P2P_PEER_SAME_IP = 0x08    # b3 behind the same public IP as the recipient
RELAY_P2P_PEER_NAT_PORTS = 0x10  # b4 its NAT changes the port per destination
RELAY_P2P_PEER_YOU_HOST = 0x20   # b5 the recipient hosts a session the two share
RELAY_P2P_CHALLENGE = 1 << 63    # a PEERS-SAME version with this bit: "send this back to prove the address"
RELAY_P2P_PUSH_ECHO = 0          # the echo tx ms of a push (a game never sends tx ms 0)
# candidate types (5, peer-reflexive, is learned by the games and never sent)
RELAY_P2P_PUBLIC, RELAY_P2P_LAN, RELAY_P2P_VPN, RELAY_P2P_IPV6 = 1, 2, 3, 4
RELAY_P2P_MAPPED = bytes(10) + b"\xff\xff"   # the first 12 bytes of an IPv4 address as ::ffff:a.b.c.d
RELAY_P2P_LOOPBACK6 = bytes(15) + b"\x01"
RELAY_P2P_PATH_NAMES = {0: "relay", 1: "direct", 2: "via host", 3: "relay (sealed)"}
RELAY_P2P_CANDIDATE_NAMES = {1: "public", 2: "LAN", 3: "VPN", 4: "IPv6", 5: "peer-reflexive"}

# All under RELAY_LOCK.
RELAY_P2P = {}               # str(xuidBase) -> its game's last PEERQ, and what that game was last sent
RELAY_PAIR_KEYS = {}         # (low, high) str(xuidBase)s -> {"keyId", "key", "instances", "lastShared"}
RELAY_SESSION_HOSTS = {}     # session key -> str(xuidBase) hosting it (properties.system.host), sessions of 2 or more
RELAY_SESSION_RESERVED = {}  # session key -> frozenset of str(xuidBase)s that hold only a reserved place in it
RELAY_P2P_PENDING = set()    # str(xuidBase)s waiting for a PEERS push
RELAY_P2P_PATHS = {}         # (from, to) str(xuidBase)s -> the latest path report
RELAY_P2P_DROP_LOGGED = {}   # sender's ip -> when a dropped PEERQ from it was last logged
RELAY_LOOP = None            # the relay's event loop: pushes go out from it
RELAY_P2P_TIMER = None       # a push waiting for a player's send budget
RELAY_P2P_CONFIG_TASK = None
RELAY_P2P_CONFIG_STAMP = None    # server_config.json's modification time when the switches were last read


def relay_p2p_switch(name):
    """A server_config.json switch ("peerDirect", "peerDirectLan"): on unless false, 0, "false", "off" or "no"."""
    value = SERVER_CONFIG.get(name, True)
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "off", "no", "0", "")
    return bool(value)


def relay_p2p_state_text():
    if not relay_p2p_switch("peerDirect"):
        return "direct connections OFF (peerDirect is false): every game is told to use the relay only"
    return "direct connections on (LAN and VPN addresses %s)" % ("on" if relay_p2p_switch("peerDirectLan") else
                                                                "off: peerDirectLan is false")


def relay_p2p_mask(ip):
    """An address for the log without the part that says who it is: 73.12.x.x, 2001:db8:x:x."""
    try:
        address = ipaddress.ip_address(str(ip).split("%")[0])
    except ValueError:
        return "?"
    if address.version == 6 and address.ipv4_mapped:
        address = address.ipv4_mapped
    if address.version == 4:
        return "%d.%d.x.x" % (address.packed[0], address.packed[1])
    return "%x:%x:x:x" % (int.from_bytes(address.packed[0:2], "big"), int.from_bytes(address.packed[2:4], "big"))


def relay_p2p_address(ip):
    """The 16 bytes of an address as a datagram's source gives it (IPv4 as ::ffff:a.b.c.d), or None."""
    try:
        address = ipaddress.ip_address(str(ip).split("%")[0])
    except ValueError:
        return None
    return RELAY_P2P_MAPPED + address.packed if address.version == 4 else address.packed


def relay_p2p_valid_address(address):
    """Whether a game may be sent packets at this address: not unspecified, multicast, 0.x or a virtual address."""
    if len(address) != 16:
        return False
    if address[:12] == RELAY_P2P_MAPPED:
        return 0 < address[12] < 224
    return any(address) and address[0] != 0xFF and address[:8] != RELAY_PREFIX


def relay_p2p_valid_candidate(kind, address, port, kinds):
    """Whether a game may hand out this candidate: the public one (what this relay saw) needs only a usable address;
    LAN, VPN and IPv6 ones may not be loopback (127/8, ::1) or link-local (169.254/16, fe80::/10 - which can carry the
    network card's MAC), a LAN one must be RFC 1918 IPv4 and an IPv6 one global unicast (2000::/3). The same rules as
    the games' parser (WdPeerDirect)."""
    if kind not in kinds or port == 0 or not relay_p2p_valid_address(address):
        return False
    if kind == RELAY_P2P_PUBLIC:
        return True
    mapped = address[:12] == RELAY_P2P_MAPPED
    if mapped and (address[12] == 127 or address[12:14] == b"\xa9\xfe"):
        return False
    if not mapped and (address == RELAY_P2P_LOOPBACK6 or (address[0] == 0xFE and address[1] & 0xC0 == 0x80)):
        return False
    if kind == RELAY_P2P_LAN:
        return mapped and (address[12] == 10 or (address[12] == 172 and address[13] & 0xF0 == 16)
                           or address[12:14] == b"\xc0\xa8")
    if kind == RELAY_P2P_IPV6:
        return address[0] & 0xE0 == 0x20
    return True


def relay_p2p_mac(secret, label, prefix, packet):
    """The 16-byte mac of a PEERQ / PEERS / PEERS-SAME (its last 16 bytes are not part of what is signed)."""
    message = label + prefix + bytes(packet[5:len(packet) - RELAY_P2P_TAG_SIZE])
    return hmac.new(str(secret).encode("utf-8"), message, hashlib.sha256).digest()[:RELAY_P2P_TAG_SIZE]


def relay_p2p_pad(secret, recipient, instance, version, peer, key_id):
    """The 32 bytes a pair key is XORed with in one recipient's PEERS."""
    message = (b"WDR1-pkey" + int(recipient).to_bytes(8, "big") + int(instance).to_bytes(8, "big")
               + int(version).to_bytes(8, "big") + int(peer).to_bytes(8, "big") + int(key_id).to_bytes(4, "big"))
    return hmac.new(str(secret).encode("utf-8"), message, hashlib.sha256).digest()


def parse_relay_p2p_query(packet):
    """What a PEERQ says, or None when this relay does not take it. The mac is checked later (it needs the secret
    of the player the PEERQ names). The same rules as the games' own parser (WdPeerDirect)."""
    size = len(packet)
    if (not RELAY_P2P_QUERY_MIN <= size <= RELAY_P2P_QUERY_MAX
            or packet[:5] != RELAY_MAGIC + bytes([RELAY_P2P_QUERY])):
        return None
    flags, count = packet[41], packet[42]
    if flags & ~RELAY_P2P_QUERY_FLAGS or count > RELAY_P2P_MAX_CANDIDATES:
        return None
    at = 43
    if size < at + RELAY_P2P_CANDIDATE_SIZE * count + 1 + RELAY_P2P_TAG_SIZE:
        return None
    candidates = []
    for _ in range(count):
        kind, address, port = packet[at], bytes(packet[at + 1:at + 17]), int.from_bytes(packet[at + 17:at + 19], "big")
        at += RELAY_P2P_CANDIDATE_SIZE
        if (not relay_p2p_valid_candidate(kind, address, port, (RELAY_P2P_LAN, RELAY_P2P_VPN, RELAY_P2P_IPV6))
                or (kind in (RELAY_P2P_LAN, RELAY_P2P_VPN) and not flags & RELAY_P2P_SHARE_LAN)
                or (kind == RELAY_P2P_IPV6 and not flags & RELAY_P2P_SHARE_IPV6)
                or any((address, port) == (known[1], known[2]) for known in candidates)):
            return None
        candidates.append((kind, address, port))
    if candidates and not flags & RELAY_P2P_SHARE_DIRECT:
        return None
    reports_count = packet[at]
    at += 1
    if reports_count > RELAY_P2P_MAX_REPORTS or size != at + RELAY_P2P_REPORT_SIZE * reports_count + RELAY_P2P_TAG_SIZE:
        return None
    base = int.from_bytes(packet[5:13], "big")
    reports = {}
    for _ in range(reports_count):
        peer = int.from_bytes(packet[at:at + 8], "big")
        path, rtt, kind = packet[at + 8], int.from_bytes(packet[at + 9:at + 11], "big"), packet[at + 11]
        at += RELAY_P2P_REPORT_SIZE
        if path > 3 or kind > 5 or peer == base or str(peer) in reports:
            return None
        reports[str(peer)] = (path, rtt, kind)
    return {"base": base, "unixMs": int.from_bytes(packet[13:21], "big"),
            "instance": int.from_bytes(packet[21:29], "big"), "haveVersion": int.from_bytes(packet[29:37], "big"),
            "txMs": int.from_bytes(packet[37:41], "big"), "flags": flags, "candidates": tuple(candidates),
            "reports": reports}


def build_relay_p2p_list(secret, recipient, instance, version, echo, server_flags, address, port, peers):
    """A signed PEERS for one run of one player's game. peers: (xuidBase, game port, flags, key id, pair key in
    clear, candidates); a peer without direct connections has flags 0, key id 0, key None and no candidates."""
    out = bytearray(RELAY_MAGIC + bytes([RELAY_P2P_LIST]))
    out += (int(version).to_bytes(8, "big") + int(echo).to_bytes(4, "big") + bytes([server_flags]) + bytes(address)
            + int(port).to_bytes(2, "big") + bytes([len(peers)]))
    for peer, game_port, flags, key_id, key, candidates in peers:
        if key is None:
            wire = bytes(RELAY_P2P_KEY_SIZE)
        else:
            wire = bytes(a ^ b for a, b in zip(key, relay_p2p_pad(secret, recipient, instance, version, peer, key_id)))
        out += (int(peer).to_bytes(8, "big") + int(game_port).to_bytes(2, "big") + bytes([flags])
                + int(key_id).to_bytes(4, "big") + wire + bytes([len(candidates)]))
        for kind, candidate, candidate_port in candidates:
            out += bytes([kind]) + bytes(candidate) + int(candidate_port).to_bytes(2, "big")
    out += bytes(RELAY_P2P_TAG_SIZE)
    prefix = int(recipient).to_bytes(8, "big") + int(instance).to_bytes(8, "big")
    return bytes(out[:-RELAY_P2P_TAG_SIZE]) + relay_p2p_mac(secret, b"WDR1-peers", prefix, out)


def build_relay_p2p_same(secret, recipient, instance, version, echo):
    """A signed PEERS-SAME: the recipient has the current version."""
    packet = (RELAY_MAGIC + bytes([RELAY_P2P_SAME]) + int(version).to_bytes(8, "big") + int(echo).to_bytes(4, "big")
              + bytes(RELAY_P2P_TAG_SIZE))
    prefix = int(recipient).to_bytes(8, "big") + int(instance).to_bytes(8, "big")
    return packet[:-RELAY_P2P_TAG_SIZE] + relay_p2p_mac(secret, b"WDR1-same", prefix, packet)


def relay_p2p_drop(source, reason, now, detail="", quiet=False):
    """Drop a PEERQ: never answered, counted in the relay's minute summary, and (unless quiet: the rate limits,
    which a normal game can touch) logged at most once a minute per sender, its address masked."""
    with RELAY_LOCK:
        RELAY_DROPS[reason] = RELAY_DROPS.get(reason, 0) + 1
        if quiet:
            return None
        logged = RELAY_P2P_DROP_LOGGED.get(source[0])
        if logged is not None and now - logged < 60:
            return None
        if logged is None and len(RELAY_P2P_DROP_LOGGED) >= RELAY_DROP_LOG_LIMIT:
            return None
        RELAY_P2P_DROP_LOGGED[source[0]] = now
    print("[P2P] dropped a PEERQ from %s: %s%s" % (relay_p2p_mask(source[0]), reason, detail))
    return None


def relay_p2p_count(reason):
    """A PEERS / PEERS-SAME that is not sent, counted in the relay's minute summary. Never logged on its own line: the
    address it was for is nobody's business in the log."""
    with RELAY_LOCK:
        RELAY_DROPS[reason] = RELAY_DROPS.get(reason, 0) + 1


def relay_p2p_queue_full():
    transport = RELAY_TRANSPORT
    return transport is not None and transport.get_write_buffer_size() > RELAY_SEND_BUFFER


def relay_p2p_proved(me):
    """(Under RELAY_LOCK) whether this game proved it receives where its PEERQs come from now (this run)."""
    return me["checked"] is not None and me["checked"] == (me["instance"], me["source"])


def relay_p2p_listening(base, now):
    """(Under RELAY_LOCK) the game's last PEERQ when it is under a minute old, came from where that player's HELLOs
    come from now, and that address is proved (pushes go there), else None."""
    query = RELAY_P2P.get(base) if base else None
    player = RELAY_PLAYERS.get(base) if base else None
    if (query is None or now - query["lastQuery"] > RELAY_P2P_CAPABLE_SECONDS or not relay_reachable(player, now)
            or player["source"] != query["source"] or not relay_p2p_proved(query)):
        return None
    return query


def relay_p2p_capable(base, now):
    """(Under RELAY_LOCK) whether this player's game takes direct connections now."""
    query = relay_p2p_listening(base, now)
    return bool(query and query["flags"] & RELAY_P2P_SHARE_DIRECT)


def relay_p2p_advert(base, now):
    """(Under RELAY_LOCK) what this player's peers see of its game in their PEERS, apart from its relay address."""
    query = relay_p2p_listening(base, now)
    return (query["instance"], query["flags"], query["candidates"]) if query else None


def relay_p2p_where(base, now):
    player = RELAY_PLAYERS.get(base) if base else None
    return (player["source"], player["gamePort"], relay_reachable(player, now)) if player else None


def relay_p2p_host_of(a, b):
    """(Under RELAY_LOCK) the host of a session a and b share (another player first: the one who can pass packets
    between them), or None."""
    found = None
    for key, members in RELAY_SESSIONS.items():
        host = RELAY_SESSION_HOSTS.get(key)
        if host and a in members and b in members:
            if host not in (a, b):
                return host
            found = found or host
    return found


def relay_p2p_hosts(host, other):
    """(Under RELAY_LOCK) whether host hosts a session that other is in as well."""
    return any(RELAY_SESSION_HOSTS.get(key) == host and host in members and other in members
               for key, members in RELAY_SESSIONS.items())


def relay_p2p_joined_together(a, b):
    """(Under RELAY_LOCK) whether a and b share a session in which neither holds only a reserved place. A place
    matchmaking reserved lets the relay carry their packets already (RELAY_PEERS counts it), but their addresses and
    a key wait until the player comes in: a reservation that lapses shares nothing."""
    return any(a in members and b in members and a not in RELAY_SESSION_RESERVED.get(key, ())
               and b not in RELAY_SESSION_RESERVED.get(key, ()) for key, members in RELAY_SESSIONS.items())


def relay_p2p_pair_key(a, b, now, touched):
    """(Under RELAY_LOCK) the key two direct-capable players share: made when first needed, made again when either
    starts a new run (fresh replay windows on both sides). Its key id is random, never 0 and never one either player
    has for another pair (or had for this one). Whoever of the two gets a new key goes into touched."""
    pair = tuple(sorted((a, b), key=int))
    instances = (RELAY_P2P[pair[0]]["instance"], RELAY_P2P[pair[1]]["instance"])
    record = RELAY_PAIR_KEYS.get(pair)
    if record is None or record["instances"] != instances:
        used = {known["keyId"] for together, known in RELAY_PAIR_KEYS.items() if a in together or b in together}
        key_id = 0
        while key_id == 0 or key_id in used:
            key_id = secrets.randbits(32)
        record = RELAY_PAIR_KEYS[pair] = {"keyId": key_id, "key": secrets.token_bytes(RELAY_P2P_KEY_SIZE),
                                          "instances": instances, "lastShared": now}
        touched.update(pair)
    record["lastShared"] = now
    return record


def relay_p2p_content(base, now, touched):
    """(Under RELAY_LOCK) what this player's PEERS says now: (server flags, its address, its port, peers).
    Only players who share a session with it, at most RELAY_P2P_MAX_PEERS (those it may connect to directly first).
    A peer gets a key and addresses only when both games take direct connections and both have come into a session
    they share (not only a reserved place)."""
    player = RELAY_PLAYERS[base]
    mine = relay_p2p_address(player["source"][0]) or bytes(16)
    if not relay_p2p_switch("peerDirect"):
        return 0, mine, player["source"][1], ()
    lan = relay_p2p_switch("peerDirectLan")
    direct = bool(RELAY_P2P[base]["flags"] & RELAY_P2P_SHARE_DIRECT)

    def reachable(other):
        return direct and relay_p2p_capable(other, now) and relay_p2p_joined_together(base, other)

    others = sorted(RELAY_PEERS.get(base, ()), key=lambda other: (not reachable(other), int(other)))
    peers = []
    for other in sorted(others[:RELAY_P2P_MAX_PEERS], key=int):
        record = RELAY_PLAYERS.get(other)
        game_port = record["gamePort"] if record else 0
        if not reachable(other):
            peers.append((int(other), game_port, 0, 0, None, ()))
            continue
        query = RELAY_P2P[other]
        key = relay_p2p_pair_key(base, other, now, touched)
        public = relay_p2p_address(record["source"][0])
        same_ip = public is not None and public == mine
        flags = (RELAY_P2P_PEER_DIRECT
                 | (RELAY_P2P_PEER_HOST if relay_p2p_hosts(other, base) else 0)
                 | (RELAY_P2P_PEER_FORWARDS if query["flags"] & RELAY_P2P_FORWARDS else 0)
                 | (RELAY_P2P_PEER_SAME_IP if same_ip else 0)
                 | (RELAY_P2P_PEER_NAT_PORTS if query["flags"] & RELAY_P2P_NAT_PORTS else 0)
                 | (RELAY_P2P_PEER_YOU_HOST if relay_p2p_hosts(base, other) else 0))
        candidates = []
        if public is not None and relay_p2p_valid_candidate(RELAY_P2P_PUBLIC, public, record["source"][1],
                                                            (RELAY_P2P_PUBLIC,)):
            candidates.append((RELAY_P2P_PUBLIC, public, record["source"][1]))
        for kind, address, port in query["candidates"]:
            if kind in (RELAY_P2P_LAN, RELAY_P2P_VPN) and not lan:
                continue   # the owner turned LAN and VPN addresses off
            if kind == RELAY_P2P_LAN and not same_ip:
                continue   # a LAN address only reaches players behind the same public IP
            if any((address, port) == (known[1], known[2]) for known in candidates):
                continue
            candidates.append((kind, address, port))
        peers.append((int(other), game_port, flags, key["keyId"], key["key"],
                      tuple(candidates[:RELAY_P2P_MAX_CANDIDATES])))
    server_flags = RELAY_P2P_SERVER_DIRECT | (RELAY_P2P_SERVER_LAN if lan else 0)
    return server_flags, mine, player["source"][1], tuple(peers)


def relay_p2p_current(base, now, touched):
    """(Under RELAY_LOCK) this player's PEERS content and its version: server unix ms of the last change, strictly
    increasing."""
    me = RELAY_P2P[base]
    content = relay_p2p_content(base, now, touched)
    if content != me["content"]:
        me["content"] = content
        me["version"] = max(int(now * 1000), me["version"] + 1)
    return content, me["version"]


def relay_p2p_budget(me, now):
    """(Under RELAY_LOCK) whether this player may be sent one more PEERS / PEERS-SAME now."""
    me["sends"] = [when for when in me["sends"] if 0 <= now - when < 1.0]
    return len(me["sends"]) < RELAY_P2P_SENDS_PER_SECOND


def relay_p2p_packet(base, secret, me, content, version, echo):
    server_flags, address, port, peers = content
    return build_relay_p2p_list(secret, base, me["instance"], version, echo, server_flags, address, port, peers)


def relay_p2p_answer(base, secret, have, echo, now, touched):
    """(Under RELAY_LOCK) the answer to a player's PEERQ: the challenge (a 33-byte PEERS-SAME) while its address is
    not proved; then PEERS-SAME when it has the current version, else PEERS. With direct connections off it is a
    PEERS (an empty one, smaller than any PEERQ)."""
    me = RELAY_P2P[base]
    me["sends"].append(now)
    if not relay_p2p_proved(me):
        return build_relay_p2p_same(secret, base, me["instance"], me["challenge"][1], echo)
    content, version = relay_p2p_current(base, now, touched)
    me["sent"] = version
    if have == version and content[0] & RELAY_P2P_SERVER_DIRECT:
        return build_relay_p2p_same(secret, base, me["instance"], version, echo)
    return relay_p2p_packet(base, secret, me, content, version, echo)


def relay_p2p_path_text(sender, peer, report, short=False):
    """"direct (public) 23 ms (relay 71 ms)", "via Mako 48 ms", "relay 90 ms" (short: "direct 23 ms")."""
    path, rtt = report["path"], report["rtt"]
    if path == 1:
        kind = RELAY_P2P_CANDIDATE_NAMES.get(report["candType"])
        text = "direct" if short or not kind else "direct (%s)" % kind
    elif path == 2:
        host = relay_p2p_host_of(sender, peer)
        text = "via %s" % (relay_name(host) if host else "the host")
    else:
        text = RELAY_P2P_PATH_NAMES[path]
    text += " %d ms" % rtt if rtt != RELAY_P2P_UNKNOWN_RTT else " (no round trip yet)"
    if not short and path != 0 and report["relayRtt"] != RELAY_P2P_UNKNOWN_RTT:
        text += " (relay %d ms)" % report["relayRtt"]
    return text


def relay_p2p_reports(base, reports, now):
    """(Under RELAY_LOCK) remember what a game says about its paths to the players it shares a session with (reports
    about anyone else are ignored); the log lines for pairs whose path changed, at most RELAY_P2P_LINES a minute
    each. Every pair starts on the relay, so a first report of the relay is not a change."""
    lines = []
    for other, (path, rtt, kind) in sorted(reports.items()):
        if other not in RELAY_PEERS.get(base, ()):
            continue
        known = RELAY_P2P_PATHS.get((base, other))
        if known is None:
            known = RELAY_P2P_PATHS[(base, other)] = {"path": 0, "candType": 0, "rtt": RELAY_P2P_UNKNOWN_RTT,
                                                      "relayRtt": RELAY_P2P_UNKNOWN_RTT, "logged": []}
        changed = (path, kind) != (known["path"], known["candType"])
        if path == 0 and rtt != RELAY_P2P_UNKNOWN_RTT:
            known["relayRtt"] = rtt
        known.update(path=path, candType=kind, rtt=rtt, when=now)
        if changed:
            known["logged"] = [when for when in known["logged"] if now - when < 60]
            if len(known["logged"]) < RELAY_P2P_LINES:
                known["logged"].append(now)
                lines.append("[P2P] %s -> %s %s" % (relay_name(base), relay_name(other),
                                                     relay_p2p_path_text(base, other, known)))
    return lines


def relay_p2p_game_text(base, flags):
    if not flags & RELAY_P2P_SHARE_DIRECT:
        return "[P2P] %s's game asks without direct connections (it stays on the relay)" % relay_name(base)
    shares = (["its public address"] + (["LAN/VPN"] if flags & RELAY_P2P_SHARE_LAN else [])
              + (["IPv6"] if flags & RELAY_P2P_SHARE_IPV6 else []))
    return "[P2P] %s's game takes direct connections (shares %s%s%s)" % (
        relay_name(base), ", ".join(shares),
        "; passes packets between guests when hosting" if flags & RELAY_P2P_FORWARDS else "",
        "; its NAT changes the port per destination" if flags & RELAY_P2P_NAT_PORTS else "")


def relay_p2p_forget_runs(me, now):
    runs = me["runs"]
    for instance, (_, seen) in list(runs.items()):
        if instance != me["instance"] and now - seen > RELAY_P2P_RUN_SECONDS:
            del runs[instance]
    while len(runs) > RELAY_P2P_RUNS_KEPT:
        del runs[min((i for i in runs if i != me["instance"]), key=lambda i: runs[i][1])]


def relay_p2p_query(packet, source, now):
    """(PEERS or PEERS-SAME, address) answering a game's PEERQ, or None when the PEERQ is dropped: malformed, from
    an unknown player, a time too far off, a bad mac, not newer than the last one from that run, not from the
    player's relay address (a HELLO in the last minute), under RELAY_P2P_QUERY_GAP after the last one, or past
    RELAY_P2P_SENDS_PER_SECOND. A valid PEERQ that is only over the send budget still counts (its game's addresses
    and reports are kept). Until the run proved its address (it sent the challenge back), the answer is the
    challenge."""
    query = parse_relay_p2p_query(packet)
    if query is None:
        return relay_p2p_drop(source, "malformed PEERQ", now)
    base = str(query["base"])
    record = PLAYER_REGISTRY.get(base)
    secret = record.get("secret") if isinstance(record, dict) else None
    if not secret:
        return relay_p2p_drop(source, "PEERQ from an unknown player", now, " (%s)" % base)
    if abs(query["unixMs"] / 1000.0 - now) > RELAY_CLOCK_SKEW:
        return relay_p2p_drop(source, "PEERQ with a time too far from this server's", now,
                              " (%s, off by %d s - is that PC's clock right?)" % (
                                  relay_name(base), query["unixMs"] // 1000 - int(now)))
    if not hmac.compare_digest(bytes(packet[-RELAY_P2P_TAG_SIZE:]), relay_p2p_mac(secret, b"WDR1-peerq", b"", packet)):
        return relay_p2p_drop(source, "PEERQ with a bad mac", now, " (claims to be %s)" % relay_name(base))

    touched, lines, reason, quiet, answer = set(), [], None, False, None
    with RELAY_LOCK:
        me = RELAY_P2P.get(base)
        run = me["runs"].get(query["instance"]) if me else None
        player = RELAY_PLAYERS.get(base)
        if run is not None and query["unixMs"] <= run[0]:
            reason = "PEERQ older than the last one, or a copy of it"
        elif not relay_reachable(player, now):
            reason = "PEERQ from a player without a HELLO"
        elif player["source"] != source:
            reason = "PEERQ from an address that is not the player's relay address"
        elif me is not None and 0 <= now - me["lastQuery"] < RELAY_P2P_QUERY_GAP:
            reason, quiet = "PEERQ under 150 ms after the last one", True
        else:
            advert = relay_p2p_advert(base, now)   # what the peers' lists showed of this game until now
            if me is None:
                me = RELAY_P2P[base] = {"runs": {}, "sends": [], "content": None, "version": 0, "sent": None,
                                        "logged": [], "instance": None, "flags": None, "checked": None,
                                        "challenge": None}
            elif now - me["lastQuery"] > RELAY_P2P_CAPABLE_SECONDS:
                me["checked"] = None   # quiet for a minute: its address is proved again
            game = (me["instance"], me["flags"])
            me.update(instance=query["instance"], unixMs=query["unixMs"], flags=query["flags"],
                      candidates=query["candidates"], txMs=query["txMs"], lastQuery=now, source=source)
            me["runs"][query["instance"]] = (query["unixMs"], now)
            relay_p2p_forget_runs(me, now)
            run = (query["instance"], source)
            if me["checked"] != run:
                challenge = me["challenge"]
                if challenge is not None and challenge[0] == run and query["haveVersion"] == challenge[1]:
                    me["checked"], me["challenge"] = run, None   # the challenge reached it there: proved
                elif challenge is None or challenge[0] != run:
                    me["challenge"] = (run, RELAY_P2P_CHALLENGE | secrets.randbits(63))
            if relay_p2p_advert(base, now) != advert:
                touched.update(RELAY_PEERS.get(base, ()))   # their lists show this game: push them a new one
            if game != (query["instance"], query["flags"]):   # a new run of the game, or other flags: say so
                me["logged"] = [when for when in me["logged"] if now - when < 60]
                if len(me["logged"]) < RELAY_P2P_LINES:
                    me["logged"].append(now)
                    lines.append(relay_p2p_game_text(base, query["flags"]))
            lines += relay_p2p_reports(base, query["reports"], now)
            if relay_p2p_budget(me, now):
                answer = relay_p2p_answer(base, secret, query["haveVersion"], query["txMs"], now, touched)
            else:
                reason, quiet = "PEERQ answers over 4 a second", True
    for line in lines:
        print(line)
    relay_p2p_schedule(touched - {base})
    if reason:
        return relay_p2p_drop(source, reason, now, " (%s)" % relay_name(base), quiet=quiet)
    if relay_p2p_queue_full():   # counted here, so the relay's own drop line (which shows the address) never runs
        relay_p2p_count("PEERS answer: send queue full")
        return None
    return answer, source


def relay_p2p_hello(packet, source, now):
    """relay_hello, unchanged; then a push to the players whose lists changed because a player moved (a new relay
    address or game port, or back on the relay after a minute without HELLO) or lost its address to another. A game
    whose relay address is no longer the one it proved proves the new one (or a return to the old one) again."""
    parsed = parse_relay_hello(packet)
    base = str(parsed[0]) if parsed else None
    with RELAY_LOCK:
        before = relay_p2p_where(base, now)
        holder = RELAY_SOURCES.get(source)
        held = relay_p2p_where(holder, now)
    result = relay_hello(packet, source, now)
    touched = set()
    with RELAY_LOCK:
        for who in {base, holder} - {None}:
            me, player = RELAY_P2P.get(who), RELAY_PLAYERS.get(who)
            if me is not None and me["checked"] is not None and (player is None
                                                                  or player["source"] != me["checked"][1]):
                me["checked"] = None
        if base and relay_p2p_where(base, now) != before:
            touched.update(RELAY_PEERS.get(base, ()))
            touched.add(base)
        if holder and holder != base and relay_p2p_where(holder, now) != held:
            touched.update(RELAY_PEERS.get(holder, ()))
    relay_p2p_schedule(touched)
    return result


def relay_p2p_schedule(bases):
    """A fresh PEERS for these players (the ones whose game asked in the last minute, and only when their list
    changed), sent from the relay's loop. Safe from any thread (Flask's session writes call it)."""
    if not bases:
        return
    with RELAY_LOCK:
        RELAY_P2P_PENDING.update(bases)
    loop = RELAY_LOOP
    if loop is None:
        return   # not listening yet: nobody has asked
    try:
        loop.call_soon_threadsafe(relay_p2p_push)
    except RuntimeError:   # the loop is closed: the server is stopping
        pass


def relay_p2p_push(bases=()):
    """(On the relay's loop) send every pending player whose list changed since it was last sent one a PEERS, at
    most RELAY_P2P_SENDS_PER_SECOND a second each; one over that waits for its turn. Only to a game that proved its
    address (relay_p2p_listening), and with echo tx ms 0: a push answers no PEERQ."""
    global RELAY_P2P_TIMER
    now = time.time()
    sends, later = [], set()
    with RELAY_LOCK:
        RELAY_P2P_PENDING.update(bases)
        for _ in range(8):   # a key made for one player changes the other's list too: a round or two settles it
            if not RELAY_P2P_PENDING:
                break
            pending = sorted(RELAY_P2P_PENDING, key=int)
            RELAY_P2P_PENDING.clear()
            touched = set()
            for base in pending:
                me = relay_p2p_listening(base, now)
                secret = (PLAYER_REGISTRY.get(base) or {}).get("secret")
                if me is None or not secret:
                    continue
                content, version = relay_p2p_current(base, now, touched)
                if version == me["sent"]:
                    continue
                if not relay_p2p_budget(me, now):
                    later.add(base)
                    continue
                me["sends"].append(now)
                me["sent"] = version
                sends.append((relay_p2p_packet(base, secret, me, content, version, RELAY_P2P_PUSH_ECHO),
                              RELAY_PLAYERS[base]["source"]))
            RELAY_P2P_PENDING.update(touched - later)
        wait = None
        if later:
            RELAY_P2P_PENDING.update(later)
            wait = min(1.0 - (now - RELAY_P2P[base]["sends"][0]) for base in later) + 0.01
        elif RELAY_P2P_PENDING and RELAY_LOOP is not None:
            RELAY_LOOP.call_soon_threadsafe(relay_p2p_push)
    transport = RELAY_TRANSPORT
    for packet, address in sends:
        if transport is None:
            break
        if relay_p2p_queue_full():
            relay_p2p_count("PEERS push: send queue full")   # the game's next PEERQ (2 s) gets the list
            continue
        transport.sendto(packet, address)
    if wait is not None and RELAY_P2P_TIMER is None and RELAY_LOOP is not None:
        RELAY_P2P_TIMER = RELAY_LOOP.call_later(max(0.01, wait), relay_p2p_timer)
    return len(sends)


def relay_p2p_timer():
    global RELAY_P2P_TIMER
    RELAY_P2P_TIMER = None
    relay_p2p_push()


def relay_p2p_peers_changed(before, now):
    """(Under RELAY_LOCK, from relay_set_session) the players whose peers changed. Pair keys note when their pair
    last shared a session; a pair that meets again after its key was due to be forgotten gets a new one."""
    changed = {base for base in set(before) | set(RELAY_PEERS) if before.get(base) != RELAY_PEERS.get(base)}
    for pair, record in list(RELAY_PAIR_KEYS.items()):
        if pair[1] in before.get(pair[0], ()):
            record["lastShared"] = now
        elif pair[1] in RELAY_PEERS.get(pair[0], ()) and now - record["lastShared"] > RELAY_P2P_KEY_GRACE:
            del RELAY_PAIR_KEYS[pair]
    relay_p2p_forget_sessions()
    return changed


def relay_p2p_forget_sessions():
    """(Under RELAY_LOCK, from relay_set_session only) the host of sessions that are gone (or down to one player), and
    reservations of players no longer in their session. Only relay_set_session may do this: it runs under the
    server's STATE_LOCK right after the session hook, so nothing can come between the two - the relay's loop (the
    minute sweep) could, and would drop a new session's reservations before relay_set_session added it."""
    for key in list(RELAY_SESSION_HOSTS):
        if key not in RELAY_SESSIONS:
            del RELAY_SESSION_HOSTS[key]
    for key, reserved in list(RELAY_SESSION_RESERVED.items()):
        left = reserved & RELAY_SESSIONS.get(key, frozenset())
        if left:
            RELAY_SESSION_RESERVED[key] = left
        else:
            del RELAY_SESSION_RESERVED[key]


def relay_p2p_session_changed(key, bases, reserved, host):
    """(relay_session_changed / relay_session_removed, BEFORE relay_set_session) remember who hosts a session of two
    or more players and who in it holds only a reserved place. Returns the players to push to once relay_set_session
    has run (relay_p2p_schedule): everyone who was or is in it, when its members, host or reservations changed. A
    player's list can change while its set of peers stays the same - it shares another session with them - because
    the host flags and 'come in' are per session.
    Until relay_set_session has run, RELAY_SESSIONS still holds the session as it was, and a push may run on the
    relay's loop meanwhile. So a reserved player on its way out (its reservation lapsed) stays marked reserved until
    relay_set_session has taken it out (relay_p2p_forget_sessions), and a session that is gone or down to one player
    keeps its records until then: no key or address is handed out early, not even for that moment."""
    group = len(bases) > 1
    host = host if group and host in bases else None
    reserved = frozenset(reserved) & bases if group else frozenset()
    with RELAY_LOCK:
        members = RELAY_SESSIONS.get(key, frozenset())
        known = RELAY_SESSION_RESERVED.get(key, frozenset())
        before = (RELAY_SESSION_HOSTS.get(key), members, known)
        if before == (host, bases if group else frozenset(), reserved):
            return set()
        if group:
            if host is None:
                RELAY_SESSION_HOSTS.pop(key, None)
            else:
                RELAY_SESSION_HOSTS[key] = host
            keep = reserved | (known - bases)   # (and the leaving ones, until relay_set_session took them out)
            if keep:
                RELAY_SESSION_RESERVED[key] = keep
            else:
                RELAY_SESSION_RESERVED.pop(key, None)
    return set(members | bases)


def relay_p2p_sweep(now):
    """(Under RELAY_LOCK, once a minute) forget pair keys RELAY_P2P_KEY_GRACE after their pair stopped sharing a
    session, games not heard from for RELAY_P2P_RUN_SECONDS, and old path reports and drop-log entries."""
    for pair, record in list(RELAY_PAIR_KEYS.items()):
        if pair[1] in RELAY_PEERS.get(pair[0], ()):
            record["lastShared"] = now
        elif now - record["lastShared"] > RELAY_P2P_KEY_GRACE:
            del RELAY_PAIR_KEYS[pair]
    for base, me in list(RELAY_P2P.items()):
        if now - me["lastQuery"] > RELAY_P2P_RUN_SECONDS:
            del RELAY_P2P[base]
    for pair, report in list(RELAY_P2P_PATHS.items()):
        if now - report["when"] > RELAY_P2P_RUN_SECONDS:
            del RELAY_P2P_PATHS[pair]
    for ip, logged in list(RELAY_P2P_DROP_LOGGED.items()):
        if now - logged >= 60:
            del RELAY_P2P_DROP_LOGGED[ip]
    # (not the session records: relay_set_session keeps those tidy - see relay_p2p_forget_sessions)


def relay_p2p_minute(now=None):
    """Once a minute, from relay_summaries: relay_p2p_sweep, and "[P2P] last minute: Mako->Porty direct 23 ms; ..."
    for the paths the games reported in the last minute (returned too; None when none did)."""
    now = time.time() if now is None else now
    try:
        with RELAY_LOCK:
            relay_p2p_sweep(now)
            parts = []
            for (sender, peer), report in sorted(RELAY_P2P_PATHS.items(), key=lambda item: (
                    relay_name(item[0][0]).lower(), relay_name(item[0][1]).lower())):
                if now - report["when"] > 60:
                    continue
                text = "%s->%s %s" % (relay_name(sender), relay_name(peer),
                                      relay_p2p_path_text(sender, peer, report, short=True))
                without = [relay_name(b) for b in (sender, peer) if not relay_p2p_capable(b, now)]
                if without:
                    text += " (%s)" % "; ".join("%s has no direct connections" % name for name in without)
                parts.append(text)
    except Exception as error:   # the relay's own summary goes on whatever happens here
        print("[P2P] error in the minute summary: %r" % (error,))
        return None
    if not parts:
        return None
    line = "[P2P] last minute: " + "; ".join(parts)
    if not relay_p2p_switch("peerDirect"):
        line += " (direct connections are off: peerDirect is false)"
    print(line)
    return line


def relay_p2p_set_switches(direct, lan):
    """Turn direct connections (and LAN/VPN addresses) on or off while the server runs. Every game that asked in the
    last minute gets its new PEERS at once. Off also forgets every pair key, so on again hands out new ones.
    Returns whether anything changed."""
    with RELAY_LOCK:
        before = (relay_p2p_switch("peerDirect"), relay_p2p_switch("peerDirectLan"))
        SERVER_CONFIG["peerDirect"], SERVER_CONFIG["peerDirectLan"] = direct, lan
        after = (relay_p2p_switch("peerDirect"), relay_p2p_switch("peerDirectLan"))
        if after == before:
            return False
        if not after[0]:
            RELAY_PAIR_KEYS.clear()
        everyone = set(RELAY_P2P)
    print("[P2P] server_config.json: " + relay_p2p_state_text())
    relay_p2p_schedule(everyone)
    return True


async def relay_p2p_watch_config():
    """Read "peerDirect" / "peerDirectLan" again whenever server_config.json changes, so the owner's kill switch
    works without a restart (only those two: the rest still needs one)."""
    global RELAY_P2P_CONFIG_STAMP
    while True:
        await asyncio.sleep(RELAY_P2P_CONFIG_SECONDS)
        try:
            stamp = os.stat(SERVER_CONFIG_FILE).st_mtime_ns
            if stamp == RELAY_P2P_CONFIG_STAMP:
                continue
            with open(SERVER_CONFIG_FILE, encoding="utf-8-sig") as handle:
                loaded = json.load(handle)
        except (OSError, ValueError):
            continue   # gone or half written: look again in a second
        RELAY_P2P_CONFIG_STAMP = stamp
        if isinstance(loaded, dict):
            relay_p2p_set_switches(loaded.get("peerDirect", True), loaded.get("peerDirectLan", True))


def relay_p2p_start(loop):
    """(start_peer_relay, once the relay listens) pushes go out from this loop; server_config.json is watched."""
    global RELAY_LOOP, RELAY_P2P_CONFIG_TASK, RELAY_P2P_CONFIG_STAMP
    RELAY_LOOP = loop
    try:
        RELAY_P2P_CONFIG_STAMP = os.stat(SERVER_CONFIG_FILE).st_mtime_ns
    except OSError:
        RELAY_P2P_CONFIG_STAMP = None
    if RELAY_P2P_CONFIG_TASK is None or RELAY_P2P_CONFIG_TASK.done():
        RELAY_P2P_CONFIG_TASK = loop.create_task(relay_p2p_watch_config())
    print("Peer relay: " + relay_p2p_state_text())


def relay_p2p_direct_fields(now):
    """(Under RELAY_LOCK, for GET /windurango/relay) {"direct": true/false} for every game that asked for direct
    connections: true while it takes them and this server allows them. A game that never asked (an older
    WinDurango, or "peerDirect": false in its winDurango.json) gets no field: its row is exactly as before."""
    on = relay_p2p_switch("peerDirect")
    return {base: {"direct": bool(on and relay_p2p_capable(base, now))} for base in RELAY_P2P}


def relay_virtual_address(base):
    """The 16-byte address WinDurango advertises for this player in relay mode."""
    return RELAY_PREFIX + int(base).to_bytes(8, "big")


def relay_base_of(address):
    """The xuidBase (int) in a virtual address, or None when it is not one."""
    if len(address) != 16 or address[:8] != RELAY_PREFIX:
        return None
    return int.from_bytes(address[8:], "big")


def relay_hello_mac(secret, base, when, game_port):
    message = (b"WDR1-hello" + int(base).to_bytes(8, "big") + int(when).to_bytes(8, "big")
               + int(game_port).to_bytes(2, "big"))
    return hmac.new(str(secret).encode("utf-8"), message, hashlib.sha256).digest()[:16]


def build_relay_hello(base, secret, when, game_port):
    return (RELAY_MAGIC + bytes([RELAY_HELLO]) + int(base).to_bytes(8, "big") + int(when).to_bytes(8, "big")
            + int(game_port).to_bytes(2, "big") + relay_hello_mac(secret, base, when, game_port))


def parse_relay_hello(packet):
    """(xuidBase, time, game port, mac) from a HELLO, or None."""
    if len(packet) != RELAY_HELLO_SIZE or packet[:5] != RELAY_MAGIC + bytes([RELAY_HELLO]):
        return None
    return (int.from_bytes(packet[5:13], "big"), int.from_bytes(packet[13:21], "big"),
            int.from_bytes(packet[21:23], "big"), bytes(packet[23:39]))


def build_relay_data(kind, address, port, payload):
    """A DATA packet: kind RELAY_DATA_IN with the destination, RELAY_DATA_OUT with the source."""
    return RELAY_MAGIC + bytes([kind]) + bytes(address) + int(port).to_bytes(2, "big") + bytes(payload)


def parse_relay_data(packet, kind):
    """(virtual address, port, payload) from a DATA packet of this kind, or None (also when the payload is over
    RELAY_MAX_PAYLOAD)."""
    if (not RELAY_HEADER_SIZE <= len(packet) <= RELAY_HEADER_SIZE + RELAY_MAX_PAYLOAD
            or packet[:5] != RELAY_MAGIC + bytes([kind])):
        return None
    return bytes(packet[5:21]), int.from_bytes(packet[21:23], "big"), bytes(packet[23:])


def relay_name(base):
    record = PLAYER_REGISTRY.get(str(base)) or {}
    return record.get("gamertag") or "Player " + primary_xuid(base)


def relay_reachable(record, now):
    return bool(record and record["source"] and now - record["registered"] <= RELAY_REGISTERED_SECONDS)


def relay_set_session(key, bases):
    """Who is in this session now (an empty set once it is gone); RELAY_PEERS is worked out again from every
    session when that changes who shares one with whom. Sessions are few, and the relay only ever reads the result."""
    with RELAY_LOCK:
        if len(bases) > 1:
            if RELAY_SESSIONS.get(key) == bases:
                return
            RELAY_SESSIONS[key] = bases
        elif RELAY_SESSIONS.pop(key, None) is None:
            return   # a session of one player (or none) lets nobody talk to anybody
        peers = {}
        for together in RELAY_SESSIONS.values():
            for base in together:
                peers.setdefault(base, set()).update(together)
        before = dict(RELAY_PEERS)
        RELAY_PEERS.clear()
        RELAY_PEERS.update((base, frozenset(others - {base})) for base, others in peers.items())
        changed = relay_p2p_peers_changed(before, time.time())
    relay_p2p_schedule(changed)   # direct connections: a fresh PEERS for everyone whose peers changed


def relay_session_changed(session):
    """Call after every write of a (decorated) session's members: matchmaking's reservations, joins, leaves."""
    members = session.get("members") if isinstance(session.get("members"), dict) else {}
    bases = frozenset(base for base in (base_of(member_xuid(members[k])) for k in member_keys(members)) if base)
    key = ref_key(session.get("sessionRef") or {})
    joined = frozenset(base for base in (base_of(member_xuid(members[k])) for k in accepted_member_keys(members))
                       if base)
    # direct connections: the host flags, and who holds only a reserved place (no key or address for them yet)
    touched = relay_p2p_session_changed(key, bases, bases - joined, base_of(session_host_xuid(session)))
    relay_set_session(key, bases)
    relay_p2p_schedule(touched)


def relay_session_removed(scid, template_name, session_name):
    key = ref_key({"scid": scid, "templateName": template_name, "name": session_name})
    touched = relay_p2p_session_changed(key, frozenset(), frozenset(), None)   # direct connections
    relay_set_session(key, frozenset())
    relay_p2p_schedule(touched)


def relay_load_sessions():
    """Sessions kept from the last run (RESET_TRANSIENT_MULTIPLAYER_ON_START off) count as well."""
    for _path, session in iter_sessions(HD_SCID):
        relay_session_changed(session)


relay_load_sessions()


def relay_drop(source, reason, now, detail=""):
    """Drop a datagram. Anyone can send anything to a UDP port, so this logs at most once a minute per sender."""
    with RELAY_LOCK:
        RELAY_DROPS[reason] = RELAY_DROPS.get(reason, 0) + 1
        logged = RELAY_DROP_LOGGED.get(source[0])
        if logged is not None and now - logged < 60:
            return None
        if logged is None and len(RELAY_DROP_LOGGED) >= RELAY_DROP_LOG_LIMIT:
            return None   # a flood from many addresses: counted in the summary, not logged one by one
        RELAY_DROP_LOGGED[source[0]] = now
    print("[RELAY] dropped a packet from %s:%d: %s%s" % (source[0], source[1], reason, detail))
    return None


RELAY_MOVES_LOGGED = 5


def relay_hello(packet, source, now):
    """Remember where a player's game is, when the HELLO proves it is theirs."""
    parsed = parse_relay_hello(packet)
    if parsed is None:
        return relay_drop(source, "malformed HELLO", now)
    base, when, game_port, mac = parsed
    record = PLAYER_REGISTRY.get(str(base))
    secret = record.get("secret") if isinstance(record, dict) else None
    if not secret:
        return relay_drop(source, "HELLO from an unknown player", now, " (%d)" % base)
    if abs(now - when) > RELAY_CLOCK_SKEW:
        return relay_drop(source, "HELLO with a time too far from this server's", now,
                          " (%s, off by %d s - is that PC's clock right?)" % (relay_name(base), when - now))
    if not hmac.compare_digest(mac, relay_hello_mac(secret, base, when, game_port)):
        return relay_drop(source, "HELLO with a bad mac", now, " (claims to be %s)" % relay_name(base))

    base = str(base)
    with RELAY_LOCK:
        known = RELAY_PLAYERS.get(base)
        # An older HELLO, or an exact copy of the latest one (same time and game port) from somewhere else, is a
        # replay. A game that re-binds within the same second says a different game port, which only the player's
        # secret can sign; one on the same port moves with its next HELLO, 10 s later.
        if known and (when < known["helloTime"] or (when == known["helloTime"] and known["source"] is not None
                                                     and known["source"] != source
                                                     and known["gamePort"] == game_port)):
            replayed = True
        else:
            replayed = False
            moved = (not relay_reachable(known, now) or known["source"] != source
                     or known["gamePort"] != game_port)
            # A player has one address, the newest (their game re-bound its socket, or their NAT moved it).
            old_source = known["source"] if known else None
            if old_source and old_source != source and RELAY_SOURCES.get(old_source) == base:
                del RELAY_SOURCES[old_source]
            # One address, one player: whoever was there before is no longer reachable through it.
            previous = RELAY_SOURCES.get(source)
            if previous is not None and previous != base and previous in RELAY_PLAYERS:
                RELAY_PLAYERS[previous]["source"] = None
            RELAY_SOURCES[source] = base
            logged = [t for t in (known.get("movedLogged", []) if known else []) if now - t < 60]
            RELAY_PLAYERS[base] = {"source": source, "gamePort": game_port, "helloTime": when,
                                   "registered": now, "seen": now, "movedLogged": logged}
            # At most RELAY_MOVES_LOGGED "where is this player" lines a minute each, however often it moves.
            if moved and len(logged) < RELAY_MOVES_LOGGED:
                logged.append(now)
            else:
                moved = False
    if replayed:
        return relay_drop(source, "HELLO older than the last one, or a copy of it", now, " (%s)" % relay_name(base))
    if moved:
        print("[RELAY] %s at %s:%d (game port %d)" % (relay_name(base), source[0], source[1], game_port))
    return None


def relay_forward(packet, source, now):
    """(packet, address) passing a game's DATA packet on to the player it is for, or None when it is dropped."""
    if len(packet) > RELAY_HEADER_SIZE + RELAY_MAX_PAYLOAD:
        return relay_drop(source, "payload over %d bytes" % RELAY_MAX_PAYLOAD, now)
    parsed = parse_relay_data(packet, RELAY_DATA_IN)
    if parsed is None:
        return relay_drop(source, "malformed DATA", now)
    address, _port, payload = parsed
    target = relay_base_of(address)

    forward, reason = None, None
    with RELAY_LOCK:
        sender = RELAY_SOURCES.get(source)
        sent_by = RELAY_PLAYERS.get(sender) if sender else None
        if not relay_reachable(sent_by, now):
            reason = "DATA from an address without a HELLO"
        elif target is None:
            reason = "DATA for an address that is not a relay address"
        else:
            to = RELAY_PLAYERS.get(str(target))
            if not relay_reachable(to, now):
                reason = "DATA for a player who is not on the relay"
            elif str(target) not in RELAY_PEERS.get(sender, ()):
                reason = "DATA for a player who is not in a session with the sender"
            else:
                sent_by["seen"] = now
                count = RELAY_TRAFFIC.setdefault((sender, str(target)), [0, 0])
                count[0] += 1
                count[1] += len(payload)
                forward = (build_relay_data(RELAY_DATA_OUT, relay_virtual_address(sender), sent_by["gamePort"],
                                            payload), to["source"])
    if reason:
        return relay_drop(source, reason, now)
    return forward


def relay_datagram(packet, source, now=None):
    """What to do with one datagram that reached the relay: (packet, address) to send, or None."""
    now = time.time() if now is None else now
    source = (source[0], source[1])
    kind = packet[4] if len(packet) >= 5 and packet[:4] == RELAY_MAGIC else None
    if kind == RELAY_HELLO:
        return relay_p2p_hello(packet, source, now)   # relay_hello, then a push if the player moved
    if kind == RELAY_DATA_IN:
        return relay_forward(packet, source, now)
    if kind == RELAY_P2P_QUERY:
        return relay_p2p_query(packet, source, now)
    return relay_drop(source, "not a relay packet", now)


def relay_summary(now=None):
    """The log line for what the relay did since the last call (None when it did nothing), and a fresh count."""
    now = time.time() if now is None else now
    with RELAY_LOCK:
        traffic, drops = dict(RELAY_TRAFFIC), dict(RELAY_DROPS)
        RELAY_TRAFFIC.clear()
        RELAY_DROPS.clear()
        for ip, logged in list(RELAY_DROP_LOGGED.items()):
            if now - logged >= 60:
                del RELAY_DROP_LOGGED[ip]
    if not traffic and not drops:
        return None
    parts = ["%s -> %s %d packets, %d bytes" % (relay_name(a), relay_name(b), n, size)
             for (a, b), (n, size) in sorted(traffic.items())]
    if drops:
        parts.append("dropped " + ", ".join("%d %s" % (n, reason) for reason, n in sorted(drops.items())))
    return "[RELAY] last minute: " + "; ".join(parts)


async def relay_summaries():
    while True:
        await asyncio.sleep(RELAY_SUMMARY_SECONDS)
        line = relay_summary()
        if line:
            print(line)
        relay_p2p_minute()   # direct connections: old pair keys go; the [P2P] minute line


class PeerRelay(asyncio.DatagramProtocol):
    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        try:
            forward = relay_datagram(data, addr)
        except Exception as error:   # one odd packet must not stop the relay
            print("[RELAY] error handling a packet from %s: %r" % (addr[0], error))
            return
        if forward is None:
            return
        if self.transport.get_write_buffer_size() > RELAY_SEND_BUFFER:
            relay_drop(addr, "send queue full", time.time())
            return
        self.transport.sendto(*forward)

    def error_received(self, exc):
        # A player's game went away and the send bounced (ICMP port unreachable): their next HELLO, or the lack
        # of one, sorts it out.
        pass


async def start_peer_relay():
    """Listen for peer traffic on relayBind:relayPort (UDP). Returns the transport, or None when the relay is off or
    cannot listen - the rest of the server works without it; players then need a VPN for dungeons."""
    global RELAY_TRANSPORT, RELAY_SUMMARY_TASK

    port = SERVER_CONFIG.get("relayPort")
    bind = SERVER_CONFIG.get("relayBind") or "0.0.0.0"
    if not port:
        print("Peer relay: off (relayPort is 0 in server_config.json)")
        return None
    loop = asyncio.get_running_loop()
    try:
        transport, _ = await loop.create_datagram_endpoint(PeerRelay, local_addr=(bind, int(port)))
    except (OSError, ValueError, TypeError, OverflowError) as error:   # OverflowError: a port past 65535
        print("WARNING: the peer relay cannot listen on udp %s:%s (%s). Lobbies still work; dungeons need players "
              "who can reach each other." % (bind, port, error))
        return None
    RELAY_TRANSPORT = transport
    RELAY_SUMMARY_TASK = loop.create_task(relay_summaries())
    print("Peer relay: udp://%s:%d (open this UDP port in the server's firewall)" % (bind, int(port)))
    relay_p2p_start(loop)
    return transport


@app.route("/windurango/relay", methods=["GET"])
def relay_status():
    """Who the peer relay can reach. Never addresses or secrets. (privacy_patch v2) The players only for this
    server's owners (a verified owner key); everyone else - the launcher's "Relay" light reads only "running" -
    gets whether it runs and how many players it heard from, and an empty "players"."""
    caller = g.get("caller_base") if g.get("caller_verified") is True else None
    owner = bool(caller and client_is_owner(caller))
    now = time.time()
    with RELAY_LOCK:
        heard = [(base, dict(record)) for base, record in RELAY_PLAYERS.items()
                 if now - record["seen"] <= RELAY_LISTED_SECONDS]
        direct = relay_p2p_direct_fields(now) if owner else {}
    players = sorted(({"gamertag": relay_name(base), "xuid": primary_xuid(base),
                       "registered": relay_reachable(record, now),
                       "lastSeenSeconds": max(0, int(now - record["seen"])), **direct.get(base, {})}
                      for base, record in heard),
                     key=lambda p: p["gamertag"].lower()) if owner else []
    return json_response({"port": SERVER_CONFIG.get("relayPort"), "running": RELAY_TRANSPORT is not None,
                          "players": players, "playerCount": len(heard)})


# Local Xbox RTA WebSocket

def forget_rta_websocket(websocket):
    for connection_id, (known, _) in list(RTA_CONNECTIONS.items()):
        if known is websocket:
            RTA_CONNECTIONS.pop(connection_id, None)
            print("[RTA] CONNECTION RELEASED:", connection_id)
            connection_released(connection_id)


def rta_connection_free_for(connection_id, rta_base):
    """(privacy_patch v2 rta) A connection id that no socket holds now (after a restart nobody holds any) goes back
    to this socket only when no stored session names it as a member's connection, or the socket proves (its key) it
    is one of those members' players - the game's socket carries its key, and reconnects after a restart with its
    old id. An unauthenticated socket that read the id from a session document gets a fresh id instead of that
    player's shoulder taps (and could no longer take the player out of every session by closing)."""
    connection_id = str(connection_id).lower()
    bases = set()
    try:
        for _, session in iter_sessions(HD_SCID):
            members = session.get("members") if isinstance(session.get("members"), dict) else {}
            for key in member_keys(members):
                properties = members[key].get("properties")
                system = properties.get("system") if isinstance(properties, dict) else None
                connection = system.get("connection") if isinstance(system, dict) else None
                if connection and str(connection).lower() == connection_id:
                    bases.add(base_of(member_xuid(members[key])))
    except Exception as error:   # the check never stops the socket: the rule before this patch applies
        builtins.print("[RTA] could not look up connection %s (%r) - kept as asked" % (connection_id, error))
        return True
    if not bases:
        return True
    if rta_base is not None and str(rta_base) in bases:
        return True
    builtins.print("[RTA] connection %s belongs to a session member; this socket (%s) did not prove it is that player"
                   " - it gets a new id" % (connection_id, "a player" if rta_base else "no key"))
    return False


async def rta_client(websocket, path):
    connection_id = str(uuid.uuid4())
    next_subscription_id = 0

    print("\n" + "=" * 76)
    print(f"[RTA] {utc_now()} CLIENT CONNECTED")
    print("PATH:", path)
    rta_base, rta_secret = parse_token(websocket.request_headers.get("Authorization"))
    if rta_base is not None and verify_player(rta_base, rta_secret) is False:
        rta_base = None   # a claimed identity with the wrong secret does not count as that player online
    print("AUTH HEADER:", ("player %s" % rta_base) if rta_base else
          ("present" if websocket.request_headers.get("Authorization") else "missing"))
    if rta_base is not None:
        with STATE_LOCK:
            RTA_PLAYERS[rta_base] = RTA_PLAYERS.get(rta_base, 0) + 1
    print("LOCAL CONNECTION ID:", connection_id)
    print("=" * 76)

    try:
        async for raw in websocket:
            print(f"\n[RTA] {utc_now()} CLIENT -> SERVER")
            print(raw)

            try:
                message = json.loads(raw)
            except Exception:
                print("[RTA] Ignoring non-JSON frame")
                continue

            if not isinstance(message, list) or not message:
                print("[RTA] Unexpected frame shape")
                continue

            msg_type = message[0]

            # Subscribe frame: [1, clientSequenceId, resourceUri].
            if msg_type == 1 and len(message) >= 3:
                sequence_id = message[1]
                resource = message[2]

                subscription_id = next_subscription_id
                next_subscription_id += 1

                data = None

                connections_prefix = "https://sessiondirectory.xboxlive.com/connections"
                if (
                    isinstance(resource, str)
                    and resource.lower().startswith(connections_prefix)
                ):
                    # A reconnecting client resubscribes with its previous id; keep it so the
                    # connection recorded in its sessions stays valid.
                    previous_id = resource[len(connections_prefix):].strip("/").lower()
                    # Only the game that had it: session members show their connection ids, and whoever took over
                    # one would get that player's taps and, by closing, take the player out of every session.
                    # privacy_patch v2 rta: an id no socket holds only for the player a session names with it.
                    if previous_id and ((previous_id not in RTA_CONNECTION_OWNERS
                                         and rta_connection_free_for(previous_id, rta_base))
                                        or (rta_base is not None
                                            and RTA_CONNECTION_OWNERS.get(previous_id) == rta_base)):
                        connection_id = previous_id
                    elif previous_id:
                        print("[RTA] %s is another game's connection - this one stays %s" % (
                            previous_id, connection_id))

                    RTA_CONNECTIONS[connection_id] = (websocket, subscription_id)
                    RTA_CONNECTION_OWNERS[connection_id] = rta_base
                    print("[RTA] MPSD CONNECTION:", connection_id, "subscription", subscription_id)

                    data = {
                        "ConnectionId": connection_id
                    }

                reply = [
                    1,
                    sequence_id,
                    0,
                    subscription_id,
                    data
                ]

                outgoing = json.dumps(
                    reply,
                    separators=(",", ":")
                )

                print("[RTA] SERVER -> CLIENT")
                print(outgoing)

                await websocket.send(outgoing)
                continue

            if msg_type == 2 and len(message) >= 2:
                sequence_id = message[1]
                reply = [2, sequence_id, 0]

                if len(message) >= 3:
                    for known_id, (known, known_sub) in list(RTA_CONNECTIONS.items()):
                        if known is websocket and known_sub == message[2]:
                            RTA_CONNECTIONS.pop(known_id, None)

                outgoing = json.dumps(
                    reply,
                    separators=(",", ":")
                )

                print("[RTA] SERVER -> CLIENT")
                print(outgoing)

                await websocket.send(outgoing)
                continue

            print("[RTA] UNHANDLED MESSAGE TYPE:", msg_type)

    except Exception as exc:
        print(f"[RTA] {utc_now()} CONNECTION ENDED:", repr(exc))
    finally:
        forget_rta_websocket(websocket)
        if rta_base is not None:
            with STATE_LOCK:
                RTA_PLAYERS[rta_base] = max(0, RTA_PLAYERS.get(rta_base, 0) - 1)


async def run_rta():
    global RTA_LOOP

    RTA_LOOP = asyncio.get_running_loop()
    print("RTA WebSocket: ws://%s:%d/connect" % (SERVER_CONFIG["bind"], SERVER_CONFIG["rtaPort"]))
    await start_peer_relay()

    # No server keepalive pings: the title's websocket (through Fiddler) never answers them,
    # and the resulting disconnect aborted lobby/matchmaking.
    async with serve(
        rta_client,
        SERVER_CONFIG["bind"],
        SERVER_CONFIG["rtaPort"],
        ping_interval=None,
        ping_timeout=None
    ):
        await asyncio.Future()


# Where the server listens. server_config.json next to this file; the defaults keep it on this PC only.
# To host for friends set "bind" to "0.0.0.0" (every interface) or, better, this PC's Tailscale address, and give
# them that address as "server" in their winDurango.json. See the README for the security side of it.
# On a cloud server keep "bind" at "127.0.0.1": a reverse proxy (Caddy) on the same machine is then the only thing
# the internet reaches, and it adds HTTPS - https://<host>/connect goes to rtaPort, everything else to httpPort.
# The peer relay is UDP, which Caddy does not carry, so it listens on its own: "relayBind" (every interface by
# default) and "relayPort" (0 turns it off). That UDP port has to be open in the server's firewall.
SERVER_CONFIG_FILE = os.path.join(ROOT, "server_config.json")
SERVER_CONFIG = {"bind": "127.0.0.1", "httpPort": 5000, "rtaPort": 8765, "relayBind": "0.0.0.0", "relayPort": 3075}
# Direct connections between players (the peer relay's PEERQ / PEERS): "peerDirect": false is the owner's kill
# switch (every game is told at once to use the relay only), "peerDirectLan": false leaves out LAN and VPN
# addresses. Both are read again while the server runs.
SERVER_CONFIG.update({"peerDirect": True, "peerDirectLan": True})
LOOPBACK_BINDS = ("127.0.0.1", "localhost", "::1")
try:
    with open(SERVER_CONFIG_FILE, encoding="utf-8-sig") as _handle:
        SERVER_CONFIG.update({k: v for k, v in json.load(_handle).items() if k in SERVER_CONFIG})
except OSError:
    pass
except (ValueError, AttributeError) as _error:
    print("WARNING: server_config.json is unreadable (%s); using the default address and ports." % _error)

# Listening on this machine only means whatever connects is on this machine too - behind a reverse proxy, the
# proxy. Take the player's address and http/https from the X-Forwarded-For / X-Forwarded-Proto it adds, so the
# request log shows who is really connecting instead of 127.0.0.1. Nothing uses either one to decide access:
# who a player is comes only from the WD1 secret in the Authorization header.
if SERVER_CONFIG["bind"] in LOOPBACK_BINDS:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)


# (tuning_patch) fastPaths wrappers. save_json: compact JSON in one C-encoded write. Config-file readers: looked at every 2 s, not on every call; the server's own
# min-version change resets them (it counts at once). /windurango/players, the same for every caller: built at most every
# 2 s. Without fastPaths each wrapper calls straight through.
save_json = tuning_save_json(save_json)
client_config = tuning_ttl(client_config)
event_owners = tuning_ttl(event_owners)
join_mode = tuning_ttl(join_mode)
news_item = tuning_ttl(news_item)
client_write_min_version = tuning_ttl_resetting(client_write_min_version)
# privacy_patch v1: /windurango/players depends on the caller now - no shared answer (privacy_everyone caches)
# privacy_patch v2: groups relay, achievements, presence, lobbies, hardening, sessions, rta, quietlogs (privacy2\privacy_patch2.py)
PRIVACY_PATCH2_GROUPS = frozenset(('relay', 'achievements', 'presence', 'lobbies', 'hardening', 'sessions', 'rta', 'quietlogs'))


def run_http():
    print("HTTP compatibility server: http://%s:%d" % (SERVER_CONFIG["bind"], SERVER_CONFIG["httpPort"]))
    if SERVER_CONFIG["bind"] not in LOOPBACK_BINDS:
        print("HOSTING: players on other PCs can connect. Only share this address with people you trust.")

    print(tuning_summary())   # (tuning_patch: here, not with the other startup lines - their patches check those)
    print(matchmaking_summary())   # (lobby_patch v1)
    print(solo_startup_line())   # (solo_patch v1)
    print(fps_startup_line())    # (fps_split v3)
    app.run(
        host=SERVER_CONFIG["bind"],
        port=SERVER_CONFIG["httpPort"],
        debug=False,
        use_reloader=False,
        threaded=True,
        request_handler=TunedRequestHandler   # (tuning_patch: werkzeug's own; quietLogs shortens its access line)
    )


# ---------------------------------------------------------------------------
# achievements_patch v1 - Happy Dungeons achievements (release 2c; next3\achievements\DESIGN.md 4.5).
#
# The game's achievements lived in Xbox Live. WinDurango (winrt_x HDAchievements.cpp) now works them out on the
# player's PC from the game's own events and reports each unlock here, so the players page can list them and they
# follow the player to another PC. The client reports; the server only checks that the XUID is the caller's own (one
# of their four) and the ids are real. Owners can revoke (calibration).
ACHIEVEMENTS_FILE = os.path.join(DATA, "achievements.json")
ACHIEVEMENTS_TITLE = "3751A2CA"
ACHIEVEMENTS_CATALOG = json.loads(r"""{"titleId":"3751A2CA","achievements":[{"id":1,"key":"Achievement001","name":"A Journey of 1000 Miles!","description":"Start a game of Happy Dungeons.","gamerscore":10,"secret":false},{"id":2,"key":"Achievement002","name":"Ready for Action","description":"View the Playing Guide.","gamerscore":10,"secret":false},{"id":3,"key":"Achievement003","name":"And So It Begins!","description":"Complete the quest \"Search for the Princess.\"","gamerscore":20,"secret":true},{"id":4,"key":"Achievement004","name":"Hope I Haven't Mist Anything","description":"Complete the quest \"Dense Fog.\"","gamerscore":20,"secret":true},{"id":5,"key":"Achievement005","name":"Made It Big","description":"Complete the quest \"Big Game.\"","gamerscore":30,"secret":true},{"id":6,"key":"Achievement006","name":"Making Friends","description":"Complete the quest \"A Little Friend.\"","gamerscore":30,"secret":true},{"id":7,"key":"Achievement007","name":"There Was a Lot at Steak","description":"Complete the quest \"Forest Barbecue.\"","gamerscore":40,"secret":true},{"id":8,"key":"Achievement008","name":"Not So Bossy","description":"Complete the quest \"The Goblin Leader.\"","gamerscore":50,"secret":true},{"id":9,"key":"Achievement009","name":"Goblins Shmoblins","description":"Kill 100 goblins.","gamerscore":20,"secret":false},{"id":10,"key":"Achievement010","name":"Game Ogre","description":"Kill 10 ogres.","gamerscore":20,"secret":false},{"id":11,"key":"Achievement011","name":"Not Much of a People Person","description":"Kill 100 human enemies.","gamerscore":20,"secret":false},{"id":12,"key":"Achievement012","name":"They Won't Be Getting Up Too Soon","description":"Use a Finishing Move on a boss.","gamerscore":30,"secret":true},{"id":13,"key":"Achievement013","name":"Go Team!","description":"Complete a mission with at least two people remaining.","gamerscore":20,"secret":false},{"id":14,"key":"Achievement014","name":"Death Is Overrated","description":"Complete a mission on Death difficulty with at least four people remaining.","gamerscore":25,"secret":false},{"id":15,"key":"Achievement015","name":"Happy Times!","description":"Open a card pack in Happy Cards.","gamerscore":10,"secret":false},{"id":16,"key":"Achievement016","name":"Hey There, Lil' Buddy!","description":"Get a minion.","gamerscore":5,"secret":false},{"id":17,"key":"Achievement017","name":"This Is Gonna Be Hell to Insure...","description":"Equip an item of epic rarity or higher.","gamerscore":10,"secret":false},{"id":18,"key":"Achievement018","name":"I Could Open a Museum","description":"Get 100 pieces of equipment.","gamerscore":50,"secret":false},{"id":19,"key":"Achievement019","name":"Ready for the Big Leagues","description":"Level up a minion via \"Boost Minions.\"","gamerscore":20,"secret":false},{"id":20,"key":"Achievement020","name":"A Myriad of Merry Minions","description":"Get a total of 30 minions.","gamerscore":50,"secret":false},{"id":21,"key":"Achievement021","name":"Bring It On!","description":"Level up a piece of equipment to its maximum level.","gamerscore":30,"secret":false},{"id":22,"key":"Achievement022","name":"Hey There, Big Buddy!","description":"Level up a minion to its maximum level.","gamerscore":30,"secret":false},{"id":23,"key":"Achievement023","name":"Promising Rookie","description":"Reach player level 10.","gamerscore":10,"secret":false},{"id":24,"key":"Achievement024","name":"Adventuring Knight","description":"Reach player level 30.","gamerscore":30,"secret":false},{"id":25,"key":"Achievement025","name":"Grand Master","description":"Reach player level 50.","gamerscore":50,"secret":false},{"id":26,"key":"Achievement026","name":"Hardened Warrior","description":"Play for 10 hours as a warrior.","gamerscore":20,"secret":false},{"id":27,"key":"Achievement027","name":"A-Mage-Ing!","description":"Play for 10 hours as a mage.","gamerscore":20,"secret":false},{"id":28,"key":"Achievement028","name":"Ain't No Generic Cleric","description":"Play for 10 hours as a cleric.","gamerscore":20,"secret":false},{"id":29,"key":"Achievement029","name":"Attention Deficit Hyper Mode","description":"Enter Hyper Mode.","gamerscore":50,"secret":false},{"id":30,"key":"Achievement030","name":"Gonna Need a Lie Down Now","description":"Complete a mission on Insane difficulty.","gamerscore":100,"secret":false},{"id":31,"key":"Achievement031","name":"Thanks, Mr. Barrel","description":"Destroy a total of 1000 barrels, pots, and/or mushrooms.","gamerscore":50,"secret":true},{"id":32,"key":"Achievement032","name":"Well Done-geon!","description":"Complete 42 missions on Insane difficulty.","gamerscore":100,"secret":false},{"id":33,"key":"Achievement033","name":"Knight apprentice","description":"25 hours played.","gamerscore":10,"secret":false},{"id":34,"key":"Achievement034","name":"Dungeon raider","description":"50 hours played.","gamerscore":20,"secret":false},{"id":35,"key":"Achievement035","name":"Super General","description":"75 hours played.","gamerscore":30,"secret":false},{"id":36,"key":"Achievement036","name":"The Unbearable Lightness of the Dungeon","description":"100 hours played.","gamerscore":50,"secret":false},{"id":37,"key":"Achievement037","name":"The 13 stairs of death","description":"Defeated 8192 zombies.","gamerscore":20,"secret":false},{"id":38,"key":"Achievement038","name":"Everyone's price","description":"Complete \"Front and Back Stage\".","gamerscore":10,"secret":false},{"id":39,"key":"Achievement039","name":"Happy Dungeons","description":"Escape from any level in Infinite Dungeon.","gamerscore":10,"secret":false},{"id":40,"key":"Achievement040","name":"The path to strength","description":"Complete \"The Goblin Leader\" in Death difficulty.","gamerscore":50,"secret":false},{"id":41,"key":"Achievement041","name":"No fruit comes after death","description":"Resurrect allies 77 times.","gamerscore":30,"secret":false},{"id":42,"key":"Achievement042","name":"Diversity","description":"Evolve an item.","gamerscore":20,"secret":false}]}""")
ACHIEVEMENTS_BY_ID = {a["id"]: a for a in ACHIEVEMENTS_CATALOG["achievements"]}
ACHIEVEMENTS_TOTAL_G = sum(a["gamerscore"] for a in ACHIEVEMENTS_CATALOG["achievements"])
ACHIEVEMENTS_MAX_BODY = 8192
ACHIEVEMENTS_RATE = 60                # unlock calls per player ...
ACHIEVEMENTS_RATE_SECONDS = 60        # ... in this many seconds
ACHIEVEMENTS_LOCK = threading.Lock()
ACHIEVEMENTS = {}                     # xuid -> {titleId -> {str(id) -> {"at", "evidence"}}}
ACHIEVEMENTS_CALLS = {}               # str(xuidBase) -> recent unlock call times (time.monotonic)


def achievements_load():
    try:
        with open(ACHIEVEMENTS_FILE, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        print("[ACHIEVEMENTS] data/achievements.json cannot be read (%s) - starting empty, the file is kept" % error)
        return {}
    clean = {}
    for xuid, titles in (data.items() if isinstance(data, dict) else []):
        if not str(xuid).isdigit() or not isinstance(titles, dict):
            continue
        for title, ids in titles.items():
            if not isinstance(ids, dict):
                continue
            kept = {str(int(k)): v for k, v in ids.items()
                    if str(k).isdigit() and int(k) in ACHIEVEMENTS_BY_ID and isinstance(v, dict)}
            if kept:
                clean.setdefault(str(xuid), {})[str(title)] = kept
    return clean


def achievements_save_locked():
    """Writes data/achievements.json (call with ACHIEVEMENTS_LOCK held)."""
    try:
        os.makedirs(DATA, exist_ok=True)
        temp = ACHIEVEMENTS_FILE + ".tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(ACHIEVEMENTS, handle, indent=1, sort_keys=True)
        os.replace(temp, ACHIEVEMENTS_FILE)
        return True
    except OSError as error:
        print("[ACHIEVEMENTS] COULD NOT SAVE data/achievements.json:", error)
        return False


ACHIEVEMENTS.update(achievements_load())


def achievements_unlocked(xuid):
    """[{id, at}] of this XUID, oldest first."""
    with ACHIEVEMENTS_LOCK:
        ids = dict(ACHIEVEMENTS.get(str(xuid), {}).get(ACHIEVEMENTS_TITLE, {}))
    return sorted(({"id": int(k), "at": v.get("at", "")} for k, v in ids.items()),
                  key=lambda entry: (entry["at"], entry["id"]))


def achievements_summary(xuid, with_list=True):
    xuid = str(xuid)
    unlocked = achievements_unlocked(xuid)
    base = base_of(xuid)
    player = ((int(xuid) - int(base)) or XUIDS_PER_PLAYER) if base else None   # base+1 is player 1, base+0 player 4
    summary = {"xuid": xuid, "gamertag": gamertag_for(xuid), "player": player, "count": len(unlocked),
               "gamerscore": sum(ACHIEVEMENTS_BY_ID[e["id"]]["gamerscore"] for e in unlocked),
               "total": len(ACHIEVEMENTS_BY_ID), "totalGamerscore": ACHIEVEMENTS_TOTAL_G}
    if with_list:
        summary["unlocked"] = unlocked
    return summary


@app.route("/windurango/achievements/catalog", methods=["GET"])
def achievements_catalog():
    return json_response({
        "titleId": ACHIEVEMENTS_TITLE, "total": len(ACHIEVEMENTS_BY_ID), "totalGamerscore": ACHIEVEMENTS_TOTAL_G,
        "achievements": [{k: a[k] for k in ("id", "name", "description", "gamerscore", "secret")}
                         for a in ACHIEVEMENTS_CATALOG["achievements"]]})


@app.route("/windurango/achievements/players", methods=["GET"])
def achievements_players():
    """(privacy_patch v2) No public directory of players: the owners see every registered player, a verified player
    themselves and their friends, anyone else nobody. "scope" says which ("everyone", "friends", "none")."""
    base, owner, allowed = privacy_viewer()
    if not base:
        bases, scope = [], "none"
    elif owner:
        bases, scope = sorted(PLAYER_REGISTRY), "everyone"
    else:   # primary XUIDs are xuidBase + 1
        bases, scope = sorted(b for b in (str(int(x) - 1) for x in allowed) if b in PLAYER_REGISTRY), "friends"
    everyone = [achievements_summary(primary_xuid(b), with_list=False) for b in bases]
    everyone.sort(key=lambda s: (-s["gamerscore"], str(s["gamertag"]).lower()))
    return json_response({"players": everyone, "scope": scope, "total": len(ACHIEVEMENTS_BY_ID),
                          "totalGamerscore": ACHIEVEMENTS_TOTAL_G})


@app.route("/windurango/achievements/me", methods=["GET"])
def achievements_me():
    base, refusal = require_player()
    if refusal:
        return refusal
    players = []
    for i in (1, 2, 3, 0):   # player 1 first, then the local players in the order they join
        xuid = str(int(base) + i)
        summary = achievements_summary(xuid)
        if i == 1 or summary["count"]:
            players.append(summary)
    return json_response({"xuidBase": base, "players": players, "total": len(ACHIEVEMENTS_BY_ID),
                          "totalGamerscore": ACHIEVEMENTS_TOTAL_G})


@app.route("/windurango/achievements/<xuid>", methods=["GET"])
def achievements_of_player(xuid):
    """(privacy_patch v2) Only that player (any of their four XUIDs - the game merges its own), their friends and
    this server's owners. Anyone else gets 401/403 whether or not the XUID is registered (other routes - the profile's
    gamertag, identity/claim's 409, friends/add's 404 - still confirm a XUID someone already knows; XUID blocks are
    46-bit random, so none of them lists players)."""
    if not xuid.isdigit() or len(xuid) > 20:
        return json_response({"ok": False, "reason": "no player with that XUID"}, 404)
    if g.get("caller_verified") is not True:
        return json_response({"ok": False, "reason": "sign in first"}, 401)
    if not privacy_may_see(xuid):
        return json_response({"ok": False, "reason": "only that player, their friends and this server's owners "
                                                     "can see their achievements"}, 403)
    if base_of(xuid) is None:
        return json_response({"ok": False, "reason": "no player with that XUID"}, 404)
    return json_response(achievements_summary(xuid))


def achievements_rate_limited(base):
    now = time.monotonic()
    with ACHIEVEMENTS_LOCK:
        recent = [t for t in ACHIEVEMENTS_CALLS.get(base, []) if now - t < ACHIEVEMENTS_RATE_SECONDS]
        limited = len(recent) >= ACHIEVEMENTS_RATE
        if not limited:
            recent.append(now)
        ACHIEVEMENTS_CALLS[base] = recent
    return limited


def achievements_body(raw):
    """(body dict, None) or (None, the 400 response)."""
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, json_response({"ok": False, "reason": "the body is not JSON"}, 400)
    if not isinstance(body, dict):
        return None, json_response({"ok": False, "reason": "the body is not a JSON object"}, 400)
    return body, None


def achievements_ids(value):
    """The ids as ints, or None when they are not 1 to 42 real achievement ids."""
    if not isinstance(value, list) or not 1 <= len(value) <= len(ACHIEVEMENTS_BY_ID):
        return None
    ids = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or item not in ACHIEVEMENTS_BY_ID:
            return None
        ids.append(item)
    return sorted(set(ids))


@app.route("/windurango/achievements/unlock", methods=["POST"])
def achievements_unlock():
    base, refusal = require_player()
    if refusal:
        return refusal
    raw = request.get_data(cache=True) or b""
    if len(raw) > ACHIEVEMENTS_MAX_BODY:
        return json_response({"ok": False, "reason": "the body is too big"}, 413)
    if achievements_rate_limited(base):
        response = json_response({"ok": False, "reason": "too many achievement reports - try again in a minute"}, 429)
        response.headers["Retry-After"] = str(ACHIEVEMENTS_RATE_SECONDS)
        return response
    body, refusal = achievements_body(raw)
    if refusal:
        return refusal
    xuid = str(body.get("xuid") or "")
    if not xuid.isdigit() or len(xuid) > 20 or not int(base) <= int(xuid) < int(base) + XUIDS_PER_PLAYER:
        return json_response({"ok": False, "reason": "that XUID is not one of yours"}, 403)
    if str(body.get("titleId") or ACHIEVEMENTS_TITLE).upper() != ACHIEVEMENTS_TITLE:
        return json_response({"ok": False, "reason": "unknown titleId"}, 400)
    ids = achievements_ids(body.get("ids"))
    if ids is None:
        return json_response({"ok": False, "reason": "ids must be 1 to %d achievement numbers (1-%d)" % (
            len(ACHIEVEMENTS_BY_ID), len(ACHIEVEMENTS_BY_ID))}, 400)
    evidence = body.get("evidence") if isinstance(body.get("evidence"), dict) else {}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with ACHIEVEMENTS_LOCK:
        mine = ACHIEVEMENTS.setdefault(xuid, {}).setdefault(ACHIEVEMENTS_TITLE, {})
        new = [i for i in ids if str(i) not in mine]
        for i in new:
            text = evidence.get(str(i))
            mine[str(i)] = {"at": now, "evidence": re.sub(r"[^\x20-\x7e]", "?", str(text))[:200] if text else ""}
        saved = achievements_save_locked() if new else True
    if new:
        print("[ACHIEVEMENTS] %s (%s) unlocked %s" % (xuid, gamertag_for(xuid).encode("ascii", "replace").decode(),
                                                      ",".join("%03d" % i for i in new)))
    summary = achievements_summary(xuid)
    return json_response({"ok": saved, "new": new, "unlocked": [e["id"] for e in summary["unlocked"]],
                          "count": summary["count"], "gamerscore": summary["gamerscore"]}, 200 if saved else 500)


@app.route("/windurango/admin/achievements/revoke", methods=["POST"])
def achievements_revoke():
    owner_check = globals().get("require_owner")
    if owner_check is None:   # a server without the event switcher's owners list: nobody may revoke
        return json_response({"ok": False, "reason": "this server has no owners list"}, 403)
    base, refusal = owner_check()
    if refusal:
        if refusal.status_code == 403:
            return json_response({"ok": False, "reason": "only this server's owners can revoke achievements"}, 403)
        return refusal
    raw = request.get_data(cache=True) or b""
    if len(raw) > ACHIEVEMENTS_MAX_BODY:
        return json_response({"ok": False, "reason": "the body is too big"}, 413)
    body, refusal = achievements_body(raw)
    if refusal:
        return refusal
    xuid = str(body.get("xuid") or "")
    ids = achievements_ids(body.get("ids"))
    if not xuid.isdigit() or ids is None:
        return json_response({"ok": False, "reason": "give a xuid and ids"}, 400)
    with ACHIEVEMENTS_LOCK:
        mine = ACHIEVEMENTS.get(xuid, {}).get(ACHIEVEMENTS_TITLE, {})
        removed = [i for i in ids if mine.pop(str(i), None) is not None]
        saved = achievements_save_locked() if removed else True
    if removed:
        print("[ACHIEVEMENTS] owner %s revoked %s from %s" % (base, ",".join("%03d" % i for i in removed), xuid))
    return json_response({"ok": saved, "removed": removed, **{k: v for k, v in achievements_summary(xuid).items()
                                                             if k in ("count", "gamerscore")}})


def achievements_summary_line():
    with ACHIEVEMENTS_LOCK:
        players = sum(1 for titles in ACHIEVEMENTS.values() if titles.get(ACHIEVEMENTS_TITLE))
        unlocks = sum(len(titles.get(ACHIEVEMENTS_TITLE, {})) for titles in ACHIEVEMENTS.values())
    return "ACHIEVEMENTS: %d achievements (%d G); %d unlock(s) stored for %d XUID(s)" % (
        len(ACHIEVEMENTS_BY_ID), ACHIEVEMENTS_TOTAL_G, unlocks, players)


print(achievements_summary_line())


if __name__ == "__main__":
    print("=" * 76)
    print("Happy Dungeons - WinDurango Xbox Live server")
    print("=" * 76)
    print(
        "RESET_TRANSIENT_MULTIPLAYER_ON_START:",
        RESET_TRANSIENT_MULTIPLAYER_ON_START
    )
    print(friends_join_summary())
    print(hardening_summary())
    print(client_update_summary())
    print(serverdata_summary())
    print(event_switcher_summary())
    print(invite_summary())   # (invite_patch v1)

    http_thread = threading.Thread(
        target=run_http,
        daemon=True
    )
    http_thread.start()

    try:
        asyncio.run(run_rta())
    except KeyboardInterrupt:
        print("\nStopping.")
