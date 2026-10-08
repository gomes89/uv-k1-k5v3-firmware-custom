# Copyright 2026 André S. Gomes
# Licensed under the Apache License, Version 2.0
"""Sky plot background for the radio, drawn here so the app only has to copy
it: horizon and 45 deg rings, N/E/S/W and the dashed pass path. Pixel-exact with
the firmware's line and small-font routines, so the app's solid line lands
exactly on the dashed one."""
import math
import os
import re

from .fmt import _cdefine

PX, PY, R = _cdefine("SAT_PLOT_X"), _cdefine("SAT_PLOT_Y"), _cdefine("SAT_PLOT_R")
# LCD pixels are taller than wide: a 31 x 31 px circle measures about
# 6.5 x 9.5 mm. Squash y by that ratio so the dome looks round on the glass.
RY = round(R * 6.5 / 9.5)
FX, FW, FP = _cdefine("SAT_FRAME_X"), _cdefine("SAT_FRAME_W"), _cdefine("SAT_FRAME_PAGES")
FONT_C = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "App", "font.c")


def _font3x5():
    with open(FONT_C) as f:
        src = f.read()
    body = src[src.index("gFont3x5[][3]"):]
    body = body[:body.index("};")]
    return [tuple(int(v, 16) for v in m) for m in
            re.findall(r"\{\s*(0x[0-9a-fA-F]+)\s*,\s*(0x[0-9a-fA-F]+)\s*,\s*(0x[0-9a-fA-F]+)\s*\}", body)]


def _ctrunc(a, b):
    """C integer division (truncates toward zero)."""
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b > 0) else -q


class Canvas:
    def __init__(self):
        self.px = set()
        self.trace = None           # ordered path pixels while tracing a dashed path

    def pix(self, x, y):
        if self.trace is not None:
            if (x, y) not in self.trace:
                self.trace.append((x, y))
            return
        if 0 <= x < 128 and 0 <= y < 56:
            self.px.add((x, y))

    def line(self, x1, y1, x2, y2):
        """UI_DrawLineBuffer, including its rounding."""
        if x1 == x2:
            for y in range(min(y1, y2), max(y1, y2) + 1):
                self.pix(x1, y)
            return
        a = _ctrunc((y2 - y1) * 1000, x2 - x1)
        b = y1 - _ctrunc(a * x1, 1000)
        for x in range(min(x1, x2), max(x1, x2) + 1):
            self.pix(x, _ctrunc(x * a, 1000) + b)

    def ellipse(self, rx, ry, dashed):
        """Ellipse round the plot centre: one quarter, x stepped where the
        curve is flatter than 45 deg and y where steeper, each rounded to the
        nearest pixel, then mirrored. dashed keeps 2 pixels in every 4."""
        h = math.hypot(rx, ry)
        q = {(x, round(ry * math.sqrt(1 - (x / rx) ** 2)))
             for x in range(rx + 1) if x <= rx * rx / h + 0.5}
        q |= {(round(rx * math.sqrt(1 - (y / ry) ** 2)), y)
              for y in range(ry + 1) if y <= ry * ry / h + 0.5}
        q = sorted(q, key=lambda p: math.atan2(p[1] * rx, p[0] * ry))
        for n, (x, y) in enumerate(q):
            if not dashed or (n & 2):
                for dx, dy in ((x, y), (-x, y), (x, -y), (-x, -y)):
                    self.pix(PX + dx, PY + dy)

    def text(self, s, x, y, font):
        """GUI_DisplaySmallest."""
        for ch in s:
            for i, col in enumerate(font[ord(ch) - 0x20]):
                for j in range(6):
                    if col >> j & 1:
                        self.pix(x + i, y + j)
            x += 4


def frame(points):
    """Frame bytes for pages 1..FP, columns FX..FX+FW-1, page-major."""
    c = Canvas()
    font = _font3x5()
    c.ellipse(R, RY, False)
    c.ellipse(R // 2, RY // 2, True)
    c.text("N", PX - 1, PY - RY - 6, font)             # 1 px gap to the horizon
    c.text("E", PX + R + 2, PY - 3, font)                 # ring on all four sides
    c.text("S", PX - 1, PY + RY + 2, font)
    c.text("W", PX - R - 4, PY - 3, font)
    c.trace = []                                        # trace the path once, in order,
    for i in range(len(points) - 1):                    # then keep 2 pixels in every 4
        c.line(*points[i], *points[i + 1])
    path, c.trace = c.trace, None
    for n, (x, y) in enumerate(path):
        if not n & 2:
            c.pix(x, y)
    out = bytearray()
    for page in range(1, FP + 1):
        for x in range(FX, FX + FW):
            out.append(sum(1 << j for j in range(8) if (x, page * 8 + j) in c.px))
    return bytes(out)
