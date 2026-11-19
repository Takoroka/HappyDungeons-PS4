#!/usr/bin/env python3
"""Roll the event orders, v2: every game day gets its own order keys, and a key is never reused.

WHY v1 (roll_event_orders.py) never refreshed
  The save tracks an event order by its JSON key and nothing else:
    dungeon_SaveData.dungeonOrderSaveData.achievementStates_.achievementStateS32_.<order key>
      = {"type_": 1, "state_": <bits>, "achievedValue_": {"bits": <progress>}}
  There is no date, period id or Index in it. v1 re-dated the SAME 28 keys every day, so the game
  kept showing the same records: progress kept adding up and a claimed order stayed claimed.

WHAT v2 serves (the calendar shape Toylogic used in 180608_event_order__ev1119.pak)
  * Game day d runs from d 00:00:00 to d+1 00:00:00 UTC (HHMMSS 0), the owner's choice (the same
    moment v1 re-dated its file). The exe compares periods with the PC's UTC clock (0x140EB75E0,
    GetSystemTimeAsFileTime + FileTimeToSystemTime), both ends inclusive. (Toylogic used 07:00 UTC,
    which is also the exe's own daily-lottery reset, -25200 s at 0x140EBD9B1; that reset is in the
    exe and does not depend on this file.)
  * Day d gets the keys E_<yymmdd>_00 .. E_<yymmdd>_NN, one per Enable=true template order, in
    template order, plus two periods:
      E_<yymmdd>_D00  display  d 00:00 -> d+1 00:00            (progress counts only here)
      E_<yymmdd>_R00  receive  d 00:00 -> d+1+TAIL 00:00       (claim tail for achieved orders;
                                                               TAIL 1 = until the end of day d+1)
  * The file served during game day G holds days max(G-TAIL, SWITCH_DAY) .. G+1 (at most 3 days =
    84 orders with TAIL 1). Tomorrow is pre-loaded, so at 00:00:00 UTC the new day's set is already
    in every client's file: a running game rolls over without a restart, and a game started
    between 00:00 and the 00:05 run gets the new set from the file built the day before. A day
    leaves the file at the first build after its R00 closes. The game deletes a type_ 1 record
    whose key is in no loaded order table (0x140EB08E0, erase at 0x140EB0AD6), so saves stay
    bounded (at most one record per order in the file).
  * Both period ends are inclusive, so during the single second 00:00:00 UTC the ending day's set
    and the new day's set both pass the display test. That is harmless: the keys differ, so no
    record can be claimed twice; the ending day is still inside its own claim window; and nothing
    else is displayable. The build checks this second explicitly.
  * A day's definitions are frozen the first time it is built (STATE_DIR/<yyyymmdd>.json). A
    template edit then changes only days that have not been built yet, and a key never changes
    meaning.
  * Switch-over: SWITCH_DAY is the game day of the first install (STATE_DIR/SWITCH_DAY). The old
    template keys behave like a day SWITCH_DAY-1: while G <= SWITCH_DAY-1+TAIL (with TAIL 1: on
    SWITCH_DAY only) they stay in the file with display closed (E_LEGACY_D00, ends SWITCH_DAY
    00:00) and receive open (E_LEGACY_R00, ends SWITCH_DAY+TAIL 00:00), so an achieved old order
    can still be claimed. After that they are dropped and the game deletes their records. The
    earlier in the UTC day the first install runs, the longer that window is.
  * Index = SortNumber = 1..N in file order, oldest day first (the save never stores Index).
    E_M00_TEST and its period E_P00_TEST are kept as in the template (Index 0, disabled).

SAFETY (same rules as v1)
  * It builds from the TEMPLATE only. The live file is read only to compare bytes.
  * The text is parsed back and checked, and the pak round trip must be byte-exact, before
    anything is written. Checks: duplicate keys, unique Index <= 127, keys <= 31 characters,
    every period id exists, each day's orders and reward mix, claim tails, retirement, and the
    exe's own period test: exactly one day's whole set displayable at every hour from now until
    the pre-loaded day ends (always more than the next 24 h), and around the next 00:00:00 reset
    today's set the second before, both sets for that one second, tomorrow's set from the second
    after (well past the next hourly run), and every day the next run drops already closed.
  * Install = write a temp file, fsync, chown happydungeons, chmod 600, os.replace, then
    manifest rev+1 the same way. Identical bytes = nothing written, no rev bump. On any failure
    the live file and the manifest are left as they were and the reason is logged.
  * It refuses to run if the game day goes backwards (a wrong clock would re-serve retired keys).

USAGE
  install (VPS cron, hourly; the 00:05 UTC run installs each new day, the others are no-ops):
      python3 roll_event_orders_v2.py
      crontab:  5 * * * * /usr/bin/python3 /opt/happydungeons/eventorder/roll_event_orders_v2.py >/dev/null 2>&1
  dry run (writes OUT and nothing else; never touches the live file, manifest, log or state):
      python3 roll_event_orders_v2.py --dry-run OUT.pak [--date 2026-10-05 | --date 2026-10-05T13:30]
                                      [--switch-day 2026-10-03] [--state-dir DIR] [--tail N]
      --date YYYY-MM-DD means that game day at 00:05 UTC (when the cron run installs it);
      --date YYYY-MM-DDTHH:MM[:SS] is an exact UTC moment. Default: now.
      --switch-day defaults to STATE_DIR/SWITCH_DAY if it exists, otherwise the game day itself
      (which is what a first install would do).
  timeline test (no files):    python3 roll_event_orders_v2.py --simulate [DAYS]
  sandbox install (tests the install path anywhere; paths go under DIR, no chown):
      python3 roll_event_orders_v2.py --root DIR [--date ...] [--template PAK]
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import traceback
from collections import Counter
from datetime import date, datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "event_order.template.pak")
NAME = "DungeonEventOrderParam.json"

DAY_START_H = 0                       # game day boundary, hours after 00:00 UTC (owner: reset at 00:00 UTC)
DAY_START = DAY_START_H * 10000       # the same, as HHMMSS (0)
CRON_MINUTE = 5                       # the hourly cron runs at hh:05; the daily install is the 00:05 run
TAIL_DAYS = 1                         # claim tail after a day's display window (owner: 1 day; file <= 84 orders)
PRELOAD_DAYS = 1                      # future days carried in the file
MAX_INDEX = 127                       # highest Index any shipped order table used
MAX_KEY_LEN = 31                      # keys are copied into a 0x20-char buffer (0x140EB09B8)
LEGACY_START = 20260925               # start date of E_LEGACY_D00/R00 (any past date works)
TEST_KEY = "E_M00_TEST"

# VPS layout (same paths as roll_event_orders.py)
DATA_ROOT = "/opt/happydungeons/data"
OWNER = "happydungeons"

# Fields copied unchanged from a template order into each day's order
COPIED = ["Condition", "ConditionOpt", "TargetValue", "Level", "RewardSetId", "RewardSetTextId",
          "TitleTextId", "DetailTextId", "DisplayProgressEnable"]
REQUIRED = ["Condition", "TargetValue", "Level", "RewardSetId", "TitleTextId", "DetailTextId"]
FIELD_ORDER = ["Enable", "Index", "SortNumber", "Condition", "ConditionOpt", "TargetValue", "Level",
               "RewardSetId", "RewardSetTextId", "TitleTextId", "DetailTextId",
               "DisplayPeriodId", "ReceivePeriodId", "CheckSaveDataEnable", "DisplayProgressEnable"]
SET_BY_ROLLER = {"Index", "SortNumber", "DisplayPeriodId", "ReceivePeriodId"}

sys.path.insert(0, HERE)
import hdpak  # noqa: E402


class BuildError(Exception):
    pass


def layout(root):
    serverdata = os.path.join(root, "serverdata")
    return {
        "live": os.path.join(serverdata, "event_order.pak"),
        "manifest": os.path.join(serverdata, "manifest.json"),
        "log": os.path.join(root, "event_order_roll.log"),
        "state": os.path.join(root, "event_order_days"),
        "lock": os.path.join(root, "event_order_roll.lock"),
    }


# ---------------------------------------------------------------- time

def game_day(now_utc):
    """The game day in force at now_utc: day d runs [d 00:00:00, d+1 00:00:00) UTC, i.e. the UTC date.
    (The reset second 00:00:00 itself belongs to the new day; see the overlap note above.)"""
    return (now_utc - timedelta(hours=DAY_START_H)).date()


def day_start(d):
    """The UTC moment game day d begins (its D00/R00 start, and the previous day's D00 end)."""
    return datetime(d.year, d.month, d.day, DAY_START_H, tzinfo=timezone.utc)


def ymd(d):
    return d.year * 10000 + d.month * 100 + d.day


def tag_of(d):
    return d.strftime("%y%m%d")


def parse_day(text):
    return datetime.strptime(text.strip(), "%Y-%m-%d").date()


def parse_when(text):
    """YYYY-MM-DD = that game day at 00:05 UTC (its daily install run); YYYY-MM-DDTHH:MM[:SS] = that UTC moment."""
    text = text.strip().replace(" ", "T").rstrip("Z")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return day_start(parse_day(text)) + timedelta(minutes=CRON_MINUTE)


def exe_period_active(p, t):
    """The exe's own period test, 0x140EB75E0: UTC, both ends include their exact second,
    an end date <= 0 means open-ended."""
    nd = t.year * 10000 + t.month * 100 + t.day
    nt = t.hour * 10000 + t.minute * 100 + t.second
    sd, st = p.get("DisplayStartDate", 0), p.get("DisplayStartTime", 0)
    ed, et = p.get("DisplayEndDate", 0), p.get("DisplayEndTime", 0)
    start_ok = sd < nd or (sd == nd and st <= nt)
    end_ok = ed <= 0 or ed > nd or (ed == nd and et >= nt)
    return start_ok and end_ok


def period(sd, st, ed, et):
    return {"DisplayStartDate": sd, "DisplayStartTime": st, "DisplayEndDate": ed, "DisplayEndTime": et}


def day_periods(d, tail):
    """(D00, R00) of game day d: display d 00:00 -> d+1 00:00, receive d 00:00 -> d+1+tail 00:00."""
    nxt = d + timedelta(days=1)
    return (period(ymd(d), DAY_START, ymd(nxt), DAY_START),
            period(ymd(d), DAY_START, ymd(nxt + timedelta(days=tail)), DAY_START))


def legacy_periods(switch_day, tail):
    """The old keys as a day SWITCH_DAY-1: display closed at SWITCH_DAY 00:00, claimable until SWITCH_DAY+tail 00:00."""
    return (period(LEGACY_START, 0, ymd(switch_day), DAY_START),
            period(LEGACY_START, 0, ymd(switch_day + timedelta(days=tail)), DAY_START))


# ---------------------------------------------------------------- JSON helpers

def loads_strict(text):
    """The game's JSON (// comments allowed), refusing duplicate keys anywhere."""
    def pairs(items):
        out = {}
        for k, v in items:
            if k in out:
                raise BuildError("duplicate key %r" % k)
            out[k] = v
        return out
    return json.loads(hdpak.strip_comments(text), object_pairs_hook=pairs)


def emit_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    raise BuildError("unexpected value %r" % (v,))


def emit_block(key, fields, order=None):
    keys = [f for f in (order or []) if f in fields] + [f for f in fields if not order or f not in order]
    lines = ['\t"%s":{' % key]
    for i, f in enumerate(keys):
        lines.append('\t\t"%s" : %s%s' % (f, emit_value(fields[f]), "," if i + 1 < len(keys) else ""))
    lines.append("\t}")
    return "\n".join(lines)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- template

class Template:
    def __init__(self, path=TEMPLATE):
        self.path = path
        with open(path, "rb") as fh:
            self.raw = fh.read()
        self.sha256 = sha256(self.raw)
        pak = hdpak.Pak.from_bytes(self.raw)
        self.names = pak.names()
        payload = pak.read_file(NAME)
        self.declared = pak.declared_size(NAME)
        self.text = hdpak.text_of(payload)
        self.suffix_bytes = payload[:self.declared][len(self.text.encode("utf-8")):]   # b"\0" in the template
        j = loads_strict(self.text)["JsonDungeonOrderParam"]
        if set(j) != {"eventOrder_", "eventOrderPeriod_", "optionList_"}:
            raise BuildError("template sections are %s" % sorted(j))
        self.orders, self.periods, self.options = j["eventOrder_"], j["eventOrderPeriod_"], j["optionList_"]

        # framing text, copied verbatim so the output looks like every shipped file
        m = re.match(r'(?s)(.*?"eventOrder_":\{\n)\t"', self.text)
        p = re.search(r'(?s)\n\},\n(// [^\n]*Period@Event Mission[^\n]*\n\n"eventOrderPeriod_":\{\n)\t"', self.text)
        o = re.search(r'(?s)\n\},\n// [^\n]*OptionList[^\n]*\n\n"optionList_":.*\Z', self.text)
        if not (m and p and o):
            raise BuildError("template text is not shaped like DungeonEventOrderParam.json")
        self.head, self.mid, self.tail = m.group(1), "\n},\n" + p.group(1), o.group(0)

        if TEST_KEY not in self.orders or self.orders[TEST_KEY].get("Enable") is not False:
            raise BuildError("template has no disabled %s" % TEST_KEY)
        self.test_order = self.orders[TEST_KEY]
        self.test_pid = self.test_order["DisplayPeriodId"]
        if self.test_pid not in self.periods:
            raise BuildError("template %s references missing period %s" % (TEST_KEY, self.test_pid))
        self.test_period = self.periods[self.test_pid]

        self.daily = [(k, v) for k, v in self.orders.items() if v.get("Enable") is True]
        if not self.daily:
            raise BuildError("template has no Enable=true order")
        if len(self.daily) > 100:
            raise BuildError("template has %d enabled orders; keys allow 100 a day" % len(self.daily))
        for k, v in self.daily:
            extra = set(v) - set(FIELD_ORDER)
            if extra:
                raise BuildError("template order %s has fields this roller does not know: %s" % (k, sorted(extra)))
            missing = [f for f in REQUIRED if f not in v]
            if missing:
                raise BuildError("template order %s lacks %s" % (k, missing))
        self.mix = Counter(v["RewardSetId"] for _, v in self.daily)

    def fresh_day(self, d):
        out = []
        for n, (_, src) in enumerate(self.daily):
            o = {"Enable": True}
            for f in COPIED:
                if f in src:
                    o[f] = src[f]
            o["CheckSaveDataEnable"] = False      # progress from the record, not lifetime stats
            out.append(["E_%s_%02d" % (tag_of(d), n), o])
        return out

    def fresh_legacy(self):
        return [[k, {f: v for f, v in src.items() if f not in SET_BY_ROLLER}] for k, src in self.daily]


# ---------------------------------------------------------------- state (frozen days, switch day)

class State:
    """STATE_DIR: SWITCH_DAY, legacy.json, <yyyymmdd>.json per built day, installed.json."""

    def __init__(self, path):
        self.path = path

    def _p(self, name):
        return os.path.join(self.path, name) if self.path else None

    def exists(self, name):
        p = self._p(name)
        return bool(p) and os.path.exists(p)

    def read_json(self, name):
        with open(self._p(name), encoding="utf-8") as fh:
            return json.load(fh)

    def switch_day(self):
        if not self.exists("SWITCH_DAY"):
            return None
        with open(self._p("SWITCH_DAY"), encoding="utf-8") as fh:
            return parse_day(fh.read())

    def installed(self):
        return self.read_json("installed.json") if self.exists("installed.json") else None

    def day(self, d):
        name = "%s.json" % d.strftime("%Y%m%d")
        if not self.exists(name):
            return None
        snap = self.read_json(name)
        orders = snap.get("orders") if isinstance(snap, dict) else None
        if snap.get("day") != d.isoformat() or not isinstance(orders, list) or not orders:
            raise BuildError("state file %s is not a day snapshot for %s" % (name, d))
        want = re.compile(r"^E_%s_\d\d$" % tag_of(d))
        allowed = set(COPIED) | {"Enable", "CheckSaveDataEnable"}
        seen = set()
        for item in orders:
            if not (isinstance(item, list) and len(item) == 2 and isinstance(item[1], dict)):
                raise BuildError("state file %s: bad entry %r" % (name, item))
            k, o = item
            if not want.match(k) or k in seen or set(o) - allowed or o.get("Enable") is not True:
                raise BuildError("state file %s: bad order %s" % (name, k))
            seen.add(k)
        return orders

    def legacy(self):
        if not self.exists("legacy.json"):
            return None
        orders = self.read_json("legacy.json").get("orders")
        if not isinstance(orders, list) or not orders:
            raise BuildError("state file legacy.json has no orders")
        return orders

    # writers (install only)
    def _write(self, name, data):
        os.makedirs(self.path, exist_ok=True)
        p = self._p(name)
        tmp = p + ".incoming"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, p)

    def write_json(self, name, obj):
        self._write(name, json.dumps(obj, ensure_ascii=False, indent=1) + "\n")

    def write_switch_day(self, d):
        self._write("SWITCH_DAY", d.isoformat() + "\n")


