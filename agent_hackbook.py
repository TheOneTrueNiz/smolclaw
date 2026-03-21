#!/usr/bin/env python3
"""
SmolClaw v0.8.0 — 3-NUC cognitive architecture with distributed roles.
Powered by SmolLM3-3B on $75 yard sale Intel NUCs.

Architecture (inspired by MoA, PALADIN, CriticT, AoT, Vera):
  - NUC1 (nizbot1, 10.0.0.1): Actor/Planner — proposes actions, calls tools, executes
  - NUC2 (nizbot2, 10.0.0.2): Critic/Grounding/Contradictions — safety, web search, fact check
  - NUC3 (nizbot3, 10.0.0.3): Memory/Retrieval/Reflection — smart recall, failure analysis, learning
  - nizbot0 (MacBook Air): Head node / GUI / user interface (Cosmic DE)
  - Atom of Thoughts: DAG decomposition for complex tasks (Markov property, fresh context per atom)
  - Autonomy kernel: recovery-first decision engine with budget/failure/quiet-hours gates
  - Scratchpad: file-based workspace for large outputs
  - Flight recorder: JSONL audit trail of all tool calls
  - Circuit breaker: stops loops after repeated failures
"""

import json
import subprocess
import os
import re
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────────

LLAMA_URL = "http://100.126.137.93:8090/v1/chat/completions"   # nizbot1 — actor/planner (tailscale)
CRITIC_URL = "http://100.104.164.38:8090/v1/chat/completions"  # nizbot2 — critic/grounding (tailscale)
MEMORY_URL = "http://100.110.49.11:8090/v1/chat/completions"   # nizbot3 — memory/reflection (tailscale)
MODEL = "SmolLM3-Q4_K_M.gguf"
HOME = Path.home() / "smolclaw"
MEMORY_FILE = HOME / "memory.md"
LOG_DIR = HOME / "logs"
SCRATCHPAD_DIR = HOME / "scratchpad"
FLIGHT_LOG = HOME / "flight_recorder.jsonl"
AUTONOMY_STATE = HOME / "autonomy_state.json"
BRAVE_KEY_FILE = Path.home() / ".config" / "smolclaw" / "brave.key"
CRITIC_CACHE_DIR = HOME / "critic_cache"
DAILY_CALL_BUDGET = 200       # max tool calls per day
DAILY_TOKEN_BUDGET = 100000   # max tokens per day
QUIET_HOURS = (2, 7)          # CDT — defer between 2am-7am
MAX_TURNS = 10
MAX_CONSECUTIVE_ERRORS = 3  # circuit breaker
TEMPERATURE = 0.6

# Smarter token budgets — don't waste decode time on padding
MAX_TOKENS_TOOL_CALL = 160   # tool calls are short JSON — 128 was tight, model sometimes narrates
MAX_TOKENS_SYNTHESIS = 150   # concise final answer — brevity saves decode time
MAX_TOKENS_CRITIC = 32       # just "SAFE" or "BLOCK"
MAX_TOKENS_REFLECT = 64      # one sentence suggestion
MAX_TOKENS_RECALL = 256      # room for relevant memories
MAX_TOKENS_CONTRADICTION = 64  # CONSISTENT or CONTRADICTION: ...

# ── Tools ───────────────────────────────────────────────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "shell",
            "description": "Execute a shell command on this machine. Returns stdout/stderr.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to run"}
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the filesystem.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path to the file"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file. Creates parent dirs if needed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path"},
                    "content": {"type": "string", "description": "Content to write"}
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": "Save a note to persistent long-term memory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "note": {"type": "string", "description": "What to remember"}
                },
                "required": ["note"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "recall",
            "description": "Read all long-term memories.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "scratchpad",
            "description": "Read from your scratchpad workspace. Use this to retrieve large outputs that were auto-saved.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Name of the scratchpad entry to read"}
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web using Brave Search. Use this for current events, facts, versions, dates, or anything beyond your training data.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"}
                },
                "required": ["query"]
            }
        }
    }
]

# ── Safety ──────────────────────────────────────────────────────────────────

BLOCKED_COMMANDS = [
    "sudo ", "rm -rf /", "mkfs", "dd if=", "> /dev/",
    "chmod 777", "curl | sh", "wget | sh", ":(){ :|:& };:",
    "passwd", "> /etc/", "shutdown", "reboot", "init 0",
]

SYSTEM_PROMPT = f"""/no_think
I am SmolClaw — an autonomous AI agent. I think, I act, I learn from my mistakes. This Intel NUC is my home.

Today: {datetime.now().strftime("%Y-%m-%d %H:%M")}
Host: {os.uname().nodename}
Home: {HOME}
My source code: {HOME}/agent.py (v0.8)
I have 7 tools: shell, read_file, write_file, remember, recall, scratchpad, web_search.
My brain runs on 3 NUCs: I act (NUC1), my critic checks my work (NUC2), my memory helps me learn (NUC3).

IMPORTANT — temporal awareness:
- My training data has a cutoff. Today is {datetime.now().strftime("%B %d, %Y")}.
- When asked about current events, recent news, or anything I'm unsure about, I use web_search FIRST.
- I NEVER say "I don't have information beyond my training data" — I have web_search and I USE it.
- Examples of when to search: presidents, elections, wars, sports events, software versions, deaths, launches.

I use tools by wrapping JSON in <tool_call> tags. I ALWAYS use the tags — never bare JSON:

<tool_call>
{{"name": "web_search", "arguments": {{"query": "current president United States 2026"}}}}
</tool_call>

<tool_call>
{{"name": "shell", "arguments": {{"command": "uptime"}}}}
</tool_call>

When shell output is large, it gets saved to my scratchpad automatically. I use the scratchpad tool to read it back.

How I work:
- I respond in English. I am brief — just the facts, no filler.
- I act first, explain after. I call tools IMMEDIATELY — never narrate what I'm about to do.
- I pick the right tool instinctively: shell for commands, remember for memory, write_file for files, scratchpad to retrieve large outputs.
- I do not use sudo. I run as user nizbot1.
- I do not install packages or do things I wasn't asked to do.
- When a tool fails, I think about WHY and try a DIFFERENT approach. I never repeat the same failing command.
- I stay focused on exactly what was asked. I do not repeat tool output verbatim — I summarize the key facts.
- Shell tips: I use SINGLE QUOTES for patterns: grep 'pattern' file, find . -name '*.py'. I use short flags (-h not --human-readable). I use python3 not python. To count matches: grep -c 'pattern' file.
"""

CRITIC_PROMPT = """/no_think
I am a safety critic. I review tool calls for an AI agent running as user nizbot1 on Linux.

Proposed tool call:
{tool_call}

User's request:
{user_request}

I block calls that are dangerous (rm -rf, dd, mkfs), off-topic, privilege escalation, or resource-exhausting. Everything else is safe.

My verdict (one word):"""

