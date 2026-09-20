"""
tests/test_loopguard.py — unittest suite for loopguard.py

Run with:  python3 -m unittest discover -s tests
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from collections import deque
import unittest

# Allow importing loopguard from the parent directory
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import loopguard  # noqa: E402 (after sys.path tweak)

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures")


def fixture(name: str) -> str:
    return os.path.join(FIXTURES, name)


def run_once(path: str, extra_args: list[str] | None = None) -> int:
    """Run `python3 loopguard.py watch <path> --once --from-start` and return exit code."""
    script = os.path.join(os.path.dirname(__file__), "..", "loopguard.py")
    cmd = [sys.executable, script, "watch", path, "--once", "--from-start"]
    if extra_args:
        cmd.extend(extra_args)
    result = subprocess.run(cmd, capture_output=True)
    return result.returncode


# ── Unit tests for internal helpers ───────────────────────────────────────────

class TestParseEvent(unittest.TestCase):
    """parse_event() should handle multiple schemas and never crash."""

    def test_generic_schema(self):
        raw = '{"tool": "bash", "tool_input": {"command": "ls"}}'
        ev = loopguard.parse_event(raw)
        self.assertEqual(ev["tool"], "bash")
        self.assertIsNotNone(ev["args_hash"])

    def test_tool_use_schema(self):
        raw = '{"type": "tool_use", "name": "read_file", "input": {"path": "/tmp/x"}}'
        ev = loopguard.parse_event(raw)
        self.assertEqual(ev["tool"], "read_file")

    def test_nested_message_schema(self):
        raw = '{"message": {"content": [{"type": "tool_use", "name": "write_file", "input": {}}]}}'
        ev = loopguard.parse_event(raw)
        self.assertEqual(ev["tool"], "write_file")

    def test_missing_tool_returns_none(self):
        raw = '{"timestamp": "2024-01-01T00:00:00Z"}'
        ev = loopguard.parse_event(raw)
        self.assertIsNone(ev["tool"])
        self.assertIsNone(ev["args_hash"])

    def test_malformed_json_no_crash(self):
        ev = loopguard.parse_event("{this is not valid json!!!")
        self.assertIsNone(ev["tool"])

    def test_empty_string_no_crash(self):
        ev = loopguard.parse_event("")
        self.assertIsNone(ev["tool"])

    def test_non_dict_json_no_crash(self):
        ev = loopguard.parse_event("[1,2,3]")
        self.assertIsNone(ev["tool"])


class TestHashArgs(unittest.TestCase):
    """loopguard._hash_args() must be stable regardless of dict key order."""

    def test_key_order_independent(self):
        h1 = loopguard._hash_args({"b": 2, "a": 1})
        h2 = loopguard._hash_args({"a": 1, "b": 2})
        self.assertEqual(h1, h2)

    def test_different_args_different_hash(self):
        h1 = loopguard._hash_args({"cmd": "ls"})
        h2 = loopguard._hash_args({"cmd": "pwd"})
        self.assertNotEqual(h1, h2)

    def test_empty_dict(self):
        h = loopguard._hash_args({})
        self.assertIsInstance(h, str)
        self.assertGreater(len(h), 0)

    def test_non_dict_treated_as_empty(self):
        h1 = loopguard._hash_args(None)
        h2 = loopguard._hash_args({})
        self.assertEqual(h1, h2)


class TestDetectLoop(unittest.TestCase):
    """detect_loop() fires when a fingerprint appears >= N times."""

    def _make_window(self, items, maxlen=20):
        from collections import deque
        d = deque(maxlen=maxlen)
        d.extend(items)
        return d

    def test_loop_detected(self):
        window = self._make_window(["bash:abc", "bash:abc", "bash:abc"])
        detected, fp = loopguard.detect_loop(window, n=3)
        self.assertTrue(detected)
        self.assertEqual(fp, "bash:abc")

    def test_loop_not_detected_below_threshold(self):
        window = self._make_window(["bash:abc", "bash:abc"])
        detected, _ = loopguard.detect_loop(window, n=3)
        self.assertFalse(detected)

    def test_none_entries_ignored(self):
        window = self._make_window([None, None, "bash:abc", "bash:abc", "bash:abc"])
        detected, _ = loopguard.detect_loop(window, n=3)
        self.assertTrue(detected)


class TestDetectOscillation(unittest.TestCase):
    """detect_oscillation() fires on A→B→A→B patterns."""

    def _make_window(self, items, maxlen=20):
        from collections import deque
        d = deque(maxlen=maxlen)
        d.extend(items)
        return d

    def test_oscillation_detected(self):
        window = self._make_window(["read:aaa", "write:bbb", "read:aaa", "write:bbb"])
        detected, _ = loopguard.detect_oscillation(window)
        self.assertTrue(detected)

    def test_no_oscillation_same_tool(self):
        window = self._make_window(["bash:aaa", "bash:aaa", "bash:aaa", "bash:aaa"])
        detected, _ = loopguard.detect_oscillation(window)
        self.assertFalse(detected)

    def test_no_oscillation_too_short(self):
        window = self._make_window(["read:aaa", "write:bbb", "read:aaa"])
        detected, _ = loopguard.detect_oscillation(window)
        self.assertFalse(detected)

    def test_none_values_filtered(self):
        # None entries should not contribute to pattern
        window = self._make_window([None, "read:aaa", "write:bbb", "read:aaa", "write:bbb"])
        detected, _ = loopguard.detect_oscillation(window)
        self.assertTrue(detected)


def _run_lines(lines, tmpdir, extra=()):
    """Write lines to a temp .jsonl; run the real CLI path once; return CompletedProcess."""
    p = Path(tmpdir) / "s.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return subprocess.run(
        [sys.executable, "loopguard.py", "watch", str(p), "--once"] + list(extra),
        capture_output=True, timeout=20,
    )


class TestAnalyseLines(unittest.TestCase):
    """Detection integration via the real CLI path (watch --once)."""

    def test_loop_detected(self):
        with tempfile.TemporaryDirectory() as d:
            r = _run_lines(['{"tool":"bash","tool_input":{"cmd":"ls -la"}}'] * 3, d)
            self.assertEqual(r.returncode, 1)
            self.assertIn(b"LOOP", r.stdout)

    def test_oscillation_detected(self):
        with tempfile.TemporaryDirectory() as d:
            lines = [
                '{"tool":"read_file","tool_input":{"p":"a"}}',
                '{"tool":"write_file","tool_input":{"p":"b"}}',
            ] * 2
            r = _run_lines(lines, d)
            self.assertEqual(r.returncode, 1)
            self.assertIn(b"OSCILLATION", r.stdout)

    def test_clean_pass(self):
        with tempfile.TemporaryDirectory() as d:
            lines = [
                '{"tool":"bash","tool_input":{"cmd":"ls"}}',
                '{"tool":"read_file","tool_input":{"p":"a"}}',
                '{"tool":"write_file","tool_input":{"p":"b"}}',
            ]
            r = _run_lines(lines, d)
            self.assertEqual(r.returncode, 0)

    def test_malformed_lines_no_crash(self):
        with tempfile.TemporaryDirectory() as d:
            lines = ['{"tool":"bash", TRUNCATED', '{"tool":"bash"}', "not json at all"]
            r = _run_lines(lines, d)
            self.assertIn(r.returncode, (0, 1))  # no crash is the contract

    def test_empty_input(self):
        with tempfile.TemporaryDirectory() as d:
            r = _run_lines([], d)
            self.assertEqual(r.returncode, 0)


class TestCLILoopFixture(unittest.TestCase):
    """3 identical tool calls in fixture → exit 1."""

    def test_loop_exit_code(self):
        code = run_once(fixture("loop_tool.jsonl"))
        self.assertEqual(code, 1, "Expected exit 1 (LOOP detected) for loop_tool.jsonl")


class TestCLIOscillationFixture(unittest.TestCase):
    """A→B→A→B in fixture → exit 1."""

    def test_oscillation_exit_code(self):
        code = run_once(fixture("oscillation.jsonl"))
        self.assertEqual(code, 1, "Expected exit 1 (OSCILLATION) for oscillation.jsonl")


class TestCLICleanFixture(unittest.TestCase):
    """Varied calls → exit 0."""

    def test_clean_exit_code(self):
        code = run_once(fixture("clean.jsonl"))
        self.assertEqual(code, 0, "Expected exit 0 (clean) for clean.jsonl")


class TestCLIMalformedFixture(unittest.TestCase):
    """Malformed lines → no crash (exit 0 or 1, but never 2 or exception)."""

    def test_no_crash(self):
        code = run_once(fixture("malformed.jsonl"))
        self.assertIn(code, (0, 1), "Malformed fixture should exit 0 or 1, not crash")


class TestCLIUsageErrors(unittest.TestCase):
    """Usage errors → exit 2."""

    def test_no_command_exits_2(self):
        script = os.path.join(os.path.dirname(__file__), "..", "loopguard.py")
        result = subprocess.run([sys.executable, script], capture_output=True)
        self.assertEqual(result.returncode, 2)

    def test_nonexistent_file_exits_2(self):
        script = os.path.join(os.path.dirname(__file__), "..", "loopguard.py")
        result = subprocess.run(
            [sys.executable, script, "watch", "/nonexistent/path.jsonl", "--once"],
            capture_output=True,
        )
        self.assertEqual(result.returncode, 2)


class TestNoArgNormalisationStability(unittest.TestCase):
    """
    Regardless of dict key ordering in the source JSON, the same logical
    args must produce the same fingerprint (key-order stability).
    """

    def test_shuffled_keys_same_hash(self):
        import json

        # Simulate two events where the args dict has keys in different order
        raw_a = json.dumps({"tool": "bash", "tool_input": {"z": 9, "a": 1, "m": 5}})
        raw_b = json.dumps({"tool": "bash", "tool_input": {"a": 1, "m": 5, "z": 9}})

        ev_a = loopguard.parse_event(raw_a)
        ev_b = loopguard.parse_event(raw_b)

        self.assertEqual(
            ev_a["args_hash"],
            ev_b["args_hash"],
            "Args hash must be key-order independent",
        )


if __name__ == "__main__":
    unittest.main()


class TestMultiAgentFormats(unittest.TestCase):
    """Codex CLI function_call and Gemini CLI toolName/functionCall schemas."""

    def test_codex_function_call_args_string(self):
        line = '{"type":"function_call","name":"shell","arguments":"{\\"cmd\\": \\"ls\\"}"}'
        ev = loopguard.parse_event(line)
        self.assertEqual(ev["tool"], "shell")
        self.assertEqual(ev["args_hash"], loopguard._hash_args({"cmd": "ls"}))

    def test_codex_function_call_args_dict(self):
        line = '{"type":"function_call","name":"shell","arguments":{"cmd":"ls"}}'
        ev = loopguard.parse_event(line)
        self.assertEqual(ev["tool"], "shell")

    def test_codex_bad_args_string_no_crash(self):
        line = '{"type":"function_call","name":"shell","arguments":"not json{"}'
        ev = loopguard.parse_event(line)
        self.assertEqual(ev["tool"], "shell")
        self.assertEqual(ev["args_hash"], loopguard._hash_args({}))

    def test_gemini_toolname(self):
        line = '{"toolName":"run_shell_command","toolArgs":{"command":"ls"}}'
        ev = loopguard.parse_event(line)
        self.assertEqual(ev["tool"], "run_shell_command")
        self.assertEqual(ev["args_hash"], loopguard._hash_args({"command": "ls"}))

    def test_gemini_function_call_nested(self):
        line = '{"functionCall":{"name":"read_file","args":{"path":"x.py"}}}'
        ev = loopguard.parse_event(line)
        self.assertEqual(ev["tool"], "read_file")
        self.assertEqual(ev["args_hash"], loopguard._hash_args({"path": "x.py"}))

    def test_codex_loop_detected_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.jsonl"
            calls = ['{"type":"function_call","name":"shell","arguments":"{\\"cmd\\": \\"ls -la\\"}"}'] * 3
            p.write_text("\n".join(calls) + "\n")
            r = subprocess.run([sys.executable, "loopguard.py", "watch", str(p), "--once", "--from-start"],
                               capture_output=True, timeout=15)
            self.assertEqual(r.returncode, 1)
            self.assertIn(b"LOOP", r.stdout)


class TestBrainstormFixes(unittest.TestCase):
    """W1 multi-block lines, W3 --once reads from start, W2 --osc-n, replay."""

    def test_multi_block_line_all_extracted(self):
        line = ('{"timestamp":"2026-09-20T10:00:00Z","message":{"content":['
                '{"type":"tool_use","name":"Bash","input":{"cmd":"ls"}},'
                '{"type":"tool_use","name":"Bash","input":{"cmd":"ls"}}]}}')
        evs = loopguard.parse_events(line)
        self.assertEqual(len(evs), 2)
        self.assertEqual([e["tool"] for e in evs], ["Bash", "Bash"])
        self.assertEqual(evs[0]["args_hash"], evs[1]["args_hash"])

    def test_multi_block_line_loop_detected_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.jsonl"
            line = ('{"message":{"content":['
                    '{"type":"tool_use","name":"Bash","input":{"cmd":"ls -la"}},'
                    '{"type":"tool_use","name":"Bash","input":{"cmd":"ls -la"}}]}}')
            p.write_text("\n".join([line] * 2) + "\n")  # 4 identical calls in 2 lines
            r = subprocess.run([sys.executable, "loopguard.py", "watch", str(p), "--once"],
                               capture_output=True, timeout=20)
            self.assertEqual(r.returncode, 1)
            self.assertIn(b"LOOP", r.stdout)

    def test_once_reads_whole_file_without_from_start(self):
        # W3: --once must analyse the full file even without --from-start
        with tempfile.TemporaryDirectory() as d:
            r = _run_lines(['{"tool":"bash","tool_input":{"cmd":"ls -la"}}'] * 3, d)
            self.assertEqual(r.returncode, 1)
            self.assertIn(b"LOOP", r.stdout)

    def test_osc_n_tunable(self):
        # read→write→read→write→read→write = oscillation with n=4 but also n=6
        fps = ["read:x", "write:y"] * 3
        w = deque(fps, maxlen=20)
        self.assertTrue(loopguard.detect_oscillation(w, 4)[0])
        w2 = deque(fps[:4], maxlen=20)
        self.assertFalse(loopguard.detect_oscillation(w2, 6)[0])  # too short for 6

    def test_replay_detects_loop(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.jsonl"
            p.write_text("\n".join(['{"tool":"bash","tool_input":{"cmd":"ls -la"}}'] * 3) + "\n")
            r = subprocess.run([sys.executable, "loopguard.py", "replay", str(p)],
                               capture_output=True, timeout=20)
            self.assertEqual(r.returncode, 1)
            self.assertIn(b"LOOP", r.stdout)
            self.assertIn(b"bash", r.stdout)
