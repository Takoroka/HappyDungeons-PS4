"""Set the portable shadPS4's volume (General.volume_slider, 0-100). Usage: py -3 -I set_volume.py 100"""
import json
import os
import sys

cfg = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shadps4", "user", "config.json")
level = max(0, min(100, int(sys.argv[1])))
with open(cfg, encoding="utf-8") as f:
    c = json.load(f)
if c.get("General", {}).get("volume_slider") != level:
    c.setdefault("General", {})["volume_slider"] = level
    with open(cfg, "w", encoding="utf-8") as f:
        json.dump(c, f, indent=4)