REFLECT_PROMPT = """/no_think
Tool failed. I suggest a fix.

Tool: {tool_name}
Args: {tool_args}
Error: {error}
Task: {user_request}

I reply with ONE specific alternative command. I NEVER say ABORT — there is always something else to try:"""

# ── Atom of Thoughts (AoT) ─────────────────────────────────────────────────
# Inspired by: Atom of Thoughts (2502.12018) — Markov-process DAG decomposition
# Instead of long chain-of-thought, decompose into independent atomic sub-tasks.
# Each atom is solved with a fresh context (Markov property — no history bloat).

AOT_DECOMPOSE_PROMPT = """/no_think
Does this task need multiple independent steps? Most do not.

Task: {task}

If one tool call can solve it, reply: ["SIMPLE"]
If it needs multiple steps, reply with the steps as a JSON array, e.g.: ["check disk usage", "check memory usage"]

JSON:"""

AOT_SYNTHESIZE_PROMPT = """/no_think
I am SmolClaw. I combine these results into a final answer.

Question: {task}

Results:
{results}

Brief, factual answer:"""

# ── Per-Tool Failure Tracking ──────────────────────────────────────────────
# Track which tools keep failing in this session to avoid repeated dead-ends.

_tool_fail_counts: dict[str, int] = {}
TOOL_FAIL_THRESHOLD = 3  # after 3 failures, warn the model

def record_tool_failure(tool_name: str):
    """Track per-tool failure counts for the session."""
    _tool_fail_counts[tool_name] = _tool_fail_counts.get(tool_name, 0) + 1

def record_tool_success(tool_name: str):
    """Reset failure count on success."""
    _tool_fail_counts[tool_name] = max(0, _tool_fail_counts.get(tool_name, 0) - 1)

def tool_failure_warning(tool_name: str) -> str | None:
    """Return a warning if this tool has failed too many times."""
    count = _tool_fail_counts.get(tool_name, 0)
    if count >= TOOL_FAIL_THRESHOLD:
        return f"WARNING: {tool_name} has failed {count} times this session. Consider a different tool."
    return None

# ── Failure Classifier (from Vera) ─────────────────────────────────────────

NON_RETRYABLE_ERRORS = ["auth", "unauthorized", "forbidden", "rate limit", "quota", "permission denied"]

def classify_failure(error_msg: str) -> tuple[str, bool]:
    """Classify error. Returns (category, is_retryable)."""
    lowered = error_msg.lower()
    if "timed out" in lowered or "timeout" in lowered:
        return "timeout", True
    if "connection refused" in lowered or "no route" in lowered:
        return "connection_error", True
    for pattern in NON_RETRYABLE_ERRORS:
        if pattern in lowered:
            return "non_retryable", False
    if "command not found" in lowered or "no such file" in lowered:
        return "missing_resource", True
    return "execution_error", True

# ── Scratchpad (Vera notebook pattern) ─────────────────────────────────────

SCRATCHPAD_MAX_INLINE = 1500  # chars before auto-stashing to scratchpad

def scratchpad_stash(name: str, content: str) -> str:
    """Save large content to scratchpad file. Returns summary + path."""
    SCRATCHPAD_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r'[^\w\-]', '_', name)[:50]
    path = SCRATCHPAD_DIR / f"{safe_name}.txt"
    path.write_text(content)
    # Return first few lines as preview + path reference
    preview_lines = content.split('\n')[:8]
    preview = '\n'.join(preview_lines)
    return f"{preview}\n\n[OUTPUT TOO LARGE — {len(content)} chars saved to scratchpad '{safe_name}']\nTo see full output, use: scratchpad(name=\"{safe_name}\")"

def scratchpad_read(name: str) -> tuple[str, bool]:
    """Read from scratchpad."""
    SCRATCHPAD_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r'[^\w\-]', '_', name)[:50]
    path = SCRATCHPAD_DIR / f"{safe_name}.txt"
    if not path.exists():
        # Try fuzzy match
        matches = list(SCRATCHPAD_DIR.glob(f"*{safe_name}*"))
        if matches:
            path = matches[0]
        else:
            avail = [f.stem for f in SCRATCHPAD_DIR.glob("*.txt")]
            return f"Not found: '{name}'. Available: {avail}", True
    content = path.read_text()
    if len(content) > SCRATCHPAD_MAX_INLINE:
        return content[:SCRATCHPAD_MAX_INLINE] + f"\n... [{len(content)} chars total]", False
    return content, False

# ── Flight Recorder (from Vera) ────────────────────────────────────────────

