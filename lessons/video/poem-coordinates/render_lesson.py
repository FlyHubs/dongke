#!/usr/bin/env python3
"""Draw EVERY video frame using Pillow + NumPy; encode with FFmpeg.

No browser, SVG renderer, slide software, or generative video is used.
An existing narration file and its matching timeline allow an offline rerender.
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import shutil
import subprocess
import time
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
FONT_DIR = None
STORY = json.loads((ROOT / "storyboard.json").read_text(encoding="utf-8"))
TIME = json.loads((ROOT / "timeline.json").read_text(encoding="utf-8"))
W, H, FPS = STORY["width"], STORY["height"], STORY["fps"]
DURATION = TIME["total_duration"]
SCENES = TIME["scenes"]
STARTS = [s["start"] for s in SCENES]
LINES = [ln for s in SCENES for ln in s["lines"]]
LINE_STARTS = [ln["start"] for ln in LINES]
GX, GY, CELL = 206, 343, 82
GW, GH = CELL * 9, CELL * 6
RX, RY, RW, RH = 1082, 298, 774, 622

INK = "#17394B"
MUTED = "#607986"
COL = "#008A8C"
COL_PALE = "#DDF4EF"
ROW = "#C67B1D"
ROW_PALE = "#FFF0CF"
PINK = "#D34C61"
PINK_PALE = "#FFE6E9"
GRID = "#A8B8BF"
LINE = "#DCE6E9"
WHITE = "#FFFFFF"


@lru_cache(maxsize=128)
def font(size: int, bold: bool = False):
    suffix = "Bold" if bold else "Regular"
    directories = ([Path(FONT_DIR)] if FONT_DIR else []) + [
        ROOT / "assets", Path.home() / ".local/share/fonts",
        Path.home() / "Library/Fonts", Path("/Library/Fonts"),
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts",
        Path("/usr/share/fonts/opentype/noto"),
    ]
    for directory in directories:
        for filename, index in [(f"NotoSansCJKsc-{suffix}.otf", 0),
                                (f"NotoSansCJK-{suffix}.ttc", 2)]:
            candidate = directory / filename
            if candidate.is_file():
                # Face 2 of the standard Noto CJK collection is Simplified Chinese.
                return ImageFont.truetype(str(candidate), size, index=index)
    raise FileNotFoundError(
        "Noto Sans CJK SC fonts not found. Install the Regular/Bold fonts or "
        "pass --font-dir /path/to/fonts (see video-lessons/README.md).")


def configure(timeline_path=None, font_dir=None):
    """Select generated timing/fonts without changing the lesson's artwork."""
    global TIME, DURATION, SCENES, STARTS, LINES, LINE_STARTS, FONT_DIR
    if timeline_path is not None:
        TIME = json.loads(Path(timeline_path).read_text(encoding="utf-8"))
    DURATION = TIME["total_duration"]
    SCENES = TIME["scenes"]
    STARTS = [s["start"] for s in SCENES]
    LINES = [line for scene in SCENES for line in scene["lines"]]
    LINE_STARTS = [line["start"] for line in LINES]
    FONT_DIR = font_dir
    for cached in (font, base_frame, poem_layer, right_panel, mini_board, subtitle_layer):
        cached.cache_clear()


def txt(d, xy, value, size=36, fill=INK, bold=False, anchor="lt"):
    d.text(xy, str(value), font=font(size, bold), fill=fill, anchor=anchor)


def center(d, xy, value, size=36, fill=INK, bold=False):
    txt(d, xy, value, size, fill, bold, "mm")


def smooth(v):
    v = float(np.clip(v, 0.0, 1.0))
    return v * v * (3.0 - 2.0 * v)


def between(t, a, b):
    return smooth((t - a) / max(b - a, 0.001))


def mix(a, b, amount):
    def rgb(c):
        return np.array([int(c[i:i + 2], 16) for i in (1, 3, 5)], dtype=float)
    return tuple(np.rint(rgb(a) * (1 - amount) + rgb(b) * amount).astype(int))


def arrow(d, p0, p1, fill=COL, width=5, head=14, progress=1.0):
    if progress <= 0:
        return
    start = np.asarray(p0, dtype=float)
    end = start + (np.asarray(p1, dtype=float) - start) * progress
    delta = end - start
    length = float(np.linalg.norm(delta))
    if length < 2:
        return
    u = delta / length
    n = np.array([-u[1], u[0]])
    d.line([tuple(start), tuple(end)], fill=fill, width=width)
    triangle = [end, end - u * head + n * head * .56, end - u * head - n * head * .56]
    d.polygon([tuple(p) for p in triangle], fill=fill)


