"""The tree a prompt run works in: what it was at the start, what changed, put it back.

Every state of the tree is a git tree object written through a private index
(GIT_INDEX_FILE), so the project's own index, branch and stash are never touched. A state
holds every file git would not ignore -- tracked or untracked -- so one diff covers edits,
new files, deleted files, binaries and a file that was already untracked when the run
started (memdoor's changes.sh lists only files that appeared, and cannot see an edit to
one that was already there).

Outside a git repository the same code runs against a shadow repository in
.team-solve/shadow.git with the folder as its work tree, so the diff, the snapshots and the
keep net work in a plain folder too (memdoor falls back to "files newer than the start
marker" there, with no way to put a state back).

    start()            base tree + base commit, HEAD where it was (real repos), refs kept
    changes()          base -> now as a unified diff (new files in full)
    changed_files()    [(added, removed, path)] base -> now
    save(name)         now as <name>.tree + <name>.patch in the run folder
    restore(name)      the tree put back to a saved state, ignored files untouched
    restore_head()     HEAD moved by a role? put it back (memdoor scope.sh)
    baseline_copy(d)   the base tree extracted into d, ignored entries linked in

Paths are relative to the run's folder (ws), which may be a subdirectory of the repo.
"""
import os
import shutil
import subprocess
import tarfile
import tempfile

STATE_DIR = ".team-solve"
# The literal ":(exclude).team-solve" makes `git add` exit 1 in the shadow repository, where
# .team-solve/ is also in info/exclude ("paths are ignored"); the glob form works in both.
EXCLUDES = (":(exclude,glob)**/.team-solve/**", ":(exclude,glob)**/__pycache__/**")
SHADOW_EXCLUDES = (".team-solve/", "__pycache__/", "node_modules/", ".venv/", "venv/", "target/",
                   ".mypy_cache/", ".pytest_cache/", "*.pyc")
IDENTITY = {"GIT_AUTHOR_NAME": "team-solve", "GIT_AUTHOR_EMAIL": "team-solve@localhost",
            "GIT_COMMITTER_NAME": "team-solve", "GIT_COMMITTER_EMAIL": "team-solve@localhost"}
GIT_TIMEOUT_SEC = 600


class GitError(RuntimeError):
    pass


