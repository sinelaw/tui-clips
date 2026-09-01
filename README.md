# tui-clips

Short annotated videos of a terminal program, from a JSON spec — two builds
side by side, one build on its own, or one screen taken apart into the
elements behind it.

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

## Three shapes

**Comparison** — panes named `before` and `after`. Both captures appear side by
side, the BEFORE panel swipes out left, the AFTER zooms to full frame, then one
annotated beat per callout. See `specs/fresh-markdown-compose.json`.

**Solo** — one pane, any name, for a feature with nothing to compare against.
The capture opens centred as an establishing shot, grows in place to fill the
frame, then the same annotated beats. See `specs/fresh-markdown-compose-solo.json`.

**Explode** — one pane, plus a `render.explode` section naming rects on the
screen. The capture comes apart: every named piece is cut out of it and slides
away, and the camera then visits each piece in turn. A piece that has pieces of
its own bursts open in place when the camera reaches it, so a screen can be
taken apart down as many levels as it has. See `specs/fresh-ui-anatomy.json`.

Cut out, not faded out: the original does not stay behind the pieces, and a
piece drawn over by another loses those pixels to a hole. A capture is flat, so
an overlay's pixels sit in the image at the rows the things underneath occupy —
cut those out as they are and every one of them carries a copy of the overlay
away with it.

The mode is `explode` when the spec has a `render.explode`, else it is inferred
from the pane set. For the first two, everything after the opening — the zoom,
the dimmed bands, the captions, the outro — is identical.

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

## Explode specs

Instead of `annotations`, an explode clip gives `render.explode.pieces` — a
tree. Each piece is a rect in cells, a short `label` for the chip that rides
with it, and the `head`/`sub` for its beat. A piece with no `head` is drawn and
labelled but never dwelt on, which is what you want for the thirty rows of a
list.

```jsonc
"explode": {
  "spread": 0.4,                 // default push-apart, as a fraction of the
                                 // distance from the container's centre
  "pieces": [
    {"rows": [0, 1], "cols": [0, 127],
     "offset": [0, -3],          // cells; overrides `spread` for this piece
     "label": "Menu bar", "label_at": "above",   // auto|above|below|left|right
     "head": "The menu bar is a region",
     "sub": "it rebuilds only when its own state moves"},

    {"rows": [22, 35], "cols": [0, 127], "offset": [0, 17],
     "label": "Command palette", "label_at": "below",
     "spread": [0, 0.95],        // how this piece's own children come apart
     "stagger": [1.15, 0],       // ... and how far each one is dealt past the last
     "head": "The palette is a layer", "sub": "it floats over the rest",
     "dive_head": "Ten rows, ranked",     // the caption while it is open
     "dive_sub": "only what fits is ever built",
     "pieces": [ ... ]
  ]
}
```

`stagger` is what makes a long list worth looking at: pushing thirty
same-shaped rows apart radially just piles them up along one axis, whereas
dealing each one a little further along than the last opens them into a fan.

Timings are `intro`, `explode`, `survey`, `move`, `hold`, `dive`, `rise`,
`regroup`, `implode`, `outro`. Set any to `0` to drop that beat.

## Where the rects come from

Reading two dozen nested rects off a grid by hand is not worth doing. If the
program can report its own layout, `tui-tree` turns that report into pieces.
`fresh` writes its retained UI tree as JSON — one object per element, with the
rect the layout gave it — from **Dump UI Tree** in the command palette. Get the
JSON out of the editor with `ctrl+a`, `ctrl+n`, `ctrl+v`, then save it.

```sh
./bin/tui-tree tree.json --list                 # every element that has a key
./bin/tui-tree tree.json --list --all --grep pane
./bin/tui-tree tree.json plan.json --into specs/mine.json
```

A *plan* names elements and says what to call them; the dump supplies the
rects, so a font or geometry change costs one re-dump and no edits:

```jsonc
[{"key": "region:2", "label": "Menu bar", "offset": [0, -3],
  "head": "...", "sub": "..."},
 {"key": "region:3", "label": "File explorer", "stagger": [1.15, 0],
  "head": "...", "sub": "...", "dive_head": "...", "dive_sub": "...",
  "children": [{"key": "explorer_row:*", "label": ""}]}]
```

`id` matches the element's id and `key` its key, either exactly or as a glob;
a glob that hits several elements expands to one piece each. Everything else
in an entry passes straight through to the piece.

Two capture options exist for this shape: `workdir` points the program at a
real directory instead of the scratch copy, for something that has nothing to
show without one, and `copy_dirs` brings a config directory along as a copy, so
the capture gets the real look without the run being able to write back over
it. A `{"shot": "path"}` step takes a screenshot mid-sequence, which is how a
capture and a dump of the same screen come out of one run.

## Storyboards

`lib/render.py` holds both. `Renderer` is one `frame(n)` driven by `phase(t)`,
mapping a timestamp to `(zoom, annotation index, pan, outro)`.
`ExplodeRenderer` builds an explicit list of segments up front, each with a
camera move and a caption; a piece's position at any moment is its own rect
plus every ancestor's offset, weighted by how far that ancestor has come
apart.
