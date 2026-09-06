"""Microsoft Word 6.0 (Windows 3.1) chrome.

A *chrome* wraps a clip's viewport in a period application window and hosts its
captions there, in place of the renderer's default header band and caption bar.
The capture stops being a screenshot on a dark ground and becomes an embedded
object in a document — which is a joke a clip can then make arguments inside.

Selected from a spec with `render.chrome`:

    "chrome": {
      "style": "word6",
      "assets": "~/Documents/fresh-clip-assets",
      "title": "Microsoft Word - WAVE.DOC",
      "scale": 3,
      "headline_height": 38,
      "body_lines": 2
    }

and per beat, optionally, `bullet` (a Wingdings key) and `misspell` (words to
draw Word's red zigzag under).

Everything is drawn at logical screen pixels and scaled up with
nearest-neighbour, so 1px bevels stay 1px and text keeps the aliased edge of a
bitmap UI font — closer to how MS Sans Serif looked than any smooth vector
clone gets. Only the capture itself is composited at full output resolution,
so the terminal stays legible inside a deliberately chunky window.

Fonts. The authentic faces (Wingdings, Impact, Times New Roman, Microsoft Sans
Serif, Marlett) ship with Windows, are licensed with it, and are therefore not
vendored here. Point `assets` at a directory with them under `fonts/`. Every role falls
back to a free face, so the chrome always renders — just less convincingly.

Symbol fonts (Wingdings, Webdings, Marlett, Symbol) carry a (3,0) symbol cmap
rather than a Unicode one, and Pillow reaches it only with `encoding="symb"`.
Without that every glyph comes back as .notdef — which is itself a hollow box,
so a failed load renders as a plausible-looking icon set rather than as an
error. `Fonts.symbol()` is the only correct way to open them, and it returns
None rather than boxes when a face is absent.
"""
from __future__ import annotations

import math
import os

from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------- palette

# Sampled off a real Word 6.0 screenshot. Windows 3.1 has no "button light"
# tone -- that arrived with Win95 -- so every raised edge is WHITE against
# SHADOW or BLACK, and nothing in the chrome is a soft grey.
BLACK = (0, 0, 0)
WHITE = (255, 255, 255)
FACE = (192, 192, 192)          # 3D face, menus' buttons, toolbars
SHADOW = (128, 128, 128)        # 3D shadow
TITLE = (0, 0, 192)             # active title bar -- brighter than navy
NAVY = (0, 0, 128)              # selection / extrusion shade
TEAL = (0, 128, 128)            # the Win3.1 desktop
INFO_BG = (255, 255, 206)       # tooltip yellow
# Win3.1 menu bars are WHITE, not grey. Grey menus are a Win95 habit and are
# the single fastest way to make a 1993 window look like a 1995 one.
MENU_BG = WHITE

# ------------------------------------------------------------------ fonts

_FACES = {   # role -> (Windows filename, free fallback)
    "ui": ("micross.ttf", "/usr/share/fonts/TTF/LiberationSans-Regular.ttf"),
    "ui_bold": ("tahomabd.ttf", "/usr/share/fonts/TTF/LiberationSans-Bold.ttf"),
    "body": ("times.ttf", "/usr/share/fonts/TTF/LiberationSerif-Regular.ttf"),
    "body_bold": ("timesbd.ttf", "/usr/share/fonts/TTF/LiberationSerif-Bold.ttf"),
    "art": ("impact.ttf", "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"),
    "mono": ("cour.ttf", "/usr/share/fonts/TTF/DejaVuSansMono.ttf"),
}
_SYMBOL_FACES = {
    "wingdings": "wingding.ttf",
    "webdings": "webdings.ttf",
    "marlett": "marlett.ttf",
    "symbol": "symbol.ttf",
}


