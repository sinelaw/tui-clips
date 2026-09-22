"""Count an editor's source by concern, with cloc, splitting the tests out.

Four kinds of line:

  code       in a test that lives outside the code it tests: its own crate
             in Rust, the repository's own test/ tree in TypeScript
  code       in a unit test living with the code it tests -- a `#[cfg(test)]`
             block in Rust, a `test/` folder beside the source in TypeScript
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

Two repositories are described below, counted the same way so that the two
pictures can be read against each other: fresh, a Rust editor with a
TypeScript plugin layer, and VS Code, a TypeScript editor with a Rust CLI.
The repo is taken from the directory's name, or named outright:

    python3 examples/source-survey.py ~/src/fresh               # the table
    python3 examples/source-survey.py ~/src/fresh --spec  > spec.json
    python3 examples/source-survey.py ~/src/fresh --peel  > peel.json
    python3 examples/source-survey.py ~/src/fresh --peel --big > big.json
    python3 examples/source-survey.py ~/src/vscode --repo vscode

The path rules are the only part of this that does not generalise: a concern
is a judgement about what belongs with what, and nothing can read that off a
directory tree. Everything around them -- the test split, the counting, the
spec it writes -- would serve any repository whose concerns you were willing
to name.
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

E2E = "Integration tests"
UNIT = "Inline unit tests"
COMMENTS = "Comments"
# Everything the rules do not name is folded into one section rather than
# left as a pile: a breakdown with a dozen one-percent slices is a legend,
# not a picture.
REST = "Everything else"

CFG_TEST = re.compile(r"^\s*#\[cfg\(test\)\]")
MOD_OPEN = re.compile(r"\bmod\s+\w+\s*\{")

# Both repositories are counted with the same four kinds of line and the same
# bucket names wherever a bucket means the same thing in both, which is what
# makes the two pictures comparable at all. What differs is where those things
# live, and how each language writes a unit test.
FRESH = {
    "root": "~/fresh",
    "slug": "fresh-source",
    "title": "fresh 0.5.1 — source by concern",
    "intro": "every crate, Rust and TypeScript alike",
    "code": (".rs", ".ts", ".tsx", ".js", ".jsx"),
    "skip_dir": {".git", "target", "node_modules", "dist", "vendor",
                 ".vitepress", "fixtures"},
    "skip_file": (".d.ts", ".min.js"),
    "skip_path": (),
    # a `tests/` directory is an integration crate, compiled on its own
    # against the public API; a src file named test*.rs is a unit test moved
    # out of its file for room, and a #[cfg(test)] block is one still in it
    "e2e": lambda rel, fn: "tests" in rel.split("/"),
    "unit": lambda rel, fn: fn.startswith("test") and fn.endswith(".rs"),
    "unit_where": "in the files they test",
    # fresh's two kinds of test are 70/30, so the ring shows both
    "tests_bucket": None,
    "peel_drop": "Plugins (TS)",
    "peel_title": "core only (rust)",
    # the last header is a claim about the language; make it earn that
    "pure": ((".ts", ".tsx", ".js", ".jsx"), "non-Rust", "core only (rust)"),
    # Path rules, first match wins, so the specific comes before the general.
    "rules": [
        # The two halves of the plugin story, kept apart because the clip
        # takes one of them out and leaves the other: the runtime is Rust and
        # is part of the editor, the plugins are TypeScript and are not.
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
        # Config, the daemon and the services around them are one concern from
        # a reader's point of view -- the parts that talk to something outside
        # the process -- and three one-percent slices are a legend, not a
        # picture. The name is short because a label's width sets the ring's
        # radius; what it leaves out, the note says.
        ("Services & IPC", (
            "crates/fresh-editor-core/src/config",
            "crates/fresh-core/src/config",
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
    ],
    # What each slice is, in one line, for the card the camera stops on. The
    # numbers come out of the tree; these do not, and could not.
    "notes": {
        E2E: "test crates built against the editor's API rather than into it",
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
    },
}

VSCODE = {
    "root": "~/vscode",
    "slug": "vscode-source",
    "title": "VS Code 1.140 — source by concern",
    "intro": "every extension and the Rust CLI with it",
    "code": (".ts", ".tsx", ".js", ".jsx", ".mts", ".mjs", ".rs"),
    # `fixtures` and anything named for them are data a test reads, not code
    # a person wrote to be read: one colourisation fixture alone is 147K
    # lines, which is half of fresh's entire non-test source.
    "skip_dir": {".git", "node_modules", "dist", "vendor", "out", "out-build",
                 ".build", "typings", "vscode-dts", "fixtures",
                 "colorize-fixtures"},
    "skip_file": (".d.ts", ".min.js"),
    # Every file under here opens "DO NOT modify, this file was COPIED from
    # 'microsoft/vscode'", and 98% of their lines are byte-identical to the
    # original a few directories away. Counting both copies credits the same
    # source twice, once to the editor and once to the extension.
    "skip_path": ("extensions/copilot/src/util/vs/",),
    # VS Code puts its unit tests in a `test/` folder beside the code they
    # test -- the same relationship a #[cfg(test)] block has to its file, one
    # directory further out -- and its end-to-end tests in the repo's own
    # top-level `test/`, which drives a built application.
    "e2e": lambda rel, fn: rel.split("/")[0] == "test",
    "unit": lambda rel, fn: ("test" in rel.split("/")
                             or "tests" in rel.split("/")
                             or ".test." in fn),
    "unit_where": "beside the code they test",
    # 98% of VS Code's test lines are the colocated kind, so the ring says
    # "tests" once rather than spending a slice and a beat on a 0.5% sliver.
    # The table still prints the split.
    "tests_bucket": "Tests",
    "tests_line": "unit suites beside the code, smoke tests over a build",
    "peel_drop": "AI & agents",
    "peel_title": "editor only (no AI)",
    "pure": None,
    "rules": [
        # Chat, the agent host and the Copilot extension are one concern and
        # a new one: none of it existed when this was an editor and nothing
        # else. It is first because everything after it would otherwise
        # absorb it -- chat alone is most of `workbench/contrib`.
        #
        # The list is long because the concern is spread: the model context
        # protocol, the editor-side inline completions, the agent tools the
        # terminal grew, the voice services, the extension-host bridges and
        # the CLI's agent commands are all the same subject in twelve places.
        # A bucket that named only the obvious ones would be 41% of the code
        # while its own description said something narrower than it counted.
        ("AI & agents", (
            "src/vs/workbench/contrib/chat/",
            "src/vs/workbench/contrib/inlineChat/",
            "src/vs/workbench/contrib/inlineCompletions/",
            "src/vs/workbench/contrib/mcp/",
            "src/vs/workbench/contrib/agentsVoice/",
            "src/vs/workbench/contrib/speech/",
            "src/vs/workbench/contrib/welcomeAgentSessions/",
            "src/vs/workbench/contrib/terminalContrib/chat",
            "src/vs/workbench/services/chat/",
            "src/vs/workbench/services/mcp/",
            "src/vs/workbench/services/agentHost/",
            "src/vs/workbench/api/common/extHostChat",
            "src/vs/workbench/api/common/extHostMcp",
            "src/vs/workbench/api/common/extHostSpeech",
            "src/vs/workbench/api/common/extHostAgent",
            "src/vs/workbench/api/common/extHostLanguageModel",
            "src/vs/workbench/api/browser/mainThreadChat",
            "src/vs/workbench/api/browser/mainThreadMcp",
            "src/vs/workbench/api/browser/mainThreadSpeech",
            "src/vs/workbench/api/browser/mainThreadAgent",
            "src/vs/editor/contrib/inlineCompletions/",
            "src/vs/platform/agentHost/", "src/vs/platform/agentPlugins/",
            "src/vs/platform/chat/", "src/vs/platform/mcp/",
            "src/vs/sessions/", "extensions/copilot/",
            "scripts/chat-simulation/", ".github/skills/",
            "cli/src/tunnels/agent", "cli/src/commands/agent")),
        ("Language servers", (
            "extensions/css-language-features/",
            "extensions/html-language-features/",
            "extensions/json-language-features/",
            "extensions/markdown-language-features/",
            "extensions/typescript-language-features/",
            "extensions/php-language-features/",
            "extensions/ipynb/", "extensions/notebook-renderers/")),
        ("Syntax & themes", (
            "extensions/theme-", "extensions/vscode-colorize",
            "src/vs/platform/theme/", "src/vs/workbench/services/themes/",
            "src/vs/editor/common/languages/", "src/vs/editor/common/tokens/",
            "src/vs/editor/standalone/common/themes")),
        ("Plugins (TS)", ("extensions/",)),
        ("CLI (Rust)", ("cli/",)),
        ("Build & release", (
            "build/", "scripts/", "remote/", "resources/", "gulpfile",
            ".eslint-plugin-local/", "eslint.config.js", ".vscode/",
            ".github/", ".agents/", ".devcontainer/")),
        ("Plugin runtime", (
            "src/vs/workbench/api/", "src/vs/workbench/services/extensions/",
            "src/vs/workbench/contrib/extensions/",
            "src/vs/workbench/services/extensionManagement/",
            "src/vs/workbench/services/extensionRecommendations/",
            "src/vs/platform/extensions/",
            "src/vs/platform/extensionManagement/",
            "src/vs/platform/extensionResourceLoader/",
            "src/vs/platform/extensionRecommendations/")),
        ("Input & keys", (
            "src/vs/platform/keybinding/", "src/vs/platform/actions/",
            "src/vs/platform/commands/", "src/vs/workbench/services/keybinding/",
            "src/vs/workbench/services/actions/",
            "src/vs/base/browser/keyboardEvent", "src/vs/base/common/keyCodes",
            "src/vs/base/common/keybindings")),
        ("Text model", ("src/vs/editor/common/",)),
        # The platform's own widgets are named one by one because they sit
        # among the services rather than beside the rest of the view, and
        # fresh counts its widgets as rendering -- a comparison of the two
        # numbers is only worth making if they are a count of the same thing.
        ("Rendering & UI", (
            "src/vs/editor/browser/", "src/vs/editor/standalone/",
            "src/vs/base/browser/", "src/vs/workbench/browser/",
            "src/vs/workbench/electron-browser/",
            "src/vs/platform/quickinput/", "src/vs/platform/browserView/",
            "src/vs/platform/actionWidget/", "src/vs/platform/contextview/",
            "src/vs/platform/hover/", "src/vs/platform/list/",
            "src/vs/platform/menubar/", "src/vs/platform/dialogs/",
            "src/vs/platform/dnd/", "src/vs/platform/auxiliaryWindow/",
            "src/vs/platform/opener/", "src/vs/platform/progress/",
            "src/vs/platform/notification/", "src/vs/platform/domWidget/",
            "src/vs/platform/layout/", "src/vs/platform/markdown/")),
        ("Services & IPC", (
            "src/vs/platform/", "src/vs/base/parts/", "src/vs/server/",
            "src/vs/workbench/services/", "src/vs/code/")),
        ("Editor app", (
            "src/vs/editor/contrib/", "src/vs/workbench/", "src/vs/base/")),
    ],
    "notes": {
        "Tests": "unit suites beside the code, and smoke tests over a build",
        E2E: "smoke and integration suites, driving a built application",
        UNIT: "mocha suites in a test/ folder beside the code they test",
        COMMENTS: "every comment line in the repo, tests and code alike",
        "AI & agents":
            "chat, agents, MCP, voice — and the Copilot extension with them",
        "Editor app":
            "the workbench: parts, panels, and every contributed feature",
        "Rendering & UI":
            "the editor view, the DOM layer, and every widget on it",
        "Plugins (TS)":
            "the built-in extensions: git, emmet, the terminal suggestions",
        "Plugin runtime":
            "the extension host, its API surface, and the marketplace",
        "Services & IPC":
            "platform services, the remote server, and the wiring between",
        "Text model":
            "the piece tree, cursors, view model — what an edit goes through",
        "Input & keys":
            "keybindings, commands and actions: keys in, actions out",
        "Language servers":
            "the language-features extensions and the servers behind them",
        "Syntax & themes":
            "tokenisation, the theme service, and the colour themes",
        "CLI (Rust)": "the tunnel and launcher CLI, the one Rust crate here",
        "Build & release":
            "gulp, esbuild, the pipelines, and the repo's own tooling",
        REST: "bootstrap files, entry points, odds and ends",
    },
}

REPOS = {"fresh": FRESH, "vscode": VSCODE}


def _chosen():
    """which repository's rules to count with.

    Named outright, or read off the directory being counted -- which is right
    often enough to be worth doing and wrong quietly enough to be worth being
    able to override.
    """
    if "--repo" in sys.argv:
        name = sys.argv[sys.argv.index("--repo") + 1]
        if name not in REPOS:
            raise SystemExit(f"--repo is one of {sorted(REPOS)}; got {name!r}")
        return name
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        base = os.path.basename(sys.argv[1].rstrip("/")).lower()
        for name in REPOS:
            if name in base:
                return name
    return "fresh"


CFG = REPOS[_chosen()]
ROOT = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") \
    else os.path.expanduser(CFG["root"])
CODE = CFG["code"]
SKIP_DIR = CFG["skip_dir"]
SKIP_FILE = CFG["skip_file"]
SKIP_PATH = CFG["skip_path"]
RULES = CFG["rules"]
NOTES = CFG["notes"]

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
                if rel.startswith(SKIP_PATH):
                    continue
                ext = os.path.splitext(fn)[1]
                whole = open(full, encoding="utf-8", errors="replace").read()
                if CFG["e2e"](rel, fn):
                    emit(E2E, ext, whole)
                    files[E2E] += 1
                    continue
                if CFG["unit"](rel, fn):
                    emit(UNIT, ext, whole)   # a test that lives with its code
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
    try:
        out = subprocess.run(
            ["cloc", "--by-file", "--csv", "--quiet", "--follow-links",
             # cloc suppresses a file whose content it has already seen, and
             # which copy it keeps depends on the order it walked them in --
             # so two runs of this script disagreed by a few hundred lines
             # about which bucket a duplicate belonged to. It also has no
             # entry for .mts, and silently counted none of them.
             "--skip-uniqueness", "--force-lang=TypeScript,mts", tmp],
            capture_output=True, text=True, check=True).stdout
    except FileNotFoundError:
        raise SystemExit(
            "this needs cloc on the path: `apt install cloc`, `brew install "
            "cloc`, or https://github.com/AlDanial/cloc")
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
    """a line count as a person would say it out loud

    Three significant figures at most, and a millions column once there is
    one: "3524K" is a number a reader has to parse rather than take in.
    """
    if n >= 950_000:
        return f"{n / 1e6:.0f}M" if n >= 9_500_000 else f"{n / 1e6:.2f}M"
    return f"{n / 1000:.0f}K" if n >= 9500 else f"{n / 1000:.1f}K"


def for_ring(loc):
    """the buckets as the ring draws them, which is not always as counted.

    A repo whose two kinds of test are 98% one of them has no business
    spending two slices and two beats of the camera on the distinction: the
    sliver is unreadable at ring scale and the clip stops twice to say the
    same word. The table still reports both -- the split is a fact about the
    repository, it is just not a fact this picture is about.
    """
    one = CFG["tests_bucket"]
    if not one:
        return dict(loc)
    d = {k: v for k, v in loc.items() if k not in (E2E, UNIT)}
    d[one] = loc[E2E] + loc[UNIT]
    return d


def test_slices():
    """the bucket names the ring uses for tests, largest first"""
    return [CFG["tests_bucket"]] if CFG["tests_bucket"] else [E2E, UNIT]


def spec(loc, total):
    """the whole donut spec, so the picture cannot drift from the count"""
    loc = for_ring(loc)
    tests = sum(loc[k] for k in test_slices())
    items = []
    for i, (k, v) in enumerate(sorted(loc.items(), key=lambda kv: -kv[1])):
        d = {"label": k, "value": v, "display": short(v), "note": NOTES[k],
             "color": PALETTE[i % len(PALETTE)]}
        if k in NO_VISIT:
            d["visit"] = False
        items.append(d)
    return json.dumps({
        "name": f"{CFG['slug']}-breakdown",
        "render": {
            "size": [1080, 1080], "fps": 60,
            "title": CFG["title"],
            "intro_caption": [f"{short(total)} LoC", CFG["intro"]],
            "outro_caption": [
                f"{round(100 * tests / total)}% of it is tests",
                CFG["tests_line"] if CFG["tests_bucket"] else
                f"{short(loc[E2E])} standing apart, "
                f"{short(loc[UNIT])} {CFG['unit_where']}"],
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


def peeled_first(loc):
    """what goes out of the ring before anything else does.

    Comments go with the tests: they are not what the ring is a breakdown
    *of* either, and a reader who wants to know how much code there is does
    not want the prose about it counted in. Largest first, which is ring
    order and so read order -- the camera walks round rather than doubling
    back, and a quarter of the ring cannot fly out unexplained.
    """
    return sorted(test_slices() + [COMMENTS], key=lambda k: -loc[k])


def peel_spec(loc, total, card="full"):
    """the same numbers, told as three rings instead of one.

    Each stage re-normalises: a section that was 14% of everything is 34% of
    what is left once the tests have gone. That restatement is the only reason
    to take anything out of a ring, and it is the reason the totals are given
    per stage rather than scaled.
    """
    loc = for_ring(loc)
    tests = peeled_first(loc)
    drop = CFG["peel_drop"]
    s2 = total - sum(loc[k] for k in tests)
    s3 = s2 - loc[drop]
    items = []
    for i, (k, v) in enumerate(sorted(loc.items(), key=lambda kv: -kv[1])):
        items.append({"label": k, "value": v, "display": short(v),
                      "note": NOTES[k], "color": PALETTE[i % len(PALETTE)]})
    core = [k for k, _ in sorted(loc.items(), key=lambda kv: -kv[1])
            if k not in tests and k != drop][:4]
    return json.dumps({
        "name": (f"{CFG['slug']}-peel"
                 + ("-big" if card == "big" else "")),
        "render": {
            "size": [1080, 1080], "fps": 60,
            "title": CFG["title"],
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
                    {"read": tests, "drop": tests,
                     "gather": [f"{round(100 * (total - s2) / total)}% "
                                "tests + comments", ""],
                     "title": "code only",
                     "total": short(s2)},
                    {"read": [drop], "drop": [drop],
                     "title": CFG["peel_title"],
                     "total": short(s3)},
                    {"read": core},
                ],
            },
        },
        "encode": {"crf": 18, "preset": "slow"},
    }, indent=2, ensure_ascii=False)


def check_claim():
    """some last headers are a claim; make them earn it.

    fresh's peel ends on "core only (rust)", so everything that is not Rust
    has to be inside the one section the peel throws out or the claim is
    decoration. A repo whose last header claims nothing of the sort (`pure`
    is None) has nothing here to check.
    """
    if not CFG["pure"]:
        return []
    exts, _, _ = CFG["pure"]
    stray = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIR]
        for fn in filenames:
            if not fn.endswith(exts):
                continue
            if fn.endswith(SKIP_FILE):
                continue
            rel = os.path.relpath(os.path.join(dirpath, fn), ROOT).replace(
                os.sep, "/")
            if CFG["e2e"](rel, fn) or CFG["unit"](rel, fn) \
                    or concern(rel) == CFG["peel_drop"]:
                continue
            stray.append(rel)
    return stray


def main():
    loc, files, rest = survey()
    total = sum(loc.values())
    if "--peel" in sys.argv:
        stray = check_claim()
        if stray:
            _, what, claim = CFG["pure"]
            raise SystemExit(
                f"{len(stray)} {what} files would survive the peel, so "
                f'"{claim}" would be a lie: {stray[:5]}')
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
    tests = loc[E2E] + loc[UNIT]
    print(f"\ntests are {100 * tests / total:.0f}% of it "
          f"({tests:,d} of {total:,d})")
    if rest:
        print(f"\ninside {REST!r}:")
        for k, v in sorted(rest.items(), key=lambda kv: -kv[1])[:10]:
            print(f"  {v:8,d}  {k}")


if __name__ == "__main__":
    main()
