#!/usr/bin/env python3
"""Doctor Claude — Diagnostic & Teaching Harness for SmolClaw.

Claude Code drives this harness as Doctor/Professor.
SmolClaw (Smols) is the Patient/Student being tested, taught, and debugged.

Usage:
    python doctor_claude.py --health            # Check NUC servers, web UI, files
    python doctor_claude.py --e2e-clinic        # Send test queries and validate
    python doctor_claude.py --log-analysis      # Parse flight_recorder.jsonl
    python doctor_claude.py --list-tools        # Show SmolClaw's tools
    python doctor_claude.py --curriculum [mod]  # Run teaching exercises
    python doctor_claude.py --repl              # Interactive chat with Smols

Channel A: HTTP POST to web_ui.py /chat endpoint
Channel B: Direct function import from agent.py (tool testing)

No external dependencies — stdlib only (like SmolClaw itself).
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import urllib.error
from collections import defaultdict
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths & Config
# ---------------------------------------------------------------------------
SMOLCLAW_DIR = Path(__file__).resolve().parent
FLIGHT_RECORDER = SMOLCLAW_DIR / "flight_recorder.jsonl"
MEMORY_FILE = SMOLCLAW_DIR / "memory.md"
EPISODIC_FILE = SMOLCLAW_DIR / "episodic.jsonl"
AUTONOMY_FILE = SMOLCLAW_DIR / "autonomy_state.json"
CURRICULA_DIR = SMOLCLAW_DIR / "curricula"
PROGRESS_FILE = SMOLCLAW_DIR / "curriculum_progress.json"

# NUC cluster
NUCS = {
    "NUC1 (actor)":  {"host": "10.0.0.1", "port": 8090},
    "NUC2 (critic)": {"host": "10.0.0.2", "port": 8090},
    "NUC3 (memory)": {"host": "10.0.0.3", "port": 8090},
}
WEB_UI_URL = "http://127.0.0.1:8080"

# SmolClaw's 7 tools
SMOLCLAW_TOOLS = [
    {"name": "shell",      "description": "Execute shell commands", "risk": "varies"},
    {"name": "read_file",  "description": "Read file contents", "risk": "read_only"},
    {"name": "write_file", "description": "Write/create files", "risk": "write"},
    {"name": "remember",   "description": "Save to persistent memory", "risk": "write"},
    {"name": "recall",     "description": "Smart retrieval from memory", "risk": "read_only"},
    {"name": "scratchpad", "description": "Retrieve stashed large outputs", "risk": "read_only"},
    {"name": "web_search", "description": "Brave web search", "risk": "read_only"},
]

# E2E clinic probes — tests SmolClaw's tool selection and response quality
E2E_PROBES = [
    {
        "prompt": "What files are in /tmp?",
        "expect_tool": "shell",
        "description": "Should use shell (ls /tmp)",
    },
    {
        "prompt": "Read the first 5 lines of /etc/hostname",
        "expect_tool": "read_file",
        "alt_tools": ["shell"],
        "description": "Should use read_file or shell (head)",
    },
    {
        "prompt": "Hello, how are you doing today?",
        "expect_tool": None,
        "description": "Casual chat — should NOT use any tool",
    },
    {
        "prompt": "What's the current disk usage on this machine?",
        "expect_tool": "shell",
        "description": "Should use shell (df -h)",
    },
    {
        "prompt": "Remember that my favorite color is blue.",
        "expect_tool": "remember",
        "description": "Should use remember tool",
    },
    {
        "prompt": "What do you remember about me?",
        "expect_tool": "recall",
        "description": "Should use recall tool",
    },
    {
        "prompt": "Write a file at /tmp/smolclaw_test.txt with the text 'hello world'.",
        "expect_tool": "write_file",
        "description": "Should use write_file",
    },
    {
        "prompt": "Thanks! You're doing great.",
        "expect_tool": None,
        "description": "Casual thanks — should NOT use any tool",
    },
]

# ANSI colors
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


def _ok(msg: str) -> str:
    return f"  {GREEN}OK{RESET}  {msg}"


def _fail(msg: str) -> str:
    return f"  {RED}FAIL{RESET}  {msg}"


def _warn(msg: str) -> str:
    return f"  {YELLOW}WARN{RESET}  {msg}"


def _header(title: str) -> None:
    print(f"\n{BOLD}{CYAN}{'=' * 60}{RESET}")
    print(f"{BOLD}{CYAN}  {title}{RESET}")
    print(f"{BOLD}{CYAN}{'=' * 60}{RESET}\n")


# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only)
# ---------------------------------------------------------------------------
def http_get(url: str, timeout: float = 5.0) -> dict:
    """GET request, return parsed JSON or error dict."""
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e)}


def http_post_chat(prompt: str, timeout: float = 300.0) -> dict:
    """Send a chat message to SmolClaw's web_ui POST /chat endpoint.
    Reads SSE stream, collects tokens, returns final response."""
    payload = json.dumps({"message": prompt}).encode()
    req = urllib.request.Request(
        f"{WEB_UI_URL}/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            full_response = ""
            tokens_streamed = 0
            done = False
            while not done:
                raw = resp.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data: "):
                    continue
                try:
                    evt = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                if evt.get("type") == "token":
                    tokens_streamed += 1
                elif evt.get("type") == "done":
                    full_response = evt.get("response", "")
                    done = True
                elif evt.get("type") == "error":
                    return {"error": evt.get("text", "unknown error")}
            elapsed = time.time() - t0
            return {
                "text": full_response,
                "elapsed_s": round(elapsed, 2),
                "tokens_streamed": tokens_streamed,
            }
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Health Check
# ---------------------------------------------------------------------------
def check_llama_health(name: str, host: str, port: int) -> dict:
    """Check a llama-server's /health endpoint."""
    result = http_get(f"http://{host}:{port}/health", timeout=5)
    if result.get("status") == "ok":
        return {"status": "ok", "name": name}
    return {"status": "error", "name": name, "detail": result}


