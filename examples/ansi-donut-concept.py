"""A sketch: the donut printed into a terminal rather than drawn into a frame.

NOT WIRED IN. Nothing in bin/ or lib/ knows this file exists, and no spec can
ask for it. It is a subclass of `DonutRenderer` that replaces every drawing
method with one that prints cells, so the storyboard, the camera solve and the
card placement are the real ones -- only the ink changes. That is the point of
keeping it as a subclass: it says what the look would cost, which is the
drawing and nothing else.

    python3 examples/ansi-donut-concept.py /tmp/ansi

It exists because this repo is about terminals, and the donut is the one shape
in it that does not look like one. If it earns its place it becomes a
`render.donut.style` switch rather than a second renderer.
"""
from __future__ import annotations

import bisect
import json
import math
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont  # noqa: E402
import render  # noqa: E402

# The cell. A terminal has one size of character and everything is made of it,
# so this is the only length in the picture that is not derived from something
# else. DejaVu Sans Mono at 20px advances 12.05 and its full block runs the
# whole 24 -- and overhangs its advance by a pixel each side, so blocks tile
# with no seam between them.
FONT_PX = 20
CELL_W, CELL_H = 12, 24

# How much of a cell the ring covers, and what gets printed for it. This is
# the whole effect: an arc has no smooth edge, it frays.
SHADES = ((0.86, "█"), (0.60, "▓"), (0.34, "▒"), (0.10, "░"))
SS = 3                      # coverage samples per cell, per axis

SCANLINE = 0.88             # how far every second row is taken down
BLOOM = 0.40                # how much blurred light the lit section adds back
GLOW_PX = 9                 # how far the phosphor spreads


def shade(cov: float) -> str:
    for thresh, ch in SHADES:
        if cov >= thresh:
            return ch
    return ""


