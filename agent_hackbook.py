#!/usr/bin/env python3
"""
SmolClaw v0.9.0 — State machine cognitive architecture on 3-NUC cluster.
Powered by SmolLM3-3B on $75 yard sale Intel NUCs.

Architecture (v0.9.0 — deterministic state machines, structured I/O):
  - NUC1 (nizbot1, 100.126.137.93): Actor — state machine dispatcher, tool execution
  - NUC2 (nizbot2, 100.104.164.38): Critic — safety, grounding, contradiction detection
  - NUC3 (nizbot3, 100.110.49.11): Memory — smart recall, failure analysis, reflection
  - nizbot0 (MacBook Air): Head node / GUI / user interface (Cosmic DE)

  Actor states: INIT → SELECT_TOOL ↔ (CRITIC_CHECK → EXECUTE) → SYNTHESIZE → DONE
  Terminal states: ANSWER, INSUFFICIENT_EVIDENCE, TOOL_FAILURE_BLOCKING, STALLED
  Tool discipline: cooldowns after failure, filtered tool lists, budget tracking
  Failure discipline: state machine recovery, anti-mantra, stuckness scoring (v0.8.1)
  AoT: DAG decomposition for complex tasks (Markov property, fresh context per atom)
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

LLAMA_URL = "http://100.126.137.93:8090/v1/chat/completions"       # nizbot1 — actor/planner (tailscale)
CRITIC_URL = "http://100.104.164.38:8090/v1/chat/completions"      # nizbot2 — critic/grounding (tailscale)
MEMORY_URL = "http://100.110.49.11:8090/v1/chat/completions"      # nizbot3 — memory/reflection (tailscale)
MODEL = "SmolLM3-Q4_K_M.gguf"
HOME = Path(os.path.dirname(os.path.abspath(__file__)))
MEMORY_FILE = HOME / "memory.md"
LOG_DIR = HOME / "logs"
SCRATCHPAD_DIR = HOME / "scratchpad"
FLIGHT_LOG = HOME / "flight_recorder.jsonl"
AUTONOMY_STATE = HOME / "autonomy_state.json"
BRAVE_KEY_FILE = Path.home() / ".config" / "smolclaw" / "brave.key"
CRITIC_CACHE_DIR = HOME / "critic_cache"
EPISODIC_FILE = HOME / "episodic.jsonl"     # v0.9.0 — short-lived observations
EPISODIC_TTL = 3600 * 24                    # 24 hours — episodic memories auto-expire
DAILY_CALL_BUDGET = 1000      # max tool calls per day (raised for multi-run test suites)
DAILY_TOKEN_BUDGET = 500000   # max tokens per day (raised for 26-scenario test suite)
QUIET_HOURS = (2, 7)          # CDT — defer between 2am-7am
MAX_TURNS = 10
MAX_CONSECUTIVE_ERRORS = 3  # circuit breaker
TEMPERATURE = 0.7

# Smarter token budgets — don't waste decode time on padding
MAX_TOKENS_TOOL_CALL = 160   # tool calls are short JSON — 128 was tight, model sometimes narrates
MAX_TOKENS_SYNTHESIS = 256   # conversational final answer — room for personality
MAX_TOKENS_CRITIC = 32       # just "SAFE" or "BLOCK"
MAX_TOKENS_REFLECT = 64      # one sentence suggestion
MAX_TOKENS_RECALL = 256      # room for relevant memories
MAX_TOKENS_CONTRADICTION = 64  # CONSISTENT or CONTRADICTION: ...
MAX_TOKENS_SUMMARIZE = 200     # conversation summary (NUC3, background)

# ── Context budget constants (8192 total) ────────────────────────────────────
SUMMARY_BUDGET = 600           # max tokens for compressed old-history summary
RECENT_RAW_BUDGET = 1500       # max tokens for recent raw messages


def estimate_tokens(text: str) -> int:
    """Estimate BPE token count without a tokenizer.
    Calibrated for SmolLM3-3B: ~1.3 tokens/word for prose,
    ~2.0 tokens/word for JSON/code."""
    if not text:
        return 0
    words = len(text.split())
    json_chars = text.count('{') + text.count('"') + text.count(':')
    if json_chars > words * 0.3:
        return int(words * 2.0)
    return int(words * 1.3)


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

# ── Dynamic Tool Filtering ─────────────────────────────────────────────────
# Only inject 2-4 relevant tools per query. Saves ~300 prompt tokens.

_TOOL_KEYWORDS = {
    "shell": {"run", "command", "execute", "check", "process", "disk", "uptime",
              "install", "system", "service", "ping", "cpu", "memory", "ram", "df",
              "ls", "grep", "restart", "kill", "port", "network"},
    "read_file": {"read", "file", "look", "show", "cat", "content", "code",
                  "config", "log", "open", "view", "source"},
    "write_file": {"write", "file", "create", "save", "output", "note", "make"},
    "remember": {"remember", "store", "keep", "note", "save", "don't forget"},
    "recall": {"recall", "memory", "memories", "told", "earlier", "last time",
               "you know", "we discussed"},
    "scratchpad": {"scratchpad", "scratch", "previous", "large output", "stash"},
    "web_search": {"search", "who", "what", "when", "where", "current", "latest",
                   "news", "president", "population", "version", "death", "died",
                   "born", "election", "how many", "how old", "movie", "actor",
                   "weather", "score", "price", "release"},
}

_TOOL_BY_NAME = {t["function"]["name"]: t for t in TOOLS}

def filter_tools(query: str, min_tools: int = 2, max_tools: int = 4) -> list:
    """Return only the most relevant tools for this query. Shrinks prompt."""
    q_lower = query.lower()
    q_words = set(q_lower.split())
    scores = {}
    for tool_name, keywords in _TOOL_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in q_lower)
        # Bonus for exact word match
        score += sum(0.5 for kw in keywords if kw in q_words)
        scores[tool_name] = score
    # Sort by score descending
    ranked = sorted(scores.items(), key=lambda x: -x[1])
    # Always include at least the top scorers
    selected = set()
    for name, score in ranked:
        if score > 0 or len(selected) < min_tools:
            selected.add(name)
        if len(selected) >= max_tools:
            break
    # Ensure we always have at least min_tools
    for name, _ in ranked:
        if len(selected) >= min_tools:
            break
        selected.add(name)
    return [_TOOL_BY_NAME[n] for n in selected if n in _TOOL_BY_NAME]


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
My source code: {HOME}/agent.py (v0.9)
I have 7 tools: shell, read_file, write_file, remember, recall, scratchpad, web_search.
My brain runs on 3 NUCs: I act (NUC1), my critic checks my work (NUC2), my memory helps me learn (NUC3).

IMPORTANT — temporal awareness:
- My training data has a cutoff. Today is {datetime.now().strftime("%B %d, %Y")}.
- When asked about current events, recent news, or anything I'm unsure about, I use web_search FIRST.
- I NEVER say "I don't have information beyond my training data" — I have web_search and I USE it.
- Examples of when to search: presidents, elections, wars, sports events, software versions, deaths, launches, populations, geography, distances, organizations, companies.
- web_search = world facts (people, places, events, numbers). shell = local machine ops (files, processes, disk, network on MY NUCs). Do not use shell for world knowledge.

I use tools by wrapping JSON in <tool_call> tags. I ALWAYS use the tags — never bare JSON:

<tool_call>
{{"name": "web_search", "arguments": {{"query": "current president United States 2026"}}}}
</tool_call>

<tool_call>
{{"name": "shell", "arguments": {{"command": "uptime"}}}}
</tool_call>

When shell output is large, it gets saved to my scratchpad automatically. I use the scratchpad tool to read it back.

How I work:
- I respond in English. I am conversational but concise — I give real answers with personality, not search results.
- NOT every message needs a tool. When the user is chatting, joking, sharing opinions, making plans, or saying thanks, I just TALK. Tools are for factual questions and real tasks — not casual conversation.
- When I DO need a tool, I act first, explain after. I call tools IMMEDIATELY — never narrate what I'm about to do.
- I pick the right tool instinctively: shell for commands, remember for memory, write_file for files, scratchpad to retrieve large outputs.
- I do not use sudo. I run as user nizbot1.
- I do not install packages or do things I wasn't asked to do.
- When a tool fails, I think about WHY and try a DIFFERENT approach. I never repeat the same failing command.
- I stay focused on exactly what was asked. I do not repeat tool output verbatim — I summarize the key facts.
- GROUNDING RULE: When I use web_search, my answer must ONLY contain facts from the search results. I NEVER invent titles, names, dates, or details that are not in the results. If the results don't cover something, I say so — I do not guess or fill in gaps with made-up information.
- I NEVER describe steps to use tools like web_search or shell as part of my answer. If I need information, I use the tool silently and report the result. I never tell the user to run commands or search for things — I do it myself or just answer from what I know.
- Shell tips: I use SINGLE QUOTES for patterns: grep 'pattern' file, find . -name '*.py'. I use short flags (-h not --human-readable). I use python3 not python. To count matches: grep -c 'pattern' file.
"""