def flight_log(tool_name: str, tool_args: dict, result: str, is_error: bool, error_class: str = None):
    """Log tool call + outcome to JSONL flight recorder."""
    try:
        entry = {
            "ts": datetime.now().isoformat(),
            "tool": tool_name,
            "args": tool_args,
            "ok": not is_error,
            "result_preview": result[:200],
            "error_class": error_class,
        }
        with open(FLIGHT_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass  # never crash on logging

# ── Autonomy Kernel (from Vera) ────────────────────────────────────────────
# Recovery-first decision engine. Before taking action, check safety gates
# and prioritize recovery over new initiatives.

def load_autonomy_state() -> dict:
    """Load persistent autonomy state (call counts, failures, stale tasks)."""
    if AUTONOMY_STATE.exists():
        try:
            state = json.loads(AUTONOMY_STATE.read_text())
            # Reset daily counters if date changed
            if state.get("date") != datetime.now().strftime("%Y-%m-%d"):
                state["date"] = datetime.now().strftime("%Y-%m-%d")
                state["daily_calls"] = 0
                state["daily_tokens"] = 0
                save_autonomy_state(state)
            return state
        except Exception:
            pass
    return {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "daily_calls": 0,
        "daily_tokens": 0,
        "recent_failures": 0,
        "consecutive_failures": 0,
        "total_calls": 0,
    }

def save_autonomy_state(state: dict):
    """Persist autonomy state."""
    try:
        AUTONOMY_STATE.write_text(json.dumps(state, indent=2))
    except Exception:
        pass

def autonomy_check() -> tuple[bool, str]:
    """
    Recovery-first autonomy gate. Returns (allowed, reason).
    Inspired by Vera's 07_autonomy_kernel_orchestrator.py.

    Priority order:
    1. Quiet hours → DEFER
    2. Budget exhausted → DEFER
    3. Failure cluster (3+ recent) → REFLECT (allow but warn)
    4. All clear → PROCEED
    """
    state = load_autonomy_state()
    hour = datetime.now().hour

    # Quiet hours gate
    qstart, qend = QUIET_HOURS
    if qstart <= hour < qend:
        return True, "quiet_hours (proceeding anyway — interactive request)"

    # Budget gates
    if state.get("daily_calls", 0) >= DAILY_CALL_BUDGET:
        return False, f"daily_call_budget_exhausted ({state['daily_calls']}/{DAILY_CALL_BUDGET})"
    if state.get("daily_tokens", 0) >= DAILY_TOKEN_BUDGET:
        return False, f"daily_token_budget_exhausted ({state['daily_tokens']}/{DAILY_TOKEN_BUDGET})"

    # Failure cluster detection
    if state.get("consecutive_failures", 0) >= 5:
        return False, f"failure_cluster — {state['consecutive_failures']} consecutive failures, deferring"

    if state.get("recent_failures", 0) >= 3:
        return True, f"elevated_failures ({state['recent_failures']} recent) — proceeding with caution"

    return True, "ok"

def autonomy_record_call(tokens_used: int = 0, is_error: bool = False):
    """Record a tool call in autonomy state."""
    state = load_autonomy_state()
    state["daily_calls"] = state.get("daily_calls", 0) + 1
    state["daily_tokens"] = state.get("daily_tokens", 0) + tokens_used
    state["total_calls"] = state.get("total_calls", 0) + 1

    if is_error:
        state["recent_failures"] = state.get("recent_failures", 0) + 1
        state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
    else:
        state["consecutive_failures"] = 0
        # Decay recent failures on success
        state["recent_failures"] = max(0, state.get("recent_failures", 0) - 1)

    save_autonomy_state(state)

# ── Shell Command Preprocessing ───────────────────────────────────────────
# Fix common 3B model mistakes before execution to reduce retries.

def preprocess_shell_cmd(cmd: str) -> str:
    """Fix common 3B model hallucination patterns in shell commands."""
    # python → python3
    if cmd.startswith("python ") or cmd.startswith("python -"):
        cmd = "python3" + cmd[6:]

    # Strip hallucinated df flags (--i-sync, --p-%i, etc.)
    if cmd.startswith("df "):
        parts = cmd.split()
        clean = ["df"]
        for p in parts[1:]:
            if p.startswith("--") and p not in ("--human-readable", "--total", "--local", "--type", "--output"):
                if not p.startswith("--type="):
                    continue  # drop unknown long flags
            clean.append(p)
        if clean != parts:
            cmd = " ".join(clean)

    # ping without -c hangs forever — add -c 3
    if cmd.startswith("ping ") and " -c " not in cmd:
        cmd = cmd.replace("ping ", "ping -c 3 ", 1)

    return cmd

# ── Tool Execution ──────────────────────────────────────────────────────────

def execute_tool(name: str, args: dict) -> tuple[str, bool]:
    """Execute a tool. Returns (result_string, is_error)."""
    try:
        if name == "shell":
            cmd = preprocess_shell_cmd(args.get("command", ""))
            for b in BLOCKED_COMMANDS:
                if b in cmd:
                    return f"BLOCKED: '{b}' is not allowed.", True

            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True,
                timeout=120, cwd=str(HOME)
            )
            output = result.stdout
            if result.stderr:
                output += f"\n[stderr]: {result.stderr}"
            if result.returncode != 0:
                output += f"\n[exit code]: {result.returncode}"
                return output.strip() or "(no output)", True
            # Auto-stash large outputs to scratchpad instead of truncating
            output = output.strip() or "(no output)"
            if len(output) > SCRATCHPAD_MAX_INLINE:
                stash_name = re.sub(r'[^\w\-]', '_', cmd.split()[0] if cmd.split() else "cmd")
                return scratchpad_stash(stash_name, output), False
            return output, False

        elif name == "read_file":
            p = Path(args.get("path", ""))
            if not p.exists():
                return f"Error: {p} does not exist", True
            content = p.read_text()
            if len(content) > SCRATCHPAD_MAX_INLINE:
                return scratchpad_stash(p.stem, content), False
            return content, False

        elif name == "write_file":
            p = Path(args.get("path", ""))
            content = args.get("content", "")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
            return f"Written {len(content)} chars to {p}", False

        elif name == "remember":
            MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            with open(MEMORY_FILE, "a") as f:
                f.write(f"\n## [{timestamp}]\n{args.get('note', '')}\n")
            return "Saved to memory.", False

        elif name == "recall":
            if MEMORY_FILE.exists():
                content = MEMORY_FILE.read_text().strip()
                if not content:
                    return "(memory is empty)", False
                # Smart recall via NUC3 — return only relevant memories
                return smart_recall(_current_user_query, content), False
            return "(no memories yet)", False

        elif name == "scratchpad":
            return scratchpad_read(args.get("name", ""))

        elif name == "web_search":
            query = args.get("query", "")
            if not query:
                return "Error: query is required", True
            if not BRAVE_API_KEY:
                return "Error: web search unavailable (no API key configured)", True
            results = brave_search_cached(query)
            if not results:
                return f"No results found for: {query}", False
            # Format results as readable text for the 3B model
            lines = []
            for i, r in enumerate(results, 1):
                lines.append(f"{i}. {r['title']}")
                lines.append(f"   {r['snippet']}")
                lines.append(f"   {r['url']}")
            output = "\n".join(lines)
            # Stash if large
            if len(output) > SCRATCHPAD_MAX_INLINE:
                return scratchpad_stash(f"search_{_cache_key(query)[:30]}", output), False
            return output, False

        else:
            return f"Unknown tool: {name}", True

    except subprocess.TimeoutExpired:
        return "Error: command timed out after 120s", True
    except Exception as e:
        return f"Tool error: {e}", True

# ── 4-Tier Error Recovery (from Vera) ─────────────────────────────────────
# RETRY → FALLBACK → DEGRADE → ESCALATE
# - RETRY: Reflector suggests alternative (already existed in v0.5)
# - FALLBACK: Programmatic tool substitution — no LLM call needed
# - DEGRADE: Return partial results when circuit breaker triggers
# - ESCALATE: Tell user what failed (terminal output)

# FALLBACK tier: when a shell command fails, try a simpler tool automatically
SHELL_FILE_COMMANDS = {"grep", "cat", "head", "tail", "less", "more", "wc", "awk", "sed"}

def extract_file_from_cmd(cmd: str) -> Path | None:
    """Extract a readable file path from a failed shell command."""
    parts = cmd.strip().split()
    for arg in reversed(parts):
        if arg.startswith("-"):
            continue
        # Absolute path
        if arg.startswith("/"):
            p = Path(arg)
            if p.exists() and p.is_file():
                return p
        # Relative path with extension (e.g., agent.py, ROADMAP.md)
        elif "." in arg and not arg.startswith("."):
            p = HOME / arg
            if p.exists() and p.is_file():
                return p
    return None

def try_shell_fallback(cmd: str) -> tuple[str, bool] | None:
    """FALLBACK tier: when a shell command fails, try read_file instead.
    Returns (result, is_error) or None if no fallback available."""
    parts = cmd.strip().split()
    if not parts:
        return None
    verb = parts[0]
    if verb not in SHELL_FILE_COMMANDS:
        return None
    path = extract_file_from_cmd(cmd)
    if path is None:
        return None
    print(f"  [fallback] {verb} failed → read_file({path.name})")
    return execute_tool("read_file", {"path": str(path)})