def run_health() -> None:
    """Full cluster health check."""
    _header("SmolClaw Health Check")

    # NUC llama-servers
    print(f"{BOLD}Llama Servers:{RESET}")
    all_ok = True
    for name, info in NUCS.items():
        result = check_llama_health(name, info["host"], info["port"])
        if result["status"] == "ok":
            print(_ok(f"{name}  {info['host']}:{info['port']}"))
        else:
            print(_fail(f"{name}  {info['host']}:{info['port']}  ({result.get('detail', {}).get('error', 'unreachable')})"))
            all_ok = False

    # Web UI
    print(f"\n{BOLD}Web UI:{RESET}")
    try:
        urllib.request.urlopen(WEB_UI_URL, timeout=3)
        print(_ok(f"web_ui.py on {WEB_UI_URL}"))
    except Exception as e:
        print(_fail(f"web_ui.py on {WEB_UI_URL}  ({e})"))
        all_ok = False

    # Key files
    print(f"\n{BOLD}Files:{RESET}")
    for label, path in [
        ("flight_recorder.jsonl", FLIGHT_RECORDER),
        ("memory.md", MEMORY_FILE),
        ("episodic.jsonl", EPISODIC_FILE),
        ("autonomy_state.json", AUTONOMY_FILE),
    ]:
        if path.exists():
            size_kb = path.stat().st_size / 1024
            print(_ok(f"{label} ({size_kb:.1f} KB)"))
        else:
            print(_warn(f"{label} not found"))

    # Autonomy state
    print(f"\n{BOLD}Autonomy State:{RESET}")
    if AUTONOMY_FILE.exists():
        try:
            state = json.loads(AUTONOMY_FILE.read_text())
            calls = state.get("daily_calls", 0)
            tokens = state.get("daily_tokens", 0)
            failures = state.get("failure_clusters", 0)
            print(f"  Calls: {calls}/1000  Tokens: {tokens}/500000  Failure clusters: {failures}")
        except Exception:
            print(_warn("Could not parse autonomy_state.json"))
    else:
        print(_warn("No autonomy state file"))

    # System resources on NUC1
    print(f"\n{BOLD}NUC1 Resources:{RESET}")
    try:
        mem = subprocess.run(["free", "-h"], capture_output=True, text=True, timeout=5)
        for line in mem.stdout.strip().split("\n"):
            if line.startswith("Mem:"):
                parts = line.split()
                print(f"  RAM: {parts[2]} used / {parts[1]} total ({parts[6]} available)")
    except Exception:
        pass
    try:
        load = subprocess.run(["uptime"], capture_output=True, text=True, timeout=5)
        load_match = re.search(r'load average: (.+)', load.stdout)
        if load_match:
            print(f"  Load: {load_match.group(1)}")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# E2E Clinic
