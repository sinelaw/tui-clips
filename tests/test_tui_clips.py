#!/usr/bin/env python3
"""Regression tests. Run them with `python3 tests/test_tui_clips.py`.

They cover the things that were found the hard way and would otherwise be
found the hard way again: a cache that returned an image a pixel off, a note
that vanished with the band it was never part of, and a trim that has to land
on the event rather than near it.
"""
from __future__ import annotations

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


def test_donut_camera_closes_in_and_comes_back(tmp):
    """a section is read closer than the whole, and a sliver is not read so
    close that the ring around it is lost"""
    r = render.make(donut_spec(), {}, os.path.join(tmp, "f"))
    whole = r.cam_whole[0]
    for i, d in enumerate(r.items):
        s = r._section_cam(i)[0]
        check(s >= whole - 1e-6,
              f'{d["label"]} is read no further away than the whole '
              f"({s:.1f} vs {whole:.1f})")
        check(s <= whole * r.max_zoom + 1e-6,
              f'{d["label"]} is held to max_zoom ({s / whole:.2f}x)')
    check(r._section_cam(2)[0] == whole * r.max_zoom,
          "the 4% sliver is the one the cap actually bites on")
    # the travel lifts away and settles again rather than sliding flat across
    a, b = r._section_cam(0), r._section_cam(1)
    mid = r._cam_lerp(a, b, 0.5, render.PULLBACK)[0]
    check(mid < min(a[0], b[0]),
          f"the camera pulls back over the middle of a travel ({mid:.1f})")
    check(abs(r._cam_lerp(a, b, 0.0, render.PULLBACK)[0] - a[0]) < 1e-6
          and abs(r._cam_lerp(a, b, 1.0, render.PULLBACK)[0] - b[0]) < 1e-6,
          "and lands exactly where it was going")


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
        y = r._pt(r.cam_whole, *r.labels[i]["text"])[1]
        return (y - 15 * r.uk - r.f_lab.size / 2,
                y + 16 * r.uk + r.f_val.size / 2)
    for side in (1, -1):
        col = sorted(block(i) for i, L in r.labels.items() if L["side"] == side)
        worst = min((b[0] - a[1] for a, b in zip(col, col[1:])), default=99)
        check(worst >= 0,
              f"side {side}: no label is drawn over the next "
              f"(closest pair clears by {worst:.1f}px)")
        check(col[0][0] >= r.header_h and col[-1][1] <= r.cap_y,
              f"side {side}: the column stays between the bars")
    # and the whole figure came down to make room for the long names
    short = render.make(donut_spec(donut={"items": [
        {"label": "A", "value": 1}, {"label": "B", "value": 1}]}), {},
        os.path.join(tmp, "f2"))
    check(short.cam_whole[0] > r.cam_whole[0],
          f"short labels buy a bigger ring ({short.cam_whole[0]:.0f} vs "
          f"{r.cam_whole[0]:.0f})")


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
    mid = r2.frame(int((r2.t["grow"] + r2.t["intro"] * 0.9) * r2.fps))
    cx, cy = r2._pt(r2.cam_whole, 0.0, 0.0)
    band = (1.0 + r2.inner) / 2 * r2.cam_whole[0]
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
