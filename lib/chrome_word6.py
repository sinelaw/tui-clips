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

and per beat, optionally, `bullet` (a Wingdings key), `misspell` (words to draw
Word's red zigzag under) and `tip` (lines for the Assistant).

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
INFO_BG = (255, 255, 206)       # tooltip / Assistant yellow
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


def fit_wordart(text: str, fonts: Fonts, maxw: int, maxh: int, **kw) -> Image.Image:
    """Largest WordArt of `text` that fits the measure. Binary search on size."""
    lo, hi, best = 8, 120, None
    while lo <= hi:
        mid = (lo + hi) // 2
        art = wordart(text, fonts, size=mid, **kw)
        if art.size[0] <= maxw and art.size[1] <= maxh:
            best, lo = art, mid + 1
        else:
            hi = mid - 1
    return best if best is not None else wordart(text, fonts, size=8, **kw)

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
        self.head_h = int(cfg.get("headline_height", 38))
        self.body_lines = int(cfg.get("body_lines", 2))
        self.body_size = int(cfg.get("body_size", 12))
        self.body_lead = int(cfg.get("body_lead", 14))
        self.handles = bool(cfg.get("handles", True))
        self.status_extra = cfg.get("status", "At 2.5\"   Ln 6   Col 1")
        self.art_opts = dict(cfg.get("wordart", {}))

        self._art: dict = {}
        self.base, self.document = self._build()
        bx0, by0, bx1, by1 = self.document
        self._vp_logical = (bx0 + 14,
                            by0 + 6 + self.head_h + 4
                            + self.body_lines * self.body_lead + 6,
                            bx1 - 14, by1 - 8)
        self._draw_embed_frame()

    # -- what the renderer needs

    def viewport(self):
        """(x, y, w, h) in output pixels."""
        x0, y0, x1, y1 = self._vp_logical
        s = self.scale
        return (x0 * s, y0 * s, (x1 - x0) * s, (y1 - y0) * s)

    def frame(self, head: str, sub, alpha: int = 255, page: int = 1,
              total: int = 1, bullet=None, misspell=(), tip=None) -> Image.Image:
        """The window for one frame, upscaled, with a hole for the capture."""
        im = self.base.copy()
        d = ImageDraw.Draw(im)
        self._caption(im, d, head, sub, alpha, bullet, misspell)
        self._status(d, page, total)
        return im.resize((self.OW, self.OH), Image.NEAREST)

    def overlay(self, canvas: Image.Image, tip=None) -> None:
        """Drawn *after* the capture is pasted. The Assistant leans over the
        document, so anything under him — the embedded object included — has to
        be down already or he is simply painted out."""
        if not tip:
            return
        lay = Image.new("RGB", (self.W, self.H), FACE)
        d = ImageDraw.Draw(lay)
        mask = Image.new("L", (self.W, self.H), 0)
        md = ImageDraw.Draw(mask)
        self._assistant(d, tip, md)
        canvas.paste(lay.resize((self.OW, self.OH), Image.NEAREST), (0, 0),
                     mask.resize((self.OW, self.OH), Image.NEAREST))

    # -- the window itself

    def _build(self):
        im = Image.new("RGB", (self.W, self.H), FACE)
        d = ImageDraw.Draw(im)
        W, H = self.W, self.H
        fui = self.f.get("ui", 11)
        fuib = self.f.get("ui_bold", 11)
        fsm = self.f.get("ui", 10)
        mar = self.f.symbol("marlett", 10)

        # window frame: 1px black, 2px face, 1px black
        d.rectangle([0, 0, W - 1, H - 1], fill=FACE, outline=BLACK)
        d.rectangle([3, 3, W - 4, H - 4], outline=BLACK)

        y = 4
        # title bar
        d.rectangle([4, y, W - 5, y + self.TITLE_H - 1], fill=TITLE)
        d.text(((4 + W - 5) // 2, y + self.TITLE_H // 2), self.title,
               font=fuib, fill=WHITE, anchor="mm")
        button(d, (5, y + 1, 24, y + self.TITLE_H - 1))
        d.rectangle([10, y + self.TITLE_H // 2 - 2, 19, y + self.TITLE_H // 2 + 1],
                    fill=FACE, outline=BLACK)
        for i, g in enumerate("01"):        # Marlett: minimise, maximise
            bx = W - 6 - (2 - i) * 20
            button(d, (bx, y + 1, bx + 19, y + self.TITLE_H - 1))
            if mar:
                d.text((bx + 9, y + self.TITLE_H // 2), g, font=mar,
                       fill=BLACK, anchor="mm")
        y += self.TITLE_H
        d.line([(4, y), (W - 5, y)], fill=BLACK)
        y += 1

        # menu bar — white, as Windows 3.1 had it
        d.rectangle([4, y, W - 5, y + self.MENU_H - 1], fill=MENU_BG)
        mx = 14
        for m in self.MENUS:
            d.text((mx, y + 3), m, font=fui, fill=BLACK)
            d.line([(mx, y + 14), (mx + d.textlength(m[0], font=fui), y + 14)],
                   fill=BLACK)
            mx += d.textlength(m, font=fui) + 16
        y += self.MENU_H
        d.line([(4, y), (W - 5, y)], fill=BLACK)
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

        def combo(x, w, txt):
            """A field and a *detached* drop-down button, as Word 6.0 drew it."""
            d.rectangle([x, top + 2, x + w, top + self.BTN_H - 2], fill=WHITE)
            bevel(d, (x, top + 2, x + w + 1, top + self.BTN_H - 1), sunken=True)
            d.text((x + 4, top + 5), txt, font=fsm, fill=BLACK)
            bb = x + w + 3
            button(d, (bb, top + 1, bb + 15, top + self.BTN_H - 1))
            if mar:
                d.text((bb + 7, top + self.BTN_H // 2), "6", font=mar,
                       fill=BLACK, anchor="mm")
            return bb + 15

        bx = combo(bx + 2, 88, "Times New Roman") + 6
        bx = combo(bx, 30, "10") + 8
        for lbl, role in (("B", "ui_bold"), ("I", "ui"), ("U", "ui")):
            button(d, (bx, top, bx + 22, top + self.BTN_H))
            d.text((bx + 11, top + self.BTN_H // 2), lbl,
                   font=self.f.get(role, 13), fill=BLACK, anchor="mm")
            bx += 23
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

        doc = (4, y, W - 5, H - 38)
        d.rectangle(list(doc), fill=WHITE)
        bevel(d, (doc[0], doc[1], doc[2] + 1, doc[3] + 1), sunken=True)
        return im, doc

    def _draw_embed_frame(self):
        """Hairline border and selection handles around where the capture goes.

        Baked into the static window so they upscale chunky like the rest of the
        chrome, and so the capture paints inside them rather than over them.
        """
        d = ImageDraw.Draw(self.base)
        x0, y0, x1, y1 = self._vp_logical
        d.rectangle([x0 - 1, y0 - 1, x1, y1], outline=BLACK)
        if not self.handles:
            return
        for hx, hy in ((x0, y0), ((x0 + x1) // 2, y0), (x1, y0),
                       (x0, (y0 + y1) // 2), (x1, (y0 + y1) // 2),
                       (x0, y1), ((x0 + x1) // 2, y1), (x1, y1)):
            d.rectangle([hx - 2, hy - 2, hx + 2, hy + 2], fill=BLACK)

    # -- contents

    def _art_for(self, text: str, maxw: int, maxh: int):
        key = (text, maxw, maxh)
        if key not in self._art:
            if len(self._art) > 24:
                self._art.clear()
            self._art[key] = fit_wordart(text, self.f, maxw, maxh, **self.art_opts)
        return self._art[key]

    def _caption(self, im, d, head, sub, alpha, bullet, misspell):
        if alpha <= 4:
            return
        x0, y0, x1, _ = self.document
        art = self._art_for(head, x1 - x0 - 56, self.head_h)
        if alpha < 255:
            art = art.copy()
            art.putalpha(art.getchannel("A").point(lambda v: v * alpha // 255))
        im.paste(art, ((x0 + x1) // 2 - art.size[0] // 2, y0 + 6), art)

        fb = self.f.get("body", self.body_size)
        ink = tuple(int(255 - (255 - c) * alpha / 255) for c in BLACK)
        tx = x0 + 22
        wng = self.f.symbol("wingdings", self.body_size - 1)
        ty = y0 + 10 + self.head_h
        if bullet and wng:
            d.text((tx, ty - 1), bullet, font=wng, fill=ink)
        tx += 18 if bullet else 0
        for ln in (sub if isinstance(sub, (list, tuple)) else [sub]):
            d.text((tx, ty), ln, font=fb, fill=ink)
            for word in misspell:
                i = ln.find(word)
                if i >= 0:
                    sx = tx + d.textlength(ln[:i], font=fb)
                    squiggle(d, int(sx), int(sx + d.textlength(word, font=fb)),
                             ty + self.body_size + 2)
            ty += self.body_lead

    def _assistant(self, d, lines, md=None):
        """The paperclip. He belongs to Word 97 and is three years early.

        `md` collects his silhouette, so the caller can composite just him
        rather than a rectangle of document around him.
        """
        x1b, y1b = self.document[2] - 14, self.document[3] - 14
        x0, y0, x1, y1 = x1b - 182, y1b - 54, x1b, y1b
        if md is not None:
            md.rectangle([x0, y0, x1, y1], fill=255)
            md.rounded_rectangle([x0 - 38, y0 + 4, x0 - 8, y0 + 48],
                                 radius=13, fill=255)
        d.rectangle([x0, y0, x1, y1], fill=INFO_BG, outline=BLACK)
        d.text((x0 + 7, y0 + 6), lines[0], font=self.f.get("ui_bold", 10), fill=BLACK)
        y = y0 + 19
        for ln in lines[1:]:
            d.text((x0 + 7, y), ln, font=self.f.get("ui", 10), fill=BLACK)
            y += 12
        cx, cy = x0 - 24, y0 + 24
        for r in (11, 6):
            d.rounded_rectangle([cx - r, cy - r - 7, cx + r, cy + r + 11],
                                radius=r, outline=(100, 100, 120), width=3)
        for ex in (cx - 5, cx + 4):
            d.ellipse([ex - 4, cy - 15, ex + 4, cy - 7], fill=WHITE, outline=BLACK)
            d.ellipse([ex - 2, cy - 13, ex + 1, cy - 10], fill=BLACK)

    def _status(self, d, page, total):
        W, H = self.W, self.H
        fsm = self.f.get("ui", 10)
        sy = H - 34
        d.rectangle([4, sy, W - 5, H - 5], fill=FACE)
        d.line([(4, sy), (W - 5, sy)], fill=SHADOW)
        d.line([(4, sy + 1), (W - 5, sy + 1)], fill=WHITE)
        for fx, txt in ((12, f"Page {page}    Sec 1     {page}/{total}"),
                        (166, self.status_extra),
                        (W - 146, "REC  MRK"), (W - 82, "EXT  OVR  WPH")):
            d.text((fx, sy + 9), txt, font=fsm, fill=BLACK)
        for sx in (158, W - 156, W - 92):
            d.line([(sx, sy + 5), (sx, H - 10)], fill=SHADOW)
            d.line([(sx + 1, sy + 5), (sx + 1, H - 10)], fill=WHITE)


STYLES = {"word6": Word6Chrome}


def make(cfg: dict, out_size, title_default="Untitled"):
    """The chrome a `render.chrome` block asks for."""
    style = cfg.get("style", "word6")
    if style not in STYLES:
        raise SystemExit(f"unknown chrome style {style!r}; have {sorted(STYLES)}")
    return STYLES[style](cfg, out_size, title_default)
