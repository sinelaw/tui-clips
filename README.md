# tui-clips

Short annotated videos of a terminal program, from a JSON spec — two builds
side by side, one build on its own, or one screen taken apart into the
elements behind it. Or, when what you have to explain is a set of numbers
rather than a screen, a donut the camera reads one section at a time.

## Quick start

Needs `Xvfb`, a terminal (`xfce4-terminal` or `xterm`), `xdotool`, `xwd`
(from `x11-apps`), `ffmpeg`, and Python with Pillow. ImageMagick is optional —
only the `import` backend uses it. Runs headless: no X session, and nothing
touches the terminal or editor you have open.

```sh
./bin/tui-clip ~/repos/fresh/scripts/clips/fresh-markdown-compose.json
# -> out/<name>.mp4
```

A spec goes stale when the program it films changes its layout, so specs live
with the program, not here. The worked examples below are fresh's, in
[`scripts/clips`](https://github.com/sinelaw/fresh/tree/master/scripts/clips);
a spec path is any path, so yours can live wherever its subject does. The one
exception is a donut clip, which films nothing and so has nothing to go stale
against — [`examples/memory-breakdown.json`](examples/memory-breakdown.json)
is a complete one, and runs with no X and no terminal:

```sh
./bin/tui-clip examples/memory-breakdown.json --draft        # ~40s
./bin/tui-clip examples/memory-breakdown-ansi.json --draft   # the same, printed
```

About 75s: ~25s capturing, ~40s rendering frames, ~10s encoding. While
iterating:

```sh
./bin/tui-clip mine.json --stills        # capture only, check framing
./bin/tui-clip mine.json --skip-capture  # re-render from cached captures
./bin/tui-clip mine.json --draft --skip-capture   # ~40s instead of ~4min
./bin/tui-grid out/mine/after.png --rows 50 --cols 64 --parts 2
./bin/tui-probe                          # what this machine's terminal does
```

`tui-grid` overlays a numbered row/column grid on a capture. Annotation bands
are written in those same cells, so you read the numbers straight off it.

## Four shapes

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

**Donut** — no panes at all, and a `render.donut` section carrying the
numbers, drawn smoothly or printed into a character grid. The ring draws
itself in, stands as a labelled whole, and is then
read one section at a time: the camera lands on the largest share, and pans
and zooms to each next one down. See
[`examples/memory-breakdown.json`](examples/memory-breakdown.json).

The first three film a screen and annotate what is in it. This one has no
screen to film: a memory breakdown, a startup-time budget, a size-on-disk
report is a set of numbers, and the picture has to be drawn rather than
captured. Which is what makes the camera free — every frame is drawn at the
scale it is seen at, so a section can fill the frame as cleanly as the ring
did, with none of the resampling a zoom into a screenshot pays for.

## What a screenshot is for

Three things about filming a terminal were learned by filming one, and none of
them are guessable from the code. They are the reason the capture is shaped the
way it is, so they come before the API that follows from them.

They were also all learned on *one* stack — Xvfb with xfce4-terminal — and one
of them turned out not to hold on the next stack we tried. So before trusting
any of it, measure:

```sh
./bin/tui-probe --term xterm --display :97
```

`tui-probe` films a known animation and reports what a frame costs through each
backend, how much of the animation an `x11grab` recording actually contains, and
whether stopping the grabbing makes the terminal fall behind. Its verdicts say
which of the sections below apply to you.

### A screenshot is not an observation, it is the clock

On the stack this tool was built for, the terminal window does not repaint on
its own schedule. It repaints when an X client calls `XGetImage` on it. A
4-second `ffmpeg -f x11grab` recording of a visibly animating TUI yielded three
distinct images, and all three coincided with an unrelated screenshot being
taken elsewhere. Stills four seconds apart differed by 160,000 pixels, so the
motion was certainly real — the recording simply could not see it. It was not
the emulator (xterm behaved the same), not the window manager (openbox did not
help), and not load (the box was idle). `xrefresh` does force repaints but
blanks alternate frames, so it is not usable as a shutter.

Where that is true, screenshot-driven capture is not a fallback for `x11grab`
— it is the only instrument that works, because the screenshot is what makes
the frame exist.

**It was not true on the machine this paragraph was written on.** There, Xvfb
with xterm gave 33 distinct frames from an x11grab recording of 34 animation
steps, and `x11grab` is a perfectly good backend. Both stacks are real, which
is why `backend` is a spec key and `tui-probe` exists.

### `xwd` is the cheap shutter

Unlike the other two, this one held on both stacks we measured, because it is a
property of the format rather than of the compositing. `xwd` writes the
server's raw bytes instead of encoding a PNG:

| window | `xwd` | ImageMagick `import` | |
|---|---|---|---|
| 964×580 | 7 ms | 50 ms | measured here |
| 1540×1140 | 29 ms | 128 ms | measured here |
| 2382×1142 | 123 ms | 295 ms | the original report |

The ratio differs by stack — 2.4× there, 4–7× here — but the direction does
not, and at the larger sizes it is the difference between sampling an animation
at 30fps and stepping over it at 8. Run `tui-probe` for your own numbers. So `xwd` is the default backend, the dumps are decoded by a reader of
our own (`lib/xwdfile.py`, pixel-identical to ImageMagick's decode), and
**nothing is encoded until the sequence is over** — an encode between two frames
lands in the middle of the motion being filmed, which is the one place it must
not go.

### The terminal replays a backlog

The subtlest of the three, and the one that cost the most time. Where the first
finding holds, what a repaint paints is not "now" — it is the next chunk of the
program's queued output. **Stop grabbing and you fall behind**, and the next
burst opens on a screen from a second ago.

It showed up as a bug that was not there. In the orchestrator clip the dock's
active-session highlight appeared to flicker between two rows around the nine
second mark. It never did. Switches were landing a whole recorded run late, so
consecutive runs disagreed about which row was active, and the eye read the
disagreement as a bounce.

So grabbing does not start or stop with a run. It runs from the first step to
the last. Frames outside a recorded run go to a single path that is overwritten
every time — they are not wanted as pictures, only as the asking that keeps the
queue empty. A `record` step decides *which frames are kept*, never *whether
grabbing happens*.

This one also did not reproduce on the machine this was written on: a 2.5s gap
with nothing asking for pixels cost nothing, and the first grab after it was
current. Continuous grabbing is harmless where it is unnecessary, so it is the
default either way and a spec stays portable.

### Let the camera find the event

Even with the queue drained, the frame a keystroke lands on moves by a few
hundred milliseconds between takes. A 1.6s window aimed by `sleep` misses often
enough to matter, and a window wide enough to be safe is too wide to use.

So aim wide and cut afterwards. `keep` grabs a long window across the keystroke
and then keeps N frames around the largest frame-to-frame change in it — which,
in a window whose only event is the switch, *is* the switch:

```jsonc
{"record": "switch", "seconds": 6, "keep": 18, "anchor": "max-diff"}
```

Frames are compared as 240×120 grayscale, by mean absolute difference. The kept
window is anchored asymmetrically — about a third of the frames before the
event and two thirds after — because the eye needs a moment of the old screen to
register that it *was* the old screen, and rather longer of the new one to read
it.

`anchor` picks what counts as the event. `max-diff` (the default) is right for a
wipe and wrong for a long scroll, where the largest single change is arbitrary
and `sustained` — the densest run of change — is what you want:

| `anchor` | finds |
|---|---|
| `max-diff` | the largest single frame-to-frame change. A wipe, a switch, a modal opening. |
| `sustained` | the densest run of change. A scroll, a long redraw. |
| `first-change` | the first change within half of the largest. When the event starts the motion. |
| `last-change` | the last such change. When the event ends it. |

## Recording: what a run is

`{"shot": ...}` photographs one screen. A screen that is moving needs a run:

```jsonc
"keys": [
  {"record": "wave", "seconds": 16, "fps": 30},   // opens a run, does NOT wait
  {"sleep": 1},                                   // ... so this second is kept
  {"key": "F9"}                                   // ... and so is what it starts
]
```

**A run does not block.** That is the whole point, and it is a genuinely
different execution model from "record this block": the steps after the
`record` — a sleep, the keystroke, another sleep — execute *inside* the window,
which is the only way a keystroke and the animation it causes end up in one
run. The sequence waits for any open run at the end.

A beat plays a run by naming it as a string:

```jsonc
{"shots": "wave", "hold": 16, "head": "...", "sub": ["...", "..."]}
```

A `shots` **array** steps through named stills across the dwell; a `shots`
**string** plays a recorded run straight through. Give the beat a `hold` equal
to the recording's `seconds` and it plays at the rate it was filmed at — or, if
the run was trimmed with `keep`, give it a hold long enough to read the
trimmed frames at. Frames are opened as needed rather than held in memory: a
few seconds at 30fps is several gigabytes decoded, and each frame is wanted for
about two output frames and then never again.

`"backend": "x11grab"` opts out of all of this and streams with ffmpeg, as
older versions did. It turns the continuous grab off, and `keep` and the
pointer path do not apply. Use it if `tui-probe` says your stack supports it and
you would rather have ffmpeg's timing than a grab loop's.

## The pause after a key

`key_settle` is 0.12s where grabbing is continuous, not the 1.2s earlier
versions used. The animation the orchestrator clip was filming lasted 180ms —
seven times shorter than the pause that used to follow the keystroke causing
it. A settle long enough to be safe for a screen you are photographing is long
enough to hide everything worth filming on a screen you are not.

The default follows the capture model rather than taste. Under `x11grab` there
is no continuous grab and a `shot` is the only observation there is, so it has
to land on a settled screen and the old 1.2s stands. **Porting a spec written
against the old default**: its `shot` steps used to be shielded by that 1.2s.
If one now catches a screen mid-redraw, either set `"key_settle": 1.2` for the
whole capture or put an explicit `{"sleep": ...}` before the shot — the second
is better, because it says which screen actually needed the wait.

Where a particular step really does need longer, give it its own:

```jsonc
{"key": "Return", "settle": 0.6}
```

`type_settle` is separate and longer (0.4s): a program reacting per character
is still catching up when the last one lands.

## The mouse

Some things a program will only let you do with a pointer — the width of the
orchestrator's dock had no config key and no action, only a drag. Steps are in
cells, like everything else in a spec:

```jsonc
{"drag": {"from_col": 38, "to_col": 24, "row": 12}},
{"click": {"col": 4, "row": 9}},
{"move": {"col": 20, "row": 3}},
{"park": true}
```

A drag presses, moves in eight interpolated steps so a program tracking the
motion is told where the pointer went, releases, and then **parks the pointer
out of frame** — a cursor left sitting over a list leaves a hover highlight on
whatever row it landed on, and the clip then shows a selection nobody made.

X keeps the cursor out of a window grab, so it is not in the captured pixels at
all and a drag otherwise reads as a panel resizing itself for no reason. The
capture writes the path it walked into the run's `frames.json`, and the
renderer draws the pointer from it — interpolated to the output frame rate and
eased across each gesture rather than sampled, with a bump on the press.
`"cursor": false` in `render` turns it off; `{"size": 1.25}` sets its height in
terminal rows.

## Did the take work?

Worth answering before four minutes of rendering, not after. `capture.verify`
names rects in cells; every frame of every run is measured, and the report says
which one was brightest:

```jsonc
"verify": {
  "regions": {"s0": [0,0,20,1], "s1": [0,1,20,2], "s2": [0,2,20,3]},
  "expect_changes": 1,
  "monotonic": true
}
```

For the orchestrator clip the question was "which dock row is brightest in each
frame of each run", and the answer had to step exactly once per switch beat and
never step back. `monotonic` checks it never steps back; `expect_changes` checks
it stepped as often as it should have. `tui-clip` prints the report after each
take and flags a run that failed, alongside the frame count, the achieved fps
and the per-frame change magnitudes.

## Drafts

```sh
./bin/tui-clip mine.json --draft --skip-capture
```

Half size, 30fps, and `{"crf": 30, "preset": "ultrafast"}`, rendered against
the frames you already captured. On the clip this was developed against it is
5 seconds where the full render is 28; the original report measured 40 seconds
against 3m44s on a longer one. Either way the ratio is what matters, and every
framing decision — what a note points at, whether a pane is wide enough,
whether two things collide — is legible at half size.

Every pixel constant comes down with the canvas, so a draft is a faithful
miniature rather than a full-size caption bar over a shrunken picture. The size
is derived rather than left to you: libx264 refuses a dimension that is not
divisible by two, and a chrome draws at 1/`scale` of the canvas and needs that
to divide as well. A chrome draft reduces the upscale instead of the layout, so
the window's own type keeps its proportion to its furniture.

## Staging a clip

A clip should not be able to see, or write to, anything real. Each pane gets
its own XDG directories under the clip's scratch — `XDG_DATA_HOME`,
`XDG_STATE_HOME`, `XDG_CONFIG_HOME`, `XDG_CACHE_HOME`, `XDG_RUNTIME_DIR` — so a
program that keeps history or sessions starts from the same nothing every time
rather than showing a different screen on the second take. `"xdg": false` opts
out; anything in `capture.env` overrides.

`copy_dirs` stages what the clip *should* see: a config, a set of plugins, a
`bin/` of fake tools that answer instantly and identically. Together they are
the supported way to dress a clip without touching a real install.

`LANG` and `LC_ALL` are forced to `C.UTF-8`. A terminal that decodes
box-drawing glyphs as latin-1 renders every one as about three columns and
wraps every line that has any; the failure is silent and looks exactly like a
layout bug in the program you are filming. `"locale"` overrides it.

**Set the scene through the program's own API, not through its UI.** Cutting
four git worktrees through the orchestrator's dialogs would have been about
forty keystrokes, forty chances to desync, and not one of them the thing the
clip was about. Doing it through the program's plugin/scripting interface — one
call each — was decisively better. Drive the setup however the program will let
you script it, and film only the thing the clip is about.

## Typing

A beat may set its `sub` one character at a time, with a caret:

```jsonc
{"shots": "wave", "hold": 16, "typewriter": 0.5,
 "head": "Fresh: The Wave", "sub": ["first line", "second line"]}
```

The value is the share of the dwell the typing takes, so the sentence lands
before the beat does and the rest of the hold is spent reading it rather than
waiting for it. The caret is solid while typing and blinks once it has stopped.
The reveal is spent over the whole paragraph rather than per line, so a long
first line does not race a short second one, and a word only gets its
`misspell` squiggle once it has been typed in full.

Pair it with a recorded run and the clip is one continuous shot: the thing
plays at speed while the explanation writes itself over it.

## Chrome: the window a clip is explained inside

A solo clip may name a `render.chrome`. Instead of the dark ground, the header
band and the caption bar, the frame becomes a period application window: the
capture is composited into its document area as an embedded object, and each
beat's `head` and `sub` are set inside that document. The storyboard does not
change — intro, zoom, one beat per annotation, outro — only where the words go.

```jsonc
"chrome": {
  "style": "word6",                        // the only one so far
  "assets": "~/Documents/fresh-clip-assets",
  "scale": 3,                              // must divide render.size
  "headline_height": 38,
  "body_lines": 2
}
```

A beat may then also carry `bullet` (a Wingdings key, drawn beside the body)
and `misspell` (words to draw Word's red spelling zigzag under). `sub` may be a
list of lines rather than one string.

`"layout": "side"` puts the WordArt in a left column with the body beside it,
instead of a headline across the top. That is for a clip that has to be legible
in a timeline at thumbnail size: the same words get roughly twice the type
size, paid for out of the document's width rather than the capture's. Size it
with `headline_width` (the left column, as a fraction), `header_block` (the
band's height) and `body_size` / `body_lead` / `body_lines`. Whatever the
header leaves is the viewport — so match the capture's geometry to that aspect,
or the camera crops the screen to fit it.

`word6` is Microsoft Word 6.0 on Windows 3.1, and its `head` is set in WordArt
2.0 — dithered gradient face, black outline, a stamped 3D extrusion, and the
sinusoidal baseline its "Wave" style was named for. Everything but the capture
is drawn at logical screen pixels and scaled up with nearest neighbour, so 1px
bevels stay 1px and the UI text keeps the aliased edge of a bitmap font; only
the capture is composited at full resolution, so the terminal stays legible
inside a deliberately chunky window.

The period faces (Wingdings, Impact, Times New Roman, Microsoft Sans Serif,
Marlett) and the toolbar artwork ship with Windows and are licensed with it, so
neither is vendored. `assets` points at a directory holding them — `fonts/` for
the faces, `word6-toolbar.png` and its `.json` index for the buttons. Each
falls back: a missing face becomes a free one, missing buttons become Wingdings
glyphs. The chrome always renders, just less convincingly.

Two defaults change under a chrome. The dimmed band is off unless a beat asks
for `"band": true` — a rectangle drawn over a page reads as a video overlay,
not as a document. And `rows` becomes optional, defaulting to the whole
capture, since a beat that fills the document has no smaller rect to frame.

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
    "term": "xfce4-terminal",         // or "xterm"
    "backend": "xwd",                 // or "import", or "x11grab"; see above
    "grab_fps": 30,                   // continuous grab rate; 0 grabs flat out
    "key_settle": 0.12,               // pause after a key; 1.2 under x11grab
    "type_settle": 0.4,
    "locale": "C.UTF-8",              // forced, or box-drawing glyphs wrap
    "xdg": true,                      // per-pane XDG dirs under the scratch
    "copy_files": ["~/repo/FILE.md",  // copied into a scratch working dir
                   {"src": "~/repo/docs/index.md", "as": "configuration.md"}],
    "copy_dirs": {"~/repo/clip-config": "{scratch}/config"},
    "verify": {                       // did the take work? see above
      "regions": {"s0": [0, 0, 20, 1], "s1": [0, 1, 20, 2]},
      "monotonic": true
    },
    "env": {                          // {scratch} {proj} {pane} expand
      "XDG_DATA_HOME": "{scratch}/data-{pane}",
      "XDG_RUNTIME_DIR": "{scratch}/run"
    },
    "keys": [                         // driven into every pane, in order
      {"key": "ctrl+p"},
      {"type": "toggle compose"},
      {"key": "Return", "settle": 0.6},   // this one needs longer
      {"shot": "rest"},               // a named screen a beat can be drawn on
      {"drag": {"from_col": 38, "to_col": 24, "row": 12}},
      {"record": "switch", "seconds": 6,  // opens a run; does NOT wait
       "keep": 18, "anchor": "max-diff"},
      {"sleep": 1},                   // ... filmed by the run above
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
    "chrome": {"style": "word6", "assets": "~/assets"},  // optional; see below
    "labels": {"before": "BEFORE", "after": "AFTER", "solo": "NEW"},
    "note_size": 54,                          // the callout text, in px
    "cursor": {"size": 1.25},                 // the drawn pointer; false to hide
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

That is the spec for a clip that films something. A donut clip drops the whole
`capture` section and swaps `render.annotations` for `render.donut` — `name`,
`size`, `fps`, `title`, the captions, the theme and `encode` all mean the same
things. See [Donut specs](#donut-specs).

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

## The phosphor pass

`render.crt` puts a tube in front of the whole video — every finished frame,
on the way to disk:

```jsonc
"crt": {"scanlines": 0.34, "gap": 4, "bloom": 0.55, "shift": 3,
        "vignette": 0.45, "curve": 0.11}
```

`"crt": true` takes the defaults, which are those. Five things, in the order
a real one does them:

| key | what it is |
|---|---|
| `shift` | red and blue pulled apart, the convergence error of a three-gun tube |
| `bloom` | the bright parts bled into their neighbours — phosphor glows, it does not stop at the pixel |
| `scanlines` | every `gap`-th row darkened |
| `curve` | the raster bent, corners reaching past the edge and coming back black |
| `vignette` | the corners taken down |

**`curve` is the one that does the work.** Scanlines and a vignette on a flat
rectangle read as a filter laid over a screenshot; bending the raster is what
makes it a thing with glass in front of it, and it brings the rounded-off
corner of the tube with it for free. The first version of this pass had
everything but the curve and was politely described as not visible.

### Powering it off

`"shutdown": 1.5` spends the last second and a half of the clip turning the
tube off instead of holding the last frame and fading:

    the raster squeezes vertically into one over-bright horizontal line
    -> the line shortens to a dot
    -> the dot decays on the phosphor

The order is the whole effect, and so is the brightening: it is the same beam
energy over a fraction of the height, which is why a dying CRT gets *brighter*
as it collapses rather than dimmer. Fading the picture to black instead is
what a power-off looks like to someone who has only ever read a description
of one.

`off_glow` scales the bloom on the dying raster, which is the brightest thing
in the clip by a long way and so gets its own number rather than riding the
`bloom` used for ordinary phosphor. It is thrown at two radii: a tight halo
for the shape and a wide one for the wash on the glass. One blur wide enough
to give the wash loses the hard edge of the line; one tight enough to keep
the edge does not spill at all.

The dot at the end is drawn with an explicit halo rather than a blurred one,
and the reason generalises. **A Gaussian conserves energy**, so blurring a
five-pixel dot over a fifty-pixel radius returns something arithmetically
correct and visually black. The squeezed raster and the line are large enough
sources to survive that treatment; the dot is not, and it is the frame
everybody remembers about a CRT going off. Anywhere else a small bright thing
needs to glow, blur is the wrong instrument — composite a sprite.

It takes the end of the clip rather than adding to it, so whatever is left of
the last beat in front of it is the final hold — lengthen that beat if you
want longer on the finished screen, and remember that shortening `shutdown`
moves the collapse *later*, not the ending sooner.

Keep the comb light. A deep one is the first thing h.264 turns to mush, and a
one-pixel line every three pixels is a moire pattern once the video is scaled
down a feed — so `gap` comes down with the canvas like every other constant
here, and the masks are built once rather than per frame, which is most of
what the pass would otherwise cost.

## Notes: saying it beside the thing

**Say it in the frame, not in a bar under it.** A caption bar across the foot
of the video is read last or not at all. A beat with a `note` — a named rect
framed, everything else dimmed, three or four words on a plate with a leader
line back to the frame — is read first, because the picture is doing the
explaining. "agent is running", "switch between sessions", "diff and full IDE"
beat any sentence anyone wrote for the bar. Treat the note as the default and
the caption bar as the exception.

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
screen -- and no `sub` at all. Both are optional, and when *no* beat has
either, the bar is not drawn: the viewport takes its height, and the intro and
outro captions float over the ground the vignette has already darkened. A strip
of empty chrome across the bottom of every frame is worse than no strip.

A note is independent of `band`. It used to be drawn with the band, so a beat
that set `"band": false` -- which is what a beat whose point is pure motion
does -- silently lost its note along with the dimming, and the words had to go
on the beats either side, describing the movement before and after the frames
that showed it. The two are now separate: `band` controls the dimming and the
frame, and a note is drawn whenever the beat has one.

`render.note_size` is the text size in pixels, 30 by default. Two words at 54
carry across a phone; a sentence at 30 does not. The room the fit reserves
follows the font rather than being a constant, which it was until the font
became a knob and the constant was quietly a second, disagreeing one.

## Tags: words that name the beat rather than point at it

A `note` points. It is anchored to the beat's rect and draws a leader back
to it, which is exactly right when the words single out one row and wrong
when they name the whole beat -- there the leader has nothing to single out,
so it just crosses the picture, and the plate has to sit wherever the rect
puts it.

A `tag` names instead. It takes a fixed place in the frame, wraps to its own
column, and draws no leader at all:

```jsonc
{"tag": {"text": "organize into folders", "at": "center-right",
         "width": 0.40, "size": 58, "color": "after", "bg": true}}
```

`at` is `center|top|bottom` crossed with `left|right` (default
`center-right`), `width` is the wrapping column as a share of the frame, and
`size` overrides `render.note_size`. `color` and `bg` each take a theme key
(`after`, `fg`, `bg`, …) or an explicit `[r, g, b]`; `bg: true` uses the
theme's ground.

Without a `bg` the words are stroked in the ground colour instead: enough to
hold the letterforms apart from whatever is behind them, without drawing a
box that reads as a second window. Over a screen that is *itself* text a
stroke is not enough — the rows keep showing between the letters, which reads
as two things in one place — so give it a fill.

A tag may carry emoji anywhere in its text, not only at the front the way a
note's does — `"too many sessions? 😰"`. They are composited rather than set,
because Noto's colour emoji are bitmaps cut at exactly one size and cannot be
a second font in a text run, and the wrap measures them as their drawn width:
a line ending in one otherwise wraps as though the emoji were zero wide.

A tag is placed against the **frame**, not against the camera's panel. The
panel is whatever size the current scale makes it and is pasted at an offset,
so a tag set flush to the panel's edge lands off-screen the moment the camera
is zoomed in far enough for the panel to be wider than the frame.

Unlike a note, a tag survives the travel between beats -- a beat's name
should not blink off while the screen it names is arriving.

## Wiping one beat onto the next

`"transition": "wipe"` drags a hard edge across the frame instead of
dissolving or pushing, with a bright leading bar in the beat's `tone`. It is
the beat-level twin of [`swipe`](#swiping-one-screen-onto-another) and it is
for the same reason: a dissolve between two screens of the same list reads as
a smear, and a `push` slides the whole picture sideways, which says *another
screen* when what happened is *this screen, changed*. It takes
`timing.wipe`, falling back to `timing.push`.

Two tags either side of a wipe do not cross-fade. Each is clipped to its own
side of the moving edge, so the words are replaced in place exactly as the
screen under them is -- which is what you want when both tags sit at the same
spot, and that shared position is the point of them.

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

## Meeting a schedule the program keeps

A spec that photographs a program stepping through states of its own has two
clocks in it, and a run of `sleep`s cannot hold them together. Every shot
costs a grab and every key costs its settle; none of that is in the numbers
the spec wrote down, so the sequence lands progressively late. Over
twenty-five shots of the orchestrator dock that came to more than a whole
step, and the last few shots all photographed the same finished screen --
still all *distinct*, so nothing looked broken until the render.

Two steps fix the drift:

```jsonc
{"mark": true},          // start the sequence clock here
{"at": 12.13},           // be at 12.13s past the mark, however long the
{"shot": "mv00"}         // steps in between actually took
```

`at` sleeps to a deadline rather than for a duration, so the lateness cannot
accumulate; a step that misses its deadline by more than 150ms says so on
stderr rather than quietly shifting everything after it.

That is enough when the program's schedule is known. It is not enough when
the *anchor* is a guess -- here, how long the keystroke that launches the
program takes to become a running process. Measured at 1.2s every shot came
back one step late; corrected to 2.3s the middle of the sequence lined up and
the ends still did not, because the error was never a constant.

**Where the program can be made to follow the camera instead, do that.**
`shot` writes its raw dump into the shots directory at the instant it grabs
and only encodes at the end of the take, so a new `.xwd` appearing there *is*
the shutter, visible to any process that can watch the directory. A program
that waits for one before making its next change cannot be photographed
mid-step, and the spec's sleeps stop having to be aimed at anything -- they
only have to be longer than one step's work. fresh's `fresh-dock-cleanup-focus`
drives its agent this way; `scripts/clips/assets/fresh-dock-cleanup/bin/clip-agent`
is the worked example.

## Swiping one screen onto another

Two screens that differ only in the *text of some rows* -- a rename, a
retitle, a units change -- must not be dissolved into each other. Through the
middle of a dissolve both strings are legible at once and the eye cannot tell
which one is arriving; it reads as a blur, not as a change. A wipe with a hard
edge can only ever show one of them per pixel:

```jsonc
{"shot": "before", "hold": 3.0,
 "swipe": {"to": "after", "rows": [5, 6, 7, 8, 9, 11, 12],
           "at": 0.35, "row": 0.20, "stagger": 0.10, "edge": 3}}
```

The beat is drawn on `shot`, and each row named in `rows` wipes across to the
same row of `to`, left to right. `at` is when the first row starts, `row` is
how long one row takes, `stagger` is the gap between one row and the next --
all in **seconds off the beat's `hold`**, not shares of it, so a cascade keeps
its cadence when the beat is retimed. `edge` is the width in pixels of the
leading bar drawn at the wipe front (`edge_color` overrides the theme's
`after`); `0` turns it off.

Rows are wiped in the order given, not in the order they sit on screen, and a
row not named is never touched -- which is what keeps a folder header from
flickering under a cascade running past it. The two screens have to be the
same capture geometry, which for two `shot`s of one take they always are.

The stagger is the part that matters. All the rows wiping at once reads as one
repaint; a tenth of a second between them reads as a sequence of separate
edits, which is usually what actually happened.

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

## Donut specs

A donut clip has no `capture` section at all — nothing is filmed, and the
picture is the numbers. `render.donut.items` is the whole of it: a `label`, a
`value`, and the one-line `note` that says what the thing *is*.

```jsonc
"donut": {
  "unit": "MB",                // appended to every value
  "total": "412 MB",           // what stands in the hole; default is the sum
  "total_label": "resident",
  "sort": true,                // rank by value, largest first (the default)
  "start_angle": -90,          // 12 o'clock, read clockwise
  "thickness": 0.40,           // the band, as a fraction of the outer radius
  "gap": 1.0,                  // degrees of ground between two sections
  "style": "ansi",             // optional; "smooth" (the default) or "ansi"
  "peel": [ ... ],             // optional; drop sections stage by stage
  "max_zoom": 7.0,             // a rail on how close a section may be read
  "dim": 0.68,                 // how far the other sections go back
  "items": [
    {"label": "Syntax trees",  // beside the ring, and on the card
     "value": 148,
     "note": "one per buffer, kept whole so an edit reparses a subtree",
     "display": "148.2 MB",    // optional; overrides value + unit
     "color": [74, 222, 128],  // optional; else the next colour off the ramp
     "hold": 3.4,              // optional; else timing.hold
     "visit": false}           // drawn and labelled, but never dwelt on
  ]
}
```

Timings are `grow`, `intro`, `move`, `hold`, `regroup`, `outro`. Duration is
`grow + intro + (move + hold) per visited item + regroup + outro`; six items at
the defaults gives 27.3s. Set any to `0` to drop that beat.

There is nothing to capture, so `--stills` and `--skip-capture` have nothing to
do and a run is all render: about 40s for a `--draft`, a few minutes at full
size. Every frame is drawn from scratch at three times the size and brought
back down, which is what buys the arcs an edge that does not crawl — so the
draft is where the framing gets checked, as it is for every other shape.

**There is no caption bar.** The other shapes put a headline and a line of
detail along the bottom of the frame. A donut does not: the camera has just
spent half a second putting one section in the middle of the frame, which is
where the reader is looking, and a strip of words along the bottom is a second
place to look for what the thing in the middle is already about. It gets read
after the picture has been understood, if at all.

So an item's whole annotation — name, value, share, and the `note` that says
what the thing *is* — is on the card beside its section. The only words that
go under the ring are `intro_caption` and `outro_caption`, centred directly
beneath it, and they are only up while the camera is still.

**The card decides the framing, not the other way round.** A section and its
card are one object: the card is dealt out along the radius, so it is always
clear of the ring and always on the far side of the section from the middle,
and the camera frames the pair — then slides along that radius until the card
is against the corner of the frame, or the section is against the opposite
edge. What that spends is the hole, which goes off screen on most beats. It is
the right thing to spend: the middle of the ring is the one part of the
picture that is not about the section being read.

The scale has to be solved for rather than computed, because the card is a
fixed size in pixels and so its size *in the picture* depends on the scale
that framing the picture produces. Iterating from the scale the section alone
would take — an upper bound, since the card only ever makes the box bigger —
walks down to the fixed point in a few steps. A small section therefore ends
up read closer than a large one, and `max_zoom` is a rail rather than a
working limit: the card is a fixed share of the frame however small the
section is, so a sliver can never fill the frame with flat colour.

**Keep labels short.** How big the ring can be is decided by the longest
label: the words are a fixed size in pixels, and they have to fit between the
ring and the edge of the frame, so a long name costs every section its radius.
Names go around the ring, explanations go in the `note`.

**The reading order is the ranking.** Sorted largest first, the clip answers
"what is using the memory?" in the order the question is asked. `"sort":
false` keeps the order you wrote, for a breakdown where that order means
something — a timeline, a pipeline, a call stack.

**Nothing but the ring is ever carried by the camera.** The labels around the
figure and the card beside a section are laid out once in frame coordinates
and cut in and out over `LABEL_FADE` (0.22s) around the camera — up only while
it is sitting still, gone before it starts again. Laid out in the world
instead, a zoom deals six labels outwards across the frame and off it, which
the eye reads as *the labels* doing something at the one moment the camera is
what is supposed to be moving. There is no frame in which a label or a card is
both visible and in the wrong place.

The camera also pulls back a little over the middle of every travel rather
than sliding flat across: two sections on opposite sides of the ring are a
long way apart once you are close to one, and lifting away and settling again
puts the whole ring back on screen at the midpoint — which is where a reader
who has lost their place gets it back. A little, because the travel is short:
a deep arc crossed in half a second is a lurch rather than a lift.

## Peeling a donut

A breakdown is sometimes an argument about what to leave out. `render.donut.peel`
makes that the clip: read a few sections, throw them out of the ring, let the
rest grow into the gap, and say what the ring is now. Repeat. See
[`examples/fresh-source-peel.json`](examples/fresh-source-peel.json), which
takes fresh's 908k lines down to the 363k that are neither tests nor
TypeScript.

```jsonc
"peel": [
  {"read": ["End-to-end tests", "Inline unit tests"],   // dwell on these ...
   "drop": ["End-to-end tests", "Inline unit tests"],   // ... then lose them
   "gather": ["51% tests", ""],        // the caption while they are still lit
   "title": "code only (no tests)",    // what the ring is once they have gone
   "total": "445k lines"},             // ... and what it adds up to
  {"read": ["Plugins (TS)"], "drop": ["Plugins (TS)"],
   "title": "core only (rust)", "total": "363k lines"},
  {"read": ["Editor app", "Rendering & UI"]}    // the last ring, read as-is
]
```

**Each stage re-normalises, and that is the whole point.** A section that was
14% of everything is 34% of what is left once two thirds of the ring has gone.
Nothing is scaled: the survivors are divided into 360° again, so the ring
closes. If a peel only shrank the ring it would be a subtraction, and there
would be no reason to animate it.

**Four beats to a drop, and they are separate on purpose.** `gather` lights
what is about to go while it is still in place, so the reader sees what is
being taken; `eject` slides it out along its own radius and past the frame,
fading as it goes; `reflow` is the only beat in the clip where a surviving
section's angles move, so nothing competes with the one thing worth watching;
and `settle` stands still long enough for the new ring to read as a ring
rather than as the end of a move. Timings are `gather`, `eject`, `reflow`,
`settle`.

A section's angles live on the item and are rewritten every frame, which is
why none of the drawing had to learn what a stage is — everything that draws a
section already read them from there. What did need saying once per stage is
everything derived from an angle: the labels around the ring, the cards (a
share is a share *of* something, and the something shrinks), and the camera
that frames a section together with its card.

## The printed donut

This repo is about terminals, and a donut drawn with polygons is the one shape
in it that does not look like one. `"style": "ansi"` prints it instead: the
ring becomes a grid of characters, the card becomes a box-drawn panel titled in
its own top rule, the header becomes a status line in reverse video, and the
two places that want bigger type get a double-height line — DECDHL, the only
way a real terminal ever had two sizes at once. See
[`examples/memory-breakdown-ansi.json`](examples/memory-breakdown-ansi.json).

```jsonc
"donut": {
  "style": "ansi",
  "charset": "unicode",      // a named set, or the characters themselves
  "cell": 14,                // the type cell's width; its height is twice it
  "focus": [36, 8],          // the chart's cell at either end of the rack focus
  "focus_time": [0.42, 0.28],
  "sharp_at": 0.70,          // past this the grid dissolves into the real thing
  "scanline": 0.88,
  "bloom": 0.40,
  "items": [ ... ]           // everything else is as it is for a smooth donut
}
```

It is a subclass rather than a second renderer, and that is the design: the
storyboard, the camera solve, the card placement and the label layout are the
ones a smooth donut uses, and only the drawing is replaced.

**The chart is dithered, not classified.** The ring is drawn the ordinary way
first — the polygons the smooth renderer already gets right — and *that* is
reduced to the grid. There is no per-cell "which section covers me?" left to
get wrong, which is what a boundary cell, and a section slid out of the ring,
both get wrong. Alpha is coverage, the colour channels are the sections
premultiplied by it, and the residual is error-diffused Floyd–Steinberg.

**A character is matched on where its ink is, not how much.** Each one is
rendered at the cell size, reduced to a 4×6 grid of its own ink and compared
against the cell's. Ranking a set by darkness and picking by darkness alone is
the classic trick and it is exactly right for `shades`, where every character
is uniform — but `'` and `_` ink about the same share of a cell in completely
different parts of it, and `/` and `\` are one glyph mirrored. Matching the
grid is what lets an edge running down-and-left pick `/` and a flat bottom pick
`_`. The named sets are `shades`, `dots`, `ascii` (all 95 printable), `ansi`
(CP437's shades, blocks and box rules), `blocks` and `unicode`; `charset` also
takes a bare string, since a set is only ever a string — every character in it
is measured off the font at the size it will be printed.

**The colour is snapped to the ring's own palette**, by the *direction* of the
premultiplied colour rather than by dividing it back out. Dividing divides by a
small number exactly where the rounding on it is worst, and a faint edge cell
comes back grey, or some hue the chart does not contain.

**The rack focus is why it moves well.** A beat the camera holds still for
pulls the grid from 36px cells down to 8px, and then past `sharp_at` the grid
stops getting finer and dissolves into the picture it was standing for —
running the cell down to a single pixel arrives at the same place and costs a
hundred times as much. Before the camera moves it runs backwards. Travels and
the opening sweep are coarse throughout, which is not decoration: a *moving*
picture on a fixed grid crawls, because cells pop between glyphs as the shape
slides under them and the eye reads the popping rather than the motion. Nothing
is ever both sharp and in motion, so there is nothing to crawl.

It costs about 0.3–0.6s a frame against the smooth style's 0.1s, nearly all of
it in the dither, so a full-size clip is closer to ten minutes than three.
Glyph masks and match results are cached per cell size — a font hints its
glyphs differently small than large, and the focus runs through a dozen sizes —
and the caches outlive the frame, so only the first one pays.

## Storyboards

`lib/render.py` holds all three. `Renderer` is one `frame(n)` driven by
`phase(t)`, mapping a timestamp to `(zoom, annotation index, pan, outro)`.
`ExplodeRenderer` builds an explicit list of segments up front, each with a
camera move and a caption; a piece's position at any moment is its own rect
plus every ancestor's offset, weighted by how far that ancestor has come
apart. `DonutRenderer` builds a segment list the same way, but draws rather
than crops: a camera is `(pixels per world unit, x, y)`, the ring is a set of
annulus polygons drawn at that scale into a supersampled layer, and a scale is
interpolated in log space so that halfway between 1× and 4× is 2× rather than
2.5×. Everything that is not the ring — labels, the total in the hole, the
establishing caption, each section's card — is placed once in frame
coordinates, by `_place_figure` and `_section_view`, and never moves. The header strip and the caption bar are `Furniture`, shared
because two storyboards that draw the same frame separately draw it a couple
of pixels apart; a donut wears the header only. `AnsiDonutRenderer` subclasses
`DonutRenderer` and replaces only its drawing methods, which is why the two
donut styles cannot drift apart on anything but ink.