class Fonts:
    """Resolves the period faces out of `font_dir`, falling back to free ones."""

    def __init__(self, assets: str | None = None):
        # Faces live in <assets>/fonts; the toolbar sheet sits beside it.
        self.dir = os.path.join(os.path.expanduser(assets), "fonts") if assets else None
        self._cache: dict = {}

    def _path(self, role: str):
        win, free = _FACES[role]
        if self.dir:
            p = os.path.join(self.dir, win)
            if os.path.exists(p):
                return p, True
        return free, False

    def get(self, role: str, size: int) -> ImageFont.FreeTypeFont:
        key = (role, size)
        if key not in self._cache:
            self._cache[key] = ImageFont.truetype(self._path(role)[0], size)
        return self._cache[key]

    def symbol(self, name: str, size: int):
        """A symbol-cmap font, or None when this directory has not got it."""
        key = ("sym", name, size)
        if key not in self._cache:
            p = os.path.join(self.dir, _SYMBOL_FACES[name]) if self.dir else None
            self._cache[key] = (ImageFont.truetype(p, size, encoding="symb")
                                if p and os.path.exists(p) else None)
        return self._cache[key]

    def missing(self) -> list[str]:
        out = [r for r in _FACES if not self._path(r)[1]]
        out += [n for n, fn in _SYMBOL_FACES.items()
                if not (self.dir and os.path.exists(os.path.join(self.dir, fn)))]
        return out

# ------------------------------------------------------------- primitives


def bevel(d, box, sunken: bool = False) -> None:
    """A 1px Win3.1 3D edge: raised by default, sunken for wells and fields."""
    x0, y0, x1, y1 = box
    tl, br = (SHADOW, WHITE) if sunken else (WHITE, SHADOW)
    d.line([(x0, y0), (x1 - 1, y0)], fill=tl)
    d.line([(x0, y0), (x0, y1 - 1)], fill=tl)
    d.line([(x0, y1 - 1), (x1 - 1, y1 - 1)], fill=br)
    d.line([(x1 - 1, y0), (x1 - 1, y1 - 1)], fill=br)


def button(d, box) -> None:
    """A raised push button: the double edge that makes Win3.1 look Win3.1."""
    x0, y0, x1, y1 = box
    d.rectangle([x0, y0, x1 - 1, y1 - 1], fill=FACE)
    d.line([(x0, y0), (x1 - 2, y0)], fill=WHITE)
    d.line([(x0, y0), (x0, y1 - 2)], fill=WHITE)
    d.line([(x0 + 1, y1 - 1), (x1 - 1, y1 - 1)], fill=BLACK)
    d.line([(x1 - 1, y0 + 1), (x1 - 1, y1 - 1)], fill=BLACK)
    d.line([(x0 + 1, y1 - 2), (x1 - 2, y1 - 2)], fill=SHADOW)
    d.line([(x1 - 2, y0 + 1), (x1 - 2, y1 - 2)], fill=SHADOW)


