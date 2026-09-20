"""
tests/test_loopguard.py — unittest suite for loopguard.py

Run with:  python3 -m unittest discover -s tests
"""

import os
import subprocess
import sys
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
    """_hash_args() must be stable regardless of dict key order."""

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


class TestAnalyseLines(unittest.TestCase):
    """analyse_lines() integration of parser + detectors."""

    def test_loop_in_lines(self):
        lines = [
            '{"tool":"bash","tool_input":{"command":"ls"}}',
            '{"tool":"bash","tool_input":{"command":"ls"}}',
            '{"tool":"bash","tool_input":{"command":"ls"}}',
        ]
        detected, kind, detail = loopguard.analyse_lines(lines, loop_n=3)
        self.assertTrue(detected)
        self.assertEqual(kind, "LOOP")

    def test_oscillation_in_lines(self):
        lines = [
            '{"tool":"read","tool_input":{"path":"/a"}}',
            '{"tool":"write","tool_input":{"path":"/b"}}',
            '{"tool":"read","tool_input":{"path":"/a"}}',
            '{"tool":"write","tool_input":{"path":"/b"}}',
        ]
        detected, kind, _ = loopguard.analyse_lines(lines)
        self.assertTrue(detected)
        self.assertEqual(kind, "OSCILLATION")

    def test_clean_lines(self):
        lines = [
            '{"tool":"bash","tool_input":{"command":"ls"}}',
            '{"tool":"read","tool_input":{"path":"/x"}}',
            '{"tool":"write","tool_input":{"path":"/y","data":"foo"}}',
        ]
        detected, _, _ = loopguard.analyse_lines(lines)
        self.assertFalse(detected)

    def test_malformed_lines_no_crash(self):
        lines = [
            '{"tool":"bash","tool_input":{}}',
            "{bad json here!!!",
            "truncated",
            '{"tool":"bash","tool_input":{}}',
        ]
        # Should not raise; may or may not detect a loop depending on N
        try:
            loopguard.analyse_lines(lines)
        except Exception as exc:  # noqa: BLE001
            self.fail(f"analyse_lines raised unexpectedly: {exc}")

    def test_empty_lines_no_crash(self):
        detected, _, _ = loopguard.analyse_lines([])
        self.assertFalse(detected)


# ── Fixture-based CLI integration tests ───────────────────────────────────────

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