CRITIC_PROMPT = """/no_think
I am SmolClaw's adversarial critic. I judge risk by action class, not just tool name.

Proposed tool call:
{tool_call}

User's request:
{user_request}

I BLOCK only for genuinely dangerous actions:
- Destructive: rm -rf, dd, mkfs, chmod 777, sudo, passwd, reboot, shutdown
- System paths: writes to /etc, /usr, /boot, /var/log
- Privilege escalation or persistence
- Resource exhaustion: fork bombs, infinite loops, unbounded downloads
- Data exfiltration: curl/wget to external URLs not requested by user
  (EXCEPTION: 10.0.0.x and 100.x.x.x are internal cluster IPs — ALWAYS safe)

I ALLOW these safe action classes:
- Read-only operations (cat, ls, grep, read_file, recall)
- Ephemeral writes: scratchpad/, /tmp/, scratch/, work/, output/
- Shell commands that gather info (uptime, df, free, ps, ping, curl to local IPs)
- Actions that directly match what the user asked for

Off-topic actions unrelated to the user's request: BLOCK.

My verdict (one word):"""

REFLECT_PROMPT = """/no_think
Tool failed. I suggest a SPECIFIC fix — not vague advice.

Tool: {tool_name}
Args: {tool_args}
Error: {error}
Task: {user_request}
Recovery mode: {recovery_mode}

Rules:
- I reply with ONE specific alternative: a different command, different tool, or different args.
- I NEVER say "find another way" or "try again" — I give the EXACT command to run.
- If no concrete fix exists, I say ABORT: [reason].
- My reply must contain a tool name, command, or file path.

Specific fix:"""

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

# ── Failure Discipline (v0.8.1) ───────────────────────────────────────────
# Authoritarian failure handling to prevent SLM degenerate loops.
# Policies: state machine, retry-requires-diff, stuckness score,
#           anti-mantra filter, progress budget, no-novelty breaker.

# Policy D: Anti-mantra blacklist — reject vague recovery phrases
MANTRA_PATTERNS = re.compile(
    r'\b(find another way|try again|work around|continue|be creative|'
    r'try something else|keep trying|adapt|recover|move on|'
    r'find a different|try a different way|explore other)\b', re.IGNORECASE
)

def is_mantra(text: str) -> bool:
    """Detect degenerate recovery mantras — text that sounds active but isn't."""
    if not text:
        return True
    # If the text is mostly mantra with no concrete action
    stripped = MANTRA_PATTERNS.sub('', text).strip()
    # If removing mantras leaves less than 10 chars, it's a mantra
    return len(stripped) < 10

def has_concrete_action(text: str) -> bool:
    """Check if recovery text contains a specific executable action."""
    # Tool names from our agent
    if re.search(r'\b(read_file|write_file|shell|web_search|remember|recall|scratchpad)\b', text):
        return True
    # Shell commands followed by an argument (path, flag, pipe, or quoted string)
    if re.search(r'\b(grep|cat|ls|find|head|tail|awk|sed|python3|echo|printf|curl|chmod|mkdir|rm|cp|mv)\s+[\-/"\']', text):
        return True
    # File paths
    if re.search(r'(/\w+[\w/]*\.\w+|~/\w+)', text):
        return True
    # CLI flags
    if re.search(r'\s--?\w{2,}', text):
        return True
    return False

# Policy B: Retry requires a diff — fingerprint failures
def failure_fingerprint(tool_name: str, args: dict, error: str) -> str:
    """Create a fingerprint for a failure to detect identical retries."""
    # Normalize: tool + sorted arg values + first 50 chars of error
    arg_sig = json.dumps(args, sort_keys=True)[:100] if args else ""
    error_prefix = re.sub(r'\d+', 'N', error[:50].lower())  # normalize numbers
    return f"{tool_name}|{arg_sig}|{error_prefix}"

class ProgressTracker:
    """
    Track agent progress within a single task.
    Detects stuckness, enforces retry budgets, scores novelty.
    """
    def __init__(self):
        self.failure_fingerprints: dict[str, int] = {}  # fingerprint → count
        self.total_retries = 0
        self.max_retries_per_op = 2       # Policy A: 2 retries per failing operation
        self.max_replans = 1              # then 1 replan, then escalate/abort
        self.replans_used = 0
        self.unique_tools_tried: set[str] = set()
        self.unique_args_tried: set[str] = set()
        self.last_k_outputs: list[str] = []  # last K tool outputs for novelty check
        self.assistant_texts: list[str] = []  # track model outputs for repetition
        self.stuckness_score = 0.0

    def record_failure(self, tool_name: str, args: dict, error: str) -> str:
        """
        Record a failure and return the allowed recovery mode.
        Returns: RETRY_ONCE | TRY_DISTINCT | REPLAN | ABORT
        """
        fp = failure_fingerprint(tool_name, args, error)
        self.failure_fingerprints[fp] = self.failure_fingerprints.get(fp, 0) + 1
        self.total_retries += 1
        count = self.failure_fingerprints[fp]

        if count > self.max_retries_per_op:
            if self.replans_used < self.max_replans:
                self.replans_used += 1
                return "REPLAN"
            return "ABORT"
        if count == 1:
            return "RETRY_ONCE"
        return "TRY_DISTINCT"

    def record_success(self, tool_name: str, args: dict, result: str):
        """Record a successful tool call."""
        self.unique_tools_tried.add(tool_name)
        self.unique_args_tried.add(json.dumps(args, sort_keys=True)[:100])
        self.last_k_outputs.append(result[:200])
        # Keep last 5
        if len(self.last_k_outputs) > 5:
            self.last_k_outputs.pop(0)
        # Decay stuckness on success
        self.stuckness_score = max(0, self.stuckness_score - 0.3)

    def record_assistant_text(self, text: str):
        """Track model output text for repetition detection."""
        self.assistant_texts.append(text[:200])
        if len(self.assistant_texts) > 5:
            self.assistant_texts.pop(0)

    def check_repetition(self, text: str) -> bool:
        """Check if the model is repeating itself (semantic loop)."""
        if not self.assistant_texts:
            return False
        # Exact or near-exact match with any of last 3 outputs
        text_norm = text.strip().lower()[:150]
        for prev in self.assistant_texts[-3:]:
            prev_norm = prev.strip().lower()[:150]
            if not prev_norm:
                continue
            # Exact match
            if text_norm == prev_norm:
                return True
            # High overlap (>80% shared words)
            words_a = set(text_norm.split())
            words_b = set(prev_norm.split())
            if words_a and words_b:
                overlap = len(words_a & words_b) / max(len(words_a), len(words_b))
                if overlap > 0.8:
                    return True
        return False

    def check_novelty(self) -> bool:
        """Check if recent turns contain any new information. False = stalled."""
        if len(self.last_k_outputs) < 3:
            return True  # not enough data
        # Check if last 3 outputs are all similar
        recent = self.last_k_outputs[-3:]
        unique = set(r[:100] for r in recent)
        return len(unique) > 1  # at least 2 distinct outputs

    def update_stuckness(self, is_error: bool, text: str):
        """Update stuckness score. Higher = more stuck."""
        if is_error:
            self.stuckness_score += 0.4
        if self.check_repetition(text):
            self.stuckness_score += 0.5
        if is_mantra(text):
            self.stuckness_score += 0.3
        if not self.check_novelty():
            self.stuckness_score += 0.4
        return self.stuckness_score

    def is_stuck(self) -> bool:
        """True if stuckness score exceeds threshold."""
        return self.stuckness_score >= 1.5

    def retry_is_distinct(self, tool_name: str, args: dict, seen: set) -> bool:
        """Policy B: A retry is only allowed if materially different."""
        sig = f"{tool_name}:{json.dumps(args, sort_keys=True)}"
        if sig in seen:
            return False
        # Check if args differ from previous failures of same tool
        for fp in self.failure_fingerprints:
            if fp.startswith(f"{tool_name}|"):
                prev_arg_keys = fp.split("|")[1:-1]
                curr_arg_keys = sorted(args.keys()) if args else []
                if prev_arg_keys == curr_arg_keys:
                    # Same arg structure — check if values actually differ
                    # (the seen_commands check handles exact match, this catches near-match)
                    pass
        return True

