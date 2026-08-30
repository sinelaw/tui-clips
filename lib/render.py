"""Compose a TUI feature clip into a PNG frame sequence.

Two shapes, chosen by how many panes the spec captures:

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

Annotation bands are given in terminal cells (rows and columns), which is what
`tui-grid` prints, so the numbers survive a font or geometry change.
"""
from __future__ import annotations

import os
from PIL import Image, ImageDraw, ImageFont

BOLD = "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"
MONO = "/usr/share/fonts/TTF/JetBrainsMono-Regular.ttf"

DEFAULT_THEME = {
    "bg": [10, 10, 12],
    "caption_bg": [17, 17, 22],
    "panel_border": [44, 44, 52],
    "muted": [139, 139, 150],
    "fg": [232, 232, 238],
    "before": [255, 107, 94],
    "after": [74, 222, 128],
}
DEFAULT_TIMING = {"intro": 2.1, "zoom": 0.8, "hold": 1.8, "pan": 0.4, "outro": 1.0}
DEFAULT_LABELS = {"before": "BEFORE", "after": "AFTER", "solo": "NEW"}


def ease(t: float) -> float:
    """easeInOutCubic"""
    t = max(0.0, min(1.0, t))
    return 4 * t * t * t if t < 0.5 else 1 - pow(-2 * t + 2, 3) / 2


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def fade(c, a: int):
    return tuple(int(v * a / 255) for v in c)


def mode_of(spec: dict) -> str:
    """'compare' when the spec captures both a before and an after, else 'solo'"""
    panes = spec["capture"]["panes"]
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