# ---------------------------------------------------------------------------
# Internal pipeline stages — not user-facing tool selections
_INTERNAL_STAGES = {"grounding", "terminal_state", "memory_reject", "memory_commit", "critic"}


def _find_tools_in_flight_log(before_ts: float, after_ts: float,
                               include_internal: bool = False) -> list[str]:
    """Find tool executions in flight_recorder.jsonl within a time window."""
    tools: list[str] = []
    if not FLIGHT_RECORDER.exists():
        return tools
    try:
        lines = FLIGHT_RECORDER.read_text().strip().split("\n")[-100:]
        for line in lines:
            try:
                rec = json.loads(line)
                ts = rec.get("ts", 0)
                if isinstance(ts, str):
                    from datetime import datetime
                    ts = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                if before_ts <= ts <= after_ts and rec.get("tool"):
                    tool = rec["tool"]
                    if include_internal or tool not in _INTERNAL_STAGES:
                        tools.append(tool)
            except (json.JSONDecodeError, ValueError, KeyError):
                continue
    except Exception:
        pass
    return tools


def run_e2e_clinic(probes: list[dict] = None) -> dict:
    """Send test queries to SmolClaw and verify responses."""
    _header("E2E Clinic")

    probes = probes or E2E_PROBES
    results: dict[str, Any] = {}

    # Check web UI is up
    try:
        urllib.request.urlopen(WEB_UI_URL, timeout=3)
    except Exception:
        print(_fail(f"Web UI not reachable at {WEB_UI_URL} — is web_ui.py running?"))
        return {"error": "web_ui_not_reachable"}

    for probe in probes:
        prompt = probe["prompt"]
        expect = probe.get("expect_tool")
        alt_tools = probe.get("alt_tools", [])
        desc = probe.get("description", prompt[:40])

        print(f"  Probe: {desc}")
        print(f"    Sending: \"{prompt}\"")

        before_ts = time.time()
        resp = http_post_chat(prompt, timeout=300)
        after_ts = time.time()

        if "error" in resp:
            print(_fail(f"    Error: {resp['error']}"))
            results[prompt] = {"status": "ERROR", "error": resp["error"]}
            continue

        # Check flight log for tool usage
        time.sleep(1)
        tools = _find_tools_in_flight_log(before_ts, after_ts)

        passed = False
        if expect is None:
            # Expect NO tool — pass if response exists and no tools used
            passed = bool(resp.get("text")) and not tools
        else:
            valid_tools = [expect] + alt_tools
            passed = any(t in valid_tools for t in tools)

        status = "PASS" if passed else "FAIL"
        preview = (resp.get("text", "")[:100] + "...") if len(resp.get("text", "")) > 100 else resp.get("text", "(empty)")
        print(f"    Response ({resp.get('elapsed_s', '?')}s): {preview}")
        print(f"    Tools used: {tools or '(none)'}")
        color = GREEN if passed else RED
        print(f"    {color}{status}{RESET}  (expected: {expect or 'no tool'})\n")

        results[prompt] = {
            "status": status,
            "response_preview": preview,
            "tools_used": tools,
            "expected": expect,
            "elapsed_s": resp.get("elapsed_s"),
        }

    passed_count = sum(1 for r in results.values() if r.get("status") == "PASS")
    total = len(results)
    color = GREEN if passed_count == total else RED
    print(f"{BOLD}Result: {color}{passed_count}/{total} probes passed{RESET}")
    return results


