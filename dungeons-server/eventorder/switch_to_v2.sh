#!/bin/bash
# One-time switch from the v1 event-order roller to v2, run by root's cron at 2026-10-03 00:07 UTC
# (two minutes after v1's last 00:05 run). It swaps the cron lines, runs v2 once, logs, and removes itself.
set -u
LOG=/opt/happydungeons/data/event_order_roll.log
ERR=/opt/happydungeons/data/event_order_roll.cron.err
V2LINE='5 * * * * /usr/bin/python3 /opt/happydungeons/eventorder/roll_event_orders_v2.py >>/opt/happydungeons/data/event_order_roll.cron.err 2>&1'
stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }

TMP=$(mktemp)
crontab -l > "$TMP.old" 2>/dev/null || true
# drop the v1 line ("roll_event_orders.py " - the v2 name does not match) and this one-shot line
grep -v -F 'roll_event_orders.py ' "$TMP.old" | grep -v -F 'switch_to_v2.sh' > "$TMP"
grep -q -F 'roll_event_orders_v2.py' "$TMP" || echo "$V2LINE" >> "$TMP"
if crontab "$TMP"; then
    echo "$(stamp)  switch: cron now runs roll_event_orders_v2.py hourly at :05 (v1 line removed)" >> "$LOG"
else
    echo "$(stamp)  switch: FAILED to install the new crontab - nothing changed, v1 still scheduled" >> "$LOG"
    rm -f "$TMP" "$TMP.old"; exit 1
fi
cp "$TMP.old" /opt/happydungeons/data/crontab.before-v2-switch.txt
rm -f "$TMP" "$TMP.old"

/usr/bin/python3 /opt/happydungeons/eventorder/roll_event_orders_v2.py >> "$ERR" 2>&1
rc=$?
echo "$(stamp)  switch: first v2 run exit=$rc" >> "$LOG"
exit $rc
