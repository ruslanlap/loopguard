#!/usr/bin/env python3
"""
loopguard.py — Watchdog for AI CLI agents.

Detects loops and stalls in JSONL session transcripts (Claude Code, Codex,
Gemini CLI layouts; any {"tool":...} JSONL) and alerts via stdout + Telegram.

Usage:
    python3 loopguard.py watch <path> [options]
    python3 loopguard.py watch <path> --once
    python3 loopguard.py watch <path> --from-start

Exit codes: 0 healthy, 1 incident detected (--once), 2 usage error.
"""

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

# ── Defaults ──────────────────────────────────────────────────────────────────
LOOP_N = 3        # how many repeats within window triggers LOOP
LOOP_M = 20       # sliding window size (last M events)
STALL_S = 300     # seconds of no new lines → STALL
POLL_INTERVAL = 2 # seconds between file polls (watch mode)

# ponytail: STALL detection is wall-clock based; NTP jumps could confuse it


# ── Argument parsing ───────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="loopguard",
        description="Watchdog for AI CLI agent transcripts.",
    )
    sub = p.add_subparsers(dest="command")

    watch = sub.add_parser("watch", help="Watch a transcript file or directory.")
    watch.add_argument("path", help="Path to a .jsonl file or directory.")
    watch.add_argument(
        "--once",
        action="store_true",
        help="Analyse existing content and exit (no polling).",
    )
    watch.add_argument(
        "--from-start",
        action="store_true",
        dest="from_start",
        help="Process the file from the beginning (default: tail from end).",
    )
    watch.add_argument(
        "--loop-n",
        type=int,
        default=LOOP_N,
        metavar="N",
        help=f"Repeat count threshold for LOOP detection (default: {LOOP_N}).",
    )
    watch.add_argument(
        "--loop-m",
        type=int,
        default=LOOP_M,
        metavar="M",
        help=f"Sliding window size for LOOP detection (default: {LOOP_M}).",
    )
    watch.add_argument(
        "--osc-n",
        type=int,
        default=4,
        metavar="N",
        help="Window size for OSCILLATION detection (default: 4).",
    )
    watch.add_argument(
        "--stall-s",
        type=int,
        default=STALL_S,
        metavar="S",
        help=f"Seconds of inactivity before STALL alert (default: {STALL_S}).",
    )

    replay = sub.add_parser("replay", help="Print an annotated event-by-event replay with detector verdicts.")
    replay.add_argument("path", help="Path to a .jsonl file or directory.")
    return p


# ── JSONL parsing ──────────────────────────────────────────────────────────────

def parse_events(raw_line: str) -> list[dict]:
    """
    Parse a JSONL line into a list of normalised event dicts.

    Claude Code batches several tool_use blocks into one line; we return them
    all. Malformed lines return a single generic-activity event.

    Each dict has keys:
        timestamp (str|None), tool (str|None), args_hash (str|None)
    """
    generic = [{"timestamp": None, "tool": None, "args_hash": None}]
    try:
        obj = json.loads(raw_line)
        if not isinstance(obj, dict):
            return generic
        ts = obj.get("timestamp") or obj.get("ts")

        tool_name = None
        tool_args = None
        events: list[dict] = []

        if "tool" in obj:
            tool_name = obj["tool"]
            tool_args = obj.get("tool_input") or obj.get("tool_args") or {}
        elif "toolName" in obj:
            tool_name = obj["toolName"]
            tool_args = obj.get("toolArgs") or obj.get("args") or {}
        elif obj.get("type") == "tool_use":
            tool_name = obj.get("name")
            tool_args = obj.get("input") or {}
        elif obj.get("type") == "function_call":
            tool_name = obj.get("name")
            raw = obj.get("arguments")
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except json.JSONDecodeError:
                    raw = {}
            tool_args = raw or {}
        elif isinstance(obj.get("functionCall"), dict):
            fc = obj["functionCall"]
            tool_name = fc.get("name")
            tool_args = fc.get("args") or fc.get("parameters") or {}
        else:
            # Claude Code .jsonl: message.content may hold SEVERAL tool_use
            # blocks per line — extract all of them, not just the first.
            msg = obj.get("message") or {}
            content = msg.get("content") or []
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        ev = {"timestamp": ts, "tool": block.get("name"),
                              "args_hash": _hash_args(block.get("input") or {})}
                        events.append(ev)
                if events:
                    return events

        if tool_name:
            return [{"timestamp": ts, "tool": tool_name, "args_hash": _hash_args(tool_args)}]
        return [{"timestamp": ts, "tool": None, "args_hash": None}]
    except (json.JSONDecodeError, TypeError, ValueError):
        return generic