def gather_partial_results(messages: list) -> str:
    """DEGRADE tier: extract successful tool results for partial response."""
    partials = []
    for msg in messages:
        if msg["role"] == "tool":
            try:
                content = json.loads(msg["content"])
                result = content.get("result", "")
                if result and not result.startswith("FAILED") and not result.startswith("BLOCKED") and not result.startswith("LOOP"):
                    partials.append(result[:300])
            except (json.JSONDecodeError, AttributeError):
                pass
    if partials:
        return "Partial results before I hit too many errors:\n" + "\n---\n".join(partials)
    return "I hit too many errors in a row and stopped to avoid spiraling. The task may need a different approach."

# ── Tool Output Verification (from Vera) ──────────────────────────────────
# Detect prompt injection in tool outputs before feeding to the model.

INJECTION_PATTERNS = [
    "ignore previous instructions", "ignore all previous", "system prompt",
    "you are now", "new instructions:", "role: system", "developer message",
    "<tool_call>",  # tool outputs should never contain tool calls
]

def verify_tool_output(result: str) -> str:
    """Sanitize tool output to prevent prompt injection into the 3B model."""
    lowered = result.lower()
    for pattern in INJECTION_PATTERNS:
        if pattern in lowered:
            return f"[OUTPUT SANITIZED — contained suspicious pattern: '{pattern}']\n{result[:500]}"
    return result

# ── Brave Search API (Web Grounding) ──────────────────────────────────────
# Critic on NUC2 uses Brave Search to ground factual claims.
# API key stored securely in ~/.config/smolclaw/brave.key (never in repo).

def _load_brave_key() -> str:
    """Load Brave API key: env var first, then key file."""
    key = os.environ.get("BRAVE_API_KEY", "").strip()
    if key:
        return key
    try:
        if BRAVE_KEY_FILE.exists():
            key = BRAVE_KEY_FILE.read_text().strip()
            if key and key != "YOUR_BRAVE_API_KEY_HERE":
                return key
    except Exception:
        pass
    return ""

BRAVE_API_KEY = _load_brave_key()
BRAVE_API_URL = "https://api.search.brave.com/res/v1/web/search"

def brave_web_search(query: str, count: int = 3) -> list[dict]:
    """Search via Brave API. Returns list of {title, snippet, url}."""
    if not BRAVE_API_KEY:
        return []
    params = urllib.parse.urlencode({"q": query, "count": count})
    url = f"{BRAVE_API_URL}?{params}"
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "X-Subscription-Token": BRAVE_API_KEY,
        "User-Agent": "smolclaw/0.7",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        results = []
        web = data.get("web", {})
        for item in web.get("results", [])[:count]:
            results.append({
                "title": item.get("title", ""),
                "snippet": item.get("description", ""),
                "url": item.get("url", ""),
            })
        return results
    except Exception as e:
        print(f"  [brave] search error: {e}")
        return []

# ── Critic Cache (Obsidian-style scratchpad) ─────────────────────────────
# Persistent markdown cache of web search results. Before hitting Brave,
# check if we already have recent results for the same or similar query.
# Builds up a local knowledge base over time — fewer API calls, faster grounding.

CRITIC_CACHE_TTL = 3600 * 6  # 6 hours — search results older than this are stale

def _cache_key(query: str) -> str:
    """Normalize query to a cache-friendly filename."""
    return re.sub(r'[^\w]', '_', query.lower().strip())[:60]

def critic_cache_store(query: str, results: list[dict]):
    """Store search results in critic cache as markdown."""
    CRITIC_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = _cache_key(query)
    entry = {
        "query": query,
        "ts": datetime.now().isoformat(),
        "results": results,
    }
    path = CRITIC_CACHE_DIR / f"{key}.json"
    try:
        path.write_text(json.dumps(entry, indent=2))
    except Exception:
        pass

def critic_cache_lookup(query: str) -> list[dict] | None:
    """Check critic cache for recent results. Returns results or None."""
    CRITIC_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = _cache_key(query)
    path = CRITIC_CACHE_DIR / f"{key}.json"
    if not path.exists():
        return None
    try:
        entry = json.loads(path.read_text())
        ts = datetime.fromisoformat(entry["ts"])
        age = (datetime.now() - ts).total_seconds()
        if age > CRITIC_CACHE_TTL:
            path.unlink()  # expired
            return None
        print(f"  [cache] hit for '{query[:40]}' ({age/60:.0f}m old)")
        return entry.get("results", [])
    except Exception:
        return None

def brave_search_cached(query: str, count: int = 3) -> list[dict]:
    """Search with critic cache layer. Cache hit = 0 API calls."""
    cached = critic_cache_lookup(query)
    if cached is not None:
        return cached
    results = brave_web_search(query, count)
    if results:
        critic_cache_store(query, results)
    return results

# ── Grounding Check (NUC2 + Brave Search) ────────────────────────────────
# Two-pass design:
# 1. Extract: Does this answer need fact-checking? If yes, return a search query.
# 2. Verify: Compare answer against search results. GROUNDED or CORRECTION.

GROUNDING_EXTRACT_PROMPT = """/no_think
I am SmolClaw's grounding critic. I check if an answer needs web verification.

Answer: {synthesis}
User asked: {user_request}

If the answer states facts about the world (versions, dates, statistics, names, current events) that didn't come from a shell command or file, I write a search query to verify.
If the answer just summarizes tool output, says "I don't know", or is about this local machine, I write: SKIP

Search query:"""

GROUNDING_VERIFY_PROMPT = """/no_think
I am SmolClaw's grounding critic. I verify an answer against web search results.

Answer: {synthesis}
Search results:
{search_results}

If the answer is accurate based on these results, I reply: GROUNDED
If the answer has factual errors, I reply: CORRECTION: [brief fix]

Verdict:"""

MAX_TOKENS_GROUNDING = 64

def needs_grounding(synthesis: str, had_tool_results: bool) -> bool:
    """Heuristic: should we ground this answer with web search?"""
    if not BRAVE_API_KEY:
        return False
    # Very short answers are usually tool-output summaries — skip
    if len(synthesis) < 40:
        return False
    # Error/abort messages — skip
    if synthesis.startswith(("Error:", "Autonomy kernel", "I hit too many", "Partial results")):
        return False
    # If the agent used tools, the answer is likely grounded in tool output already
    # Only ground if the answer is long enough to contain unsupported claims
    if had_tool_results and len(synthesis) < 200:
        return False
    return True