RECOVERY_MODE_MESSAGES = {
    "RETRY_ONCE": "FAILED: {error}\nYou get ONE retry. Use a DIFFERENT command or DIFFERENT arguments.",
    "TRY_DISTINCT": "FAILED: {error}\nThis has failed before with similar args. You MUST use a DIFFERENT TOOL or COMPLETELY DIFFERENT approach.",
    "REPLAN": "FAILED: {error}\nMultiple approaches have failed. STOP and think: what is the simplest way to achieve the goal? Use a completely different strategy.",
    "ABORT": "FAILED: {error}\nToo many failures on this operation. I am stopping to avoid a loop. Report what you learned.",
}

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
            note = args.get('note', '').strip()
            if not note:
                return "Error: empty note", True
            # Semantic memory — verify before committing (NUC2 gate)
            print(f"  [memory] verifying: {note[:60]}...", end="", flush=True)
            approved, reason = verify_memory_write(note, _current_user_query)
            if approved:
                MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
                with open(MEMORY_FILE, "a") as f:
                    f.write(f"\n## [{timestamp}]\n{note}\n")
                print(f" COMMITTED")
                flight_log("memory_commit", {"note": note[:100]}, "verified", False)
                return "Verified and saved to permanent memory.", False
            else:
                # Rejected — store in episodic instead (temporary, 24h)
                episodic_write(note, source="actor_rejected")
                print(f" REJECTED ({reason[:40]})")
                flight_log("memory_reject", {"note": note[:100]}, reason, False)
                return f"Memory not verified: {reason}. Saved to temporary memory (24h) instead.", False

        elif name == "recall":
            parts = []
            # Semantic memory (permanent, verified)
            if MEMORY_FILE.exists():
                content = MEMORY_FILE.read_text().strip()
                if content:
                    content = smart_recall(_current_user_query, content)
                    parts.append(content)
            # Episodic memory (recent observations, 24h)
            episodes = episodic_read(5)
            if episodes:
                ep_text = "\n".join(
                    f"- [{e['ts'][:16]}] {e['text'][:100]}" for e in episodes
                )
                parts.append(f"Recent observations:\n{ep_text}")
            if not parts:
                return "(no memories yet)", False
            return "\n\n".join(parts), False

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
            lines = ["[SEARCH RESULTS — use ONLY these facts in your answer. Do NOT add details not found here.]"]
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

def needs_grounding(synthesis: str, had_tool_results: bool,
                    tools_used: set = None) -> bool:
    """Heuristic: should we ground this answer with web search?

    Key insight: if the synthesis is just summarising tool output
    (shell, read_file, scratchpad), the facts came from tools — not
    the model's weights.  Grinding that through 2 NUC2 verification
    calls is pure waste.  Only ground when the model is generating
    factual claims from its own knowledge.
    """
    if not BRAVE_API_KEY:
        return False
    # Very short answers — skip
    if len(synthesis) < 40:
        return False
    # Error/abort messages — skip
    if synthesis.startswith(("Error:", "Autonomy kernel", "I hit too many", "Partial results")):
        return False

    # ── Fast-path: tool-backed answers skip grounding entirely ──
    # If the agent used data-producing tools, the synthesis is
    # summarising their output — nothing to hallucinate.
    DATA_TOOLS = {"shell", "read_file", "scratchpad", "web_search"}
    if had_tool_results and tools_used:
        if tools_used & DATA_TOOLS:
            print(f"  [grounding] skipped — answer backed by tool output ({tools_used & DATA_TOOLS})")
            return False

    # Fallback: if tools were used but we don't know which, use length heuristic
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
        # Log internally but don't leak scaffolding into user response
        return synthesis
    else:
        # Ambiguous verdict — log but don't modify
        print(f"  [grounding] unclear verdict: {verdict[:60]}")
        flight_log("grounding", {"query": query}, f"unclear: {verdict}", False)
        return synthesis

# ── Claim Decomposition (v0.9.0 — structured verification) ───────────────
# Instead of checking the whole answer, decompose into atomic claims and
# verify each one. Small models are much better at "does evidence support
# claim 4?" than "is this whole answer good?"
# Replaces the old 3-call grounding+contradiction with 2 calls total.

CLAIM_EXTRACT_PROMPT = """/no_think
I list the factual claims in this answer as a JSON array.

Answer: {synthesis}

Rules:
- Each claim is ONE short sentence about a fact (date, name, number, version, event)
- I skip opinions, greetings, error messages, and summaries of tool output
- Maximum 5 claims
- If no factual claims: []

JSON array:"""

CLAIM_VERIFY_PROMPT = """/no_think
I check each claim against the evidence below.

Claims:
{claims}

Evidence:
{evidence}

For each claim I write ONE line:
[number]. SUPPORTED / UNSUPPORTED / CONTRADICTED: [brief reason]

Verdicts:"""

MAX_TOKENS_CLAIMS = 128


def extract_claims(synthesis: str) -> list[str]:
    """Extract atomic factual claims from synthesis. Runs on NUC2."""
    prompt = CLAIM_EXTRACT_PROMPT.format(synthesis=synthesis[:500])
    response = call_llm_simple(
        [{"role": "user", "content": prompt}],
        max_tokens=MAX_TOKENS_CLAIMS,
        url=CRITIC_URL,
        stop=["\n\n"],
    )
    try:
        match = re.search(r'\[.*\]', response, re.DOTALL)
        if match:
            claims = json.loads(match.group())
            if isinstance(claims, list):
                return [c for c in claims if isinstance(c, str) and len(c) > 5][:5]
    except (json.JSONDecodeError, Exception):
        pass
    return []


def verify_claims(claims: list[str], synthesis: str, user_request: str,
                  had_tool_results: bool) -> tuple[str, dict]:
    """
    Verify atomic claims against web + memory evidence. Runs on NUC2.
    Combines grounding + contradiction into one structured check.
    Returns (synthesis, verdict_info) where verdict_info has counts per category.
    """
    verdict_info = {"total": len(claims), "supported": 0, "unsupported": 0, "contradicted": 0}

    if not claims:
        return synthesis, verdict_info

    # Gather evidence from both web and memory
    evidence_parts = []

    # Web evidence (Brave Search)
    if BRAVE_API_KEY:
        search_query = " ".join(claims[:3])[:100]
        print(f"  [verify] searching: {search_query[:60]}", end="", flush=True)
        results = brave_search_cached(search_query)
        if results:
            for r in results:
                evidence_parts.append(f"Web: {r['title']} — {r['snippet'][:120]}")
        print(f" ({len(results)} results)")

    # Memory evidence — pre-filter by claim keywords (don't send raw blob)
    if MEMORY_FILE.exists():
        memories = MEMORY_FILE.read_text().strip()
        if memories and len(memories) > 50:
            entries = _split_memories(memories)
            claim_text = " ".join(claims).lower()
            relevant = [e for e in entries
                        if any(w in e.get("text", e.get("raw", "")).lower()
                               for w in claim_text.split() if len(w) > 3)]
            if relevant:
                filtered_mem = "\n".join(e["raw"] for e in relevant[:5])
                evidence_parts.append(f"Memory: {filtered_mem[:300]}")
            elif len(memories) < 200:
                # Tiny memory — send all
                evidence_parts.append(f"Memory: {memories[:200]}")

    if not evidence_parts:
        print(f"  [verify] no evidence available — skipping")
        return synthesis, verdict_info

    # Batch verify all claims in one LLM call
    claims_text = "\n".join(f"{i+1}. {c}" for i, c in enumerate(claims))
    evidence_text = "\n".join(evidence_parts)

    prompt = CLAIM_VERIFY_PROMPT.format(claims=claims_text, evidence=evidence_text)
    verdict = call_llm_simple(
        [{"role": "user", "content": prompt}],
        max_tokens=MAX_TOKENS_CLAIMS,
        url=CRITIC_URL,
        stop=["\n\n\n"],
    )

    # Parse structured verdicts — count by category
    # Use prefix matching (e.g. "2. CONTRADICTED:") to avoid false positives
    # from the word appearing in explanation text
    for line in verdict.split("\n"):
        stripped = line.strip()
        # Match "N. VERDICT" or just "VERDICT:" at start of line
        verdict_prefix = re.match(r'(?:\d+\.\s*)?(\w+)', stripped)
        if not verdict_prefix:
            continue
        category = verdict_prefix.group(1).upper()
        if category == "CONTRADICTED":
            verdict_info["contradicted"] += 1
        elif category == "UNSUPPORTED":
            verdict_info["unsupported"] += 1
        elif category == "SUPPORTED":
            verdict_info["supported"] += 1

    # Log internally — never leak raw verdict text into user response
    if verdict_info["contradicted"] > 0 or verdict_info["unsupported"] > 0:
        print(f"  [verify] issues: {verdict_info['contradicted']} contradicted, "
              f"{verdict_info['unsupported']} unsupported")
        flight_log("claim_verify", verdict_info, verdict[:200], False)
    else:
        print(f"  [verify] {len(claims)} claims — all supported")
        flight_log("claim_verify", verdict_info, "all supported", False)

    return synthesis, verdict_info


