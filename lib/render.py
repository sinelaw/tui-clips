"""Compose a TUI feature clip into a PNG frame sequence.

Three shapes. Two are chosen by how many panes the spec captures:

  compare  two builds, `before` and `after`
           intro   both captures side by side, labelled, with a caption
           zoom    the BEFORE panel slides out left while AFTER fills the frame
  solo     one build, for a feature with nothing to compare against
           intro   the whole capture, centred, as an establishing shot
           zoom    it grows in place to fill the frame

From there both are the same:

    hold    one beat per annotation: everything but the band is dimmed
    pan     travel between annotations, captions cross-fading
    outro   the dim releases and the end caption appears

A solo clip may also name a `render.chrome`: a period application window that
wraps the viewport and hosts the captions, in place of the header band and the
caption bar. The storyboard is unchanged — intro, zoom, one beat per
annotation, outro — but the capture is composited into the window's document
area, and the beat's head and sub are set inside it. See `chrome_word6`.

The third, `explode`, is a different storyboard on one capture: the screen is
taken apart into the rects a set of annotated pieces name, and the camera
visits each one in turn. A piece that has pieces of its own bursts open in
place when the camera reaches it. See `ExplodeRenderer`.

Rects are given in terminal cells (rows and columns) throughout, which is what
`tui-grid` prints and what a UI-tree dump reports, so the numbers survive a
font or geometry change.
"""
from __future__ import annotations

import bisect
import glob
import json
import math
import os
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

import chrome_word6

def _font(*names: str) -> str | None:
    """the first of `names` that this machine actually has.

    Distributions do not agree on where a font file lives -- /usr/share/fonts/
    TTF on Arch, /usr/share/fonts/truetype/<family> on Debian -- so the family
    is searched for rather than spelled out, and the alternatives are listed
    in preference order.
    """
    for n in names:
        hit = sorted(glob.glob(f"/usr/share/fonts/**/{n}", recursive=True))
        if hit:
            return hit[0]
    return None


BOLD = _font("DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf",
             "FreeSansBold.ttf")
# Colour emoji are bitmaps, and Noto's are cut at exactly one size: asking
# FreeType for any other raises. So they are drawn at 109 and scaled, which is
# also why they cannot simply be a second font in a text run.
EMOJI = _font("NotoColorEmoji*.ttf")
EMOJI_PX = 109
MONO = _font("JetBrainsMono-Regular.ttf", "DejaVuSansMono.ttf",
             "LiberationMono-Regular.ttf")
if not BOLD or not MONO:
    raise SystemExit(
        "no usable fonts: tui-clips wants a bold sans (DejaVu Sans Bold) and "
        "a mono (JetBrains Mono, or DejaVu Sans Mono) under /usr/share/fonts")

DEFAULT_THEME = {
    "bg": [10, 10, 12],
    "caption_bg": [17, 17, 22],
    "panel_border": [44, 44, 52],
    "muted": [139, 139, 150],
    "fg": [232, 232, 238],
    "before": [255, 107, 94],
    "after": [74, 222, 128],
}
DEFAULT_TIMING = {"title": 0.0, "intro": 2.1, "zoom": 0.8, "hold": 1.8,
                  "pan": 0.4, "push": 0.9, "outro": 1.0}
DEFAULT_LABELS = {"before": "BEFORE", "after": "AFTER", "solo": "NEW",
                  "explode": "ANATOMY", "donut": "BREAKDOWN"}


def cap_of(a: dict):
    """a beat's caption-bar lines.

    Both are optional now: a beat that says its piece with a `note` in the
    frame wants the bar for where it is, or for nothing at all.
    """
    return a.get("head", ""), a.get("sub", "")


def ease(t: float) -> float:
    """easeInOutCubic"""
    t = max(0.0, min(1.0, t))
    return 4 * t * t * t if t < 0.5 else 1 - pow(-2 * t + 2, 3) / 2


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def fade_c(c, a: int):
    return tuple(int(v * a / 255) for v in c)


def mode_of(spec: dict) -> str:
    """'donut' when the spec carries its own data rather than a screen, else
    'explode' when it says how to take the capture apart, else 'compare' when
    it captures both a before and an after, else 'solo'"""
    if spec.get("render", {}).get("donut"):
        if spec.get("capture", {}).get("panes"):
            raise SystemExit(
                "a donut clip draws its own picture from render.donut.items; "
                "it films nothing, so capture.panes must be absent")
        return "donut"
    panes = spec["capture"]["panes"]
    if spec.get("render", {}).get("explode"):
        if len(panes) != 1:
            raise SystemExit(
                "an explode clip takes one capture apart; capture.panes has "
                f"{sorted(panes)}")
        return "explode"
    if {"before", "after"} <= set(panes):
        return "compare"
    if len(panes) == 1:
        return "solo"
    raise SystemExit(
        "capture.panes must be either {'before', 'after'} for a comparison "
        f"or exactly one pane for a solo clip; got {sorted(panes)}"
    )


def subject_pane(spec: dict) -> str:
    """the pane the annotations are drawn on"""
    panes = spec["capture"]["panes"]
    return "after" if mode_of(spec) == "compare" else next(iter(panes))


def make(spec: dict, captures: dict[str, str], outdir: str,
         shots: dict[str, str] | None = None,
         runs: dict[str, list[str]] | None = None):
    """the renderer this spec asks for"""
    mode = mode_of(spec)
    if mode == "donut":
        style = spec["render"]["donut"].get("style", "smooth")
        if style == "ansi":
            return AnsiDonutRenderer(spec, outdir)
        if style != "smooth":
            raise SystemExit(
                f"render.donut.style is 'smooth' or 'ansi'; got {style!r}")
        return DonutRenderer(spec, outdir)
    if mode == "explode":
        return ExplodeRenderer(spec, captures, outdir)
    return Renderer(spec, captures, outdir, shots, runs)