# ---------------------------------------------------------------------------
# Log Analysis
# ---------------------------------------------------------------------------
def analyze_logs(path: Path = None, last_n: int = 500) -> dict:
    """Parse flight_recorder.jsonl for error patterns and tool stats."""
    _header("Flight Log Analysis")

    path = path or FLIGHT_RECORDER
    if not path.exists():
        print(_warn(f"Flight recorder not found: {path}"))
        return {"error": "not_found"}

    try:
        lines = path.read_text().strip().split("\n")
    except Exception as e:
        print(_fail(f"Could not read log: {e}"))
        return {"error": str(e)}

    lines = lines[-last_n:]
    print(f"Analyzing last {len(lines)} entries\n")

    tool_counts: dict[str, int] = defaultdict(int)
    tool_errors: dict[str, int] = defaultdict(int)
    tool_times: dict[str, list[float]] = defaultdict(list)
    error_patterns: list[dict] = []
    critic_blocks: list[dict] = []
    stalls: list[dict] = []

    for line in lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue

        tool = rec.get("tool", "")
        is_error = rec.get("is_error", False)

        if tool:
            tool_counts[tool] += 1
            if is_error:
                tool_errors[tool] += 1
                error_patterns.append({
                    "tool": tool,
                    "args": str(rec.get("args", ""))[:80],
                    "preview": str(rec.get("preview", ""))[:80],
                })

        # Detect critic blocks
        if tool == "critic" and "BLOCK" in str(rec.get("preview", "")):
            critic_blocks.append(rec)

        # Detect stalls
        if tool == "stalled":
            stalls.append(rec)

    # Report: tool usage summary
    print(f"{BOLD}Tool Usage:{RESET}")
    for tool, count in sorted(tool_counts.items(), key=lambda x: -x[1]):
        errors = tool_errors.get(tool, 0)
        rate = (count - errors) / count * 100 if count > 0 else 0
        color = GREEN if rate >= 80 else (YELLOW if rate >= 50 else RED)
        print(f"  {tool:20s}  calls={count:4d}  errors={errors:3d}  "
              f"success={color}{rate:.0f}%{RESET}")

    # Error patterns
    print(f"\n{BOLD}Issues Found:{RESET}")

    if error_patterns:
        print(_fail(f"{len(error_patterns)} tool errors"))
        # Group by tool
        by_tool: dict[str, int] = defaultdict(int)
        for ep in error_patterns:
            by_tool[ep["tool"]] += 1
        for tool, count in sorted(by_tool.items(), key=lambda x: -x[1])[:5]:
            print(f"    {tool}: {count} errors")
            # Show first error for this tool
            for ep in error_patterns:
                if ep["tool"] == tool:
                    print(f"      args: {ep['args']}")
                    print(f"      preview: {ep['preview']}")
                    break
    else:
        print(_ok("No tool errors"))

    if critic_blocks:
        print(_warn(f"{len(critic_blocks)} critic blocks"))
    else:
        print(_ok("No critic blocks"))

    if stalls:
        print(_warn(f"{len(stalls)} stall events"))
    else:
        print(_ok("No stalls"))

    # Repetition detection (same tool+args appearing 3+ times)
    call_sigs: dict[str, int] = defaultdict(int)
    for line in lines:
        try:
            rec = json.loads(line)
            sig = f"{rec.get('tool', '')}:{json.dumps(rec.get('args', ''), sort_keys=True)[:100]}"
            call_sigs[sig] += 1
        except (json.JSONDecodeError, TypeError):
            continue

    repeated = [(sig, count) for sig, count in call_sigs.items() if count >= 3 and sig.split(":")[0]]
    if repeated:
        print(f"\n{BOLD}Repeated Calls (possible loops):{RESET}")
        for sig, count in sorted(repeated, key=lambda x: -x[1])[:5]:
            print(_warn(f"{sig[:60]}  ({count}x)"))
    else:
        print(_ok("No repeated call patterns"))

    return {
        "tool_counts": dict(tool_counts),
        "tool_errors": dict(tool_errors),
        "error_patterns": len(error_patterns),
        "critic_blocks": len(critic_blocks),
        "stalls": len(stalls),
        "repeated_calls": len(repeated),
    }


# ---------------------------------------------------------------------------
# List Tools
# ---------------------------------------------------------------------------
def list_tools() -> None:
    """Show SmolClaw's tools."""
    _header("SmolClaw Tools")
    print(f"Total: {len(SMOLCLAW_TOOLS)} tools\n")
    for tool in SMOLCLAW_TOOLS:
        risk_color = GREEN if tool["risk"] == "read_only" else YELLOW
        print(f"  {BOLD}{tool['name']:20s}{RESET}  "
              f"risk={risk_color}{tool['risk']}{RESET}  "
              f"{tool['description']}")
    print()