class AnsiDonut(render.DonutRenderer):
    """The same clip, printed.

    Three decisions make it read as a terminal rather than as pixel art:

    1. The grid belongs to the *screen*, not to the chart. Cells are a fixed
       size in canvas pixels whatever the camera is doing, so zooming in does
       not magnify the blocks -- it resolves the arc into more of them, the
       way a TUI redraws when you make the window bigger. The whole look rests
       on this one.

    2. Coverage picks a glyph, not an alpha. A cell the band half covers gets
       a medium shade at full brightness, which is how a terminal antialiases:
       with texture rather than with light.

    3. Everything else is on the same grid. The card is a box-drawn panel
       titled in its own top rule, the leader steps in box-drawing characters,
       the header is a status line in reverse video, and the two places that
       need bigger type get it the only way a terminal can -- a double-height
       line, which is a real thing a real VT does.

    Then two CRT effects, both nearly free: scanlines, and a phosphor bloom on
    the section being read and nothing else, which does the work the dim was
    doing from the other end.
    """

    def __init__(self, spec, outdir):
        super().__init__(spec, outdir)
        self.cw = CELL_W * self.uk
        self.ch = CELL_H * self.uk
        self.cols = int(self.W / self.cw) + 1
        self.rows = int(self.H / self.ch) + 1
        self.fm = ImageFont.truetype(render.MONO, int(FONT_PX * self.uk))
        self.fbig = ImageFont.truetype(render.MONO, int(FONT_PX * 2 * self.uk))
        self._tiles: dict = {}
        self._scan = None
        self._cv = None
        self._lit_cells: list = []
        self._ends = [d["a1"] for d in self.items]

    # -- printing -----------------------------------------------------------
    def _tile(self, ch, fg, bg):
        """one printed cell, cached; there are only ever a few dozen"""
        key = (ch, fg, bg)
        if key not in self._tiles:
            im = Image.new("RGB", (int(self.cw) + 2, int(self.ch) + 2), bg)
            ImageDraw.Draw(im).text((0, 0), ch, font=self.fm, fill=fg)
            self._tiles[key] = im
        return self._tiles[key]

    def _print(self, grid) -> None:
        cv, bgc = self._cv, self.th["bg"]
        for (c, r), (ch, fg, bg) in grid.items():
            x, y = int(c * self.cw), int(r * self.ch)
            if bg is not None and ch == " ":
                cv.paste(bg, (x, y, x + int(self.cw) + 1, y + int(self.ch) + 1))
            elif ch != " ":
                cv.paste(self._tile(ch, fg, bg or bgc), (x, y))

    def _cell(self, px, py):
        return int(px / self.cw), int(py / self.ch)

    def _text(self, grid, c, r, txt, fg, bg=None):
        for k, ch in enumerate(txt):
            grid[(c + k, r)] = (ch, fg, bg)

    def _elbow(self, grid, a, b, fg):
        """a leader from cell `a` to cell `b`: along, turn, down"""
        (c0, r0), (c1, r1) = a, b
        step = 1 if c1 >= c0 else -1
        for c in range(c0, c1, step):
            grid[(c, r0)] = ("─", fg, None)
        if r0 == r1:
            grid[(c1, r1)] = ("─", fg, None)
            return
        grid[(c1, r0)] = (("┐" if step > 0 else "┌") if r1 > r0
                          else ("┘" if step > 0 else "└"), fg, None)
        lo, hi = (r0 + 1, r1) if r1 > r0 else (r1, r0 - 1)
        for r in range(lo, hi + 1):
            grid[(c1, r)] = ("│", fg, None)

    # -- the ring -----------------------------------------------------------
    def _world(self, cam, px, py):
        s, cx, cy = cam
        return (cx + (px - (self.vp[0] + self.vp[2] / 2)) / s,
                cy + (py - (self.vp[1] + self.vp[3] / 2)) / s)

    def _hit(self, x, y, sweep, offs):
        """which section covers this world point, or None.

        The angle of the untranslated point picks the candidate: a section's
        pop is radial and small, so it never carries a point out of its own
        wedge, and one test then does what six would.
        """
        t = self.start + (math.degrees(math.atan2(y, x)) - self.start) % 360.0
        i = bisect.bisect_right(self._ends, t)
        if i >= len(self.items):
            return None
        d = self.items[i]
        px, py = x - offs[i][0], y - offs[i][1]
        r = math.hypot(px, py)
        if not self.inner <= r <= 1.0:
            return None
        tt = self.start + (math.degrees(math.atan2(py, px)) - self.start) % 360.0
        g = min(self.gap, (d["a1"] - d["a0"]) * 0.45)
        if tt < d["a0"] + g / 2 or tt > min(d["a1"] - g / 2,
                                            self.start + 360.0 * sweep):
            return None
        return i

    def _ring(self, cv, cam, sweep, focus) -> None:
        # `_ring` runs first, so this is where the canvas for the whole frame
        # is picked up; the base class hands the others an ImageDraw only.
        self._cv = cv
        s = cam[0]
        offs, cols = [], []
        for i, d in enumerate(self.items):
            lit = self._lit(i, focus)
            mid = math.radians((d["a0"] + d["a1"]) / 2)
            offs.append((math.cos(mid) * render.POP * lit,
                         math.sin(mid) * render.POP * lit))
            cols.append(tuple(int(render.lerp(d["color"][k], self.th["bg"][k],
                                              self.dim * (1.0 - lit)))
                              for k in range(3)))
        # a cell whose centre is further from the band than its own half
        # diagonal cannot be touched by it, which is most of them most of the
        # time -- one hypot to skip nine atan2s
        reach = math.hypot(self.cw, self.ch) / 2 / s + render.POP + 0.02

        grid, self._lit_cells = {}, []
        for r in range(1, self.rows):
            for c in range(self.cols):
                wx, wy = self._world(cam, (c + 0.5) * self.cw,
                                     (r + 0.5) * self.ch)
                d0 = math.hypot(wx, wy)
                if d0 - 1.0 > reach or self.inner - d0 > reach:
                    continue
                seen: dict = {}
                for sy in range(SS):
                    for sx in range(SS):
                        h = self._hit(*self._world(
                            cam, (c + (sx + 0.5) / SS) * self.cw,
                            (r + (sy + 0.5) / SS) * self.ch), sweep, offs)
                        if h is not None:
                            seen[h] = seen.get(h, 0) + 1
                if not seen:
                    continue
                i = max(seen, key=seen.get)
                ch = shade(sum(seen.values()) / (SS * SS))
                if ch:
                    grid[(c, r)] = (ch, cols[i], None)
                    # Only a section being read glows. With nothing in focus
                    # -- the establishing shot -- every section would qualify,
                    # and a bloom over the whole ring is not a phosphor, it is
                    # a fog: the colours wash to pastel and the dither it was
                    # meant to flatter disappears into it.
                    if focus and self._lit(i, focus) > 0.5:
                        self._lit_cells.append((c, r))
        self._print(grid)

    # -- the furniture, also printed ----------------------------------------
    def _card(self, cv, d, i, alpha) -> None:
        if alpha < 0.02:
            return
        item = self.items[i]
        cam, (px, py) = self.section_view[i]
        col, bg = item["color"], self.th["caption_bg"]
        note = self._wrap(item["note"], self.f_note, render.CARD_TEXT * self.uk)
        # two blank rows where the double-height value goes
        body = [("", None), ("", None), ("", None),
                (f'{render._fmt_share(item["share"])} of {self.total_txt}',
                 self.th["muted"]), ("", None)]
        body += [(ln, self.note_c) for ln in note] + [("", None)]
        inner = max([len(item["label"]) + 6, len(item["display"]) * 2 + 4]
                    + [len(t) + 4 for t, _ in body])
        w, h = inner + 2, len(body) + 2
        c0 = max(0, min(self.cols - w, int((px - w * self.cw / 2) / self.cw)))
        r0 = max(1, min(self.rows - h - 1,
                        int((py - h * self.ch / 2) / self.ch)))

        grid = {}
        for r in range(r0, r0 + h):
            for c in range(c0, c0 + w):
                grid[(c, r)] = (" ", col, bg)
        for c in range(c0, c0 + w):
            grid[(c, r0)] = ("─", col, bg)
            grid[(c, r0 + h - 1)] = ("─", col, bg)
        for r in range(r0 + 1, r0 + h - 1):
            grid[(c0, r)] = ("│", col, bg)
            grid[(c0 + w - 1, r)] = ("│", col, bg)
        for corner, cell in (("┌", (c0, r0)), ("┐", (c0 + w - 1, r0)),
                             ("└", (c0, r0 + h - 1)),
                             ("┘", (c0 + w - 1, r0 + h - 1))):
            grid[cell] = (corner, col, bg)
        # a TUI titles a box in the box's own top rule
        self._text(grid, c0 + 2, r0, f' {item["label"]} ', col, bg)
        for k, (txt, fg) in enumerate(body):
            if txt:
                self._text(grid, c0 + 3, r0 + 1 + k, txt, fg, bg)

        ax, ay = self._nearest([self._pt(cam, *q)
                                for q in self._wedge(i, pop=render.POP)],
                               px, py)
        a = self._cell(ax, ay)
        if not (c0 <= a[0] <= c0 + w - 1 and r0 <= a[1] <= r0 + h - 1):
            self._elbow(grid, a,
                        (max(c0 + 1, min(c0 + w - 2, a[0])),
                         r0 + h - 1 if a[1] > r0 else r0), col)
        self._print(grid)
        # the value as a double-height line -- DECDHL, the only way a terminal
        # ever had two sizes of type on one screen
        ImageDraw.Draw(self._cv).text(
            ((c0 + 3) * self.cw, (r0 + 1) * self.ch + self.ch * 0.05),
            item["display"], font=self.fbig, fill=col)

    def _hole(self, d, alpha) -> None:
        if alpha < 0.02 or not self.total_txt:
            return
        c, r = self._cell(*self.centre)
        ImageDraw.Draw(self._cv).text(
            ((c - len(self.total_txt)) * self.cw, (r - 1) * self.ch),
            self.total_txt, font=self.fbig, fill=self.th["fg"])
        if self.total_label:
            grid = {}
            self._text(grid, c - len(self.total_label) // 2, r + 1,
                       self.total_label, self.th["muted"])
            self._print(grid)

    def _labels(self, d, alpha) -> None:
        if alpha < 0.02:
            return
        grid = {}
        cx, cy = self.centre
        for i, item in enumerate(self.items):
            L = self.labels[i]
            a = self._cell(cx + L["anchor"][0], cy + L["anchor"][1])
            tc, tr = self._cell(cx + L["text"][0], cy + L["text"][1])
            name, val = item["label"], self._sub_label(item)
            width = max(len(name), len(val))
            if L["side"] < 0:
                tc -= width
            self._elbow(grid, a,
                        (tc - 2 if L["side"] > 0 else tc + width + 1, tr),
                        item["color"])
            self._text(grid, tc, tr, name, self.th["fg"])
            self._text(grid, tc, tr + 1, val, item["color"])
        self._print(grid)

    def _establish(self, d, head, sub, alpha) -> None:
        if alpha < 0.02 or not (head or sub):
            return
        grid = {}
        c, r = self._cell(self.vp[0] + self.vp[2] / 2, self.estab_y)
        self._text(grid, c - len(head) // 2, r + 1, head, self.th["fg"])
        self._text(grid, c - len(sub) // 2, r + 3, sub, self.th["muted"])
        self._print(grid)

    def header(self, d, accent=None) -> None:
        """a status line in reverse video, which is where a TUI puts one"""
        accent = accent or self.th["after"]
        grid = {(c, 0): (" ", self.th["bg"], accent) for c in range(self.cols)}
        self._text(grid, 1, 0, f" {self.lead} ", self.th["bg"], accent)
        self._text(grid, self.cols - len(self.title) - 2, 0, self.title,
                   self.th["bg"], accent)
        self._print(grid)

    # -- the tube -----------------------------------------------------------
    def _scanlines(self):
        if self._scan is None:
            im = Image.new("RGB", (1, self.H), (255, 255, 255))
            d = ImageDraw.Draw(im)
            for y in range(0, self.H, 2):
                d.point((0, y), fill=(int(255 * SCANLINE),) * 3)
            self._scan = im.resize((self.W, self.H), Image.NEAREST)
        return self._scan

    def frame(self, n: int) -> Image.Image:
        cv = super().frame(n)
        if self._lit_cells and BLOOM > 0:
            mask = Image.new("L", cv.size, 0)
            md = ImageDraw.Draw(mask)
            for c, r in self._lit_cells:
                md.rectangle([c * self.cw, r * self.ch,
                              (c + 1) * self.cw, (r + 1) * self.ch], fill=255)
            lit = Image.new("RGB", cv.size, (0, 0, 0))
            lit.paste(cv, mask=mask)
            glow = lit.filter(ImageFilter.GaussianBlur(int(GLOW_PX * self.uk)))
            cv = ImageChops.add(cv, ImageChops.multiply(
                glow, Image.new("RGB", cv.size, (int(255 * BLOOM),) * 3)))
        return ImageChops.multiply(cv, self._scanlines())


def main():
    out = sys.argv[1]
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(ROOT, "examples/memory-breakdown.json")) as fh:
        spec = json.load(fh)
    r = AnsiDonut(spec, out)
    acc, picks = 0.0, {}
    for s in r.timeline:
        if s["kind"] == "intro":
            picks["intro"] = acc + s["dur"] * 0.75
        if s["kind"] == "hold":
            picks[f'hold{s["focus"]}'] = acc + s["dur"] * 0.55
        acc += s["dur"]
    for name in ("intro", "hold0", "hold4"):
        t0 = time.time()
        r.frame(int(picks[name] * r.fps)).save(
            os.path.join(out, f"ansi-{name}.png"))
        print(f"  {name:6s} t={picks[name]:5.2f}s  "
              f"{(time.time() - t0) * 1000:.0f}ms")


if __name__ == "__main__":
    main()
