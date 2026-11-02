"""One model call per role, through the workspace router's Responses endpoint.

Streaming is required, not a nicety: the router drops an upstream that is silent for
600 s (router/router.mjs DEFAULT_UPSTREAM_IDLE_MS), and a non-streamed 128K-token reply
is silent for its whole generation.

Wire shape, measured against deepseek-flash through the router (2026-11-01):

    event: response.output_text.delta / response.reasoning_text.delta   (text pieces)
    event: response.completed   {"response": {"status": "completed", "output": [...], "usage": {...}}}
    event: response.incomplete  {"response": {"status": "incomplete",
                                              "incomplete_details": {"reason": "max_output_tokens"}}}

`finish` is normalised to the authors' vocabulary: "stop", or "length" for a reply cut at
the cap. Retry policy is the authors' cloud chat (orchestrator.py): an empty reply, or one
"clamped" (cut at length well under the cap, i.e. a provider limit rather than ours), is
discarded and retried; a reply with no text is answered by its reasoning.
"""
import http.client
import json
import os
import random
import time
import urllib.parse
from dataclasses import dataclass, field

HTTP_OK = 200
HTTP_BAD_REQUEST = 400
HTTP_TOO_MANY = 429
RETRYABLE_STATUS = {408, 409, HTTP_TOO_MANY, 500, 502, 503, 504, 529}
CLAMP_RATIO = 0.9
DEFAULT_ROUTER_PORT = 8877
STREAM_READ_TIMEOUT_SEC = 900  # longer than the router's own 600 s idle cut
CONTEXT_ERROR_HINTS = ("context", "maximum context length", "too long", "exceeds", "max_model_len")


class CallError(RuntimeError):
    pass


@dataclass
class Reply:
    text: str
    finish: str
    reasoning: str = ""
    usage: dict = field(default_factory=dict)
    attempts: int = 1
    discarded: list = field(default_factory=list)
    infra_exhausted: bool = False
    ms: int = 0
    model: str = ""

    def to_json(self):
        return dict(self.__dict__)

    @classmethod
    def from_json(cls, d):
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


def _workspace_root():
    # .codex/skills/team-solve/scripts/teamsolve/client.py -> workspace root
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(here, "..", "..", "..", "..", ".."))


def _secrets_token():
    path = os.path.join(_workspace_root(), ".codex", "secrets.env")
    if not os.path.exists(path):
        return ""
    for line in open(path):
        line = line.strip()
        if line.startswith("CODEX_ROUTER_TOKEN="):
            return line.split("=", 1)[1].strip().strip("'\"")
    return ""


def router_base():
    return os.environ.get("TEAM_SOLVE_BASE_URL") or \
        f"http://127.0.0.1:{os.environ.get('CODEX_ROUTER_PORT', DEFAULT_ROUTER_PORT)}/v1"


def router_token():
    return os.environ.get("TEAM_SOLVE_API_KEY") or os.environ.get("CODEX_ROUTER_TOKEN") or _secrets_token()


