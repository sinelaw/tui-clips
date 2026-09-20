"""Count fresh's source by concern, with cloc, splitting the tests out of it.

Four kinds of line:

  code       in a `tests/` directory -- an integration test, which in Rust is
             compiled as its own crate against the public API
  code       inside a `#[cfg(test)]` block, or in a src file named test*.rs,
             which is the same test moved out of the way for room
  comment    anywhere at all, counted together rather than per concern
  code       everything left, bucketed into concerns by path

cloc decides which of a file's lines are code and which are comment; this
script decides which of them are tests. Neither can do the other's job. cloc
counts a file, and a Rust unit test lives inside the file it tests, so cloc
alone credits every subsystem with its own tests; the brace counting here
finds the test blocks but has no idea what a comment is.

So the two are composed: every file is cut into its test half and its rest,
both halves are written out under a directory named for the bucket they
belong to, and cloc is run over the result. Blank lines are dropped -- cloc
counts them separately, and a blank line is not a line of code.

TypeScript counts too: fresh's plugin system is a JS runtime whose plugins are
written in TypeScript, and leaving them out would shrink the one subsystem
that is deliberately not written in Rust.

    python3 examples/fresh-source-survey.py ~/src/fresh
    python3 examples/fresh-source-survey.py ~/src/fresh --spec > out.json
    python3 examples/fresh-source-survey.py ~/src/fresh --peel > peel.json
    python3 examples/fresh-source-survey.py ~/src/fresh --peel --big > big.json

The path rules below are fresh's, and they are the only part of this that does
not generalise: a concern is a judgement about what belongs with what, and
nothing can read that off a directory tree. Everything around them -- the test
split, the counting, the spec it writes -- would serve any Rust workspace whose
concerns you were willing to name.
"""
import csv
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
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
    # The two halves of the plugin story, kept apart because the clip takes
    # one of them out and leaves the other: the runtime is Rust and is part of
    # the editor, the plugins are TypeScript and are not.
    ("Plugins (TS)", (
        "crates/fresh-editor/plugins/", "crates/fresh-editor/npm-package/",
        "crates/fresh-editor/web-ui/")),
    ("Plugin runtime", (
        "crates/fresh-plugin-runtime/", "crates/fresh-plugin-api-macros/",
        "crates/fresh-parser-js/",
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
        "crates/fresh-editor/src/services/")),
    ("Text model", (
        "crates/fresh-editor-core/src/model/",
        "crates/fresh-editor-core/src/primitives/",
        "crates/fresh-editor-core/src/wrap_machine.rs",
        "crates/fresh-editor-core/src/markdown.rs",
        "tests/wrap_model/src/")),
    ("Editor app", ("crates/fresh-editor/src/app/", "crates/fresh-core/")),
]
E2E = "End-to-end tests"
UNIT = "Inline unit tests"
COMMENTS = "Comments"


# Everything the rules do not name is folded into one section rather than
# left as a pile: a breakdown with a dozen one-percent slices is a legend,
# not a picture.
REST = "Everything else"

