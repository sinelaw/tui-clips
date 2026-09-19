"""Count fresh's source by concern, splitting the tests out of it.

Three kinds of line:

  e2e        a file under a `tests/` directory -- an integration test, which
             in Rust is compiled as its own crate against the public API
  unit       a `#[cfg(test)]` block inside a source file, plus any src file
             named test*.rs, which is the same tests moved out for room
  <concern>  everything else, bucketed by path

The test split is the whole reason to do this rather than run `tokei`. Rust
puts a unit test in the file it tests, so a plain line count credits every
subsystem with its own tests and the picture then says nothing about either
one. Pulling them out says something about both.

TypeScript counts too: fresh's plugin system is a JS runtime with its plugins
written in TypeScript, and leaving them out would shrink the one subsystem
that is deliberately not written in Rust.

    python3 examples/fresh-source-survey.py ~/src/fresh
    python3 examples/fresh-source-survey.py ~/src/fresh --spec > out.json

The path rules below are fresh's, and they are the only part of this that does
not generalise: a concern is a judgement about what belongs with what, and
nothing can read that off a directory tree. Everything around them -- the test
split, the counting, the spec it writes -- would serve any Rust workspace whose
concerns you were willing to name.
"""
import json
import os
import re
import sys
from collections import defaultdict

ROOT = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") \
    else os.path.expanduser("~/fresh")
CODE = (".rs", ".ts", ".tsx", ".js", ".jsx")
SKIP_DIR = {".git", "target", "node_modules", "dist", "vendor", ".vitepress"}
SKIP_FILE = (".d.ts", ".min.js")

CFG_TEST = re.compile(r"^\s*#\[cfg\(test\)\]")
MOD_OPEN = re.compile(r"\bmod\s+\w+\s*\{")

# Path rules, first match wins, so the specific comes before the general.
RULES = [
    ("Plugins & JS", (
        "crates/fresh-plugin-runtime/", "crates/fresh-plugin-api-macros/",
        "crates/fresh-parser-js/", "crates/fresh-editor/plugins/",
        "crates/fresh-editor/npm-package/",
        "crates/fresh-editor/src/services/plugins/",
        "crates/fresh-core/src/plugin_schemas.rs",
        "crates/fresh-editor/src/plugin_schemas.rs")),
    ("Language servers", (
        "crates/fresh-editor/src/services/lsp/",
        "crates/fresh-editor/src/services/completion/")),
    ("Syntax & themes", (
        "crates/fresh-editor-core/src/primitives/highlight",
        "crates/fresh-editor-core/src/primitives/reference_highlight",
        "crates/fresh-editor-core/src/grammars/",
        "crates/fresh-editor-core/src/theme/",
        "crates/fresh-editor-core/src/language_detect.rs",
        "crates/fresh-languages/",
        "crates/fresh-editor/src/view/bracket_highlight",
        "crates/fresh-editor/src/view/reference_highlight")),
    ("Rendering & UI", (
        "crates/fresh-ui/", "crates/fresh-gui/", "crates/fresh-winterm/",
        "crates/fresh-editor/src/view/", "crates/fresh-editor/src/gui/",
        "crates/fresh-editor-core/src/widgets/")),
    ("Input & keys", (
        "crates/fresh-input-parser/", "crates/fresh-editor/src/input/",
        "crates/fresh-editor-core/src/keys/")),
    # Config, the daemon and the services around them are one concern from a
    # reader's point of view -- the parts that talk to something outside the
    # process -- and three one-percent slices are a legend, not a picture.
    # The name is short because a label's width sets the ring's radius; what
    # it leaves out, the note says.
    ("Services & IPC", (
        "crates/fresh-editor-core/src/config", "crates/fresh-core/src/config",
        "crates/fresh-editor-core/src/partial_config.rs",
        "crates/fresh-editor/src/server/", "crates/fresh-editor/src/client/",
        "crates/fresh-editor/src/webui/", "crates/fresh-editor/src/wasm/",
        "crates/fresh-editor/web-ui/",
        "crates/fresh-editor/src/services/")),
    ("Text model", (
        "crates/fresh-editor-core/src/model/",
        "crates/fresh-editor-core/src/primitives/",
        "crates/fresh-editor-core/src/wrap_machine.rs",
        "crates/fresh-editor-core/src/markdown.rs",
        "tests/wrap_model/src/")),
    ("Editor app", ("crates/fresh-editor/src/app/", "crates/fresh-core/")),
]
# Everything the rules do not name is folded into one section rather than
# left as a pile: a breakdown with a dozen one-percent slices is a legend,
# not a picture.
REST = "Everything else"

# What each slice is, in one line, for the card the camera stops on. The
# numbers come out of the tree; these do not, and could not.
NOTES = {
    "End-to-end tests":
        "integration crates that drive the whole editor from outside it",
    "Inline unit tests":
        "#[cfg(test)] blocks, living in the file they are testing",
    "Editor app":
        "commands, dispatch, buffers — what the editor does when asked",
    "Rendering & UI":
        "the view tree, the terminal backend, and every widget on it",
    "Plugins & JS":
        "a JS engine, its host API, and the plugins written against it",
    "Services & IPC":
        "the parts that talk outside the process: files, daemon, config",
    "Text model":
        "ropes, wrapping, and the primitives every edit goes through",
    "Input & keys":
        "keys in, actions out, and the router that decides between them",
    "Language servers":
        "LSP clients and completion, talking to other people's tools",
    "Syntax & themes":
        "grammars, highlighting, and the colours they get mapped onto",
    REST: "entry points, the updater, localisation, odds and ends",
}
# Drawn and labelled, but the camera does not stop on it: a section whose
# whole description is "odds and ends" has nothing to say for three seconds.
NO_VISIT = {REST}

