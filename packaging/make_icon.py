"""Draws the app icon (a redacted memo with a TOP SECRET stamp) and builds
packaging/icon.icns. Run from the project root:

    .venv/bin/python packaging/make_icon.py
"""

import os
import random
import shutil
import subprocess
import sys
import tempfile

import pymupdf as fitz

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from redactor.engine import _ink_polygon  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
STAMP_FONT = "/System/Library/Fonts/Supplemental/DIN Condensed Bold.ttf"
TYPE_FONT = "/System/Library/Fonts/Supplemental/Courier New Bold.ttf"


def rgb(h):
    return tuple(int(h[i:i + 2], 16) / 255 for i in (1, 3, 5))


def draw(path):
    random.seed(61)  # same icon every build
    doc = fitz.open()
    page = doc.new_page(width=1024, height=1024)
    sh = page.new_shape()

    # macOS icon grid: 824pt rounded square centred on a 1024 canvas
    tile = fitz.Rect(100, 100, 924, 924)
    sh.draw_rect(tile, radius=0.225)
    sh.finish(fill=rgb("#26302a"), color=None)

    # manila folder peeking out behind the memo
    sh.draw_rect(fitz.Rect(205, 250, 400, 320), radius=0.25)
    sh.finish(fill=rgb("#b9a46c"), color=None)
    sh.draw_rect(fitz.Rect(190, 290, 834, 820), radius=0.03)
    sh.finish(fill=rgb("#d8c48f"), color=None)

    # the memo, slightly askew
    paper = fitz.Rect(245, 215, 785, 860)
    tilt = fitz.Matrix(-4)
    centre = fitz.Point((paper.x0 + paper.x1) / 2, (paper.y0 + paper.y1) / 2)

    def rot(p):
        return (fitz.Point(p) - centre) * tilt + centre

    sh.draw_polyline([rot(p) for p in (paper.tl + (8, 10), paper.tr + (8, 10),
                                       paper.br + (8, 10), paper.bl + (8, 10))])
    sh.finish(fill=(0, 0, 0), fill_opacity=0.35, color=None, closePath=True)
    sh.draw_polyline([rot(p) for p in (paper.tl, paper.tr, paper.br, paper.bl)])
    sh.finish(fill=rgb("#f2ead3"), color=None, closePath=True)

    # typed lines, some of them struck through with ink
    y = paper.y0 + 120
    rows = [(0.0, 0.62, None), (0.0, 0.88, (0.18, 0.55)), (0.0, 0.8, None),
            (0.0, 0.92, (0.4, 0.92)), (0.0, 0.7, None), (0.0, 0.85, (0.0, 0.45)),
            (0.0, 0.6, None)]
    left, width = paper.x0 + 50, paper.width - 100
    for _start, end, bar in rows:
        line = fitz.Rect(left, y, left + width * end, y + 14)
        sh.draw_polyline([rot(p) for p in (line.tl, line.tr, line.br, line.bl)])
        sh.finish(fill=rgb("#8d8670"), color=None, closePath=True)
        if bar:
            r = fitz.Rect(left + width * bar[0], y - 14, left + width * bar[1], y + 30)
            pts = _ink_polygon(r, pad_x=2, pad_y=2, jitter=7, step=14)
            sh.draw_polyline([rot(p) for p in pts])
            sh.finish(fill=rgb("#121214"), color=None, closePath=True)
        y += 72

    sh.commit()

    # red TOP SECRET stamp across the lower half
    stamp = fitz.Rect(300, 610, 760, 760)
    st = fitz.Matrix(-14)
    sc = fitz.Point((stamp.x0 + stamp.x1) / 2, (stamp.y0 + stamp.y1) / 2)
    red = rgb("#a8231b")
    sh = page.new_shape()
    for inset, width in ((0, 16), (26, 5)):
        r = fitz.Rect(stamp.x0 + inset, stamp.y0 + inset, stamp.x1 - inset, stamp.y1 - inset)
        sh.draw_polyline([(fitz.Point(p) - sc) * st + sc for p in (r.tl, r.tr, r.br, r.bl)])
        sh.finish(color=red, width=width, closePath=True, stroke_opacity=0.9)
    sh.commit()
    if os.path.exists(STAMP_FONT):
        page.insert_font(fontname="din", fontfile=STAMP_FONT)
        font, fname = fitz.Font(fontfile=STAMP_FONT), "din"
    else:
        font, fname = fitz.Font("hebo"), "hebo"
    text, size = "TOP SECRET", 84
    tw = font.text_length(text, fontsize=size)
    origin = fitz.Point(sc.x - tw / 2, sc.y + size * 0.33)
    page.insert_text(origin, text, fontname=fname, fontsize=size, color=red,
                     fill_opacity=0.9, morph=(sc, fitz.Matrix(14)))  # text space is y-up

    pix = page.get_pixmap(alpha=True)
    pix.save(path)


def build_ico(png, ico):
    """Windows icon. Needs Pillow."""
    from PIL import Image
    Image.open(png).save(ico, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
                                     (128, 128), (256, 256)])


def build_icns(png, icns):
    tmp = tempfile.mkdtemp()
    iconset = os.path.join(tmp, "icon.iconset")
    os.makedirs(iconset)
    for size in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            px = size * scale
            name = "icon_%dx%d%s.png" % (size, size, "@2x" if scale == 2 else "")
            subprocess.run(["sips", "-z", str(px), str(px), png, "--out",
                            os.path.join(iconset, name)], check=True, capture_output=True)
    subprocess.run(["iconutil", "-c", "icns", iconset, "-o", icns], check=True)
    shutil.rmtree(tmp)


if __name__ == "__main__":
    png = os.path.join(HERE, "icon.png")
    draw(png)
    build_icns(png, os.path.join(HERE, "icon.icns"))
    build_ico(png, os.path.join(HERE, "icon.ico"))
    print("wrote icon.png, icon.icns and icon.ico in", HERE)
