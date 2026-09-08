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
                  "explode": "ANATOMY"}


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
    """'explode' when the spec says how to take the capture apart, else
    'compare' when it captures both a before and an after, else 'solo'"""
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
    if mode_of(spec) == "explode":
        return ExplodeRenderer(spec, captures, outdir)
    return Renderer(spec, captures, outdir, shots, runs)


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

        A push moves a whole screen the width of the frame, which wants longer
        than the cross-fade a pan is, so it takes its own timing.
        """
        if i + 1 < len(self.ann) and self.ann[i + 1].get("transition") == "push":
            return float(self.t["push"])
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
        pad = 2 * self.FIT_PAD
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
        sh = self.shot(i, hp)
        base = self.scaled(sh[0], s, sh[1])
        if len(sh) == 5:
            base = Image.blend(base, self.scaled(sh[2], s, sh[3]), sh[4])
        return base

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
            self.frame(n).save(f"{self.outdir}/f{n:05d}.png")
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
        if push:
            for k, hpk, off in ((ai, 1.0, -pan * cwi), (nxt, 0.0, (1 - pan) * cwi)):
                sk, sxk, syk = self.cam(k)
                lay.paste(self.screen(k, hpk, sk),
                          (int(round(cwi / 2 - sxk * sk + off)),
                           int(round(chi / 2 - syk * sk))))
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
        if self.cursor_cfg is not None and not push:
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
        vis = 0.0 if push else (min(1.0, max(0.0, (p - 0.55) / 0.45))
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


class ExplodeRenderer:
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
        self.header_h = int(r.get("header_height", 96))
        self.cap_h = int(r.get("caption_height", 132))
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

        k = self.H / 1080
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
            self.frame(n).save(f"{self.outdir}/f{n:05d}.png")
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

        # header and caption bars, painted over whatever ran under them
        d.rectangle([0, 0, W, self.header_h], fill=th["bg"])
        d.rectangle([0, self.cap_y, W, H], fill=th["caption_bg"])
        d.rectangle([0, self.cap_y, W, self.cap_y + 3], fill=fade_c(th["after"], 200))
        if self.lead:
            d.text((32, self.header_h / 2), self.lead, font=self.f_hdr,
                   fill=th["after"], anchor="lm")
            d.text((W - 32, self.header_h / 2 + 2), self.title, font=self.f_meta,
                   fill=th["muted"], anchor="rm")
        else:
            d.text((32, self.header_h / 2), self.title, font=self.f_meta,
                   fill=th["muted"], anchor="lm")

        head, sub, ca = s["head"], s["sub"], 255
        xf = min(0.38, s["dur"] / 2) / max(s["dur"], 1e-6)
        if prev and (prev["head"], prev["sub"]) != (head, sub) and u < xf:
            v = u / xf
            if v < 0.5:
                head, sub, ca = prev["head"], prev["sub"], int(255 * (1 - v / 0.5))
            else:
                ca = int(255 * ((v - 0.5) / 0.5))
        d.rectangle([32, self.cap_y + 44, 40, self.cap_y + 86],
                    fill=fade_c(th["after"], ca))
        d.text((60, ty + 50), head, font=self.f_cap,
               fill=fade_c(th["fg"], ca), anchor="lm")
        d.text((60, self.cap_y + 94), sub, font=self.f_sub,
               fill=fade_c(th["muted"], ca), anchor="lm")
        return cv
