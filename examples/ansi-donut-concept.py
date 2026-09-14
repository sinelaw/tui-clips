"""A sketch: the donut printed into a terminal rather than drawn into a frame.

NOT WIRED IN. Nothing in bin/ or lib/ knows this file exists, and no spec can
ask for it. It is a subclass of `DonutRenderer` that replaces every drawing
method with one that prints cells, so the storyboard, the camera solve and the
card placement are the real ones -- only the ink changes. That is the point of
keeping it a subclass: it says what the look would cost, which is the drawing
and nothing else.

    python3 examples/ansi-donut-concept.py /tmp/ansi [cell] [ramp]

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

# Two cells. Type sits on the character cell, because that is the size type
# has to be to be read. The chart is printed on a finer one -- the liberty
# every TUI charting library takes when it reaches for quadrant or braille
# glyphs to get more pixels than the terminal has characters.
#
# The two are multiplied, not traded: the chart cell is a third the width of
# the type cell, and each of its cells carries 4x4 of sub-cell resolution
# through the glyph it picks. That is 12x the linear resolution of the type
# grid, which is why the arc reads as a curve rather than as a staircase while
# the labels stay legible.
CELL_W, CELL_H = 14, 28
RING_W = 5

# The alphabet the ring is printed in, and the whole reason it can read as a
# curve at 77 columns. A coverage ramp (` ░▒▓█`) can only say *how much* of a
# cell is covered; these say *which part*, so an edge lands where the edge is:
# quadrants for corners, eighths for a near-flat edge, and -- the ones that
# matter most for an arc -- triangles for a diagonal one. The shades are last,
# for the ragged cells nothing structural fits.
#
# Every one of these was checked against the font rather than assumed: DejaVu
# Sans Mono has all of them, and has neither braille nor the Unicode 13 legacy
# sextants, which would have given 2x4 and 2x3 exactly.
# The alphabet, ordered by how much of a cell each one inks. This is libcaca's
# trick and it is the opposite of what the first pass here did: do not try to
# work out what shape a cell is, just work out how *dark* it is and print the
# character of that darkness -- then push the rounding error into the cells
# next door so it comes out in the texture instead of in a band.
#
# Five levels sounds like nothing until you remember that is what dithering is
# for. Five levels plus error diffusion resolves an edge better than thirty-six
# glyphs matched cell by cell did, and it cannot invent a shape that is not
# there, which is where the gaps and the floating slivers came from.
# The alphabet. Any string works: each character is rendered at the cell size
# and reduced to an NX-by-NY grid of its own ink, so a set is just a string and
# a preference. The third argument to the script picks one.
#
# The first pass ranked characters by how much of a cell they ink and chose by
# darkness alone. That is libcaca's classic trick and it is exactly right for
# ` ░▒▓█`, where every character is uniform and only the amount differs -- and
# it throws away the whole reason to use ASCII. `'` and `_` ink about the same
# share of a cell in completely different parts of it; `/` and `\` are one
# glyph mirrored. Matching the grid rather than its mean is what lets an edge
# running down-and-left pick `/`, a flat bottom pick `_`, and a top corner
# pick `'`.
NX, NY = 4, 6

RAMPS = {
    # uniform sets: only the amount differs, so matching degenerates to a ramp
    "shades":  " ░▒▓█",
    "dots":    " .·:∘∙•●",
    # every printable ASCII character, which is the point -- the matcher gets
    # to use the ones with ink in a corner, along an edge, or on a diagonal
    "ascii":   (" !\"#$%&'()*+,-./0123456789:;<=>?@"
                "ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_`"
                "abcdefghijklmnopqrstuvwxyz{|}~"),
    # what an ANSI artist actually had: CP437's shades, blocks and box rules
    "ansi":    " ░▒▓█▀▄▌▐─│┌┐└┘├┤┬┴┼═║╔╗╚╝▬■·.:*+",
    # the block families on their own, which tile without a join
    "blocks":  " ▁▂▃▄▅▆▇█▏▎▍▌▋▊▉▖▗▘▝▚▞▙▛▜▟▀▐",
    # everything the font has that is shaped like part of a cell
    "unicode": (" ░▒▓█▀▄▌▐▁▂▃▅▆▇▏▎▍▋▊▉▖▗▘▝▚▞▙▛▜▟"
                "◢◣◤◥◺◿◹◸─│┌┐└┘╱╲╳▲▼◀▶●◗◖"),
}

NOTE_COLS = 30              # a card's description, wrapped in columns
# Error is never pushed into a cell the ring does not touch at all. Without
# that, diffusion walks ink out past the edge of the arc and prints it in the
# dark -- the same floating-sliver artefact as before, arrived at from the
# other direction.
INK = 0.004
PANEL_A = 232               # how opaque a card's panel is over the ring
SCANLINE = 0.88             # how far every second row is taken down
BLOOM = 0.40                # how much blurred light the lit section adds back
GLOW_PX = 9                 # how far the phosphor spreads


class AnsiDonut(render.DonutRenderer):
    """The same clip, printed.

    Three decisions carry it.

    The grid belongs to the *screen*, not to the chart. Cells are a fixed size
    in canvas pixels whatever the camera is doing, so zooming into a section
    does not magnify the blocks -- it resolves the arc into more of them, the
    way a TUI redraws when you make the window bigger. Everything else here is
    set dressing; this is the one that makes it read as a terminal rather than
    as pixel art.

    Coverage picks a glyph, not an alpha. A cell the band half covers gets a
    medium shade at full brightness: a terminal antialiases with texture, not
    with light.

    Everything else is on the type grid. The card is a box-drawn panel titled
    in its own top rule, the leader steps in box-drawing characters, the status
    line is reverse video, and the two places that want bigger type get a
    double-height line -- DECDHL, the only way a real terminal ever had two
    sizes at once.
    """

    # Two lines of label at the type cell, plus air. The base class sets its
    # labels in something smaller and says 64.
    LABEL_LH = 2 * CELL_H + 14

    def __init__(self, spec, outdir, ring=RING_W, ramp="shades"):
        # The cells have to exist before `super().__init__` runs, because it
        # calls `_fit` and `_build_cards`, and both of those are overridden
        # here and measure in cells.
        uk = spec["render"].get("size", [1080, 1080])[1] / 1080
        self.tcw, self.tch = CELL_W * uk, CELL_H * uk
        self.rcw, self.rch = ring * uk, ring * 2 * uk
        px = int(CELL_W / 0.6023 * uk)    # the size whose advance is one cell
        self.ftype = ImageFont.truetype(render.MONO, px)
        self.fbig = ImageFont.truetype(render.MONO, px * 2)
        self.fring = ImageFont.truetype(render.MONO, int(ring / 0.6023 * uk))
        self.ramp = RAMPS[ramp]
        self._tiles: dict = {}
        self._masks: list = []
        self._match: dict = {}
        self._scan = None
        self._cv = None
        self._lit_cells: list = []
        super().__init__(spec, outdir)
        self.cols = int(self.W / self.tcw) + 1
        self.rows = int(self.H / self.tch) + 1
        self.rcols = int(self.W / self.rcw) + 1
        self.rrows = int(self.H / self.rch) + 1
        self._ends = [d["a1"] for d in self.items]
        self._build_masks()

    def _build_masks(self) -> None:
        """each character of the set as an NX-by-NY grid of its own ink.

        Measured off the rendered glyph rather than assumed from its name: a
        font's idea of where its medium shade puts ink, and of how far down a
        comma sits, is the only one that matters -- it is the one that will be
        on the screen.
        """
        w, h = int(self.rcw), int(self.rch)
        for g in self.ramp:
            im = Image.new("L", (w, h), 0)
            if g != " ":
                ImageDraw.Draw(im).text((0, 0), g, font=self.fring, fill=255)
            cells = [v / 255.0 for v in im.resize((NX, NY), Image.BOX).tobytes()]
            self._masks.append((g, cells, sum(cells) / len(cells)))

    def _pick(self, want: list, key):
        """-> (character, how much of a cell it inks) for this coverage grid.

        Cached on a coarsely quantised grid. There are more patterns in
        principle than anyone could enumerate; on a real frame there are a few
        thousand, because every cell that is neither empty nor full is an edge
        and edges repeat -- and the cache outlives the frame, so the second one
        is nearly all hits.
        """
        if key not in self._match:
            best, pick = None, (" ", 0.0)
            for g, m, dens in self._masks:
                e = 0.0
                for a, b in zip(want, m):
                    d = a - b
                    e += d * d
                if best is None or e < best:
                    best, pick = e, (g, dens)
            self._match[key] = pick
        return self._match[key]

    def _fit(self) -> None:
        """the first thing `super().__init__` calls after the fonts exist, so
        the first chance to say the labels are set in something else"""
        self.f_lab = self.f_val = self.ftype
        super()._fit()

    # -- printing -----------------------------------------------------------
    def _tile(self, ch, fg, font, w, h):
        """one printed character, on nothing.

        Transparent, and pasted through its own alpha. An opaque tile has to be
        bigger than the cell -- a block glyph overhangs its advance, which is
        what makes blocks tile seamlessly -- and an opaque tile that is bigger
        than the cell paints its own background over the neighbour it overhangs
        into. Every cell then lays a dark strip along the next one, and the
        picture comes out combed. That was the gaps.
        """
        key = (ch, fg, font.size, w)
        if key not in self._tiles:
            im = Image.new("RGBA", (int(w) + 3, int(h) + 3), (0, 0, 0, 0))
            ImageDraw.Draw(im).text((0, 0), ch, font=font, fill=(*fg, 255))
            self._tiles[key] = im
        return self._tiles[key]

    def _print(self, grid, font=None, cw=None, chh=None) -> None:
        """paint a grid of cells, backgrounds then glyphs.

        Backgrounds go down first and all at once, into one transparent
        overlay that is composited in a single pass. Painted cell by cell they
        arrive after some of their own neighbours' glyphs and rub them out; and
        painted opaque they punch a flat hole in the picture, where what a
        panel over a chart wants is to let a little of the chart through.
        """
        cv = self._cv
        font = font or self.ftype
        cw = cw or self.tcw
        chh = chh or self.tch
        bgs = [(cell, bg) for cell, (_, _, bg) in grid.items() if bg]
        if bgs:
            ov = Image.new("RGBA", cv.size, (0, 0, 0, 0))
            od = ImageDraw.Draw(ov)
            for (c, r), bg in bgs:
                od.rectangle([int(c * cw), int(r * chh),
                              int((c + 1) * cw), int((r + 1) * chh)], fill=bg)
            cv.paste(ov, (0, 0), ov)
        for (c, r), (ch, fg, _) in grid.items():
            if ch != " ":
                t = self._tile(ch, fg, font, cw, chh)
                cv.paste(t, (int(c * cw), int(r * chh)), t)

    def _cell(self, px, py):
        return int(px / self.tcw), int(py / self.tch)

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

    @staticmethod
    def _wrap_cols(text, n):
        """wrapped in columns, which is the unit a terminal wraps in"""
        out, line = [], ""
        for word in text.split():
            trial = f"{line} {word}".strip()
            if line and len(trial) > n:
                out.append(line)
                line = word
            else:
                line = trial
        return out + ([line] if line else [])

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

    def _smooth(self, cam, sweep, focus):
        """the ring drawn the ordinary way, as RGBA over the viewport.

        The alpha channel is coverage and the colour channels are the section
        colours, premultiplied by it -- which is exactly what a downsample of a
        correct picture gives you, and exactly what the dither needs. Every
        artefact in the first pass came from not having this: classifying a
        cell by the angle of its centre misreads every cell on a boundary, and
        a section slid out of the ring has two boundaries nobody else agrees
        about. Drawing the thing properly and then reducing it cannot make that
        mistake, because there is no classification left to get wrong.
        """
        ss, (_, _, vw, vh) = render.DONUT_SS, self.vp
        lay = Image.new("RGBA", (vw * ss, vh * ss), (0, 0, 0, 0))
        ld = ImageDraw.Draw(lay)
        s, cx, cy = cam
        ox, oy = (vw / 2 - cx * s) * ss, (vh / 2 - cy * s) * ss
        for i, d in enumerate(self.items):
            lit = self._lit(i, focus)
            pts = self._wedge(i, sweep, render.POP * lit)
            if pts is None:
                continue
            col = tuple(int(render.lerp(d["color"][k], self.th["bg"][k],
                                        self.dim * (1.0 - lit)))
                        for k in range(3))
            ld.polygon([(ox + x * s * ss, oy + y * s * ss) for x, y in pts],
                       fill=(*col, 255))
        return lay.reduce(ss)

    def _ring(self, cv, cam, sweep, focus) -> None:
        # `_ring` runs first, so this is where the canvas for the whole frame
        # is picked up; the base class hands the others an ImageDraw only.
        self._cv = cv
        gw, gh = int(self.rcols * self.rcw), int(self.rrows * self.rch)
        full = Image.new("RGBA", (gw, gh), (0, 0, 0, 0))
        full.paste(self._smooth(cam, sweep, focus), (self.vp[0], self.vp[1]))
        cells = full.resize((self.rcols, self.rrows), Image.BOX)
        rgb = cells.convert("RGB").tobytes()
        # ... and again at sub-cell resolution, which is what says *where* in
        # each cell the ink goes. One resize in C, rather than a sampling loop
        # in Python -- and it is a reduction of a correct picture, so unlike
        # the geometry sampling it replaced it cannot misread a boundary.
        sw = self.rcols * NX
        sub = full.resize((sw, self.rrows * NY), Image.BOX).getchannel("A").tobytes()

        # Floyd-Steinberg, one row of error ahead of the cursor and one below.
        # The error is what makes five levels enough: a cell that wanted 0.6
        # and got 0.5 hands the missing tenth to its neighbours, and the ramp
        # comes out as texture rather than as five flat bands with seams.
        here = [0.0] * (self.rcols + 2)
        below = [0.0] * (self.rcols + 2)
        grid, self._lit_cells = {}, []
        r0 = int(self.tch / self.rch) + 1       # clear of the status line
        # A terminal has a palette, and the palette is the answer to the one
        # thing the dither gets wrong on its own. A cell's colour comes back
        # premultiplied by its coverage, so recovering the ink means dividing
        # by a number that is small exactly where the eighth-of-a-level of
        # rounding on it matters most -- and a faint edge cell comes out grey,
        # or white, or some hue the chart does not contain. Snapping to the
        # colours the ring is actually made of cannot do that.
        pal = [(tuple(int(render.lerp(d["color"][k], self.th["bg"][k],
                                      self.dim * (1.0 - self._lit(i, focus))))
                      for k in range(3)),
                bool(focus) and self._lit(i, focus) > 0.5)
               for i, d in enumerate(self.items)]
        snap: dict = {}
        for r in range(r0, self.rrows):
            here, below = below, [0.0] * (self.rcols + 2)
            for c in range(self.rcols):
                k = r * self.rcols + c
                grid_ = []
                for y in range(NY):
                    row = (r * NY + y) * sw + c * NX
                    grid_ += sub[row:row + NX]
                a = sum(grid_) / (255.0 * NX * NY)
                if a < INK:
                    here[c + 1] = 0.0           # nothing here: drop the error
                    continue
                # the diffused error is a debt on the whole cell, so it is
                # carried by every sub-cell alike -- it says "this cell owes
                # the picture a little more ink", not "the shape was different"
                d = here[c + 1]
                want = [min(1.0, max(0.0, v / 255.0 + d)) for v in grid_]
                key = tuple(int(v * 3.99) for v in want)
                ch, dens = self._pick(want, key)
                e = sum(want) / len(want) - dens
                here[c + 2] += e * 7 / 16
                below[c] += e * 3 / 16
                below[c + 1] += e * 5 / 16
                below[c + 2] += e * 1 / 16
                if ch == " ":
                    continue
                # Which of the ring's colours is this cell? Not "divide the
                # premultiplied colour by the coverage and look for the
                # nearest" -- that divides by a small number precisely where
                # the rounding on it is worst, and a faint edge cell comes back
                # some hue the chart does not contain. The *direction* of the
                # premultiplied colour is the ink's direction whatever the
                # coverage, so match on that: the best fit is the palette
                # entry with the largest cosine, and no division happens.
                raw = (rgb[3 * k], rgb[3 * k + 1], rgb[3 * k + 2])
                key = (raw[0] >> 2, raw[1] >> 2, raw[2] >> 2)
                if key not in snap:
                    snap[key] = max(pal, key=lambda p: (
                        sum(p[0][j] * raw[j] for j in range(3)) ** 2
                        / max(1, sum(v * v for v in p[0]))))
                col, lit = snap[key]
                grid[(c, r)] = (ch, col, None)
                if lit:
                    self._lit_cells.append((c, r))
        self._print(grid, self.fring, self.rcw, self.rch)

    # -- the card, measured in cells so the camera solves for the real one ---
    def _build_cards(self) -> None:
        self.cards: dict[int, dict] = {}
        for i, item in enumerate(self.items):
            share = f'{render._fmt_share(item["share"])} of {self.total_txt}'
            note = self._wrap_cols(item["note"], NOTE_COLS)
            body = [("", None),                     # air under the title rule
                    ("", None), ("", None),         # the double-height value
                    (share, self.th["muted"]), ("", None)]
            body += [(ln, self.note_c) for ln in note] + [("", None)]
            inner = max([len(item["label"]) + 6, len(item["display"]) * 2 + 4]
                        + [len(t) + 4 for t, _ in body])
            cols, rows = inner + 2, len(body) + 2
            self.cards[i] = {"cols": cols, "rows": rows, "body": body,
                             "w": cols * self.tcw, "h": rows * self.tch}

    def _card(self, cv, d, i, alpha) -> None:
        if alpha < 0.02:
            return
        item, card = self.items[i], self.cards[i]
        cam, (px, py) = self.section_view[i]
        col, bg = item["color"], (*self.th["caption_bg"], PANEL_A)
        w, h = card["cols"], card["rows"]
        c0 = max(0, min(self.cols - w, int((px - card["w"] / 2) / self.tcw)))
        r0 = max(1, min(self.rows - h - 1,
                        int((py - card["h"] / 2) / self.tch)))

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
        for ch, cell in (("┌", (c0, r0)), ("┐", (c0 + w - 1, r0)),
                         ("└", (c0, r0 + h - 1)),
                         ("┘", (c0 + w - 1, r0 + h - 1))):
            grid[cell] = (ch, col, bg)
        # a TUI titles a box in the box's own top rule
        self._text(grid, c0 + 2, r0, f' {item["label"]} ', col, bg)
        for k, (txt, fg) in enumerate(card["body"]):
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
            ((c0 + 3) * self.tcw, (r0 + 1) * self.tch), item["display"],
            font=self.fbig, fill=col)

    # -- the rest of the furniture ------------------------------------------
    def _hole(self, d, alpha) -> None:
        if alpha < 0.02 or not self.total_txt:
            return
        c, r = self._cell(*self.centre)
        ImageDraw.Draw(self._cv).text(
            ((c - len(self.total_txt)) * self.tcw, (r - 1) * self.tch),
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
        self._text(grid, c - len(head) // 2, r, head, self.th["fg"])
        self._text(grid, c - len(sub) // 2, r + 2, sub, self.th["muted"])
        self._print(grid)

    def header(self, d, accent=None) -> None:
        """a status line in reverse video, which is where a TUI puts one"""
        accent = accent or self.th["after"]
        grid = {(c, 0): (" ", self.th["bg"], (*accent, 255))
                for c in range(self.cols)}
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
                md.rectangle([c * self.rcw, r * self.rch,
                              (c + 1) * self.rcw, (r + 1) * self.rch], fill=255)
            lit = Image.new("RGB", cv.size, (0, 0, 0))
            lit.paste(cv, mask=mask)
            glow = lit.filter(ImageFilter.GaussianBlur(int(GLOW_PX * self.uk)))
            cv = ImageChops.add(cv, ImageChops.multiply(
                glow, Image.new("RGB", cv.size, (int(255 * BLOOM),) * 3)))
        return ImageChops.multiply(cv, self._scanlines())


def main():
    out = sys.argv[1]
    cell = int(sys.argv[2]) if len(sys.argv) > 2 else RING_W
    ramp = sys.argv[3] if len(sys.argv) > 3 else "unicode"
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(ROOT, "examples/memory-breakdown.json")) as fh:
        spec = json.load(fh)
    r = AnsiDonut(spec, out, cell, ramp)
    print(f"  ring {cell}x{cell * 2}px -> {r.rcols}x{r.rrows} characters; "
          f"type {CELL_W}x{CELL_H}px -> {r.cols}x{r.rows}")
    print(f"  ramp {ramp!r}: {len(r.ramp)} characters, "
          f"{NX}x{NY} sub-cells each")
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
            os.path.join(out, f"{ramp}{cell}-{name}.png"))
        print(f"  {name:6s} t={picks[name]:5.2f}s  "
              f"{(time.time() - t0) * 1000:.0f}ms")


if __name__ == "__main__":
    main()