# What each slice is, in one line, for the card the camera stops on. The
# numbers come out of the tree; these do not, and could not.
NOTES = {
    E2E: "integration crates that drive the whole editor from outside it",
    UNIT: "#[cfg(test)] blocks, living in the file they are testing",
    COMMENTS: "every comment line in the repo, tests and code alike",
    "Editor app":
        "commands, dispatch, buffers — what the editor does when asked",
    "Rendering & UI":
        "the view tree, the terminal backend, and every widget on it",
    "Plugins (TS)":
        "the plugin layer in TypeScript, and the browser UI with it",
    "Plugin runtime":
        "the JS engine, the host API, and the macros that bind them",
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
    """-> (the file without its test blocks, the test blocks on their own)

    Brace counting from the `#[cfg(test)]` attribute. Crude, and exactly right
    for the shape Rust actually uses: an attribute, a `mod tests {`, and a
    closing brace back in column zero. Returned as text rather than as counts
    because cloc has to see both halves to say which of their lines are
    comments.
    """
    with open(path, encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()
    code, test = [], []
    i = 0
    while i < len(lines):
        if not CFG_TEST.match(lines[i]):
            code.append(lines[i])
            i += 1
            continue
        j = i
        while j < len(lines) and not MOD_OPEN.search(lines[j]):
            j += 1
        if j >= len(lines):              # the attribute was on something else
            code.append(lines[i])
            i += 1
            continue
        depth, k = 0, j
        while k < len(lines):
            depth += lines[k].count("{") - lines[k].count("}")
            k += 1
            if depth <= 0:
                break
        test += lines[i:k]
        i = k
    return "".join(code), "".join(test)


def concern(rel):
    for name, prefixes in RULES:
        if any(rel.startswith(p) for p in prefixes):
            return name
    return REST


def survey():
    """-> (lines by bucket, files by bucket, what fell into the catch-all)

    Every file is cut into the part that is a test and the part that is not,
    each half is written under a directory named for its bucket, and cloc is
    run over the lot. What comes back is per-file code and comment counts that
    already know which bucket they belong to.
    """
    tmp = tempfile.mkdtemp(prefix="fresh-survey-")
    try:
        buckets, files, rest = [], defaultdict(int), defaultdict(int)

        def emit(bucket, ext, text):
            if not text.strip():
                return
            k = len(buckets)
            buckets.append(bucket)
            d = os.path.join(tmp, f"b{k:05d}")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, f"part{ext}"), "w",
                      encoding="utf-8") as fh:
                fh.write(text)

        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIR]
            for fn in filenames:
                if not fn.endswith(CODE) or fn.endswith(SKIP_FILE):
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, ROOT).replace(os.sep, "/")
                ext = os.path.splitext(fn)[1]
                whole = open(full, encoding="utf-8", errors="replace").read()
                if "tests" in rel.split("/"):
                    emit(E2E, ext, whole)
                    files[E2E] += 1
                    continue
                if fn.startswith("test") and fn.endswith(".rs"):
                    emit(UNIT, ext, whole)   # a unit test moved out for room
                    continue
                c = concern(rel)
                if fn.endswith(".rs"):
                    body, tests = split_tests(full)
                    emit(UNIT, ext, tests)
                else:
                    body = whole
                emit(c, ext, body)
                files[c] += 1
                if c == REST:
                    rest[os.path.dirname(rel)] += len(body.splitlines())

        loc = defaultdict(int)
        for bucket, code, comment in cloc(tmp, buckets):
            loc[bucket] += code
            loc[COMMENTS] += comment
        return loc, files, rest
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def cloc(tmp, buckets):
    """-> (bucket, code, comment) per part, from cloc's own accounting.

    `--by-file` so each part is reported separately, and the part's directory
    name is the index back into the bucket it came from. Blank lines come back
    too and are dropped on the floor: cloc counts them apart from code for the
    same reason this does not want them.
    """
    out = subprocess.run(
        ["cloc", "--by-file", "--csv", "--quiet", "--follow-links", tmp],
        capture_output=True, text=True, check=True).stdout
    rows = csv.DictReader(io.StringIO(
        "\n".join(l for l in out.splitlines() if l.strip())))
    seen = []
    for row in rows:
        name = (row.get("filename") or "").strip()
        if not name or name.lower() == "sum":
            continue
        m = re.search(r"/b(\d{5})/", name)
        if not m:
            continue
        seen.append((buckets[int(m.group(1))],
                     int(row["code"]), int(row["comment"])))
    if not seen:
        raise SystemExit("cloc reported nothing; is it installed?")
    return seen


def short(n):
    """a line count as a person would say it out loud"""
    return f"{n / 1000:.0f}K" if n >= 9500 else f"{n / 1000:.1f}K"


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
            "intro_caption": [f"{short(total)} LoC",
                              "every crate, Rust and TypeScript alike"],
            "outro_caption": [
                f"{round(100 * tests / total)}% of it is tests",
                f"{short(loc[E2E])} end-to-end, "
                f"{short(loc[UNIT])} in the files they test"],
            "timing": {"hold": 2.8},
            "donut": {
                "style": "ansi", "charset": "unicode",
                "unit": "LoC", "total": short(total),
                "total_label": "LoC", "thickness": 0.40,
                "items": items,
            },
        },
        "encode": {"crf": 18, "preset": "slow"},
    }, indent=2, ensure_ascii=False)


