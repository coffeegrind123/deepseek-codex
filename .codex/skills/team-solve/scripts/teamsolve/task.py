"""task.md for a prompt run: the person's prompt plus what the planner settled.

    ## Prompt        the person's words, untouched (and the --until end condition, theirs too)
    ## Deliverable   what the run leaves: "changes to this tree", or new files by name
    ## Check         one command in backticks, or the word samples, or the word judge
    ## Scope         the files and directories the work may touch, in backticks
    ## Done when     what the reviewer and the judge check, one line each

Ported from memdoor workflows/team-solve (task.template.md, task.sh, settle.sh and the
Check rules of check.sh). A section the planner left out or got wrong is filled safely,
so a run never stops on its plan: the Check by the one the prompt names ("Check: go test
./x/ passes."), else the judge; the Scope by the paths the prompt names that exist, else
the whole tree. Two things only the person can set win over the planner: --verify-cmd is
the Check, and --until is the first line of Done when, word for word.
"""
import os
import re

SECTIONS = ("Deliverable", "Check", "Scope", "Done when")
UNTIL_LABEL = "End condition (the run is done only when this holds):"

TEMPLATE = """# Task

## Prompt

@@PROMPT@@

## Deliverable

<!-- plan: what the run leaves. "changes to this tree", or the new files by name (solution.py, answer.md, ...) -->

## Check

<!-- plan: one command in backticks that exits 0 when the work is right (`go test ./...`, `npm test`, `make check`); the word samples when tests/N.in and tests/N.out are here; the word judge when nothing can be run -->

## Scope

<!-- plan: the files and directories the work may touch; everything else stays as it is -->

## Done when

<!-- plan: what the reviewer and the judge check, one line each -->
"""

_TICKED = re.compile(r"`[^`]+`")
_PATHLIKE = re.compile(r"[A-Za-z0-9_./-]+/[A-Za-z0-9_./-]+|[A-Za-z0-9_-]+\.[A-Za-z0-9_]{1,5}")


def prompt_text(prompt, until=None):
    """prompt.md: the person's words, and their end condition when they gave one."""
    text = prompt.strip("\n")
    if until and until.strip():
        text += f"\n\n{UNTIL_LABEL} {until.strip()}"
    return text + "\n"