def squiggle(d, x0: int, x1: int, y: int, col=(255, 0, 0)) -> None:
    """Word's red spelling zigzag. Two pixels per step, like the original."""
    pts, x = [], x0
    while x < x1:
        pts.append((x, y + (0 if (x - x0) // 2 % 2 else 2)))
        x += 2
    if len(pts) > 1:
        d.line(pts, fill=col)

# ----------------------------------------------------------------- WordArt

RAINBOW = [(255, 0, 0), (255, 128, 0), (255, 255, 0),
           (0, 192, 0), (0, 128, 255), (96, 0, 192)]


def _dither(img: Image.Image, mask: Image.Image, shade: float = 0.62):
    """Darken every other pixel on a checkerboard.

    WordArt filled a face with a patterned brush rather than a smooth ramp,
    and at this resolution the checker survives the nearest-neighbour upscale
    as a visible halftone -- which is most of why the original looks the way
    it does.
    """
    px = img.load()
    mk = mask.load()
    for y in range(img.height):
        for x in range(img.width):
            if (x + y) % 2 and mk[x, y]:
                r, g, b = px[x, y][:3]
                px[x, y] = (int(r * shade), int(g * shade), int(b * shade))
    return img


def wordart(text: str, fonts: Fonts, size: int = 54, fill=RAINBOW,
            amp: float = 0.10, outline: int = 0, extrude=(1, -1),
            depth: int | None = None, extrude_col=(0, 0, 140),
            dither: bool = True, **_ignored) -> Image.Image:
    """WordArt 2.0: a "Wave" baseline over an extruded, dithered face.

    The face is a vertical gradient through `fill`, checker-dithered, wrapped
    in a black outline. Behind it the same silhouette is stamped `depth` times
    along `extrude`, which is what gives WordArt its solid 3D side rather than
    the blurred shadow a modern tool would reach for. The sinusoidal
    per-column warp is applied last, so outline and extrusion ride the curve
    with the letters instead of being sheared off them.
    """
    # Depth and outline are fractions of the type size, not absolutes: a fixed
    # seven-pixel extrusion is a bevel on a headline and a solid slab on a long
    # one, and `fit_wordart` shrinks the type until it fits, so the slab wins.
    if depth is None:
        depth = max(2, round(size * 0.11))
    if not outline:
        outline = max(1, round(size * 0.035))
    fnt = fonts.get("art", size)
    pad = size
    tmp = Image.new("L", (len(text) * size + 2 * pad, size * 3), 0)
    ImageDraw.Draw(tmp).text((tmp.width // 2, tmp.height // 2), text,
                             font=fnt, fill=255, anchor="mm")
    bb = tmp.getbbox()
    if bb is None:
        return Image.new("RGBA", (1, 1), (0, 0, 0, 0))
    tmp = tmp.crop(bb)
    w, h = tmp.size

    grad = Image.new("RGB", (w, h))
    gd = ImageDraw.Draw(grad)
    for y in range(h):
        t = y / max(1, h - 1) * (len(fill) - 1)
        i = min(int(t), len(fill) - 2)
        ft = t - i
        a, b = fill[i], fill[i + 1]
        gd.line([(0, y), (w, y)],
                fill=tuple(int(a[k] + (b[k] - a[k]) * ft) for k in range(3)))
    if dither:
        _dither(grad, tmp)

    ol = tmp.copy()
    for dx in range(-outline, outline + 1):
        for dy in range(-outline, outline + 1):
            if dx * dx + dy * dy <= outline * outline:
                ol.paste(tmp, (dx, dy), tmp)

    face = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    face.paste(BLACK, (0, 0), ol)
    face.paste(grad, (0, 0), tmp)
    face.putalpha(ol)

    ex, ey = extrude
    ox, oy = max(0, -ex * depth), max(0, -ey * depth)
    sw = w + abs(ex) * depth
    sh = h + abs(ey) * depth
    flat = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
    # farthest slab first, so nearer ones overwrite it and the side reads solid
    side = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    side.paste(extrude_col, (0, 0), ol)
    side.putalpha(ol)
    for i in range(depth, 0, -1):
        flat.paste(side, (ox + ex * i, oy + ey * i), side)
    flat.paste(face, (ox, oy), face)

    if amp <= 0:
        return flat
    a = sh * amp
    out = Image.new("RGBA", (sw, sh + int(a * 2)), (0, 0, 0, 0))
    for x in range(sw):
        out.paste(flat.crop((x, 0, x + 1, sh)),
                  (x, int(a * math.sin(2 * math.pi * x / sw) + a)))
    return out


def fit_wordart(text, fonts: Fonts, maxw: int, maxh: int, gap: int = 2,
                **kw) -> Image.Image:
    """Largest WordArt of `text` that fits the measure. Binary search on size.

    `text` may be a list, in which case the lines are stacked and share one
    type size — sized independently they would each fill the width and a short
    word would come out bigger than a long one, which is not how a headline
    works. Stacking is what makes a narrow column usable: two short words get
    several times the size one long line could, which is the whole reason the
    side layout has a column rather than a strip.
    """
    lines = list(text) if isinstance(text, (list, tuple)) else [text]

    def build(size):
        arts = [wordart(l, fonts, size=size, **kw) for l in lines]
        w = max(a.size[0] for a in arts)
        h = sum(a.size[1] for a in arts) + gap * (len(arts) - 1)
        return arts, w, h

    lo, hi, best = 8, 120, None
    while lo <= hi:
        mid = (lo + hi) // 2
        arts, w, h = build(mid)
        if w <= maxw and h <= maxh:
            best, lo = (arts, w, h), mid + 1
        else:
            hi = mid - 1
    if best is None:
        best = build(8)
    arts, w, h = best
    if len(arts) == 1:
        return arts[0]
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    y = 0
    for a in arts:
        out.paste(a, ((w - a.size[0]) // 2, y), a)
        y += a.size[1] + gap
    return out

# ------------------------------------------------------------ the chrome


class Icons:
    """Word 6.0's toolbar buttons, lifted whole from a screenshot of it.

    Each sprite is a complete 22px button — bevel included — so the chrome
    pastes them rather than redrawing them, and the icons are the real 16-colour
    artwork instead of a Wingdings glyph that merely rhymes with it. Like the
    fonts, this is Microsoft's artwork and is not vendored; point `assets` at
    a directory holding `word6-toolbar.png` and its `.json` index.
    """

    def __init__(self, assets: str | None):
        self.sheet = None
        self.meta: dict = {}
        if not assets:
            return
        base = os.path.expanduser(assets)
        png, js = os.path.join(base, "word6-toolbar.png"), os.path.join(base, "word6-toolbar.json")
        if os.path.exists(png) and os.path.exists(js):
            import json
            self.sheet = Image.open(png).convert("RGB")
            self.meta = json.load(open(js))

    def get(self, name: str):
        m = self.meta.get(name)
        if self.sheet is None or m is None:
            return None
        return self.sheet.crop((m["x"], 0, m["x"] + m["w"], m["h"]))


class Word6Chrome:
    """A Word 6.0 window that hosts a clip.

    The renderer asks for `viewport()` once — where, in output pixels, the
    capture belongs — then calls `frame()` per frame with that frame's caption.
    The static window is built once; each frame copies it, draws the caption
    and status bar over the copy, and upscales.

    Band heights, button pitch and the palette are measured off a real Word 6.0
    screenshot rather than guessed, which is why the menu bar is white: Windows
    3.1 menus were, and grey ones are the fastest way to make 1993 look 1995.
    """

    MENUS = ["File", "Edit", "View", "Insert", "Format",
             "Tools", "Table", "Window", "Help"]
    # Word 6.0's Standard toolbar, in its own grouping. The formatting toolbar
    # is a second row in the real thing; here the two are merged into one.
    TOOLBAR = [("new", "open", "save"),
               ("print", "preview", "spelling"),
               ("cut", "copy", "paste", "painter"),
               ("undo", "undo_arrow", "redo", "redo_arrow"),
               ("autoformat", "borders"),
               ("table", "columns", "drawing", "chart"),
               ("pilcrow",)]
    TITLE_H = 18
    MENU_H = 18
    MENU_GAP = 19
    BTN_H = 22
    BTN_GAP = 1
    GROUP_GAP = 8

    def __init__(self, cfg: dict, out_size, title_default="Untitled"):
        self.scale = int(cfg.get("scale", 3))
        self.OW, self.OH = out_size
        if self.OW % self.scale or self.OH % self.scale:
            raise SystemExit(
                f"chrome scale {self.scale} does not divide render.size "
                f"{self.OW}x{self.OH}; pick a scale that does")
        self.W, self.H = self.OW // self.scale, self.OH // self.scale
        self.f = Fonts(cfg.get("assets"))
        self.icons = Icons(cfg.get("assets"))
        self.title = cfg.get("title", title_default)
        # "stack" sets the headline across the top with the body beneath it;
        # "side" puts the WordArt in a left column with the body beside it. The
        # side layout exists for clips that have to be legible in a timeline at
        # thumbnail size: the same words get roughly three times the type size,
        # paid for out of the document's width rather than the capture's.
        self.layout = cfg.get("layout", "stack")
        self.head_h = int(cfg.get("headline_height", 38))
        self.head_w = float(cfg.get("headline_width", 0.36))
        self.head_block = int(cfg.get("header_block", 0))
        self.body_lines = int(cfg.get("body_lines", 2))
        self.body_size = int(cfg.get("body_size", 12))
        self.body_lead = int(cfg.get("body_lead", 14))
        self.handles = bool(cfg.get("handles", True))
        self.art_opts = dict(cfg.get("wordart", {}))

        # What the renderer fills the growing intro panel with, so the capture
        # opens on the document's own paper rather than on a clip background.
        self.fill = WHITE
        self._art: dict = {}
        self.base, self.document = self._build()
        bx0, by0, bx1, by1 = self.document
        if self.layout == "side":
            block = self.head_block or (self.body_lines * self.body_lead + 16)
            self.head_block = block
            top = by0 + block
        else:
            top = (by0 + 6 + self.head_h + 4
                   + self.body_lines * self.body_lead + 6)
        self._vp_logical = (bx0 + 14, top, bx1 - 14, by1)
        self._draw_embed_frame()

    # -- what the renderer needs

    def viewport(self):
        """(x, y, w, h) in output pixels."""
        x0, y0, x1, y1 = self._vp_logical
        s = self.scale
        return (x0 * s, y0 * s, (x1 - x0) * s, (y1 - y0) * s)

    def frame(self, head: str, sub, alpha: int = 255, page: int = 1,
              total: int = 1, bullet=None, misspell=(), reveal=None,
              caret=None) -> Image.Image:
        """The window for one frame, upscaled, with a hole for the capture.

        `reveal` is the share of the body text that has been typed so far;
        `caret` whether to draw the insertion point at the end of it.
        """
        im = self.base.copy()
        d = ImageDraw.Draw(im)
        self._caption(im, d, head, sub, alpha, bullet, misspell, reveal, caret)
        return im.resize((self.OW, self.OH), Image.NEAREST)

    # -- the window itself

    def _build(self):
        im = Image.new("RGB", (self.W, self.H), FACE)
        d = ImageDraw.Draw(im)
        W, H = self.W, self.H
        fui = self.f.get("ui", 11)
        fuib = self.f.get("ui_bold", 11)
        fsm = self.f.get("ui", 10)
        mar = self.f.symbol("marlett", 10)

        # Window frame and the control column, straight off the reference's
        # pixel grid. The title bar and menu bar do NOT start at the window
        # edge: a face-coloured column runs down the left holding the two
        # control-menu boxes, fenced off by a black rule at x=22.
        # No bottom edge on either frame: the window is taller than the
        # screen, so the document runs off it and there is visibly more to
        # scroll to. That also buys the capture the rows a status bar would eat.
        d.rectangle([0, 0, W - 1, H - 1], fill=FACE)
        d.line([(0, 0), (W - 1, 0)], fill=BLACK)
        d.line([(0, 0), (0, H - 1)], fill=BLACK)
        d.line([(W - 1, 0), (W - 1, H - 1)], fill=BLACK)
        d.line([(3, 3), (W - 4, 3)], fill=BLACK)
        d.line([(3, 3), (3, H - 1)], fill=BLACK)
        d.line([(W - 4, 3), (W - 4, H - 1)], fill=BLACK)
        col_x0, col_x1 = 4, 21
        bar_x0 = 23

        y = 4
        ty0, ty1 = y, y + self.TITLE_H - 1
        d.rectangle([bar_x0, ty0, W - 5, ty1], fill=TITLE)
        d.text(((bar_x0 + W - 5) // 2, (ty0 + ty1) // 2), self.title,
               font=fuib, fill=WHITE, anchor="mm")
        self._sysbox(d, col_x0, col_x1, ty0, ty1, 13)
        y = ty1 + 1
        d.line([(3, y), (W - 4, y)], fill=BLACK)
        y += 1

        # Menu bar — white, as Windows 3.1 had it. Label pitch is measured off
        # the reference: text opens at x=31, labels ~19px apart, bold.
        my0, my1 = y, y + self.MENU_H - 1
        d.rectangle([bar_x0, my0, W - 5, my1], fill=MENU_BG)
        self._sysbox(d, col_x0, col_x1, my0, my1, 7)
        mx = 31
        for m in self.MENUS:
            d.text((mx, my0 + 3), m, font=fuib, fill=BLACK)
            w0 = d.textlength(m[0], font=fuib)
            d.line([(mx, my0 + 14), (mx + w0 - 1, my0 + 14)], fill=BLACK)
            mx += d.textlength(m, font=fuib) + self.MENU_GAP
        y = my1 + 1
        d.line([(22, ty0), (22, y - 1)], fill=BLACK)      # the column's fence
        d.line([(3, y), (W - 4, y)], fill=BLACK)
        y += 1

        # one toolbar row: the real buttons, then the controls worth keeping
        top = y + 3
        d.rectangle([4, y, W - 5, top + self.BTN_H + 2], fill=FACE)
        bx = 11
        wng = self.f.symbol("wingdings", 13)
        for grp in self.TOOLBAR:
            for name in grp:
                spr = self.icons.get(name)
                if spr is not None:
                    im.paste(spr, (bx, top))
                    bx += spr.width + self.BTN_GAP
                else:
                    button(d, (bx, top, bx + 22, top + self.BTN_H))
                    if wng:
                        d.text((bx + 11, top + self.BTN_H // 2), "1",
                               font=wng, fill=BLACK, anchor="mm")
                    bx += 22 + self.BTN_GAP
            bx += self.GROUP_GAP

        # The reference's first toolbar row ends with the zoom field, so this
        # one does too. The font and size combos live on the second row in the
        # real thing; merging them in here only overflows the width.
        zx, zw = bx + 2, 46
        d.rectangle([zx, top + 1, zx + zw, top + self.BTN_H - 1], fill=WHITE)
        bevel(d, (zx, top + 1, zx + zw + 1, top + self.BTN_H), sunken=True)
        d.text((zx + zw // 2, top + self.BTN_H // 2 - 1), "100%",
               font=self.f.get("ui_bold", 11), fill=BLACK, anchor="mm")
        ax = zx + zw + 3
        button(d, (ax, top, ax + 16, top + self.BTN_H))
        d.polygon([(ax + 5, top + 9), (ax + 11, top + 9), (ax + 8, top + 13)],
                  fill=BLACK)
        y = top + self.BTN_H + 3
        d.line([(4, y), (W - 5, y)], fill=SHADOW)
        d.line([(4, y + 1), (W - 5, y + 1)], fill=WHITE)
        y += 2

        # ruler
        d.rectangle([4, y, W - 5, y + 14], fill=FACE)
        rl, rr = 44, W - 48
        d.rectangle([rl, y + 2, rr, y + 12], fill=WHITE)
        bevel(d, (rl, y + 2, rr + 1, y + 13), sunken=True)
        for i in range(0, rr - rl, 12):
            hh = 5 if i % 60 == 0 else 3
            d.line([(rl + i, y + 7 - hh // 2), (rl + i, y + 7 + hh // 2)], fill=SHADOW)
        if mar:
            for mx2, gl in ((rl - 4, "6"), (rr - 2, "5")):
                d.text((mx2 + 3, y + 7), gl, font=mar, fill=BLACK, anchor="mm")
        y += 15

        doc = (4, y, W - 5, H - 1)
        d.rectangle(list(doc), fill=WHITE)
        d.line([(doc[0], doc[1]), (doc[2], doc[1])], fill=SHADOW)
        d.line([(doc[0], doc[1]), (doc[0], H - 1)], fill=SHADOW)
        return im, doc

    def _draw_embed_frame(self):
        """Hairline border and selection handles around where the capture goes.

        Baked into the static window so they upscale chunky like the rest of the
        chrome, and so the capture paints inside them rather than over them.
        """
        d = ImageDraw.Draw(self.base)
        x0, y0, x1, y1 = self._vp_logical
        d.line([(x0 - 1, y0 - 1), (x1, y0 - 1)], fill=BLACK)
        d.line([(x0 - 1, y0 - 1), (x0 - 1, y1)], fill=BLACK)
        d.line([(x1, y0 - 1), (x1, y1)], fill=BLACK)
        if not self.handles:
            return
        # Only the handles that are on screen: the object's bottom edge is
        # below the frame, so its bottom row of handles is not there to draw.
        for hx, hy in ((x0, y0), ((x0 + x1) // 2, y0), (x1, y0),
                       (x0, (y0 + y1) // 2), (x1, (y0 + y1) // 2)):
            d.rectangle([hx - 2, hy - 2, hx + 2, hy + 2], fill=BLACK)

    # -- contents

    def _art_for(self, text, maxw: int, maxh: int):
        key = (tuple(text) if isinstance(text, (list, tuple)) else text,
               maxw, maxh)
        if key not in self._art:
            if len(self._art) > 24:
                self._art.clear()
            self._art[key] = fit_wordart(text, self.f, maxw, maxh, **self.art_opts)
        return self._art[key]

    def _caption(self, im, d, head, sub, alpha, bullet, misspell,
                 reveal=None, caret=None):
        if alpha <= 4:
            return
        x0, y0, x1, _ = self.document
        lines = list(sub if isinstance(sub, (list, tuple)) else [sub])

        if self.layout == "side":
            # WordArt down the left, body beside it. Both are centred in the
            # header block so a short paragraph does not sit high against a
            # tall headline.
            aw = int((x1 - x0) * self.head_w)
            art = self._art_for(head, aw - 24, self.head_block - 16)
            ax = x0 + 14 + max(0, (aw - 24 - art.size[0]) // 2)
            ay = y0 + max(6, (self.head_block - art.size[1]) // 2)
            tx = x0 + aw + 6
            ty = y0 + max(8, (self.head_block
                              - len(lines) * self.body_lead) // 2)
        else:
            art = self._art_for(head, x1 - x0 - 56, self.head_h)
            ax = (x0 + x1) // 2 - art.size[0] // 2
            ay = y0 + 6
            tx = x0 + 22
            ty = y0 + 10 + self.head_h

        if alpha < 255:
            art = art.copy()
            art.putalpha(art.getchannel("A").point(lambda v: v * alpha // 255))
        im.paste(art, (ax, ay), art)

        fb = self.f.get("body", self.body_size)
        ink = tuple(int(255 - (255 - c) * alpha / 255) for c in BLACK)
        wng = self.f.symbol("wingdings", self.body_size - 1)
        if bullet and wng and self.layout != "side":
            d.text((tx, ty - 1), bullet, font=wng, fill=ink)
            tx += 18

        # Typing: the reveal fraction is spent over the whole paragraph, not
        # per line, so a long first line does not race a short second one.
        left = None
        if reveal is not None:
            left = int(round(sum(len(l) for l in lines)
                             * max(0.0, min(1.0, reveal))))

        for ln in lines:
            # Clamp before slicing: a bare `ln[:left]` with a negative `left`
            # is a slice from the END, so an untyped line renders as its own
            # tail instead of as nothing.
            take = len(ln) if left is None else max(0, min(len(ln), left))
            shown = ln[:take]
            typing_here = left is not None and 0 <= left <= len(ln)
            if left is not None:
                left -= len(ln)
            if shown:
                d.text((tx, ty), shown, font=fb, fill=ink)
            # A word only gets its squiggle once it has been typed in full --
            # Word does not mark a word it is still watching you write.
            for word in misspell:
                i = shown.find(word)
                if i >= 0:
                    sx = tx + d.textlength(shown[:i], font=fb)
                    squiggle(d, int(sx), int(sx + d.textlength(word, font=fb)),
                             ty + self.body_size + 2)
            if caret and typing_here:
                cx = tx + d.textlength(shown, font=fb)
                d.rectangle([cx + 1, ty, cx + 2, ty + self.body_size], fill=BLACK)
                caret = False
            ty += self.body_lead

    def _sysbox(self, d, cx0, cx1, by0, by1, w):
        """A Windows 3.1 control-menu box.

        Not a bevelled button — a flat black-outlined bar with a white fill and
        a one-pixel grey drop shadow, centred in the control column. The
        application's is wide, the document's is half that; the difference is
        the only thing distinguishing the two, so it is worth getting right.
        """
        x0 = (cx0 + cx1) // 2 - w // 2
        x1 = x0 + w - 1
        y0 = (by0 + by1) // 2 - 1
        d.rectangle([x0, y0, x1, y0 + 2], fill=WHITE, outline=BLACK)
        d.line([(x1 + 1, y0 + 1), (x1 + 1, y0 + 2)], fill=SHADOW)
        d.line([(x0 + 1, y0 + 3), (x1 + 1, y0 + 3)], fill=SHADOW)



STYLES = {"word6": Word6Chrome}


def make(cfg: dict, out_size, title_default="Untitled"):
    """The chrome a `render.chrome` block asks for."""
    style = cfg.get("style", "word6")
    if style not in STYLES:
        raise SystemExit(f"unknown chrome style {style!r}; have {sorted(STYLES)}")
    return STYLES[style](cfg, out_size, title_default)