class Renderer:
    def __init__(self, spec: dict, captures: dict[str, str], outdir: str):
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

        self.rows = int(r["rows"])
        self.cols = int(r["cols"])
        self.RH = self.IH / self.rows
        self.CW = self.IW / self.cols

        self.W, self.H = r.get("size", [1080, 1080])
        self.fps = int(r.get("fps", 60))
        self.header_h = int(r.get("header_height", 100))
        self.cap_h = int(r.get("caption_height", 130))
        self.cap_y = self.H - self.cap_h
        self.vp = (0, self.header_h, self.W, self.cap_y - self.header_h)

        th = dict(DEFAULT_THEME)
        th.update(r.get("theme", {}))
        self.th = {k: tuple(v) for k, v in th.items()}

        self.t = dict(DEFAULT_TIMING)
        given = dict(r.get("timing", {}))
        if "swipe" in given and "zoom" not in given:   # pre-solo spelling
            given["zoom"] = given.pop("swipe")
        self.t.update(given)

        self.ann = r["annotations"]
        if not self.ann:
            raise SystemExit("render.annotations is empty; nothing to show")
        self.cap_intro = r.get("intro_caption", ["", ""])
        self.cap_outro = r.get("outro_caption", ["", ""])
        lb = dict(DEFAULT_LABELS)
        lb.update(r.get("labels", {}))
        self.labels = lb
        self.title = r.get("title", "")
        # the label that rides into the header once zoomed in
        self.lead = self.labels["after"] if self.mode == "compare" else self.labels["solo"]
        self.accent_intro = self.th["before"] if self.mode == "compare" else self.th["after"]

        # intro panel geometry, derived so it always fits the canvas
        label_h, gutter, margin = 44, 24, 16
        self.panel_y = self.header_h + label_h
        avail_h = self.cap_y - self.panel_y - 6
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
            self.ax = (self.W - self.panel_w) / 2
        self.s_c = self.vp[2] / self.IW

        self.f_label = ImageFont.truetype(BOLD, 40)
        self.f_meta = ImageFont.truetype(MONO, 24)
        self.f_hdr = ImageFont.truetype(BOLD, 38)
        self.f_cap = ImageFont.truetype(BOLD, 40)
        self.f_sub = ImageFont.truetype(MONO, 23)

        self._cache: dict = {}

    # -- geometry helpers ---------------------------------------------------
    def total(self) -> float:
        n = len(self.ann)
        return (self.t["intro"] + self.t["zoom"] + self.t["hold"] * n
                + self.t["pan"] * max(0, n - 1) + self.t["outro"])

    def ann_cy(self, i: int) -> float:
        """source-y that centres annotation i, clamped inside the image"""
        a = self.ann[i]
        half = self.vp[3] / self.s_c / 2
        cy = (a["rows"][0] + a["rows"][1]) / 2 * self.RH
        if self.IH <= 2 * half:          # capture shorter than the viewport
            return self.IH / 2
        return max(half, min(self.IH - half, cy))

    def band(self, i: int, py: float, s: float):
        a = self.ann[i]
        r0, r1 = a["rows"]
        c0, c1 = a.get("cols", [0, self.cols])
        return (c0 * self.CW * s - 6, py + r0 * self.RH * s - 6,
                c1 * self.CW * s + 6, py + r1 * self.RH * s + 6)

    def scaled(self, img, s: float, tag: str):
        key = (tag, round(s, 4))
        if key not in self._cache:
            if len(self._cache) > 6:
                self._cache.clear()
            self._cache[key] = img.resize(
                (int(round(self.IW * s)), int(round(self.IH * s))), Image.LANCZOS)
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
        """-> (zoom progress p, annotation index, pan progress, outro progress)"""
        T = self.t
        if t < T["intro"]:
            return 0.0, 0, 0.0, 0.0
        if T["zoom"] > 0 and t < T["intro"] + T["zoom"]:
            return ease((t - T["intro"]) / T["zoom"]), 0, 0.0, 0.0
        u = t - T["intro"] - T["zoom"]
        last = len(self.ann) - 1
        for i in range(len(self.ann)):
            seg = T["hold"] + (T["pan"] if i < last else 0)
            if u < seg:
                pan = (0.0 if u < T["hold"] or T["pan"] <= 0
                       else ease((u - T["hold"]) / T["pan"]))
                return 1.0, i, pan, 0.0
            u -= seg
        return 1.0, last, 0.0, ease(u / T["outro"]) if T["outro"] > 0 else 1.0

    def frame(self, n: int) -> Image.Image:
        th, W, H = self.th, self.W, self.H
        t = n / self.fps
        p, ai, pan, rel = self.phase(t)
        nxt = min(ai + 1, len(self.ann) - 1)

        cv = Image.new("RGB", (W, H), th["bg"])
        d = ImageDraw.Draw(cv)

        s = lerp(self.s_a, self.s_c, p)
        cw = lerp(self.panel_w, self.vp[2], p)
        ch = lerp(self.panel_h, self.vp[3], p)
        cx = lerp(self.ax + self.panel_w / 2, self.vp[0] + self.vp[2] / 2, p)
        cy = lerp(self.panel_y + self.panel_h / 2, self.vp[1] + self.vp[3] / 2, p)
        src_cy = lerp(self.IH / 2,
                      lerp(self.ann_cy(ai), self.ann_cy(nxt), pan), p)

        # BEFORE panel, sliding out to the left (comparison clips only)
        if self.B is not None and p < 1.0:
            bx = int(round(lerp(self.bx, -self.panel_w - 100, p)))
            pw, ph = int(round(self.panel_w)), int(round(self.panel_h))
            lay = Image.new("RGB", (pw, ph), th["bg"])
            lay.paste(self.scaled(self.B, self.s_a, "B"),
                      (int(round(pw / 2 - self.IW / 2 * self.s_a)),
                       int(round(ph / 2 - self.IH / 2 * self.s_a))))
            cv.paste(lay, (bx, int(self.panel_y)))
            d.rectangle([bx, self.panel_y, bx + pw - 1, self.panel_y + ph - 1],
                        outline=th["panel_border"], width=2)

        # subject panel, growing from the intro shot to the full viewport
        cwi, chi = int(round(cw)), int(round(ch))
        lay = Image.new("RGB", (cwi, chi), th["bg"])
        px = int(round(cwi / 2 - self.IW / 2 * s))
        py = int(round(chi / 2 - src_cy * s))
        lay.paste(self.scaled(self.A, s, "A"), (px, py))

        dim = int(150 * min(1.0, max(0.0, (p - 0.55) / 0.45)) * (1.0 - rel))
        if dim > 3:
            b = self.band(ai, py, s)
            if pan > 0:
                b2 = self.band(nxt, py, s)
                b = tuple(lerp(b[k], b2[k], pan) for k in range(4))
            ov = Image.new("RGBA", lay.size, (0, 0, 0, dim))
            ImageDraw.Draw(ov).rounded_rectangle(list(b), radius=10, fill=(0, 0, 0, 0))
            lay = lay.convert("RGBA")
            lay.alpha_composite(ov)
            lay = lay.convert("RGB")
            ld = ImageDraw.Draw(lay)
            g = fade(th["after"], int(255 * (1.0 - rel)))
            ld.rounded_rectangle(list(b), radius=10, outline=g, width=3)
            ld.rectangle([b[0] - 12, b[1], b[0] - 7, b[3]], fill=g)

        pos = (int(round(cx - cw / 2)), int(round(cy - ch / 2)))
        cv.paste(lay, pos)
        if p < 0.9:
            d.rectangle([pos[0], pos[1], pos[0] + cwi - 1, pos[1] + chi - 1],
                        outline=th["panel_border"], width=2)

        # header: intro labels cross-fade into a single banner
        d.rectangle([0, 0, W, self.panel_y - 44 if p < 0.5 else self.vp[1]],
                    fill=th["bg"])
        a_in = int(255 * max(0.0, min(1.0, (p - 0.45) / 0.40)))
        a_out = 255 - a_in
        if a_out > 4:
            d.text((W / 2, 40), self.title, font=self.f_meta,
                   fill=fade(th["muted"], a_out), anchor="mm")
            if self.B is not None:
                d.text((self.bx + self.panel_w / 2, 108), self.labels["before"],
                       font=self.f_label, fill=fade(th["before"], a_out), anchor="mm")
            if self.lead:
                d.text((self.ax + self.panel_w / 2, 108), self.lead,
                       font=self.f_label, fill=fade(th["after"], a_out), anchor="mm")
        if a_in > 4:
            if self.lead:
                d.text((32, 50), self.lead, font=self.f_hdr,
                       fill=fade(th["after"], a_in), anchor="lm")
                d.text((W - 32, 52), self.title, font=self.f_meta,
                       fill=fade(th["muted"], a_in), anchor="rm")
            else:
                d.text((32, 50), self.title, font=self.f_meta,
                       fill=fade(th["muted"], a_in), anchor="lm")

        # caption bar
        d.rectangle([0, self.cap_y, W, H], fill=th["caption_bg"])
        accent = self.accent_intro if p < 0.5 else th["after"]
        d.rectangle([0, self.cap_y, W, self.cap_y + 3], fill=fade(accent, 200))

        head, sub, ca = self.cap_intro[0], self.cap_intro[1], 255
        if 0.0 < p < 1.0:
            if p < 0.5:
                ca = int(255 * (1 - p / 0.5))
            else:
                ca = int(255 * ((p - 0.5) / 0.5))
                head, sub = self.ann[0]["head"], self.ann[0]["sub"]
        elif p >= 1.0:
            head, sub = self.ann[ai]["head"], self.ann[ai]["sub"]
            if pan > 0:
                if pan < 0.5:
                    ca = int(255 * (1 - pan / 0.5))
                else:
                    ca = int(255 * ((pan - 0.5) / 0.5))
                    head, sub = self.ann[nxt]["head"], self.ann[nxt]["sub"]
            if rel > 0:
                if rel < 0.45:
                    ca = int(255 * (1 - rel / 0.45))
                else:
                    ca = int(255 * ((rel - 0.45) / 0.55))
                    head, sub = self.cap_outro

        d.rectangle([32, self.cap_y + 44, 40, self.cap_y + 84], fill=fade(accent, ca))
        d.text((60, self.cap_y + 50), head, font=self.f_cap,
               fill=fade(th["fg"], ca), anchor="lm")
        d.text((60, self.cap_y + 92), sub, font=self.f_sub,
               fill=fade(th["muted"], ca), anchor="lm")
        return cv