# ---------------------------------------------------------------- plan + build

class Plan:
    pass


def make_plan(now, tmpl, switch_day, state, tail=TAIL_DAYS):
    G = game_day(now)
    if G < switch_day:
        raise BuildError("game day %s is before SWITCH_DAY %s" % (G, switch_day))
    p = Plan()
    p.now, p.G, p.switch_day, p.tail = now, G, switch_day, tail
    first = max(G - timedelta(days=tail), switch_day)
    last = G + timedelta(days=PRELOAD_DAYS)
    p.days = [first + timedelta(days=n) for n in range((last - first).days + 1)]
    p.defs, p.new_days = {}, []
    for d in p.days:
        frozen = state.day(d)
        if frozen is None:
            p.defs[d] = tmpl.fresh_day(d)
            p.new_days.append(d)
        else:
            p.defs[d] = frozen
    # legacy keys behave like a day SWITCH_DAY-1: kept while G <= SWITCH_DAY-1+TAIL (TAIL 1: SWITCH_DAY only)
    p.legacy_on = G <= switch_day - timedelta(days=1) + timedelta(days=tail)
    p.legacy_new = False
    p.legacy = None
    if p.legacy_on:
        frozen = state.legacy() if state.switch_day() == switch_day else None
        if frozen is None:
            p.legacy, p.legacy_new = tmpl.fresh_legacy(), True
        else:
            p.legacy = frozen
    return p


