"""The failing tests a test runner's log lists (memdoor workflows/team-solve/failures.sh).

When the Check already fails on the tree as the run found it, a change is judged by the
failing tests it ADDS: one unrelated failing test in a large suite would otherwise turn the
whole Check off (astropy-13033 in memdoor's runs, 2026-11-05).

Understood logs:

    pytest      FAILED path::test - why / ERROR path::test     + "N failed, M passed in 1.2s"
    unittest    FAIL: test_x (mod.Class) / ERROR: ...          + "Ran N tests in" + "FAILED (failures=1, errors=2)" | "OK"
    Django      same as unittest
    go test     --- FAIL: TestX (0.00s)                        + "ok  pkg 0.1s" / "FAIL\tpkg\t0.1s" lines

`parse(text)` returns the sorted list of failing test ids, or None when the log is not a
finished run of one of these, its failure count does not match the list it printed, or it
names no failing test at all -- then nothing is known about which tests failed.
"""
import re

_COUNT = r"[0-9]+ (passed|failed|errors?|skipped|deselected|xfailed|xpassed)"
_PY_DONE = re.compile(
    _COUNT + r"[ ,].* in [0-9.]+s|[0-9]+ (passed|failed|errors?|skipped) in [0-9.]+s|no tests ran in [0-9.]+s")
_PY_FAIL = re.compile(r"^(FAILED|ERROR) [^ (]")
_UT_FAIL = re.compile(r"^(FAIL|ERROR): ")
_UT_RAN = re.compile(r"^Ran [0-9]+ tests? in ")
_UT_OK = re.compile(r"^OK( |$)")
_GO_SUB = re.compile(r"^[ \t]*--- FAIL: ")
_GO_PKG_DONE = re.compile(r"^(ok|FAIL|\?)[ \t]+[^ \t:]+([ \t]|$)")
_GO_PKG_FAIL = re.compile(r"^FAIL[ \t]+[^ \t:]+([ \t]|$)")


def _num(line, word):
    m = re.search(r"([0-9]+) " + word, line)
    return int(m.group(1)) if m else 0


def parse(text):
    py, ut, go = set(), set(), set()
    pyn = utn = 0
    pydone = utran = utdone = godone = False
    pyexp = utexp = 0
    for line in text.splitlines():
        if _PY_FAIL.match(line):
            tid = line.split(" ", 1)[1]
            tid = re.sub(r" - .*$", "", tid)
            py.add(tid)
            pyn += 1
        if _PY_DONE.search(line):
            pydone = True
            pyexp = _num(line, "failed") + _num(line, "error")
        if _UT_FAIL.match(line):
            ut.add(line.split(": ", 1)[1])
            utn += 1
        if _UT_RAN.match(line):
            utran = True
        if utran and _UT_OK.match(line):
            utdone, utexp = True, 0
        if utran and line.startswith("FAILED ("):
            utdone, utexp = True, 0
            m = re.search(r"failures=([0-9]+)", line)
            utexp += int(m.group(1)) if m else 0
            m = re.search(r"errors=([0-9]+)", line)
            utexp += int(m.group(1)) if m else 0
        if _GO_SUB.match(line):
            tid = _GO_SUB.sub("", line)
            go.add(re.sub(r" \([0-9.]+s\)$", "", tid))
        if _GO_PKG_DONE.match(line):
            godone = True
        if _GO_PKG_FAIL.match(line):
            tid = re.sub(r"^FAIL[ \t]+", "", line)
            go.add("package " + re.sub(r"[ \t]+[0-9.]+s$", "", tid))

    known, out = False, set()
    if pydone:
        if pyn != pyexp:
            return None
        known = True
        out |= py
    if utran:
        if not (utdone and utn == utexp):
            return None
        known = True
        out |= ut
    if godone:
        known = True
        out |= go
    # A failed run that names no failing test says nothing about the tests.
    if not known or not out:
        return None
    return sorted(out)
