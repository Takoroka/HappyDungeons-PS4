"""Edit a portable shadPS4 config.json: py -3 -I set_cfg.py <profile folder> Section.key=value ...
Values are parsed as JSON when possible (true, 1920, 0.5), else kept as strings. Prints the changed keys."""
import json
import os
import sys

profile = sys.argv[1]
cfg = os.path.join(profile, "user", "config.json") if os.path.isdir(os.path.join(profile, "user")) else profile
with open(cfg, encoding="utf-8") as f:
    c = json.load(f)
for item in sys.argv[2:]:
    key, _, raw = item.partition("=")
    section, _, name = key.partition(".")
    try:
        value = json.loads(raw)
    except ValueError:
        value = raw
    c.setdefault(section, {})[name] = value
    print("%s.%s = %r" % (section, name, value))
with open(cfg, "w", encoding="utf-8") as f:
    json.dump(c, f, indent=4)