def cell_center(col, row, shift=0):
    return GX + (col - .5 + shift) * CELL, GY + (6 - row + .5) * CELL


def cell_box(col, row, shift=0, pad=4):
    x, y = cell_center(col, row, shift)
    return (x - CELL / 2 + pad, y - CELL / 2 + pad,
            x + CELL / 2 - pad, y + CELL / 2 - pad)


def dashed_rect(d, box, fill, width=3, dash=10):
    x0, y0, x1, y1 = map(int, box)
    for x in range(x0, x1, dash * 2):
        d.line((x, y0, min(x + dash, x1), y0), fill=fill, width=width)
        d.line((x, y1, min(x + dash, x1), y1), fill=fill, width=width)
    for y in range(y0, y1, dash * 2):
        d.line((x0, y, x0, min(y + dash, y1)), fill=fill, width=width)
        d.line((x1, y, x1, min(y + dash, y1)), fill=fill, width=width)


def make_background():
    # NumPy computes a very light two-dimensional colour gradient once.
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    weight = np.clip(.7 * yy / H + .3 * xx / W, 0, 1)[..., None]
    top = np.array([240, 247, 249], dtype=np.float32)
    bottom = np.array([250, 249, 242], dtype=np.float32)
    pixels = (top * (1 - weight) + bottom * weight).astype(np.uint8)
    return Image.fromarray(pixels)


BACKGROUND = make_background()


@lru_cache(maxsize=32)
def base_frame(scene_id):
    s = next(s for s in SCENES if s["id"] == scene_id)
    im = BACKGROUND.copy()
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((64, 43, 73, 118), radius=4, fill=COL)
    txt(d, (92, 42), "古诗里的位置", 54, INK, True)
    txt(d, (95, 112), "《赠汪伦》中的有序数对与平移", 27, MUTED)
    d.rounded_rectangle((1544, 60, 1856, 116), radius=28, fill=WHITE, outline=LINE, width=2)
    center(d, (1700, 87), "数学 × 古诗", 28, COL, True)
    tabs = ["认识列行", "定位“舟”", "寻找“千”", "向左平移", "答案回顾"]
    for i, label in enumerate(tabs):
        x = 64 + i * 362
        active = i == s["chapter"]
        done = i < s["chapter"]
        d.rounded_rectangle((x, 171, x + 344, 220), radius=16,
                            fill=COL if active else WHITE,
                            outline=COL if active else LINE, width=1)
        d.ellipse((x + 20, 181, x + 49, 210), fill=WHITE if active else COL_PALE)
        center(d, (x + 34.5, 195.5), str(i + 1), 18, COL, True)
        txt(d, (x + 66, 181), label, 26, WHITE if active else (COL if done else MUTED), True)
    txt(d, (83, 245), s["title"], 35, INK, True)
    txt(d, (1720, 253), f'{SCENES.index(s)+1:02d} / {len(SCENES):02d}', 24, MUTED)
    if scene_id != "recap":
        d.rounded_rectangle((64, 298, 1050, 920), radius=26, fill=WHITE, outline=LINE, width=2)
        d.rounded_rectangle((RX, RY, RX + RW, RY + RH), radius=26, fill=WHITE, outline=LINE, width=2)
    d.rounded_rectangle((64, 946, 1856, 1032), radius=22, fill=INK)
    d.rounded_rectangle((87, 967, 175, 1011), radius=12, fill="#2B5061")
    center(d, (131, 989), "讲解", 24, "#DAEDEF", True)
    d.rounded_rectangle((64, 1053, 1856, 1060), radius=3, fill="#DDE7E9")
    return im