# Eleven concerns is three more than the renderer's own ramp has colours, and
# it wraps -- so the 1% syntax slice came out the same amber as the 14% editor
# app. Named here instead. The last three are the tail, and what matters for
# them is not being told apart from the whole wheel but from their immediate
# neighbours, which is all a reader ever compares a 1% sliver against.
PALETTE = [
    [74, 222, 128], [56, 189, 248], [251, 191, 36], [167, 139, 250],
    [244, 114, 182], [45, 212, 191], [248, 113, 113], [148, 163, 184],
    [249, 146, 58], [132, 204, 22], [217, 70, 239],
]


def split_tests(path):
    """-> (code lines, lines inside #[cfg(test)] blocks)

    Brace counting from the `mod` that follows the attribute. Crude, and
    exactly right for the shape Rust actually uses: an attribute, a
    `mod tests {`, and a closing brace back in column zero.
    """
    with open(path, encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()
    code = test = 0
    i = 0
    while i < len(lines):
        if not CFG_TEST.match(lines[i]):
            code += 1
            i += 1
            continue
        j = i
        while j < len(lines) and not MOD_OPEN.search(lines[j]):
            j += 1
        if j >= len(lines):              # the attribute was on something else
            code += 1
            i += 1
            continue
        depth, k = 0, j
        while k < len(lines):
            depth += lines[k].count("{") - lines[k].count("}")
            k += 1
            if depth <= 0:
                break
        test += k - i
        i = k
    return code, test


def concern(rel):
    for name, prefixes in RULES:
        if any(rel.startswith(p) for p in prefixes):
            return name
    return REST


def survey():
    loc = defaultdict(int)
    files = defaultdict(int)
    rest = defaultdict(int)
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIR]
        for fn in filenames:
            if not fn.endswith(CODE) or fn.endswith(SKIP_FILE):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, ROOT).replace(os.sep, "/")
            parts = rel.split("/")
            if "tests" in parts:
                n = sum(1 for _ in open(full, encoding="utf-8",
                                        errors="replace"))
                loc["End-to-end tests"] += n
                files["End-to-end tests"] += 1
                continue
            if fn.startswith("test") and fn.endswith(".rs"):
                # a unit test moved into a file of its own for room
                n = sum(1 for _ in open(full, encoding="utf-8",
                                        errors="replace"))
                loc["Inline unit tests"] += n
                continue
            if fn.endswith(".rs"):
                code, test = split_tests(full)
                loc["Inline unit tests"] += test
            else:
                code = sum(1 for _ in open(full, encoding="utf-8",
                                           errors="replace"))
            c = concern(rel)
            loc[c] += code
            files[c] += 1
            if c == REST:
                rest[os.path.dirname(rel)] += code
    return loc, files, rest


def short(n):
    """a line count as a person would say it out loud"""
    return f"{n / 1000:.0f}k" if n >= 9500 else f"{n / 1000:.1f}k"


def spec(loc, total):
    """the whole donut spec, so the picture cannot drift from the count"""
    tests = loc["End-to-end tests"] + loc["Inline unit tests"]
    items = []
    for i, (k, v) in enumerate(sorted(loc.items(), key=lambda kv: -kv[1])):
        d = {"label": k, "value": v, "display": short(v), "note": NOTES[k],
             "color": PALETTE[i % len(PALETTE)]}
        if k in NO_VISIT:
            d["visit"] = False
        items.append(d)
    return json.dumps({
        "name": "fresh-source-breakdown",
        "render": {
            "size": [1080, 1080], "fps": 60,
            "title": "fresh 0.5.1 — source by concern",
            "intro_caption": [f"{short(total)} lines of fresh",
                              "every crate, Rust and TypeScript alike"],
            "outro_caption": [
                f"{round(100 * tests / total)}% of it is tests",
                f"{short(loc['End-to-end tests'])} end-to-end, "
                f"{short(loc['Unit tests, in place'])} in the files they test"],
            "timing": {"hold": 2.8},
            "donut": {
                "style": "ansi", "charset": "unicode",
                "unit": "lines", "total": f"{short(total)} lines",
                "total_label": "of source", "thickness": 0.40,
                "items": items,
            },
        },
        "encode": {"crf": 18, "preset": "slow"},
    }, indent=2, ensure_ascii=False)


def main():
    loc, files, rest = survey()
    total = sum(loc.values())
    if "--spec" in sys.argv:
        print(spec(loc, total))
        return
    print(f"{'concern':24s} {'lines':>9s} {'share':>7s}  files")
    for k, v in sorted(loc.items(), key=lambda kv: -kv[1]):
        print(f"{k:24s} {v:9,d} {100 * v / total:6.1f}%  {files.get(k, 0)}")
    print(f"{'TOTAL':24s} {total:9,d}")
    tests = loc["End-to-end tests"] + loc["Inline unit tests"]
    print(f"\ntests are {100 * tests / total:.0f}% of it "
          f"({tests:,d} of {total:,d})")
    if rest:
        print(f"\ninside {REST!r}:")
        for k, v in sorted(rest.items(), key=lambda kv: -kv[1])[:10]:
            print(f"  {v:8,d}  {k}")


if __name__ == "__main__":
    main()