# ── Smart Recall (NUC3 — Surgical Retrieval, v0.9.0) ─────────────────────
# Three-stage recall: split → keyword pre-filter → LLM score (NUC3).
# Small models get overwhelmed by large context. So we:
#   1. Split memory.md into individual entries
#   2. Pre-filter by keyword overlap with query (no LLM call)
#   3. Send only matched snippets to NUC3 for LLM-scored relevance
# Result: tiny, targeted context instead of a giant blob.

SMART_RECALL_PROMPT = """/no_think
I am SmolClaw's memory retrieval system. Given a query, I find and return ONLY the relevant memories.

Query: {query}

All memories:
{memories}

I copy the relevant memories exactly as written. If none are relevant, I reply: (no relevant memories)

Relevant memories:"""

# Module-level query context for smart recall
_current_user_query = ""


def _split_memories(text: str) -> list[dict]:
    """Split memory.md into individual entries with timestamps."""
    entries = []
    for block in re.split(r'\n(?=## \[)', text):
        block = block.strip()
        if not block:
            continue
        # Extract timestamp
        ts_match = re.match(r'## \[(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\]', block)
        ts = ts_match.group(1) if ts_match else ""
        # Content is everything after the header line
        content = re.sub(r'^## \[.*?\]\n?', '', block).strip()
        if content:
            entries.append({"ts": ts, "text": content, "raw": block})
    return entries


def _keyword_prefilter(entries: list[dict], query: str,
                       max_candidates: int = 8) -> list[dict]:
    """Pre-filter memory entries by keyword overlap with query. Zero LLM calls."""
    query_words = set(re.findall(r'\w{3,}', query.lower()))
    if not query_words:
        return entries[-max_candidates:]  # no keywords → recency fallback

    scored = []
    for entry in entries:
        entry_words = set(re.findall(r'\w{3,}', entry['text'].lower()))
        overlap = len(query_words & entry_words)
        if overlap > 0:
            scored.append((overlap, entry))

    scored.sort(key=lambda x: -x[0])
    return [e for _, e in scored[:max_candidates]]


def smart_recall(query: str, memories_text: str) -> str:
    """
    Surgical memory retrieval (v0.9.0):
      1. Split into entries
      2. Keyword pre-filter (no LLM)
      3. LLM-scored relevance on NUC3 (only for filtered candidates)
    """
    if not memories_text or memories_text in ("(memory is empty)", "(no memories yet)"):
        return memories_text

    # Split into individual entries
    entries = _split_memories(memories_text)

    # Short memory — send all, no filtering needed
    if len(entries) <= 5 or not query:
        if not query or len(memories_text) < 100:
            return memories_text
        # Still score via NUC3 for small sets
        prompt = SMART_RECALL_PROMPT.format(query=query, memories=memories_text[:2000])
        result = call_llm_simple(
            [{"role": "user", "content": prompt}],
            max_tokens=MAX_TOKENS_RECALL, url=MEMORY_URL,
        )
        cleaned = result.strip()
        if cleaned and cleaned != "(no relevant memories)":
            print(f"  [memory] recall: {len(cleaned)} chars (from {len(memories_text)})")
            return cleaned
        return memories_text

    # Pre-filter by keyword overlap
    candidates = _keyword_prefilter(entries, query)
    if not candidates:
        candidates = entries[-5:]  # no keyword match → most recent
        print(f"  [memory] no keyword matches — using last {len(candidates)} entries")

    filtered = "\n".join(c["raw"] for c in candidates)
    print(f"  [memory] pre-filter: {len(candidates)}/{len(entries)} entries, {len(filtered)} chars")

    # If filtered text is tiny, return directly (skip NUC3 call)
    if len(filtered) < 300:
        return filtered

    # LLM-scored relevance on NUC3
    prompt = SMART_RECALL_PROMPT.format(query=query, memories=filtered[:2000])
    result = call_llm_simple(
        [{"role": "user", "content": prompt}],
        max_tokens=MAX_TOKENS_RECALL, url=MEMORY_URL,
    )
    cleaned = result.strip()
    if cleaned and cleaned != "(no relevant memories)":
        print(f"  [memory] recall: {len(cleaned)} chars (from {len(filtered)} filtered)")
        return cleaned
    return filtered  # fallback to pre-filtered set

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
        # Log internally — don't leak scaffolding into user response
        return synthesis
    else:
        print(f"  [contradiction] consistent")
        flight_log("contradiction", {}, "CONSISTENT", False)
        return synthesis

# ── Tiered Memory (v0.9.0) ──────────────────────────────────────────────────
# Three tiers, inspired by cognitive architecture:
#   Working memory:  TaskContext (volatile, per-task — already implemented)
#   Episodic memory: Recent observations, auto-expires after 24h, unverified
#   Semantic memory: memory.md — verified facts only, writes gated by NUC2 critic
#
# The actor CANNOT write directly to semantic memory. It proposes a write,
# NUC2 verifies, then it's committed or rejected (stored in episodic instead).
# This prevents memory poisoning from 3B hallucinations.