class CRT:
    """A phosphor look laid over the finished frame.

    Four cheap things, in the order a real tube does them. Two of them are
    the ones that actually read as a CRT and two are what stop it looking
    like a filter:

      * `shift`      the red and blue channels pulled apart by a pixel or
                     two, which is the convergence error of a three-gun tube
      * `bloom`      the bright parts bled into their neighbours -- phosphor
                     glows, it does not stop at the pixel
      * `scanlines`  every `gap`-th row darkened
      * `vignette`   the corners taken down, because the screen is curved
                     even when the image is not

    The masks are built once and reused: they depend only on the canvas, and
    rebuilding them per frame is most of the cost of the whole pass.

    Scanlines are the part that goes wrong at small sizes. A one-pixel line
    every three pixels is a moire pattern once the video is scaled down a
    feed, so the gap comes down with the canvas like every other constant
    here, and the default is light enough to survive the encode -- a deep
    comb is the first thing h.264 turns to mush.
    """

    def __init__(self, cfg, w: int, h: int, k: float = 1.0):
        cfg = {} if cfg is True else dict(cfg or {})
        self.scan = float(cfg.get("scanlines", 0.34))
        self.gap = max(2, int(round(float(cfg.get("gap", 4)) * k)))
        self.bloom = float(cfg.get("bloom", 0.55))
        self.blur = max(1.0, float(cfg.get("blur", 9)) * k)
        self.shift = int(round(float(cfg.get("shift", 3)) * k))
        self.vig = float(cfg.get("vignette", 0.45))
        self.curve = float(cfg.get("curve", 0.10))
        self.cells = int(cfg.get("cells", 24))
        # Seconds of power-off at the end of the clip. 0 to hold instead.
        self.shutdown = float(cfg.get("shutdown", 0.0))
        self.off_color = tuple(cfg.get("off_color", (223, 255, 233)))
        # How hard the dying raster blooms. It is the brightest thing in the
        # clip by a long way, so it gets its own number rather than riding
        # the `bloom` used for ordinary phosphor.
        self.off_glow = float(cfg.get("off_glow", 1.35))
        self.kk = k
        self.w, self.h = w, h
        self._scan = None
        self._vig = None
        self._mesh = None
        self._halo = {}

    def scan_mask(self):
        """one dark row every `gap`, tiled -- built once, reused every frame"""
        if self._scan is None:
            if self.scan <= 0:
                self._scan = Image.new("L", (self.w, self.h), 255)
            else:
                tile = Image.new("L", (self.w, self.gap), 255)
                ImageDraw.Draw(tile).rectangle(
                    [0, 0, self.w - 1, 0], fill=int(255 * (1.0 - self.scan)))
                m = Image.new("L", (self.w, self.h), 255)
                for y in range(0, self.h, self.gap):
                    m.paste(tile, (0, y))
                self._scan = m
        return self._scan

    def vig_mask(self):
        """bright in the middle, down by `vig` at the corners"""
        if self._vig is None:
            if self.vig <= 0:
                self._vig = Image.new("L", (self.w, self.h), 255)
            else:
                # radial_gradient is black at the centre and white at the
                # edge, which is the shape wanted, inverted and scaled.
                g = Image.radial_gradient("L").resize((self.w, self.h),
                                                      Image.BILINEAR)
                self._vig = g.point(
                    lambda v: int(255 - self.vig * v))
        return self._vig

    def halo(self, d: int):
        """a radial glow sprite `d` across, cached by size.

        The dot at the end needs its own, because a blur cannot give it one.
        A Gaussian conserves energy: spread five bright pixels over a radius
        of fifty and what comes back is arithmetically correct and visually
        nothing. The line and the squeezed raster are large enough sources to
        survive that; the dot is not, and it is the one frame everybody
        remembers about a CRT going off.
        """
        if d not in self._halo:
            g = Image.radial_gradient("L").resize((d, d), Image.BILINEAR)
            # 0 at the centre, 255 at the edge -- inverted, and squared up so
            # the falloff is a glow rather than a flat disc
            self._halo[d] = g.point(lambda v: max(0, 255 - int(v * 1.9)))
        return self._halo[d]

    def mesh(self):
        """the barrel warp, as a grid of quads -- built once per canvas.

        The curve is the trait that actually says "tube". Scanlines and a
        vignette on a flat rectangle read as a filter laid over a screenshot;
        bending the raster is what makes it a thing with glass in front of it.

        Each destination cell samples from a source quad pushed outward by
        `1 + curve*r²`, so the middle is near enough untouched and the corners
        reach past the edge of the image and come back black -- which is the
        rounded-off corner of the tube, for free.
        """
        if self._mesh is None:
            k, w, h, n = self.curve, self.w, self.h, self.cells
            quads = []
            for gy in range(n):
                for gx in range(n):
                    x0, x1 = w * gx / n, w * (gx + 1) / n
                    y0, y1 = h * gy / n, h * (gy + 1) / n
                    src = []
                    # MESH wants the source quad as nw, sw, se, ne
                    for px, py in ((x0, y0), (x0, y1), (x1, y1), (x1, y0)):
                        u, v = (px / w) * 2 - 1, (py / h) * 2 - 1
                        f = 1.0 + k * (u * u + v * v)
                        src += [(u * f + 1) / 2 * w, (v * f + 1) / 2 * h]
                    quads.append(((int(round(x0)), int(round(y0)),
                                   int(round(x1)), int(round(y1))), tuple(src)))
            self._mesh = quads
        return self._mesh

    def power_off(self, im: Image.Image, q: float) -> Image.Image:
        """the tube losing its deflection, over `q` in 0..1.

        A CRT does not fade out, it collapses. The vertical deflection gives
        up first and the whole raster is squeezed into one over-bright
        horizontal line -- the same beam energy over a fraction of the
        height, which is why it gets brighter as it gets thinner rather than
        dimmer. Then the horizontal deflection goes and the line shortens to
        a dot, and the dot sits there on the phosphor for a moment after
        there is nothing driving it.

        Doing it in that order is the whole effect. Fading the picture to
        black and calling it a power-off is what it looks like when someone
        has only ever seen it described.
        """
        w, h = im.size
        q = max(0.0, min(1.0, q))
        out = Image.new("RGB", (w, h), (0, 0, 0))
        lh = max(2, int(round(3 * self.kk)))
        cx, cy = w // 2, h // 2
        if q < 0.42:                                  # the raster squeezes
            t = q / 0.42
            hh = max(lh, int(h * (1.0 - t) ** 2.3))
            band = im.resize((w, hh), Image.BILINEAR)
            band = band.point(lambda v: min(255, int(v * (1.0 + 3.0 * t))))
            out.paste(band, (0, (h - hh) // 2))
        elif q < 0.76:                                # the line shortens
            t = (q - 0.42) / 0.34
            ww = max(2, int(w * (1.0 - t) ** 1.4))
            ImageDraw.Draw(out).rectangle(
                [cx - ww // 2, cy - lh // 2, cx + ww // 2, cy + lh // 2],
                fill=self.off_color)
        else:                                         # the dot decays
            t = (q - 0.76) / 0.24
            fade = (1.0 - t) ** 1.5
            r = max(1, int(lh * 1.7 * (1.0 - t)))
            c = tuple(int(v * fade) for v in self.off_color)
            # the halo first, so the dot itself sits on top of it
            hd = max(8, int((lh * 26 * (1.0 - t) + 10) * self.kk)) | 1
            sprite = Image.new("RGB", (hd, hd),
                               tuple(int(v * fade * 0.85)
                                     for v in self.off_color))
            glow = Image.new("RGB", (w, h), (0, 0, 0))
            glow.paste(sprite, (cx - hd // 2, cy - hd // 2), self.halo(hd))
            out = ImageChops.screen(out, glow)
            ImageDraw.Draw(out).ellipse([cx - r, cy - r, cx + r, cy + r],
                                        fill=c)
        # Two radii, not one: a tight halo for the shape and a wide one for
        # the wash it throws on the glass. A single blur wide enough to give
        # the wash loses the hard edge of the line, and one tight enough to
        # keep the edge does not spill at all.
        r = self.blur * 2.0
        tight = out.filter(ImageFilter.GaussianBlur(r))
        wide = out.filter(ImageFilter.GaussianBlur(r * 3.2))
        glow = ImageChops.add(tight.point(lambda v: int(v * 0.95)),
                              wide.point(lambda v: int(v * 0.75)))
        if self.off_glow != 1.0:
            glow = glow.point(lambda v: min(255, int(v * self.off_glow)))
        return ImageChops.screen(out, glow)

    def __call__(self, im: Image.Image, q=None) -> Image.Image:
        im = im.convert("RGB")
        if self.shift:
            r, g, b = im.split()
            r = ImageChops.offset(r, -self.shift, 0)
            b = ImageChops.offset(b, self.shift, 0)
            im = Image.merge("RGB", (r, g, b))
        if self.bloom > 0:
            lit = im.point(lambda v: max(0, int((v - 150) * 1.9)))
            lit = lit.filter(ImageFilter.GaussianBlur(self.blur))
            if self.bloom < 1.0:
                lit = lit.point(lambda v: int(v * self.bloom))
            im = ImageChops.screen(im, lit)
        if self.scan > 0:
            im = ImageChops.multiply(im, Image.merge(
                "RGB", (self.scan_mask(),) * 3))
        # Curve last of the geometry, so the scanlines bend with the raster
        # rather than staying flat behind a warped picture.
        if self.curve > 0:
            im = im.transform((self.w, self.h), Image.MESH, self.mesh(),
                              Image.BILINEAR, fillcolor=(0, 0, 0))
        if self.vig > 0:
            im = ImageChops.multiply(im, Image.merge(
                "RGB", (self.vig_mask(),) * 3))
        # Last, and over the finished tube: it is the tube that is dying.
        if q is not None:
            im = self.power_off(im, q)
        return im


def post_fx(obj, im, n=None, nf=None):
    """the finished frame, through whatever whole-frame pass the spec asked for.

    `n` and `nf` are only needed by a pass that has to know where the end is
    -- at present the CRT's power-off, which takes the last `shutdown`
    seconds of the clip rather than adding any.
    """
    fx = getattr(obj, "crt_fx", None)
    if fx is None:
        return im
    q = None
    if fx.shutdown > 0 and n is not None and nf:
        start = nf - int(round(fx.shutdown * float(getattr(obj, "fps", 60))))
        if n >= start:
            q = (n - start) / float(max(1, nf - 1 - start))
    return fx(im, q)


class Renderer:
    def __init__(self, spec: dict, captures: dict[str, str], outdir: str,
                 shots: dict[str, str] | None = None,
                 runs: dict[str, list[str]] | None = None):
        r = spec["render"]
        self.spec = spec
        self.r = r
        self.outdir = outdir
        self.mode = mode_of(spec)

        self.A = Image.open(captures[subject_pane(spec)]).convert("RGB")
        self.B = None
        if self.mode == "compare":
            self.B = Image.open(captures["before"]).convert("RGB")
            if self.A.size != self.B.size:
                raise SystemExit(
                    f"captures differ in size: {self.B.size} vs {self.A.size}; "
                    "both panes must use the same --geometry and --font"
                )
        self.IW, self.IH = self.A.size

        # Named shots: screens taken part-way through the key sequence, so one
        # clip can show a caret move and what it did. A beat's `shot` picks
        # one; the pan into that beat cross-fades to it, which is the move.
        # Every shot is the same terminal, so they share the capture's grid —
        # a differing size means a differing --geometry or --font, and the
        # camera arithmetic would be wrong for one of them.
        self.shots = {}
        for nm, path in (shots or {}).items():
            img = Image.open(path).convert("RGB")
            if img.size != self.A.size:
                raise SystemExit(
                    f"shot {nm!r} is {img.size}, capture is {self.A.size}; "
                    "every shot must come from the same run")
            self.shots[nm] = img

        # Recorded runs: a `{"record": ...}` step films the window with x11grab
        # instead of photographing it, and a beat plays the whole sequence by
        # naming it in `shots`. Frames stay on disk and are opened as needed --
        # a few seconds at 30fps is several gigabytes decoded, and each frame is
        # wanted for about two output frames and then never again.
        self.runs = dict(runs or {})
        self._runcache: dict = {}
        # A run's sidecar carries what is not in its pixels: when each kept
        # frame was taken, and where the pointer was at the time.
        self.paths = {nm: load_path(os.path.dirname(fs[0]))
                      for nm, fs in self.runs.items() if fs}
        cur = r.get("cursor", True)
        self.cursor_cfg = ({} if cur is True else dict(cur)) if cur else None
        for nm, frames in self.runs.items():
            probe = Image.open(frames[0])
            if probe.size != self.A.size:
                raise SystemExit(
                    f"recording {nm!r} is {probe.size}, capture is "
                    f"{self.A.size}; both come from the same window, so this "
                    "means the window moved or resized mid-run")

        self.rows = int(r["rows"])
        self.cols = int(r["cols"])
        self.RH = self.IH / self.rows
        self.CW = self.IW / self.cols

        self.W, self.H = r.get("size", [1080, 1080])
        self.fps = int(r.get("fps", 60))
        # A draft is the same picture at a fraction of the size, so every
        # pixel constant has to come down with it. Left at 1.0 a render is
        # unchanged; `tui-clip --draft` sets it alongside a halved `size`.
        # Without it a draft shows notes and captions at twice their finished
        # size, and the framing decisions a draft exists to check -- what a
        # note points at, whether a pane is wide enough -- are exactly the
        # ones it would get wrong.
        self.dk = float(r.get("draft_scale", 1.0))
        # A whole-frame pass, applied on the way to disk (see post_fx).
        self.crt_fx = CRT(r["crt"], self.W, self.H, self.dk) if r.get("crt") else None
        self.header_h = self.k(r.get("header_height", 100))
        self.cap_h = self.k(r.get("caption_height", 130))
        # The bar exists for the beats. A clip whose beats all say their piece
        # in the frame has nothing to put in it, and a strip of empty chrome
        # across the bottom is worse than no strip: the intro and outro
        # captions float over the ground instead, which the vignette has
        # already darkened for them.
        self.bar = any(a.get("head") or a.get("sub")
                       for a in r.get("annotations", []))
        self.cap_y = self.H - (self.cap_h if self.bar else 0)
        self.vp = (0, self.header_h, self.W, self.cap_y - self.header_h)

        # A chrome owns the whole frame: it supplies the viewport the capture
        # lands in, and it draws the captions itself. The header band and the
        # caption bar are then never painted -- they would sit on top of a
        # window that already has somewhere to put both.
        self.chrome = None
        if r.get("chrome"):
            if self.mode == "compare":
                raise SystemExit(
                    "render.chrome is for solo clips; a comparison puts two "
                    "captures side by side and a document has room for one")
            self.chrome = chrome_word6.make(
                r["chrome"], (self.W, self.H), r.get("title", "Untitled"))
            self.vp = self.chrome.viewport()
            self.header_h = self.vp[1]
            self.cap_y = self.vp[1] + self.vp[3]

        th = dict(DEFAULT_THEME)
        th.update(r.get("theme", {}))
        self.th = {k: tuple(v) for k, v in th.items()}

        self.t = dict(DEFAULT_TIMING)
        given = dict(r.get("timing", {}))
        if "swipe" in given and "zoom" not in given:   # pre-solo spelling
            given["zoom"] = given.pop("swipe")
        self.t.update(given)

        self.ann = r["annotations"]
        # A named rect for the camera to sit on. Beats that share one share a
        # camera, so nothing moves between them but the band and the note --
        # which is what a clip of a static screen wants: a screen that pans
        # every 1.5s asks the reader to re-find their place every 1.5s, and
        # the thing that moved was never the code.
        self.views = r.get("views", {})
        # A card before the clip: a few lines, each with an effect of its own.
        # It is the only place the renderer says anything in its own voice, so
        # it is also the only place with an effect that is not a camera move.
        # Edges that fall off instead of stopping: the capture is a window
        # onto a screen that carries on past it, and a hard rectangular cut
        # says the opposite -- that what you can see is all there is.
        vg = r.get("vignette")
        self.vignette = ({} if vg is True else dict(vg)) if vg else None
        self._vmask: dict = {}
        self.card = r.get("title_card")
        if self.card and not self.t.get("title"):
            self.t["title"] = 2.6
        if not self.ann:
            raise SystemExit("render.annotations is empty; nothing to show")
        for a in self.ann:
            if a.get("view") and a["view"] not in self.views:
                raise SystemExit(
                    f"annotation view {a['view']!r} is not in render.views")
            run = a.get("shots")
            if isinstance(run, str) and run not in self.runs:
                raise SystemExit(
                    f'annotation shots {run!r} was never recorded; add '
                    f'{{"record": "{run}", "seconds": N}} to capture.keys')
            if a.get("shot") and a["shot"] not in self.shots:
                raise SystemExit(
                    f"annotation shot {a['shot']!r} was never taken; add "
                    f'{{"shot": "{a["shot"]}"}} to capture.keys')
            sw = a.get("swipe")
            if sw:
                if not sw.get("to"):
                    raise SystemExit('annotation swipe needs a "to" shot')
                if sw["to"] not in self.shots:
                    raise SystemExit(
                        f"annotation swipe.to {sw['to']!r} was never taken; "
                        f'add {{"shot": "{sw["to"]}"}} to capture.keys')
                if not sw.get("rows"):
                    raise SystemExit(
                        'annotation swipe needs "rows": the row indices to '
                        "wipe, in the order they should go")
        self.cap_intro = r.get("intro_caption", ["", ""])
        self.cap_outro = r.get("outro_caption", ["", ""])
        lb = dict(DEFAULT_LABELS)
        lb.update(r.get("labels", {}))
        self.labels = lb
        self.title = r.get("title", "")
        # the label that rides into the header once zoomed in
        self.lead = self.labels["after"] if self.mode == "compare" else self.labels["solo"]
        # The establishing shot is the first beat's screen, so it takes that
        # beat's colour: a clip whose first half is a BEFORE opens on it, and
        # opening it in the after's green says the wrong thing for two seconds.
        self.accent_intro = (self.th["before"] if self.mode == "compare"
                             else self.th[self.ann[0].get("tone", "after")])

        # intro panel geometry, derived so it always fits the canvas
        label_h, gutter, margin = self.k(44), self.k(24), self.k(16)
        if self.chrome:
            # No room for a label above the panel: the establishing shot is the
            # document's own page, so the capture opens small inside it.
            self.panel_y = self.vp[1]
            avail_h, avail_w = self.vp[3], self.vp[2]
        else:
            self.panel_y = self.header_h + label_h
            avail_h = self.cap_y - self.panel_y - self.k(6)
            avail_w = ((self.W - 2 * margin - gutter) / 2 if self.mode == "compare"
                       else self.W - 2 * margin)
        self.s_a = min(avail_w / self.IW, avail_h / self.IH)
        self.panel_w = self.IW * self.s_a
        self.panel_h = self.IH * self.s_a
        if self.mode == "compare":
            self.bx = (self.W - (2 * self.panel_w + gutter)) / 2
            self.ax = self.bx + self.panel_w + gutter
        else:
            self.bx = None
            self.ax = (self.vp[0] + (self.vp[2] - self.panel_w) / 2 if self.chrome
                       else (self.W - self.panel_w) / 2)
        self.s_c = self.vp[2] / self.IW

        self.f_label = ImageFont.truetype(BOLD, self.k(40))
        self.f_meta = ImageFont.truetype(MONO, self.k(24))
        self.f_hdr = ImageFont.truetype(BOLD, self.k(38))
        self.f_cap = ImageFont.truetype(BOLD, self.k(40))
        self.f_sub = ImageFont.truetype(MONO, self.k(23))
        self.f_note = ImageFont.truetype(BOLD, self.k(r.get("note_size", 30)))

        self._cache: dict = {}

    # -- geometry helpers ---------------------------------------------------
    def k(self, v: float) -> int:
        """`v` output pixels, brought down for a draft render"""
        if self.dk == 1.0:
            return int(v)
        return max(1, int(round(v * self.dk))) if v >= 1 else int(round(v * self.dk))

    def hold(self, i: int) -> float:
        """beat i's dwell -- its own `hold`, else the clip's"""
        return float(self.ann[i].get("hold", self.t["hold"]))

    def gap(self, i: int) -> float:
        """the travel out of beat i.

        A push moves a whole screen the width of the frame, and a wipe drags
        an edge across it; both want longer than the cross-fade a pan is, so
        both take their own timing. `wipe` falls back to `push`'s, which is
        the right order of magnitude for the same reason.
        """
        if i + 1 < len(self.ann):
            t = self.ann[i + 1].get("transition")
            if t == "push":
                return float(self.t["push"])
            if t == "wipe":
                return float(self.t.get("wipe", self.t["push"]))
        return float(self.t["pan"])

    def total(self) -> float:
        n = len(self.ann)
        return (self.t["title"] + self.t["intro"] + self.t["zoom"]
                + sum(self.hold(i) for i in range(n))
                + sum(self.gap(i) for i in range(n - 1)) + self.t["outro"])

    def banner(self, i: int, j: int, pan: float):
        """the word over the frame at this moment, and its colour.

        One clip can be about two things -- a before and an after -- and then
        a single banner for the whole run is wrong over one half of it. A beat
        naming its own `label` (and a `tone`, which is a theme key) takes the
        header for as long as it is on screen; the swap rides the travel, so
        it lands with the screen it describes.
        """
        a = self.ann[j if pan >= 0.5 else i]
        return a.get("label", self.lead), self.th[a.get("tone", "after")]

    def rect_of(self, a: dict):
        """a {rows, cols} rect in source pixels -- (x0, y0, x1, y1)

        `rows` is optional: a beat under a chrome fills the document and has no
        smaller rect to frame, so it defaults to the whole capture.
        """
        r0, r1 = a.get("rows", [0, self.rows])
        c0, c1 = a.get("cols", [0, self.cols])
        return (c0 * self.CW, r0 * self.RH, c1 * self.CW, r1 * self.RH)

    def rect(self, i: int):
        """annotation i's rect in source pixels"""
        return self.rect_of(self.ann[i])

    def ann_cy(self, i: int) -> float:
        """source-y that centres annotation i, clamped inside the image"""
        a = self.ann[i]
        half = self.vp[3] / self.s_c / 2
        rows = a.get("rows", [0, self.rows])
        cy = (rows[0] + rows[1]) / 2 * self.RH
        if self.IH <= 2 * half:          # capture shorter than the viewport
            return self.IH / 2
        return max(half, min(self.IH - half, cy))

    # How much of the frame a `camera: "fit"` beat leaves around its rect. The
    # band is drawn 6px outside the rect, its outline 3px wide, and the accent
    # tick another 12px left of that, so anything under ~24 clips the marks the
    # beat is made of.
    FIT_PAD = 56

    # A note is drawn outside the rect, so a beat that has one has to be
    # framed with somewhere to put it: reserve a band on the note's side and
    # push the rect off-centre by half of it, rather than padding both edges
    # and shrinking the code to buy room it does not need above.
    def note_room(self) -> int:
        """the band a note stands in, which is however tall the note is.

        It was a constant until the text became a knob, at which point the
        constant was quietly a second, disagreeing knob.
        """
        asc, desc = self.f_note.getmetrics()
        return int(asc + desc + self.NOTE_GAP + 2 * self.NOTE_PAD + 70)


    def cam(self, i: int):
        """beat i's camera -- (scale, source-x centred, source-y centred).

        The default frames the capture across the full width and travels only
        up and down, which is what a clip of one tall screen wants. A beat that
        asks to `fit` is framed on its own rect instead: the rect is centred in
        both axes and scaled to fill the viewport, so a narrow column can be
        brought to the middle of a wide frame. Nothing clamps it back inside
        the capture -- the ground beyond the edge is the point, and it paints
        as background.
        """
        a = self.ann[i]
        if a.get("view"):
            # The author framed this one; the beat's own rect is what the band
            # points at, not what the camera sits on.
            return self.fit(self.rect_of(self.views[a["view"]]))
        if a.get("camera") != "fit":
            return self.s_c, self.IW / 2, self.ann_cy(i)
        return self.fit(self.rect(i), a)

    def fit(self, rect, a: dict | None = None):
        """(scale, source-x, source-y) that frames `rect` in the viewport"""
        a = a or {}
        x0, y0, x1, y1 = rect
        # Down with the canvas, like every other pixel constant here. Left
        # unscaled it is 56px of 1080 in a full render and 56px of 540 in a
        # draft -- twice the margin -- so a draft framed a rect noticeably
        # smaller than the render it was standing in for, which is the one
        # thing a draft must not do.
        pad = 2 * self.k(self.FIT_PAD)
        note = bool(a.get("note"))
        room = self.note_room() if note else 0
        # Only the note's own axis is reserved. It is dealt sideways off the
        # leader's landing point, not off the rect, so the width it needs is
        # the frame's -- and buying it a margin beside the rect as well only
        # shrinks the code to pay for room the note never stands in.
        s = min((self.vp[2] - pad) / max(1.0, x1 - x0),
                (self.vp[3] - pad - room) / max(1.0, y1 - y0))
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        if note:
            # centring a point below the rect lifts the rect up the frame,
            # which is where it has to sit for a note drawn beneath it
            above = str(a.get("note_at", "below-right")).startswith("above")
            cy += (-1 if above else 1) * (room / 2) / s
        return s, cx, cy

    # Of each shot's slot in a run, the share spent dissolving into the next.
    # Zero by default: a dissolve between two screens that differ by more than
    # a line or two is a double exposure, not a movement. A run wanting the
    # softer join sets `crossfade` on the beat.
    STEP_XF = 0.0

    def run_image(self, name: str, k: int):
        """frame `k` of a recorded run, off disk, with a shallow cache."""
        key = (name, k)
        if key not in self._runcache:
            if len(self._runcache) > 6:
                self._runcache.clear()
            self._runcache[key] = Image.open(self.runs[name][k]).convert("RGB")
        return self._runcache[key]

    def shot(self, i: int, hp: float = 0.0):
        """what beat i is drawn on at hold-progress `hp`.

        -> (image, tag) or, mid-step, (imageA, tagA, imageB, tagB, blend). A
        beat with `shots` walks that list across its dwell; one with `shot`
        holds a single screen; one with neither gets the final capture.
        """
        a = self.ann[i]
        run = a.get("shots")
        if isinstance(run, str):
            # A recorded run: played straight through across the dwell, so the
            # motion comes out at the rate it was filmed at rather than stepped.
            frames = self.runs[run]
            n = len(frames)
            k = min(int(min(hp, 0.999999) * n), n - 1)
            return self.run_image(run, k), f"run:{run}:{k}"
        if not run:
            nm = a.get("shot")
            return (self.shots[nm], f"shot:{nm}") if nm else (self.A, "A")
        n = len(run)
        f = min(hp, 0.999999) * n
        k = min(int(f), n - 1)
        frac = f - k
        xf = float(a.get("crossfade", self.STEP_XF))
        cur = (self.shots[run[k]], f"shot:{run[k]}")
        if k + 1 >= n or xf <= 0 or frac <= 1 - xf:
            return cur
        nxt = (self.shots[run[k + 1]], f"shot:{run[k + 1]}")
        return (*cur, *nxt, (frac - (1 - xf)) / xf)

    def screen(self, i: int, hp: float, s: float):
        """beat i's scaled screen at hold-progress `hp`"""
        if self.ann[i].get("swipe"):
            return self.swiped(i, hp, s)
        sh = self.shot(i, hp)
        base = self.scaled(sh[0], s, sh[1])
        if len(sh) == 5:
            base = Image.blend(base, self.scaled(sh[2], s, sh[3]), sh[4])
        return base

    def swiped(self, i: int, hp: float, s: float):
        """beat i's screen with a row-wise wipe from `shot` to `swipe.to`.

        A dissolve between two screens that differ only in the *text of some
        rows* reads as a blur, not as a change: both strings are legible at
        once through the middle of it and the eye cannot tell which is
        arriving. A wipe with a hard edge can only ever show one of them per
        pixel, so the new name is unambiguous the moment it appears.

        Staggering the rows is what makes it read as a sequence of separate
        edits rather than one repaint. Each named row runs its own wipe,
        `row` seconds long, starting `stagger` seconds after the row above --
        so the times are in seconds and follow the beat's `hold` rather than
        being a share of it, which is what lets a cascade keep its cadence
        when the hold is retimed.

        The two screens must be the same capture geometry, which for two
        `shot`s of one take they always are. Rows are wiped in the order
        given, not in the order they sit on screen; a caller stepping down a
        list just lists them in that order.
        """
        a = self.ann[i]
        sw = a["swipe"]
        src = a.get("shot")
        A = self.scaled(self.shots[src], s, f"shot:{src}") if src else \
            self.scaled(self.A, s, "A")
        B = self.scaled(self.shots[sw["to"]], s, f"shot:{sw['to']}")
        rows = [int(r) for r in sw.get("rows", [])]
        if not rows:
            return A
        t = hp * self.hold(i)
        t0 = float(sw.get("at", 0.0))
        dur = float(sw.get("row", 0.2))
        stag = float(sw.get("stagger", 0.1))
        edge = int(sw.get("edge", 3))
        ec = sw.get("edge_color") or self.th["after"]

        W, H = A.size
        rh = self.RH * s
        out = A.copy()
        mask = Image.new("L", A.size, 0)
        md = ImageDraw.Draw(mask)
        moving = []
        for k, r in enumerate(rows):
            p = (t - (t0 + k * stag)) / dur if dur > 0 else 1.0
            p = 0.0 if p < 0.0 else (1.0 if p > 1.0 else p)
            if p <= 0.0:
                continue
            y0 = int(round(r * rh))
            y1 = int(round((r + 1) * rh))
            if y1 <= 0 or y0 >= H:
                continue
            x = int(round(W * p))
            md.rectangle([0, max(0, y0), x, min(H, y1) - 1], fill=255)
            if 0.0 < p < 1.0 and edge > 0:
                moving.append((x, y0, y1))
        out.paste(B, (0, 0), mask)
        if moving:
            d = ImageDraw.Draw(out)
            for x, y0, y1 in moving:
                d.rectangle([max(0, x - edge), max(0, y0),
                             x, min(H, y1) - 1], fill=tuple(ec))
        return out

    # The pointer, in units of its own height: the classic arrow. Drawn
    # rather than screenshotted because X keeps the cursor out of a window
    # grab, so there is nothing to screenshot.
    ARROW = ((0.00, 0.00), (0.00, 0.72), (0.19, 0.55), (0.31, 0.84),
             (0.44, 0.79), (0.32, 0.51), (0.53, 0.50))

    def arrow(self, h: int):
        """the cursor, `h` pixels tall, antialiased"""
        key = ("arrow", h)
        if key not in self._cache:
            ss = 4                      # supersample; the edges are diagonal
            w = int(h * 0.62) + 4
            im = Image.new("RGBA", ((w + 2) * ss, (h + 2) * ss), (0, 0, 0, 0))
            dr = ImageDraw.Draw(im)
            pts = [((x * h + 1) * ss, (y * h + 1) * ss) for x, y in self.ARROW]
            # White with a thin dark edge, so it reads on a dark terminal and
            # on a light one. The edge has to stay thin: the arrow's tail is
            # only a few pixels across, and a heavy stroke closes it up into a
            # black blob that no longer looks like a pointer.
            dr.polygon(pts, fill=(255, 255, 255, 255),
                       outline=(16, 16, 20, 255),
                       width=max(1, int(h * 0.055)) * ss)
            if len(self._cache) > 24:
                self._cache.clear()
            self._cache[key] = im.resize((w + 2, h + 2), Image.LANCZOS)
        return self._cache[key]

    def cursor(self, nov, name: str, k: int, px: float, py: float, s: float,
               alpha: int) -> None:
        """draw the pointer over frame `k` of run `name`, if it had one"""
        if self.cursor_cfg is None or alpha <= 4:
            return
        path = self.paths.get(name) or {}
        ts, ptr = path.get("t"), path.get("pointer")
        if not ts or ptr is None or k >= len(ts):
            return
        at = ptr.at(ts[k])
        if at is None:
            return
        x, y, bump = at
        # The cursor scales with the zoom: it is part of the picture, not an
        # overlay on it, and one that stayed the same size while the screen
        # grew would read as a sticker on the lens.
        h = max(8, int(self.RH * float(self.cursor_cfg.get("size", 1.25)) * s
                       * (1.0 + 0.35 * bump)))
        a = self.arrow(h)
        if alpha < 255:
            a = a.copy()
            a.putalpha(a.getchannel("A").point(lambda v: int(v * alpha / 255)))
        nov.alpha_composite(a, (int(round(px + x * s)), int(round(py + y * s))))

    def run_at(self, i: int, hp: float):
        """-> (run name, frame index) if beat i is playing a recording"""
        a = self.ann[i]
        run = a.get("shots")
        if not isinstance(run, str):
            return None
        n = len(self.runs[run])
        return run, min(int(min(hp, 0.999999) * n), n - 1)

    def band(self, i: int, px: float, py: float, s: float):
        x0, y0, x1, y1 = self.rect(i)
        g = self.k(6)
        return (px + x0 * s - g, py + y0 * s - g,
                px + x1 * s + g, py + y1 * s + g)

    # A callout's parts, in pixels: the clearance between the rect and the
    # text, how far along the rect the text is dealt so the leader has a
    # corner to turn, and the side border's thickness.
    NOTE_GAP, NOTE_SIDE, NOTE_RULE = 70, 190, 6
    NOTE_PAD = 18

    def emoji(self, ch: str, h: int):
        """`ch` as an image `h` pixels tall, or None if it is not one"""
        if not EMOJI or not ch:
            return None
        key = ("emoji", ch, h)
        if key not in self._cache:
            f = ImageFont.truetype(EMOJI, EMOJI_PX)
            im = Image.new("RGBA", (EMOJI_PX * 2, int(EMOJI_PX * 1.6)),
                           (0, 0, 0, 0))
            ImageDraw.Draw(im).text((4, 4), ch, font=f, embedded_color=True)
            box = im.getbbox()
            if not box:
                return None
            im = im.crop(box)
            w = max(1, round(im.width * h / max(1, im.height)))
            if len(self._cache) > 24:
                self._cache.clear()
            self._cache[key] = im.resize((w, h), Image.LANCZOS)
        return self._cache[key]

    @staticmethod
    def split_note(note: str):
        """-> (emoji, words). A note may open with one; most do."""
        head, _, rest = note.partition(" ")
        if head and rest and not head.isascii():
            return head, rest
        return None, note


    # A tag is the other way to put words on a picture. A note points: it is
    # anchored to a rect and carries a leader back to it, which is right when
    # the words are about one row and wrong when they name the whole beat --
    # there the leader has nothing to single out, and crossing the picture to
    # reach an arbitrary rect is just a line drawn over the content. A tag
    # names the beat instead: it sits at a fixed place in the frame, wraps to
    # its own column, and draws no leader at all.
    TAG_PAD = 52          # the frame margin a tag keeps, before its own fill
    TAG_GAP = 1.18          # line spacing, in multiples of the line height

    def tone_of(self, v, default):
        """a theme key, an explicit [r, g, b], or the default"""
        if isinstance(v, str):
            return tuple(self.th.get(v, default))
        if isinstance(v, (list, tuple)) and len(v) >= 3:
            return tuple(int(c) for c in v[:3])
        return tuple(default)

    def wrap_tag(self, text: str, font, max_w: int) -> list[str]:
        """greedy wrap; a single word longer than the column keeps its line"""
        lines, cur = [], ""
        for word in str(text).split():
            trial = f"{cur} {word}".strip()
            if cur and font.getlength(trial) > max_w:
                lines.append(cur); cur = word
            else:
                cur = trial
        if cur:
            lines.append(cur)
        return lines

    def tag(self, ov, i: int, alpha: int, size) -> None:
        """beat i's tag, set against an edge of the frame"""
        a = self.ann[i]
        t = a.get("tag")
        if not t or alpha <= 4:
            return
        t = {"text": t} if isinstance(t, str) else dict(t)
        text = t.get("text", "")
        if not text:
            return
        at = str(t.get("at", "center-right"))
        font = (self.f_note if not t.get("size") else
                ImageFont.truetype(BOLD, self.k(int(t["size"]))))
        cw, ch = size
        pad = self.k(self.TAG_PAD)
        # The column is a share of the frame, so the same tag wraps the same
        # way whatever the canvas is -- a draft is a faithful miniature.
        col = int(cw * float(t.get("width", 0.42)))
        lines = self.wrap_tag(text, font, col)
        asc, desc = font.getmetrics()
        lh = asc + desc
        step = int(lh * self.TAG_GAP)
        block_h = step * (len(lines) - 1) + lh
        right = not at.endswith("left")
        if "top" in at:
            y = pad
        elif "bottom" in at:
            y = ch - pad - block_h
        else:
            y = int((ch - block_h) / 2)
        ld = ImageDraw.Draw(ov)
        fill = fade_c(self.tone_of(t.get("color"), self.th.get("fg")), alpha)
        widest = max((font.getlength(l) for l in lines), default=0)
        x0 = (cw - pad - widest) if right else pad

        # `bg` gives the words something to sit on. Over a screen that is
        # itself text, a fill is what separates one from the other -- a
        # stroke alone holds the letterforms apart but leaves the rows
        # showing between them, which reads as two things in one place.
        bg = t.get("bg")
        if bg:
            bp = self.k(int(t.get("bg_pad", 26)))
            box = [x0 - bp, y - bp, x0 + widest + bp, y + block_h + bp]
            ld.rounded_rectangle(
                box, radius=self.k(10),
                fill=fade_c(self.tone_of(bg if bg is not True else None,
                                         self.th["bg"]),
                            int(alpha * float(t.get("bg_alpha", 0.86)))))
        # Stroked as well when there is no fill: enough to hold the
        # letterforms apart from whatever is behind them, without drawing a
        # box that reads as a second window.
        sw = 0 if bg else self.k(4)
        stroke = fade_c(self.th["bg"], alpha)
        for n, line in enumerate(lines):
            w = font.getlength(line)
            x = (cw - pad - w) if right else pad
            ld.text((x, y + n * step), line, font=font, fill=fill,
                    stroke_width=sw, stroke_fill=stroke)

    def callout(self, nov, i: int, b, alpha: int, tone, size):
        ld = ImageDraw.Draw(nov)
        """beat i's note, drawn beside its rect.

        A caption bar has room for a sentence, and a sentence is the wrong
        length for pointing at one expression. So the note goes in the frame:
        a few words, offset off the rect's corner so they cover nothing, with
        a rule down their outer side and a square-cornered leader from that
        rule back to the rect. The corners are what make it read as a pointer
        rather than as more text -- a straight line to the same place looks
        like an underline, and a curve looks like decoration.
        """
        a = self.ann[i]
        note = a.get("note")
        if not note or alpha <= 4:
            return
        at = str(a.get("note_at", "below-right"))
        below, right = not at.startswith("above"), not at.endswith("left")
        x0, y0, x1, y1 = b
        cw, ch = size

        # where the leader lands on the rect: in from the corner nearest the
        # note, so the last segment runs alongside the rows it points at
        inset, corner = self.k(10), self.k(48)
        px = min(x1 - inset, max(x0 + inset,
                                 x1 - corner if right else x0 + corner))

        pd = self.k(self.NOTE_PAD)
        emo_ch, note = self.split_note(note)
        asc, desc = self.f_note.getmetrics()
        th_ = asc + desc
        emo = self.emoji(emo_ch, int(asc * 0.92))
        tw = ld.textlength(note, font=self.f_note)
        emo_w = (emo.width + int(pd * 0.8)) if emo else 0
        tw += emo_w
        side, lead = self.k(self.NOTE_SIDE), self.k(20)
        rx = px + (side if right else -side)
        tx = rx + lead if right else rx - lead - tw
        # keep the words on the canvas; the leader stretches instead
        edge = self.k(40)
        tx = min(cw - edge - pd - tw, max(float(edge) + pd, tx))
        rx = tx - lead if right else tx + tw + lead
        # A static camera frames a whole function, so a beat near its foot has
        # no room under it. Flip rather than run off the frame: the note is
        # for reading, and half a note below the edge is none.
        gap = self.k(self.NOTE_GAP)
        def place(down):
            t0 = (y1 + gap) if down else (y0 - gap - th_)
            return t0, t0 + th_
        ty0, ty1 = place(below)
        if below and ty1 + pd > ch - lead:
            below = False
            ty0, ty1 = place(False)
        elif not below and ty0 - pd < lead:
            below = True
            ty0, ty1 = place(True)
        py = y1 if below else y0

        col = (*tone, alpha)
        # A plate under the words. They sit over dimmed code, and dimmed code
        # is still code: without a ground of its own the note reads as one
        # more line of the program rather than as something said about it.
        ld.rounded_rectangle(
            [min(tx, rx) - pd, ty0 - pd, max(tx + tw, rx) + pd, ty1 + pd],
            radius=self.k(8), fill=(*self.th["caption_bg"], min(alpha, 242)))
        rule = self.k(self.NOTE_RULE)
        ld.rectangle([rx - rule / 2, ty0 - pd,
                      rx + rule / 2, ty1 + pd], fill=col)
        # the leader: out of the rule's near end, along, and back to the rect
        near = ty0 - pd if below else ty1 + pd
        mid = (near + py) / 2
        ld.line([(rx, near), (rx, mid), (px, mid), (px, py)], fill=col,
                width=self.k(4))
        dot = self.k(6)
        ld.rectangle([px - dot, py - dot, px + dot, py + dot], fill=col)
        if emo:
            e = emo.copy()
            e.putalpha(e.getchannel("A").point(lambda v: int(v * alpha / 255)))
            nov.alpha_composite(e, (int(tx), int(ty0 + (asc - e.height) / 2)))
        ld.text((tx + emo_w, ty0), note, font=self.f_note,
                fill=(*self.th["fg"], alpha))

    # How far in from each edge the picture gives up, as a fraction of that
    # side. Taller on the top and bottom because that is where a terminal
    # capture is cut: the sides end at the text, but the rows above and below
    # are the same rows going on, and a hard edge there reads as a boundary
    # the code does not have.
    VIG_X, VIG_Y, VIG_BLUR = 0.085, 0.165, 9

    def vignette(self, size):
        """a mask: opaque in the middle, gone at the edges"""
        key = ("vig", size)
        if key not in self._cache:
            w, h = size

            def ramp(n, frac):
                g = Image.new("L", (n, 1))
                px, band = g.load(), max(1.0, n * frac)
                for i in range(n):
                    t = min(min(i, n - 1 - i) / band, 1.0)
                    px[i, 0] = int(255 * (t * t * (3 - 2 * t)))   # smoothstep
                return g

            gx = ramp(w, self.VIG_X).resize((w, h))
            gy = ramp(h, self.VIG_Y).rotate(90, expand=True).resize((w, h))
            if len(self._cache) > 16:
                self._cache.clear()
            self._cache[key] = ImageChops.multiply(gx, gy)
        return self._cache[key]

    def vignetted(self, lay, p: float):
        """`lay` with its edges blurred and taken back to the ground.

        Both, not either: fading alone leaves the last legible row hanging in
        mid-air, and blurring alone leaves a smear with a hard edge under it.
        Strength follows the zoom, so the intro's framed panel keeps the crisp
        border it is drawn with.
        """
        if p <= 0.02:
            return lay
        v = self.vignette(lay.size)
        if p < 0.999:
            v = v.point(lambda t: int(255 - (255 - t) * p))
        lay = Image.composite(lay, lay.filter(
            ImageFilter.GaussianBlur(self.VIG_BLUR)), v)
        return Image.composite(
            lay, Image.new("RGB", lay.size, self.th["bg"]), v)

    def vmask(self, size):
        """the falloff, cached per panel size.

        Stronger top and bottom than left and right: a clip of code pans
        vertically, so those are the edges the eye keeps arriving at, and the
        ones a hard cut keeps stopping it at.
        """
        if size in self._vmask:
            return self._vmask[size]
        v = self.vignette or {}
        w, h = size
        mx = max(1, int(w * float(v.get("side", 0.06))))
        my = max(1, int(h * float(v.get("edge", 0.15))))
        # one row and one column of falloff, multiplied into a full mask by
        # resizing each to the panel and compositing -- cheaper than, and
        # identical to, evaluating the product per pixel
        col = Image.new("L", (w, 1), 255)
        px = col.load()
        for x in range(mx):
            k = int(255 * ease(x / mx))
            px[x, 0] = k
            px[w - 1 - x, 0] = k
        row = Image.new("L", (1, h), 255)
        py = row.load()
        for y in range(my):
            k = int(255 * ease(y / my))
            py[0, y] = k
            py[0, h - 1 - y] = k
        m = ImageChops.multiply(col.resize(size), row.resize(size))
        self._vmask.clear()
        self._vmask[size] = m
        return m

    def vignetted(self, lay, strength: float):
        """`lay` with its edges blurred and faded back toward the ground"""
        if not self.vignette or strength <= 0.02:
            return lay
        v = self.vignette
        edge = lay.filter(ImageFilter.GaussianBlur(float(v.get("blur", 7))))
        edge = Image.blend(edge, Image.new("RGB", lay.size, self.th["bg"]),
                           float(v.get("fade", 0.6)))
        m = self.vmask(lay.size)
        if strength < 1.0:
            m = m.point(lambda k: int(255 - (255 - k) * strength))
        return Image.composite(lay, edge, m)

    def size_at(self, s: float) -> tuple[int, int]:
        """the capture's pixel size at scale `s`.

        The scale is quantised *before* the size is taken off it, not after,
        because the two have to come from the same number. Keying the cache on
        `round(s, 4)` while sizing from the raw `s` lets two scales a
        ten-thousandth apart share a key and hold images a pixel apart in
        size, and the failure surfaces nowhere near here -- it surfaces later
        as `ValueError: images do not match` when one of them is blended with
        the other, on a clip that renders fine at 29 rows and dies at 30.

        Half away from zero rather than Python's half-to-even, so a height
        landing exactly on .5 does not round one way or the other depending on
        the parity of the integer beside it.
        """
        q = round(float(s), 4)
        return (int(math.floor(self.IW * q + 0.5)),
                int(math.floor(self.IH * q + 0.5)))

    def scaled(self, img, s: float, tag: str):
        w, h = self.size_at(s)
        # keyed on the size it produced, so a hit can only ever be that size
        key = (tag, w, h)
        if key not in self._cache:
            if len(self._cache) > 16:
                self._cache.clear()
            self._cache[key] = img.resize((w, h), Image.LANCZOS)
        return self._cache[key]

    # -- main ---------------------------------------------------------------
    def run(self) -> int:
        os.makedirs(self.outdir, exist_ok=True)
        for f in os.listdir(self.outdir):
            os.remove(os.path.join(self.outdir, f))

        nf = int(round(self.total() * self.fps))
        for n in range(nf):
            post_fx(self, self.frame(n), n, nf).save(f"{self.outdir}/f{n:05d}.png")
        return nf

    def phase(self, t: float):
        """-> (zoom p, annotation index, pan progress, outro progress, hold progress)

        The last value is how far through beat i's own dwell we are, 0..1. A
        beat that plays several shots steps through them on it.
        """
        T = self.t
        if t < T["intro"]:
            return 0.0, 0, 0.0, 0.0, 0.0
        if T["zoom"] > 0 and t < T["intro"] + T["zoom"]:
            return ease((t - T["intro"]) / T["zoom"]), 0, 0.0, 0.0, 0.0
        u = t - T["intro"] - T["zoom"]
        last = len(self.ann) - 1
        for i in range(len(self.ann)):
            hold = self.hold(i)
            travel = self.gap(i) if i < last else 0.0
            seg = hold + travel
            if u < seg:
                pan = (0.0 if u < hold or travel <= 0
                       else ease((u - hold) / travel))
                hp = min(1.0, u / hold) if hold > 0 else 1.0
                return 1.0, i, pan, 0.0, hp
            u -= seg
        return (1.0, last, 0.0,
                ease(u / T["outro"]) if T["outro"] > 0 else 1.0, 1.0)

    # -- title card ---------------------------------------------------------
    def _glyphs(self, text, font):
        """the line as a bare alpha mask, and its size"""
        x0, y0, x1, y1 = font.getbbox(text)
        w, h = max(1, x1 - x0), max(1, y1 - y0)
        m = Image.new("L", (w + 8, h + 8), 0)
        ImageDraw.Draw(m).text((4 - x0, 4 - y0), text, font=font, fill=255)
        return m

    def _sick(self, size, t: float) -> Image.Image:
        """bile. A vertical ramp between two greens that will not settle,
        crawling upward -- the colour of code you do not want to touch."""
        w, h = size
        col = Image.new("RGB", (1, h))
        px = col.load()
        for y in range(h):
            k = 0.5 + 0.5 * math.sin(y * 0.30 - t * 7.0)
            k2 = 0.5 + 0.5 * math.sin(y * 0.11 + t * 3.1)
            px[0, y] = (int(lerp(66, 132, k) * lerp(0.8, 1.0, k2)),
                        int(lerp(96, 176, k)),
                        int(lerp(20, 40, k)))
        return col.resize((w, h), Image.BILINEAR)

    def _shine(self, size, t: float, sweep: float) -> Image.Image:
        """clean metal, with one highlight crossing it.

        The band is built once as a horizontal profile and sheared, rather
        than evaluated per pixel: it is the same band at every row, only
        further along.
        """
        w, h = size
        prof = Image.new("L", (w, 1), 0)
        px = prof.load()
        centre, sigma = sweep * (w * 1.6) - w * 0.3, w * 0.085
        for x in range(w):
            d = (x - centre) / sigma
            px[x, 0] = int(255 * math.exp(-d * d)) if abs(d) < 4 else 0
        band = prof.resize((w, h), Image.BILINEAR).transform(
            (w, h), Image.AFFINE, (1, 0.45, 0, 0, 1, 0), Image.BILINEAR)
        base = Image.new("RGB", (w, h), (150, 186, 176))
        base.paste(Image.new("RGB", (w, h), (255, 255, 255)), (0, 0), band)
        return base

    def title_frame(self, u: float) -> Image.Image:
        """the card at progress `u`, 0..1"""
        th, W, H = self.th, self.W, self.H
        cv = Image.new("RGB", (W, H), th["bg"])
        lines = self.card["lines"]
        t = u * self.t["title"]
        fade = 1.0 - ease(max(0.0, (u - 0.90) / 0.10))

        big = ImageFont.truetype(BOLD, int(self.W * 0.082))
        small = ImageFont.truetype(BOLD, int(self.W * 0.036))
        rendered, total_h = [], 0
        for i, ln in enumerate(lines):
            f = small if ln.get("small") or not ln.get("effect") else big
            m = self._glyphs(ln["text"], f)
            rendered.append((ln, m))
            total_h += m.height + (26 if i else 0)

        y = (H - total_h) / 2
        for i, (ln, m) in enumerate(rendered):
            if i:
                y += 26
            at = float(ln.get("at", 0.0))
            a = ease(max(0.0, min(1.0, (u - at) / 0.07))) * fade
            if a > 0.004:
                x = (W - m.width) / 2
                eff = ln.get("effect")
                dx = dy = 0.0
                if eff == "sick":
                    # it will not hold still either
                    dx, dy = math.sin(t * 21) * 2.6, math.cos(t * 15) * 2.0
                    col = self._sick(m.size, t)
                    glow, gcol, ga = 15, (86, 150, 26), 0.55
                elif eff == "shine":
                    sweep = max(0.0, min(1.0, (u - 0.18) / 0.50))
                    col = self._shine(m.size, t, sweep)
                    glow, gcol, ga = 19, (150, 235, 190), 0.42
                else:
                    col = Image.new("RGB", m.size, th["muted"])
                    glow = 0
                if glow:
                    # on a canvas of its own, three radii bigger: a blur that
                    # reaches the edge of its image has that edge for an
                    # outline, and the outline is a rectangle
                    q = glow * 3
                    gm = Image.new("L", (m.width + 2 * q, m.height + 2 * q), 0)
                    gm.paste(m, (q, q))
                    g = gm.filter(ImageFilter.GaussianBlur(glow))
                    g = g.point(lambda v: int(v * a * ga))
                    cv.paste(Image.new("RGB", gm.size, gcol),
                             (int(x + dx - q), int(y + dy - q)), g)
                cv.paste(col, (int(x + dx), int(y + dy)),
                         m.point(lambda v: int(v * a)))
            y += m.height

        d = ImageDraw.Draw(cv)
        foot = self.card.get("footer")
        if foot:
            # The diffstat, in the colours a diff uses, because that is the
            # one line of the card that is a measurement rather than a claim
            # -- and the claim under it is that all of this is one example
            # out of that.
            fa = ease(max(0.0, min(1.0, (u - float(foot.get("at", 0.62)))
                                   / 0.16))) * fade
            if fa > 0.004:
                big = ImageFont.truetype(BOLD, int(self.W * 0.044))
                plus = f"+{int(foot['added']):,}"
                minus = f"-{int(foot['removed']):,}"
                gap = int(self.W * 0.030)
                wp, wm = d.textlength(plus, font=big), d.textlength(minus, font=big)
                sy = H * 0.755
                x = (W - (wp + gap + wm)) / 2
                d.text((x, sy), plus, font=big,
                       fill=fade_c(th["after"], int(255 * fa)), anchor="lt")
                d.text((x + wp + gap, sy), minus, font=big,
                       fill=fade_c(th["before"], int(255 * fa)), anchor="lt")
                if foot.get("note"):
                    # the same size as the count above it: it names what the
                    # count is a count of, and a caption half the size reads
                    # as a footnote to the number rather than its subject
                    d.text((W / 2, sy + self.W * 0.058), foot["note"],
                           font=big,
                           fill=fade_c(th["muted"], int(225 * fa)), anchor="mt")
        d.text((W / 2, H - 64), self.title, font=self.f_meta,
               fill=fade_c(th["muted"], int(150 * fade)), anchor="mm")
        return cv

    def caption_at(self, p: float, ai: int, pan: float, rel: float):
        """(head, sub, alpha, beat, is_beat) for this moment.

        The intro caption cross-fades into beat one, each beat into the next
        across its pan, and the last into the outro. `is_beat` is False while
        an intro or outro caption is the one showing, so a chrome knows not to
        decorate it with a beat's bullet.
        """
        nxt = min(ai + 1, len(self.ann) - 1)
        head, sub, ca, idx, beat = self.cap_intro[0], self.cap_intro[1], 255, 0, False
        if 0.0 < p < 1.0:
            if p < 0.5:
                ca = int(255 * (1 - p / 0.5))
            else:
                ca = int(255 * ((p - 0.5) / 0.5))
                (head, sub), beat = cap_of(self.ann[0]), True
        elif p >= 1.0:
            (head, sub), idx, beat = cap_of(self.ann[ai]), ai, True
            if pan > 0:
                if pan < 0.5:
                    ca = int(255 * (1 - pan / 0.5))
                else:
                    ca = int(255 * ((pan - 0.5) / 0.5))
                    (head, sub), idx = cap_of(self.ann[nxt]), nxt
            if rel > 0:
                if rel < 0.45:
                    ca = int(255 * (1 - rel / 0.45))
                else:
                    ca = int(255 * ((rel - 0.45) / 0.55))
                    head, sub, beat = self.cap_outro[0], self.cap_outro[1], False
        return head, sub, ca, idx, beat

    def frame(self, n: int) -> Image.Image:
        th, W, H = self.th, self.W, self.H
        t = n / self.fps
        if self.t["title"] > 0:
            if t < self.t["title"]:
                return self.title_frame(t / self.t["title"])
            t -= self.t["title"]
        p, ai, pan, rel, hp = self.phase(t)
        nxt = min(ai + 1, len(self.ann) - 1)

        head, sub, ca, cidx, is_beat = self.caption_at(p, ai, pan, rel)
        if self.chrome:
            a = self.ann[cidx] if is_beat else {}
            # A `typewriter` beat sets its sub one character at a time, with a
            # caret. The value is the share of the dwell the typing takes, so
            # the sentence lands before the beat does and the rest of the hold
            # is spent reading it rather than waiting for it.
            reveal = caret = None
            tw = a.get("typewriter")
            if tw and p >= 1.0 and rel <= 0:
                until = float(tw) if not isinstance(tw, bool) else 0.8
                reveal = min(1.0, hp / until) if until > 0 else 1.0
                # Solid while typing; blinking once it has stopped, which is
                # what an idle caret does and what says the typing is finished.
                caret = reveal < 1.0 or int(t * 1.6) % 2 == 0
            cv = self.chrome.frame(head, sub, ca, page=cidx + 1,
                                   total=len(self.ann),
                                   bullet=a.get("bullet"),
                                   misspell=a.get("misspell", ()),
                                   reveal=reveal, caret=caret)
        else:
            cv = Image.new("RGB", (W, H), th["bg"])
        d = ImageDraw.Draw(cv)

        s_i, cx_i, cy_i = self.cam(ai)
        s_j, cx_j, cy_j = self.cam(nxt)
        s = lerp(self.s_a, lerp(s_i, s_j, pan), p)
        cw = lerp(self.panel_w, self.vp[2], p)
        ch = lerp(self.panel_h, self.vp[3], p)
        cx = lerp(self.ax + self.panel_w / 2, self.vp[0] + self.vp[2] / 2, p)
        cy = lerp(self.panel_y + self.panel_h / 2, self.vp[1] + self.vp[3] / 2, p)
        src_cx = lerp(self.IW / 2, lerp(cx_i, cx_j, pan), p)
        src_cy = lerp(self.IH / 2, lerp(cy_i, cy_j, pan), p)

        # BEFORE panel, sliding out to the left (comparison clips only)
        if self.B is not None and p < 1.0:
            bx = int(round(lerp(self.bx, -self.panel_w - self.k(100), p)))
            pw, ph = int(round(self.panel_w)), int(round(self.panel_h))
            lay = Image.new("RGB", (pw, ph), th["bg"])
            lay.paste(self.scaled(self.B, self.s_a, "B"),
                      (int(round(pw / 2 - self.IW / 2 * self.s_a)),
                       int(round(ph / 2 - self.IH / 2 * self.s_a))))
            cv.paste(lay, (bx, int(self.panel_y)))
            d.rectangle([bx, self.panel_y, bx + pw - 1, self.panel_y + ph - 1],
                        outline=th["panel_border"], width=self.k(2))

        # subject panel, growing from the intro shot to the full viewport
        cwi, chi = int(round(cw)), int(round(ch))
        lay = Image.new("RGB", (cwi, chi),
                        self.chrome.fill if self.chrome else th["bg"])
        px = int(round(cwi / 2 - src_cx * s))
        py = int(round(chi / 2 - src_cy * s))
        # A push replaces the screen instead of dissolving into it: the old
        # one leaves to the left and the new arrives behind it. Each keeps its
        # own camera through the move -- a screen caught mid-scale while it is
        # also travelling reads as a stumble, and the point of the shape is
        # that one thing *replaced* another, not that one became it.
        push = pan > 0 and self.ann[nxt].get("transition") == "push"
        # A wipe replaces the screen with a hard edge travelling across it,
        # the same language the row-level `swipe` uses: one screen per pixel,
        # never a blend of both. A dissolve between two screens of the same
        # list reads as a smear, and a push slides the whole picture sideways
        # -- which says "another screen" when what happened is "this screen,
        # changed".
        wipe = pan > 0 and self.ann[nxt].get("transition") == "wipe"
        if push:
            for k, hpk, off in ((ai, 1.0, -pan * cwi), (nxt, 0.0, (1 - pan) * cwi)):
                sk, sxk, syk = self.cam(k)
                lay.paste(self.screen(k, hpk, sk),
                          (int(round(cwi / 2 - sxk * sk + off)),
                           int(round(chi / 2 - syk * sk))))
        elif wipe:
            for k, hpk in ((ai, 1.0), (nxt, 0.0)):
                sk, sxk, syk = self.cam(k)
                at = (int(round(cwi / 2 - sxk * sk)),
                      int(round(chi / 2 - syk * sk)))
                img = self.screen(k, hpk, sk)
                if k == ai:
                    lay.paste(img, at)
                    continue
                edge_x = int(round(cwi * pan))
                inc = Image.new("RGB", (cwi, chi),
                                self.chrome.fill if self.chrome else th["bg"])
                inc.paste(img, at)
                mask = Image.new("L", (cwi, chi), 0)
                ImageDraw.Draw(mask).rectangle([0, 0, edge_x, chi - 1], fill=255)
                lay.paste(inc, (0, 0), mask)
                ew = self.k(4)
                if 0 < edge_x < cwi:
                    ImageDraw.Draw(lay).rectangle(
                        [max(0, edge_x - ew), 0, edge_x, chi - 1],
                        fill=tuple(self.th[self.ann[nxt].get("tone", "after")]))
        else:
            base = self.screen(ai, 1.0 if pan > 0 else hp, s)
            if pan > 0:
                nb = self.screen(nxt, 0.0, s)
                if nb is not base:
                    base = Image.blend(base, nb, pan)
            lay.paste(base, (px, py))

        # The pointer goes on the screen, under the annotation layer: it is
        # part of what was filmed, so the dim that falls on the screen falls
        # on it too.
        if self.cursor_cfg is not None and not push and not wipe:
            cov = Image.new("RGBA", lay.size, (0, 0, 0, 0))
            drew = False
            for k, alpha in ((ai, int(255 * (1.0 - pan))), (nxt, int(255 * pan))):
                at = self.run_at(k, 1.0 if (pan > 0 and k == ai) else
                                 (0.0 if pan > 0 else hp))
                if at:
                    self.cursor(cov, at[0], at[1], px, py, s, alpha)
                    drew = True
            if drew:
                lay = lay.convert("RGBA")
                lay.alpha_composite(cov)
                lay = lay.convert("RGB")

        # A beat with `band: false` is the screen alone -- nothing dimmed and
        # no frame drawn -- for a run of shots whose own movement is the point.
        # A rectangle with an accent tick drawn over a document page reads as a
        # video overlay rather than as part of the page, so a chrome turns the
        # band off unless a beat asks for it back.
        band_default = not self.chrome
        lit = lerp(0.0 if not self.ann[ai].get("band", band_default) else 1.0,
                   0.0 if not self.ann[nxt].get("band", band_default) else 1.0, pan)
        # How far into the clip the annotation layer is up at all: past the
        # zoom, not yet into the outro, and never over a screen on its way out
        # of a push. The band's dimming is this times `lit`; the note is this
        # on its own. They used to be the same number, which meant a beat that
        # turned the band off to let pure motion speak lost its note with it,
        # and the words had to go on the beats either side -- describing the
        # movement before and after the frames that showed it.
        vis = 0.0 if (push or wipe) else (min(1.0, max(0.0, (p - 0.55) / 0.45))
                                * (1.0 - rel))
        tone_i = self.th[self.ann[ai].get("tone", "after")]
        tone_j = self.th[self.ann[nxt].get("tone", "after")]
        b = self.band(ai, px, py, s)
        b_next = self.band(nxt, px, py, s) if pan > 0 else b
        b_mix = (tuple(lerp(b[k], b_next[k], pan) for k in range(4))
                 if pan > 0 else b)

        dim = int(150 * vis * lit)
        if dim > 3:
            ov = Image.new("RGBA", lay.size, (0, 0, 0, dim))
            ImageDraw.Draw(ov).rounded_rectangle(list(b_mix), radius=self.k(10),
                                                 fill=(0, 0, 0, 0))
            lay = lay.convert("RGBA")
            lay.alpha_composite(ov)
            lay = lay.convert("RGB")
            ld = ImageDraw.Draw(lay)
            g = fade_c(tuple(int(lerp(tone_i[k], tone_j[k], pan)) for k in range(3)),
                     int(255 * vis * lit))
            ld.rounded_rectangle(list(b_mix), radius=self.k(10), outline=g,
                                 width=self.k(3))
            ld.rectangle([b_mix[0] - self.k(12), b_mix[1],
                          b_mix[0] - self.k(7), b_mix[3]], fill=g)

        # Notes go on their own RGBA pass: a plate that fades has to be
        # composited, not drawn, and the outgoing note has to cross the
        # incoming one rather than overwrite it. The pass runs whether or not
        # the band was drawn -- the note is anchored to the beat's rect, which
        # exists either way.
        tvis = min(1.0, max(0.0, (p - 0.55) / 0.45)) * (1.0 - rel)
        if vis > 0.015:
            nov = Image.new("RGBA", lay.size, (0, 0, 0, 0))
            self.callout(nov, ai, b, int(255 * vis * (1.0 - pan)), tone_i,
                         lay.size)
            if pan > 0:
                self.callout(nov, nxt, b_next, int(255 * vis * pan), tone_j,
                             lay.size)
            lay = lay.convert("RGBA")
            lay.alpha_composite(nov)
            lay = lay.convert("RGB")

        lay = self.vignetted(lay, p)
        lay = self.vignetted(lay, max(0.0, (p - 0.55) / 0.45))
        pos = (int(round(cx - cw / 2)), int(round(cy - ch / 2)))
        cv.paste(lay, pos)

        # Tags are placed against the *frame*, not against the camera's panel.
        # The panel is whatever size the current scale makes it and is pasted
        # at an offset, so a tag set flush to the panel's right edge lands
        # off-screen the moment the panel is wider than the frame -- which is
        # exactly what a zoomed-in camera produces.
        #
        # A tag names the beat, so it survives the travel rather than blinking
        # off with the band. Across a wipe it rides the edge: both tags are
        # drawn at full strength and each clipped to its own side of the moving
        # line, so the words are replaced in place exactly as the screen under
        # them is. Cross-fading them would put one word on top of the other --
        # and sharing a position is the whole point of them.
        if tvis > 0.015 and any(self.ann[k].get("tag") for k in (ai, nxt)):
            fw, fh = cv.size
            tov = Image.new("RGBA", (fw, fh), (0, 0, 0, 0))
            if wipe:
                ex = max(0, min(fw, pos[0] + int(round(cwi * pan))))
                for k, box in ((ai, (ex, 0, fw, fh)), (nxt, (0, 0, ex, fh))):
                    if box[2] <= box[0]:
                        continue
                    one = Image.new("RGBA", (fw, fh), (0, 0, 0, 0))
                    self.tag(one, k, int(255 * tvis), (fw, fh))
                    tov.alpha_composite(one.crop(box), (box[0], box[1]))
            else:
                self.tag(tov, ai, int(255 * tvis * (1.0 - pan)), (fw, fh))
                if pan > 0:
                    self.tag(tov, nxt, int(255 * tvis * pan), (fw, fh))
            cv = cv.convert("RGBA")
            cv.alpha_composite(tov)
            cv = cv.convert("RGB")
            d = ImageDraw.Draw(cv)
        if p < 0.9 and not self.vignette and not self.chrome:
            d.rectangle([pos[0], pos[1], pos[0] + cwi - 1, pos[1] + chi - 1],
                        outline=th["panel_border"], width=self.k(2))
        if self.chrome:
            return cv

        # header: intro labels cross-fade into a single banner
        d.rectangle([0, 0, W, self.panel_y - self.k(44) if p < 0.5
                     else self.vp[1]], fill=th["bg"])
        a_in = int(255 * max(0.0, min(1.0, (p - 0.45) / 0.40)))
        a_out = 255 - a_in
        if a_out > 4:
            d.text((W / 2, self.k(40)), self.title, font=self.f_meta,
                   fill=fade_c(th["muted"], a_out), anchor="mm")
            if self.B is not None:
                d.text((self.bx + self.panel_w / 2, self.k(108)),
                       self.labels["before"],
                       font=self.f_label, fill=fade_c(th["before"], a_out), anchor="mm")
            intro_lead = self.ann[0].get("label", self.lead)
            if intro_lead:
                d.text((self.ax + self.panel_w / 2, self.k(108)), intro_lead,
                       font=self.f_label, fill=fade_c(self.accent_intro, a_out),
                       anchor="mm")
        if a_in > 4:
            lead, tone = self.banner(ai, nxt, pan)
            if lead:
                d.text((self.k(32), self.k(50)), lead, font=self.f_hdr,
                       fill=fade_c(tone, a_in), anchor="lm")
                d.text((W - self.k(32), self.k(52)), self.title,
                       font=self.f_meta,
                       fill=fade_c(th["muted"], a_in), anchor="rm")
            else:
                d.text((self.k(32), self.k(50)), self.title, font=self.f_meta,
                       fill=fade_c(th["muted"], a_in), anchor="lm")

        # caption bar -- or, with no bar, the line it would have held
        ty = self.cap_y if self.bar else H - self.cap_h
        if self.bar:
            d.rectangle([0, self.cap_y, W, H], fill=th["caption_bg"])
        # the rule under the frame is the beat's colour, like everything else
        # the beat owns; a clip whose halves are two colours cannot have one
        accent = (self.accent_intro if p < 0.5 else
                  self.th[self.ann[nxt if pan >= 0.5 else ai].get("tone", "after")])
        if self.bar:
            d.rectangle([0, self.cap_y, W, self.cap_y + self.k(3)],
                        fill=fade_c(accent, 200))

        # No tick without words beside it. A clip that says its piece in the
        # frame leaves this bar empty, and an accent mark alone in an empty bar
        # is a label for nothing.
        if head or sub:
            d.rectangle([self.k(32), ty + self.k(44), self.k(40),
                         ty + self.k(84)], fill=fade_c(accent, ca))
        d.text((self.k(60), ty + self.k(50)), head, font=self.f_cap,
               fill=fade_c(th["fg"], ca), anchor="lm")
        d.text((self.k(60), ty + self.k(92)), sub, font=self.f_sub,
               fill=fade_c(th["muted"], ca), anchor="lm")
        return cv


# ---------------------------------------------------------------------------
# the frame everything is read inside
# ---------------------------------------------------------------------------


class Furniture:
    """The header strip and the caption bar, and the cross-fade between two
    captions.

    A word and a title along the top, a headline and a line of detail along
    the bottom: that is the frame a clip is read inside, and it is the same
    frame whatever the storyboard puts in the middle. Written out once per
    storyboard it is written out once per storyboard *wrong* -- the two drift
    a couple of pixels and a colour apart, and nobody notices until they are
    seen one after the other.

    A host supplies `th`, `W`, `H`, `uk` (canvas height over 1080), `header_h`,
    `cap_y`, `lead`, `title`, and the `f_hdr`/`f_meta`/`f_cap`/`f_sub` fonts.
    """

    def caption(self, seg: dict, u: float, prev: dict | None):
        """-> (head, sub, alpha) for this moment.

        A caption that changes does not cut: the old words go out and the new
        ones come in over the front of the segment, so the swap rides the
        camera move rather than landing on top of it.
        """
        head, sub, ca = seg["head"], seg["sub"], 255
        xf = min(0.38, seg["dur"] / 2) / max(seg["dur"], 1e-6)
        if prev and (prev["head"], prev["sub"]) != (head, sub) and u < xf:
            v = u / xf
            if v < 0.5:
                head, sub, ca = prev["head"], prev["sub"], int(255 * (1 - v / 0.5))
            else:
                ca = int(255 * ((v - 0.5) / 0.5))
        return head, sub, ca

    def header(self, d, accent=None) -> None:
        """the strip along the top, painted over whatever ran under it.

        `accent` is the colour of the moment -- the theme's `after` unless the
        beat has a colour of its own, which a donut section does.
        """
        th, k = self.th, self.uk
        accent = accent or th["after"]
        m = int(32 * k)
        d.rectangle([0, 0, self.W, self.header_h], fill=th["bg"])
        if self.lead:
            d.text((m, self.header_h / 2), self.lead, font=self.f_hdr,
                   fill=accent, anchor="lm")
            d.text((self.W - m, self.header_h / 2 + 2 * k), self.title,
                   font=self.f_meta, fill=th["muted"], anchor="rm")
        else:
            d.text((m, self.header_h / 2), self.title, font=self.f_meta,
                   fill=th["muted"], anchor="lm")

    def bar(self, d, head: str, sub: str, ca: int, accent=None) -> None:
        """the caption bar along the bottom.

        Not every storyboard wants one. A donut has the reader's eye in the
        middle of the frame and an annotation already there, and a strip of
        words along the bottom is a second place to look for what the first
        one is already saying.
        """
        th, k = self.th, self.uk
        accent = accent or th["after"]
        d.rectangle([0, self.cap_y, self.W, self.H], fill=th["caption_bg"])
        d.rectangle([0, self.cap_y, self.W, self.cap_y + max(1, int(3 * k))],
                    fill=fade_c(accent, 200))
        d.rectangle([int(32 * k), self.cap_y + 44 * k, int(40 * k),
                     self.cap_y + 86 * k], fill=fade_c(accent, ca))
        d.text((60 * k, self.cap_y + 50 * k), head, font=self.f_cap,
               fill=fade_c(th["fg"], ca), anchor="lm")
        d.text((60 * k, self.cap_y + 94 * k), sub, font=self.f_sub,
               fill=fade_c(th["muted"], ca), anchor="lm")

    def furniture(self, d, head: str, sub: str, ca: int, accent=None) -> None:
        self.header(d, accent)
        self.bar(d, head, sub, ca, accent)


# ---------------------------------------------------------------------------
# explode: one capture, taken apart
# ---------------------------------------------------------------------------

DEFAULT_EXPLODE_TIMING = {
    "intro": 1.6, "explode": 1.5, "survey": 2.0, "move": 0.55, "hold": 2.0,
    "dive": 0.9, "rise": 0.7, "regroup": 0.6, "implode": 1.1, "outro": 1.4,
}
# how much room a camera leaves around whatever it is framing
CONTEXT = 1.36
SURVEY_CONTEXT = 1.05
# how far a container has to open before nothing of it is left behind
GHOST_GONE = 0.04
# how far an uninvolved piece recedes while a sibling is being taken apart
ASIDE = 0.09


# ---------------------------------------------------------------------------
# the pointer
# ---------------------------------------------------------------------------

# How long a gap in the samples ends one gesture and starts another, and how
# long the click bump takes to settle.
GESTURE_GAP = 0.4
CLICK_BUMP = 0.22


class Pointer:
    """where the mouse was, eased to the output frame rate.

    X does not draw the cursor into a window grab, so it is not in the
    captured pixels at all -- a drag arrives as a panel resizing itself for no
    visible reason. What the capture does record is the path it walked, and
    that is eight jumps for a drag, because eight is how many the program had
    to be told about. Played back one sample per frame it reads as eight
    teleports, so the samples of one gesture are treated as a single travel
    and the position is taken along its arc with an ease -- which is what a
    hand does, and what the eight jumps were standing in for.
    """

    def __init__(self, samples: list[dict]):
        self.s = [dict(p) for p in samples]
        self.gestures = []
        self.clicks = [b["t"] for a, b in zip(self.s, self.s[1:])
                       if b["down"] and not a["down"]]
        if self.s and self.s[0]["down"]:
            self.clicks.insert(0, self.s[0]["t"])
        run: list[dict] = []
        for p in self.s:
            if run and (p["t"] - run[-1]["t"] > GESTURE_GAP
                        or p["visible"] != run[-1]["visible"]):
                self.gestures.append(run)
                run = []
            run.append(p)
        if run:
            self.gestures.append(run)

    @staticmethod
    def _along(pts, frac):
        """the point `frac` of the way along a polyline, by arc length"""
        segs = [math.dist((a["x"], a["y"]), (b["x"], b["y"]))
                for a, b in zip(pts, pts[1:])]
        total = sum(segs)
        if total <= 0:
            return pts[-1]["x"], pts[-1]["y"]
        want, run = frac * total, 0.0
        for (a, b), L in zip(zip(pts, pts[1:]), segs):
            if run + L >= want:
                u = (want - run) / L if L else 0.0
                return lerp(a["x"], b["x"], u), lerp(a["y"], b["y"], u)
            run += L
        return pts[-1]["x"], pts[-1]["y"]

    def at(self, t: float):
        """-> (x, y, bump) in capture pixels, or None if there is no pointer.

        None before the first sample -- the pointer is wherever X happened to
        leave it, and drawing it there would invent a mouse the clip never
        moved -- and None once a step has parked it out of frame.
        """
        if not self.s or t < self.s[0]["t"]:
            return None
        g = None
        for cand in self.gestures:
            if cand[0]["t"] <= t:
                g = cand
            else:
                break
        if g is None or not g[-1]["visible"]:
            return None
        t0, t1 = g[0]["t"], g[-1]["t"]
        if t <= t0 or t1 <= t0:
            x, y = g[0]["x"], g[0]["y"]
        elif t >= t1:
            x, y = g[-1]["x"], g[-1]["y"]
        else:
            x, y = self._along(g, ease((t - t0) / (t1 - t0)))
        bump = 0.0
        for ct in self.clicks:
            if 0 <= t - ct < CLICK_BUMP:
                bump = max(bump, 1.0 - (t - ct) / CLICK_BUMP)
        return x, y, bump


def load_path(run_dir: str) -> dict:
    """a recorded run's timing and pointer path, if the capture left one"""
    f = os.path.join(run_dir, "frames.json")
    if not os.path.exists(f):
        return {}
    with open(f) as fh:
        d = json.load(fh)
    return {"t": [fr["t"] for fr in d.get("frames", [])],
            "pointer": Pointer(d.get("pointer", []))}


def _union(rects):
    return (min(r[0] for r in rects), min(r[1] for r in rects),
            max(r[2] for r in rects), max(r[3] for r in rects))


def _shift(r, off):
    return (r[0] + off[0], r[1] + off[1], r[2] + off[0], r[3] + off[1])


def _meet(a, b):
    """where two rects overlap, or None"""
    r = (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))
    return r if r[2] > r[0] and r[3] > r[1] else None


def _cut_overlaps(pieces, inherited=()):
    """work out what each piece must not carry away with it.

    A capture is flat: the palette's pixels are in the image at the rows the
    explorer and the editor occupy, so cutting those two out cuts out a copy of
    the palette along with them, and pulling the pieces apart leaves the copy
    on show. Whatever is drawn over a piece is therefore a hole in it — those
    pixels belong to the overlay, and what was underneath was never captured.
    """
    for i, p in enumerate(pieces):
        holes = [h for h in (_meet(p.src, q) for q in inherited) if h]
        holes += [h for h in (_meet(p.src, o.src) for o in pieces[i + 1:]) if h]
        p.holes = tuple(holes)
        _cut_overlaps(p.children, p.holes)


def _lerp_rect(a, b, t):
    return tuple(lerp(a[k], b[k], t) for k in range(4))


def _ghost(e: float) -> float:
    """what is left of a container while its children come out of it.

    Gone as soon as anything moves. A copy of the original sitting behind
    pieces that have only half moved reads as a smear, not as a thing coming
    apart — and at rest the pieces cover what they were cut from anyway, so
    there is nothing to keep. The pieces carry the image.
    """
    return max(0.0, 1.0 - e / GHOST_GONE)


def _pair(v):
    """a spread or a stagger, given either as one number or as [x, y]"""
    if isinstance(v, (int, float)):
        return (float(v), float(v))
    return (float(v[0]), float(v[1]))


class Piece:
    """One annotated rect, and where it goes when its container comes apart.

    `off` is in cells, and is the whole of what exploding means: a piece's
    position at any moment is its source rect plus every ancestor's offset,
    each weighted by how far that ancestor has come apart. Nothing else moves,
    so a piece that bursts open does it in place, at the scale it already had,
    and the camera is what makes the result legible.
    """

    def __init__(self, d: dict, path: tuple, cont, spread,
                 stagger=(0.0, 0.0), index: float = 0.0):
        self.rows = [float(v) for v in d["rows"]]
        # a piece that names no columns spans its container's
        self.cols = [float(v) for v in d.get("cols", [cont[0], cont[2]])]
        self.src = (self.cols[0], self.rows[0], self.cols[1], self.rows[1])
        self.path = path
        self.holes: tuple = ()          # filled in by `_cut_overlaps`
        self.head = d.get("head", "")
        self.sub = d.get("sub", "")
        self.label = d.get("label", self.head)
        self.at = d.get("label_at", "auto")
        # a container that has children says what to call the moment it opens
        self.dive_head = d.get("dive_head", "")
        self.dive_sub = d.get("dive_sub", "")

        if "offset" in d:
            self.off = (float(d["offset"][0]), float(d["offset"][1]))
        else:
            sx, sy = spread
            self.off = (((self.src[0] + self.src[2]) - (cont[0] + cont[2])) / 2 * sx,
                        ((self.src[1] + self.src[3]) - (cont[1] + cont[3])) / 2 * sy)

        # A long list of same-shaped children pushed apart radially piles up
        # along one axis and stays as cramped as it started. `stagger` deals
        # each child a little further along than the last, so the group opens
        # into a fan the camera can actually get close to.
        self.off = (self.off[0] + stagger[0] * index,
                    self.off[1] + stagger[1] * index)

        sub_spread = _pair(d.get("spread", spread))
        sub_stagger = _pair(d.get("stagger", (0.0, 0.0)))
        kids = d.get("pieces", [])
        mid = (len(kids) - 1) / 2
        self.children = [
            Piece(c, path + (i,), self.src, sub_spread, sub_stagger, i - mid)
            for i, c in enumerate(kids)]

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()


class ExplodeRenderer(Furniture):
    """A capture taken apart into its annotated rects, one beat per piece.

        intro    the capture whole
        explode  every piece is cut out of it and slides away; what was not
                 cut out goes with the original, which does not survive
        survey   the assembly entire, every piece labelled
        move     the camera travels to the next piece
        hold     one piece framed and lit, the rest dimmed
        dive     a piece with children bursts open in place, and the camera
                 follows it in; a survey of the opened group follows, and any
                 child carrying a caption of its own then gets a beat too
        implode  everything returns, and the end caption arrives
    """

    def __init__(self, spec: dict, captures: dict[str, str], outdir: str):
        r = spec["render"]
        ex = r["explode"]
        self.spec, self.r, self.outdir = spec, r, outdir
        self.mode = "explode"

        pane = next(iter(spec["capture"]["panes"]))
        self.A = Image.open(captures[pane]).convert("RGB")
        self.IW, self.IH = self.A.size
        self.rows = int(r["rows"])
        self.cols = int(r["cols"])
        self.RH = self.IH / self.rows
        self.CW = self.IW / self.cols
        self.screen = (0.0, 0.0, float(self.cols), float(self.rows))

        self.W, self.H = r.get("size", [1920, 1080])
        self.fps = int(r.get("fps", 60))
        # Everything this renderer draws in its own voice is sized off the
        # canvas, so a half-size draft is a half-size picture rather than a
        # full-size caption bar over a shrunken one.
        self.uk = self.H / 1080
        self.header_h = int(r.get("header_height", 96) * self.uk)
        self.cap_h = int(r.get("caption_height", 132) * self.uk)
        self.cap_y = self.H - self.cap_h
        self.vp = (0, self.header_h, self.W, self.cap_y - self.header_h)


        th = dict(DEFAULT_THEME)
        th.update(r.get("theme", {}))
        self.th = {k: tuple(v) for k, v in th.items()}
        self.t = dict(DEFAULT_EXPLODE_TIMING)
        self.t.update(r.get("timing", {}))

        spread = _pair(ex.get("spread", 0.45))
        stagger = _pair(ex.get("stagger", (0.0, 0.0)))
        mid = (len(ex["pieces"]) - 1) / 2
        self.pieces = [Piece(d, (i,), self.screen, spread, stagger, i - mid)
                       for i, d in enumerate(ex["pieces"])]
        _cut_overlaps(self.pieces)
        if not self.pieces:
            raise SystemExit("render.explode.pieces is empty; nothing to take apart")
        self.by_path = {p.path: p for p in self.walk()}

        self.cap_intro = r.get("intro_caption", ["", ""])
        self.cap_outro = r.get("outro_caption", ["", ""])
        self.cap_survey = r.get("survey_caption", self.cap_intro)
        lb = dict(DEFAULT_LABELS)
        lb.update(r.get("labels", {}))
        self.lead = lb["explode"]
        self.title = r.get("title", "")

        k = self.uk
        self.f_hdr = ImageFont.truetype(BOLD, int(38 * k))
        self.f_meta = ImageFont.truetype(MONO, int(24 * k))
        self.f_cap = ImageFont.truetype(BOLD, int(42 * k))
        self.f_sub = ImageFont.truetype(MONO, int(24 * k))
        self.f_chip = ImageFont.truetype(BOLD, int(23 * k))
        self._cache: dict = {}
        self.timeline = self._timeline()

    def walk(self):
        for p in self.pieces:
            yield from p.walk()

    # -- world geometry -----------------------------------------------------
    def _offset(self, path: tuple, levels: dict) -> tuple:
        """where the piece at `path` currently sits, relative to its source rect"""
        dx = dy = 0.0
        pieces, prefix = self.pieces, ()
        for i in path:
            node = pieces[i]
            e = levels.get(prefix, 0.0)
            dx += node.off[0] * e
            dy += node.off[1] * e
            pieces, prefix = node.children, prefix + (i,)
        return dx, dy

    def world(self, path: tuple, levels: dict):
        return _shift(self.by_path[path].src, self._offset(path, levels))

    def group_box(self, path: tuple, levels: dict):
        """the bounding box of a group, in world cells"""
        kids = (self.pieces if not path else self.by_path[path].children)
        rects = [self.world(k.path, levels) for k in kids]
        if not path:
            rects.append(self.screen)
        return _union(rects)

    # -- camera -------------------------------------------------------------
    def _frame_rect(self, box, context: float):
        """grow `box` to the viewport's aspect, then leave room around it"""
        x0, y0, x1, y1 = box
        w = max(1e-6, (x1 - x0) * self.CW)
        h = max(1e-6, (y1 - y0) * self.RH)
        want = self.vp[2] / self.vp[3]
        if w / h < want:
            w = h * want
        else:
            h = w / want
        w, h = w * context / self.CW, h * context / self.RH
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        return (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)

    def _camera(self, box):
        """-> (scale, canvas x of cell 0, canvas y of cell 0)"""
        w = max(1e-6, (box[2] - box[0]) * self.CW)
        h = max(1e-6, (box[3] - box[1]) * self.RH)
        s = min(self.vp[2] / w, self.vp[3] / h)
        cx = self.vp[0] + self.vp[2] / 2 - (box[0] + box[2]) / 2 * self.CW * s
        cy = self.vp[1] + self.vp[3] / 2 - (box[1] + box[3]) / 2 * self.RH * s
        return s, cx, cy

    def _to_canvas(self, rect, cam):
        s, ox, oy = cam
        return (ox + rect[0] * self.CW * s, oy + rect[1] * self.RH * s,
                ox + rect[2] * self.CW * s, oy + rect[3] * self.RH * s)

    # -- storyboard ---------------------------------------------------------
    def _levels(self, path: tuple, opening: float = 1.0) -> dict:
        """every ancestor group of `path` open, and `path`'s own group `opening`"""
        lv = {(): 1.0}
        for k in range(1, len(path)):
            lv[path[:k]] = 1.0
        if path:
            lv[path] = opening
        return lv

    def _timeline(self) -> list[dict]:
        """the whole clip as segments, each with a camera move and a caption"""
        T = self.t
        segs: list[dict] = []

        def seg(kind, dur, cam0, cam1, focus=None, head="", sub="",
                lv0=None, lv1=None):
            if dur > 0:
                segs.append({"kind": kind, "dur": float(dur),
                             "cam0": cam0, "cam1": cam1, "focus": focus,
                             "head": head, "sub": sub,
                             "lv0": lv0 or {}, "lv1": lv1 if lv1 is not None else (lv0 or {})})

        open_root = {(): 1.0}
        whole = self._frame_rect(self.screen, SURVEY_CONTEXT)
        survey = self._frame_rect(self.group_box((), open_root), SURVEY_CONTEXT)

        def framed(path, levels):
            return self._frame_rect(self.world(path, levels), CONTEXT)

        def dive_framed(path, levels):
            return self._frame_rect(self.group_box(path, levels), CONTEXT)

        seg("intro", T["intro"], whole, whole,
            head=self.cap_intro[0], sub=self.cap_intro[1])
        seg("explode", T["explode"], whole, survey,
            head=self.cap_survey[0], sub=self.cap_survey[1],
            lv0={}, lv1=open_root)
        seg("survey", T["survey"], survey, survey,
            head=self.cap_survey[0], sub=self.cap_survey[1], lv0=open_root)

        def walk(pieces, path, enter_cam):
            """a beat for every piece that carries a caption, in order"""
            for p in pieces:
                if not p.head:
                    continue          # drawn, and labelled, but not dwelt on
                cp, lv = p.path, self._levels(path)
                cam = framed(cp, lv)
                seg("move", T["move"], enter_cam, cam,
                    focus=cp, head=p.head, sub=p.sub, lv0=lv)
                seg("hold", T["hold"], cam, cam,
                    focus=cp, head=p.head, sub=p.sub, lv0=lv)
                enter_cam = cam
                if p.children:
                    lv_in = self._levels(cp)     # ... and now this piece's own
                    inner = dive_framed(cp, lv_in)
                    dh = p.dive_head or p.head
                    ds = p.dive_sub or p.sub
                    seg("dive", T["dive"], cam, inner, focus=cp,
                        head=dh, sub=ds,
                        lv0=self._levels(cp, 0.0), lv1=lv_in)
                    seg("survey", T["survey"], inner, inner, focus=cp,
                        head=dh, sub=ds, lv0=lv_in)
                    walk(p.children, cp, inner)
                    seg("rise", T["rise"], inner, cam, focus=cp,
                        head=p.head, sub=p.sub,
                        lv0=lv_in, lv1=self._levels(cp, 0.0))
                    enter_cam = cam

        walk(self.pieces, (), survey)
        seg("regroup", T["regroup"], segs[-1]["cam1"], survey,
            head=self.cap_survey[0], sub=self.cap_survey[1], lv0=open_root)
        seg("implode", T["implode"], survey, whole,
            head=self.cap_outro[0], sub=self.cap_outro[1],
            lv0=open_root, lv1={})
        seg("outro", T["outro"], whole, whole,
            head=self.cap_outro[0], sub=self.cap_outro[1])
        return segs

    def total(self) -> float:
        return sum(s["dur"] for s in self.timeline)

    def at(self, t: float):
        """-> (segment, progress through it, the caption that preceded it)"""
        acc = 0.0
        for i, s in enumerate(self.timeline):
            if t < acc + s["dur"] or i == len(self.timeline) - 1:
                u = (t - acc) / s["dur"] if s["dur"] > 0 else 1.0
                prev = self.timeline[i - 1] if i else None
                return s, max(0.0, min(1.0, u)), prev
            acc += s["dur"]
        raise AssertionError("empty timeline")

    # -- painting -----------------------------------------------------------
    def scaled(self, src, size, holes=()):
        key = (src, size, holes)
        if key not in self._cache:
            if len(self._cache) > 240:
                self._cache.clear()
            box = (int(round(src[0] * self.CW)), int(round(src[1] * self.RH)),
                   int(round(src[2] * self.CW)), int(round(src[3] * self.RH)))
            crop = self.A.crop(box)
            if holes:
                hd = ImageDraw.Draw(crop)
                for h in holes:
                    hd.rectangle(
                        [int(round(h[0] * self.CW)) - box[0],
                         int(round(h[1] * self.RH)) - box[1],
                         int(round(h[2] * self.CW)) - box[0] - 1,
                         int(round(h[3] * self.RH)) - box[1] - 1],
                        fill=self.th["bg"])
            self._cache[key] = crop.resize(size, Image.LANCZOS)
        return self._cache[key]

    def _paint(self, cv, src, dest, alpha: float, dim: float, holes=()):
        if alpha <= 0.006:
            return None
        x0, y0 = int(round(dest[0])), int(round(dest[1]))
        w = max(1, int(round(dest[2])) - x0)
        h = max(1, int(round(dest[3])) - y0)
        if x0 > self.W or y0 > self.cap_y or x0 + w < 0 or y0 + h < self.header_h:
            return (x0, y0, w, h)          # off frame: place it, do not paint it
        img = self.scaled(src, (w, h), holes)
        if dim > 0.004:
            img = Image.blend(img, Image.new("RGB", (w, h), self.th["bg"]), dim)
        if alpha >= 0.995:
            cv.paste(img, (x0, y0))
        else:
            box = (x0, y0, x0 + w, y0 + h)
            cv.paste(Image.blend(cv.crop(box), img, alpha), (x0, y0))
        return (x0, y0, w, h)

    def _draw_group(self, cv, d, pieces, path, levels, cam, off, amult, focus,
                    labels):
        e = levels.get(path, 0.0)
        opening = max([levels.get(p.path, 0.0) for p in pieces], default=0.0)
        for p in pieces:
            poff = (off[0] + p.off[0] * e, off[1] + p.off[1] * e)
            dest = self._to_canvas(_shift(p.src, poff), cam)
            q = levels.get(p.path, 0.0)
            # a sibling of whatever is being taken apart steps out of the way
            a = amult * (1.0 - (1.0 - ASIDE) * (opening - q if q < opening else 0.0))
            lit = self._lit(p.path, focus)
            if q > 0.0:
                # this piece is (or is becoming) a container: what is left of it
                # goes down first, and its children over the top
                self._paint(cv, p.src, dest, a * _ghost(q), 0.0, p.holes)
                self._draw_group(cv, d, p.children, p.path, levels, cam, poff,
                                 a, focus, labels)
                continue
            box = self._paint(cv, p.src, dest, a, 0.55 * (1.0 - lit) * e,
                              p.holes)
            if box and e > 0.06:
                self._outline(d, box, lit, a * e)
            if p.label and e > 0.10:
                labels.append((p, dest, a * e, lit))

    def _lit(self, path, focus) -> float:
        """1 where the beat is, falling off for everything else.

        Focus weights sum to 1 once a beat has settled; while one is still
        arriving they sum to less, and the shortfall lights everything, so the
        dim comes up with the beat instead of snapping on ahead of it.
        """
        if not focus:
            return 1.0
        rest = max(0.0, 1.0 - sum(w for _, w in focus))
        return max(rest, max((w for fp, w in focus if path[:len(fp)] == fp),
                             default=0.0))

    def _outline(self, d, box, lit: float, alpha: float):
        x, y, w, h = box
        col = tuple(int(lerp(self.th["panel_border"][k], self.th["after"][k], lit))
                    for k in range(3))
        rad = max(2, min(10, min(w, h) // 3))
        d.rounded_rectangle([x, y, x + w - 1, y + h - 1], radius=rad,
                            outline=fade_c(col, int(255 * alpha * (0.5 + 0.5 * lit))),
                            width=2 if lit > 0.5 else 1)

    def _chips(self, cv, d, labels):
        """place every label, brightest first, dropping the ones with no room.

        A chip belongs to a piece, so it goes where the piece is; but at a beat
        most pieces are off frame, and a chip clamped back into view is a chip
        sitting on somebody else's. An off-frame piece loses its label, and what
        is left gets nudged until it stops colliding.
        """
        placed: list = []
        for piece, dest, alpha, lit in sorted(labels, key=lambda l: -l[3]):
            a = int(255 * alpha * (0.30 + 0.70 * lit))
            if a < 8 or not self._on_screen(dest):
                continue
            tb = d.textbbox((0, 0), piece.label, font=self.f_chip)
            pad = 10
            w, h = tb[2] - tb[0] + 2 * pad, tb[3] - tb[1] + 2 * pad
            spot = self._spot(piece, dest, w, h, placed)
            if spot is None:
                continue
            x, y = spot
            placed.append((x, y, x + w, y + h))

            chip = Image.new("RGBA", (int(w), int(h)), (0, 0, 0, 0))
            cd = ImageDraw.Draw(chip)
            col = self.th["after"] if lit > 0.5 else self.th["fg"]
            cd.rounded_rectangle([0, 0, w - 1, h - 1], radius=8,
                                 fill=(*self.th["caption_bg"], int(a * 0.90)),
                                 outline=(*col, int(a * 0.55)), width=1)
            cd.text((w / 2, h / 2), piece.label, font=self.f_chip,
                    fill=(*col, a), anchor="mm")
            cv.paste(chip, (int(x), int(y)), chip)

    def _on_screen(self, dest) -> bool:
        return (dest[2] > 0 and dest[0] < self.W
                and dest[3] > self.header_h and dest[1] < self.cap_y)

    def _spot(self, piece, dest, w, h, placed):
        """-> where this chip goes, or None when everything near it is taken"""
        gap = 14
        cx, cy = (dest[0] + dest[2]) / 2, (dest[1] + dest[3]) / 2
        side = piece.at
        if side == "auto":
            dx, dy = piece.off
            side = (("below" if dy > 0 else "above")
                    if abs(dy) * self.RH >= abs(dx) * self.CW
                    else ("right" if dx > 0 else "left"))
        x, y = cx - w / 2, cy - h / 2
        if side == "above":
            y = dest[1] - gap - h
        elif side == "below":
            y = dest[3] + gap
        elif side == "left":
            x = dest[0] - gap - w
        elif side == "right":
            x = dest[2] + gap
        x = max(8, min(self.W - w - 8, x))
        step = h + 6
        for k in range(6):                    # its own spot first, then aside
            for dy in ((0.0,) if k == 0 else (k * step, -k * step)):
                yy = y + dy
                if yy < self.header_h + 6 or yy + h > self.cap_y - 6:
                    continue
                box = (x, yy, x + w, yy + h)
                if not any(box[0] < q[2] and q[0] < box[2]
                           and box[1] < q[3] and q[1] < box[3] for q in placed):
                    return (x, yy)
        return None

    # -- main ---------------------------------------------------------------
    def run(self) -> int:
        os.makedirs(self.outdir, exist_ok=True)
        for f in os.listdir(self.outdir):
            os.remove(os.path.join(self.outdir, f))
        nf = int(round(self.total() * self.fps))
        for n in range(nf):
            post_fx(self, self.frame(n), n, nf).save(f"{self.outdir}/f{n:05d}.png")
        return nf

    def frame(self, n: int) -> Image.Image:
        th, W, H = self.th, self.W, self.H
        s, u, prev = self.at(n / self.fps)
        p = ease(u)

        levels = {k: lerp(s["lv0"].get(k, 0.0), s["lv1"].get(k, 0.0), p)
                  for k in set(s["lv0"]) | set(s["lv1"])}
        cam = self._camera(_lerp_rect(s["cam0"], s["cam1"], p))

        cv = Image.new("RGB", (W, H), th["bg"])
        d = ImageDraw.Draw(cv)

        focus = []
        if s["focus"]:
            if s["kind"] == "move":
                if prev and prev["focus"]:
                    focus.append((prev["focus"], 1.0 - p))
                focus.append((s["focus"], p))
            else:
                focus.append((s["focus"], 1.0))
        elif prev and prev["focus"]:
            focus.append((prev["focus"], 1.0 - p))   # the dim releasing

        e_root = levels.get((), 0.0)
        self._paint(cv, self.screen, self._to_canvas(self.screen, cam),
                    _ghost(e_root), 0.0)
        labels: list = []
        self._draw_group(cv, d, self.pieces, (), levels, cam, (0.0, 0.0),
                         1.0, focus, labels)
        self._chips(cv, d, labels)

        self.furniture(d, *self.caption(s, u, prev))
        return cv


# ---------------------------------------------------------------------------
# donut: a ranked breakdown, read one section at a time
# ---------------------------------------------------------------------------

DEFAULT_DONUT_TIMING = {
    "grow": 1.2, "intro": 2.2, "move": 0.55, "hold": 3.0,
    "regroup": 0.6, "outro": 2.0,
}
# How long the labels take to arrive and to go. They are not carried by the
# camera, they are cut in and out around it: a move begins once they are gone
# and the next still frame brings them back.
LABEL_FADE = 0.22

# World x where a label's words start, the ring's outer radius being 1. The
# scale of the establishing shot follows from it: the words are a fixed size in
# pixels, so the longest of them is what decides how big the ring can be and
# still have its labels inside the frame. Solving it that way round is the only
# way that terminates -- measuring the words needs a scale, and the scale is
# what the measurement was for.
LABEL_X = 1.30
# what the ring alone is allowed of the viewport's height, for the case where
# the labels are short enough that the width stops being the constraint
RING_FILL = 0.92
# the establishing caption's two lines, and the air between them and the ring
ESTAB_H = 104
ESTAB_GAP = 44
# How wide a card's description is allowed to run before it wraps. It has to
# go up with the description's type: the note is set nearly as large as the
# name above it, because it is the half of the card a reader has to actually
# read, and at a narrow measure that size wraps a sentence into a column.
CARD_TEXT = 470
# How much air the camera leaves around a section *and its card*, which are
# framed as one object. Little, because the card is most of the air already.
SECTION_CONTEXT = 1.1
# between a section's rim and its card, and between the card and the frame
CARD_GAP = 28
CARD_MARGIN = 30
# A safety rail rather than a working limit. A section is framed together with
# its card, and the card is a fixed size in pixels, so a sliver cannot fill the
# frame with flat colour however small it is -- the card is always a third of
# the picture and holds the zoom down on its own. This is here for the spec
# that wants a flatter clip than the geometry would give it.
MAX_ZOOM = 7.0
# How far the camera pulls back over the middle of a travel. Two sections on
# opposite sides of the ring are a long way apart once you are close to one,
# and a flat pan between them is a wall of colour going past; lifting away and
# settling again is the move a hand makes, and it puts the whole ring back on
# screen in the middle of it, which is where the reader re-finds themselves.
# Small, because the move is short: a deep arc crossed in half a second is a
# lurch rather than a lift.
PULLBACK = 0.12
# how far the section being read slides out of the ring
POP = 0.045
# what is left of a section nobody is looking at
DONUT_DIM = 0.68
# The ring is drawn at this multiple and brought back down. PIL has no
# antialiasing, and a hard-edged arc that moves is a crawling staircase --
# on a curve it is the one artefact a viewer will see before the data.
DONUT_SS = 3

# A ranked breakdown is categorical, not a scale: neighbouring sections have
# to be told apart at a glance, so the ramp steps around the wheel rather than
# along a gradient. The last is grey, which is where an "other" lands.
DONUT_RAMP = [
    [74, 222, 128], [56, 189, 248], [251, 191, 36], [167, 139, 250],
    [244, 114, 182], [45, 212, 191], [248, 113, 113], [148, 163, 184],
]


def _fmt_value(v: float) -> str:
    """a number as a person would write it"""
    if abs(v - round(v)) < 1e-9:
        return f"{round(v):,}"
    return f"{v:,.1f}"


def _fmt_share(f: float) -> str:
    """a share as a percentage, with a decimal only where it earns one"""
    pct = f * 100
    return f"{pct:.1f}%" if pct < 10 else f"{pct:.0f}%"


def _push(c0, w0, c1, w1, lo, hi, out) -> float:
    """how far a card and its section can slide together before one of them
    hits a wall -- outwards when `out` is positive, inwards when it is not.

    Never past the wall it started against, which is what the `max` is for: a
    card already outside the frame is not helped by sliding it further out.
    """
    if out >= 0:
        return max(0.0, min(hi - c1, hi - w1))
    return -max(0.0, min(c0 - lo, w0 - lo))


def _bbox(pts):
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _stack(wants: list[float], lh: float, top: float, bot: float) -> list[float]:
    """slide a column of labels apart until none of them overlap.

    `wants[i]` is where label i would like to sit, in ascending order. Each is
    pushed down until it clears the one above, then the whole column is pulled
    back inside (top, bot) from whichever end it ran past. Two passes, because
    pulling the bottom back in can push the top out again.
    """
    ys = list(wants)
    for i in range(1, len(ys)):
        ys[i] = max(ys[i], ys[i - 1] + lh)
    if ys and ys[-1] > bot:
        ys[-1] = bot
        for i in range(len(ys) - 2, -1, -1):
            ys[i] = min(ys[i], ys[i + 1] - lh)
    if ys and ys[0] < top:
        ys[0] = top
        for i in range(1, len(ys)):
            ys[i] = max(ys[i], ys[i - 1] + lh)
    return ys


class DonutRenderer(Furniture):
    """A ranked breakdown as a donut, read one section at a time.

        grow     the ring draws itself in, clockwise from the start angle
        intro    the whole figure, every section labelled with its share
        move     a pan-and-zoom onto the next section, pulling back over the
                 middle of the travel so the ring is never lost
        hold     the section framed, slid out of the ring and lit while the
                 rest is dimmed, with its label, value and share beside it
        regroup  the camera comes back to the whole
        outro    the end caption

    Nothing is filmed. A breakdown is a set of numbers, not a screen, so the
    picture is drawn from the data in the spec -- which is also why the camera
    can go as close as it likes: every frame is drawn at the scale it is seen
    at, and a section fills the frame as cleanly as the ring did.

    The reading order is the ranking. Sorted by value unless the spec says
    otherwise, the clip walks from the biggest share down, which is the order
    a person asking "what is using the memory?" wants the answer in.
    """

    def __init__(self, spec: dict, outdir: str):
        r = spec["render"]
        dn = r["donut"]
        self.spec, self.r, self.outdir = spec, r, outdir
        self.mode = "donut"

        self.W, self.H = r.get("size", [1080, 1080])
        self.fps = int(r.get("fps", 60))
        self.uk = self.H / 1080
        self.header_h = int(r.get("header_height", 96) * self.uk)
        # No caption bar. What a beat has to say is said on the section the
        # camera is sitting on, and a reader whose eye is in the middle of the
        # frame does not read a strip along the bottom -- they read it after,
        # if at all, having already decided what the picture meant. So the
        # picture gets the height the bar would have taken.
        self.vp = (0, self.header_h, self.W, self.H - self.header_h)

        th = dict(DEFAULT_THEME)
        th.update(r.get("theme", {}))
        self.th = {k: tuple(v) for k, v in th.items()}
        self.t = dict(DEFAULT_DONUT_TIMING)
        self.t.update(r.get("timing", {}))

        self.unit = dn.get("unit", "")
        self.start = float(dn.get("start_angle", -90))
        self.inner = 1.0 - float(dn.get("thickness", 0.40))
        if not 0.0 < self.inner < 1.0:
            raise SystemExit("render.donut.thickness must be between 0 and 1")
        self.gap = float(dn.get("gap", 1.0))
        self.max_zoom = float(dn.get("max_zoom", MAX_ZOOM))
        self.dim = float(dn.get("dim", DONUT_DIM))

        items = [dict(d) for d in dn.get("items", [])]
        if not items:
            raise SystemExit("render.donut.items is empty; nothing to break down")
        for d in items:
            if "label" not in d or "value" not in d:
                raise SystemExit(
                    f"every donut item needs a `label` and a `value`; got {d!r}")
            d["value"] = float(d["value"])
            if d["value"] < 0:
                raise SystemExit(
                    f"donut item {d['label']!r} has a negative value; a section "
                    "of a whole cannot be less than nothing")
        if dn.get("sort", True):
            items.sort(key=lambda d: -d["value"])
        total = sum(d["value"] for d in items)
        if total <= 0:
            raise SystemExit("the donut's values sum to zero; nothing to draw")

        # angles accumulate clockwise from `start_angle`, which is where the
        # ring is read from -- 12 o'clock by default, as a clock is
        acc = self.start
        for i, d in enumerate(items):
            d["share"] = d["value"] / total
            d["a0"], d["a1"] = acc, acc + 360.0 * d["share"]
            acc = d["a1"]
            d["color"] = tuple(d.get("color", DONUT_RAMP[i % len(DONUT_RAMP)]))
            d["note"] = d.get("note", "")
            d["display"] = d.get("display") or (
                f"{_fmt_value(d['value'])} {self.unit}".strip())
        self.items = items
        self.visit = [i for i, d in enumerate(items) if d.get("visit", True)]

        self.total_txt = dn.get("total", f"{_fmt_value(total)} {self.unit}".strip())
        self.total_label = dn.get("total_label", "")

        self.cap_intro = r.get("intro_caption", ["", ""])
        self.cap_outro = r.get("outro_caption", ["", ""])
        self.estab_h = ((ESTAB_H + ESTAB_GAP) * self.uk
                        if any(self.cap_intro) or any(self.cap_outro) else 0)
        lb = dict(DEFAULT_LABELS)
        lb.update(r.get("labels", {}))
        self.lead = lb["donut"]
        self.title = r.get("title", "")

        k = self.uk
        self.f_hdr = ImageFont.truetype(BOLD, int(38 * k))
        self.f_meta = ImageFont.truetype(MONO, int(24 * k))
        self.f_cap = ImageFont.truetype(BOLD, int(42 * k))
        self.f_sub = ImageFont.truetype(MONO, int(24 * k))
        self.f_lab = ImageFont.truetype(BOLD, int(26 * k))
        self.f_val = ImageFont.truetype(MONO, int(21 * k))
        self.f_card = ImageFont.truetype(BOLD, int(34 * k))
        self.f_big = ImageFont.truetype(BOLD, int(52 * k))
        self.f_pct = ImageFont.truetype(MONO, int(24 * k))
        self.f_note = ImageFont.truetype(MONO, int(29 * k))
        self._fonts: dict = {}
        # a description is the point of the card, so it is set between the
        # muted grey of a measurement and the full white of a headline
        self.note_c = tuple(int(lerp(self.th["muted"][c], self.th["fg"][c], 0.5))
                            for c in range(3))

        self._fit()
        self._layout_labels()
        self._place_figure()
        self._build_cards()
        self.section_view = {i: self._section_view(i) for i in self.visit}
        self.timeline = self._timeline()

    # -- camera -------------------------------------------------------------
    def _cam_for(self, box, context: float):
        """-> (canvas px per world unit, world x, world y) framing `box`"""
        w = max(1e-6, box[2] - box[0]) * context
        h = max(1e-6, box[3] - box[1]) * context
        s = min(self.vp[2] / w, self.vp[3] / h)
        return (s, (box[0] + box[2]) / 2, (box[1] + box[3]) / 2)

    def _pt(self, cam, x: float, y: float):
        s, cx, cy = cam
        return (self.vp[0] + self.vp[2] / 2 + (x - cx) * s,
                self.vp[1] + self.vp[3] / 2 + (y - cy) * s)

    def _cam_lerp(self, c0, c1, p: float, arc: float = 0.0):
        """the camera part-way from c0 to c1.

        The scale is interpolated in log space: halfway between 1x and 4x is
        2x, not 2.5x, and a linear scale spends most of a travel already close
        in. `arc` lifts the camera away over the middle and brings it back.
        """
        s = math.exp(lerp(math.log(c0[0]), math.log(c1[0]), p))
        s *= 1.0 - arc * math.sin(math.pi * p)
        return (s, lerp(c0[1], c1[1], p), lerp(c0[2], c1[2], p))

    # -- geometry -----------------------------------------------------------
    def _arc(self, a0: float, a1: float, rad: float, off=(0.0, 0.0)):
        """points along an arc, at about a degree and a half apart"""
        n = max(2, int(abs(a1 - a0) / 1.5) + 2)
        out = []
        for j in range(n):
            t = math.radians(a0 + (a1 - a0) * j / (n - 1))
            out.append((math.cos(t) * rad + off[0], math.sin(t) * rad + off[1]))
        return out

    def _wedge(self, i: int, sweep: float = 1.0, pop: float = 0.0):
        """item i as an annulus sector, in world points, or None.

        The gap between sections is taken out of the section's own ends rather
        than drawn between them, so what separates two sections is the ground
        -- which is the only separator that stays a separator at every zoom.
        """
        d = self.items[i]
        a0, a1 = d["a0"], d["a1"]
        g = min(self.gap, (a1 - a0) * 0.45)
        b0, b1 = a0 + g / 2, min(a1 - g / 2, self.start + 360.0 * sweep)
        if b1 <= b0:
            return None
        mid = math.radians((a0 + a1) / 2)
        off = (math.cos(mid) * pop, math.sin(mid) * pop)
        return (self._arc(b0, b1, 1.0, off)
                + self._arc(b1, b0, self.inner, off))

    @staticmethod
    def _nearest(rim, x: float, y: float):
        """the point of `rim` closest to (x, y), all in canvas pixels.

        Where a leader starts, so that it is a stub between the section and
        its card rather than a line across the middle of the ring.
        """
        return min(rim, key=lambda q: (q[0] - x) ** 2 + (q[1] - y) ** 2)

    def _section_view(self, i: int):
        """-> (camera, where the card sits) for the beat that reads item i.

        The card is not fitted into whatever room the section's framing
        happened to leave. It is the other way round: the section and its card
        are one object, dealt out along the radius so the card is always clear
        of the ring and always on the far side of the section from the middle,
        and the camera frames the pair. Then the pair slides along that radius
        until the card is against the corner of the frame, or the section is
        against the opposite edge.

        What that spends is the hole, which goes off screen. It is the right
        thing to spend: the middle of the ring is the one part of the picture
        that is not about the section being read.

        The scale has to be solved for rather than computed, because the card
        is a fixed size in pixels and so its size *in the picture* depends on
        the scale that framing the picture produces. Iterating from the scale
        the section alone would take -- an upper bound, the card only ever
        making the box bigger -- walks down to the fixed point in a few steps.
        """
        card = self.cards[i]
        w, h = card["w"], card["h"]
        wedge = self._wedge(i, pop=POP)
        wb = _bbox(wedge)
        mid = math.radians((self.items[i]["a0"] + self.items[i]["a1"]) / 2)
        ux, uy = math.cos(mid), math.sin(mid)

        s = self._cam_for(wb, SECTION_CONTEXT)[0]
        box, at = wb, (0.0, 0.0)
        for _ in range(24):
            cw, ch = w / s, h / s
            reach = (1.0 + POP + math.hypot(cw, ch) / 2 + CARD_GAP * self.uk / s)
            at = (ux * reach, uy * reach)
            box = _union([wb, (at[0] - cw / 2, at[1] - ch / 2,
                               at[0] + cw / 2, at[1] + ch / 2)])
            new = min(self._cam_for(box, SECTION_CONTEXT)[0],
                      self.scale * self.max_zoom)
            if abs(new - s) < 0.05:
                s = new
                break
            s = new
        cam = (s, (box[0] + box[2]) / 2, (box[1] + box[3]) / 2)

        # ... and now slide the pair into the corner
        m = CARD_MARGIN * self.uk
        v = (self.vp[0] + m, self.vp[1] + m,
             self.vp[0] + self.vp[2] - m, self.vp[1] + self.vp[3] - m)
        px, py = self._pt(cam, *at)
        cr = (px - w / 2, py - h / 2, px + w / 2, py + h / 2)
        wr = _bbox([self._pt(cam, *q) for q in wedge])
        d = [_push(cr[k], wr[k], cr[k + 2], wr[k + 2], v[k], v[k + 2],
                   (ux, uy)[k]) for k in (0, 1)]
        cam = (s, cam[1] - d[0] / s, cam[2] - d[1] / s)
        return cam, self._pt(cam, *at)

    # How much room a two-line label takes in the column around the ring. A
    # class attribute rather than a number in the loop because it is a fact
    # about the type, and a renderer that sets its labels in something else
    # has to be able to say so.
    LABEL_LH = 64

    # -- labels around the ring ---------------------------------------------
    def _fit(self) -> None:
        """how big the ring can be and still have its labels in the frame.

        The longest label decides it. A smaller ring with every name legible
        beats a bigger one with two of them running off the side -- and where
        the names are short it is the viewport's height that binds instead, so
        a breakdown of three things is not held to the size a breakdown of ten
        would need.
        """
        margin = int(30 * self.uk)
        need = max(max(self.f_lab.getlength(d["label"]),
                       self.f_val.getlength(self._sub_label(d)))
                   for d in self.items)
        by_width = (self.W / 2 - margin - need - 12 * self.uk) / LABEL_X
        by_height = (self.vp[3] - self.estab_h) / 2 * RING_FILL
        self.scale = max(40.0, min(by_width, by_height))

    @staticmethod
    def _sub_label(item: dict) -> str:
        return f'{item["display"]}  ·  {_fmt_share(item["share"])}'

    def _layout_labels(self) -> None:
        """each label's leader and words, as offsets from the ring's centre in
        canvas pixels.

        Canvas, not world. A label belongs to the establishing shot, not to the
        ring: laid out in the world it travels with the ring, and a zoom then
        deals six labels outwards across the frame and off it -- which is
        movement the eye reads as the labels doing something, at the one moment
        the camera is the thing that is supposed to be moving. Placed here they
        never move at all. They are cut in once the camera is still and cut out
        before it starts, so there is no frame in which a label is both on
        screen and in the wrong place.
        """
        s, k = self.scale, self.uk
        lh = self.LABEL_LH * k            # two lines of label
        half = (self.vp[3] - self.estab_h) / 2 - 16 * k
        top, bot = -half + lh / 2, half - lh / 2
        self.labels: dict[int, dict] = {}
        for side in (1, -1):
            rows = []
            for i, d in enumerate(self.items):
                mid = math.radians((d["a0"] + d["a1"]) / 2)
                if (1 if math.cos(mid) >= 0 else -1) != side:
                    continue
                rows.append((math.sin(mid) * 1.20 * s, i, mid))
            rows.sort()
            ys = _stack([y for y, _, _ in rows], lh, top, bot)
            for (_, i, mid), y in zip(rows, ys):
                self.labels[i] = {
                    "side": side,
                    "anchor": (math.cos(mid) * 1.02 * s, math.sin(mid) * 1.02 * s),
                    "elbow": (math.cos(mid) * 1.17 * s, math.sin(mid) * 1.17 * s),
                    "stub": (side * LABEL_X * s, y),
                    "text": (side * (LABEL_X + 0.05) * s, y),
                }

    def _place_figure(self) -> None:
        """where the ring's centre sits, and where the establishing caption
        goes under it.

        The figure is however tall the ring and its labels turned out to be,
        plus the caption if there is one, all centred together. Reserving a
        fixed band instead leaves whatever the labels did not use as a gap
        between the ring and the words, and a caption floating a third of a
        frame below its subject is a caption for nothing.
        """
        s, k = self.scale, self.uk
        lo, hi = -s * 1.06, s * 1.06
        for L in self.labels.values():
            lo = min(lo, L["text"][1] - 15 * k - self.f_lab.size)
            hi = max(hi, L["text"][1] + 16 * k + self.f_val.size)
        total = (hi - lo) + self.estab_h
        cy = self.vp[1] + (self.vp[3] - total) / 2 - lo
        self.centre = (self.vp[0] + self.vp[2] / 2, cy)
        self.estab_y = cy + hi + (ESTAB_GAP * k if self.estab_h else 0)
        # the camera that puts the ring's centre exactly there
        self.cam_whole = (s, 0.0, (self.vp[1] + self.vp[3] / 2 - cy) / s)

    # -- storyboard ---------------------------------------------------------
    def _timeline(self) -> list[dict]:
        T = self.t
        segs: list[dict] = []

        def seg(kind, dur, cam0, cam1, focus=None, estab=None,
                sweep0=1.0, sweep1=1.0, labels=False, arc=0.0):
            """`labels` marks a segment the labels belong to -- one the camera
            holds still on the whole figure. Everywhere else they are simply
            not there, rather than there and being carried about."""
            if dur > 0:
                segs.append({"kind": kind, "dur": float(dur), "cam0": cam0,
                             "cam1": cam1, "focus": focus,
                             "estab": estab or ["", ""],
                             "sweep0": sweep0, "sweep1": sweep1,
                             "labels": labels, "arc": arc})

        whole = self.cam_whole
        seg("grow", T["grow"], whole, whole, estab=self.cap_intro,
            sweep0=0.0, sweep1=1.0)
        seg("intro", T["intro"], whole, whole, estab=self.cap_intro,
            labels=True)

        cam = whole
        for i in self.visit:
            to = self.section_view[i][0]
            seg("move", T["move"], cam, to, focus=i, arc=PULLBACK)
            seg("hold", float(self.items[i].get("hold", T["hold"])), to, to,
                focus=i)
            cam = to
        seg("regroup", T["regroup"], cam, whole, estab=self.cap_outro,
            arc=PULLBACK / 2)
        seg("outro", T["outro"], whole, whole, estab=self.cap_outro,
            labels=True)
        if not segs:
            raise SystemExit("every donut timing is zero; nothing to render")
        return segs

    def cut_alpha(self, seg: dict, u: float, last: bool) -> float:
        """how much of this segment's own overlay is showing, on its clock.

        Everything that is not the ring -- the labels around it, the card
        beside a section -- is cut in and out rather than carried: it arrives
        once the camera has settled and is gone before it starts again, so the
        travel between two sections is the only thing moving during the travel
        between two sections. The last segment does not fade out; there is
        nothing after it to get out of the way of.
        """
        t, d = u * seg["dur"], seg["dur"]
        f = min(LABEL_FADE, d / 2)
        if f <= 0:
            return 1.0
        a = min(1.0, t / f)
        if not last:
            a = min(a, (d - t) / f)
        return max(0.0, min(1.0, a))

    def total(self) -> float:
        return sum(s["dur"] for s in self.timeline)

    def at(self, t: float):
        """-> (segment, progress through it, the segment before it)"""
        acc = 0.0
        for i, s in enumerate(self.timeline):
            if t < acc + s["dur"] or i == len(self.timeline) - 1:
                u = (t - acc) / s["dur"] if s["dur"] > 0 else 1.0
                return s, max(0.0, min(1.0, u)), (self.timeline[i - 1] if i else None)
            acc += s["dur"]
        raise AssertionError("empty timeline")

    # -- painting -----------------------------------------------------------
    def _font(self, px: int):
        px = max(6, int(px))
        if px not in self._fonts:
            if len(self._fonts) > 64:
                self._fonts.clear()
            self._fonts[px] = ImageFont.truetype(BOLD, px)
        return self._fonts[px]

    def _lit(self, i: int, focus: list) -> float:
        """1 where the beat is, falling off for everything else"""
        if not focus:
            return 1.0
        rest = max(0.0, 1.0 - sum(w for _, w in focus))
        return max(rest, max((w for j, w in focus if j == i), default=0.0))

    def _ring(self, cv, cam, sweep: float, focus: list) -> None:
        """the ring, drawn at `DONUT_SS` and brought back down.

        Only the viewport is oversampled: it is the only part of the frame the
        ring is allowed into, and a full-canvas layer at three times the size
        is most of a render's cost for rows that end up under a caption bar.
        """
        ss, (vx, vy, vw, vh) = DONUT_SS, self.vp
        lay = Image.new("RGBA", (vw * ss, vh * ss), (0, 0, 0, 0))
        ld = ImageDraw.Draw(lay)
        s, cx, cy = cam
        ox = (vw / 2 - cx * s) * ss
        oy = (vh / 2 - cy * s) * ss
        for i, d in enumerate(self.items):
            lit = self._lit(i, focus)
            pts = self._wedge(i, sweep, POP * lit)
            if pts is None:
                continue
            col = tuple(int(lerp(d["color"][c], self.th["bg"][c],
                                 self.dim * (1.0 - lit))) for c in range(3))
            ld.polygon([(ox + x * s * ss, oy + y * s * ss) for x, y in pts],
                       fill=(*col, 255))
        # `reduce` is a box average over exactly `ss` by `ss` pixels, which is
        # what supersampling wants and what a resampling filter only
        # approximates -- and it is several times quicker, which at a frame per
        # sixtieth of a second is the difference between a render and a wait.
        lay = lay.reduce(ss)
        cv.paste(lay, (vx, vy), lay)

    def _hole(self, d, alpha: float) -> None:
        """what the ring is a breakdown *of*, in the middle of it"""
        if alpha < 0.02 or not self.total_txt:
            return
        s, (x, y) = self.scale, self.centre
        a = int(255 * alpha)
        d.text((x, y - 0.06 * s), self.total_txt, font=self._font(0.23 * s),
               fill=fade_c(self.th["fg"], a), anchor="mm")
        if self.total_label:
            d.text((x, y + 0.14 * s), self.total_label,
                   font=self._font(0.095 * s),
                   fill=fade_c(self.th["muted"], a), anchor="mm")

    def _labels(self, d, alpha: float) -> None:
        """leader, name and number, for every section at once.

        Drawn where `_layout_labels` put them, which is a fixed place on the
        canvas -- the camera is on the whole figure whenever any of this is
        visible, so there is nothing to project through and nothing to move.
        """
        if alpha < 0.02:
            return
        a, k = int(255 * alpha), self.uk
        cx, cy = self.centre
        for i, item in enumerate(self.items):
            L = self.labels[i]
            pts = [(cx + L[n][0], cy + L[n][1])
                   for n in ("anchor", "elbow", "stub", "text")]
            d.line(pts, fill=fade_c(item["color"], int(a * 0.75)),
                   width=max(1, int(2 * k)))
            tx, ty = pts[-1]
            anc = "lm" if L["side"] > 0 else "rm"
            pad = 10 * k * L["side"]
            d.text((tx + pad, ty - 15 * k), item["label"], font=self.f_lab,
                   fill=fade_c(self.th["fg"], a), anchor=anc)
            d.text((tx + pad, ty + 16 * k), self._sub_label(item),
                   font=self.f_val, fill=fade_c(item["color"], int(a * 0.92)),
                   anchor=anc)

    def _establish(self, d, head: str, sub: str, alpha: float) -> None:
        """the clip's opening and closing words, under the ring.

        Centred, and as close under the figure as the labels leave room for.
        Off in a bar along the bottom they are a second place to look at the
        moment the reader is looking at the first.
        """
        if alpha < 0.02 or not (head or sub):
            return
        a, k = int(255 * alpha), self.uk
        x, y = self.vp[0] + self.vp[2] / 2, self.estab_y
        d.text((x, y + 26 * k), head, font=self.f_cap,
               fill=fade_c(self.th["fg"], a), anchor="mm")
        d.text((x, y + 76 * k), sub, font=self.f_sub,
               fill=fade_c(self.th["muted"], a), anchor="mm")

    @staticmethod
    def _wrap(text: str, font, width: float) -> list[str]:
        """`text` broken into lines no wider than `width`"""
        out, line = [], ""
        for word in text.split():
            trial = f"{line} {word}".strip()
            if line and font.getlength(trial) > width:
                out.append(line)
                line = word
            else:
                line = trial
        return out + ([line] if line else [])

    def _build_cards(self) -> None:
        """each section's whole annotation, drawn once.

        Name, number, share and the line that says what the thing *is*, all on
        the card, because the card is where the reader is already looking: the
        camera has just spent half a second putting this section in the middle
        of the frame. A description parked along the bottom of the frame is
        read after the picture has already been understood, if at all.

        A fixed size in pixels, like every other bit of type here, so it is
        the same object at every beat and can be measured and drawn once. What
        varies frame to frame is only how much of it is showing.
        """
        k = self.uk
        pad = int(26 * k)
        self.cards: dict[int, dict] = {}
        for i, item in enumerate(self.items):
            lines = [(item["label"], self.f_card, self.th["fg"], 0),
                     (item["display"], self.f_big, item["color"], int(13 * k)),
                     (f'{_fmt_share(item["share"])} of {self.total_txt}',
                      self.f_pct, self.th["muted"], int(10 * k))]
            for j, ln in enumerate(self._wrap(item["note"], self.f_note,
                                              CARD_TEXT * k)):
                lines.append((ln, self.f_note, self.note_c,
                              int((24 if j == 0 else 9) * k)))
            # Sized on the ink, not on the font: a card set from the ascent of
            # four faces has a band of nothing under every line, and five lines
            # of nothing is how a readout becomes a poster.
            ink = [f.getbbox(t) for t, f, _, _ in lines]
            w = (int(max(f.getlength(t) for t, f, _, _ in lines))
                 + 2 * pad + int(10 * k))
            h = (sum(b[3] - b[1] for b in ink)
                 + sum(g for *_, g in lines) + 2 * pad)

            img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            cd = ImageDraw.Draw(img)
            cd.rounded_rectangle([0, 0, w - 1, h - 1], radius=int(14 * k),
                                 fill=(*self.th["caption_bg"], 240),
                                 outline=(*item["color"], 128),
                                 width=max(1, int(1 * k)))
            cd.rectangle([0, int(14 * k), max(2, int(5 * k)), h - int(14 * k)],
                         fill=(*item["color"], 255))
            ty = pad
            for (txt, font, col, above), b in zip(lines, ink):
                # `text` puts the ascender top at `ty`, and the ascent of a
                # face is mostly headroom for accents nothing here uses.
                # Backing off by where the ink actually starts is what makes
                # the height above the height the card is.
                ty += above
                cd.text((pad + int(10 * k), ty - b[1]), txt, font=font,
                        fill=(*col, 255))
                ty += b[3] - b[1]
            self.cards[i] = {"w": w, "h": h, "img": img}

    def _card(self, cv, d, i: int, alpha: float) -> None:
        """the card, where `_section_view` decided it goes, and its leader"""
        if alpha < 0.02:
            return
        card = self.cards[i]
        cam, (px, py) = self.section_view[i]
        w, h = card["w"], card["h"]
        x, y = int(px - w / 2), int(py - h / 2)

        ax, ay = self._nearest([self._pt(cam, *q)
                                for q in self._wedge(i, pop=POP)], px, py)
        edge = x + w if x + w < ax else (x if x > ax else ax)
        d.line([(ax, ay), (edge, max(y + int(20 * self.uk),
                                     min(y + h - int(20 * self.uk), ay)))],
               fill=fade_c(self.items[i]["color"], int(255 * alpha * 0.8)),
               width=max(1, int(2 * self.uk)))

        img = card["img"]
        if alpha < 0.995:
            img = img.copy()
            img.putalpha(img.getchannel("A").point(
                lambda v: int(v * alpha)))
        cv.paste(img, (x, y), img)

    # -- main ---------------------------------------------------------------
    def run(self) -> int:
        os.makedirs(self.outdir, exist_ok=True)
        for f in os.listdir(self.outdir):
            os.remove(os.path.join(self.outdir, f))
        nf = int(round(self.total() * self.fps))
        for n in range(nf):
            post_fx(self, self.frame(n), n, nf).save(f"{self.outdir}/f{n:05d}.png")
        return nf

    def frame(self, n: int) -> Image.Image:
        s, u, prev = self.at(n / self.fps)
        p = ease(u)
        cam = self._cam_lerp(s["cam0"], s["cam1"], p, s["arc"])
        sweep = lerp(s["sweep0"], s["sweep1"], p)
        cut = self.cut_alpha(s, u, s is self.timeline[-1])
        lab = cut if s["labels"] else 0.0

        # The dim follows the camera, so it arrives with the section rather
        # than snapping on ahead of it; on the way out it releases the same way.
        focus = []
        if s["focus"] is not None:
            if s["kind"] == "move":
                if prev and prev["focus"] is not None:
                    focus.append((prev["focus"], 1.0 - p))
                focus.append((s["focus"], p))
            else:
                focus.append((s["focus"], 1.0))
        elif prev and prev["focus"] is not None:
            focus.append((prev["focus"], 1.0 - p))

        cv = Image.new("RGB", (self.W, self.H), self.th["bg"])
        d = ImageDraw.Draw(cv)
        self._ring(cv, cam, sweep, focus)
        self._hole(d, lab)
        self._labels(d, lab)
        self._establish(d, s["estab"][0], s["estab"][1], lab)
        # The card belongs to the beat that holds on its section, and the
        # camera is still for the whole of that beat. Shown during a travel it
        # would have to be drawn against a camera it was not placed for, and
        # would slide across the frame to catch up.
        if s["kind"] == "hold":
            self._card(cv, d, s["focus"], cut)

        # The word along the top takes the colour of whichever section the
        # frame is mostly about, so the swap lands with the section rather than
        # a travel ahead of it.
        accent = (self.items[max(focus, key=lambda f: f[1])[0]]["color"]
                  if focus else self.th["after"])
        self.header(d, accent)
        return cv


# ---------------------------------------------------------------------------
# donut, printed: the same clip drawn into a terminal
# ---------------------------------------------------------------------------

# This repo is about terminals, and a donut drawn with polygons is the one
# shape in it that does not look like one. `render.donut.style: "ansi"` prints
# it instead: the ring is reduced to a grid of characters, the card becomes a
# box-drawn panel, the header becomes a status line in reverse video.
#
# It is a subclass rather than a second renderer, and that is the whole design.
# The storyboard, the camera solve, the card placement and the label layout are
# the ones above; only the drawing is replaced. A style that changes what the
# clip *says* would be a different feature.

# Where the advance of DejaVu Sans Mono sits, as a fraction of its point size.
# The cell is the unit here, so the type is sized from the cell rather than the
# other way round, and this is the number that inverts it.
MONO_ADVANCE = 0.6023

ANSI_CELL = 14              # the type cell's width; its height is twice that
ANSI_FOCUS = (36, 8)        # the chart's cell at either end of the rack focus
ANSI_FOCUS_TIME = (0.42, 0.28)
ANSI_SHARP_AT = 0.70        # past this the grid dissolves into the real thing
ANSI_SCANLINE = 0.88        # how far every second row is taken down
ANSI_BLOOM = 0.40           # how much blurred light the lit section adds back
ANSI_GLOW = 9               # how far the phosphor spreads
ANSI_PANEL_A = 232          # how opaque a card's panel is over the ring
ANSI_NOTE_COLS = 30         # a card's description, wrapped in columns

# Error is never pushed into a cell the ring does not touch at all. Without
# that, diffusion walks ink out past the edge of the arc and prints it in the
# dark, on its own.
ANSI_INK = 0.004

# How finely a character and the cell it stands for are compared. Not one
# number but a grid, and that is the point: ranking an alphabet by how much of
# a cell each character inks and picking by darkness alone is the classic
# trick, and it is exactly right for ` ░▒▓█`, where every character is uniform
# and only the amount differs. It throws away the whole reason to use ASCII.
# `'` and `_` ink about the same share of a cell in completely different parts
# of it; `/` and `\` are one glyph mirrored. Matching the grid rather than its
# mean is what lets an edge running down-and-left pick `/`, a flat bottom pick
# `_`, and a top corner pick `'`.
NX, NY = 4, 6

# `charset` names one of these, or is the characters themselves -- a set is
# only ever a string, since each character is measured off the font at the size
# it will be printed rather than assumed from its name. Every glyph below was
# checked against DejaVu Sans Mono, which has all of them and has neither
# braille nor the Unicode 13 sextants.
CHARSETS = {
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


class AnsiDonutRenderer(DonutRenderer):
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
    LABEL_LH = 2 * ANSI_CELL * 2 + 14

    def __init__(self, spec, outdir):
        dn = spec["render"]["donut"]
        # The cells and the alphabet have to exist before `super().__init__`
        # runs, because it calls `_fit` and `_build_cards` and both of those
        # are overridden here and measure in cells.
        uk = spec["render"].get("size", [1080, 1080])[1] / 1080
        self.cell = int(dn.get("cell", ANSI_CELL))
        self.tcw, self.tch = self.cell * uk, self.cell * 2 * uk
        px = int(self.cell / MONO_ADVANCE * uk)   # advance of exactly one cell
        self.ftype = ImageFont.truetype(MONO, px)
        self.fbig = ImageFont.truetype(MONO, px * 2)
        name = dn.get("charset", "unicode")
        # A named set, or the characters themselves -- which is the whole of
        # what a set is here, since every one of them is measured off the font
        # rather than assumed.
        self.ramp = CHARSETS.get(name, name)
        if len(self.ramp) < 2:
            raise SystemExit(
                f"render.donut.charset {name!r} is not one of "
                f"{sorted(CHARSETS)} and is not a string of characters")
        self.coarse, self.fine = (int(v) for v in dn.get("focus", ANSI_FOCUS))
        if not self.coarse > self.fine >= 2:
            raise SystemExit(
                "render.donut.focus is [coarse, fine] in pixels, coarsest "
                f"first and both at least 2; got {dn.get('focus')}")
        self.focus_in, self.focus_out = (
            float(v) for v in dn.get("focus_time", ANSI_FOCUS_TIME))
        self.sharp_at = float(dn.get("sharp_at", ANSI_SHARP_AT))
        self.scanline = float(dn.get("scanline", ANSI_SCANLINE))
        self.bloom = float(dn.get("bloom", ANSI_BLOOM))
        self.panel_a = int(dn.get("panel_alpha", ANSI_PANEL_A))
        self._tiles: dict = {}
        self._masks: dict = {}
        self._match: dict = {}
        self._fonts: dict = {}
        self._scan = None
        self._cv = None
        self._sharp = 1.0
        super().__init__(spec, outdir)
        self.cols = int(self.W / self.tcw) + 1
        self.rows = int(self.H / self.tch) + 1
        self._ends = [d["a1"] for d in self.items]

    def _font_for(self, cell: int):
        if cell not in self._fonts:
            self._fonts[cell] = ImageFont.truetype(
                MONO, max(4, int(cell / 0.6023)))
        return self._fonts[cell]

    def _masks_for(self, cell: int):
        """each character of the set as an NX-by-NY grid of its own ink.

        Measured off the rendered glyph rather than assumed from its name: a
        font's idea of where its medium shade puts ink, and of how far down a
        comma sits, is the only one that matters -- it is the one that will be
        on the screen. And it is measured per cell size, because a font hints
        its glyphs differently small than large, and the focus runs through a
        dozen sizes.
        """
        if cell not in self._masks:
            out, font = [], self._font_for(cell)
            for g in self.ramp:
                im = Image.new("L", (cell, cell * 2), 0)
                if g != " ":
                    ImageDraw.Draw(im).text((0, 0), g, font=font, fill=255)
                grid = [v / 255.0
                        for v in im.resize((NX, NY), Image.BOX).tobytes()]
                out.append((g, grid, sum(grid) / len(grid)))
            self._masks[cell] = out
        return self._masks[cell]

    def _pick(self, cell: int, want: list, key):
        """-> (character, how much of a cell it inks) for this coverage grid.

        Cached on a coarsely quantised grid. There are more patterns in
        principle than anyone could enumerate; on a real frame there are a few
        thousand, because every cell that is neither empty nor full is an edge
        and edges repeat -- and the cache outlives the frame, so the second one
        is nearly all hits.
        """
        key = (cell, key)
        if key not in self._match:
            best, pick = None, (" ", 0.0)
            for g, m, dens in self._masks_for(cell):
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
        ss, (_, _, vw, vh) = DONUT_SS, self.vp
        lay = Image.new("RGBA", (vw * ss, vh * ss), (0, 0, 0, 0))
        ld = ImageDraw.Draw(lay)
        s, cx, cy = cam
        ox, oy = (vw / 2 - cx * s) * ss, (vh / 2 - cy * s) * ss
        for i, d in enumerate(self.items):
            lit = self._lit(i, focus)
            pts = self._wedge(i, sweep, POP * lit)
            if pts is None:
                continue
            col = tuple(int(lerp(d["color"][k], self.th["bg"][k],
                                        self.dim * (1.0 - lit)))
                        for k in range(3))
            ld.polygon([(ox + x * s * ss, oy + y * s * ss) for x, y in pts],
                       fill=(*col, 255))
        return lay.reduce(ss)

    def _lit_mask(self, cam, sweep, focus):
        """where the section being read is, for the phosphor to glow through.

        Drawn from the wedges rather than collected from the cells, because
        there are no cells once the focus has pulled all the way in.
        """
        m = Image.new("L", (self.W, self.H), 0)
        if not focus:
            return None
        md = ImageDraw.Draw(m)
        s, cx, cy = cam
        ox = self.vp[0] + self.vp[2] / 2 - cx * s
        oy = self.vp[1] + self.vp[3] / 2 - cy * s
        hit = False
        for i, d in enumerate(self.items):
            lit = self._lit(i, focus)
            if lit <= 0.5:
                continue
            pts = self._wedge(i, sweep, POP * lit)
            if pts is None:
                continue
            md.polygon([(ox + x * s, oy + y * s) for x, y in pts], fill=255)
            hit = True
        return m if hit else None

    def cell_at(self, f: float) -> int:
        """the grid the focus is currently on.

        Geometric, not linear: the eye reads a grid by how many cells it has,
        and halving the cell doubles that. Interpolated linearly, the whole
        interesting part of the pull happens in the last fifth of it.
        """
        g = min(1.0, f / self.sharp_at)
        return max(2, int(round(math.exp(lerp(math.log(self.coarse),
                                                     math.log(self.fine), g)))))

    def _ring(self, cv, cam, sweep, focus) -> None:
        # `_ring` runs first, so this is where the canvas for the whole frame
        # is picked up; the base class hands the others an ImageDraw only.
        self._cv = cv
        f = self._sharp
        vx, vy = self.vp[0], self.vp[1]
        lay = self._smooth(cam, sweep, focus)
        # Past `self.sharp_at` the grid stops getting finer and dissolves into the
        # picture it was standing for instead. Running the cell all the way
        # down to one pixel would get there on its own and cost a hundred
        # times as much to do it, for a difference nobody can see.
        mix = max(0.0, min(1.0, (f - self.sharp_at) / (1.0 - self.sharp_at)))
        if mix >= 0.999:
            cv.paste(lay, (vx, vy), lay)
            return

        cell = self.cell_at(f)
        cw, ch = cell * self.uk, cell * 2 * self.uk
        cols, rows = int(self.W / cw) + 1, int(self.H / ch) + 1
        gw, gh = int(cols * cw), int(rows * ch)
        full = Image.new("RGBA", (gw, gh), (0, 0, 0, 0))
        full.paste(lay, (vx, vy))
        rgb = full.resize((cols, rows), Image.BOX).convert("RGB").tobytes()
        # ... and again at sub-cell resolution, which is what says *where* in
        # each cell the ink goes. One resize in C rather than a sampling loop
        # in Python -- and it is a reduction of a correct picture, so unlike
        # the geometry sampling it replaced it cannot misread a boundary.
        sw = cols * NX
        sub = full.resize((sw, rows * NY),
                          Image.BOX).getchannel("A").tobytes()

        # Floyd-Steinberg, one row of error ahead of the cursor and one below.
        here = [0.0] * (cols + 2)
        below = [0.0] * (cols + 2)
        grid = {}
        r0 = int(self.tch / ch) + 1             # clear of the status line
        pal = [(tuple(int(lerp(d["color"][k], self.th["bg"][k],
                                      self.dim * (1.0 - self._lit(i, focus))))
                      for k in range(3)),)
               for i, d in enumerate(self.items)]
        snap: dict = {}
        for r in range(r0, rows):
            here, below = below, [0.0] * (cols + 2)
            for c in range(cols):
                k = r * cols + c
                block = []
                for y in range(NY):
                    row = (r * NY + y) * sw + c * NX
                    block += sub[row:row + NX]
                if sum(block) / (255.0 * NX * NY) < ANSI_INK:
                    here[c + 1] = 0.0           # nothing here: drop the error
                    continue
                # the diffused error is a debt on the whole cell, so every
                # sub-cell carries it alike -- it says "this cell owes the
                # picture a little more ink", not "the shape was different"
                d = here[c + 1]
                want = [min(1.0, max(0.0, v / 255.0 + d)) for v in block]
                ch_, dens = self._pick(cell, want,
                                       tuple(int(v * 3.99) for v in want))
                e = sum(want) / len(want) - dens
                here[c + 2] += e * 7 / 16
                below[c] += e * 3 / 16
                below[c + 1] += e * 5 / 16
                below[c + 2] += e * 1 / 16
                if ch_ == " ":
                    continue
                # Which of the ring's colours is this? Not "divide the
                # premultiplied colour by the coverage and take the nearest" --
                # that divides by a small number exactly where the rounding on
                # it is worst, and a faint edge cell comes back some hue the
                # chart does not contain. The *direction* of a premultiplied
                # colour is the ink's direction whatever the coverage.
                raw = (rgb[3 * k], rgb[3 * k + 1], rgb[3 * k + 2])
                ckey = (raw[0] >> 2, raw[1] >> 2, raw[2] >> 2)
                if ckey not in snap:
                    snap[ckey] = max(pal, key=lambda p: (
                        sum(p[0][j] * raw[j] for j in range(3)) ** 2
                        / max(1, sum(v * v for v in p[0]))))[0]
                grid[(c, r)] = (ch_, snap[ckey], None)

        printed = Image.new("RGB", cv.size, self.th["bg"])
        self._cv = printed
        self._print(grid, self._font_for(cell), cw, ch)
        self._cv = cv
        if mix > 0.002:
            true = Image.new("RGB", cv.size, self.th["bg"])
            true.paste(lay, (vx, vy), lay)
            printed = Image.blend(printed, true, mix)
        cv.paste(printed, (0, 0))

    # -- the card, measured in cells so the camera solves for the real one ---
    def _build_cards(self) -> None:
        self.cards: dict[int, dict] = {}
        for i, item in enumerate(self.items):
            share = f'{_fmt_share(item["share"])} of {self.total_txt}'
            note = self._wrap_cols(item["note"], ANSI_NOTE_COLS)
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
        col, bg = item["color"], (*self.th["caption_bg"], self.panel_a)
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
                                for q in self._wedge(i, pop=POP)],
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
                d.point((0, y), fill=(int(255 * self.scanline),) * 3)
            self._scan = im.resize((self.W, self.H), Image.NEAREST)
        return self._scan

    def focus_at(self, seg: dict, u: float, last: bool) -> float:
        """how far into focus the ring is, on this segment's own clock.

        Only the beats the camera holds still for come into focus at all, and
        they let go of it before it moves again. So the ring is sharp exactly
        when it is being looked at and coarse exactly when it is travelling --
        which is the right way round for a character grid, because a grid that
        moves is a grid that crawls.
        """
        if seg["kind"] not in ("intro", "hold", "outro"):
            return 0.0
        t, d = u * seg["dur"], seg["dur"]
        i, o = min(self.focus_in, d / 2), min(self.focus_out, d / 2)
        a = min(1.0, t / i) if i > 0 else 1.0
        if not last and o > 0:
            a = min(a, (d - t) / o)
        return ease(max(0.0, min(1.0, a)))

    def frame(self, n: int) -> Image.Image:
        s, u, prev = self.at(n / self.fps)
        self._sharp = self.focus_at(s, u, s is self.timeline[-1])
        p = ease(u)
        cam = self._cam_lerp(s["cam0"], s["cam1"], p, s["arc"])
        sweep = lerp(s["sweep0"], s["sweep1"], p)
        foc = []
        if s["focus"] is not None:
            foc = [(s["focus"], 1.0)] if s["kind"] != "move" else []
        cv = super().frame(n)

        mask = self._lit_mask(cam, sweep, foc) if self.bloom > 0 else None
        if mask is not None:
            lit = Image.new("RGB", cv.size, (0, 0, 0))
            lit.paste(cv, mask=mask)
            glow = lit.filter(ImageFilter.GaussianBlur(int(ANSI_GLOW * self.uk)))
            cv = ImageChops.add(cv, ImageChops.multiply(
                glow, Image.new("RGB", cv.size, (int(255 * self.bloom),) * 3)))
        return ImageChops.multiply(cv, self._scanlines())
