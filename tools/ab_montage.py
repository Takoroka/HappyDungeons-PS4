"""Side-by-side grid of the A/B timing runs: one row per run, one column per offset after the cross press.
Usage: py -3 -I ab_montage.py out.png runs\\ab_A... runs\\ab_B... runs\\ab_C..."""
import glob
import os
import re
import sys
from PIL import Image, ImageDraw

out, dirs = sys.argv[1], sys.argv[2:]
rows = []
for d in dirs:
    shots = sorted(glob.glob(os.path.join(d, "x*_*.png")), key=lambda p: int(re.search(r"x(\d+)_", os.path.basename(p)).group(1)))
    rows.append((os.path.basename(d), shots))
cols = max(len(s) for _, s in rows)
W, H, LBL = 400, 225, 26
img = Image.new("RGB", (cols * W, len(rows) * (H + LBL)), "black")
dr = ImageDraw.Draw(img)
for r, (name, shots) in enumerate(rows):
    y = r * (H + LBL)
    for c, p in enumerate(shots):
        im = Image.open(p).convert("RGB").resize((W, H))
        img.paste(im, (c * W, y + LBL))
        dr.text((c * W + 6, y + 6), "%s  +%ss" % (name, re.search(r"x(\d+)_", os.path.basename(p)).group(1)), fill="white")
img.save(out)
print(out)