def make_task(prompt):
    """The template with the prompt in place of @@PROMPT@@, its blank first and last lines
    dropped; a prompt's own "## " headings go one level down so they do not read as
    task.md's sections (task.sh)."""
    lines = [("#" + l if l.startswith("## ") else l) for l in prompt.split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return TEMPLATE.replace("@@PROMPT@@", "\n".join(lines))


def section(text, name):
    """The lines under "## <name>" up to the next "## " heading, trimmed, comments dropped."""
    out, on = [], False
    for line in text.split("\n"):
        if on and re.match(r"^##[ \t]", line):
            break
        if on:
            out.append(line)
        elif re.match(r"^##[ \t]*" + re.escape(name) + r"[ \t]*$", line):
            on = True
    out = [l for l in out if not re.match(r"^\s*<!--.*-->\s*$", l)]
    while out and not out[0].strip():
        out.pop(0)
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(l.rstrip("\r") for l in out).strip("\n")


def _first_word(text):
    for tok in text.split():
        return re.sub(r"[^A-Za-z]", "", tok).lower()
    return ""


def prompt_check(prompt):
    """A Check the prompt names in so many words: "Check: <cmd> passes" / "exits 0"."""
    for line in prompt.split("\n"):
        i = line.find("Check:")
        if i < 0:
            continue
        s = line[i + 6:].lstrip(" \t")
        s = s[1:] if s.startswith("`") else s
        m = re.search(r"`?[ \t]+(passes|exits 0)([^A-Za-z0-9_]|$)", s)
        if m:
            c = s[:m.start()].strip()
            if c and "`" not in c:
                return c
    return ""


def settle(plan, task, prompt, ws, until=None, verify_cmd=None):
    """plan.md's sections into task.md, the Prompt untouched. Returns (task, filled)."""
    got = {name: section(plan, name) for name in SECTIONS}
    filled = []

    if not got["Deliverable"]:
        got["Deliverable"] = "changes to this tree"
        filled.append("Deliverable")

    if verify_cmd:
        got["Check"] = f"`{verify_cmd}`"
    else:
        first = _first_word(got["Check"])
        if not _TICKED.search(got["Check"]) and first not in ("samples", "judge"):
            cmd = prompt_check(prompt)
            got["Check"] = f"`{cmd}`" if cmd else "judge"
            filled.append("Check")

    if not _TICKED.search(got["Scope"]):
        paths = sorted({re.sub(r"[.,:;)]*$", "", p) for p in _PATHLIKE.findall(prompt)})
        found = [p for p in paths if p and os.path.exists(os.path.join(ws, p))]
        got["Scope"] = "\n".join(f"`{p}`" for p in found) or "`.`"
        filled.append("Scope")

    if until and until.strip():
        want = until.strip()
        rest = [l for l in got["Done when"].split("\n") if l.strip() and want not in l]
        got["Done when"] = "\n".join([f"- {want}"] + rest)
    elif not got["Done when"]:
        got["Done when"] = "The Prompt is done as written."
        filled.append("Done when")

    out, skip = [], False
    for line in task.split("\n"):
        if line.startswith("## "):
            name = line[3:].strip()
            out.append(line)
            skip = name in SECTIONS
            if skip:
                out += ["", got[name], ""]
            continue
        if not skip:
            out.append(line)
    return "\n".join(out), filled


def check_spec(task, ws):
    """What the Check section names: ("command", cmd) | ("samples", "") | ("judge", "").
    A Check that starts with the word samples or judge is that, whatever it quotes after;
    otherwise its first command in backticks (check.sh). The samples need tests/*.in."""
    sec = section(task, "Check")
    first = _first_word(sec)
    m = _TICKED.search(sec)
    cmd = m.group(0).strip("`") if m else ""
    if cmd in ("samples", "judge") or first in ("samples", "judge"):
        cmd = ""
    if cmd:
        return "command", cmd
    has_samples = os.path.isdir(os.path.join(ws, "tests")) and any(
        f.endswith(".in") for f in os.listdir(os.path.join(ws, "tests")))
    if has_samples and first != "judge" and not re.search(r"\bjudge\b", sec):
        return "samples", ""
    return "judge", ""


def scope_entries(task):
    """Scope paths, normalised: no leading ./, no trailing /, the whole tree as '.'."""
    out = []
    for m in _TICKED.finditer(section(task, "Scope")):
        p = m.group(0).strip("`").strip()
        p = re.sub(r"^\./", "", p).rstrip("/") or "."
        out.append(p)
    return out


def scope_report(task, tree):
    """Each file the run changed, with the lines it added and removed, against the Scope
    (scope.sh). Returns (text, outside_count). A NEW file in the directory of a file the
    Scope names is inside it: a test needs its fixture beside it and its name is not known
    in advance. An edit to an existing file outside the Scope is reported."""
    lines, outside = [], 0
    moved = tree.restore_head()
    if moved:
        lines.append(moved)
        if moved.startswith("HEAD MOVED"):
            outside += 1
    scope = scope_entries(task)
    lines.append("scope: " + (" ".join(scope) if scope else "none named in backticks in task.md, so not checked"))

    def inside(path):
        if not scope:
            return True
        return any(s == "." or path == s or path.startswith(s + "/") for s in scope)

    def beside(path):
        if tree.existed_at_base(path):
            return False
        for s in scope:
            if not os.path.isfile(os.path.join(tree.ws, s)):
                continue
            d = os.path.dirname(s)
            if d and path.startswith(d + "/"):
                return True
        return False

    for add, rem, path in tree.changed_files():
        if inside(path) or beside(path):
            lines.append(f"changed: {path} +{add} -{rem}")
        else:
            outside += 1
            lines.append(f"OUTSIDE THE SCOPE: {path} +{add} -{rem} (undo it: restore the file, or delete it "
                         f"when the run made it)")
    return "\n".join(lines) + "\n", outside