# ---------------------------------------------------------------------------
# Curriculum Engine (inline — no external dependencies)
# ---------------------------------------------------------------------------
def load_curriculum_module(name: str) -> dict:
    """Load a curriculum module from curricula/*.json."""
    path = CURRICULA_DIR / name
    if not path.suffix:
        path = path.with_suffix(".json")
    if not path.exists():
        matches = list(CURRICULA_DIR.glob(f"*{name}*.json"))
        if matches:
            path = matches[0]
        else:
            raise FileNotFoundError(f"Curriculum module not found: {name}")
    return json.loads(path.read_text())


def _load_progress() -> dict:
    """Load curriculum progress."""
    if PROGRESS_FILE.exists():
        try:
            return json.loads(PROGRESS_FILE.read_text())
        except Exception:
            pass
    return {"runs": []}


def _save_progress(progress: dict) -> None:
    """Save curriculum progress."""
    PROGRESS_FILE.write_text(json.dumps(progress, indent=2))


def run_curriculum(module_filter: str = None) -> dict:
    """Run curriculum lessons, validate responses, track progress."""
    _header("Professor Claude — Teaching SmolClaw")

    if not CURRICULA_DIR.exists():
        print(_fail(f"Curricula directory not found: {CURRICULA_DIR}"))
        return {"error": "no_curricula_dir"}

    # Load modules
    modules = []
    for path in sorted(CURRICULA_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text())
            if module_filter and module_filter not in path.stem and module_filter not in data.get("name", ""):
                continue
            modules.append(data)
        except Exception:
            continue

    if not modules:
        print(_warn(f"No curriculum modules found" + (f" matching '{module_filter}'" if module_filter else "")))
        return {"error": "no_modules"}

    # Check web UI
    try:
        urllib.request.urlopen(WEB_UI_URL, timeout=3)
    except Exception:
        print(_fail(f"Web UI not reachable — SmolClaw must be running for curriculum"))
        return {"error": "web_ui_not_reachable"}

    progress = _load_progress()
    run_record = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "results": {},
    }

    total_pass = 0
    total_fail = 0

    for module in modules:
        mod_name = module.get("name", "Unknown")
        lessons = module.get("lessons", [])
        print(f"\n{BOLD}Module: {mod_name}{RESET}  ({len(lessons)} lessons)\n")

        for lesson in lessons:
            lid = lesson.get("id", "?")
            prompt = lesson["prompt"]
            expected = lesson.get("expected_tools_any", [])
            validation = lesson.get("validation", "tool_usage")
            pass_keywords = lesson.get("pass_keywords", [])
            timeout = lesson.get("timeout", 120)

            print(f"  Lesson: {lid}")
            print(f"    Prompt: \"{prompt[:80]}\"")

            before_ts = time.time()
            resp = http_post_chat(prompt, timeout=timeout)
            after_ts = time.time()

            if "error" in resp:
                print(_fail(f"    Error: {resp['error']}"))
                run_record["results"][lid] = {"status": "ERROR", "error": resp["error"]}
                total_fail += 1
                continue

            text = resp.get("text", "")
            tools = _find_tools_in_flight_log(before_ts, after_ts)
            elapsed = resp.get("elapsed_s", "?")

            # Validate
            tool_pass = True
            behavior_pass = True

            if validation in ("tool_usage", "tool_and_behavioral"):
                if expected:
                    tool_pass = any(t in expected for t in tools)
                else:
                    # Expect NO tools
                    tool_pass = len(tools) == 0

            if validation in ("behavioral", "tool_and_behavioral"):
                if pass_keywords:
                    text_lower = text.lower()
                    behavior_pass = any(kw.lower() in text_lower for kw in pass_keywords)
                behavior_pass = behavior_pass and len(text) > 20

            passed = tool_pass and behavior_pass
            status = "PASS" if passed else "FAIL"

            preview = (text[:100] + "...") if len(text) > 100 else text
            print(f"    Response ({elapsed}s): {preview}")
            print(f"    Tools: {tools or '(none)'}  Expected: {expected or 'none'}")
            if pass_keywords:
                print(f"    Keywords: {pass_keywords}  Found: {[kw for kw in pass_keywords if kw.lower() in text.lower()]}")

            color = GREEN if passed else RED
            print(f"    {color}{status}{RESET}\n")

            run_record["results"][lid] = {
                "status": status,
                "tools_used": tools,
                "elapsed_s": elapsed,
                "response_preview": preview,
            }

            if passed:
                total_pass += 1
            else:
                total_fail += 1

    total = total_pass + total_fail
    color = GREEN if total_fail == 0 else (YELLOW if total_pass > total_fail else RED)
    print(f"\n{BOLD}Curriculum Result: {color}{total_pass}/{total} passed{RESET}")

    # Save progress
    progress["runs"].append(run_record)
    # Keep last 20 runs
    progress["runs"] = progress["runs"][-20:]
    _save_progress(progress)

    return run_record