class Tree:
    def __init__(self, ws, run_dir, run_id, log):
        self.ws, self.rd, self.run_id, self.log = ws, run_dir, run_id, log
        self.env = dict(os.environ)
        self.env.pop("GIT_INDEX_FILE", None)
        probe = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=ws,
                               capture_output=True, text=True, env=self.env)
        self.real = probe.returncode == 0 and probe.stdout.strip() == "true"
        if not self.real:
            self._shadow()
        self.top = self.git("rev-parse", "--show-toplevel").strip()
        self.prefix = self.git("rev-parse", "--show-prefix").strip()

    # --- plumbing -------------------------------------------------------------

    def _shadow(self):
        sd = os.path.join(self.ws, STATE_DIR, "shadow.git")
        if not os.path.isdir(sd):
            subprocess.run(["git", "init", "-q", "--bare", sd], check=True, capture_output=True, env=self.env)
            with open(os.path.join(sd, "info", "exclude"), "a") as f:
                f.write("\n".join(SHADOW_EXCLUDES) + "\n")
        self.env["GIT_DIR"] = sd
        self.env["GIT_WORK_TREE"] = self.ws

    def git(self, *args, env=None, input=None, check=True, binary=False):
        e = dict(self.env, **(env or {}))
        r = subprocess.run(["git", *args], cwd=self.ws, capture_output=True, env=e, input=input,
                           text=not binary, timeout=GIT_TIMEOUT_SEC)
        if check and r.returncode != 0:
            err = r.stderr if not binary else r.stderr.decode("utf-8", "replace")
            raise GitError(f"git {' '.join(args[:3])}: exit {r.returncode}: {err.strip()[:500]}")
        return r.stdout

    def _rd(self, name):
        return os.path.join(self.rd, name)

    def _read(self, name):
        p = self._rd(name)
        return open(p).read().strip() if os.path.exists(p) else ""

    def _write(self, name, text):
        with open(self._rd(name), "w") as f:
            f.write(text)

    def _keep(self, name, sha):
        """A ref per saved state, so `git gc` never prunes a state the run may put back."""
        self.git("update-ref", f"refs/team-solve/{self.run_id}/{name}", sha)

    def snapshot(self):
        """The tree as it is now: every file git does not ignore, as a tree object id."""
        fd, idx = tempfile.mkstemp(prefix="team-solve-index-")
        os.close(fd)
        try:
            env = {"GIT_INDEX_FILE": idx}
            real_index = self.git("rev-parse", "--git-path", "index").strip()
            real_index = real_index if os.path.isabs(real_index) else os.path.join(self.ws, real_index)
            # Seeding from the project's index reuses its stat cache: only changed files are
            # hashed. A conflicted or unreadable index falls back to HEAD, then to empty. copy2,
            # not copyfile: git trusts a cached entry only when the file is older than the index
            # file itself ("racy git"), and a fresh mtime on the copy made a same-size rewrite in
            # the same second invisible (measured: a.txt "a" -> "A" was not in the diff).
            seeded = False
            if os.path.exists(real_index):
                shutil.copy2(real_index, idx)
                seeded = True
            for attempt in range(3):
                try:
                    self.git("add", "-A", "--", ".", *EXCLUDES, env=env)
                    return self.git("write-tree", env=env).strip()
                except GitError:
                    if attempt == 0 and seeded and self.git("rev-parse", "-q", "--verify", "HEAD", check=False).strip():
                        os.unlink(idx)
                        self.git("read-tree", "HEAD", env=env)
                    else:
                        if os.path.exists(idx):
                            os.unlink(idx)
                        self.git("read-tree", "--empty", env=env)
            raise GitError("could not write a tree of the work folder")
        finally:
            if os.path.exists(idx):
                os.unlink(idx)

    # --- the run's states -------------------------------------------------------

    def start(self):
        if self._read("base.txt"):
            return self._read("base.txt")
        base = self.snapshot()
        head = self.git("rev-parse", "-q", "--verify", "HEAD", check=False).strip()
        cargs = ["commit-tree", base, "-m", f"team-solve {self.run_id}: the tree as the run found it"]
        if head:
            cargs += ["-p", head]
        commit = self.git(*cargs, env=IDENTITY).strip()
        self._keep("base", commit)
        self._write("base-commit.txt", commit + "\n")
        if self.real:
            where = self.git("symbolic-ref", "-q", "--short", "HEAD", check=False).strip() or head
            if where:
                self._write("head.txt", where + "\n")
        self._write("base.txt", base + "\n")
        return base

    @property
    def base(self):
        return self._read("base.txt")

    def changes(self, binary=False):
        """base -> now, as a unified diff; new files in full."""
        now = self.snapshot()
        args = ["diff", "--relative", "--no-color", "--no-ext-diff"] + (["--binary"] if binary else [])
        return self.git(*args, self.base, now)

    def changed_files(self, now=None):
        now = now or self.snapshot()
        out = []
        for line in self.git("diff", "--relative", "--numstat", "--no-renames", self.base, now).splitlines():
            parts = line.split("\t", 2)
            if len(parts) == 3:
                out.append((parts[0], parts[1], parts[2]))
        return out

    def existed_at_base(self, path):
        r = subprocess.run(["git", "cat-file", "-e", f"{self.base}:{self.prefix}{path}"], cwd=self.ws,
                           env=self.env, capture_output=True)
        return r.returncode == 0

    def save(self, name):
        now = self.snapshot()
        self._keep(name, self.git("commit-tree", now, "-p", self._read("base-commit.txt"),
                                  "-m", f"team-solve {self.run_id}: {name}", env=IDENTITY).strip())
        self._write(f"{name}.tree", now + "\n")
        with open(self._rd(f"{name}.patch"), "wb") as f:
            f.write(self.git("diff", "--relative", "--binary", "--no-color", self.base, now, binary=True))
        return now

    def restore(self, name):
        """Put the work folder back to a saved state. Ignored files are left alone."""
        target = self._read(f"{name}.tree")
        if not target:
            raise GitError(f"no saved state {name}")
        return self.restore_tree(target)

    def restore_tree(self, target):
        now = self.snapshot()
        if now == target:
            return 0
        write, delete = [], []
        fields = self.git("diff", "--relative", "--name-status", "--no-renames", "-z", now, target).split("\0")
        i = 0
        while i + 1 < len(fields):
            status, path = fields[i], fields[i + 1]
            i += 2
            (delete if status.startswith("D") else write).append(path)
        for p in delete:
            fp = os.path.join(self.ws, p)
            if os.path.lexists(fp):
                os.unlink(fp)
            _prune_empty_dirs(self.ws, os.path.dirname(fp))
        if write:
            fd, idx = tempfile.mkstemp(prefix="team-solve-index-")
            os.close(fd)
            os.unlink(idx)
            try:
                env = {"GIT_INDEX_FILE": idx}
                self.git("read-tree", target, env=env)
                # checkout-index takes paths relative to the top; ours are relative to ws.
                listing = "".join(f"{self.prefix}{p}\0" for p in write)
                for p in write:
                    fp = os.path.join(self.ws, p)
                    if os.path.isdir(fp) and not os.path.islink(fp):
                        shutil.rmtree(fp)
                subprocess.run(["git", "checkout-index", "-f", "-z", "--stdin"], cwd=self.top,
                               env=dict(self.env, GIT_INDEX_FILE=idx), input=listing, text=True,
                               check=True, capture_output=True, timeout=GIT_TIMEOUT_SEC)
            finally:
                if os.path.exists(idx):
                    os.unlink(idx)
        after = self.snapshot()
        if after != target:
            raise GitError(f"the tree did not come back to {target[:12]} (it is {after[:12]})")
        return len(write) + len(delete)

    def restore_head(self):
        """A role that ran `git checkout` moved HEAD: put it back. Returns a report line or ''."""
        want = self._read("head.txt")
        if not (self.real and want):
            return ""
        now = self.git("symbolic-ref", "-q", "--short", "HEAD", check=False).strip() or \
            self.git("rev-parse", "-q", "--verify", "HEAD", check=False).strip()
        if now == want:
            return ""
        a = self.git("rev-parse", "-q", "--verify", now, check=False).strip()
        b = self.git("rev-parse", "-q", "--verify", want, check=False).strip()
        if a and a == b:
            return ""
        r = subprocess.run(["git", "checkout", "-q", want], cwd=self.ws, env=self.env, capture_output=True, text=True)
        if r.returncode == 0:
            return f"HEAD had moved to {now}; put back on {want}"
        return f"HEAD MOVED to {now} and could not be put back on {want}: {r.stderr.strip()[:200]}"

    def baseline_copy(self, dest):
        """The base tree extracted into dest, with the project's ignored entries (installed
        dependencies, build outputs, virtualenvs) linked in from the live tree, so a Check that
        needs node_modules or a venv runs there as it would in place. Returns the folder that
        corresponds to ws inside dest."""
        os.makedirs(dest, exist_ok=True)
        data = self.git("archive", "--format=tar", self._read("base-commit.txt"), binary=True)
        fd, tar_path = tempfile.mkstemp(prefix="team-solve-base-", suffix=".tar")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            with tarfile.open(tar_path) as t:
                try:
                    t.extractall(dest, filter="tar")
                except TypeError:
                    t.extractall(dest)
        finally:
            os.unlink(tar_path)
        ignored = subprocess.run(["git", "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z"],
                                 cwd=self.top, env=self.env, capture_output=True, text=True, timeout=GIT_TIMEOUT_SEC)
        for entry in ignored.stdout.split("\0"):
            entry = entry.rstrip("/")
            if not entry or entry.split("/")[0] == STATE_DIR or f"/{STATE_DIR}" in f"/{entry}":
                continue
            src, dst = os.path.join(self.top, entry), os.path.join(dest, entry)
            if os.path.lexists(dst) or not os.path.lexists(src):
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.symlink(src, dst)
        return os.path.join(dest, self.prefix) if self.prefix else dest


def _prune_empty_dirs(root, d):
    root = os.path.abspath(root)
    d = os.path.abspath(d)
    while d.startswith(root + os.sep) and os.path.isdir(d) and not os.listdir(d):
        os.rmdir(d)
        d = os.path.dirname(d)