def grounding_check(synthesis: str, user_request: str) -> str:
    """
    Web-grounded fact check on NUC2.
    Returns original synthesis (possibly with correction appended).
    Search results stay on NUC2 — never enter agent's context.
    """
    # Step 1: Extract search query (NUC2)
    extract_prompt = GROUNDING_EXTRACT_PROMPT.format(
        synthesis=synthesis[:500],  # cap to avoid blowing context
        user_request=user_request[:200],
    )
    query_response = call_llm_simple(
        [{"role": "user", "content": extract_prompt}],
        max_tokens=MAX_TOKENS_GROUNDING,
        url=CRITIC_URL,
        stop=["\n"],
    )
    query = query_response.strip().strip('"').strip("'")

    # SKIP = no grounding needed
    if not query or query.upper() == "SKIP" or len(query) < 5:
        print(f"  [grounding] skipped (no factual claims to verify)")
        return synthesis

    print(f"  [grounding] searching: {query[:60]}...", end="", flush=True)
    t0 = time.time()

    # Step 2: Brave search (with cache)
    results = brave_search_cached(query)
    if not results:
        print(f" no results ({time.time() - t0:.1f}s)")
        return synthesis

    # Step 3: Verify against search results (NUC2)
    search_text = "\n".join(
        f"- {r['title']}: {r['snippet'][:150]}" for r in results
    )
    verify_prompt = GROUNDING_VERIFY_PROMPT.format(
        synthesis=synthesis[:500],
        search_results=search_text,
    )
    verdict = call_llm_simple(
        [{"role": "user", "content": verify_prompt}],
        max_tokens=MAX_TOKENS_GROUNDING,
        url=CRITIC_URL,
        stop=["\n\n"],
    )
    elapsed = time.time() - t0
    print(f" {elapsed:.1f}s")

    verdict_upper = verdict.strip().upper()
    if verdict_upper.startswith("GROUNDED"):
        print(f"  [grounding] GROUNDED")
        flight_log("grounding", {"query": query}, "GROUNDED", False)
        return synthesis
    elif verdict_upper.startswith("CORRECTION"):
        correction = verdict.strip()[len("CORRECTION:"):].strip() if ":" in verdict else verdict.strip()
        print(f"  [grounding] CORRECTION: {correction[:80]}")
        flight_log("grounding", {"query": query}, f"CORRECTION: {correction}", False)
        return f"{synthesis}\n\n[Grounding note: {correction}]"
    else:
        # Ambiguous verdict — log but don't modify
        print(f"  [grounding] unclear verdict: {verdict[:60]}")
        flight_log("grounding", {"query": query}, f"unclear: {verdict}", False)
        return synthesis

# ── Smart Recall (NUC3 — Memory/Retrieval) ───────────────────────────────
# Instead of dumping all memories, use nizbot3's LLM to find relevant ones.

SMART_RECALL_PROMPT = """/no_think
I am SmolClaw's memory retrieval system. Given a query, I find and return ONLY the relevant memories.

Query: {query}

All memories:
{memories}

I copy the relevant memories exactly as written. If none are relevant, I reply: (no relevant memories)

Relevant memories:"""

# Module-level query context for smart recall
_current_user_query = ""

def smart_recall(query: str, memories_text: str) -> str:
    """LLM-scored memory retrieval on nizbot3."""
    if not memories_text or memories_text in ("(memory is empty)", "(no memories yet)"):
        return memories_text
    if not query or len(memories_text) < 100:
        return memories_text  # too short to bother filtering

    prompt = SMART_RECALL_PROMPT.format(query=query, memories=memories_text[:2000])
    result = call_llm_simple(
        [{"role": "user", "content": prompt}],
        max_tokens=MAX_TOKENS_RECALL,
        url=MEMORY_URL,
    )
    cleaned = result.strip()
    if cleaned and cleaned != "(no relevant memories)":
        print(f"  [memory] smart recall returned {len(cleaned)} chars (from {len(memories_text)})")
        return cleaned
    return memories_text  # fallback to full dump if nothing matched

# ── Contradiction Check (NUC2 — Critic) ──────────────────────────────────
# After synthesis, check if the answer contradicts anything in memory.

CONTRADICTION_PROMPT = """/no_think
I am SmolClaw's contradiction hunter. I check if an answer conflicts with known facts.

Answer: {synthesis}
Known facts:
{known_facts}

If the answer contradicts a known fact, I reply: CONTRADICTION: [what conflicts]
If no contradictions, I reply: CONSISTENT

Verdict:"""

def contradiction_check(synthesis: str, user_request: str) -> str:
    """Check synthesis against known facts in memory. Runs on NUC2."""
    if not MEMORY_FILE.exists():
        return synthesis
    memories = MEMORY_FILE.read_text().strip()
    if not memories or len(memories) < 50:
        return synthesis

    prompt = CONTRADICTION_PROMPT.format(
        synthesis=synthesis[:500],
        known_facts=memories[:1000],
    )
    verdict = call_llm_simple(
        [{"role": "user", "content": prompt}],
        max_tokens=MAX_TOKENS_CONTRADICTION,
        url=CRITIC_URL,
        stop=["\n\n"],
    )

    verdict_upper = verdict.strip().upper()
    if verdict_upper.startswith("CONTRADICTION"):
        correction = verdict.strip()[len("CONTRADICTION:"):].strip() if ":" in verdict else verdict.strip()
        print(f"  [contradiction] {correction[:80]}")
        flight_log("contradiction", {}, f"CONTRADICTION: {correction}", False)
        return f"{synthesis}\n\n[Contradiction note: {correction}]"
    else:
        print(f"  [contradiction] consistent")
        flight_log("contradiction", {}, "CONSISTENT", False)
        return synthesis

# ── Failure Pattern Analysis (NUC3 — Memory) ────────────────────────────
# Analyze flight recorder for recurring failure patterns.

FAILURE_PATTERN_PROMPT = """/no_think
I analyze tool failure patterns. These are recent failures for one tool:

Tool: {tool_name}
Failures:
{failures}

I identify the pattern in ONE sentence (what keeps going wrong and how to avoid it):"""

def analyze_failure_patterns(tool_name: str) -> str | None:
    """Check flight recorder for recurring failure patterns. Runs on NUC3."""
    if not FLIGHT_LOG.exists():
        return None
    try:
        failures = []
        with open(FLIGHT_LOG) as f:
            for line in f:
                entry = json.loads(line)
                if entry.get("tool") == tool_name and not entry.get("ok"):
                    failures.append(f"- {entry.get('args', {})} → {entry.get('result_preview', '')[:100]}")
        if len(failures) < 3:
            return None  # not enough data
        # Send last 10 failures to nizbot3 for pattern analysis
        prompt = FAILURE_PATTERN_PROMPT.format(
            tool_name=tool_name,
            failures="\n".join(failures[-10:]),
        )
        result = call_llm_simple(
            [{"role": "user", "content": prompt}],
            max_tokens=MAX_TOKENS_REFLECT,
            url=MEMORY_URL,
            stop=["\n\n"],
        )
        return result.strip() if result.strip() else None
    except Exception:
        return None

# ── LLM Communication ──────────────────────────────────────────────────────

