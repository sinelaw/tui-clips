#!/usr/bin/env python3
"""Regression tests. Run them with `python3 tests/test_tui_clips.py`.

They cover the things that were found the hard way and would otherwise be
found the hard way again: a cache that returned an image a pixel off, a note
that vanished with the band it was never part of, and a trim that has to land
on the event rather than near it.
"""
from __future__ import annotations

import math
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
sys.path.insert(0, os.path.join(ROOT, "bin"))

from PIL import Image, ImageChops  # noqa: E402

import capture as cap  # noqa: E402
import render  # noqa: E402
import xwdfile  # noqa: E402

FAILED: list[str] = []


def _bbox_of(pts):
    return (min(p[0] for p in pts), min(p[1] for p in pts),
            max(p[0] for p in pts), max(p[1] for p in pts))


def check(cond, msg):
    if not cond:
        FAILED.append(msg)
        print(f"  FAIL {msg}")
    else:
        print(f"  ok   {msg}")


def screen(path, w=600, h=400, shift=0):
    """a capture-shaped image with something in it to diff"""
    im = Image.new("RGB", (w, h), (12, 12, 16))
    for y in range(0, h, 20):
        for x in range(0, w, 3):
            v = (x + y + shift * 40) % 255
            im.putpixel((x, y), (v, 200 - v // 2, 80))
    im.save(path)
    return path


def spec_for(tmp, **render_over):
    r = {"rows": 20, "cols": 60, "size": [1080, 1080], "fps": 30,
         "title": "t", "timing": {"intro": 0.4, "zoom": 0.3, "hold": 0.5,
                                  "pan": 0.2, "outro": 0.3},
         "annotations": [{"rect": [0, 0, 20, 4], "note": "one"}]}
    r.update(render_over)
    return {"name": "t",
            "capture": {"geometry": "60x20", "font": "x",
                        "panes": {"solo": {"argv": ["x"]}}},
            "render": r}


# ---------------------------------------------------------------------------

def test_scaled_cache_is_keyed_on_the_size_it_produced(tmp):
    """two scales that share a cache key must share a size.

    The old key was `(tag, round(s, 4))` while the size came off the raw `s`,
    so two scales a ten-thousandth apart could collide on the key and hold
    images a pixel apart. It surfaced far from here, as `ValueError: images do
    not match` on a blend, and only for geometries where a scaled height
    happened to land on .5 -- which is why it looked like a terminal-size bug.
    """
    png = screen(os.path.join(tmp, "a.png"), 1000, 1000)
    r = render.make(spec_for(tmp), {"solo": png}, os.path.join(tmp, "f"))
    s1, s2 = 0.33746, 0.33754           # both round to 0.3375
    check(round(s1, 4) == round(s2, 4), "the two scales do share a cache key")
    check(r.size_at(s1) == r.size_at(s2),
          f"same key -> same size ({r.size_at(s1)} == {r.size_at(s2)})")
    # Two *tags*, which is what made this bite: one entry is filled at s1 and
    # another at s2, they are cached independently, and the frame that blends
    # them is where it goes wrong. One tag alone would hit the cache and hide
    # the whole thing.
    a = r.scaled(r.A, s1, "A")
    b = r.scaled(r.A, s2, "shot:x")
    check(a.size == b.size,
          f"two tags a hair apart in scale agree on size ({a.size}, {b.size})")
    try:
        Image.blend(a, b, 0.5)
        check(True, "the two scaled images can be blended")
    except ValueError as e:
        check(False, f"blend raised {e}")

    # The bug itself, spelled out: the old key rounded the scale but sized off
    # the raw one, so these two disagreed by a pixel.
    def old_size(scale):
        return (int(round(r.IW * scale)), int(round(r.IH * scale)))
    check(old_size(s1) != old_size(s2),
          f"the old sizing really did differ here "
          f"({old_size(s1)} vs {old_size(s2)}) -- this test would have failed")

    # a scale whose height lands exactly on .5 must still be stable
    for s in (0.3375, 0.5005, 0.12345):
        check(r.size_at(s) == r.size_at(s + 1e-9),
              f"scale {s} is stable against a rounding wobble")


def test_a_note_survives_band_false(tmp):
    """a beat with `band: false` still says its piece.

    Notes used to be drawn inside the band's own `if`, so a beat that turned
    the band off -- which is what a beat whose point is pure motion does --
    lost its note with it, and the words had to go on the beats either side.
    """
    png = screen(os.path.join(tmp, "a.png"))
    anns = [{"rect": [2, 2, 30, 8], "note": "the words", "band": False},
            {"rect": [2, 2, 30, 8], "band": False}]
    r = render.make(spec_for(tmp, annotations=anns), {"solo": png},
                    os.path.join(tmp, "f"))
    # mid-way through beat 0's hold, well past the zoom
    n = int((r.t["intro"] + r.t["zoom"] + r.hold(0) * 0.5) * r.fps)
    with_note = r.frame(n)

    anns2 = [dict(anns[0]), dict(anns[1])]
    del anns2[0]["note"]
    r2 = render.make(spec_for(tmp, annotations=anns2), {"solo": png},
                     os.path.join(tmp, "f2"))
    without = r2.frame(n)
    d = ImageChops.difference(with_note, without).getbbox()
    check(d is not None, "the note is drawn even though band is false")

    # and the band itself is still off: no dimming outside the rect
    r3 = render.make(spec_for(tmp, annotations=[
        {"rect": [2, 2, 30, 8], "note": "the words", "band": True}]),
        {"solo": png}, os.path.join(tmp, "f3"))
    check(ImageChops.difference(r3.frame(n), with_note).getbbox() is not None,
          "band: true still differs from band: false")


def test_pick_anchor(tmp):
    d = [1, 1, 9, 1, 1, 1, 1]
    check(cap.pick_anchor(d, "max-diff") == 3,
          "max-diff lands on the frame the change arrives at")
    d2 = [1, 8, 1, 1, 9, 1]
    check(cap.pick_anchor(d2, "first-change") == 2, "first-change takes the first")
    check(cap.pick_anchor(d2, "last-change") == 5, "last-change takes the last")
    # a scroll: a run of middling changes should beat a lone spike
    d3 = [9, 0, 0, 4, 4, 4, 4, 4, 0, 0]
    check(cap.pick_anchor(d3, "sustained") > 3,
          "sustained prefers the run over the spike")


def test_trim_keeps_the_event(tmp):
    """the kept window holds the change, a third in"""
    d = os.path.join(tmp, "run")
    os.makedirs(d, exist_ok=True)
    run = cap.Run("r", d, 5, "png")
    for i in range(40):
        # everything is still until frame 25, which is a different screen
        p = os.path.join(d, f"{i:04d}.png")
        screen(p, 240, 120, shift=0 if i < 25 else 7)
        run.frames.append((float(i), p))
    run.keep, run.anchor = 12, "max-diff"
    cap.trim(run, cap.Grabber("0", ":0"))
    kept = [int(os.path.basename(p)[:4]) for _, p in run.frames]
    check(len(kept) == 12, f"kept 12 frames, got {len(kept)}")
    check(25 in kept, f"the event frame (25) is in the window {kept[0]}..{kept[-1]}")
    check(kept.index(25) == 4, f"the event sits a third in (index {kept.index(25)})")
    check(len(os.listdir(d)) == 12, "the discarded frames are gone from disk")


def test_pointer(tmp):
    """the path is eased across a gesture, and parked means gone"""
    pts = [{"t": 0.0, "x": 0, "y": 10, "down": False, "visible": True},
           {"t": 0.1, "x": 0, "y": 10, "down": True, "visible": True},
           {"t": 0.5, "x": 100, "y": 10, "down": True, "visible": True},
           {"t": 0.6, "x": 100, "y": 10, "down": False, "visible": True},
           {"t": 2.0, "x": 999, "y": 999, "down": False, "visible": False}]
    p = render.Pointer(pts)
    check(p.at(-1.0) is None, "no pointer before the first sample")
    check(p.at(2.5) is None, "no pointer once a step has parked it")
    mid = p.at(0.3)
    check(mid is not None and 0 < mid[0] < 100, "the pointer is mid-travel")
    # eased, not linear: at the midpoint of the gesture it is near the middle,
    # but a quarter in it has covered less than a quarter of the distance
    q = p.at(0.1 + 0.5 * 0.25)
    check(q[0] < 25, f"eased in rather than linear (x={q[0]:.1f} at 25%)")
    bump = p.at(0.12)
    check(bump[2] > 0, "the click bump is showing just after the press")
    check(p.at(0.55)[2] == 0, "the bump has settled by the end")


def donut_spec(**over):
    dn = {"unit": "MB", "items": [
        {"label": "Trees", "value": 148, "note": "one per buffer"},
        {"label": "Ropes", "value": 96, "note": "chunked"},
        {"label": "Rest", "value": 6, "note": "small"}]}
    dn.update(over.pop("donut", {}))
    r = {"size": [480, 480], "fps": 20, "title": "t",
         "intro_caption": ["i", "j"], "outro_caption": ["o", "p"],
         "timing": {"grow": .3, "intro": .3, "move": .2, "hold": .3,
                    "regroup": .2, "outro": .3},
         "donut": dn}
    r.update(over)
    return {"name": "d", "render": r}


def test_donut_ranks_and_fills_the_circle(tmp):
    """the reading order is the ranking, and the sections are a whole"""
    sp = donut_spec()
    sp["render"]["donut"]["items"].insert(0, {"label": "Tiny", "value": 2})
    r = render.make(sp, {}, os.path.join(tmp, "f"))
    check([d["label"] for d in r.items] == ["Trees", "Ropes", "Rest", "Tiny"],
          f'sorted by value: {[d["label"] for d in r.items]}')
    check(abs(sum(d["share"] for d in r.items) - 1.0) < 1e-9,
          "the shares sum to one")
    check(abs(r.items[-1]["a1"] - r.items[0]["a0"] - 360.0) < 1e-9,
          "the sections close the circle with no angle left over")
    # `sort: false` is how a breakdown that means something in its own order
    # -- a timeline, a call stack -- keeps it
    r2 = render.make(donut_spec(donut={"sort": False, "items": [
        {"label": "b", "value": 1}, {"label": "a", "value": 9}]}), {},
        os.path.join(tmp, "f2"))
    check([d["label"] for d in r2.items] == ["b", "a"], "sort: false is obeyed")


def test_donut_visits_every_section_once(tmp):
    """one move and one hold each, in rank order, between the establishing
    shots -- and `visit: false` keeps a section out of the walk without
    keeping it out of the ring"""
    sp = donut_spec()
    sp["render"]["donut"]["items"][2]["visit"] = False
    r = render.make(sp, {}, os.path.join(tmp, "f"))
    kinds = [s["kind"] for s in r.timeline]
    check(kinds == ["grow", "intro", "move", "hold", "move", "hold",
                    "regroup", "outro"], f"storyboard is {kinds}")
    check([s["focus"] for s in r.timeline if s["kind"] == "hold"] == [0, 1],
          "the un-visited section gets no beat")
    check(len(r.items) == 3, "... but it is still drawn")
    check(abs(r.total() - (.3 + .3 + 2 * (.2 + .3) + .2 + .3)) < 1e-9,
          f"duration is the sum of the beats ({r.total():.2f}s)")


def test_donut_hold_is_per_item(tmp):
    """a section with more to read gets longer to read it in.

    `timing.hold` is the clip's dwell; an item's own `hold` overrides it, which
    is what a three-line description on one section and two words on the next
    needs. Everything else about the beat is unchanged -- it is the dwell that
    varies, not the framing.
    """
    sp = donut_spec()
    sp["render"]["donut"]["items"][0]["hold"] = 5.0
    sp["render"]["donut"]["items"][2]["hold"] = 0.4
    r = render.make(sp, {}, os.path.join(tmp, "f"))
    holds = [(r.items[s["focus"]]["label"], s["dur"])
             for s in r.timeline if s["kind"] == "hold"]
    check(holds == [("Trees", 5.0), ("Ropes", r.t["hold"]), ("Rest", 0.4)],
          f"each item dwells for its own hold, else the clip's ({holds})")
    base = render.make(donut_spec(), {}, os.path.join(tmp, "f2"))
    check(abs(r.total() - (base.total() - 2 * r.t["hold"] + 5.4)) < 1e-9,
          f"and the clip is that much longer ({r.total():.2f}s vs "
          f"{base.total():.2f}s)")
    # the beat itself is the same beat: same camera, same card
    check(r.section_view[0][0] == base.section_view[0][0],
          "a longer hold does not re-frame the section")


def test_donut_camera_closes_in_and_comes_back(tmp):
    """a section is read closer than the whole, the smaller the closer"""
    r = render.make(donut_spec(), {}, os.path.join(tmp, "f"))
    whole = r.scale
    zooms = []
    for i in r.visit:
        s = r.section_view[i][0][0]
        zooms.append(s / whole)
        check(s >= whole - 1e-6,
              f'{r.items[i]["label"]} is read no further away than the whole '
              f"({s:.1f} vs {whole:.1f})")
        check(s <= whole * r.max_zoom + 1e-6,
              f'{r.items[i]["label"]} is held to max_zoom ({s / whole:.2f}x)')
    check(zooms == sorted(zooms),
          f"the smaller the share the closer the camera goes ({zooms})")
    # the travel lifts away and settles again rather than sliding flat across
    a, b = r.section_view[0][0], r.section_view[1][0]
    mid = r._cam_lerp(a, b, 0.5, render.PULLBACK)[0]
    check(mid < min(a[0], b[0]),
          f"the camera pulls back over the middle of a travel ({mid:.1f})")
    check(abs(r._cam_lerp(a, b, 0.0, render.PULLBACK)[0] - a[0]) < 1e-6
          and abs(r._cam_lerp(a, b, 1.0, render.PULLBACK)[0] - b[0]) < 1e-6,
          "and lands exactly where it was going")


def test_donut_card_is_outside_the_ring_and_in_a_corner(tmp):
    """the framing is chosen to fit the card, not the other way round.

    The card goes beyond the section's rim, on the far side from the middle of
    the ring, and is then slid into the corner of the frame on that side --
    pushing the hole off screen when that is what it takes.
    """
    items = [{"label": "Aye", "value": 148, "note": "the big one"},
             {"label": "Bee", "value": 96, "note": "the next"},
             {"label": "Cee", "value": 40, "note": "smaller"},
             {"label": "Dee", "value": 9, "note": "a sliver"}]
    r = render.make(donut_spec(donut={"items": items}), {},
                    os.path.join(tmp, "f"))
    for i in r.visit:
        cam, (px, py) = r.section_view[i]
        c, item = r.cards[i], r.items[i]
        box = (px - c["w"] / 2, py - c["h"] / 2,
               px + c["w"] / 2, py + c["h"] / 2)
        # clear of the ring: every corner of the card is outside the rim
        ox, oy = r._pt(cam, 0.0, 0.0)
        near = min(math.hypot(qx - ox, qy - oy)
                   for qx in (box[0], box[2]) for qy in (box[1], box[3]))
        check(near > cam[0] * (1.0 + render.POP),
              f'{item["label"]}: the card clears the ring by '
              f"{near - cam[0] * (1.0 + render.POP):.0f}px")
        # on the far side of the section from the middle
        mid = math.radians((item["a0"] + item["a1"]) / 2)
        dot = ((px - ox) * math.cos(mid) + (py - oy) * math.sin(mid))
        check(dot > 0, f'{item["label"]}: the card is away from the centre')
        # and in frame, having been slid as far out as it would go
        check(box[0] >= r.vp[0] - 1 and box[2] <= r.vp[0] + r.vp[2] + 1
              and box[1] >= r.vp[1] - 1 and box[3] <= r.vp[1] + r.vp[3] + 1,
              f'{item["label"]}: the card is inside the frame')
        # the whole section is on screen: that is what the camera is for
        wr = _bbox_of([r._pt(cam, *q) for q in r._wedge(i, pop=render.POP)])
        check(wr[0] >= r.vp[0] - 1 and wr[2] <= r.vp[0] + r.vp[2] + 1
              and wr[1] >= r.vp[1] - 1 and wr[3] <= r.vp[1] + r.vp[3] + 1,
              f'{item["label"]}: the whole section is on screen')
    # and at least one beat did push the hole off the frame
    off = sum(not (r.vp[0] <= r._pt(r.section_view[i][0], 0.0, 0.0)[0]
                   <= r.vp[0] + r.vp[2]
                   and r.vp[1] <= r._pt(r.section_view[i][0], 0.0, 0.0)[1]
                   <= r.vp[1] + r.vp[3]) for i in r.visit)
    check(off > 0, f"{off} of {len(r.visit)} beats push the hole off screen")


def test_donut_labels_do_not_overlap(tmp):
    """ten sections is ten labels down two columns, and a column that has run
    out of room spreads rather than stacks"""
    items = [{"label": f"Item number {i}", "value": 20 - i} for i in range(10)]
    r = render.make(donut_spec(donut={"items": items}), {},
                    os.path.join(tmp, "f"))
    # measured where they are drawn, not where they were asked for: the two
    # lines of a label are set above and below its point, so a column that
    # clears by its own spacing can still have the name of one sitting on the
    # number of the one above
    def block(i):
        y = r.centre[1] + r.labels[i]["text"][1]
        return (y - 15 * r.uk - r.f_lab.size / 2,
                y + 16 * r.uk + r.f_val.size / 2)
    for side in (1, -1):
        col = sorted(block(i) for i, L in r.labels.items() if L["side"] == side)
        worst = min((b[0] - a[1] for a, b in zip(col, col[1:])), default=99)
        check(worst >= 0,
              f"side {side}: no label is drawn over the next "
              f"(closest pair clears by {worst:.1f}px)")
        check(col[0][0] >= r.vp[1] and col[-1][1] <= r.estab_y,
              f"side {side}: the column stays clear of the header and the "
              "establishing caption")
    # and the whole figure came down to make room for the long names
    short = render.make(donut_spec(donut={"items": [
        {"label": "A", "value": 1}, {"label": "B", "value": 1}]}), {},
        os.path.join(tmp, "f2"))
    check(short.scale > r.scale,
          f"short labels buy a bigger ring ({short.scale:.0f} vs "
          f"{r.scale:.0f})")


def test_donut_labels_never_move(tmp):
    """the labels are cut in and out around the camera, never carried by it.

    Laid out in world coordinates they travel with the ring, so a zoom deals
    six of them outwards across the frame and off it -- movement the eye reads
    as the labels doing something, at the one moment the camera is what is
    supposed to be moving. So: they are only ever up while the camera is
    sitting on the whole figure, and while they are up they are in one place.
    """
    r = render.make(donut_spec(), {}, os.path.join(tmp, "f"))
    seen, moving = 0, 0
    for n in range(int(round(r.total() * r.fps))):
        s, u, _ = r.at(n / r.fps)
        a = (r.cut_alpha(s, u, s is r.timeline[-1]) if s["labels"] else 0.0)
        cam = r._cam_lerp(s["cam0"], s["cam1"], render.ease(u), s["arc"])
        off = max(abs(cam[k] - r.cam_whole[k]) for k in range(3)) > 1e-6
        seen += a > 0
        moving += a > 0 and off
    check(seen > 0, f"the labels are up for {seen} frames")
    check(moving == 0,
          f"and for none of them is the camera off the whole figure ({moving})")
    # a label's position is a constant, not a function of the frame
    check(all(isinstance(v, tuple) and len(v) == 2
              for L in r.labels.values()
              for k, v in L.items() if k != "side"),
          "every label point is a fixed canvas offset")


def test_donut_card_carries_the_description(tmp):
    """the note is on the card, beside the section, not in a bar at the bottom.

    The bar is gone: a reader whose eye is in the middle of the frame does not
    read a strip along the bottom of it.
    """
    note = "one per open buffer, kept whole so an edit reparses a subtree"
    sp = donut_spec()
    sp["render"]["donut"]["items"][0]["note"] = note
    r = render.make(sp, {}, os.path.join(tmp, "f"))
    check(not hasattr(r, "cap_y"), "a donut has no caption bar to draw into")
    check(r.vp[1] + r.vp[3] == r.H,
          "and the picture runs to the bottom of the frame")
    wrapped = r._wrap(note, r.f_note, render.CARD_TEXT * r.uk)
    check(len(wrapped) > 1 and " ".join(wrapped) == note,
          f"the note wraps to {len(wrapped)} lines and loses nothing")
    check(all(r.f_note.getlength(ln) <= render.CARD_TEXT * r.uk
              for ln in wrapped), "no wrapped line runs past the card")
    # mid-hold on section 0, the words are on screen
    mid = int((r.t["grow"] + r.t["intro"] + r.t["move"] + r.t["hold"] / 2)
              * r.fps)
    with_note = r.frame(mid)
    sp2 = donut_spec()
    sp2["render"]["donut"]["items"][0]["note"] = ""
    r2 = render.make(sp2, {}, os.path.join(tmp, "f2"))
    check(ImageChops.difference(with_note, r2.frame(mid)).getbbox() is not None,
          "the card is drawn differently for a section that has a note")


def test_donut_renders_every_beat(tmp):
    """every frame of the storyboard draws, and none of them is empty"""
    r = render.make(donut_spec(), {}, os.path.join(tmp, "f"))
    n = int(round(r.total() * r.fps))
    blank = Image.new("RGB", (r.W, r.H), r.th["bg"])
    for k in range(0, n, 2):
        im = r.frame(k)
        if ImageChops.difference(im, blank).getbbox() is None:
            check(False, f"frame {k} is empty")
            return
    check(True, f"all {n} frames draw something")
    # The ring really is a ring. `total: ""` empties the hole first, since the
    # thing that normally stands in it would answer this question the wrong
    # way round.
    r2 = render.make(donut_spec(donut={"total": ""}), {}, os.path.join(tmp, "f2"))
    mid = r2.frame(int((r2.t["grow"] + r2.t["intro"] * 0.5) * r2.fps))
    cx, cy = r2.centre
    band = (1.0 + r2.inner) / 2 * r2.scale
    check(mid.getpixel((int(cx), int(cy))) == r2.th["bg"], "the hole is a hole")
    check(mid.getpixel((int(cx + band), int(cy))) != r2.th["bg"],
          "and the band around it is not")


def test_donut_refuses_a_spec_it_cannot_draw(tmp):
    for bad, why in (
            ({"items": []}, "no items"),
            ({"items": [{"label": "a"}]}, "an item with no value"),
            ({"items": [{"label": "a", "value": -1}]}, "a negative value"),
            ({"items": [{"label": "a", "value": 0}]}, "values summing to zero"),
            ({"thickness": 1.5}, "a thickness outside the ring")):
        try:
            render.make(donut_spec(donut=bad), {}, os.path.join(tmp, "f"))
            check(False, f"{why} was accepted")
        except SystemExit:
            check(True, f"{why} is refused")
    sp = donut_spec()
    sp["capture"] = {"panes": {"solo": {"argv": ["x"]}}}
    try:
        render.mode_of(sp)
        check(False, "a donut spec that also films something was accepted")
    except SystemExit:
        check(True, "a donut spec cannot also name capture.panes")


def test_donut_ansi_style_is_selected_by_the_spec(tmp):
    """`style: "ansi"` prints the clip; anything else is refused"""
    sp = donut_spec()
    check(type(render.make(sp, {}, os.path.join(tmp, "f"))).__name__
          == "DonutRenderer", "the default style draws")
    sp["render"]["donut"]["style"] = "ansi"
    r = render.make(sp, {}, os.path.join(tmp, "f2"))
    check(type(r).__name__ == "AnsiDonutRenderer", "ansi style prints")
    check(r.ramp == render.CHARSETS["unicode"], "and defaults to the full set")
    # a set is only ever a string, so the characters themselves work too
    sp["render"]["donut"]["charset"] = "#@ ."
    check(render.make(sp, {}, os.path.join(tmp, "f3")).ramp == "#@ .",
          "a literal string is a charset")
    for bad, why in (({"style": "crt"}, "an unknown style"),
                     ({"style": "ansi", "charset": "x"}, "a one-character set"),
                     ({"style": "ansi", "focus": [4, 40]}, "focus the wrong way round")):
        try:
            render.make(donut_spec(donut=bad), {}, os.path.join(tmp, "f4"))
            check(False, f"{why} was accepted")
        except SystemExit:
            check(True, f"{why} is refused")


def test_donut_ansi_focus_pulls_only_when_the_camera_rests(tmp):
    """the ring is sharp exactly when it is being looked at.

    A character grid that moves crawls -- cells pop between glyphs as the
    shape slides under them, and the eye reads the popping rather than the
    motion. So nothing is ever both sharp and travelling.
    """
    sp = donut_spec()
    sp["render"]["donut"]["style"] = "ansi"
    r = render.make(sp, {}, os.path.join(tmp, "f"))
    seen = {}
    for n in range(int(round(r.total() * r.fps))):
        s, u, _ = r.at(n / r.fps)
        f = r.focus_at(s, u, s is r.timeline[-1])
        seen.setdefault(s["kind"], []).append(f)
    for kind in ("grow", "move", "regroup"):
        check(max(seen.get(kind, [0])) == 0.0,
              f"{kind} is coarse throughout")
    for kind in ("intro", "hold", "outro"):
        check(max(seen[kind]) > 0.99, f"{kind} reaches full focus")
    check(seen["hold"][0] < 0.2 and seen["hold"][-1] < 0.5,
          "a hold arrives out of focus and lets go before the next travel")
    check(seen["outro"][-1] > 0.99,
          "... but the last beat has nothing to let go for")
    # the grid coarsens monotonically as the focus backs off
    cells = [r.cell_at(f / 10) for f in range(11)]
    check(cells == sorted(cells, reverse=True) and cells[0] == r.coarse
          and cells[-1] == r.fine,
          f"the cell runs {r.coarse}px -> {r.fine}px without reversing ({cells})")


def test_donut_ansi_renders_every_beat(tmp):
    """every frame prints something, and the sharp ones are not a grid"""
    sp = donut_spec()
    sp["render"]["donut"].update({"style": "ansi", "charset": "shades"})
    r = render.make(sp, {}, os.path.join(tmp, "f"))
    blank = Image.new("RGB", (r.W, r.H), r.th["bg"])
    n = int(round(r.total() * r.fps))
    for k in range(0, n, 3):
        if ImageChops.difference(r.frame(k), blank).getbbox() is None:
            check(False, f"frame {k} is empty")
            return
    check(True, f"all {n} frames print something")
    # the characters are measured off the font at the size they are printed,
    # so the set spans a real range of ink rather than a nominal one
    ink = [d for _, _, d in r._masks_for(r.fine)]
    check(ink[0] == 0.0 and ink[-1] > 0.8,
          f"the set runs from blank to nearly solid ({ink[0]:.2f}..{ink[-1]:.2f})")


def test_draft_sizes(tmp):
    """derived, so nobody meets 'width not divisible by 2'"""
    # bin/tui-clip has no .py extension, so it needs its loader naming
    import importlib.util
    from importlib.machinery import SourceFileLoader
    ld = SourceFileLoader("tui_clip", os.path.join(ROOT, "bin", "tui-clip"))
    mod = importlib.util.module_from_spec(
        importlib.util.spec_from_loader("tui_clip", ld))
    ld.exec_module(mod)
    draft = mod.draft
    for size in ([1080, 1080], [1082, 723], [1920, 1080], [7, 9]):
        sp = {"render": {"size": list(size), "fps": 60}}
        draft(sp)
        w, h = sp["render"]["size"]
        check(w % 2 == 0 and h % 2 == 0,
              f"{size} -> {w}x{h}, both even for libx264")
        check(sp["render"]["fps"] == 30, "draft drops to 30fps")
    sp = {"render": {"size": [1080, 1080], "fps": 60,
                     "chrome": {"scale": 3}}}
    draft(sp)
    w, h = sp["render"]["size"]
    cs = sp["render"]["chrome"]["scale"]
    check(w % cs == 0 and h % cs == 0,
          f"chrome draft {w}x{h} still divides by its scale {cs}")
    check(w % 2 == 0 and h % 2 == 0, "and is still even")


def test_xwd_roundtrip(tmp):
    """our reader agrees with ImageMagick, where both are available"""
    if not shutil.which("xwd") or not shutil.which("Xvfb"):
        print("  skip xwd round-trip (no xwd/Xvfb here)")
        return
    disp = ":95"
    xv = subprocess.Popen(["Xvfb", disp, "-screen", "0", "300x200x24",
                           "-nolisten", "tcp"], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL)
    try:
        import time
        time.sleep(2)
        env = {**os.environ, "DISPLAY": disp}
        raw = os.path.join(tmp, "root.xwd")
        r = subprocess.run(["xwd", "-silent", "-root", "-out", raw], env=env)
        if r.returncode != 0:
            print("  skip xwd round-trip (xwd would not run)")
            return
        im = xwdfile.load(raw)
        check(im.size == (300, 200), f"read the dump at {im.size}")
        if shutil.which("convert"):
            png = os.path.join(tmp, "root.png")
            subprocess.run(["convert", raw, png], check=True)
            ref = Image.open(png).convert("RGB")
            check(ImageChops.difference(im, ref).getbbox() is None,
                  "pixel-identical to ImageMagick's decode")
    finally:
        xv.kill()


def solid(path, rgb, w=600, h=400):
    """a flat screen, so a pixel sample says which of two screens it came from"""
    Image.new("RGB", (w, h), rgb).save(path)
    return path


def test_swipe_replaces_a_row_left_to_right(tmp):
    """the rename wipe shows one name per pixel, and only on the rows named.

    A dissolve between two screens that differ only in the text of some rows
    reads as a blur: both strings are legible at once through the middle of
    it. The wipe has a hard edge, so a pixel is either the old name or the
    new one -- and the rows nobody named must not move at all, which is what
    keeps a folder header from flickering under a cascade.
    """
    a = solid(os.path.join(tmp, "a.png"), (200, 0, 0))
    b = solid(os.path.join(tmp, "b.png"), (0, 0, 200))
    ann = [{"shot": "a", "rows": [0, 20], "cols": [0, 60], "band": False,
            "hold": 1.0,
            "swipe": {"to": "b", "rows": [5], "at": 0.0, "row": 1.0,
                      "stagger": 0.0, "edge": 0}}]
    r = render.make(spec_for(tmp, annotations=ann), {"solo": a},
                    os.path.join(tmp, "f"), {"a": a, "b": b})
    red, blue = (200, 0, 0), (0, 0, 200)
    mid_y = int(5 * r.RH + r.RH / 2)          # a scanline inside row 5
    off_y = int(4 * r.RH + r.RH / 2)          # a row nobody named

    start = r.screen(0, 0.0, 1.0)
    check(start.getpixel((10, mid_y)) == red, "before its turn the row is the old screen")

    half = r.screen(0, 0.5, 1.0)
    w = half.size[0]
    check(half.getpixel((10, mid_y)) == blue, "mid-wipe the left of the row has changed")
    check(half.getpixel((w - 10, mid_y)) == red, "mid-wipe the right of the row has not")
    check(half.getpixel((10, off_y)) == red, "a row the swipe never named is untouched")

    end = r.screen(0, 0.999, 1.0)
    check(end.getpixel((10, mid_y)) == blue, "after its turn the row is the new screen")
    check(end.getpixel((w - 10, mid_y)) == blue, "... all the way across")
    check(end.getpixel((10, off_y)) == red, "and the unnamed row still is not")


def test_swipe_staggers_the_rows_it_is_given(tmp):
    """rows go one after another, in the order listed, not all at once."""
    a = solid(os.path.join(tmp, "a.png"), (200, 0, 0))
    b = solid(os.path.join(tmp, "b.png"), (0, 0, 200))
    ann = [{"shot": "a", "rows": [0, 20], "cols": [0, 60], "band": False,
            "hold": 2.0,
            "swipe": {"to": "b", "rows": [5, 6], "at": 0.0, "row": 0.4,
                      "stagger": 0.8, "edge": 0}}]
    r = render.make(spec_for(tmp, annotations=ann), {"solo": a},
                    os.path.join(tmp, "f"), {"a": a, "b": b})
    y5 = int(5 * r.RH + r.RH / 2)
    y6 = int(6 * r.RH + r.RH / 2)
    red, blue = (200, 0, 0), (0, 0, 200)
    # t = 0.5s: row 5 finished at 0.4s, row 6 does not start until 0.8s
    im = r.screen(0, 0.25, 1.0)
    check(im.getpixel((10, y5)) == blue, "the first row has gone over")
    check(im.getpixel((10, y6)) == red, "the second has not started")
    # t = 1.2s: row 6 finished at 1.2s
    im2 = r.screen(0, 0.6, 1.0)
    check(im2.getpixel((10, y6)) == blue, "the second row follows it")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(f"{t.__name__}:")
        tmp = tempfile.mkdtemp(prefix="tui-clips-test-")
        try:
            t(tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    print()
    if FAILED:
        print(f"{len(FAILED)} failed")
        for f in FAILED:
            print(f"  - {f}")
        raise SystemExit(1)
    print("all passed")


if __name__ == "__main__":
    main()