def render(plan, tmpl):
    orders = [(TEST_KEY, dict(tmpl.test_order))]
    periods = [(tmpl.test_pid, dict(tmpl.test_period))]
    if plan.legacy_on:
        ld, lr = legacy_periods(plan.switch_day, plan.tail)
        periods += [("E_LEGACY_D00", ld), ("E_LEGACY_R00", lr)]
        for k, src in plan.legacy:
            o = dict(src)
            o["DisplayPeriodId"], o["ReceivePeriodId"] = "E_LEGACY_D00", "E_LEGACY_R00"
            orders.append((k, o))
    for d in plan.days:
        dp, rp = "E_%s_D00" % tag_of(d), "E_%s_R00" % tag_of(d)
        pd, pr = day_periods(d, plan.tail)
        periods += [(dp, pd), (rp, pr)]
        for k, src in plan.defs[d]:
            o = dict(src)
            o["DisplayPeriodId"], o["ReceivePeriodId"] = dp, rp
            orders.append((k, o))
    for n, (k, o) in enumerate(orders[1:], start=1):
        o["Index"] = o["SortNumber"] = n
    return (tmpl.head
            + ",\n".join(emit_block(k, v, FIELD_ORDER) for k, v in orders)
            + tmpl.mid
            + ",\n".join(emit_block(k, v) for k, v in periods)
            + tmpl.tail)


