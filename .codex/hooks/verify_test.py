#!/usr/bin/env python3
"""Unit tests for verify.py — the parts that decide WITHOUT calling a model:
verdict parsing, the turn-changed signal, request extraction, and the round counter.
The verifier subprocess itself is exercised by the live end-to-end test, not here.

Run: python3 .codex/hooks/verify_test.py
"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("verify", HERE / "verify.py")
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)


class ParseVerdict(unittest.TestCase):
    def test_pass_block_at_end(self):
        text = ('thinking...\n<<<VERDICT\n{"result":"pass","criteria":[]}\nVERDICT\n')
        self.assertEqual(verify.parse_verdict(text)["result"], "pass")

    def test_last_block_wins(self):
        text = ('<<<VERDICT\n{"result":"fail"}\nVERDICT\n'
                'reconsidering\n<<<VERDICT\n{"result":"pass"}\nVERDICT')
        self.assertEqual(verify.parse_verdict(text)["result"], "pass")

    def test_no_block_is_none(self):
        self.assertIsNone(verify.parse_verdict("no verdict here"))

    def test_malformed_json_is_none(self):
        self.assertIsNone(verify.parse_verdict("<<<VERDICT\n{not json}\nVERDICT"))

    def test_trailing_prose_after_close(self):
        text = '<<<VERDICT\n{"result":"pass"}\nVERDICT\ndone.'
        self.assertEqual(verify.parse_verdict(text)["result"], "pass")


class TurnChanged(unittest.TestCase):
    def _row(self, **payload):
        return {"payload": payload}

    def test_command_execution_counts(self):
        rows = [self._row(type="item_completed", turn_id="T1",
                          item={"type": "CommandExecution", "command": "make"})]
        self.assertTrue(verify.turn_changed_something(rows, "T1"))

    def test_qa_only_turn_does_not(self):
        rows = [self._row(type="item_completed", turn_id="T1",
                          item={"type": "AgentMessage", "content": "hi"}),
                self._row(type="item_completed", turn_id="T1",
                          item={"type": "Reasoning"})]
        self.assertFalse(verify.turn_changed_something(rows, "T1"))

    def test_spawn_agent_call_counts(self):
        rows = [self._row(type="function_call", name="spawn_agent", turn_id="T1")]
        self.assertTrue(verify.turn_changed_something(rows, "T1"))

    def test_other_turn_ignored(self):
        rows = [self._row(type="item_completed", turn_id="T2",
                          item={"type": "CommandExecution", "command": "make"})]
        # T2 is a different turn; T1 saw nothing, and there IS a mutating item, so the
        # fallback (no matching turn seen) would fire — but we DID see T1? No: we never
        # saw T1 here, so fallback applies and returns True. Assert the documented
        # fallback behaviour explicitly.
        self.assertTrue(verify.turn_changed_something(rows, "T1"))


class Requests(unittest.TestCase):
    def test_user_only_not_developer(self):
        rows = [
            {"payload": {"type": "message", "role": "developer",
                         "content": [{"type": "text", "text": "AGENTS.md ..."}]}},
            {"payload": {"type": "message", "role": "user",
                         "content": [{"type": "text", "text": "do the thing"}]}},
            {"payload": {"type": "message", "role": "assistant",
                         "content": [{"type": "Text", "text": "done"}]}},
        ]
        self.assertEqual(verify.user_requests(rows), ["do the thing"])
        self.assertEqual(verify.last_assistant(rows), "done")


class Rounds(unittest.TestCase):
    def setUp(self):
        self._orig = verify.STATE_DIR
        self._tmp = tempfile.TemporaryDirectory()
        verify.STATE_DIR = Path(self._tmp.name)

    def tearDown(self):
        verify.STATE_DIR = self._orig
        self._tmp.cleanup()

    def test_increment_and_clear(self):
        k = "goal:abc"
        self.assertEqual(verify.get_round(k), 0)
        verify.set_round(k, 1)
        verify.set_round(k, 2)
        self.assertEqual(verify.get_round(k), 2)
        verify.clear_round(k)
        self.assertEqual(verify.get_round(k), 0)


class FailureMessage(unittest.TestCase):
    def test_no_verdict_is_failclosed_language(self):
        msg = verify.format_failure(None, 1, capped=False)
        self.assertIn("no parseable verdict", msg)
        self.assertIn("round 1", msg)

    def test_cap_orders_disclosure(self):
        v = {"result": "fail", "criteria": [
            {"criterion": "tests pass", "verdict": "fail", "evidence": "pytest: 3 failed"}],
            "unverified": ["tests pass"], "fix_instructions": "fix the assertion"}
        msg = verify.format_failure(v, verify.MAX_ROUNDS, capped=True)
        self.assertIn("unverified", msg.lower())
        self.assertIn("blocked", msg.lower())
        self.assertIn("tests pass", msg)


class DecisionPath(unittest.TestCase):
    """verify_and_decide with the model call and git stubbed, so block/allow/tamper and
    the round counter are tested deterministically."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved = (verify.STATE_DIR, verify.run_verifier,
                       verify.git_diff_fingerprint, verify.user_requests)
        verify.STATE_DIR = Path(self._tmp.name)
        verify.user_requests = lambda rows: ["do X"]
        verify.git_diff_fingerprint = lambda cwd: "SAME"
        self.blocked = []

    def tearDown(self):
        (verify.STATE_DIR, verify.run_verifier,
         verify.git_diff_fingerprint, verify.user_requests) = self._saved
        self._tmp.cleanup()

    def _decide(self, key="goal:t"):
        called = {}

        def on_fail(msg):
            called["msg"] = msg
        try:
            verify.verify_and_decide([], "T1", "claim", "s", key, on_fail, ".")
            called["allowed"] = True
        except SystemExit:
            called["allowed"] = True  # allow() exits 0
        return called

    def test_pass_allows_and_clears(self):
        verify.set_round("goal:t", 2)
        verify.run_verifier = lambda p, c: '<<<VERDICT\n{"result":"pass"}\nVERDICT'
        r = self._decide()
        self.assertNotIn("msg", r)               # no block
        self.assertEqual(verify.get_round("goal:t"), 0)  # cleared

    def test_fail_blocks_and_increments(self):
        verify.run_verifier = lambda p, c: '<<<VERDICT\n{"result":"fail","criteria":[]}\nVERDICT'
        r = self._decide()
        self.assertIn("did NOT confirm", r["msg"])
        self.assertEqual(verify.get_round("goal:t"), 1)

    def test_no_verdict_fails_closed(self):
        verify.run_verifier = lambda p, c: "the model said nothing parseable"
        r = self._decide()
        self.assertIn("no parseable verdict", r["msg"])
        self.assertEqual(verify.get_round("goal:t"), 1)

    def test_tamper_voids_verdict(self):
        # git fingerprint changes across the run => verifier edited tracked code.
        seq = iter(["BEFORE", "AFTER"])
        verify.git_diff_fingerprint = lambda cwd: next(seq)
        verify.run_verifier = lambda p, c: '<<<VERDICT\n{"result":"pass"}\nVERDICT'
        r = self._decide()
        self.assertIn("modified tracked files", r["msg"])

    def test_cap_reached_language(self):
        verify.set_round("goal:t", verify.MAX_ROUNDS - 1)
        verify.run_verifier = lambda p, c: '<<<VERDICT\n{"result":"fail"}\nVERDICT'
        r = self._decide()
        self.assertIn("blocked", r["msg"].lower())
        self.assertEqual(verify.get_round("goal:t"), verify.MAX_ROUNDS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
