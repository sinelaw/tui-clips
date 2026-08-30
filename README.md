# tui-clips

Short annotated videos of a terminal program, from a JSON spec — either two
builds side by side, or one build on its own.

## Quick start

Needs `Xvfb`, `xfce4-terminal`, `xdotool`, ImageMagick (`import`), `ffmpeg`, and
Python with Pillow. Runs headless — no X session, and nothing touches the
terminal or editor you have open.

```sh
./bin/tui-clip specs/fresh-markdown-compose.json   # -> out/<name>.mp4
```

About 75s: ~25s capturing, ~40s rendering frames, ~10s encoding. While
iterating:

```sh
./bin/tui-clip specs/mine.json --stills         # capture only, check framing
./bin/tui-clip specs/mine.json --skip-capture   # re-render from cached captures
./bin/tui-grid out/mine/after.png --rows 50 --cols 64 --parts 2
```

`tui-grid` overlays a numbered row/column grid on a capture. Annotation bands
are written in those same cells, so you read the numbers straight off it.

## Two shapes

**Comparison** — panes named `before` and `after`. Both captures appear side by
side, the BEFORE panel swipes out left, the AFTER zooms to full frame, then one
annotated beat per callout. See `specs/fresh-markdown-compose.json`.

**Solo** — one pane, any name, for a feature with nothing to compare against.
The capture opens centred as an establishing shot, grows in place to fill the
frame, then the same annotated beats. See `specs/fresh-markdown-compose-solo.json`.

The mode is inferred from the pane set. Everything after the opening — the
zoom, the dimmed bands, the captions, the outro — is identical.

## Spec

```jsonc
{
  "name": "my-clip",                  // names out/my-clip.mp4
  "capture": {
    "geometry": "64x50",              // cells; identical across panes
    "font": "JetBrains Mono 21",      // pt ~= video_width / (0.79 * columns)
    "screen": "1600x2200x24",         // must exceed the terminal window
    "display": ":99",
    "settle": 8,                      // seconds to wait for the first paint
    "copy_files": ["~/repo/FILE.md"], // copied into a scratch working dir
    "env": {                          // {scratch} {proj} {pane} expand
      "XDG_DATA_HOME": "{scratch}/data-{pane}",
      "XDG_RUNTIME_DIR": "{scratch}/run"
    },
    "keys": [                         // driven into every pane, in order
      {"key": "ctrl+p"},
      {"type": "toggle compose"},
      {"key": "Return"}
    ],
    "panes": {                        // {before, after} to compare, else one
      "before": {"argv": ["~/.local/bin/app", "FILE.md:39:1"]},
      "after":  {"argv": ["~/repo/target/debug/app", "FILE.md:39:1"],
                 "settle": 16,        // debug builds paint slowly
                 "keys": []}          // optional, appended to capture.keys
    }
  },
  "render": {
    "size": [1080, 1080],             // square fills the most timeline on X
    "fps": 60,
    "rows": 50, "cols": 64,           // must match capture geometry
    "title": "app - feature",
    "labels": {"before": "BEFORE", "after": "AFTER", "solo": "NEW"},
    "intro_caption": ["Same file, same width", "only the rendering changed"],
    "outro_caption": ["app", "github.com/you/app"],
    "timing": {"intro": 2.1, "zoom": 0.8,
               "hold": 1.8, "pan": 0.4, "outro": 1.0},
    "theme": {"after": [74, 222, 128]},   // any of bg, caption_bg, panel_border,
                                          // muted, fg, before, after
    "annotations": [                  // one beat each
      {"rows": [3, 7],                // cells, half-open: 3..6, so last_row + 1
       "cols": [0.2, 62.5],           // optional, defaults to the full width
       "head": "Short claim",
       "sub": "the mechanism, lower case"}
    ]
  },
  "encode": {"crf": 18, "preset": "slow"}
}
```

Duration is `intro + zoom + n*hold + (n-1)*pan + outro`; three annotations at
the defaults gives 10.1s. Set any timing to `0` to drop that beat.

Two things worth getting right when authoring: capture more rows than fit on
screen, since the clip pans vertically over the capture; and park the caret on a
**blank** line, because editors often reveal the raw source of the caret's line
in a rendered mode, which reads as a rendering flaw to anyone who does not know
the app.

`lib/render.py` holds the storyboard: one `frame(n)`, driven by `phase(t)`
mapping a timestamp to `(zoom, annotation index, pan, outro)`.
