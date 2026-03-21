#!/usr/bin/env python3
"""
SmolClaw Flight Recorder Analyzer — Phase 6.1
Parse flight_recorder.jsonl for patterns, failure rates, and tool performance.

Usage:
  python3 flight_analysis.py              # full report
  python3 flight_analysis.py --failures   # show only failures
  python3 flight_analysis.py --tools      # per-tool breakdown
"""

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

FLIGHT_LOG = Path("/home/nizbot1/smolclaw/flight_recorder.jsonl")


def load_entries():
    """Load all flight log entries."""
    if not FLIGHT_LOG.exists():
        print("No flight log found.")
        return []
    entries = []
    for line in FLIGHT_LOG.read_text().strip().split("\n"):
        if line.strip():
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def tool_report(entries):
    """Per-tool success/failure breakdown."""
    tools = defaultdict(lambda: {"ok": 0, "fail": 0, "errors": Counter()})
    for e in entries:
        tool = e.get("tool", "unknown")
        if e.get("ok"):
            tools[tool]["ok"] += 1
        else:
            tools[tool]["fail"] += 1
            ec = e.get("error_class", "unknown")
            tools[tool]["errors"][ec] += 1

    print(f"\n{'Tool':<15} {'OK':>5} {'Fail':>5} {'Rate':>7}  Top Error")
    print("-" * 60)
    for tool in sorted(tools, key=lambda t: tools[t]["ok"] + tools[t]["fail"], reverse=True):
        t = tools[tool]
        total = t["ok"] + t["fail"]
        rate = t["ok"] / total * 100 if total else 0
        top_err = t["errors"].most_common(1)[0] if t["errors"] else ("—", 0)
        print(f"{tool:<15} {t['ok']:>5} {t['fail']:>5} {rate:>6.1f}%  {top_err[0]}({top_err[1]})")
    return tools


def failure_report(entries):
    """Show all failures with context."""
    failures = [e for e in entries if not e.get("ok")]
    if not failures:
        print("\nNo failures recorded.")
        return

    print(f"\n{'='*60}")
    print(f"  FAILURES ({len(failures)} total)")
    print(f"{'='*60}")

    # Group by error class
    by_class = defaultdict(list)
    for f in failures:
        by_class[f.get("error_class", "unknown")].append(f)

    for ec, items in sorted(by_class.items(), key=lambda x: -len(x[1])):
        print(f"\n  [{ec}] — {len(items)} occurrences")
        for item in items[:5]:  # show up to 5 per class
            tool = item.get("tool", "?")
            args = json.dumps(item.get("args", {}))[:60]
            preview = item.get("result_preview", "")[:80]
            print(f"    {tool}({args})")
            print(f"      → {preview}")


def recovery_pairs(entries):
    """Find failure→recovery pairs (same tool, fail then succeed)."""
    pairs = []
    for i, e in enumerate(entries):
        if not e.get("ok") and i + 1 < len(entries):
            next_e = entries[i + 1]
            if next_e.get("ok") and next_e.get("tool") == e.get("tool"):
                pairs.append((e, next_e))

    if pairs:
        print(f"\n{'='*60}")
        print(f"  RECOVERY PAIRS ({len(pairs)} found)")
        print(f"{'='*60}")
        for fail, success in pairs[:10]:
            tool = fail.get("tool", "?")
            fail_args = json.dumps(fail.get("args", {}))[:50]
            ok_args = json.dumps(success.get("args", {}))[:50]
            print(f"  {tool}: {fail_args}")
            print(f"    → {ok_args}")
    return pairs


def command_hallucinations(entries):
    """Find shell commands that failed — likely hallucinated flags."""
    shell_fails = [e for e in entries if e.get("tool") == "shell" and not e.get("ok")]
    if not shell_fails:
        return

    print(f"\n{'='*60}")
    print(f"  SHELL COMMAND FAILURES ({len(shell_fails)})")
    print(f"{'='*60}")
    for e in shell_fails:
        cmd = e.get("args", {}).get("command", "?")
        preview = e.get("result_preview", "")[:80]
        print(f"  $ {cmd}")
        print(f"    → {preview}")


def main():
    entries = load_entries()
    if not entries:
        return

    total = len(entries)
    ok = sum(1 for e in entries if e.get("ok"))
    fail = total - ok

    print(f"\n{'='*60}")
    print(f"  SMOLCLAW FLIGHT RECORDER ANALYSIS")
    print(f"  {total} entries | {ok} OK ({ok/total*100:.1f}%) | {fail} failures")
    if entries:
        print(f"  Period: {entries[0].get('ts', '?')[:19]} → {entries[-1].get('ts', '?')[:19]}")
    print(f"{'='*60}")

    if "--failures" in sys.argv:
        failure_report(entries)
        return

    if "--tools" in sys.argv:
        tool_report(entries)
        return

    # Full report
    tool_report(entries)
    failure_report(entries)
    recovery_pairs(entries)
    command_hallucinations(entries)

    print(f"\n  Run with --failures or --tools for focused views.")


if __name__ == "__main__":
    main()
