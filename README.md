# tui-clips

Short annotated videos of a terminal program, from a JSON spec — two builds
side by side, one build on its own, or one screen taken apart into the
elements behind it.

## Quick start

Needs `Xvfb`, `xfce4-terminal`, `xdotool`, ImageMagick (`import`), `ffmpeg`, and
Python with Pillow. Runs headless — no X session, and nothing touches the
terminal or editor you have open.

```sh
./bin/tui-clip ~/repos/fresh/scripts/clips/fresh-markdown-compose.json
# -> out/<name>.mp4
```

A spec goes stale when the program it films changes its layout, so specs live
with the program, not here. The worked examples below are fresh's, in
[`scripts/clips`](https://github.com/sinelaw/fresh/tree/master/scripts/clips);
a spec path is any path, so yours can live wherever its subject does.

About 75s: ~25s capturing, ~40s rendering frames, ~10s encoding. While
iterating:

```sh
./bin/tui-clip mine.json --stills        # capture only, check framing
./bin/tui-clip mine.json --skip-capture  # re-render from cached captures
./bin/tui-grid out/mine/after.png --rows 50 --cols 64 --parts 2
```

`tui-grid` overlays a numbered row/column grid on a capture. Annotation bands
are written in those same cells, so you read the numbers straight off it.

## Three shapes

**Comparison** — panes named `before` and `after`. Both captures appear side by
side, the BEFORE panel swipes out left, the AFTER zooms to full frame, then one
annotated beat per callout. See fresh's `fresh-markdown-compose.json`.

**Solo** — one pane, any name, for a feature with nothing to compare against.
The capture opens centred as an establishing shot, grows in place to fill the
frame, then the same annotated beats. See fresh's `fresh-markdown-compose-solo.json`.

**Explode** — one pane, plus a `render.explode` section naming rects on the
screen. The capture comes apart: every named piece is cut out of it and slides
away, and the camera then visits each piece in turn. A piece that has pieces of
its own bursts open in place when the camera reaches it, so a screen can be
taken apart down as many levels as it has. See fresh's `fresh-ui-anatomy.json`.

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
    "copy_files": ["~/repo/FILE.md",  // copied into a scratch working dir
                   {"src": "~/repo/docs/index.md", "as": "configuration.md"}],
    "env": {                          // {scratch} {proj} {pane} expand
      "XDG_DATA_HOME": "{scratch}/data-{pane}",
      "XDG_RUNTIME_DIR": "{scratch}/run"
    },
    "keys": [                         // driven into every pane, in order
      {"key": "ctrl+p"},
      {"type": "toggle compose"},
      {"key": "Return"},
      {"shot": "rest"},               // a named screen a beat can be drawn on
      {"key": "Next"}
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
    "note_size": 54,                          // the callout text, in px
    "title_card": {                           // a card before the clip
      "lines": [{"text": "Horrible Code", "effect": "sick", "at": 0.04},
                {"text": "to", "small": true, "at": 0.34},
                {"text": "Declarative", "effect": "shine", "at": 0.50}],
      "footer": {"added": 63164, "removed": 27662, "at": 0.62,
                 "note": "the scale this is one example of"}},
    "views": {"old": {"rows": [2, 24], "cols": [0, 62]}},   // camera rects
    "intro_caption": ["Same file, same width", "only the rendering changed"],
    "outro_caption": ["app", "github.com/you/app"],
    "timing": {"title": 0, "intro": 2.1, "zoom": 0.8,
               "hold": 1.8, "pan": 0.4, "push": 0.9, "outro": 1.0},
    "theme": {"after": [74, 222, 128]},   // any of bg, caption_bg, panel_border,
                                          // muted, fg, before, after
    "annotations": [                  // one beat each
      {"rows": [3, 7],                // cells, half-open: 3..6, so last_row + 1
       "cols": [0.2, 62.5],           // optional, defaults to the full width
       "shot": "rest",                // optional; else the final capture
       "camera": "fit",               // optional; frame the rect, not the width
       "view": "old",                 // ... or sit on a named rect and not move
       "hold": 4.4,                   // optional; else timing.hold
       "head": "Short claim",
       "sub": "the mechanism, lower case"},
      {"shots": ["rest", "s1", "s2"], // a run of screens, stepped across the hold
       "band": false,                 // no dimming, no frame: the screen alone
       "rows": [19, 42],              // still steers the camera
       "head": "It follows", "sub": "the movement is the point"},
      {"rows": [8, 13], "camera": "fit",
       "note": "a few words",          // in the frame, beside the rect
       "note_at": "below-right",       // above|below x left|right
       "label": "BEFORE",              // this beat's banner ...
       "tone": "before",               // ... and its colour, a theme key
       "transition": "push",           // shove the last screen out, don't fade
       "head": "where you are"}        // `sub` is optional
    ]
  },
  "encode": {"crf": 18, "preset": "slow"}
}
```

Duration is `intro + zoom + sum(hold) + the travel between beats + outro`,
where the travel is `pan`, or `push` for a beat that asks for one; three
annotations at the defaults gives 10.1s. Set any timing to `0` to drop that beat, or give one
beat its own `hold` when it has more to show than the others.

## Framing a beat

By default the camera fits the capture across the frame and travels only up and
down, which is what a clip of one tall screen wants. `"camera": "fit"` frames a
beat on its own rect instead — centred in both axes, scaled to fill the
viewport — which is how a narrow column at one edge is brought to the middle.
Nothing clamps it back inside the capture: the ground beyond the edge paints as
background, and that emptiness is what says *this is a detail of a bigger
screen*. A tall, narrow rect in a wide frame will leave a lot of it, since the
scale that fits its height is nowhere near the scale that fills the width.

`"view"` names a rect in `render.views` and frames *that* instead, leaving the
beat's own rows and cols to say what the band points at. Beats sharing a view
share a camera, so nothing moves between them but the band and the note — and
that is what a clip of one screen usually wants. A camera that re-frames every
beat asks the reader to find their place again every beat, and re-reading the
same function from a new scale is not what the 1.5s is for; it also never lets
them see the shape of the whole thing, which for a clip about what code *looks
like* is the whole argument. Frame the function once, then point at it.

Give the view a few rows of slack past the code: a note under the last beat is
drawn below its rect, and the fit only leaves `FIT_PAD` outside the view. A
note with nowhere to go flips to the other side of its rect rather than run off
the frame, but slack is the better answer.

## A card before the clip

`render.title_card` puts one up: a few lines, each with a `text`, an `at`
(when in the card it arrives, 0..1) and an optional `effect`. `timing.title`
is how long it holds, and defaults to 2.6s once a card exists.

Two effects, and they are meant as a pair — a before and an after said in
colour before either is said in code. `sick` fills the glyphs with a vertical
ramp between two greens that crawls and will not settle, and jitters the whole
line a couple of pixels: the colour of code nobody wants to touch. `shine`
fills them with cool metal and runs one specular highlight across, built once
as a horizontal profile and sheared rather than evaluated per pixel. A line
with no effect is set smaller, in the muted colour, for the word between.

Every line arrives at its `at`, and setting them all to 0 puts the whole card
up at once -- which is usually what a card wants, the effects being the thing
that moves. The `shine` sweep runs on the card's own clock either way.

A `footer` puts a diffstat under them -- `added` and `removed`, in the theme's
`after` and `before`, which are the colours a diff uses anyway -- with an
optional `note` beneath, set at the same size: the note names what the count
is a count of, and a caption half the size reads as a footnote to the number
rather than as its subject. It is the one line of a card that is a measurement
rather than a claim, which is what makes the claim above it land.

## The edges of a capture

A terminal capture is cut, not framed: the sides end where the text does, but
the rows above and below are the same rows going on, and a hard edge there
reads as a boundary the code does not have. So the subject is vignetted
throughout -- blurred *and* taken back to the ground, since fading alone
leaves the last legible row hanging in mid-air and blurring alone leaves a
smear with a hard edge under it. Deeper on the top and bottom than the sides,
for the same reason. Its strength follows the zoom, so the intro's framed
panel keeps the crisp border it is drawn with. `VIG_X`, `VIG_Y` and
`VIG_BLUR` on the renderer are the knobs.

## Notes: saying it beside the thing

A caption bar has room for a sentence, and a sentence is the wrong length for
pointing at one expression. `"note"` puts a few words in the frame instead:
dealt off the rect's corner so they cover nothing, on a plate so they do not
read as one more line of the program, with a rule down their outer side and a
square-cornered leader back to the rect. The corners are what make it a
pointer -- a straight line to the same place looks like an underline.

`"note_at"` picks the corner (`below-right` by default). The fit reserves room
on the note's own axis and pushes the rect off-centre by half of it; it does
*not* reserve room beside the rect, because the note is dealt sideways off the
leader's landing point and has the frame's width to sit in. Words that would
run off the edge are pulled back in and the leader stretches to meet them.

A note may open with an emoji, and colour ones are drawn from Noto rather than
set as text: they are bitmaps, cut at exactly one size, so they are rendered at
109 and scaled. Install `noto-fonts-emoji` (or equivalent) and they appear; do
not and the words still do.

A beat with a note usually wants `head` alone in the bar -- which file, which
screen -- and no `sub` at all. Both are optional now, and when *no* beat has
either, the bar is not drawn: the viewport takes its height, and the intro and
outro captions float over the ground the vignette has already darkened. A strip
of empty chrome across the bottom of every frame is worse than no strip.

`render.note_size` is the text size in pixels, 30 by default. Two words at 54
carry across a phone; a sentence at 30 does not. The room the fit reserves
follows the font rather than being a constant, which it was until the font
became a knob and the constant was quietly a second, disagreeing one.

## Before and after in one clip

Two things, and the second replaced the first: `"transition": "push"` on a beat
sends the outgoing screen off to the left and brings the incoming one in behind
it, instead of dissolving. Each keeps its own camera through the move, since a
screen caught mid-scale while it is also travelling reads as a stumble -- and
the point of the shape is that one thing *replaced* another, not that one
became it. It takes `timing.push`, which wants longer than a `pan`.

`"label"` and `"tone"` then name each half. A solo clip paints one banner for
the whole run, which is wrong over a before/after; a beat naming its own takes
the header for as long as it is on screen, and the swap rides the travel so it
lands with the screen it describes. `tone` is a theme key (`before`, `after`,
...) and colours the band, the note's rule and the banner together. The
establishing shot takes the first beat's, so a clip that opens on a BEFORE does
not open in the after's green.

## Shots: more than one screen in one clip

A clip is stills with a camera over them, so nothing on screen moves by
itself — but the *screens* need not all be the same one. A `{"shot": "name"}`
step in `capture.keys` saves the screen at that point in the sequence, and a
beat's `"shot": "name"` says it is drawn on that one; a beat without a `shot`
gets the final capture. The pan into a beat cross-fades to its screen, so the
0.5s between two beats is where a caret moves, a list scrolls, or a selection
jumps — the move, shown rather than described. Take a shot after the screen
has settled (a `{"sleep": n}` before it), and remember every shot comes from
one run of one terminal, so they all share the geometry the `render.rows` /
`render.cols` grid describes.

A shot whose name looks like a path stays a path, which is how a run drops a
capture next to a dump of the same screen.

`"shots": [...]` on a beat plays a whole run of them across its dwell, each held
then cut to the next. That is the shape for a repeated action — page down eight
times, screenshot each — where no single frame makes the point and the movement
between them does. Pair it with `"band": false`: a beat whose subject is a thing
moving does not want a rectangle drawn around where it used to be.

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
It wants that report as JSON — one object per element, carrying the rect the
layout gave it, an `id` and a `key` to name it by, `type` and `text` if it has
them, and `children`:

```jsonc
{"id": "E5", "type": "Box", "key": "chrome_column",
 "rect": {"x": 0, "y": 0, "w": 127, "h": 36}, "children": [...]}
```

Getting a program to emit that is the program's own business, and belongs in
its notes rather than here — fresh's are in
[`scripts/clips`](https://github.com/sinelaw/fresh/tree/master/scripts/clips).
One rule generalizes, though: a dump taken from a command you invoked through
some overlay describes the frame with that overlay over everything, so take it
the way the program lets you take it without one.

```sh
./bin/tui-tree tree.json --list                 # every element that has a key
./bin/tui-tree tree.json --list --all --grep pane
./bin/tui-tree tree.json plan.json --into mine.json
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