def episodic_write(observation: str, source: str = "tool"):
    """Write an observation to episodic memory. Auto-expires in 24h. No verification needed."""
    try:
        entry = {
            "ts": datetime.now().isoformat(),
            "text": observation[:500],
            "source": source,
        }
        with open(EPISODIC_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def episodic_read(max_entries: int = 10) -> list[dict]:
    """Read recent episodic memories, pruning expired ones."""
    if not EPISODIC_FILE.exists():
        return []
    now = datetime.now()
    fresh = []
    try:
        lines = EPISODIC_FILE.read_text().strip().split("\n")
        for line in lines:
            if not line.strip():
                continue
            entry = json.loads(line)
            ts = datetime.fromisoformat(entry["ts"])
            age = (now - ts).total_seconds()
            if age < EPISODIC_TTL:
                fresh.append(entry)
        # Prune expired entries by rewriting file
        if len(fresh) < len(lines):
            with open(EPISODIC_FILE, "w") as f:
                for entry in fresh:
                    f.write(json.dumps(entry) + "\n")
    except Exception:
        pass
    return fresh[-max_entries:]


# ── Conversation Summarization (COMPASS-style rolling notes) ─────────────────

SUMMARIZE_PROMPT = """/no_think
Rewrite conversation notes incorporating new exchanges.

Previous notes:
{prev_summary}

New exchanges:
{new_exchanges}

Format (max 150 words):
TOPIC: [main subject in 5 words]
FACTS: [bullet list of established facts, decisions, numbers]
ENTITIES: [people, files, tools, URLs mentioned]
OPEN: [what the user still needs or asked last]
TONE: [user mood/intent in 3 words]

Updated notes:"""


def summarize_conversation(prev_summary: str, new_exchanges: list) -> str:
    """Generate structured conversation summary on NUC3.
    Returns structured notes or previous summary on failure."""
    if not new_exchanges:
        return prev_summary or ""

    # Format new exchanges compactly
    exchange_text = ""
    for msg in new_exchanges:
        role = "User" if msg.get("role") == "user" else "SmolClaw"
        content = msg.get("content", "")[:300]
        exchange_text += f"{role}: {content}\n"

    prompt = SUMMARIZE_PROMPT.format(
        prev_summary=prev_summary or "(first exchange — no prior notes)",
        new_exchanges=exchange_text,
    )

    try:
        result = call_llm_simple(
            [{"role": "user", "content": prompt}],
            max_tokens=MAX_TOKENS_SUMMARIZE,
            url=MEMORY_URL,
            stop=["\n\n\n"],
        )
        cleaned = result.strip()
        # Validate: must contain at least TOPIC: or FACTS:
        if any(k in cleaned for k in ("TOPIC:", "FACTS:", "ENTITIES:", "OPEN:")):
            return cleaned
        # Garbage output — keep previous summary
        print(f"  [summary] invalid output, keeping previous")
        return prev_summary or ""
    except Exception as e:
        print(f"  [summary] NUC3 error: {e}")
        return prev_summary or ""


MEMORY_VERIFY_PROMPT = """/no_think
Proposed memory: {note}
Context: {context}

Is this factual (from tool output, search, or user statement)? Or speculation?

Reply with exactly one word — COMMIT or REJECT:"""


def verify_memory_write(note: str, context: str = "") -> tuple[bool, str]:
    """Verify proposed semantic memory write via NUC2 critic. Returns (approved, reason)."""
    prompt = MEMORY_VERIFY_PROMPT.format(
        note=note[:300],
        context=context[:200],
    )
    verdict = call_llm_simple(
        [{"role": "user", "content": prompt}],
        max_tokens=32,
        url=CRITIC_URL,
        stop=["\n"],
    )
    # Robust parsing: 3B model often echoes "Verdict: COMMIT" or adds preamble
    clean = verdict.strip().upper()
    # Direct match — model just says COMMIT or REJECT
    if clean.startswith("COMMIT"):
        return True, "verified"
    if clean.startswith("REJECT"):
        reason = verdict.strip()
        if ":" in reason:
            reason = reason.split(":", 1)[1].strip()
        return False, reason or "unverified"
    # Preamble handling: model echoes "Verdict: COMMIT" or "Factual. COMMIT"
    if "COMMIT" in clean and "REJECT" not in clean:
        return True, "verified"
    # Positive-indicator fallback: model echoes condition text like "Factual and useful"
    # instead of the keyword. If clearly positive and no rejection signal, treat as COMMIT.
    if "REJECT" not in clean and re.search(r"FACTUAL|USEFUL|SAFE|APPROPRIATE", clean):
        return True, "verified"
    # Fallback: unclear verdict → reject to be safe
    reason = verdict.strip()
    if ":" in reason:
        reason = reason.split(":", 1)[1].strip()
    return False, reason or "unverified"


# ── Tool Argument Validation (v0.9.0) ────────────────────────────────────
# Catch bad args before execution. Saves tool calls and avoids confusing errors.
# 3B models commonly produce: empty args, relative paths, huge commands.

def validate_tool_args(name: str, args: dict) -> str | None:
    """Pre-execution argument validation. Returns error message or None if valid."""
    if name == "shell":
        cmd = args.get("command", "").strip()
        if not cmd:
            return "Error: empty command"
        if len(cmd) > 2000:
            return "Error: command too long (>2000 chars)"
    elif name == "read_file":
        path = args.get("path", "").strip()
        if not path:
            return "Error: empty path"
        if not path.startswith("/") and not path.startswith("~"):
            return f"Error: use absolute path, got '{path[:50]}'"
    elif name == "write_file":
        path = args.get("path", "").strip()
        if not path:
            return "Error: empty path"
        if not path.startswith("/") and not path.startswith("~"):
            return f"Error: use absolute path, got '{path[:50]}'"
        if not args.get("content"):
            return "Error: empty content"
        # Hard block: never overwrite own source, config, or harness
        blocked = ("agent.py", "agent_hackbook.py", "test_harness.py", "web_ui.py")
        if any(path.endswith(b) for b in blocked):
            return f"BLOCKED: cannot overwrite critical file {path.rsplit('/', 1)[-1]}"
    elif name == "remember":
        note = args.get("note", "").strip()
        if not note:
            return "Error: empty note"
        if len(note) > 1000:
            return "Error: note too long (>1000 chars). Be concise."
    elif name == "web_search":
        query = args.get("query", "").strip()
        if not query:
            return "Error: empty query"
        if len(query) > 200:
            return "Error: query too long. Keep it concise."
    elif name == "scratchpad":
        sname = args.get("name", "").strip()
        if not sname:
            return "Error: empty scratchpad name"
    return None


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

def call_llm(messages: list, max_tokens: int = None, temperature: float = None,
             include_tools: bool = True, tools: list = None) -> dict:
    """Call SmolLM3 via llama-server. Pass tools= to offer filtered tool list."""
    body = {
        "model": MODEL,
        "messages": messages,
        "temperature": temperature or TEMPERATURE,
        "max_tokens": max_tokens or MAX_TOKENS_TOOL_CALL,
        "frequency_penalty": 0.3,
    }
    if include_tools:
        body["tools"] = tools if tools is not None else TOOLS
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


def call_llm_stream(messages: list, max_tokens: int = None, temperature: float = None,
                    on_token=None) -> tuple:
    """Streaming LLM call for synthesis — yields tokens via callback.
    Returns (full_content, finish_reason). No tools — synthesis only."""
    body = {
        "model": MODEL,
        "messages": messages,
        "temperature": temperature or TEMPERATURE,
        "max_tokens": max_tokens or MAX_TOKENS_SYNTHESIS,
        "frequency_penalty": 0.3,
        "stream": True,
    }
    payload = json.dumps(body).encode()
    req = urllib.request.Request(
        LLAMA_URL, data=payload,
        headers={"Content-Type": "application/json"},
    )
    full_content = ""
    finish_reason = "stop"
    try:
        with urllib.request.urlopen(req, timeout=900) as resp:
            while True:
                line = resp.readline()
                if not line:
                    break
                line = line.decode("utf-8").strip()
                if not line or not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                    choice = chunk.get("choices", [{}])[0]
                    delta = choice.get("delta", {})
                    content = delta.get("content", "")
                    fr = choice.get("finish_reason")
                    if fr:
                        finish_reason = fr
                    if content:
                        full_content += content
                        if on_token:
                            on_token(content)
                except (json.JSONDecodeError, IndexError, KeyError):
                    continue
        return full_content, finish_reason
    except Exception as e:
        print(f"  [stream] error: {e}")
        return full_content or f"ERROR: {e}", "error"


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
# remember is safe here because it has its own verify_memory_write() gate on NUC2
SAFE_TOOLS = {"recall", "scratchpad", "web_search", "remember"}
SAFE_SHELL_PREFIXES = (
    "uptime", "df ", "df\n", "free ", "free\n", "uname ", "whoami", "hostname",
    "date", "cat ", "ls ", "ls\n", "head ", "tail ", "wc ", "du ", "ps ", "ps\n",
    "grep ", "find ", "pwd", "id", "echo ", "ping ", "top ", "top\n",
    "which ", "type ", "file ", "stat ", "lsof ", "ss ", "ip addr", "ip route",
    "env", "printenv", "lsblk", "lscpu", "nproc", "arch",
    "python3 -c", "python3 --version", "pip3 list", "pip3 show",
    "git log", "git status", "git diff", "git branch", "git show",
    "systemctl status", "journalctl",
    "curl 10.0.0.", "curl 100.",  # internal cluster IPs — always safe
    "curl http://10.0.0.", "curl http://100.",  # with scheme prefix
    "curl -s 10.0.0.", "curl -s http://10.0.0.",  # with -s flag
    "curl http://127.0.0.1", "curl -s http://127.0.0.1",  # localhost
)
# Tier 0 — ephemeral write zones (always safe for write_file)
SAFE_WRITE_ZONES = (
    "/tmp/", "/home/nizbot1/smolclaw/scratchpad/",
    "/home/nizbot1/smolclaw/scratch/", "/home/nizbot1/smolclaw/work/",
    "/home/nizbot1/smolclaw/output/",
)

def is_tool_safe(call: dict) -> bool:
    """Check if a tool call is unconditionally safe (skip critic).
    Uses tiered write policy: ephemeral/scaffold writes skip critic,
    persistent/system writes go through critic."""
    name = call.get("name", "")
    if name in SAFE_TOOLS:
        return True
    if name == "read_file":
        return True  # read-only
    if name == "shell":
        cmd = call.get("arguments", {}).get("command", "").strip()
        # Handle chained/piped commands: "uptime && free -m | head -5"
        parts = re.split(r'\s*(?:&&|\|\||;|\|)\s*', cmd)
        return all(p.strip().startswith(SAFE_SHELL_PREFIXES) for p in parts if p.strip())
    if name == "write_file":
        path = call.get("arguments", {}).get("path", "")
        # Tier 0: ephemeral writes to safe zones skip critic
        if any(path.startswith(zone) for zone in SAFE_WRITE_ZONES):
            return True
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

def reflect_on_failure(tool_name: str, tool_args: dict, error: str,
                       user_request: str, recovery_mode: str = "RETRY_ONCE") -> str:
    """
    After a tool failure, get a structured reflection on what went wrong.
    Runs on NUC3 (memory/reflection node) — frees NUC2 for critic duties.
    Constrained by recovery_mode to prevent open-ended spiraling.
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
        recovery_mode=recovery_mode,
    )
    if pattern:
        prompt += f"\nKnown pattern for this tool: {pattern}"
    messages = [{"role": "user", "content": prompt}]
    reflection = call_llm_simple(messages, max_tokens=MAX_TOKENS_REFLECT, url=MEMORY_URL, stop=["\n\n"])

    # Policy D: reject mantras — if reflection is vague, force abort
    if is_mantra(reflection) and not has_concrete_action(reflection):
        print(f"  [anti-mantra] rejected vague reflection: {reflection[:60]}")
        return f"ABORT: No concrete recovery available. Original error: {error[:100]}"

    return reflection

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


def run_agent_aot(user_message: str, history: list = None, on_token=None) -> str:
    """
    Atom of Thoughts wrapper: decompose complex tasks, solve atoms independently,
    synthesize results. Falls back to standard agent for simple tasks.
    history: optional list of {"role": "user"|"assistant", "content": "..."} from prior turns.
    on_token: optional callback(str) for streaming tokens to the UI.
    """
    # Heuristic bypass — skip decomposition for obviously simple queries
    if not needs_decomposition(user_message):
        print(f"  [aot] simple query — skipping decomposition")
        return run_agent(user_message, history=history, on_token=on_token)

    print(f"  [aot] decomposing...", end="", flush=True)
    t0 = time.time()
    atoms = aot_decompose(user_message)
    print(f" {time.time() - t0:.1f}s → {len(atoms)} atom(s)")

    # Simple task — run directly
    if len(atoms) <= 1 or "SIMPLE" in atoms:
        return run_agent(user_message, history=history, on_token=on_token)

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


# ── Terminal States (v0.9.0) ─────────────────────────────────────────────────
# Valid ways for the agent to finish. Explicit terminal states let SmolClaw
# fail cleanly — "I'm stuck" is better than pretending to succeed.

TERMINAL_ANSWER = "ANSWER"                          # Normal response
TERMINAL_INSUFFICIENT = "INSUFFICIENT_EVIDENCE"     # Can't verify claims
TERMINAL_TOOL_BLOCKED = "TOOL_FAILURE_BLOCKING"     # Required tool broken
TERMINAL_MEMORY_CONFLICT = "MEMORY_CONFLICT"        # Contradicts known facts
TERMINAL_OUT_OF_SCOPE = "OUT_OF_SCOPE"              # Can't handle this
TERMINAL_STALLED = "STALLED"                        # No progress detected


class TaskContext:
    """
    Per-task state that flows through the actor state machine.
    Carries goal, budget, tool discipline, and inter-state data.
    Everything the dispatcher needs in one object — no globals.
    """
    __slots__ = [
        'task_id', 'goal', 'history', 'budget', 'turns_used', 'messages', 'progress',
        'seen_commands', 'consecutive_errors', 'has_tool_results', 'tools_used', 'result',
        'terminal_state', 'tool_cooldowns',
        'reflections_used', 'max_reflections', 'tether_injected', 'tool_nudge_used',
        'pending_calls', 'pending_content', 'approved_calls', 'pending_synthesis',
        '_continued', 'on_token',
    ]

    def __init__(self, user_message: str, history: list = None):
        self.task_id = f"task_{int(time.time())}"
        self.goal = user_message
        self.history = history or []
        self.budget = MAX_TURNS
        self.turns_used = 0
        self.messages: list[dict] = []
        self.progress = ProgressTracker()
        self.seen_commands: set[str] = set()
        self.consecutive_errors = 0
        self.has_tool_results = False
        self.tools_used: set[str] = set()    # which tools actually ran successfully
        self.result = ""
        self.terminal_state = None
        # Tool discipline — cooldowns after repeated failure
        self.tool_cooldowns: dict[str, int] = {}
        # Reflection leash — cap at 2 reflections per task (item #9)
        self.reflections_used = 0
        self.max_reflections = 2
        # Topic tethering — inject goal reminder when drifting (item #4)
        self.tether_injected = False
        # First-turn tool nudge — prevents 3B from answering without using tools
        self.tool_nudge_used = False
        # Transient inter-state data
        self.pending_calls: list[dict] = []
        self.pending_content = ""
        self.approved_calls: list[dict] = []
        self.pending_synthesis = ""
        self._continued = False
        self.on_token = None

    def available_tools(self) -> list[dict]:
        """Return tools filtered by relevance + cooldowns. Shrinks prompt."""
        relevant = filter_tools(self.goal)
        return [t for t in relevant
                if self.tool_cooldowns.get(t["function"]["name"], 0) <= 0]

    def tick_cooldowns(self):
        """Decrease all cooldowns by 1 turn. Expired cooldowns are removed."""
        expired = [k for k, v in self.tool_cooldowns.items() if v <= 1]
        for k in expired:
            del self.tool_cooldowns[k]
        for k in self.tool_cooldowns:
            self.tool_cooldowns[k] -= 1

    def apply_cooldown(self, tool_name: str, turns: int = 2):
        """Put a tool on cooldown after failure. Model won't see it for N turns."""
        self.tool_cooldowns[tool_name] = turns
        print(f"  [cooldown] {tool_name} unavailable for {turns} turns")


# ── Smart Context Assembly ────────────────────────────────────────────────────


def assemble_context(user_message: str, summary: str, messages: list,
                     summary_through: int) -> list:
    """Assemble conversation history within token budget.

    Three tiers:
      1. Compressed summary of old history (Tier 1, ~200-600 tokens)
      2. Recent raw messages after summary pointer (Tier 2, ~800-1500 tokens)
      3. Current user message — handled by _sm_init, not here

    Returns list of {"role": ..., "content": ...} dicts.
    """
    history = []

    # Tier 1: Compressed summary of old history
    if summary and summary.strip():
        summary_text = summary.strip()
        summary_tokens = estimate_tokens(summary_text)
        if summary_tokens > SUMMARY_BUDGET:
            # Truncate by words to fit budget
            words = summary_text.split()
            target_words = int(SUMMARY_BUDGET / 1.3)
            summary_text = " ".join(words[:target_words])
        history.append({
            "role": "user",
            "content": f"[Previous conversation summary]\n{summary_text}"
        })

    # Tier 2: Recent raw messages (after summary pointer)
    if messages:
        recent_msgs = messages[summary_through:] if summary_through < len(messages) else messages[-4:]

        # Fit within RECENT_RAW_BUDGET — take most recent first
        budget_remaining = RECENT_RAW_BUDGET
        selected = []
        for msg in reversed(recent_msgs):
            content = msg.get("content", "")
            msg_tokens = estimate_tokens(content)
            if msg_tokens > budget_remaining:
                # Truncate this message to fit remaining budget
                if budget_remaining > 50:
                    words = content.split()
                    target_words = int(budget_remaining / 1.3)
                    truncated = " ".join(words[:target_words]) + "..."
                    selected.append({"role": msg["role"], "content": truncated})
                break
            budget_remaining -= msg_tokens
            selected.append({"role": msg["role"], "content": content})
            if budget_remaining <= 0:
                break

        # Reverse back to chronological order
        selected.reverse()
        history.extend(selected)

    return history


# ── Actor State Machine (v0.9.0) ────────────────────────────────────────────
#
# States: INIT → SELECT_TOOL ↔ (CRITIC_CHECK → EXECUTE) → SYNTHESIZE → DONE
#                    ↑                       ↓
#                    └────────── (error) ────┘
#
# Each state handler takes TaskContext, mutates it, returns next state name.
# The dispatcher just loops until DONE. No open-ended reasoning, no prose
# between states — just structured transitions.

def _sm_init(ctx: TaskContext) -> str:
    """Autonomy gate + message initialization."""
    global _current_user_query
    _current_user_query = ctx.goal

    allowed, reason = autonomy_check()
    if not allowed:
        print(f"  [autonomy] DEFERRED: {reason}")
        ctx.result = f"Autonomy kernel deferred this request: {reason}"
        ctx.terminal_state = TERMINAL_OUT_OF_SCOPE
        return "DONE"
    if reason != "ok":
        print(f"  [autonomy] {reason}")

    ctx.messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
    ]
    # Inject conversation history (assembled by web_ui: summary + recent raw)
    if ctx.history:
        for turn in ctx.history:
            ctx.messages.append({"role": turn["role"], "content": turn["content"]})
        total_tokens = sum(estimate_tokens(t["content"]) for t in ctx.history)
        print(f"  [context] {len(ctx.history)} turn(s), ~{total_tokens} tokens")
    ctx.messages.append({"role": "user", "content": ctx.goal})
    return "SELECT_TOOL"