@lru_cache(maxsize=1)
def poem_layer():
    # Transparent padding allows all characters to translate together.
    im = Image.new("RGBA", (GW + CELL * 2, GH + CELL * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    for row, letters in STORY["grid"].items():
        for col, char in enumerate(letters, start=1):
            if char:
                x = CELL + (col - .5) * CELL
                y = CELL + (6 - int(row) + .5) * CELL
                center(d, (x, y), char, 45, INK, int(row) == 6)
    return im


def draw_board(im, t, s):
    d = ImageDraw.Draw(im)
    sid = s["id"]
    col = row = None
    target = None
    shift = 0.0
    a = s["lines"][0]["start"]
    b = s["lines"][-1]["end"]
    state = 0

    if sid == "columns":
        p = between(t, a + .5, b - .5)
        col = min(9, int(p * 9) + 1)
        state = col
    elif sid == "rows":
        p = between(t, a + .5, b - .5)
        row = min(6, int(p * 6) + 1)
        state = row
    elif sid.startswith("boat"):
        col = 5
        row = 4 if sid != "boat_col" else None
        target = (5, 4, "舟")
    elif sid.startswith("seven"):
        col = 7
        row = 2 if sid != "seven_col" else None
        if sid == "seven_answer":
            target = (7, 2, "千")
    elif sid.startswith("shift"):
        row = 6
        if sid == "shift_start":
            col = 4
            target = (4, 6, "赠")
        elif sid == "shift_move":
            l0, l1 = s["lines"]
            shift = -between(t, l0["start"] + l0["duration"] * .45, l1["end"] - .45)
            target = (4, 6, "赠")
            state = 1 if shift < -.985 else 0
        else:
            col = 3
            shift = -1.0
            target = (4, 6, "赠")

    if col:
        x = GX + (col - 1) * CELL
        d.rectangle((x + 1, GY + 1, x + CELL - 1, GY + GH - 1), fill=COL_PALE)
    if row:
        y = GY + (6 - row) * CELL
        d.rectangle((GX + 1, y + 1, GX + GW - 1, y + CELL - 1), fill=ROW_PALE)
    if col and row:
        d.rectangle(cell_box(col, row, pad=1), fill="#DDECCC")
    for c in range(10):
        x = GX + c * CELL
        d.line((x, GY, x, GY + GH), fill=GRID, width=2)
    for r in range(7):
        y = GY + r * CELL
        d.line((GX, y, GX + GW, y), fill=GRID, width=2)
    d.rectangle((GX, GY, GX + GW, GY + GH), outline="#6F8792", width=3)

    for c in range(1, 10):
        x, _ = cell_center(c, 1)
        active = c == col
        if active:
            d.rounded_rectangle((x - 27, 848, x + 27, 900), radius=14, fill=COL)
        center(d, (x, 874), c, 34, WHITE if active else (COL if sid == "rule" else MUTED), active)
    for r in range(1, 7):
        _, y = cell_center(1, r)
        active = r == row
        if active:
            d.rounded_rectangle((137, y - 26, 187, y + 26), radius=14, fill=ROW)
        center(d, (162, y), r, 34, WHITE if active else (ROW if sid == "rule" else MUTED), active)
    center(d, (979, 875), "列", 27, COL, True)
    center(d, (163, 319), "行", 25, ROW, True)

    pl = poem_layer()
    # Fractional time becomes a new pixel position every frame.
    im.paste(pl, (round(GX - CELL + shift * CELL), GY - CELL), pl)
    d = ImageDraw.Draw(im)

    if target:
        c, r, char = target
        box = cell_box(c, r, shift)
        d.rounded_rectangle(box, radius=13, fill=PINK_PALE, outline=PINK, width=4)
        center(d, cell_center(c, r, shift), char, 50, PINK, True)
    if sid == "columns":
        first = cell_center(1, 1)[0]
        end = cell_center(col, 1)[0]
        arrow(d, (first - 17, 909), (max(first + 20, end + 18), 909), COL, 4, 10)
    if sid == "rows":
        arrow(d, (110, cell_center(1, 1)[1] + 19),
              (110, cell_center(1, row)[1] - 20), ROW, 4, 11)
    if sid == "boat_col":
        x = GX + 5 * CELL - 12
        arrow(d, (x, cell_center(5, 4)[1] + 22), (x, GY + GH - 10), COL,
              progress=between(t, a + .6, s["lines"][0]["end"]))
    if sid == "boat_row":
        y = cell_center(5, 4)[1] + CELL / 2 - 12
        arrow(d, (cell_center(5, 4)[0] - 25, y), (GX + 10, y), ROW,
              progress=between(t, a + .3, s["lines"][0]["end"]))
    if sid == "seven_col":
        x = GX + 7 * CELL - 12
        arrow(d, (x, GY + GH - 12), (x, GY + 12), COL,
              progress=between(t, a + .5, b - .3))
    if sid == "seven_row":
        y = cell_center(7, 2)[1] + CELL / 2 - 12
        arrow(d, (GX + 12, y), (cell_center(7, 2)[0] - 20, y), ROW,
              progress=between(t, a + .3, b - .3))
        if t > a + (b - a) * .6:
            d.rounded_rectangle(cell_box(7, 2), radius=13, outline=PINK, width=4)
    if sid == "shift_move":
        dashed_rect(d, cell_box(4, 6, pad=2), "#98AAB2", width=2, dash=7)
        x0 = cell_center(4, 6)[0]
        x1 = cell_center(3, 6)[0]
        arrow(d, (x0, 322), (x1, 322), PINK, 4, 10)
        txt(d, (x0 + 29, 310), "向左 1 格", 23, PINK, True)
    return state


def pair(d, cx, cy, first, second, size=78, first_color=COL, second_color=ROW):
    # Fixed fields preserve visual order, even when the strings change.
    center(d, (cx - 194, cy), "（", size, INK, True)
    center(d, (cx - 87, cy), first, size, first_color, True)
    center(d, (cx + 6, cy + 3), "，", size, INK, True)
    center(d, (cx + 99, cy), second, size, second_color, True)
    center(d, (cx + 205, cy), "）", size, INK, True)


def panel_header(d, tag, heading, heading_size=44):
    txt(d, (44, 35), tag, 24, COL, True)
    txt(d, (44, 82), heading, heading_size, INK, True)
    d.line((44, 153, RW - 44, 153), fill=LINE, width=2)


@lru_cache(maxsize=128)
def right_panel(sid, state=0):
    im = Image.new("RGBA", (RW, RH), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    mid = RW / 2
    if sid == "intro":
        panel_header(d, "今天学什么", "用两个数，说清位置")
        d.rounded_rectangle((44, 183, RW - 44, 293), radius=22, fill=COL_PALE)
        center(d, (mid, 237), "先列后行", 62, COL, True)
        for n, line in enumerate(["找“舟”的位置", "找到（7，2）的字", "让整首诗向左平移"], 1):
            yy = 355 + (n - 1) * 77
            d.ellipse((49, yy - 20, 91, yy + 22), fill=COL_PALE)
            center(d, (70, yy), n, 24, COL, True)
            txt(d, (112, yy - 19), line, 34, INK)
    elif sid in ("columns", "rows"):
        iscol = sid == "columns"
        colour = COL if iscol else ROW
        pale = COL_PALE if iscol else ROW_PALE
        noun = "列" if iscol else "行"
        panel_header(d, "看清方格外的标号", f"{noun}，看{'下方' if iscol else '左侧'}")
        center(d, (mid, 216), "从左往右数" if iscol else "从下往上数", 45, colour, True)
        if iscol:
            for n in range(1, 10):
                x = 87 + (n - 1) * 75
                d.rounded_rectangle((x - 28, 291, x + 28, 344), radius=12,
                                    fill=colour if n == state else pale)
                center(d, (x, 317), n, 28, WHITE if n == state else colour, True)
            arrow(d, (81, 365), (696, 365), colour, 4, 11)
        else:
            d.rounded_rectangle((72, 273, 292, 376), radius=18, fill=pale)
            d.rounded_rectangle((483, 273, 703, 376), radius=18, fill=pale)
            center(d, (182, 304), "最下面", 25, MUTED)
            center(d, (182, 347), "第 1 行", 34, colour, True)
            center(d, (593, 304), "最上面", 25, MUTED)
            center(d, (593, 347), "第 6 行", 34, colour, True)
            arrow(d, (335, 354), (438, 287), colour, 5, 13)
        center(d, (mid, 459), f"第 {state or 1} {noun}", 86, colour, True)
        center(d, (mid, 560), "跟着高亮的格子一起数", 28, MUTED)
    elif sid == "rule":
        panel_header(d, "最重要的规则", "有序数对，先列后行")
        d.rounded_rectangle((44, 189, 731, 344), radius=20, fill="#F3F7F8")
        center(d, (103, 267), "（", 74, INK, True)
        center(d, (264, 267), "列数", 68, COL, True)
        center(d, (407, 270), "，", 70, INK, True)
        center(d, (543, 267), "行数", 68, ROW, True)
        center(d, (691, 267), "）", 74, INK, True)
        for x, colour, pale, line1, line2 in [
            (44, COL, COL_PALE, "第一个数", "表示第几列"),
            (407, ROW, ROW_PALE, "第二个数", "表示第几行")]:
            d.rounded_rectangle((x, 383, x + 324, 511), radius=19, fill=pale)
            center(d, (x + 162, 425), line1, 35, colour, True)
            center(d, (x + 162, 474), line2, 28, colour)
        center(d, (mid, 568), "两个数的位置，不能随意交换", 29, MUTED)
    elif sid in ("boat_col", "boat_row"):
        iscol = sid == "boat_col"
        panel_header(d, "第（1）题 · 从字找数对", "先看“舟”在哪一列" if iscol else "再看“舟”在哪一行", 42)
        center(d, (mid, 223), "第 5 列" if iscol else "第 5 列  ·  第 4 行", 47,
               COL if iscol else INK, True)
        d.rounded_rectangle((44, 288, 730, 438), radius=22, fill="#F3F7F8")
        pair(d, mid, 362, "5", "？" if iscol else "4", 79,
             second_color=MUTED if iscol else ROW)
        center(d, (mid, 496), "对齐下方的列号 5" if iscol else "对齐左侧的行号 4", 34,
               COL if iscol else ROW, True)
        center(d, (mid, 568), "先确定第一个数" if iscol else "再确定第二个数", 29, MUTED)
    elif sid == "boat_answer":
        panel_header(d, "第（1）题 · 第一处答案", "“舟”在第 5 列、第 4 行", 39)
        d.rounded_rectangle((mid - 65, 187, mid + 65, 317), radius=24, fill=PINK_PALE)
        center(d, (mid, 252), "舟", 85, PINK, True)
        pair(d, mid, 419, "5", "4", 94)
        center(d, (mid - 90, 510), "列数", 30, COL, True)
        center(d, (mid + 98, 510), "行数", 30, ROW, True)
        center(d, (mid, 572), "第 5 列  ∩  第 4 行", 29, MUTED)
    elif sid.startswith("seven"):
        answer = sid == "seven_answer"
        heading = "先找到第 7 列" if sid == "seven_col" else ("再找到第 2 行" if not answer else "交叉处的字是“千”")
        panel_header(d, "第（1）题 · 从数对找字", heading)
        pair(d, mid, 246, "7", "2", 83)
        center(d, (mid - 90, 324), "先看列", 28, COL, True)
        center(d, (mid + 96, 324), "再看行", 28, ROW, True)
        if answer:
            d.rounded_rectangle((mid - 75, 379, mid + 75, 529), radius=24, fill=PINK_PALE)
            center(d, (mid, 451), "千", 108, PINK, True)
            center(d, (mid, 576), "（7，2）对应“千”", 32, INK, True)
        else:
            colour = COL if sid == "seven_col" else ROW
            pale = COL_PALE if sid == "seven_col" else ROW_PALE
            d.rounded_rectangle((44, 390, 730, 516), radius=20, fill=pale)
            center(d, (mid, 453), "第 7 列" if sid == "seven_col" else "第 2 行", 66, colour, True)
            center(d, (mid, 573), "先看第一个数：7" if sid == "seven_col" else "再看第二个数：2", 30, MUTED)
    elif sid == "shift_start":
        panel_header(d, "第（2）题 · 准备平移", "先找“赠”的原位置")
        d.rounded_rectangle((mid - 63, 186, mid + 63, 312), radius=24, fill=PINK_PALE)
        center(d, (mid, 246), "赠", 84, PINK, True)
        pair(d, mid, 408, "4", "6", 88)
        center(d, (mid, 504), "第 4 列  ·  第 6 行", 35, INK, True)
        center(d, (mid, 571), "下一步：整首诗向左移动 1 格", 29, MUTED)
    elif sid == "shift_move":
        panel_header(d, "第（2）题 · 观察移动", "向左平移 1 格")
        d.rounded_rectangle((44, 182, 730, 257), radius=17, fill=COL_PALE)
        center(d, (mid, 219), "网格、标号保持固定", 33, COL, True)
        center(d, (mid, 310), "标题、作者、诗句一起移动", 30, MUTED)
        # Destination is drawn to the left of the original position.
        for x, colour, pale, value, label in [(194, COL, COL_PALE, "3", "平移后"),
                                             (582, MUTED, "#EFF3F5", "4", "平移前")]:
            d.rounded_rectangle((x - 111, 363, x + 111, 526), radius=22, fill=pale)
            center(d, (x, 398), label, 27, colour)
            center(d, (x, 465), f"第 {value} 列", 47, colour, True)
        arrow(d, (449, 443), (326, 443), PINK, 6, 16)
        center(d, (mid, 574), "第 6 行，始终不变", 33, ROW, True)
    elif sid == "shift_rule":
        panel_header(d, "第（2）题 · 写出新位置", "列数减 1，行数不变")
        d.rounded_rectangle((44, 184, 730, 280), radius=18, fill=COL_PALE)
        center(d, (mid, 230), "列数：4 − 1 = 3", 45, COL, True)
        d.rounded_rectangle((44, 302, 730, 397), radius=18, fill=ROW_PALE)
        center(d, (mid, 349), "行数：6 保持不变", 43, ROW, True)
        pair(d, mid, 478, "3", "6", 82)
        center(d, (mid, 576), "左移后的“赠”： （3，6）", 32, INK, True)
    return im


@lru_cache(maxsize=12)
def mini_board(which):
    c, gx, gy = 35, 44, 18
    im = Image.new("RGBA", (409, 270), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    tc, tr, shift = [(5, 4, 0), (7, 2, 0), (3, 6, -1)][which]
    for col in range(10):
        x = gx + col * c
        d.line((x, gy, x, gy + 6 * c), fill=GRID, width=1)
    for row in range(7):
        y = gy + row * c
        d.line((gx, y, gx + 9 * c, y), fill=GRID, width=1)
    d.rectangle((gx + (tc - 1) * c + 1, gy + (6 - tr) * c + 1,
                 gx + tc * c - 1, gy + (7 - tr) * c - 1), fill=PINK_PALE, outline=PINK, width=2)
    for row, letters in STORY["grid"].items():
        for col, char in enumerate(letters, start=1):
            if char:
                x = gx + (col - .5 + shift) * c
                y = gy + (6 - int(row) + .5) * c
                active = col + shift == tc and int(row) == tr
                center(d, (x, y), char, 23, PINK if active else INK, active)
    for col in range(1, 10):
        center(d, (gx + (col - .5) * c, gy + 6 * c + 19), col, 19, COL if col == tc else MUTED, col == tc)
    for row in range(1, 7):
        center(d, (gx - 19, gy + (6 - row + .5) * c), row, 19, ROW if row == tr else MUTED, row == tr)
    return im


def recap_frame(im, t, s):
    d = ImageDraw.Draw(im)
    widths = [568, 568, 600]
    xs = [64, 660, 1256]
    headings = ["“舟”的位置", "（7，2）对应的字", "“赠”左移 1 格后"]
    answers = ["（5，4）", "千", "（3，6）"]
    details = ["第 5 列，第 4 行", "第 7 列与第 2 行的交叉处", "由（4，6）移到（3，6）"]
    reveal = [s["start"], s["lines"][1]["start"],
              s["lines"][1]["start"] + s["lines"][1]["duration"] * .43]
    for n, (x, width) in enumerate(zip(xs, widths)):
        active = t >= reveal[n]
        edge = COL if active else LINE
        d.rounded_rectangle((x, 298, x + width, 831), radius=25, fill=WHITE, outline=edge, width=3 if active else 2)
        center(d, (x + width / 2, 346), headings[n], 31, INK, True)
        center(d, (x + width / 2, 426), answers[n] if active else "？", 67, PINK if active else MUTED, True)
        center(d, (x + width / 2, 494), details[n], 26, MUTED)
        board = mini_board(n)
        im.paste(board, (round(x + width / 2 - board.width / 2), 535), board)
    d.rounded_rectangle((64, 852, 1856, 920), radius=20, fill=COL_PALE)
    center(d, (533, 884), "找位置：先列后行", 36, COL, True)
    center(d, (1408, 884), "向左移：列减 1，行不变", 36, COL, True)


@lru_cache(maxsize=64)
def subtitle_layer(caption):
    im = Image.new("RGBA", (1630, 86), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    size = 40
    while d.textlength(caption, font=font(size)) > 1570:
        size -= 1
    center(d, (815, 42), caption, size, WHITE)
    return im


def frame_at(t):
    idx = max(0, min(len(SCENES) - 1, bisect.bisect_right(STARTS, t) - 1))
    s = SCENES[idx]
    im = base_frame(s["id"]).copy()
    if s["id"] == "recap":
        recap_frame(im, t, s)
    else:
        state = draw_board(im, t, s)
        panel = right_panel(s["id"], state)
        im.paste(panel, (RX, RY), panel)
    d = ImageDraw.Draw(im)
    n = bisect.bisect_right(LINE_STARTS, t) - 1
    if n >= 0:
        line = LINES[n]
        if t <= line["caption_end"] + .05:
            caption = subtitle_layer(line.get("caption", line["text"]))
            im.paste(caption, (191, 946), caption)
    progress_x = 64 + int(1792 * min(t / DURATION, 1))
    d.rounded_rectangle((64, 1053, max(70, progress_x), 1060), radius=3, fill=COL)
    return im


def make_previews(directory):
    directory.mkdir(parents=True, exist_ok=True)
    thumbs = []
    for i, scene in enumerate(SCENES):
        t = scene["start"] + scene["duration"] * .72
        im = frame_at(t)
        im.save(directory / f'{i+1:02d}_{scene["id"]}.png')
        thumb = im.resize((480, 270), Image.Resampling.LANCZOS)
        thumbs.append(thumb)
    sheet = Image.new("RGB", (480 * 4, 308 * 4), "#E8EFF1")
    d = ImageDraw.Draw(sheet)
    for i, thumb in enumerate(thumbs):
        x, y = (i % 4) * 480, (i // 4) * 308
        sheet.paste(thumb, (x, y))
        txt(d, (x + 12, y + 279), f'{i+1:02d}  {SCENES[i]["title"]}', 20, INK)
    sheet.save(directory / "contact_sheet.jpg", quality=92)
    # Explicit before/mid/after samples verify the continuous translation.
    s = next(s for s in SCENES if s["id"] == "shift_move")
    for label, q in [("before", .12), ("middle", .60), ("after", .98)]:
        frame_at(s["start"] + q * s["duration"]).save(directory / f"shift_{label}.png")
    print(json.dumps({"preview_directory": str(directory), "scenes": len(SCENES)}, ensure_ascii=False), flush=True)


def encode(output, fps=FPS, audio=None, start=0.0, duration=None, silent=False):
    if not shutil.which("ffmpeg"):
        raise RuntimeError("FFmpeg is required for video encoding.")
    if not isinstance(fps, int) or isinstance(fps, bool) or fps <= 0 or not math.isfinite(start) or not 0 <= start < DURATION:
        raise ValueError("fps must be a positive integer and start must be inside the lesson.")
    duration = DURATION - start if duration is None else duration
    if not math.isfinite(duration) or duration <= 0 or start + duration > DURATION + 1e-8:
        raise ValueError("duration must be positive and the clip must fit inside the lesson.")
    audio = Path(audio) if audio is not None else ROOT / "output/narration.wav"
    if not silent and not audio.is_file():
        raise FileNotFoundError("Narration missing. Run the tts command, pass --audio FILE, or explicitly use --silent.")
    if not silent:
        if not shutil.which("ffprobe"):
            raise RuntimeError("ffprobe is required to verify narration duration.")
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                "-of", "default=noprint_wrappers=1:nokey=1", str(audio)],
                               check=True, capture_output=True, text=True)
        audio_duration = float(probe.stdout.strip())
        if not math.isfinite(audio_duration) or audio_duration + 0.05 < start + duration:
            raise ValueError("Narration is shorter than the selected timeline; pass its matching --timeline.")
    output.parent.mkdir(parents=True, exist_ok=True)
    frame_count = math.ceil(duration * fps)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "rawvideo", "-vcodec", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{W}x{H}", "-r", str(fps), "-i", "pipe:0",
           *([] if silent else ["-ss", str(start), "-i", str(audio)]),
           "-map", "0:v:0",
           *([] if silent else ["-map", "1:a:0"]),
           "-c:v", "libx264", "-preset", "fast", "-crf", "20",
           "-pix_fmt", "yuv420p",
           *([] if silent else ["-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-af", "apad"]),
           "-t", f"{frame_count / fps:.8f}",
           "-movflags", "+faststart", "-metadata", f'title={STORY["title"]}',
           "-metadata", "comment=Pillow + NumPy frame-by-frame animation; Chinese synthetic narration",
           str(output)]
    started = time.monotonic()
    with subprocess.Popen(cmd, stdin=subprocess.PIPE) as process:
        try:
            for frame in range(frame_count):
                im = frame_at(start + frame / fps)
                pixels = np.asarray(im, dtype=np.uint8)
                process.stdin.write(pixels.tobytes(order="C"))
                if frame % (fps * 10) == 0:
                    print(json.dumps({"frame": frame, "total_frames": frame_count,
                                      "video_second": round(frame / fps, 2),
                                      "render_elapsed": round(time.monotonic() - started, 1)}), flush=True)
        finally:
            process.stdin.close()
        status = process.wait()
        if status:
            raise RuntimeError(f"FFmpeg exited with status {status}")
    print(json.dumps({"output": str(output), "frames": frame_count, "fps": fps,
                      "duration": frame_count / fps,
                      "elapsed": round(time.monotonic() - started, 2)}, ensure_ascii=False), flush=True)


def validate_content():
    def require(condition, message):
        if not condition:
            raise ValueError(message)

    g = STORY["grid"]
    require(set(g) == {str(i) for i in range(1, 7)} and all(len(row) == 9 for row in g.values()),
            "The poem grid must contain six rows and nine columns.")
    require((W, H) == (1920, 1080), "This lesson's artwork requires 1920 x 1080.")
    require(g["4"][4] == "舟" and g["2"][6] == "千" and g["6"][3] == "赠", "Unexpected answer cells.")
    require(STORY["answers"]["shift"]["after"] == [3, 6], "Unexpected translation answer.")
    require(math.isfinite(DURATION) and DURATION > 0, "Invalid lesson duration.")
    require(len(SCENES) == len(STORY["scenes"]), "Storyboard and timeline scene counts differ.")
    require(abs(SCENES[-1]["end"] - DURATION) < 1e-8, "Timeline duration does not match its final scene.")
    for i, (scene, original) in enumerate(zip(SCENES, STORY["scenes"])):
        require(scene["id"] == original["id"] and scene["chapter"] == original["chapter"], "Storyboard and timeline scenes differ.")
        require(len(scene["lines"]) == len(original["lines"]) > 0, "Storyboard and timeline line counts differ.")
        expected_start = SCENES[i-1]["end"] if i else 0
        require(abs(scene["start"] - expected_start) < 1e-8 and scene["end"] > scene["start"], "Scene spans are not contiguous.")
        previous_end = scene["start"]
        for line, source in zip(scene["lines"], original["lines"]):
            text = source["text"] if isinstance(source, dict) else source
            require(line["text"] == text, "Narration changed: regenerate TTS and use its matching timeline.")
            require(previous_end <= line["start"] < line["end"] <= scene["end"], "Invalid utterance timing.")
            require(line["end"] <= line["caption_end"] <= DURATION, "Invalid subtitle timing.")
            previous_end = line["end"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--preview", action="store_true", help="Render key frames and a contact sheet.")
    actions.add_argument("--frame", type=float, help="Save one frame at the given second.")
    actions.add_argument("--validate", action="store_true", help="Check lesson content and timing without rendering.")
    parser.add_argument("--output", type=Path, default=ROOT / "output/古诗数对与平移_讲题视频.mp4")
    parser.add_argument("--preview-dir", type=Path, default=ROOT / "output/previews")
    parser.add_argument("--audio", type=Path, help="Matching narration WAV/MP3; defaults to output/narration.wav.")
    parser.add_argument("--timeline", type=Path, help="Matching timeline JSON; generated output/timeline.json takes precedence over the bundled reference.")
    parser.add_argument("--font-dir", type=Path, help="Directory containing Noto Sans CJK SC Regular and Bold.")
    parser.add_argument("--fps", type=int, default=FPS)
    parser.add_argument("--start", type=float, default=0.0, help="Clip start in lesson seconds.")
    parser.add_argument("--duration", type=float, help="Render only this many seconds.")
    parser.add_argument("--silent", action="store_true", help="Explicitly render without narration.")
    args = parser.parse_args()
    generated = ROOT / "output/timeline.json"
    try:
        configure(args.timeline or (generated if generated.is_file() else ROOT / "timeline.json"), args.font_dir)
        validate_content()
        if args.validate:
            print(json.dumps({"status": "valid", "scenes": len(SCENES), "lines": len(LINES), "duration": DURATION}))
        elif args.preview:
            make_previews(args.preview_dir)
        elif args.frame is not None:
            if not math.isfinite(args.frame) or not 0 <= args.frame <= DURATION:
                raise ValueError("Frame time must be inside the lesson.")
            path = args.preview_dir / f"frame_{args.frame:07.3f}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            frame_at(args.frame).save(path)
            print(path)
        else:
            encode(args.output, args.fps, args.audio, args.start, args.duration, args.silent)
    except (ValueError, FileNotFoundError, RuntimeError, subprocess.CalledProcessError) as error:
        parser.exit(2, f"Error: {error}\n")