def verify(text, plan, tmpl):
    """Independent read-back of the built text. Raises BuildError; returns (orders, periods, warnings)."""
    j = loads_strict(text)["JsonDungeonOrderParam"]          # parses, no duplicate key anywhere
    eo, ep = j["eventOrder_"], j["eventOrderPeriod_"]
    if j["optionList_"] != tmpl.options:
        raise BuildError("optionList_ changed")
    if eo.get(TEST_KEY) != tmpl.test_order or ep.get(tmpl.test_pid) != tmpl.test_period:
        raise BuildError("%s or %s changed" % (TEST_KEY, tmpl.test_pid))
    for k in list(eo) + list(ep):
        if len(k) > MAX_KEY_LEN:
            raise BuildError("key too long (%d > %d): %s" % (len(k), MAX_KEY_LEN, k))
    for k, v in eo.items():
        for pid in (v.get("DisplayPeriodId"), v.get("ReceivePeriodId") or v.get("DisplayPeriodId")):
            if pid not in ep:
                raise BuildError("%s references missing period %s" % (k, pid))
    keys = [k for k in eo if k != TEST_KEY]
    idx = [eo[k]["Index"] for k in keys]
    if idx != list(range(1, len(keys) + 1)) or any(eo[k]["SortNumber"] != eo[k]["Index"] for k in keys):
        raise BuildError("Index/SortNumber are not 1..N in file order")
    if idx and max(idx) > MAX_INDEX:
        raise BuildError("Index would reach %d (> %d): lower TAIL_DAYS or the daily order count"
                         % (max(idx), MAX_INDEX))
    if [k for k in eo if not eo[k]["Enable"]] != [TEST_KEY]:
        raise BuildError("a disabled order other than %s is in the file" % TEST_KEY)

    want_keys, warnings = [], []
    if plan.legacy_on:
        ld, lr = legacy_periods(plan.switch_day, plan.tail)
        if ep.get("E_LEGACY_D00") != ld or ep.get("E_LEGACY_R00") != lr:
            raise BuildError("legacy periods wrong")
        if exe_period_active(ld, plan.now) or not exe_period_active(lr, plan.now):
            raise BuildError("legacy display must be closed and receive open at %s" % plan.now)
        for k, src in plan.legacy:
            got = {f: v for f, v in eo.get(k, {}).items() if f not in SET_BY_ROLLER}
            if got != src or (eo[k]["DisplayPeriodId"], eo[k]["ReceivePeriodId"]) != ("E_LEGACY_D00", "E_LEGACY_R00"):
                raise BuildError("legacy order %s differs from its definition" % k)
            want_keys.append(k)
    elif any(k.startswith("E_LEGACY") for k in ep):
        raise BuildError("legacy periods present after the switch-over window")

    expect_first = max(plan.G - timedelta(days=plan.tail), plan.switch_day)
    if plan.days[0] != expect_first or plan.days[-1] != plan.G + timedelta(days=PRELOAD_DAYS):
        raise BuildError("day window is %s..%s" % (plan.days[0], plan.days[-1]))
    for d in plan.days:
        if plan.G > d + timedelta(days=plan.tail):
            raise BuildError("day %s is past its claim tail and must not be served" % d)
        tag = tag_of(d)
        dp, rp = "E_%s_D00" % tag, "E_%s_R00" % tag
        pd, pr = day_periods(d, plan.tail)
        if ep.get(dp) != pd or ep.get(rp) != pr:
            raise BuildError("periods of %s are wrong" % d)
        if d <= plan.G and not exe_period_active(pr, plan.now):
            raise BuildError("R00 of %s is not open at %s" % (d, plan.now))
        day_keys = [k for k in eo if k.startswith("E_%s_" % tag) and k[-2:].isdigit()]
        if day_keys != [k for k, _ in plan.defs[d]]:
            raise BuildError("orders of %s are not the planned keys" % d)
        for k, src in plan.defs[d]:
            v = eo[k]
            if ({f: x for f, x in v.items() if f not in SET_BY_ROLLER} != src
                    or (v["DisplayPeriodId"], v["ReceivePeriodId"]) != (dp, rp)
                    or v["Enable"] is not True or v["CheckSaveDataEnable"] is not False):
                raise BuildError("order %s differs from its definition" % k)
        mix = Counter(eo[k]["RewardSetId"] for k in day_keys)
        if mix != tmpl.mix:
            if d in plan.new_days:
                raise BuildError("reward mix of %s is %s, template has %s" % (d, dict(mix), dict(tmpl.mix)))
            warnings.append("day %s was frozen with reward mix %s; the template now has %s"
                            % (d, dict(mix), dict(tmpl.mix)))
        want_keys += day_keys
    if keys != want_keys:
        raise BuildError("unexpected orders in the file: %s" % sorted(set(keys) ^ set(want_keys))[:5])

    # The exe's own period test, as a client that loaded this file would see it.
    def displayable(t):
        return [k for k, v in eo.items() if v["Enable"] and exe_period_active(ep[v["DisplayPeriodId"]], t)]

    def keys_of(*days):
        return [k for d in days for k, _ in plan.defs.get(d, [])]

    def expect(t, want, what):
        live = displayable(t)
        if live != want or not want:
            raise BuildError("%d orders displayable at %s, want %d (%s)"
                             % (len(live), t.strftime("%Y-%m-%d %H:%M:%S"), len(want), what))

    # 1) at every hour from now until the pre-loaded day's display ends (always more than the next
    #    24 h) exactly one game day's whole set is displayable - this file alone keeps the list full
    #    for a day even if every later run fails
    t, end = plan.now + timedelta(minutes=1), day_start(plan.days[-1] + timedelta(days=1))
    if end - plan.now <= timedelta(hours=24):
        raise BuildError("the file covers only until %s" % end)
    while t < end:
        d = game_day(t)
        if t == day_start(d):                 # a sample that lands on a reset second: see 2)
            expect(t, keys_of(d - timedelta(days=1), d), "both sets at the reset second %s" % t)
        else:
            expect(t, keys_of(d), "the %d of %s" % (len(keys_of(d)), d))
        t += timedelta(hours=1)
    # 2) the next reset, B = G+1 00:00:00 UTC. Tomorrow is already in this file, so a client that
    #    starts or rolls over between B and the next hourly run (B + CRON_MINUTE) has the new set.
    #    At B itself both ends are inclusive: today's and tomorrow's sets both pass for that one
    #    second. Harmless: different keys (nothing can be claimed twice), today is still inside its
    #    claim window, and nothing else is displayable.
    G1 = plan.G + timedelta(days=1)
    B = day_start(G1)
    one = timedelta(seconds=1)
    expect(B - one, keys_of(plan.G), "today's %d at the last second before the reset" % len(keys_of(plan.G)))
    expect(B, keys_of(plan.G, G1), "today's and tomorrow's sets for the reset second only")
    for s in [1] + list(range(60, (60 + CRON_MINUTE) * 60 + 1, 60)):
        expect(B + timedelta(seconds=s), keys_of(G1), "tomorrow's %d after the reset" % len(keys_of(G1)))
    for d in plan.days:
        if d >= plan.G and not exe_period_active(ep["E_%s_R00" % tag_of(d)], B):
            raise BuildError("R00 of %s is not open at the reset %s" % (d, B))
    # 3) whatever the next day's build drops must already be unclaimable one second after the reset
    #    (so no run after the reset, at 00:05 or by hand, can drop an order a player could still claim)
    next_first = max(G1 - timedelta(days=plan.tail), plan.switch_day)
    dropped = [ep["E_%s_R00" % tag_of(d)] for d in plan.days if d < next_first]
    if plan.legacy_on and G1 > plan.switch_day - timedelta(days=1) + timedelta(days=plan.tail):
        dropped.append(ep["E_LEGACY_R00"])
    for p in dropped:
        if exe_period_active(p, B + one):
            raise BuildError("a period the next build drops is still open after the reset: %s" % p)
    return eo, ep, warnings


