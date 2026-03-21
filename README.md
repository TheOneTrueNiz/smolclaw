
![Gemini_Generated_Image_o5m8d0o5m8d0o5m8](https://github.com/user-attachments/assets/5d1f9878-3aa4-4220-a92a-8cba98b23522)

# SmolClaw

**An autonomous AI agent running on $50 of yard sale hardware.**

SmolClaw is a custom-built agentic harness that proves real AI autonomy doesn't require cloud GPUs, paid APIs, or expensive hardware. It runs entirely on two Intel NUCs bought at yard sales, using an open-weight 3B parameter model, and achieves a 17/17 (100%) pass rate on its own multi-tier test suite.

```
    ╔═══════════════════════════════════════╗
    ║  🦀 SmolClaw v0.6.0                  ║
    ║  SmolLM3-3B · Dual NUC · 4-Tier     ║
    ║  $50 yard sale hardware · AI for all ║
    ╚═══════════════════════════════════════╝
```

---

## Why SmolClaw Exists

Every major AI agent framework assumes cloud inference, beefy GPUs, or API keys with a credit card attached. SmolClaw exists to prove that's not necessary.

Two old Intel NUCs. A 3-billion parameter model. Zero cloud dependencies. The entire system — inference, critic, decomposition, reflection, memory — runs locally on hardware that cost less than a month of any API subscription.

If you have a computer, you can have an AI agent. No excuses.

---

## Hardware

| Node | Role | Specs | IP |
|------|------|-------|----|
| **nizbot1** | Agent + Inference | Intel NUC i5-5250U, 4 cores @ 1.60GHz, 16GB RAM | 10.0.0.1 |
| **nizbot2** | Critic + Offload | Intel NUC i5-5250U, 4 cores @ 1.60GHz, 16GB RAM | 10.0.0.2 |

- **OS:** Pop!_OS 24.04 on both nodes
- **Network:** Direct ethernet cable between NUCs (sub-ms latency)
- **Total cost:** ~$50 at yard sales
- **Power draw:** ~15W per NUC under load

---

## Model

**SmolLM3-3B** (by HuggingFace)

| Property | Value |
|----------|-------|
| Parameters | 3 billion |
| Quantization | Q4_K_M (1.9GB on disk) |
| Tool calling accuracy | 92.3% (BFCL benchmark) |
| Context window | 8,192 tokens |
| License | Apache 2.0 |
| Think toggle | `/no_think` disables CoT to save tokens |

Served via **llama.cpp** (`llama-server`) with `--jinja` for native SmolLM3 chat template support. Each NUC runs its own instance as a systemd user service.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                         USER QUERY                          │
└───────────────────────────┬─────────────────────────────────┘
                            │
                ┌───────────▼───────────┐
                │    AoT Heuristic      │  Is this query complex?
                │   (regex, no LLM)     │  Simple → skip decompose
                └───────────┬───────────┘
                            │
            ┌───────── complex ────────┐
            │                          │
  ┌─────────▼─────────┐    ┌──────────▼──────────┐
  │  AoT Decompose    │    │   Direct Agent      │
  │  (NUC2, 5.1s)     │    │   Loop (NUC1)       │
  │  → atomic tasks   │    │                     │
  └─────────┬─────────┘    └──────────┬──────────┘
            │                          │
            ▼ per atom                 │
┌───────────────────────────────────────────────────────────┐
│                     AGENT LOOP (NUC1)                     │
│                                                           │
│  ┌─────────┐     ┌──────────────┐     ┌───────────────┐  │
│  │  Model   │────▶│  Tool Call   │────▶│  Critic       │  │
│  │  Propose │     │  Parser      │     │  (NUC2)       │  │
│  │  Action  │     │  + JSON      │     │  SAFE/BLOCK   │  │
│  └─────────┘     │  Repair      │     └───────┬───────┘  │
│                   └──────────────┘             │          │
│                         ▲              ┌──────▼───────┐  │
│                         │              │  Execute     │  │
│                         │              │  Tool        │  │
│                         │              └──────┬───────┘  │
│                         │                     │          │
│                  ┌──────┴───────┐      ┌──────▼───────┐  │
│                  │  4-Tier      │◀─────│  Verify      │  │
│                  │  Recovery    │ fail │  Output      │  │
│                  │              │      └──────────────┘  │
│                  │ RETRY        │                         │
│                  │ FALLBACK     │  ← programmatic, no LLM │
│                  │ DEGRADE      │  ← partial results      │
│                  │ ESCALATE     │  ← tell user            │
│                  └──────────────┘                         │
└───────────────────────────────────────────────────────────┘
                            │
              ┌─────────────▼─────────────┐
              │   Synthesis (tools-free)  │
              │   MAX_TOKENS = 150        │
              └───────────────────────────┘
```

### Key Components

**Agent Loop** (`run_agent`) — The core loop on NUC1. Proposes tool calls, executes them, handles errors, and synthesizes a final answer. Capped at 10 turns with a circuit breaker after 3 consecutive errors.

**Parallel Critic** (`critic_check_parallel`) — Every non-whitelisted tool call is validated by the critic running on NUC2 before execution. Safe operations (read_file, recall, basic shell commands) skip the critic entirely (0.0s).

**Atom of Thoughts** (`aot_decompose`, `run_agent_aot`) — Complex multi-step queries are decomposed into independent atomic sub-tasks on NUC2. Each atom gets a fresh context (Markov property — no history bloat). Results are synthesized at the end. Simple queries bypass decomposition via regex heuristic (saves 5-42s).

**4-Tier Error Recovery** — When a tool call fails:
1. **FALLBACK** — Programmatic retry with a different tool (e.g., failed `grep` → `read_file`). No LLM call.
2. **RETRY** — Reflector on NUC2 suggests a concrete alternative command.
3. **DEGRADE** — Circuit breaker returns whatever partial results succeeded.
4. **ESCALATE** — Terminal output tells the user what went wrong.

**Shell Preprocessor** (`preprocess_shell_cmd`) — Catches and fixes known 3B model hallucination patterns before the shell command runs:
- `df --i-sync` → strips hallucinated flags
- `python` → `python3`
- `ping` without `-c` → adds `-c 3` to prevent infinite hang

**JSON Repair** (`repair_json_str`) — Fixes malformed tool call JSON that the 3B model sometimes generates:
- Nested double quotes in glob patterns: `"*.py"` → `'*.py'`
- Shell-escaped dollar signs in awk: `\$2` → `$2`

**Tool Output Verifier** (`verify_tool_output`) — Scans tool output for prompt injection patterns before feeding results back to the 3B model. Detects phrases like "ignore previous instructions", "you are now", etc.

**Autonomy Kernel** (`autonomy_check`) — Recovery-first decision engine. Before any action, checks:
- Quiet hours (2am-7am CDT) — proceeds but logs
- Daily call budget (200 calls/day)
- Daily token budget (100K tokens/day)
- Failure cluster detection (5+ consecutive failures → defer)

**Scratchpad** (`scratchpad_stash`, `scratchpad_read`) — Large tool outputs (>1500 chars) are auto-saved to files instead of being truncated. The model receives a preview + instructions to read the full content via the `scratchpad` tool.

**Flight Recorder** (`flight_log`) — Every tool call is logged to `flight_recorder.jsonl` with timestamp, tool name, arguments, success/failure, error class, and a result preview. Analyzed with `flight_analysis.py`.

**Per-Tool Failure Tracking** — Runtime counter per tool. After 3 failures on the same tool in a session, the model is warned to try a different approach.

**Failure Classifier** (`classify_failure`) — Categorizes errors as retryable (timeout, connection error, missing resource) or non-retryable (auth, permission denied, rate limit). Non-retryable errors skip the reflector and abort immediately.

---

## Tools

SmolClaw has 6 tools, injected into the prompt via the llama.cpp `/v1/chat/completions` API:

| Tool | Description | Critic |
|------|-------------|--------|
| `shell` | Execute shell commands. 120s timeout. Blocked commands list. | Whitelisted for safe commands (ls, grep, df, etc.) |
| `read_file` | Read a file. Auto-stashes large files to scratchpad. | Whitelisted (read-only) |
| `write_file` | Write content to a file. Creates parent dirs. | Requires critic approval |
| `remember` | Append a note to persistent `memory.md`. | Requires critic approval |
| `recall` | Read all entries from `memory.md`. | Whitelisted |
| `scratchpad` | Retrieve auto-stashed large outputs by name. | Whitelisted |

### Blocked Shell Commands

```
sudo, rm -rf /, mkfs, dd if=, > /dev/, chmod 777,
curl | sh, wget | sh, fork bomb, passwd, > /etc/,
shutdown, reboot, init 0
```

---

## Performance

Measured on the dual-NUC cluster with SmolLM3-3B Q4_K_M:

| Metric | Value |
|--------|-------|
| Prefill (NUC1, prompt cached) | 10-15 tokens/s |
| Decode (NUC1, 1 slot) | 2.5-4.3 tokens/s |
| Decode (NUC2, 1 slot) | 9.3 tokens/s |
| AoT decompose (NUC2) | 5.1s |
| Critic — whitelisted ops | 0.0s (skipped) |
| Critic — non-whitelisted | 4-10s |
| Typical simple query end-to-end | 60-120s |
| Typical multi-step query end-to-end | 120-300s |
| RAM usage per NUC | ~1-2GB (model + KV cache) |

### Latency Optimizations Applied

- **AoT heuristic bypass** — regex detects simple queries, skips decomposition (saves 5-42s)
- **Stop sequences** — critic, decompose, reflect all stop at `\n` instead of generating padding (saves 2-35s)
- **NUC2 offload** — decompose + reflect run on NUC2 at 9.3 t/s instead of NUC1 at 2.5 t/s (75-85% faster)
- **Critic whitelist** — safe read-only ops skip the critic entirely (saves 4-10s per call)
- **Single slot** — `--parallel 1` on NUC1 eliminates KV cache splitting overhead
- **Tools-free synthesis** — tool definitions (~405 tokens) omitted on the final answer turn
- **Token budgets** — tool calls: 160, critic: 32, reflect: 64, synthesis: 150

---

## Test Suite

Adapted from the **Doctor_Professor** CI gate pattern (Vera 2.0). 17 scenarios across 5 tiers:

| Tier | Name | Scenarios | What It Tests |
|------|------|-----------|---------------|
| 1 | Tool Fluency | 6 | Does SmolClaw pick the right tool? (shell, read_file, write_file, remember, recall) |
| 2 | Chain Tasks | 4 | Can it link multiple tools? (system report, grep+summarize, write+verify, find+count) |
| 3 | Self-Introspection | 2 | Can it reason about its own source code? (version, config values) |
| 4 | System Diagnostics | 3 | Real-world ops: top processes, network probe, large output handling |
| 5 | Error Recovery | 2 | Missing file, nonexistent command — does it recover gracefully? |

**Latest result: 17/17 passed (100%)**

```bash
python3 test_harness.py              # run all 17 scenarios
python3 test_harness.py --tier 1     # run only tier 1 (tool fluency)
python3 test_harness.py --scenario 8 # run a specific scenario
```

---

## Project Structure

```
smolclaw/
├── agent.py              # The agent (1095 lines, single file, zero dependencies beyond stdlib)
├── test_harness.py       # 17-scenario test suite
├── flight_analysis.py    # Flight recorder analysis tool
├── memory.md             # Persistent long-term memory
├── flight_recorder.jsonl # JSONL audit trail of every tool call
├── autonomy_state.json   # Daily call/token counters, failure tracking
├── scratchpad/           # Auto-stashed large outputs
├── logs/                 # Daily interaction logs
├── ROADMAP.md            # Version history and future plans
├── VERA_TECHNIQUES.md    # Applicable techniques from Vera 2.0
└── OVERNIGHT_REPORT.md   # Development session report
```

### Dependencies

**None.** The agent uses only Python standard library (`json`, `subprocess`, `urllib`, `re`, `pathlib`, `concurrent.futures`, `datetime`). No pip install. No virtualenv. No requirements.txt.

The only external dependency is `llama-server` from llama.cpp, which serves the model.

---

## Research Foundations

SmolClaw's architecture draws from published research and the Vera 2.0 codebase:

| Technique | Source | How It's Used |
|-----------|--------|---------------|
| Mixture of Agents | [MoA, 2406.04692](https://arxiv.org/abs/2406.04692) | Proposer (agent) + aggregator (critic) architecture |
| Self-correcting recovery | [PALADIN, 2509.25238](https://arxiv.org/abs/2509.25238) | Structured reflection on tool failures |
| Error taxonomy | [CriticT, 2506.13977](https://arxiv.org/abs/2506.13977) | Failure classifier: retryable vs permanent |
| DAG decomposition | [Atom of Thoughts, 2502.12018](https://arxiv.org/abs/2502.12018) | Break complex tasks into independent atoms |
| First-person prompting | Vera 2.0 genome | "I am SmolClaw" identity framing |
| Recovery-first kernel | Vera 2.0 autonomy orchestrator | Budget/failure/quiet-hours gates |
| Flight recorder | Vera 2.0 transition logger | JSONL audit trail |
| Scratchpad workspace | Vera 2.0 MARM notebook | File-based large output storage |

---

## Version History

| Version | Date | Highlights |
|---------|------|------------|
| v0.1 | 2026-03 | Basic agent loop, tool calling, systemd service |
| v0.2 | 2026-03 | Parallel critic, PALADIN reflector, circuit breaker, memory |
| v0.3 | 2026-03 | Dual NUC cluster, /no_think, token budgets, 8K context |
| v0.4 | 2026-03 | Atom of Thoughts, first-person identity, scratchpad, flight recorder |
| v0.5 | 2026-03 | Autonomy kernel, test harness (15/17 = 88%) |
| v0.5.1 | 2026-03 | 7 latency optimizations, 4 bug fixes |
| **v0.6.0** | **2026-03** | **4-tier error recovery, shell preprocessor, JSON repair, 17/17 = 100%** |

---

## Known 3B Model Limitations

These are inherent to running a 3-billion parameter model. SmolClaw mitigates each one:

| Limitation | Mitigation |
|------------|------------|
| Hallucinated shell flags (`df --i-sync`) | Shell command preprocessor strips bad flags |
| Unquoted glob patterns (`"*.py"` breaks JSON) | JSON repair function + shell tip in prompt |
| Narrates plans instead of acting | "Act first, explain after" in system prompt + 160 token budget |
| Shell-escaped `$` in awk (`\$2`) | JSON repair converts to `$2` |
| `python` instead of `python3` | Preprocessor auto-corrects |
| `ping` without `-c` (hangs forever) | Preprocessor adds `-c 3` |
| Reflector says "ABORT" on retryable errors | Strict detection requires punctuation (`ABORT:` or `ABORT.`) |
| Single-word garbage AoT atoms | Atom validation rejects atoms under 3 words |

---

## What's Next

See [ROADMAP.md](ROADMAP.md) for full details.

**Phase 6** (in progress) — Learning & inner life: flight recorder analysis, memory lifecycle management, proactive self-monitoring heartbeat, tool chain scoring.

**Phase 7** — Reach: HTTP API server, messaging bot integration, modular skill system, 7B model split across both NUCs, fine-tuning on SmolClaw's own successful traces.

---

## License

SmolClaw is a personal project by [@TheOneTrueNiz](https://github.com/TheOneTrueNiz).

The model (SmolLM3-3B) is Apache 2.0 licensed by HuggingFace.

---

*$50 hardware. Real AI. No excuses.*