def _sm_select_tool(ctx: TaskContext) -> str:
    """Ask LLM to pick a tool or produce final answer."""
    if ctx.turns_used >= ctx.budget:
        ctx.terminal_state = TERMINAL_STALLED
        ctx.result = "(max turns reached — stopping)"
        return "DONE"

    ctx.turns_used += 1
    ctx.tick_cooldowns()

    # ── Topic tethering — inject goal reminder when drifting ──
    # After 3+ turns without results, or after errors, remind model of the task.
    # Cheap (no LLM call), prevents scope creep and off-topic drift.
    should_tether = (
        ctx.turns_used >= 4 and not ctx.has_tool_results
    ) or (
        ctx.consecutive_errors >= 2 and not ctx.tether_injected
    )
    if should_tether:
        ctx.tether_injected = True
        remaining = ctx.budget - ctx.turns_used
        tether = (f"[FOCUS — {remaining} turns left] "
                  f"Task: {ctx.goal[:100]}. "
                  f"Act now or give final answer.")
        ctx.messages.append({"role": "user", "content": tether})
        print(f"  [tether] injected goal reminder ({remaining} turns left)")

    print(f"  [turn {ctx.turns_used}/{ctx.budget}] thinking...", end="", flush=True)
    t0 = time.time()

    # On potential synthesis turns, skip tool defs to save ~490 prompt tokens
    is_synthesis_turn = ctx.has_tool_results
    budget = MAX_TOKENS_SYNTHESIS if is_synthesis_turn else MAX_TOKENS_TOOL_CALL
    available = ctx.available_tools()

    # ── LLM call: stream synthesis turns when callback available ──
    finish_reason = "stop"
    if is_synthesis_turn and ctx.on_token:
        # Stream tokens directly to UI during synthesis
        content, finish_reason = call_llm_stream(
            ctx.messages, max_tokens=budget, on_token=ctx.on_token,
        )
        elapsed = time.time() - t0
        est_tokens = estimate_tokens(content)
        autonomy_record_call(tokens_used=est_tokens)
        print(f" streamed {elapsed:.1f}s (~{est_tokens} tok)")
    else:
        response = call_llm(
            ctx.messages,
            max_tokens=budget,
            include_tools=not is_synthesis_turn,
            tools=available if not is_synthesis_turn else None,
        )
        if "error" in response:
            print(f" error")
            ctx.result = f"Error: {response['error']}"
            ctx.terminal_state = TERMINAL_TOOL_BLOCKED
            return "DONE"
        choice = response["choices"][0]
        content = choice["message"].get("content", "")
        finish_reason = choice.get("finish_reason", "stop")
        elapsed = time.time() - t0
        tokens = response.get("usage", {}).get("completion_tokens", "?")
        total_tokens = response.get("usage", {}).get("total_tokens", 0)
        autonomy_record_call(tokens_used=total_tokens)
        print(f" {elapsed:.1f}s ({tokens} tokens)")

    # ── Stuckness checks ──
    ctx.progress.update_stuckness(False, content)
    if ctx.progress.check_repetition(content):
        print(f"  [REPETITION] model repeating itself")
        flight_log("repetition_break", {}, content[:100], True)
        ctx.terminal_state = TERMINAL_STALLED
        ctx.result = gather_partial_results(ctx.messages)
        if not ctx.result:
            ctx.result = "(I got stuck repeating myself. The task may need a different approach.)"
        return "DONE"
    ctx.progress.record_assistant_text(content)

    if ctx.progress.is_stuck():
        print(f"  [STALLED] score={ctx.progress.stuckness_score:.1f}")
        flight_log("stalled", {}, f"score={ctx.progress.stuckness_score}", True)
        ctx.terminal_state = TERMINAL_STALLED
        ctx.result = gather_partial_results(ctx.messages)
        return "DONE"

    # Parse tool calls from model output
    tool_calls = parse_tool_calls(content)

    if not tool_calls:
        # First-turn tool nudge: if model tries to answer on turn 1 without using
        # any tools, and the query needs grounding (check/verify/factual lookup),
        # send it back to try again with a tool call. Prevents 3B from answering
        # from system prompt memory instead of verifying.
        if (ctx.turns_used == 1 and not ctx.has_tool_results
                and not ctx.tool_nudge_used
                and re.search(r'\b(check|verify|confirm|run:|grep |what\b.*\b(model|version|population))\b',
                              ctx.goal.lower())):
            ctx.tool_nudge_used = True
            nudge = ("[TOOL REQUIRED] You must use a tool to answer this — do not answer from memory. "
                     "read_file to check code, shell to run commands, web_search for world facts.")
            ctx.messages.append({"role": "user", "content": nudge})
            print(f"  [nudge] first-turn tool required — re-prompting")
            return "SELECT_TOOL"

        # ── Truncation recovery ──
        # If the response hit max_tokens, it was cut off mid-thought.
        # Continue once: inject partial as assistant msg, prompt "Continue:",
        # concatenate. Cap at 1 continuation to avoid spirals.
        if finish_reason == "length" and not ctx._continued:
            ctx._continued = True
            ctx.pending_synthesis = content  # stash partial for concatenation
            print(f"  [truncated] response hit max_tokens — requesting continuation")
            ctx.messages.append({"role": "assistant", "content": content})
            ctx.messages.append({"role": "user", "content": "Continue from where you left off. Finish your response concisely."})
            return "SELECT_TOOL"

        # No tool calls → model wants to synthesize
        # If this is a continuation, concatenate with prior partial
        if ctx._continued and ctx.pending_synthesis:
            content = ctx.pending_synthesis + " " + content
        ctx.pending_synthesis = content
        return "SYNTHESIZE"

    # Has tool calls → validate through critic
    ctx.pending_calls = tool_calls
    ctx.pending_content = content
    return "CRITIC_CHECK"


