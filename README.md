# tui-clips

Before/after comparison clips of a terminal program, built for social video.

Two builds of a TUI are screenshotted on a throwaway X display, then composed
into a short clip: side by side, the BEFORE panel swipes out, the AFTER zooms to
full size, and a few annotated callouts walk through what changed.

Everything is driven by one JSON spec. Nothing touches the terminal or editor
session you are actually using.

```
./bin/tui-clip specs/fresh-markdown-compose.json
# -> out/fresh-markdown-compose.mp4
```

About 75 s end to end: ~25 s capturing, ~40 s rendering frames, ~10 s encoding.


## Requirements

`Xvfb`, `xfce4-terminal`, `xdotool`, ImageMagick (`import`, `magick`), `ffmpeg`,
and Python with Pillow. No X session needed — it runs headless.

`xfce4-terminal` is deliberate. Alacritty, kitty and Ghostty want OpenGL, which
Xvfb only fakes through llvmpipe; VTE-based terminals render in plain X11 and
just work. `foot` is Wayland-only.


## The pieces

| | |
|---|---|
| `bin/tui-clip` | spec -> captures -> frames -> mp4. The entry point. |
| `bin/tui-capture` | one headless screenshot of one command. Useful alone. |
| `bin/tui-grid` | grid overlay for reading off annotation coordinates. |
| `lib/render.py` | the compositor. Storyboard and drawing live here. |
| `specs/*.json` | one file per clip. |
| `out/` | captures, frame sequences, mp4s. Gitignored. |


## Making a new clip

**1. Pick the two builds and the viewport.** Both panes must use the same
`geometry` and `font`, or the render aborts — the whole thing is a pixel
comparison.

**2. Size the type for a phone.** This is the constraint that drives everything
else. Roughly `px_per_char = 0.79 x point_size`, so for a target video width and
a column count:

```
point_size ~= video_width / (0.79 x columns)
```

64 columns at 1080 px wide gives 21 pt, which is comfortable on a phone. Going
much past 70 columns stops being readable in a timeline. Narrow is fine — most
layout bugs get *more* obvious at a narrow measure, not less.

**3. Choose a row count that frames the whole story.** The clip pans vertically
over the capture, so capture more rows than fit on screen: 50 rows at 21 pt is
~1900 px tall, and the viewport shows ~850 px of it. Make sure the Xvfb `screen`
is taller than the resulting window or the capture is clipped.

**4. Find a cursor position where both panes line up.** Both captures should
share a visual anchor near the top, so the swipe reads as an overlay rather than
two unrelated screens. Open the file at an explicit line and check.

Prefer parking the caret on a **blank** line. Editors commonly reveal the raw
source of the caret's line in a rendered/preview mode, and a stray `- ` or `**`
in the AFTER pane reads as a rendering flaw to anyone who does not know the app.

**5. Shoot the stills and iterate on framing** before spending time on the video:

```
./bin/tui-clip specs/mine.json --stills
```

**6. Read off the annotation coordinates.** Bands are given in *terminal cells*,
not pixels, so they survive a font or geometry change:

```
./bin/tui-grid out/mine/after.png --rows 50 --cols 64 --parts 2
```

Open the grid images and note the first and last row of each region. Bands are
half-open on the bottom: rows `[15, 22]` covers rows 15 through 21, so use
`last_row + 1`. Getting this wrong slices a box through the middle of a line of
text, which is the most common mistake here.

**7. Render, look, adjust.** After the first run, iterate without re-capturing:

```
./bin/tui-clip specs/mine.json --skip-capture
```


## Spec reference

```jsonc
{
  "name": "my-clip",                  // names out/my-clip.mp4
  "capture": {
    "geometry": "64x50",              // terminal cells; same for both panes
    "font": "JetBrains Mono 21",
    "screen": "1600x2200x24",         // must exceed the terminal window
    "display": ":99",
    "settle": 8,                      // seconds to wait for startup
    "copy_files": ["~/repo/FILE.md"], // copied into a scratch dir, see below
    "env": {                          // {scratch} {proj} {pane} expand
      "XDG_DATA_HOME": "{scratch}/data-{pane}",
      "XDG_RUNTIME_DIR": "{scratch}/run"
    },
    "keys": [                         // driven into both panes, in order
      {"key": "ctrl+p"},
      {"type": "toggle compose"},
      {"key": "Return"}
    ],
    "panes": {
      "before": {"argv": ["~/.local/bin/app", "FILE.md:39:1"]},
      "after":  {"argv": ["~/repo/target/debug/app", "FILE.md:39:1"],
                 "settle": 16}        // debug builds start slowly
    }
  },
  "render": {
    "size": [1080, 1080],             // square fills the most timeline on X
    "fps": 60,
    "rows": 50, "cols": 64,           // must match capture geometry
    "title": "app - feature",
    "labels": {"before": "BEFORE", "after": "AFTER"},
    "intro_caption": ["Same file, same width", "only the rendering changed"],
    "outro_caption": ["app", "github.com/you/app"],
    "timing": {"intro": 2.1, "swipe": 0.8,
               "hold": 1.8, "pan": 0.4, "outro": 1.0},
    "annotations": [                  // one beat each; cells, not pixels
      {"rows": [3, 7], "cols": [0.2, 62.5],
       "head": "Short claim", "sub": "the mechanism, lower case"}
    ]
  },
  "encode": {"crf": 18, "preset": "slow"}
}
```

Total duration is `intro + swipe + n*hold + (n-1)*pan + outro`. Three
annotations at the defaults gives 10.1 s. `theme` under `render` overrides any
of `bg`, `caption_bg`, `panel_border`, `muted`, `fg`, `before`, `after`.


## Things that cost time to work out

**Isolate the app's state.** A TUI that keeps per-directory session state, a
lock, or an IPC socket will fight with the copy you have open for real, or
restore the wrong buffers into the shot. `copy_files` puts the subject file in a
fresh scratch directory, and pointing `XDG_DATA_HOME` and `XDG_RUNTIME_DIR` at
scratch keeps sessions and sockets separate. Leave `XDG_CONFIG_HOME` alone so
the app keeps your real theme.

**Never `pkill -f` a pattern that could match your own shell.** `pkill -f "Xvfb
:99"` matches the shell whose command line contains that string — including the
one running the script, and including an agent's tool-call wrapper. Both scripts
match, then kill by pid. For the same reason, `pgrep -f Xvfb` reports phantom
hits; scan `/proc/<pid>/cmdline` instead, which is what `--stop` does.

**`xdotool search` is not a readiness probe.** It exits 1 on a *working* but
empty display, because it found no windows. Use `xdotool getdisplaygeometry`.

**No window manager runs on the Xvfb display.** Windows land unmanaged at 0,0
with no titlebar, which is exactly what you want — the capture is pure terminal.

**`xfce4-terminal` needs `--disable-server`.** Without it, it hands the request
to an existing instance and exits immediately, and no window ever appears on
your display.

**Give debug builds a longer `settle`.** A large unoptimised binary can take
15 s to paint its first frame; the capture will silently grab a blank screen.

**Add a silent audio track.** X and some other players are happier with one, and
it costs nothing.


## Extending the storyboard

`lib/render.py` is one `frame(n)` function driven by `phase(t)`, which maps a
timestamp to `(swipe progress, annotation index, pan progress, outro progress)`.
Every layout number derives from the canvas size and the capture's aspect, so a
different `size` or row count needs no other edits. To change the choreography,
change `phase` and the four progress values it returns.