def build(plan, tmpl):
    """(pak bytes, text, warnings) - checked, nothing written."""
    text = render(plan, tmpl)
    eo, ep, warnings = verify(text, plan, tmpl)
    pak = hdpak.Pak.from_bytes(tmpl.raw)
    payload = text.encode("utf-8") + tmpl.suffix_bytes
    pak.write_file(NAME, payload)
    blob = pak.to_bytes()
    back = hdpak.Pak.from_bytes(blob)
    if back.names() != tmpl.names:
        raise BuildError("pak round trip changed the file list")
    if back.declared_size(NAME) != len(payload) or back.read_file(NAME)[:len(payload)] != payload:
        raise BuildError("pak round trip changed the payload")
    if hdpak.text_of(back.read_file(NAME)) != text or back.to_bytes() != blob:
        raise BuildError("pak round trip is not byte-exact")
    plan.orders, plan.max_index = len(eo) - 1, max(v["Index"] for v in eo.values())
    return blob, text, warnings


def summary(plan):
    return "days %s..%s%s, %d orders (Index 1..%d)" % (
        plan.days[0], plan.days[-1],
        " + %d legacy" % len(plan.legacy) if plan.legacy_on else "", plan.orders, plan.max_index)


# ---------------------------------------------------------------- install (VPS cron)