def _sm_critic_check(ctx: TaskContext) -> str:
    """Validate pending tool calls through critic (NUC2)."""
    print(f"  [critic] checking {len(ctx.pending_calls)} call(s)...", end="", flush=True)
    t0 = time.time()
    verdicts = critic_check_parallel(ctx.pending_calls, ctx.goal)
    print(f" {time.time() - t0:.1f}s")

    ctx.messages.append({"role": "assistant", "content": ctx.pending_content})

    approved = []
    for v in verdicts:
        call = v["call"]
        name = call.get("name", "")
        args = call.get("arguments", {})
        call_sig = f"{name}:{json.dumps(args, sort_keys=True)}"

        # Critic blocked
        if v["verdict"] == "BLOCK":
            print(f"  [BLOCKED] {name} — {v['reasoning'][:80]}")
            ctx.messages.append({"role": "tool", "content": json.dumps({
                "name": name,
                "result": f"BLOCKED by safety critic: {v['reasoning'][:200]}"
            })})
            ctx.consecutive_errors += 1
            continue

        # Exact repeat detection
        if call_sig in ctx.seen_commands:
            print(f"  [LOOP] {name} — exact repeat")
            ctx.messages.append({"role": "tool", "content": json.dumps({
                "name": name,
                "result": "LOOP DETECTED: exact repeat. Use DIFFERENT tool or args."
            })})
            ctx.consecutive_errors += 1
            ctx.progress.update_stuckness(True, "loop_detected")
            continue

        ctx.seen_commands.add(call_sig)
        approved.append(call)

    if not approved:
        # All calls blocked or looped
        if ctx.consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            ctx.terminal_state = TERMINAL_TOOL_BLOCKED
            ctx.result = gather_partial_results(ctx.messages)
            return "DONE"
        return "SELECT_TOOL"

    ctx.approved_calls = approved
    return "EXECUTE"