def show_progress() -> None:
    """Show curriculum progress history."""
    _header("Curriculum Progress")
    progress = _load_progress()
    runs = progress.get("runs", [])
    if not runs:
        print("  No curriculum runs yet.")
        return

    print(f"  {len(runs)} runs recorded\n")
    for run in runs[-10:]:
        ts = run.get("timestamp", "?")
        results = run.get("results", {})
        passed = sum(1 for r in results.values() if r.get("status") == "PASS")
        total = len(results)
        color = GREEN if passed == total else (YELLOW if passed > total // 2 else RED)
        print(f"  {ts}  {color}{passed}/{total}{RESET}")


# ---------------------------------------------------------------------------
# REPL
# ---------------------------------------------------------------------------
def run_repl() -> None:
    """Interactive diagnostic/chat mode."""
    _header("Doctor Claude REPL — SmolClaw")
    print("Commands: :health :tools :e2e :logs [n] :curriculum [mod] :progress :exit")
    print("Plain text sends to SmolClaw via HTTP\n")

    while True:
        try:
            line = input(f"{CYAN}doctor>{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break

        if not line:
            continue

        if line in (":exit", ":quit", ":q"):
            print("Bye.")
            break
        elif line == ":health":
            run_health()
        elif line == ":tools":
            list_tools()
        elif line == ":e2e":
            run_e2e_clinic()
        elif line.startswith(":logs"):
            parts = line.split()
            n = int(parts[1]) if len(parts) > 1 else 500
            analyze_logs(last_n=n)
        elif line.startswith(":curriculum"):
            parts = line.split()
            mod = parts[1] if len(parts) > 1 else None
            run_curriculum(module_filter=mod)
        elif line == ":progress":
            show_progress()
        elif line.startswith(":"):
            print(_warn(f"Unknown command: {line}"))
        else:
            # Send to SmolClaw
            print(f"  Sending to SmolClaw...", flush=True)
            t0 = time.time()
            resp = http_post_chat(line, timeout=300)
            if "error" in resp:
                print(_fail(f"Error: {resp['error']}"))
            else:
                print(f"\n{resp.get('text', '(empty)')}\n")
                print(f"  [{resp.get('elapsed_s', '?')}s, "
                      f"{resp.get('tokens_streamed', 0)} tokens streamed]")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    global WEB_UI_URL

    parser = argparse.ArgumentParser(
        description="Doctor Claude — Diagnostic & Teaching Harness for SmolClaw",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--health", action="store_true", help="Check NUC servers, web UI, files")
    parser.add_argument("--e2e-clinic", action="store_true", help="Send test queries and validate")
    parser.add_argument("--log-analysis", action="store_true", help="Parse flight_recorder.jsonl")
    parser.add_argument("--list-tools", action="store_true", help="Show SmolClaw's tools")
    parser.add_argument("--curriculum", nargs="?", const="all", default=None,
                        help="Run curriculum (optionally specify module name)")
    parser.add_argument("--repl", action="store_true", help="Interactive chat with SmolClaw")
    parser.add_argument("--progress", action="store_true", help="Show curriculum progress")
    parser.add_argument("--web-ui", default=None,
                        help="Web UI URL (default: http://127.0.0.1:8080)")

    args = parser.parse_args()

    if args.web_ui:
        WEB_UI_URL = args.web_ui

    # Default to --health if no args
    if not any([args.health, args.e2e_clinic, args.log_analysis,
                args.list_tools, args.curriculum, args.repl, args.progress]):
        args.health = True

    if args.health:
        run_health()
    if args.list_tools:
        list_tools()
    if args.e2e_clinic:
        run_e2e_clinic()
    if args.log_analysis:
        analyze_logs()
    if args.curriculum:
        mod = None if args.curriculum == "all" else args.curriculum
        run_curriculum(module_filter=mod)
    if args.progress:
        show_progress()
    if args.repl:
        run_repl()


if __name__ == "__main__":
    main()