class RouterClient:
    def __init__(self, model, max_output_tokens, reasoning_effort=None, top_p=None, top_k=None,
                 attempts=4, call_timeout_sec=4 * 3600, base_url=None, token=None, log=None):
        self.model = model
        self.max_output_tokens = max_output_tokens
        self.reasoning_effort = reasoning_effort
        self.top_p = top_p
        self.top_k = top_k
        self.attempts = attempts
        self.call_timeout_sec = call_timeout_sec
        self.base = urllib.parse.urlsplit(base_url or router_base())
        self.token = token if token is not None else router_token()
        self.log = log or (lambda *a: None)
        if not self.token:
            raise CallError("no router token: set CODEX_ROUTER_TOKEN (./codex loads .codex/secrets.env) "
                            "or TEAM_SOLVE_API_KEY")

    def params(self):
        """The request knobs that change a reply, for the replay cache key."""
        return {"model": self.model, "max_output_tokens": self.max_output_tokens,
                "reasoning_effort": self.reasoning_effort, "top_p": self.top_p, "top_k": self.top_k}

    def health(self):
        conn = self._conn(10)
        try:
            conn.request("GET", "/health")
            r = conn.getresponse()
            return r.status == HTTP_OK, r.read().decode("utf-8", "replace")[:300]
        except OSError as e:
            return False, str(e)
        finally:
            conn.close()

    def _conn(self, timeout):
        cls = http.client.HTTPSConnection if self.base.scheme == "https" else http.client.HTTPConnection
        return cls(self.base.hostname, self.base.port, timeout=timeout)

    def _body(self, messages, temperature, max_tokens):
        body = {
            "model": self.model,
            "input": [{"role": m["role"], "content": m["content"]} for m in messages],
            "temperature": temperature,
            "max_output_tokens": max_tokens,
            "stream": True,
            "store": False,
        }
        if self.top_p is not None:
            body["top_p"] = self.top_p
        if self.top_k is not None:
            body["top_k"] = self.top_k
        if self.reasoning_effort:
            body["reasoning"] = {"effort": self.reasoning_effort}
        return body

    def _stream_once(self, messages, temperature, max_tokens, deadline):
        """One HTTP attempt. Returns (status, error_text, text, reasoning, finish, usage)."""
        path = self.base.path.rstrip("/") + "/responses"
        conn = self._conn(STREAM_READ_TIMEOUT_SEC)
        try:
            conn.request("POST", path, body=json.dumps(self._body(messages, temperature, max_tokens)),
                         headers={"Authorization": f"Bearer {self.token}",
                                  "Content-Type": "application/json",
                                  "Accept": "text/event-stream"})
            r = conn.getresponse()
            if r.status != HTTP_OK:
                return r.status, r.read().decode("utf-8", "replace")[:2000], "", "", None, {}
            text_parts, reason_parts, final, event = [], [], None, None
            for raw in r:
                if time.time() > deadline:
                    raise TimeoutError(f"call exceeded its {self.call_timeout_sec}s clock")
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("event:"):
                    event = line[6:].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    obj = json.loads(data)
                except ValueError:
                    continue
                kind = obj.get("type") or event or ""
                if kind == "response.output_text.delta":
                    text_parts.append(obj.get("delta") or "")
                elif kind == "response.reasoning_text.delta":
                    reason_parts.append(obj.get("delta") or "")
                elif kind in ("response.completed", "response.incomplete", "response.failed"):
                    final = obj.get("response") or {}
                elif kind == "error":
                    return 599, json.dumps(obj)[:2000], "", "", None, {}
            if final is None:
                return 598, "stream ended without a terminal response event", "".join(text_parts), \
                    "".join(reason_parts), None, {}
            text, reasoning = _output_text(final)
            text = text if text else "".join(text_parts)
            reasoning = reasoning if reasoning else "".join(reason_parts)
            status = final.get("status")
            if status == "failed":
                return 597, json.dumps(final.get("error"))[:2000], text, reasoning, None, final.get("usage") or {}
            reason = (final.get("incomplete_details") or {}).get("reason")
            finish = "length" if status == "incomplete" and reason in ("max_output_tokens", "length", None) \
                else ("stop" if status == "completed" else str(reason or status))
            return HTTP_OK, "", text, reasoning, finish, final.get("usage") or {}
        finally:
            conn.close()

    def chat(self, messages, temperature):
        started = time.time()
        deadline = started + self.call_timeout_sec
        cap = self.max_output_tokens
        discarded, last, last_reason, last_usage, finish = [], "", "", {}, None
        got_response = False
        for attempt in range(self.attempts):
            try:
                status, err, text, reasoning, finish, usage = self._stream_once(messages, temperature, cap, deadline)
            except TimeoutError as e:
                discarded.append({"attempt": attempt + 1, "why": str(e)})
                self.log(f"    [router] {e}; giving up on this call")
                break
            except (OSError, http.client.HTTPException) as e:
                discarded.append({"attempt": attempt + 1, "why": f"{type(e).__name__}: {str(e)[:200]}"})
                self.log(f"    [router] {type(e).__name__} on attempt {attempt + 1}: {str(e)[:200]}; retrying")
                time.sleep(3 * (attempt + 1))
                continue
            if status != HTTP_OK:
                discarded.append({"attempt": attempt + 1, "why": f"HTTP {status}: {err[:300]}"})
                if status == HTTP_BAD_REQUEST and any(h in err.lower() for h in CONTEXT_ERROR_HINTS) and cap > 4096:
                    cap = max(4096, cap // 2)
                    self.log(f"    [router] HTTP 400 (prompt+max tokens > context); reducing max_output_tokens to {cap}")
                    continue
                if status not in RETRYABLE_STATUS and status < 597:
                    raise CallError(f"HTTP {status} is not retryable: {err[:500]}")
                wait = min(60.0, 5.0 * (attempt + 1)) + random.random()
                self.log(f"    [router] HTTP {status} on attempt {attempt + 1}, sleeping {wait:.0f}s: {err[:160]}")
                time.sleep(wait)
                continue
            got_response = True
            content = text if text else reasoning
            ntok = usage.get("output_tokens")
            clamped = finish == "length" and cap and (ntok or 0) < CLAMP_RATIO * cap
            last_usage = usage
            if content.strip() and not clamped:
                return Reply(content, finish, reasoning, usage, attempt + 1, discarded, False,
                             int((time.time() - started) * 1000), self.model)
            why = f"clamped at {ntok} tok (< cap)" if clamped else f"empty reply (finish={finish})"
            self.log(f"    [router] {why}; retrying (attempt {attempt + 1})")
            discarded.append({"attempt": attempt + 1, "why": why, "finish": finish, "output_tokens": ntok,
                              "content": content[-4000:], "reasoning": reasoning[-4000:]})
            last = content or last
            last_reason = reasoning or last_reason
            time.sleep(1)
        self.log(f"    [router] gave up after {len(discarded)} attempt(s) without a clean completion "
                 f"(got_response={got_response})")
        return Reply(last, finish or "error", last_reason, last_usage, len(discarded), discarded,
                     not last.strip(), int((time.time() - started) * 1000), self.model)


def _output_text(resp):
    text, reasoning = [], []
    for item in resp.get("output") or []:
        for part in item.get("content") or []:
            t = part.get("text") or ""
            if part.get("type") == "output_text":
                text.append(t)
            elif part.get("type") == "reasoning_text":
                reasoning.append(t)
    return "".join(text), "".join(reasoning)