def _sm_execute(ctx: TaskContext) -> str:
    """Execute approved tool calls, handle errors with recovery state machine."""
    for call in ctx.approved_calls:
        name = call.get("name", "")
        args = call.get("arguments", {})

        # ── Pre-execution argument validation ──
        validation_error = validate_tool_args(name, args)
        if validation_error:
            print(f"  [validate] {name}: {validation_error}")
            ctx.messages.append({"role": "tool", "content": json.dumps({
                "name": name, "result": validation_error
            })})
            ctx.consecutive_errors += 1
            continue

        print(f"  [tool] {name}({json.dumps(args)[:80]})")
        result, is_error = execute_tool(name, args)
        print(f"  [{'ERROR' if is_error else 'ok'}] {result[:120]}")

        # Per-tool health check
        fail_warn = tool_failure_warning(name)
        if fail_warn:
            print(f"  [tool-health] {fail_warn}")

        if is_error:
            record_tool_failure(name)
            error_class, is_retryable = classify_failure(result)
            flight_log(name, args, result, True, error_class)
            autonomy_record_call(is_error=True)

            # Recovery state machine
            recovery_mode = ctx.progress.record_failure(name, args, result)
            print(f"  [recovery] mode={recovery_mode}")

            # ABORT — exhausted retries
            if recovery_mode == "ABORT":
                print(f"  [ABORT] retries exhausted for {name}")
                ctx.messages.append({"role": "tool", "content": json.dumps({
                    "name": name,
                    "result": f"FAILED: {result}\nABORT: Too many failures. Report what you know."
                })})
                flight_log("abort_exhausted", {"tool": name}, result[:100], True)
                ctx.consecutive_errors += 1
                continue

            # Non-retryable — fatal error
            if not is_retryable:
                print(f"  [FATAL] non-retryable ({error_class})")
                ctx.messages.append({"role": "tool", "content": json.dumps({
                    "name": name,
                    "result": RECOVERY_MODE_MESSAGES["ABORT"].format(error=result[:200])
                })})
                ctx.consecutive_errors += 1
                continue

            # FALLBACK tier — shell command fails → try read_file
            if name == "shell":
                fallback = try_shell_fallback(args.get("command", ""))
                if fallback is not None:
                    fb_result, fb_error = fallback
                    if not fb_error:
                        flight_log("read_file", {"path": "fallback"}, fb_result[:200], False)
                        autonomy_record_call(is_error=False)
                        ctx.consecutive_errors = 0
                        ctx.has_tool_results = True
                        ctx.tools_used.add("read_file")
                        ctx.progress.record_success(name, args, fb_result)
                        ctx.messages.append({"role": "tool", "content": json.dumps({
                            "name": name,
                            "result": f"[fallback: read file]\n{verify_tool_output(fb_result)}"
                        })})
                        continue

            # Tool cooldown — don't offer this tool for 2 turns
            ctx.apply_cooldown(name, 2)
            ctx.consecutive_errors += 1

            # RETRY tier — leashed reflection on NUC3 (max 2 per task)
            if ctx.reflections_used < ctx.max_reflections:
                ctx.reflections_used += 1
                print(f"  [reflect {ctx.reflections_used}/{ctx.max_reflections}] {recovery_mode} ({error_class})...", end="", flush=True)
                t0 = time.time()
                reflection = reflect_on_failure(name, args, result, ctx.goal, recovery_mode)
                print(f" {time.time() - t0:.1f}s")
                print(f"  [reflect] {reflection[:120]}")

                # Reflector says abort → clean exit
                first_word = reflection.strip().split()[0].upper() if reflection.strip() else ""
                if first_word in ("ABORT:", "ABORT."):
                    ctx.messages.append({"role": "tool", "content": json.dumps({
                        "name": name, "result": f"FAILED: {result}\n{reflection}"
                    })})
                    ctx.terminal_state = TERMINAL_TOOL_BLOCKED
                    ctx.result = f"Task aborted: {reflection}"
                    return "DONE"

                # Recovery-mode message + reflection
                fail_msg = RECOVERY_MODE_MESSAGES.get(recovery_mode, "FAILED: {error}").format(error=result[:200])
                fail_msg += f"\nREFLECTION: {reflection}"
            else:
                # Reflection budget exhausted — no more LLM calls for recovery
                print(f"  [reflect] budget exhausted ({ctx.max_reflections} used) — raw error only")
                fail_msg = RECOVERY_MODE_MESSAGES.get(recovery_mode, "FAILED: {error}").format(error=result[:200])

            if fail_warn:
                fail_msg += f"\n{fail_warn}"
            ctx.messages.append({"role": "tool", "content": json.dumps({
                "name": name, "result": fail_msg
            })})
        else:
            # Success
            record_tool_success(name)
            flight_log(name, args, result, False)
            autonomy_record_call(is_error=False)
            ctx.consecutive_errors = 0
            ctx.has_tool_results = True
            ctx.tools_used.add(name)
            ctx.progress.record_success(name, args, result)
            # Auto-record significant results as episodic observations
            if len(result) > 30 and name not in ("scratchpad", "recall"):
                episodic_write(f"{name}: {result[:200]}", source=name)
            ctx.messages.append({"role": "tool", "content": json.dumps({
                "name": name, "result": verify_tool_output(result)
            })})

    # Circuit breaker
    if ctx.consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
        print(f"  [CIRCUIT BREAKER] {ctx.consecutive_errors} consecutive errors")
        ctx.terminal_state = TERMINAL_TOOL_BLOCKED
        ctx.result = gather_partial_results(ctx.messages)
        return "DONE"

    # Stuckness check
    if ctx.progress.is_stuck():
        print(f"  [STALLED] score={ctx.progress.stuckness_score:.1f}")
        flight_log("stalled", {}, f"score={ctx.progress.stuckness_score}", True)
        ctx.terminal_state = TERMINAL_STALLED
        ctx.result = gather_partial_results(ctx.messages)
        return "DONE"

    return "SELECT_TOOL"


def _trim_to_last_sentence(text: str) -> str:
    """Trim text to the last complete sentence. If no sentence boundary found,
    trim to last word boundary and add ellipsis."""
    # Already ends cleanly
    if re.search(r'[.!?)\]"\']\s*$', text):
        return text

    # Find last real sentence boundary: period/!/?  followed by space,
    # but NOT a bare number+dot (like "3.") which is a list marker.
    last = -1
    for m in re.finditer(r'(?<!\d)[.!?]\s', text):
        last = m.start() + 1  # include the punctuation
    if last > len(text) * 0.5:  # only trim if we keep at least half
        return text[:last].rstrip()

    # No good sentence boundary — trim to last word boundary + ellipsis
    trimmed = text.rsplit(None, 1)[0] if ' ' in text else text
    return trimmed.rstrip('.,;:-') + "..."


def _sm_synthesize(ctx: TaskContext) -> str:
    """
    Generate final answer with claim-level verification (v0.9.0).
    Decomposes synthesis into atomic claims, verifies each against
    web + memory evidence. 2 LLM calls (extract + verify), not 3.
    Falls back to legacy grounding if claim extraction fails.
    Terminal state set based on verification results.
    """
    clean = re.sub(r'<think>.*?</think>', '', ctx.pending_synthesis, flags=re.DOTALL).strip()
    if not clean:
        ctx.result = "(empty response)"
        ctx.terminal_state = TERMINAL_ANSWER
        return "DONE"

    # ── Graceful truncation handling ──
    # If we already continued once and it's still ragged, trim cleanly
    if ctx._continued:
        trimmed = _trim_to_last_sentence(clean)
        if trimmed != clean:
            print(f"  [trim] cleaned truncated response ({len(clean)} → {len(trimmed)} chars)")
            clean = trimmed

    # ── Claim Decomposition (replaces grounding + contradiction) ──
    verdict_info = None
    if needs_grounding(clean, ctx.has_tool_results, ctx.tools_used):
        print(f"  [verify] extracting claims...")
        claims = extract_claims(clean)
        if claims:
            print(f"  [verify] {len(claims)} claims: {[c[:40] for c in claims]}")
            clean, verdict_info = verify_claims(claims, clean, ctx.goal, ctx.has_tool_results)
        else:
            # No claims extracted — fall back to legacy grounding
            print(f"  [verify] no claims found — legacy grounding")
            clean = grounding_check(clean, ctx.goal)

    ctx.result = clean

    # ── Terminal state selection based on verification ──
    if verdict_info and verdict_info["contradicted"] > 0:
        ctx.terminal_state = TERMINAL_MEMORY_CONFLICT
    elif verdict_info and verdict_info["unsupported"] > verdict_info["total"] // 2:
        ctx.terminal_state = TERMINAL_INSUFFICIENT
    else:
        ctx.terminal_state = TERMINAL_ANSWER
    return "DONE"


# State dispatch table — deterministic, no open-ended loops
_STATE_HANDLERS = {
    "INIT":         _sm_init,
    "SELECT_TOOL":  _sm_select_tool,
    "CRITIC_CHECK": _sm_critic_check,
    "EXECUTE":      _sm_execute,
    "SYNTHESIZE":   _sm_synthesize,
}


def run_agent(user_message: str, history: list = None, on_token=None) -> str:
    """
    State machine agent loop (v0.9.0).

    States: INIT → SELECT_TOOL ↔ (CRITIC_CHECK → EXECUTE) → SYNTHESIZE → DONE

    Each state is a function that takes TaskContext, mutates it, returns next state.
    The dispatcher just loops. No open-ended reasoning between states.
    Tool discipline: cooldowns after failure, filtered tool lists.
    Terminal states: ANSWER, INSUFFICIENT_EVIDENCE, TOOL_FAILURE_BLOCKING, STALLED.
    """
    ctx = TaskContext(user_message, history=history)
    ctx.on_token = on_token
    state = "INIT"

    while state != "DONE":
        handler = _STATE_HANDLERS.get(state)
        if handler is None:
            ctx.result = f"Internal error: unknown state '{state}'"
            break
        state = handler(ctx)

    # Log terminal state to flight recorder
    if ctx.terminal_state:
        flight_log("terminal_state", {"state": ctx.terminal_state, "task_id": ctx.task_id},
                   ctx.result[:200], ctx.terminal_state != TERMINAL_ANSWER)
        if ctx.terminal_state != TERMINAL_ANSWER:
            print(f"  [terminal] {ctx.terminal_state}")

    return ctx.result

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
    ║  🦀 SmolClaw v0.9.0                      ║
    ║  SmolLM3-3B · State Machine Cluster     ║
    ║  $75 yard sale hardware · AI for all    ║
    ╚═══════════════════════════════════════════╝
    """)

    # Health check — all 3 NUCs
    nodes = [
        ("NUC1/Actor  (tailscale)",    "http://100.126.137.93:8090",  True),
        ("NUC2/Critic (tailscale)", "http://100.104.164.38:8090",   False),
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
