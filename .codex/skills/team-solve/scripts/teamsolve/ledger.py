"""The run's on-disk record and its resume mechanism.

Resume is deterministic replay, not a saved program counter. The loop always starts from
the top; every model call goes through `Calls.chat`, which looks up the call's slot
(position + role) in .team-solve/calls/. When the slot holds a reply to the byte-identical
request (same messages, temperature and model knobs), that reply is returned without a
network call. The first slot whose request differs -- or that is missing -- and every slot
after it are moved to .team-solve/stale/<stamp>/ and the run continues live from there.

Since the loop is a pure function of the replies plus sample runs, replay rebuilds the
same ledger files the interrupted run had, so a resumed run cannot drift from a fresh one
that received the same replies.

    .team-solve/
      calls/007-worker-2.json   {"name", "key", "request": {...}, "reply": {...}}
      meter.jsonl               one line per live call: tokens, ms, attempts, finish
      transcript.jsonl          the authors' transcript: every call of this pass, in order
      run.json                  options and result of the latest pass
      lock                      flock: one run per problem folder
"""
import fcntl
import hashlib
import json
import os
import shutil
import time

STATE_DIR = ".team-solve"


def read(ws, name):
    p = os.path.join(ws, name)
    return open(p).read() if os.path.exists(p) else ""


def write(ws, name, content):
    p = os.path.join(ws, name)
    d = os.path.dirname(p)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(p, "w") as f:
        f.write(content)


def append(ws, name, content):
    with open(os.path.join(ws, name), "a") as f:
        f.write(content)


def state_path(ws, *parts):
    return os.path.join(ws, STATE_DIR, *parts)


class Lock:
    def __init__(self, ws):
        os.makedirs(state_path(ws), exist_ok=True)
        self.path = state_path(ws, "lock")
        self.fd = None

    def __enter__(self):
        self.fd = open(self.path, "w")
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.fd.close()
            raise SystemExit(f"team-solve: another run holds {self.path}")
        self.fd.write(str(os.getpid()))
        self.fd.flush()
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        self.fd.close()


def _key(messages, temperature, params):
    blob = json.dumps({"messages": messages, "temperature": temperature, "params": params},
                      sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class Calls:
    """Replay cache + meter + transcript around a client (anything with .chat and .params)."""

    def __init__(self, ws, client, log, replay=True):
        self.ws, self.client, self.log = ws, client, log
        self.replay = replay
        self.n = 0
        self.live = 0
        self.replayed = 0
        self.diverged = False
        self.tokens = {"input": 0, "output": 0, "reasoning": 0, "cached": 0}
        os.makedirs(state_path(ws, "calls"), exist_ok=True)
        write(ws, os.path.join(STATE_DIR, "transcript.jsonl"), "")

    def _slots(self):
        d = state_path(self.ws, "calls")
        return sorted(f for f in os.listdir(d) if f.endswith(".json"))

    def _retire_from(self, index):
        """Move slot `index` and everything after it out of the replay path."""
        stale = [f for f in self._slots() if int(f.split("-", 1)[0]) >= index]
        if not stale:
            return
        dest = state_path(self.ws, "stale", time.strftime("%Y%m%d-%H%M%S"))
        os.makedirs(dest, exist_ok=True)
        for f in stale:
            shutil.move(state_path(self.ws, "calls", f), os.path.join(dest, f))
        self.log(f"    [replay] request changed at call {index}; {len(stale)} later call(s) moved to "
                 f"{os.path.relpath(dest, self.ws)}")

    def chat(self, name, messages, temperature):
        from .client import Reply  # local: client imports nothing from here
        self.n += 1
        slot = f"{self.n:03d}-{name}.json"
        path = state_path(self.ws, "calls", slot)
        key = _key(messages, temperature, self.client.params())
        reply = None
        if self.replay and not self.diverged and os.path.exists(path):
            rec = json.load(open(path))
            if rec.get("key") == key and not rec["reply"].get("infra_exhausted"):
                reply = Reply.from_json(rec["reply"])
                self.replayed += 1
        if reply is None:
            if not self.diverged:
                self._retire_from(self.n)
                self.diverged = True
            reply = self.client.chat(messages, temperature)
            self.live += 1
            rec = {"name": name, "key": key, "t": time.time(), "temperature": temperature,
                   "params": self.client.params(), "request": messages, "reply": reply.to_json()}
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(rec, f, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
            u = reply.usage or {}
            with open(state_path(self.ws, "meter.jsonl"), "a") as f:
                f.write(json.dumps({"t": time.time(), "call": slot[:-5], "model": reply.model,
                                    "finish": reply.finish, "attempts": reply.attempts, "ms": reply.ms,
                                    "input_tokens": u.get("input_tokens"),
                                    "cached_tokens": (u.get("input_tokens_details") or {}).get("cached_tokens"),
                                    "output_tokens": u.get("output_tokens"),
                                    "reasoning_tokens": (u.get("output_tokens_details") or {}).get("reasoning_tokens"),
                                    "infra_exhausted": reply.infra_exhausted}) + "\n")
        u = reply.usage or {}
        self.tokens["input"] += u.get("input_tokens") or 0
        self.tokens["output"] += u.get("output_tokens") or 0
        self.tokens["cached"] += (u.get("input_tokens_details") or {}).get("cached_tokens") or 0
        self.tokens["reasoning"] += (u.get("output_tokens_details") or {}).get("reasoning_tokens") or 0
        with open(state_path(self.ws, "transcript.jsonl"), "a") as f:
            f.write(json.dumps({"t": time.time(), "role": name, "request": messages, "response": reply.text,
                                "reasoning": reply.reasoning, "finish_reason": reply.finish,
                                "completion_tokens": u.get("output_tokens"), "prompt_tokens": u.get("input_tokens"),
                                "attempts": reply.attempts, "discarded": reply.discarded,
                                "infra_exhausted": reply.infra_exhausted, "replayed": not self.diverged},
                               ensure_ascii=False) + "\n")
        return reply