def parse_event(raw_line: str) -> dict:
    """Back-compat single-event view of parse_events()."""
    return parse_events(raw_line)[0]


def _hash_args(args) -> str:
    """
    Stable hash of tool arguments regardless of dict key order.
    JSON-dump with sorted_keys, then sha1 (first 16 hex chars for brevity).
    """
    if not isinstance(args, dict):
        args = {}
    canonical = json.dumps(args, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(canonical.encode()).hexdigest()[:16]


def make_fingerprint(tool: str, args_hash: str) -> str:
    """Combine tool name + args hash into a single opaque fingerprint."""
    return f"{tool}:{args_hash}"


# ── Detectors ─────────────────────────────────────────────────────────────────

def detect_loop(window: deque, n: int) -> tuple[bool, str]:
    """
    LOOP: same fingerprint appears >= n times in the current window.

    Returns (detected, offending_fingerprint_or_empty).
    """
    counts: dict[str, int] = {}
    for fp in window:
        if fp is None:
            continue
        counts[fp] = counts.get(fp, 0) + 1
        if counts[fp] >= n:
            return True, fp
    return False, ""


def detect_oscillation(window: deque, n: int = 4) -> tuple[bool, str]:
    """
    OSCILLATION: A→B→A→B pattern — exactly 2 unique fingerprints alternating
    across the last n events (default 4).

    Returns (detected, "A,B" fingerprints or empty).
    """
    recent = [fp for fp in window if fp is not None]
    if len(recent) < n:
        return False, ""
    tail = recent[-n:]
    # Pattern: [a, b, a, b] where a != b and tail[0]==tail[2] and tail[1]==tail[3]
    a, b, c, d = tail
    if a == c and b == d and a != b:
        return True, f"{a},{b}"
    return False, ""


# ── Alerting ──────────────────────────────────────────────────────────────────

def format_alert(kind: str, detail: str) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"[loopguard] {now} ⚠  {kind}: {detail}"


def send_telegram(token: str, chat_id: str, text: str) -> None:
    """
    Send a Telegram message via the Bot API using urllib (no requests lib).
    Sets a User-Agent header to avoid Telegram 403 responses.
    Failures are logged to stderr but never crash the watcher.
    """
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "loopguard/1.0 (https://github.com/ruslanlap/loopguard)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status not in (200, 201):
                print(
                    f"[loopguard] Telegram returned HTTP {resp.status}", file=sys.stderr
                )
    except urllib.error.URLError as exc:
        print(f"[loopguard] Telegram send failed: {exc}", file=sys.stderr)


def alert(kind: str, detail: str) -> None:
    """Print alert to stdout; send Telegram if credentials in environment."""
    msg = format_alert(kind, detail)
    print(msg, flush=True)

    tg_token = os.environ.get("LOOPGUARD_TG_TOKEN", "")
    tg_chat = os.environ.get("LOOPGUARD_TG_CHAT_ID", "")
    if tg_token and tg_chat:
        send_telegram(tg_token, tg_chat, msg)


# ── File discovery ─────────────────────────────────────────────────────────────

def find_newest_jsonl(directory: str) -> str | None:
    """
    Recursively find the newest *.jsonl file under *directory*.
    Returns the path or None if none found.
    """
    best = None
    best_mtime = -1.0
    for root, _dirs, files in os.walk(directory):
        for fname in files:
            if fname.endswith(".jsonl"):
                full = os.path.join(root, fname)
                try:
                    mtime = os.path.getmtime(full)
                except OSError:
                    continue
                if mtime > best_mtime:
                    best_mtime = mtime
                    best = full
    return best


# ── Core analysis logic ────────────────────────────────────────────────────────

# ── Watch loop ─────────────────────────────────────────────────────────────────

def watch_file(
    filepath: str,
    *,
    once: bool = False,
    from_start: bool = False,
    loop_n: int = LOOP_N,
    loop_m: int = LOOP_M,
    osc_n: int = 4,
    stall_s: int = STALL_S,
) -> int:
    """
    Watch *filepath* for loop/stall incidents.

    Returns 0 (healthy) or 1 (incident) according to TASK.md exit codes.
    In watch mode (once=False) this function runs forever.
    """
    # Determine starting byte offset
    if from_start or once:
        offset = 0
    else:
        try:
            offset = os.path.getsize(filepath)
        except OSError:
            offset = 0

    window: deque = deque(maxlen=loop_m)
    incident_reported = False  # one alert per incident; reset on new unique call
    last_activity = time.monotonic()  # wall time of last new line
    stall_reported = False

    def _process_new_lines(file_handle) -> int:
        """Read new lines from current offset; update window; return 1 if incident."""
        nonlocal incident_reported, last_activity, stall_reported

        new_data = False
        for raw in file_handle:
            raw = raw.strip()
            if not raw:
                continue
            new_data = True
            last_activity = time.monotonic()
            stall_reported = False  # reset stall on new activity

            for ev in parse_events(raw):
                if ev["tool"] is not None:
                    fp = make_fingerprint(ev["tool"], ev["args_hash"])
                else:
                    fp = None

                # A *new unique* tool call resets the incident flag
                if fp is not None and incident_reported:
                    seen_fps = set(f for f in window if f is not None)
                    if fp not in seen_fps:
                        incident_reported = False

                window.append(fp)

                osc, osc_detail = detect_oscillation(window, osc_n)
                if osc and not incident_reported:
                    alert("OSCILLATION", f"pattern {osc_detail}")
                    incident_reported = True
                    if once:
                        return 1

                loop_hit, loop_fp = detect_loop(window, loop_n)
                if loop_hit and not incident_reported:
                    tool_name = loop_fp.split(":")[0]
                    alert(
                        "LOOP",
                        f"tool '{tool_name}' repeated {loop_n}+ times in last {loop_m} events",
                    )
                    incident_reported = True
                    if once:
                        return 1
        # ponytail: new_data tracked for callers that may want it; unused now
        return 0

    # ── --once mode: read everything and exit ──────────────────────────────────
    if once:
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as fh:
                fh.seek(offset)
                result = _process_new_lines(fh)
        except OSError as exc:
            print(f"[loopguard] Cannot open {filepath}: {exc}", file=sys.stderr)
            return 2
        return result

    # ── Poll mode ──────────────────────────────────────────────────────────────
    try:
        fh = open(filepath, "r", encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"[loopguard] Cannot open {filepath}: {exc}", file=sys.stderr)
        return 2

    try:
        fh.seek(offset)
        while True:
            _process_new_lines(fh)

            # STALL detection: check if mtime is old and we haven't had new lines
            try:
                mtime_age = time.time() - os.path.getmtime(filepath)
            except OSError:
                mtime_age = 0

            idle_secs = time.monotonic() - last_activity
            if mtime_age >= stall_s and idle_secs >= stall_s and not stall_reported:
                alert("STALL", f"no new events for {int(idle_secs)}s")
                stall_reported = True
                incident_reported = True  # suppress further alerts until reset

            time.sleep(POLL_INTERVAL)
    finally:
        fh.close()


# ── Replay ─────────────────────────────────────────────────────────────────────

def replay_file(filepath: str, *, loop_n: int = LOOP_N, loop_m: int = LOOP_M, osc_n: int = 4) -> int:
    """
    Print one annotated line per tool event with the detector verdict at that
    point. Doubles as a debug surface and an honest terminal demo.
    """
    window: deque = deque(maxlen=loop_m)
    exit_code = 0
    try:
        fh = open(filepath, "r", encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"[loopguard] Cannot open {filepath}: {exc}", file=sys.stderr)
        return 2

    with fh:
        for i, raw in enumerate(fh, 1):
            raw = raw.strip()
            if not raw:
                continue
            for ev in parse_events(raw):
                if ev["tool"] is None:
                    continue
                fp = make_fingerprint(ev["tool"], ev["args_hash"])
                window.append(fp)
                osc, _ = detect_oscillation(window, osc_n)
                loop_hit, _ = detect_loop(window, loop_n)
                verdict = "⚠ OSCILLATION" if osc else ("⚠ LOOP" if loop_hit else "ok")
                if osc or loop_hit:
                    exit_code = 1
                print(f"{i:>4}  {ev['tool']:<24} {verdict}", flush=True)
    return exit_code


# ── Entry point ────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to cp1252; alerts contain UTF-8 emoji. Force UTF-8 or
    # printing crashes with UnicodeEncodeError and the alert never reaches the user.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 2

    target = args.path

    # Resolve directory → newest .jsonl file
    if os.path.isdir(target):
        found = find_newest_jsonl(target)
        if not found:
            print(f"[loopguard] No .jsonl files found under {target}", file=sys.stderr)
            return 2
        target = found
        print(f"[loopguard] Watching {target}", file=sys.stderr)

    if not os.path.isfile(target):
        print(f"[loopguard] File not found: {target}", file=sys.stderr)
        return 2

    if args.command == "replay":
        return replay_file(target)

    return watch_file(
        target,
        once=args.once,
        from_start=args.from_start,
        loop_n=args.loop_n,
        loop_m=args.loop_m,
        osc_n=args.osc_n,
        stall_s=args.stall_s,
    )


if __name__ == "__main__":
    sys.exit(main())
