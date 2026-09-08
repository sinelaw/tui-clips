"""Drive a terminal program on a headless X display and keep its frames.

The shape of this module follows from one observation about the stack it was
written for, which is worth stating before the code stops making sense:

    On some X stacks a terminal window does not repaint on its own schedule.
    It repaints when a client asks for its pixels. The screenshot is not an
    observation of the animation; it is the clock the animation runs on.

Where that is true, two things follow. Sampling has to be cheap, or the
camera steps past the motion instead of through it -- hence `xwd`, which
hands over the server's bytes and leaves the encoding for later. And the
asking must never stop, or the program's output queues up behind a window
that has not been asked to draw it, so the next burst opens on a screen from
a second ago -- hence the drain: frames grabbed outside a recorded run are
thrown away, but they are still grabbed, because the asking is the point.

Where it is not true -- and it was not true on the stack this was tested on,
see README -- none of that costs anything much, and the `x11grab` backend is
still here for anyone who measures their own stack and finds video works.

Nothing here decides for the caller which of those worlds they are in.
`tui-probe` measures it; `backend` selects it.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

from PIL import Image, ImageChops, ImageStat

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import xwdfile  # noqa: E402


def log(msg: str) -> None:
    print(f"tui-capture: {msg}", file=sys.stderr, flush=True)


class CaptureError(Exception):
    pass


# ---------------------------------------------------------------------------
# grabbers
# ---------------------------------------------------------------------------

class Grabber:
    """one still off the display, as cheaply as the backend can manage"""

    ext = "png"

    def __init__(self, wid: str, display: str):
        self.wid, self.display = wid, display
        # The grabber runs X clients of its own, so it carries the display
        # rather than trusting whatever DISPLAY the caller happened to export.
        self.env = {**os.environ, "DISPLAY": display}

    def grab(self, path: str) -> None:
        raise NotImplementedError

    def load(self, path: str) -> Image.Image:
        return Image.open(path).convert("RGB")

    def thumb(self, path: str, size=(240, 120)) -> Image.Image:
        return self.load(path).convert("L").resize(size, Image.BILINEAR)

    def to_png(self, src: str, dst: str) -> None:
        if src != dst:
            os.replace(src, dst)


class XwdGrabber(Grabber):
    """`xwd`: the server's raw bytes, no encode.

    Measured against ImageMagick `import` on the same window: ~7ms vs ~50ms at
    964x580, ~29ms vs ~128ms at 1540x1140. That is the difference between
    sampling a 180ms animation and stepping over it.
    """

    ext = "xwd"

    def grab(self, path: str) -> None:
        subprocess.run(["xwd", "-silent", "-id", self.wid, "-out", path],
                       env=self.env, check=True, stderr=subprocess.DEVNULL)

    def load(self, path: str) -> Image.Image:
        try:
            return xwdfile.load(path)
        except xwdfile.XwdError:
            # An unexpected dump is not worth failing a four-minute capture
            # over when ImageMagick can still read it.
            png = path + ".png"
            subprocess.run(["convert", path, png], check=True)
            im = Image.open(png).convert("RGB")
            os.unlink(png)
            return im

    def thumb(self, path: str, size=(240, 120)) -> Image.Image:
        try:
            return xwdfile.thumb(path, size)
        except xwdfile.XwdError:
            return super().thumb(path, size)

    def to_png(self, src: str, dst: str) -> None:
        self.load(src).save(dst)
        os.unlink(src)


class ImportGrabber(Grabber):
    """ImageMagick `import`: a PNG per frame, encode cost and all"""

    def grab(self, path: str) -> None:
        subprocess.run(["import", "-window", self.wid, path], env=self.env,
                       check=True, stderr=subprocess.DEVNULL)


GRABBERS = {"xwd": XwdGrabber, "import": ImportGrabber}


# ---------------------------------------------------------------------------
# runs
# ---------------------------------------------------------------------------

class Run:
    """a stretch of the session whose frames are kept.

    A run does not start or stop the grabbing -- that never stops. It only
    says, for a window of time, where the frames go.
    """

    def __init__(self, name: str, path: str, seconds: float, ext: str,
                 keep: int = 0, anchor: str = "max-diff"):
        self.name, self.dir, self.ext = name, path, ext
        self.seconds = float(seconds)
        self.keep = int(keep or 0)
        self.anchor = anchor
        self.deadline: float | None = None
        self.frames: list[tuple[float, str]] = []
        self.n = 0
        shutil.rmtree(self.dir, ignore_errors=True)
        os.makedirs(self.dir, exist_ok=True)

    def start(self, now: float) -> None:
        self.deadline = now + self.seconds

    def live(self, now: float) -> bool:
        return self.deadline is not None and now < self.deadline

    def next_path(self) -> str:
        p = os.path.join(self.dir, f"{self.n:04d}.{self.ext}")
        self.n += 1
        return p


def _mad(a: Image.Image, b: Image.Image) -> float:
    """mean absolute difference between two grayscale thumbnails"""
    return ImageStat.Stat(ImageChops.difference(a, b)).mean[0]


def pick_anchor(diffs: list[float], how: str) -> int:
    """which frame the event lands on, given the change into each frame.

    `diffs[i]` is how much frame i+1 differs from frame i, so the index this
    returns is into the frame list, not into `diffs`.
    """
    if not diffs:
        return 0
    if how == "max-diff":
        return max(range(len(diffs)), key=lambda i: diffs[i]) + 1
    if how in ("first-change", "last-change"):
        # Anything within a half of the largest change counts as the event;
        # below that is the cursor blinking and the clock in the status bar.
        thr = max(diffs) * 0.5
        hits = [i for i, d in enumerate(diffs) if d >= thr]
        return (hits[0] if how == "first-change" else hits[-1]) + 1
    if how == "sustained":
        # A wipe is one big frame-to-frame change; a scroll is a run of
        # middling ones, and argmax lands somewhere arbitrary inside it. Sum
        # over a window so the run beats the spike.
        w = min(5, len(diffs))
        best, bi = -1.0, 0
        for i in range(len(diffs) - w + 1):
            s = sum(diffs[i:i + w])
            if s > best:
                best, bi = s, i
        return bi + w // 2 + 1
    raise CaptureError(f"unknown anchor {how!r} (max-diff, first-change, "
                       "last-change, sustained)")


def trim(run: Run, grab: Grabber) -> list[float]:
    """keep the `keep` frames around the event, and renumber. -> the diffs.

    Aiming a short recording at a keystroke with `sleep` does not work: the
    frame the key lands on moves by a few hundred milliseconds between takes,
    and a window narrow enough to be worth keeping is narrow enough to miss.
    So the window is long, and the camera finds the event afterwards -- in a
    window whose only event is the switch, the largest frame-to-frame change
    is the switch.

    The kept frames are anchored asymmetrically, about a third before the
    event and two thirds after: the eye needs a moment of the old screen to
    register that it was the old screen, and longer on the new one to read it.
    """
    frames = run.frames
    if len(frames) < 2:
        return []
    thumbs = [grab.thumb(p) for _, p in frames]
    diffs = [_mad(thumbs[i], thumbs[i + 1]) for i in range(len(thumbs) - 1)]
    if not run.keep or run.keep >= len(frames):
        return diffs
    anchor = pick_anchor(diffs, run.anchor)
    lo = max(0, min(anchor - run.keep // 3, len(frames) - run.keep))
    kept = frames[lo:lo + run.keep]
    for _, p in frames:
        if not any(p == q for _, q in kept):
            os.unlink(p)
    run.frames = kept
    log(f"  {run.name}: kept {len(kept)}/{len(frames)} frames around "
        f"frame {anchor} ({run.anchor})")
    return diffs[lo:lo + run.keep - 1]


# ---------------------------------------------------------------------------
# the session
# ---------------------------------------------------------------------------

class Session:
    """an Xvfb, a terminal on it, and a camera that never stops asking"""

    TERMS = ("xfce4-terminal", "xterm")

    def __init__(self, display=":99", screen="1600x2200x24", geometry="80x24",
                 font="JetBrains Mono 21", workdir=None, settle=8.0,
                 term="xfce4-terminal", backend="xwd", grab_fps=30.0,
                 key_settle=0.12, type_settle=0.4, locale="C.UTF-8",
                 env=None):
        self.display, self.screen = display, screen
        self.geometry, self.font = geometry, font
        self.workdir = workdir or os.getcwd()
        self.settle, self.key_settle = float(settle), float(key_settle)
        self.type_settle = float(type_settle)
        self.term, self.backend = term, backend
        self.grab_fps = float(grab_fps)
        self.locale = locale
        self.env = dict(env or {})
        self.cols, self.rows = (int(v) for v in geometry.lower().split("x"))

        self.wid = None
        self.grabber: Grabber | None = None
        self.runs: dict[str, Run] = {}
        # (raw grab, final png). Nothing is encoded until the sequence is
        # over: an encode between two frames lands in the middle of the
        # motion being filmed, which is the one place it must not.
        self.pending: list[tuple[str, str]] = []
        self.pointer: list[dict] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._grabs = 0
        self._grab_time = 0.0
        self._started_xvfb = False
        self._ffmpegs: list[subprocess.Popen] = []
        self._ff_runs: list[tuple[str, str]] = []   # x11grab: (name, dir)

    # -- environment --------------------------------------------------------

    def _child_env(self) -> dict:
        e = dict(os.environ)
        e["DISPLAY"] = self.display
        # A terminal that decodes box-drawing glyphs as latin-1 renders every
        # one as about three columns and wraps every line that has any. The
        # failure is silent and looks exactly like a layout bug in the program
        # being filmed, so the locale is forced rather than inherited.
        if self.locale:
            e.setdefault("LANG", self.locale)
            e["LANG"] = self.env.get("LANG", self.locale)
            e["LC_ALL"] = self.env.get("LC_ALL", self.locale)
        e.update(self.env)
        return e

    # -- lifecycle ----------------------------------------------------------

    def _x_up(self) -> bool:
        return subprocess.run(["xdotool", "getdisplaygeometry"],
                              env=self._child_env(), timeout=3,
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode == 0

    def start_x(self) -> None:
        try:
            if self._x_up():
                return
        except subprocess.TimeoutExpired:
            pass
        subprocess.Popen(
            ["Xvfb", self.display, "-screen", "0", self.screen, "-nolisten",
             "tcp"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        self._started_xvfb = True
        for _ in range(20):
            time.sleep(0.25)
            try:
                if self._x_up():
                    return
            except subprocess.TimeoutExpired:
                pass
        raise CaptureError(f"Xvfb {self.display} did not come up")

    def _term_argv(self, cmd: list[str]) -> list[str]:
        if self.term == "xfce4-terminal":
            return ["xfce4-terminal", "--disable-server", "--hide-menubar",
                    "--hide-toolbar", "--hide-scrollbar", "--hide-borders",
                    f"--geometry={self.geometry}", f"--font={self.font}",
                    f"--working-directory={self.workdir}", "-x", *cmd]
        if self.term == "xterm":
            # "JetBrains Mono 21" -> -fa "JetBrains Mono" -fs 21
            m = re.match(r"^(.*?)\s+(\d+(?:\.\d+)?)$", self.font)
            fam, sz = (m.group(1), m.group(2)) if m else (self.font, "14")
            return ["xterm", "-geometry", self.geometry, "-fa", fam,
                    "-fs", sz, "-b", "0", "-bw", "0", "+sb", "-e", *cmd]
        raise CaptureError(f"unknown terminal {self.term!r} "
                           f"(one of {', '.join(self.TERMS)})")

    def kill_terms(self) -> None:
        # Never `pkill -f`: the pattern can match the shell running this and
        # take the capture out along with the target. Match, then kill by pid.
        pat = ("xfce4-terminal --disable-server" if self.term == "xfce4-terminal"
               else None)
        if pat:
            out = subprocess.run(["pgrep", "-f", pat], capture_output=True,
                                 text=True).stdout
        else:
            out = subprocess.run(["pgrep", "-x", self.term], capture_output=True,
                                 text=True).stdout
        for pid in out.split():
            try:
                os.kill(int(pid), signal.SIGTERM)
            except (ProcessLookupError, ValueError):
                pass

    def launch(self, cmd: list[str]) -> None:
        self.kill_terms()
        time.sleep(1)
        subprocess.Popen(self._term_argv(cmd), env=self._child_env(),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        time.sleep(self.settle)
        self.wid = self._find_window()
        self._xdo("windowfocus", self.wid)
        time.sleep(0.5)
        self.grabber = GRABBERS[self.backend](self.wid, self.display) \
            if self.backend in GRABBERS else XwdGrabber(self.wid, self.display)
        self.w, self.h = self._geometry_px()
        self.cw, self.ch = self.w / self.cols, self.h / self.rows

    def _find_window(self) -> str:
        sel = (["search", "--name", "Terminal - "] if self.term == "xfce4-terminal"
               else ["search", "--class", "XTerm"])
        out = subprocess.run(["xdotool", *sel], env=self._child_env(),
                             capture_output=True, text=True).stdout.split()
        if not out:
            raise CaptureError("no terminal window appeared")
        return out[-1]

    def _geometry_px(self) -> tuple[int, int]:
        out = subprocess.run(
            ["xdotool", "getwindowgeometry", "--shell", self.wid],
            env=self._child_env(), capture_output=True, text=True).stdout
        d = dict(l.split("=", 1) for l in out.strip().splitlines() if "=" in l)
        return int(d["WIDTH"]), int(d["HEIGHT"])

    def _xdo(self, *args) -> None:
        subprocess.run(["xdotool", *[str(a) for a in args]],
                       env=self._child_env(), check=False)

    # -- the grab loop ------------------------------------------------------

    def begin_grabbing(self, drain_dir: str) -> None:
        """start asking for pixels, and never stop until the session ends.

        The drain path is one file, overwritten every time. Its frames are not
        wanted as pictures; the asking is what keeps the program's output from
        queueing up behind a window nobody has invited to repaint.
        """
        if self.backend == "x11grab":
            return                      # ffmpeg does its own asking
        os.makedirs(drain_dir, exist_ok=True)
        self._drain = os.path.join(drain_dir, f"drain.{self.grabber.ext}")
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        interval = 1.0 / self.grab_fps if self.grab_fps > 0 else 0.0
        misses = 0
        while not self._stop.is_set():
            t0 = time.time()
            with self._lock:
                run = next((r for r in self.runs.values() if r.live(t0)), None)
                path = run.next_path() if run else self._drain
            try:
                self.grabber.grab(path)
                misses = 0
            except subprocess.CalledProcessError:
                # One failed grab is not worth ending a capture over; a window
                # that has gone away for good is, and it will fail every time.
                misses += 1
                if misses > 20:
                    log("grabbing stopped: the window is not answering")
                    break
                self._stop.wait(0.05)
                continue
            t1 = time.time()
            if run:
                with self._lock:
                    run.frames.append((t0, path))
            self._grabs += 1
            self._grab_time += t1 - t0
            rest = interval - (t1 - t0)
            if rest > 0:
                self._stop.wait(rest)

    def end_grabbing(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        for p in self._ffmpegs:
            try:
                p.wait(timeout=30)
            except subprocess.TimeoutExpired:
                p.kill()

    def grab_rate(self) -> tuple[int, float]:
        if not self._grabs:
            return 0, 0.0
        return self._grabs, self._grab_time / self._grabs

    # -- actions ------------------------------------------------------------

    def key(self, spec: str, repeat: int = 1, settle: float | None = None,
            delay: int = 40) -> None:
        if repeat > 1:
            # A scroll driven one press per step spends a minute on what the
            # program does in a second, and lands as N jumps rather than one
            # travel.
            self._xdo("key", "--window", self.wid, "--repeat", repeat,
                      "--repeat-delay", delay, spec)
        else:
            self._xdo("key", "--window", self.wid, spec)
        time.sleep(self.key_settle if settle is None else float(settle))

    def type(self, text: str, settle: float | None = None) -> None:
        # Typing gets a longer settle than a single key: a program that
        # reacts per character is still catching up when the last one lands.
        self._xdo("type", "--window", self.wid, "--delay", 40, text)
        time.sleep(self.type_settle if settle is None else float(settle))

    # -- the mouse ----------------------------------------------------------

    def _px(self, col: float, row: float) -> tuple[int, int]:
        """cell coordinates to pixels on the root window.

        Columns and rows rather than pixels because that is what `tui-grid`
        prints and what survives a font change, same as everywhere else.
        """
        return (int(round((col + 0.5) * self.cw)),
                int(round((row + 0.5) * self.ch)))

    def _mouse(self, x: int, y: int) -> None:
        """move the pointer, in coordinates relative to the terminal window.

        Window-relative rather than root: the window is not necessarily at the
        origin, and a spec that says column 38 means column 38 of the program,
        not of whatever the display happens to have put behind it.
        """
        self._xdo("mousemove", "--sync", "--window", self.wid, x, y)

    def _mark(self, x: int, y: int, down: bool, visible=True) -> None:
        self.pointer.append({"t": time.time(), "x": x, "y": y,
                             "down": down, "visible": visible})

    def move(self, col: float, row: float) -> None:
        x, y = self._px(col, row)
        self._mouse(x, y)
        self._mark(x, y, False)

    def drag(self, from_col: float, to_col: float, row: float,
             from_row: float | None = None, to_row: float | None = None,
             steps: int = 8, settle: float | None = None) -> None:
        """press, travel, release, then park the pointer out of the frame.

        The park matters: a pointer left sitting over a list leaves a hover
        highlight on whatever row it landed on, and the clip then shows a
        selection nobody made.
        """
        r0 = row if from_row is None else from_row
        r1 = row if to_row is None else to_row
        x0, y0 = self._px(from_col, r0)
        x1, y1 = self._px(to_col, r1)
        self._mouse(x0, y0)
        self._mark(x0, y0, False)
        time.sleep(0.15)
        self._xdo("mousedown", 1)
        self._mark(x0, y0, True)
        for i in range(1, steps + 1):
            f = i / steps
            xi = int(round(x0 + (x1 - x0) * f))
            yi = int(round(y0 + (y1 - y0) * f))
            self._mouse(xi, yi)
            self._mark(xi, yi, True)
            time.sleep(0.03)
        self._xdo("mouseup", 1)
        self._mark(x1, y1, False)
        time.sleep(self.key_settle if settle is None else float(settle))
        self.park()

    def click(self, col: float, row: float, button: int = 1,
              settle: float | None = None) -> None:
        x, y = self._px(col, row)
        self._mouse(x, y)
        self._mark(x, y, False)
        time.sleep(0.08)
        self._xdo("click", button)
        self._mark(x, y, True)
        self._mark(x, y, False)
        time.sleep(self.key_settle if settle is None else float(settle))
        self.park()

    def park(self) -> None:
        """put the pointer somewhere it cannot highlight anything"""
        x, y = self.w + 40, self.h + 40
        self._mouse(x, y)
        self._mark(x, y, False, visible=False)

    # -- stills and runs ----------------------------------------------------

    def shot(self, path: str) -> None:
        """photograph the screen now, encode it later.

        The grab is what has to happen at this instant; turning the bytes into
        a PNG does not, and doing it here would put a 40ms encode in the middle
        of the sequence -- which on a stack where the grab is the clock is the
        one place it must not go.
        """
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        if self.backend == "x11grab":
            ImportGrabber(self.wid, self.display).grab(path)
            return
        tmp = path + "." + self.grabber.ext
        self.grabber.grab(tmp)
        self.pending.append((tmp, path))

    def record(self, name: str, path: str, seconds: float, fps: float = 30,
               keep: int = 0, anchor: str = "max-diff") -> None:
        """open a window whose frames are kept, and return immediately.

        Returning immediately is the whole point. The steps after a record --
        a sleep, the keystroke, another sleep -- have to execute inside the
        window, or the keystroke and the animation it causes never end up in
        the same run.
        """
        if self.backend == "x11grab":
            self._record_ffmpeg(path, seconds, fps)
            self._ff_runs.append((name, path))
            return
        run = Run(name, path, seconds, self.grabber.ext, keep, anchor)
        with self._lock:
            run.start(time.time())
            self.runs[name] = run

    def _record_ffmpeg(self, path: str, seconds: float, fps: float) -> None:
        os.makedirs(path, exist_ok=True)
        out = subprocess.run(
            ["xdotool", "getwindowgeometry", "--shell", self.wid],
            env=self._child_env(), capture_output=True, text=True).stdout
        g = dict(l.split("=", 1) for l in out.strip().splitlines() if "=" in l)
        w, h = int(g["WIDTH"]), int(g["HEIGHT"])
        self._ffmpegs.append(subprocess.Popen(
            ["ffmpeg", "-v", "error", "-y", "-f", "x11grab", "-draw_mouse", "0",
             "-video_size", f"{w - w % 2}x{h - h % 2}", "-framerate", str(fps),
             "-i", f"{self.display}+{g['X']},{g['Y']}", "-t", str(seconds),
             "-fps_mode", "passthrough", os.path.join(path, "%04d.png")],
            env=self._child_env(), stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL))

    def await_runs(self) -> None:
        """let every open window run out its clock, still grabbing"""
        while True:
            with self._lock:
                t = time.time()
                pending = [r for r in self.runs.values() if r.live(t)]
            if not pending:
                return
            time.sleep(0.05)

    # -- what a run leaves behind -------------------------------------------

    def finish(self, verify: dict | None = None) -> dict:
        """trim, convert, and write each run's sidecar. -> the take report

        Everything encodes here, in one pass after the last step: the trim
        runs first, on the raw dumps, so the frames it throws away are never
        encoded at all.
        """
        report = {"runs": {}, "grabs": self._grabs,
                  "grab_ms": round(self.grab_rate()[1] * 1000, 2),
                  "backend": self.backend}
        for name, d in self._ff_runs:
            n = len([f for f in os.listdir(d) if f.endswith(".png")]) \
                if os.path.isdir(d) else 0
            report["runs"][name] = {"frames": n, "seconds": 0.0, "fps": 0.0,
                                    "diffs": []}
        for name, run in self.runs.items():
            diffs = trim(run, self.grabber)
            span = ((run.frames[-1][0] - run.frames[0][0])
                    if len(run.frames) > 1 else 0.0)
            for i, (t, p) in enumerate(list(run.frames)):
                dst = os.path.join(run.dir, f"{i:04d}.png")
                self.grabber.to_png(p, dst)
                run.frames[i] = (t, dst)
            rep = {"frames": len(run.frames),
                   "seconds": round(span, 3),
                   "fps": round(len(run.frames) / span, 2) if span else 0.0,
                   "diffs": [round(d, 3) for d in diffs]}
            if verify:
                rep["verify"] = self._verify(run, verify)
            report["runs"][name] = rep
            self._write_sidecar(run)
        for src, dst in self.pending:
            if os.path.exists(src):
                self.grabber.to_png(src, dst)
        report["encoded"] = sum(len(r.frames) for r in self.runs.values()) \
            + len(self.pending)
        return report

    def _write_sidecar(self, run: Run) -> None:
        """the timing and the pointer path, for the renderer.

        The renderer needs both to draw a cursor: which wall-clock instant each
        kept frame is, and where the pointer was at that instant. Neither is in
        the pixels -- X does not draw the cursor into a window grab.
        """
        if not run.frames:
            return
        t0, t1 = run.frames[0][0], run.frames[-1][0]
        pts = [p for p in self.pointer if t0 - 2.0 <= p["t"] <= t1 + 2.0]
        with open(os.path.join(run.dir, "frames.json"), "w") as fh:
            json.dump({
                "name": run.name,
                "cell": [self.cw, self.ch],
                "size": [self.w, self.h],
                "frames": [{"i": i, "t": round(t - t0, 4)}
                           for i, (t, _) in enumerate(run.frames)],
                "pointer": [{"t": round(p["t"] - t0, 4), "x": p["x"],
                             "y": p["y"], "down": p["down"],
                             "visible": p["visible"]} for p in pts],
            }, fh, indent=1)

    def _verify(self, run: Run, verify: dict) -> dict:
        """did the take work? -- answered without watching the video.

        `regions` names rects in cells. For each frame the brightest one wins,
        and the sequence of winners is the take: for a dock switching between
        sessions it must step exactly once per switch beat and never step back.
        """
        regions = verify.get("regions") or {}
        if not regions:
            return {}
        names = list(regions)
        boxes = [self._box(regions[n]) for n in names]
        seq, series = [], {n: [] for n in names}
        for _, p in run.frames:
            im = self.grabber.load(p).convert("L")
            vals = [ImageStat.Stat(im.crop(b)).mean[0] for b in boxes]
            for n, v in zip(names, vals):
                series[n].append(round(v, 2))
            seq.append(names[max(range(len(vals)), key=lambda i: vals[i])])
        steps = [i for i in range(1, len(seq)) if seq[i] != seq[i - 1]]
        order = [names.index(seq[i]) for i in [0, *steps]]
        back = [i for i in range(1, len(order)) if order[i] < order[i - 1]]
        rep = {"brightest": seq, "series": series,
               "changes": len(steps), "steps_back": len(back)}
        want = verify.get("expect_changes")
        ok = True
        if want is not None and len(steps) != int(want):
            ok = False
        if verify.get("monotonic", True) and back:
            ok = False
        rep["ok"] = ok
        return rep

    def _box(self, rect) -> tuple[int, int, int, int]:
        c0, r0, c1, r1 = rect
        return (int(c0 * self.cw), int(r0 * self.ch),
                max(int(c0 * self.cw) + 1, int(c1 * self.cw)),
                max(int(r0 * self.ch) + 1, int(r1 * self.ch)))

    # -- teardown -----------------------------------------------------------

    def stop(self, keep_display: bool = False) -> None:
        self.end_grabbing()
        self.kill_terms()
        if self._started_xvfb and not keep_display:
            kill_display(self.display)


def kill_display(display: str) -> None:
    out = subprocess.run(["pgrep", "-x", "Xvfb"], capture_output=True,
                         text=True).stdout
    for pid in out.split():
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                argv = fh.read().decode().split("\0")
        except OSError:
            continue
        if display in argv:
            try:
                os.kill(int(pid), signal.SIGKILL)
            except (ProcessLookupError, ValueError):
                pass


def report_text(report: dict) -> str:
    """the take report, short enough to read before rendering four minutes"""
    if report.get("backend") == "x11grab":
        out = ["ffmpeg recorded the runs; no continuous grabbing"]
    else:
        out = [f"grabs {report['grabs']} at {report['grab_ms']}ms each"]
    for name, r in report["runs"].items():
        line = f"  run {name}: {r['frames']} frames"
        if r["seconds"]:
            line += f" over {r['seconds']}s ({r['fps']}fps)"
        d = r.get("diffs") or []
        if d:
            line += f", change per frame max {max(d):.1f} mean {sum(d)/len(d):.1f}"
        out.append(line)
        v = r.get("verify")
        if v:
            out.append(f"    brightest: {' '.join(v['brightest'])}")
            out.append(f"    {v['changes']} changes, {v['steps_back']} back "
                       f"-> {'OK' if v['ok'] else 'SUSPECT'}")
    return "\n".join(out)