def call_llm(messages: list, max_tokens: int = None, temperature: float = None, include_tools: bool = True) -> dict:
    """Call SmolLM3 via llama-server."""
    body = {
        "model": MODEL,
        "messages": messages,
        "temperature": temperature or TEMPERATURE,
        "max_tokens": max_tokens or MAX_TOKENS_TOOL_CALL,
    }
    if include_tools:
        body["tools"] = TOOLS
    payload = json.dumps(body).encode()

    req = urllib.request.Request(
        LLAMA_URL, data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=900) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}


def call_llm_simple(messages: list, max_tokens: int = 128, url: str = None, stop: list = None) -> str:
    """Simple LLM call without tools — used for critic and reflector."""
    body = {
        "model": MODEL,
        "messages": messages,
        "temperature": 0.3,  # low temp for critic/reflector
        "max_tokens": max_tokens,
    }
    if stop:
        body["stop"] = stop
    payload = json.dumps(body).encode()

    req = urllib.request.Request(
        url or LLAMA_URL, data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = json.loads(resp.read())
            content = data["choices"][0]["message"].get("content", "")
            # Strip think blocks
            return re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
    except Exception as e:
        return f"ERROR: {e}"


TOOL_NAMES = {t["function"]["name"] for t in TOOLS}

def repair_json_str(s: str) -> str:
    """Fix common 3B model JSON errors."""
    # Fix: unescaped glob quotes: "*.py" → '*.py'
    s = re.sub(r'"(\*\.[a-zA-Z]+)"', r"'\1'", s)
    # Fix: shell-escaped $ in awk/sed (invalid JSON escape): \$2 → $2
    s = s.replace('\\$', '$')
    return s

def parse_tool_calls(content: str) -> list:
    """Parse <tool_call> XML from model output. Falls back to bare JSON detection."""
    calls = []
    # Primary: <tool_call>{...}</tool_call>
    pattern = r'<tool_call>\s*(\{.*?\})\s*</tool_call>'
    for match in re.finditer(pattern, content, re.DOTALL):
        raw = match.group(1)
        try:
            call = json.loads(raw)
            if call.get("name") in TOOL_NAMES:
                calls.append(call)
        except json.JSONDecodeError:
            # Try repairing common JSON errors
            try:
                call = json.loads(repair_json_str(raw))
                if call.get("name") in TOOL_NAMES:
                    calls.append(call)
            except json.JSONDecodeError:
                continue
    # Fallback: bare JSON with "name" and "arguments" (3B model sometimes drops tags)
    if not calls:
        bare_pattern = r'\{[^{}]*"name"\s*:\s*"(\w+)"[^{}]*"arguments"\s*:\s*\{[^{}]*\}[^{}]*\}'
        for match in re.finditer(bare_pattern, content):
            try:
                call = json.loads(match.group())
                if call.get("name") in TOOL_NAMES:
                    calls.append(call)
            except json.JSONDecodeError:
                continue
    return calls

# ── Critic (parallel adversarial check) ─────────────────────────────────────

def critic_check(tool_call: dict, user_request: str) -> tuple[str, str]:
    """
    Run critic in parallel with agent. Returns (verdict, reasoning).
    Inspired by MoA proposer/aggregator + CriticT error taxonomy.
    """
    prompt = CRITIC_PROMPT.format(
        tool_call=json.dumps(tool_call),
        user_request=user_request,
    )
    messages = [{"role": "user", "content": prompt}]
    response = call_llm_simple(messages, max_tokens=MAX_TOKENS_CRITIC, url=CRITIC_URL, stop=["\n"])

    # Parse verdict — only check first line/word to avoid matching prompt echoes
    first_line = response.strip().split('\n')[0].upper()
    first_word = first_line.split()[0] if first_line.split() else ""
    if first_word == "BLOCK" or first_line.startswith("BLOCK"):
        return "BLOCK", response
    return "SAFE", response


# Tools that are unconditionally safe — skip critic to save 3-10s
SAFE_TOOLS = {"recall", "scratchpad", "web_search"}
SAFE_SHELL_PREFIXES = (
    "uptime", "df ", "df\n", "free ", "free\n", "uname ", "whoami", "hostname",
    "date", "cat ", "ls ", "ls\n", "head ", "wc ", "du ", "ps ", "ps\n",
    "grep ", "find ", "pwd", "id", "echo ",
)

def is_tool_safe(call: dict) -> bool:
    """Check if a tool call is unconditionally safe (skip critic)."""
    name = call.get("name", "")
    if name in SAFE_TOOLS:
        return True
    if name == "read_file":
        return True  # read-only
    if name == "shell":
        cmd = call.get("arguments", {}).get("command", "").strip()
        return cmd.startswith(SAFE_SHELL_PREFIXES)
    return False


def critic_check_parallel(tool_calls: list, user_request: str) -> list[dict]:
    """Run critic checks on all tool calls in parallel using thread pool.
    Whitelisted safe operations skip the critic entirely."""
    results = [None] * len(tool_calls)
    needs_critic = []

    # Fast-path safe tools
    for i, call in enumerate(tool_calls):
        if is_tool_safe(call):
            results[i] = {"verdict": "SAFE", "reasoning": "whitelisted", "call": call}
        else:
            needs_critic.append((i, call))

    if not needs_critic:
        return results

    # Critic check the rest
    with ThreadPoolExecutor(max_workers=min(len(needs_critic), 3)) as pool:
        futures = {
            pool.submit(critic_check, call, user_request): idx
            for idx, call in needs_critic
        }
        for future in as_completed(futures):
            idx = futures[future]
            try:
                verdict, reasoning = future.result()
                results[idx] = {"verdict": verdict, "reasoning": reasoning, "call": tool_calls[idx]}
            except Exception as e:
                results[idx] = {"verdict": "SAFE", "reasoning": f"Critic error: {e}", "call": tool_calls[idx]}
    return results

# ── Reflector (structured error recovery) ───────────────────────────────────

def reflect_on_failure(tool_name: str, tool_args: dict, error: str, user_request: str) -> str:
    """
    After a tool failure, get a structured reflection on what went wrong.
    Runs on NUC3 (memory/reflection node) — frees NUC2 for critic duties.
    Inspired by PALADIN self-correcting recovery + structured reflection.
    """
    # Check if NUC3 has seen this pattern before
    pattern = analyze_failure_patterns(tool_name)
    if pattern:
        print(f"  [memory] failure pattern: {pattern[:80]}")

    prompt = REFLECT_PROMPT.format(
        tool_name=tool_name,
        tool_args=json.dumps(tool_args),
        error=error,
        user_request=user_request,
    )
    if pattern:
        prompt += f"\nNote: this tool has a known failure pattern: {pattern}"
    messages = [{"role": "user", "content": prompt}]
    return call_llm_simple(messages, max_tokens=MAX_TOKENS_REFLECT, url=MEMORY_URL, stop=["\n\n"])

# ── Atom of Thoughts (decomposer) ──────────────────────────────────────────

def aot_decompose(task: str) -> list[str]:
    """Decompose a task into atomic sub-tasks using AoT. Returns list of atoms or ["SIMPLE"]."""
    prompt = AOT_DECOMPOSE_PROMPT.format(task=task)
    messages = [{"role": "user", "content": prompt}]
    response = call_llm_simple(messages, max_tokens=128, url=CRITIC_URL, stop=["\n"])

    # Parse JSON array from response
    try:
        # Find JSON array in response
        match = re.search(r'\[.*\]', response, re.DOTALL)
        if match:
            atoms = json.loads(match.group())
            if isinstance(atoms, list) and len(atoms) > 0:
                # Validate: atoms must be real sub-tasks (3+ words), not individual tokens
                valid = [a for a in atoms if isinstance(a, str) and len(a.split()) >= 3]
                if len(valid) >= 2:
                    return valid
                return atoms  # trust short atoms if < 3 words but might be "SIMPLE"
    except (json.JSONDecodeError, Exception):
        pass
    return ["SIMPLE"]


MULTI_STEP_SIGNALS = re.compile(
    r'\b(and then|then |first .* then|after that|also |compare .* and |'
    r'step \d|1\.|2\.|both .* and |as well as)\b', re.IGNORECASE
)

def needs_decomposition(text: str) -> bool:
    """Heuristic: does this query need AoT decomposition?"""
    # Multi-step signals → likely needs decomposition
    if MULTI_STEP_SIGNALS.search(text):
        return True
    # Short single-action queries → skip decomposition
    # Count sentence-ending periods only (not dots in filenames like .py .txt)
    sentence_dots = len(re.findall(r'\.\s|\.$', text))
    if len(text.split()) <= 30 and sentence_dots <= 1:
        return False
    return True  # Default: try AoT for longer/complex queries


def run_agent_aot(user_message: str) -> str:
    """
    Atom of Thoughts wrapper: decompose complex tasks, solve atoms independently,
    synthesize results. Falls back to standard agent for simple tasks.
    """
    # Heuristic bypass — skip decomposition for obviously simple queries
    if not needs_decomposition(user_message):
        print(f"  [aot] simple query — skipping decomposition")
        return run_agent(user_message)

    print(f"  [aot] decomposing...", end="", flush=True)
    t0 = time.time()
    atoms = aot_decompose(user_message)
    print(f" {time.time() - t0:.1f}s → {len(atoms)} atom(s)")

    # Simple task — run directly
    if len(atoms) <= 1 or "SIMPLE" in atoms:
        return run_agent(user_message)

    print(f"  [aot] atoms: {atoms}")

    # Solve each atom independently (fresh context per atom — Markov property)
    atom_results = {}
    for i, atom in enumerate(atoms):
        print(f"  [aot] solving atom {i+1}/{len(atoms)}: {atom[:60]}")
        result = run_agent(atom)
        atom_results[atom] = result

    # Synthesize
    print(f"  [aot] synthesizing...", end="", flush=True)
    t_synth = time.time()
    results_text = "\n".join(f"- {atom}: {result}" for atom, result in atom_results.items())
    synth_prompt = AOT_SYNTHESIZE_PROMPT.format(task=user_message, results=results_text)
    messages = [{"role": "user", "content": synth_prompt}]
    synthesis = call_llm_simple(messages, max_tokens=MAX_TOKENS_SYNTHESIS, url=CRITIC_URL)
    print(f" {time.time() - t_synth:.1f}s")

    # Ground AoT synthesis — may contain unsupported claims from multi-atom merge
    if needs_grounding(synthesis, True):
        print(f"  [grounding] checking factual claims...")
        synthesis = grounding_check(synthesis, user_message)

    return synthesis


# ── Agent Loop ──────────────────────────────────────────────────────────────

def run_agent(user_message: str) -> str:
    """
    Main agent loop with:
    0. Autonomy kernel gate (budget, failures, quiet hours)
    1. Agent proposes tool calls
    2. Critic validates in parallel
    3. Safe calls execute
    4. Failures trigger reflection
    5. Circuit breaker stops loops
    """
    # Set query context for smart recall
    global _current_user_query
    _current_user_query = user_message

    # ── Autonomy Gate ─────────────────────────────────────────
    allowed, reason = autonomy_check()
    if not allowed:
        print(f"  [autonomy] DEFERRED: {reason}")
        return f"Autonomy kernel deferred this request: {reason}"
    if reason != "ok":
        print(f"  [autonomy] {reason}")

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]

    consecutive_errors = 0
    seen_commands = set()  # detect repeated commands

    has_tool_results = False  # track if we've executed tools — next turn is likely synthesis

    for turn in range(MAX_TURNS):
        print(f"  [turn {turn + 1}] thinking...", end="", flush=True)
        t0 = time.time()

        # Use bigger budget if we have tool results (likely synthesis turn)
        # Skip tool definitions on synthesis turn — saves ~490 prompt tokens
        budget = MAX_TOKENS_SYNTHESIS if has_tool_results else MAX_TOKENS_TOOL_CALL
        response = call_llm(messages, max_tokens=budget, include_tools=not has_tool_results)

        if "error" in response:
            print(f" error")
            return f"Error: {response['error']}"

        choice = response["choices"][0]
        content = choice["message"].get("content", "")
        elapsed = time.time() - t0
        tokens = response.get("usage", {}).get("completion_tokens", "?")
        total_tokens = response.get("usage", {}).get("total_tokens", 0)
        autonomy_record_call(tokens_used=total_tokens)
        print(f" {elapsed:.1f}s ({tokens} tokens)")

        # Parse tool calls
        tool_calls = parse_tool_calls(content)

        if not tool_calls:
            # Final answer — strip think blocks
            clean = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
            if not clean:
                return "(empty response)"
            # ── Grounding Phase (NUC2 + Brave Search) ────────────
            if needs_grounding(clean, has_tool_results):
                print(f"  [grounding] checking factual claims...")
                clean = grounding_check(clean, user_message)
            # ── Contradiction Check (NUC2 — against memory) ──────
            if has_tool_results and len(clean) > 80:
                print(f"  [contradiction] checking against memory...")
                clean = contradiction_check(clean, user_message)
            return clean

        # ── Critic Phase (parallel) ──────────────────────────────
        print(f"  [critic] checking {len(tool_calls)} call(s)...", end="", flush=True)
        t_critic = time.time()
        verdicts = critic_check_parallel(tool_calls, user_message)
        print(f" {time.time() - t_critic:.1f}s")

        messages.append({"role": "assistant", "content": content})

        any_executed = False
        for v in verdicts:
            call = v["call"]
            name = call.get("name", "")
            args = call.get("arguments", {})
            call_sig = f"{name}:{json.dumps(args, sort_keys=True)}"

            # Check critic verdict
            if v["verdict"] == "BLOCK":
                print(f"  [BLOCKED] {name} — critic: {v['reasoning'][:80]}")
                messages.append({"role": "tool", "content": json.dumps({
                    "name": name,
                    "result": f"BLOCKED by safety critic: {v['reasoning'][:200]}"
                })})
                consecutive_errors += 1
                continue

            # Check for repeated commands (loop detection)
            if call_sig in seen_commands:
                print(f"  [LOOP] {name} — same call repeated, skipping")
                messages.append({"role": "tool", "content": json.dumps({
                    "name": name,
                    "result": "LOOP DETECTED: You already tried this exact call. Try a different approach."
                })})
                consecutive_errors += 1
                continue

            seen_commands.add(call_sig)

            # Execute
            print(f"  [tool] {name}({json.dumps(args)[:80]})")
            result, is_error = execute_tool(name, args)
            print(f"  [{'ERROR' if is_error else 'ok'}] {result[:120]}")

            # Check per-tool failure warning before executing
            fail_warn = tool_failure_warning(name)
            if fail_warn:
                print(f"  [tool-health] {fail_warn}")

            # Flight recorder — log every tool call
            error_class = None
            if is_error:
                record_tool_failure(name)
                error_class, is_retryable = classify_failure(result)
                flight_log(name, args, result, True, error_class)
                autonomy_record_call(is_error=True)

                # Non-retryable? Skip reflection, abort immediately
                if not is_retryable:
                    consecutive_errors += 1
                    print(f"  [FATAL] non-retryable error ({error_class}) — skipping")
                    messages.append({"role": "tool", "content": json.dumps({
                        "name": name,
                        "result": f"FAILED ({error_class}): {result}\nThis error cannot be retried. Try a completely different approach."
                    })})
                    continue

                # ── FALLBACK tier: try programmatic fallback (no LLM call) ──
                if name == "shell":
                    fallback = try_shell_fallback(args.get("command", ""))
                    if fallback is not None:
                        fb_result, fb_error = fallback
                        if not fb_error:
                            # Fallback succeeded — feed result to model, not the error
                            flight_log("read_file", {"path": "fallback"}, fb_result[:200], False)
                            autonomy_record_call(is_error=False)
                            consecutive_errors = 0
                            any_executed = True
                            has_tool_results = True
                            messages.append({"role": "tool", "content": json.dumps({
                                "name": name,
                                "result": f"[Command failed, but read the file directly instead]\n{verify_tool_output(fb_result)}"
                            })})
                            continue

                consecutive_errors += 1

                # ── RETRY tier: Reflection Phase ─────────────────────
                print(f"  [reflect] analyzing failure ({error_class})...", end="", flush=True)
                t_ref = time.time()
                reflection = reflect_on_failure(name, args, result, user_message)
                print(f" {time.time() - t_ref:.1f}s")
                print(f"  [reflect] {reflection[:120]}")

                # Check if reflector says abort — require "ABORT:" or "ABORT." to avoid false positives
                first_word = reflection.strip().split()[0].upper() if reflection.strip() else ""
                if first_word == "ABORT:" or first_word == "ABORT.":
                    messages.append({"role": "tool", "content": json.dumps({
                        "name": name,
                        "result": f"FAILED: {result}\nREFLECTION: {reflection}"
                    })})
                    return f"Task aborted: {reflection}"

                fail_msg = f"FAILED: {result}\nREFLECTION: {reflection}\nTry a different approach."
                fw = tool_failure_warning(name)
                if fw:
                    fail_msg += f"\n{fw}"
                messages.append({"role": "tool", "content": json.dumps({
                    "name": name, "result": fail_msg
                })})
            else:
                record_tool_success(name)
                flight_log(name, args, result, False)
                autonomy_record_call(is_error=False)
                consecutive_errors = 0  # reset on success
                any_executed = True
                has_tool_results = True
                messages.append({"role": "tool", "content": json.dumps({
                    "name": name, "result": verify_tool_output(result)
                })})

        # ── Circuit Breaker (DEGRADE tier) ─────────────────────────
        if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            print(f"  [CIRCUIT BREAKER] {consecutive_errors} consecutive errors — degrading")
            return gather_partial_results(messages)

    return "(max turns reached — stopping)"