# Comments go out with the tests: they are not what the ring is a
# breakdown *of* either, and a reader who wants to know how much code
# there is does not want the prose about it counted in.
# In ring order, which is read order: the camera walks round rather than
# doubling back. Comments are the second biggest thing in the repo and get a
# beat of their own -- a quarter of the ring cannot fly out unexplained.
TESTS = [E2E, COMMENTS, UNIT]
NOT_RUST = "Plugins (TS)"


def peel_spec(loc, total, card="full"):
    """the same numbers, told as three rings instead of one.

    Each stage re-normalises: a section that was 14% of everything is 34% of
    what is left once the tests have gone. That restatement is the only reason
    to take anything out of a ring, and it is the reason the totals are given
    per stage rather than scaled.
    """
    s2 = total - sum(loc[k] for k in TESTS)
    s3 = s2 - loc[NOT_RUST]
    items = []
    for i, (k, v) in enumerate(sorted(loc.items(), key=lambda kv: -kv[1])):
        items.append({"label": k, "value": v, "display": short(v),
                      "note": NOTES[k], "color": PALETTE[i % len(PALETTE)]})
    core = [k for k, _ in sorted(loc.items(), key=lambda kv: -kv[1])
            if k not in TESTS and k != NOT_RUST][:4]
    return json.dumps({
        "name": "fresh-source-peel" + ("-big" if card == "big" else ""),
        "render": {
            "size": [1080, 1080], "fps": 60,
            "title": "fresh 0.5.1 — source by concern",
            # The only two captions in the clip, and both of them live before
            # a stage header exists. Once the header says what ring this is,
            # a caption saying it again is a second place to read the same
            # thing.
            "intro_caption": ["code + tests + comments", ""],
            "outro_caption": ["", ""],
            "labels": {"donut": "everything"},
            "timing": {"hold": 2.6},
            "donut": {
                "style": "ansi", "charset": "unicode",
                "unit": "LoC", "total": short(total),
                "total_label": "LoC", "thickness": 0.40,
                "card": card,
                "items": items,
                "peel": [
                    {"read": TESTS, "drop": TESTS,
                     "gather": [f"{round(100 * (total - s2) / total)}% "
                                "tests + comments", ""],
                     "title": "code only",
                     "total": short(s2)},
                    {"read": [NOT_RUST], "drop": [NOT_RUST],
                     "title": "core only (rust)",
                     "total": short(s3)},
                    {"read": core},
                ],
            },
        },
        "encode": {"crf": 18, "preset": "slow"},
    }, indent=2, ensure_ascii=False)


def check_pure_rust():
    """the clip's last header says "core only (rust)"; make it earn that.

    Everything that is not Rust has to be inside the one section the peel
    throws out, or the claim is decoration.
    """
    stray = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIR]
        for fn in filenames:
            if not fn.endswith((".ts", ".tsx", ".js", ".jsx")):
                continue
            if fn.endswith(SKIP_FILE):
                continue
            rel = os.path.relpath(os.path.join(dirpath, fn), ROOT).replace(
                os.sep, "/")
            if "tests" in rel.split("/") or concern(rel) == NOT_RUST:
                continue
            stray.append(rel)
    return stray


def main():
    loc, files, rest = survey()
    total = sum(loc.values())
    if "--peel" in sys.argv:
        stray = check_pure_rust()
        if stray:
            raise SystemExit(
                f"{len(stray)} non-Rust files would survive the peel, so "
                f'"core only (rust)" would be a lie: {stray[:5]}')
        print(peel_spec(loc, total,
                        "big" if "--big" in sys.argv else "full"))
        return
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