class Lock:
    def __init__(self, path):
        self.path, self.fh = path, None

    def __enter__(self):
        try:
            import fcntl
        except ImportError:
            return self                       # Windows sandbox: no locking
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.fh = open(self.path, "a")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.fh.close()
            raise BuildError("another run holds %s" % self.path)
        return self

    def __exit__(self, *exc):
        if self.fh:
            self.fh.close()


def write_atomic(path, data, mode, owner, like=None):
    tmp = path + ".incoming"
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    if like and os.path.exists(like):
        shutil.copymode(like, tmp)
    if mode is not None:
        os.chmod(tmp, mode)
    if owner:
        shutil.chown(tmp, owner, owner)
    return tmp


def install(paths, tmpl_path, now=None, sandbox=False):
    def log(msg):
        line = "%s  %s" % (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), msg)
        print(line)
        try:
            os.makedirs(os.path.dirname(paths["log"]), exist_ok=True)
            with open(paths["log"], "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass

    now = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    owner = None if sandbox else OWNER
    try:
        with Lock(paths["lock"]):
            return _install(paths, tmpl_path, now, owner, log)
    except BuildError as e:
        log("FAILED - live file untouched: %s" % e)
        return 1


def _install(paths, tmpl_path, now, owner, log):
    state = State(paths["state"])
    G = game_day(now)
    try:
        tmpl = Template(tmpl_path)
        last = state.installed()
        if last and G < parse_day(last["game_day"]):
            raise BuildError("game day %s is before the last built day %s - clock went backwards?"
                             % (G, last["game_day"]))
        switch_day = state.switch_day() or G
        plan = make_plan(now, tmpl, switch_day, state)
        blob, text, warnings = build(plan, tmpl)
    except BuildError as e:
        log("FAILED to build - live file untouched: %s" % e)
        return 1
    except Exception:
        log("FAILED to build - live file untouched\n" + traceback.format_exc())
        return 1
    for w in warnings:
        log("note: " + w)

    # freeze the day definitions this build used (only after it passed every check, before it can be served)
    try:
        for d in plan.new_days:
            state.write_json("%s.json" % d.strftime("%Y%m%d"),
                             {"day": d.isoformat(), "template_sha256": tmpl.sha256,
                              "frozen_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "orders": plan.defs[d]})
    except Exception:
        log("FAILED to save the day definitions - live file untouched\n" + traceback.format_exc())
        return 1

    def freeze_switch():
        """SWITCH_DAY (+ the legacy definitions) are recorded once a file built with them is live."""
        try:
            if plan.legacy_on and plan.legacy_new:
                state.write_json("legacy.json", {"switch_day": switch_day.isoformat(), "orders": plan.legacy})
            if state.switch_day() is None:
                state.write_switch_day(switch_day)
                until = day_start(switch_day + timedelta(days=plan.tail))
                left = (until - now).total_seconds() / 3600
                log("switch-over: SWITCH_DAY = %s, the %d old keys stay claimable until %s UTC (%s)"
                    % (switch_day, len(plan.legacy or tmpl.daily), until.strftime("%Y-%m-%d %H:%M"),
                       "%.1f h from now" % left if left > 0 else "already closed"))
        except Exception as e:
            log("note: could not save SWITCH_DAY/legacy.json (%s)" % e)

    digest = sha256(blob)
    record = {"game_day": G.isoformat(), "sha256": digest, "utc": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
    live = None
    if os.path.exists(paths["live"]):
        with open(paths["live"], "rb") as fh:
            live = fh.read()
    last = state.installed()
    if live == blob and last and last.get("sha256") == digest:
        record["rev"] = last.get("rev")
        freeze_switch()
        try:
            state.write_json("installed.json", record)
        except OSError as e:
            log("note: could not update installed.json (%s)" % e)
        log("game day %s: unchanged (%s, sha256 %s)" % (G, summary(plan), digest[:16]))
        return 0
    # identical bytes without our record = the last install did not finish: install again (rev+1)
    if live is not None and last and sha256(live) != last.get("sha256"):
        log("WARNING: the live event_order.pak (sha256 %s) is not the one v2 installed last (%s) - "
            "is the v1 cron line still there?" % (sha256(live)[:16], last.get("sha256", "")[:16]))

    tmps = []
    try:
        with open(paths["manifest"], encoding="utf-8") as fh:
            m = json.load(fh)
        files = m.get("files") if isinstance(m, dict) else None
        if (not isinstance(m, dict) or not isinstance(m.get("rev"), int) or isinstance(m.get("rev"), bool)
                or not isinstance(files, list) or not all(isinstance(f, str) for f in files)):
            raise BuildError('manifest.json is not {"rev": <number>, "files": ["<file name>", ...]}')
        old_rev = m["rev"]
        if "event_order.pak" not in files:
            files.append("event_order.pak")
        m["rev"] = old_rev + 1
        mbytes = (json.dumps(m, indent=1) + "\n").encode("utf-8")
        tmps.append(write_atomic(paths["live"], blob, 0o600, owner))
        tmps.append(write_atomic(paths["manifest"], mbytes, None, owner, like=paths["manifest"]))
        os.replace(tmps[0], paths["live"])
        os.replace(tmps[1], paths["manifest"])
        tmps = []
    except Exception as e:
        for p in tmps:
            try:
                os.unlink(p)
            except OSError:
                pass
        log("FAILED to install - live file and manifest left as they were"
            + (": %s" % e if isinstance(e, BuildError) else "\n" + traceback.format_exc()))
        return 1
    record["rev"] = m["rev"]
    freeze_switch()
    try:
        state.write_json("installed.json", record)
    except OSError as e:
        log("note: could not update installed.json (%s) - the next run installs again" % e)
    log("game day %s: installed %s, rev %d -> %d, sha256 %s, text %s, template %s%s"
        % (G, summary(plan), old_rev, m["rev"], digest[:16], sha256(text.encode("utf-8"))[:16], tmpl.sha256[:8],
           ", new days %s" % ",".join(str(d) for d in plan.new_days) if plan.new_days else ""))
    return 0


# ---------------------------------------------------------------- dry run

def dry_run(out, tmpl_path, now, switch_day, state_dir, tail, live_path):
    now = now.replace(microsecond=0)
    tmpl = Template(tmpl_path)
    state = State(state_dir if state_dir and os.path.isdir(state_dir) else None)
    G = game_day(now)
    sw = switch_day or state.switch_day() or G
    plan = make_plan(now, tmpl, sw, state, tail)
    blob, text, warnings = build(plan, tmpl)
    if live_path and (os.path.realpath(out) == os.path.realpath(live_path) or
                      (os.path.exists(out) and os.path.exists(live_path) and os.path.samefile(out, live_path))):
        raise BuildError("--dry-run must not write the live file")
    tmp = out + ".incoming"
    with open(tmp, "wb") as fh:
        fh.write(blob)
    os.replace(tmp, out)
    print("dry run, now %s UTC -> game day %s, SWITCH_DAY %s, TAIL %d"
          % (now.strftime("%Y-%m-%d %H:%M:%S"), G, sw, tail))
    print("  %s" % summary(plan))
    print("  day definitions: %s" % ", ".join("%s %s" % (d, "new" if d in plan.new_days else "frozen")
                                               for d in plan.days))
    for d in plan.days:
        pd, pr = day_periods(d, tail)
        print("  %s: E_%s_00..%02d  display %d %06d -> %d %06d  receive -> %d %06d"
              % (d, tag_of(d), len(plan.defs[d]) - 1, pd["DisplayStartDate"], pd["DisplayStartTime"],
                 pd["DisplayEndDate"], pd["DisplayEndTime"], pr["DisplayEndDate"], pr["DisplayEndTime"]))
    if plan.legacy_on:
        ld, lr = legacy_periods(sw, tail)
        print("  legacy: %d old keys, display closed %d %06d, claimable until %d %06d"
              % (len(plan.legacy), ld["DisplayEndDate"], ld["DisplayEndTime"], lr["DisplayEndDate"],
                 lr["DisplayEndTime"]))
    for w in warnings:
        print("  note: " + w)
    print("  reward mix per day: %s" % ", ".join("%d x %s" % (n, r) for r, n in sorted(tmpl.mix.items())))
    print("  wrote %s: %d bytes, sha256 %s" % (out, len(blob), sha256(blob)))
    print("  JSON text sha256 %s (compare this across machines: the pak bytes depend on the zlib build)"
          % sha256(text.encode("utf-8")))
    if live_path and os.path.exists(live_path):
        with open(live_path, "rb") as fh:
            same = fh.read() == blob
        print("  compared with %s: %s" % (live_path, "IDENTICAL (an install now would change nothing)" if same
                                          else "different (an install now would replace it and raise the rev)"))
    return 0


# ---------------------------------------------------------------- offline timeline test

def simulate(n_days=21, switch_day=date(2026, 10, 3), tail=TAIL_DAYS, tmpl_path=TEMPLATE):
    """Server: hourly rebuild at hh:05. Client: loads the file at boot only, uses the exe's
    period test, creates a record for a displayable order with none, deletes records whose key
    is in no loaded table. Checks the properties the fix needs."""
    tmpl = Template(tmpl_path)
    nostate = State(None)
    cache = {}

    def served(t):
        b = t.replace(minute=CRON_MINUTE, second=0, microsecond=0)
        if b > t:
            b -= timedelta(hours=1)
        if game_day(b) < switch_day:
            return None
        key = (game_day(b), b.hour)
        if key not in cache:
            plan = make_plan(b, tmpl, switch_day, nostate, tail)
            _, text, _ = build(plan, tmpl)
            j = loads_strict(text)["JsonDungeonOrderParam"]
            cache[key] = (j["eventOrder_"], j["eventOrderPeriod_"])
        return cache[key]

    start = day_start(switch_day) + timedelta(minutes=30)
    removed, prev = set(), None
    max_orders = max_index = 0
    t = start
    while t < start + timedelta(days=n_days):           # 1) the files over time
        eo, ep = served(t)
        keys = set(eo)
        if keys & removed:
            raise AssertionError("key reused after removal at %s: %s" % (t, sorted(keys & removed)[:3]))
        if prev is not None:
            for k in set(prev[0]) - keys:
                v = prev[0][k]
                if v["Enable"] and exe_period_active(prev[1][v["ReceivePeriodId"]], t - timedelta(minutes=1)):
                    raise AssertionError("%s removed while still claimable at %s" % (k, t))
                removed.add(k)
        max_orders = max(max_orders, len(eo) - 1)
        max_index = max(max_index, max(v["Index"] for v in eo.values()))
        prev = (eo, ep)
        t += timedelta(hours=1)
    worst = timedelta(days=99)
    t = start
    while t < start + timedelta(days=n_days - 3):       # 2) a running client keeps a set >= ~24 h
        eo, ep = served(t)
        h = timedelta(0)
        while h < timedelta(hours=72):
            if not any(v["Enable"] and exe_period_active(ep[v["DisplayPeriodId"]], t + h) for v in eo.values()):
                break
            h += timedelta(minutes=30)
        worst = min(worst, h)
        t += timedelta(minutes=37)
    save = {"E_170922_00": {"state": 0x12, "prog": 100}, "E_180119_00": {"state": 2, "prog": 38}}
    fresh_per_day, max_records = [], 0
    for day in range(n_days):                           # 3) a daily player who claims everything
        boot = start + timedelta(days=day, hours=5)
        eo, ep = served(boot)
        for k in list(save):
            if k not in eo:
                del save[k]
        fresh = 0
        for k, v in eo.items():
            if v["Enable"] and exe_period_active(ep[v["DisplayPeriodId"]], boot):
                save.setdefault(k, {"state": 1, "prog": 0})
                if not save[k]["state"] & 0x10:
                    fresh += 1
                    save[k]["state"] |= 0x10
        fresh_per_day.append(fresh)
        max_records = max(max_records, len(save))
    legacy_left = [k for k in save if not re.match(r"E_\d{6}_\d\d$", k) or k[2:8] < tag_of(switch_day)]
    resets = 0
    for day in range(1, n_days):                        # 4) a client started around the 00:00:00 reset
        B = day_start(switch_day + timedelta(days=day))
        for off in (-1, 0, 1, 60, CRON_MINUTE * 60 - 1, CRON_MINUTE * 60, CRON_MINUTE * 60 + 60):
            boot = B + timedelta(seconds=off)
            eo, ep = served(boot)                       # off < 5 min: the file the 23:05 run built
            live = [k for k, v in eo.items() if v["Enable"] and exe_period_active(ep[v["DisplayPeriodId"]], boot)]
            g = game_day(boot)
            want = [g - timedelta(days=1), g] if off == 0 else [g]
            got = sorted({k[2:8] for k in live})
            if got != [tag_of(d) for d in want] or len(live) != len(tmpl.daily) * len(want):
                raise AssertionError("%d orders of %s displayable at %s, want the %s set(s)"
                                     % (len(live), got, boot, ",".join(str(d) for d in want)))
            resets += 1
    print("files: max %d orders, max Index %d, %d keys retired, none reused, none removed while claimable"
          % (max_orders, max_index, len(removed)))
    print("client coverage after boot: worst %s" % worst)
    print("daily player: fresh claimable orders per day %s" % sorted(set(fresh_per_day)))
    print("save records: max %d, legacy records left %s" % (max_records, legacy_left))
    print("reset %02d:00 UTC: %d boots from 1 s before to %d min after: the new day's set is always there "
          "(both sets only during the reset second itself)" % (DAY_START_H, resets, CRON_MINUTE + 1))
    assert worst >= timedelta(hours=23), worst
    assert set(fresh_per_day) == {len(tmpl.daily)}, fresh_per_day
    assert max_index <= MAX_INDEX and not legacy_left
    print("simulate: OK")
    return 0


# ---------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description="Event order roller v2 (new keys every game day).",
                                 epilog="With no option it installs into %s (VPS cron)." % DATA_ROOT)
    ap.add_argument("--dry-run", metavar="OUT", help="build and write OUT only; install nothing")
    ap.add_argument("--date", help="YYYY-MM-DD (that game day at 00:05 UTC) or YYYY-MM-DDTHH:MM[:SS] UTC")
    ap.add_argument("--switch-day", help="dry run: SWITCH_DAY to assume (YYYY-MM-DD)")
    ap.add_argument("--state-dir", help="dry run: day snapshots to read (default the VPS state dir)")
    ap.add_argument("--tail", type=int, default=TAIL_DAYS, help="dry run / simulate: claim tail days")
    ap.add_argument("--simulate", nargs="?", const=21, type=int, metavar="DAYS", help="offline timeline test")
    ap.add_argument("--root", help="sandbox install: use DIR instead of %s (no chown)" % DATA_ROOT)
    ap.add_argument("--template", default=TEMPLATE, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)

    if a.tail < 1:
        ap.error("--tail must be at least 1")
    if a.simulate is not None:
        return simulate(a.simulate, tail=a.tail, tmpl_path=a.template,
                        **({"switch_day": parse_day(a.switch_day)} if a.switch_day else {}))
    now = parse_when(a.date) if a.date else datetime.now(timezone.utc)
    if a.dry_run:
        try:
            return dry_run(a.dry_run, a.template, now,
                           parse_day(a.switch_day) if a.switch_day else None,
                           a.state_dir or layout(a.root or DATA_ROOT)["state"], a.tail,
                           layout(a.root or DATA_ROOT)["live"])
        except BuildError as e:
            print("dry run FAILED: %s" % e)
            return 1
    if a.switch_day or a.state_dir or a.tail != TAIL_DAYS:
        ap.error("--switch-day, --state-dir and --tail are for --dry-run/--simulate; install reads its state dir")
    if a.date and not a.root:
        ap.error("--date with an install needs --root (a sandbox); the real install always uses the clock")
    if a.template != TEMPLATE and not a.root:
        ap.error("--template with an install needs --root")
    return install(layout(a.root or DATA_ROOT), a.template, now if a.date else None, sandbox=bool(a.root))


if __name__ == "__main__":
    sys.exit(main())