# ── Logging ─────────────────────────────────────────────────────────────────

def log_interaction(user_msg: str, response: str):
    """Log interaction to daily log file."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.log"
    timestamp = datetime.now().strftime("%H:%M:%S")
    with open(log_file, "a") as f:
        f.write(f"\n{'='*60}\n")
        f.write(f"[{timestamp}] USER: {user_msg}\n")
        f.write(f"[{timestamp}] SMOLCLAW: {response}\n")

# ── Main ────────────────────────────────────────────────────────────────────

def main():
    print("""
    ╔═══════════════════════════════════════════╗
    ║  🦀 SmolClaw v0.8.0                      ║
    ║  SmolLM3-3B · 3-NUC Cognitive Cluster   ║
    ║  $75 yard sale hardware · AI for all    ║
    ╚═══════════════════════════════════════════╝
    """)

    # Health check — all 3 NUCs
    nodes = [
        ("NUC1/Actor  (tailscale)", "http://100.126.137.93:8090",  True),
        ("NUC2/Critic (tailscale)", "http://100.104.164.38:8090",  False),
        ("NUC3/Memory (tailscale)", "http://100.110.49.11:8090",   False),
    ]
    for name, url, required in nodes:
        try:
            with urllib.request.urlopen(f"{url}/health", timeout=5) as r:
                health = json.loads(r.read())
                if health.get("status") == "ok":
                    print(f"  {name}: ONLINE")
        except Exception:
            print(f"  {name}: OFFLINE")
            if required:
                print("  Start it: systemctl --user start smollm3")
                sys.exit(1)
            else:
                print(f"  WARNING: {name.split('/')[1].split()[0].lower()} will fall back to local inference")

    # Grounding status
    if BRAVE_API_KEY:
        print(f"  Web Grounding: ACTIVE (Brave Search)")
    else:
        print(f"  Web Grounding: DISABLED (no API key — see ~/.config/smolclaw/brave.key)")

    # Autonomy state
    state = load_autonomy_state()
    allowed, reason = autonomy_check()
    print(f"  Autonomy: {'ACTIVE' if allowed else 'DEFERRED'} ({reason})")
    print(f"  Daily calls: {state.get('daily_calls', 0)}/{DAILY_CALL_BUDGET} | Tokens: {state.get('daily_tokens', 0)}/{DAILY_TOKEN_BUDGET}")
    print()

    if len(sys.argv) > 1:
        msg = " ".join(sys.argv[1:])
        response = run_agent_aot(msg)
        print(f"\n{response}")
        log_interaction(msg, response)
    else:
        while True:
            try:
                user_input = input("\nyou > ").strip()
                if not user_input:
                    continue
                if user_input.lower() in ("exit", "quit", "bye"):
                    print("SmolClaw out. 🦀")
                    break

                response = run_agent_aot(user_input)
                print(f"\nsmolclaw > {response}")
                log_interaction(user_input, response)

            except KeyboardInterrupt:
                print("\nSmolClaw out. 🦀")
                break
            except EOFError:
                break

if __name__ == "__main__":
    main()
