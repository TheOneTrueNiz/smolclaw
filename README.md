![Gemini_Generated_Image_o5m8d0o5m8d0o5m8](https://github.com/user-attachments/assets/5d1f9878-3aa4-4220-a92a-8cba98b23522)

# SmolClaw

**An autonomous AI agent running on $75 of yard sale hardware.**

SmolClaw is a custom-built agentic harness that proves real AI autonomy doesn't require cloud GPUs, paid APIs, or expensive hardware. It runs entirely on three Intel NUCs bought at yard sales, using an open-weight 3B parameter model, and achieves a 46/48 (95.8%) pass rate on its expanded multi-tier curriculum.

```
    ╔═══════════════════════════════════════════╗
    ║  🦀 SmolClaw v0.9.1                      ║
    ║  SmolLM3-3B · State Machine Cluster     ║
    ║  $75 yard sale hardware · AI for all    ║
    ╚═══════════════════════════════════════════╝
```

---

## Why SmolClaw Exists

Every major AI agent framework assumes cloud inference, beefy GPUs, or API keys with a credit card attached. SmolClaw exists to prove that's not necessary.

Three old Intel NUCs. A 3-billion parameter model. Zero cloud dependencies. The entire system — inference, critic, memory, decomposition, reflection, claim verification — runs locally on hardware that cost less than a month of any API subscription.

If you have a computer, you can have an AI agent. No excuses.

---

## Hardware

| Node | Role | Specs | IP |
|------|------|-------|----|
| **nizbot1** | Actor — state machine dispatcher, tool execution, web UI | Intel NUC i5-5250U, 4 cores @ 1.60GHz, 16GB RAM | 10.0.0.1 |
| **nizbot2** | Critic — safety, grounding, contradiction detection | Intel NUC i5-5250U, 4 cores @ 1.60GHz, 16GB RAM | 10.0.0.2 |
| **nizbot3** | Memory — smart recall, failure analysis, reflection | Intel NUC i5-5250U, 4 cores @ 1.60GHz, 16GB RAM | 10.0.0.3 |

- **OS:** Pop!_OS 24.04 on all three nodes (headless)
- **Network:** Direct ethernet between NUCs (sub-ms latency) + Tailscale for remote access
- **Total cost:** ~$75 at yard sales
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

Served via **llama.cpp** (`llama-server`) with `--jinja` for native SmolLM3 chat template support, `--flash-attn`, and KV cache quantization (`q8_0`/`q4_0`). Each NUC runs its own instance as a systemd user service with `--threads 4 --parallel 1`.

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
  │  AoT Decompose    │    │  Direct Agent Loop  │
  │  (NUC2)           │    │                     │
  │  → atomic tasks   │    │                     │
  └─────────┬─────────┘    └──────────┬──────────┘
            │                          │
            ▼ per atom                 │
┌───────────────────────────────────────────────────────────┐
│            STATE MACHINE AGENT LOOP (NUC1)                │
│                                                           │
│  INIT → SELECT_TOOL ↔ (CRITIC_CHECK → EXECUTE)           │
│                         → SYNTHESIZE → DONE               │
│                                                           │
│  Fast paths (skip LLM entirely):                          │
│    Greeting regex → instant static response               │
│    Direct-dispatch → recall/time/math/remember            │
│                                                           │
│  Terminal states:                                         │
│    ANSWER · INSUFFICIENT_EVIDENCE                         │
│    TOOL_FAILURE_BLOCKING · STALLED                        │
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
│                  │ FALLBACK     │                         │
│                  │ RETRY        │  ← reflector on NUC2    │
│                  │ DEGRADE      │  ← partial results      │
│                  │ ESCALATE     │  ← tell user            │
│                  └──────────────┘                         │
│                                                           │
│  ┌───────────────────────────────────────────────┐        │
│  │  Memory Layer (NUC3)                          │        │
│  │  Smart recall · Episodic memory · Reflection  │        │
│  │  Memory verify gate (COMMIT/REJECT)           │        │
│  └───────────────────────────────────────────────┘        │
└───────────────────────────────────────────────────────────┘
                            │
              ┌─────────────▼─────────────┐
              │   Synthesis (tools-free)  │
              │   Complexity-gated budget │
              └───────────────────────────┘
```

### Key Components

**State Machine Dispatcher** (v0.9.0) — Deterministic state machine replaces the v0.6 while-loop. States: INIT → SELECT_TOOL ↔ (CRITIC_CHECK → EXECUTE) → SYNTHESIZE → DONE. Terminal states provide structured exit conditions.

**Direct-Dispatch** (v0.9.1) — For high-confidence intents (recall, remember, time, math), regex detection skips LLM tool selection entirely and executes the tool directly. The LLM only handles synthesis. Eliminates 20-40s of unreliable 3B model deliberation.

**Greeting Fast Path** (v0.9.1) — Simple greetings return instant static responses without any LLM call (0.0s vs 40s+).

**Compressed System Prompt** (v0.9.1) — 811 → 279 tokens (66% reduction). Preserves all critical behavioral rules in tighter language. Directly reduces prefill time on every query.

**Parallel Critic** (`critic_check_parallel`) — Every non-whitelisted tool call is validated by the critic running on NUC2 before execution. Safe operations (read_file, recall, basic shell commands) skip the critic entirely (0.0s).

**Memory Layer** (NUC3) — Dedicated memory node handles smart recall, episodic memory, reflection on failures, and memory verification gate (COMMIT/REJECT verdict before persisting).

**Claim Verification** (v0.9.0) — Claims requiring current data are decomposed and verified against tool output. Grounding thresholds tuned to skip verification for self-referential, creative, and tool-backed responses.

**Atom of Thoughts** (`aot_decompose`, `run_agent_aot`) — Complex multi-step queries are decomposed into independent atomic sub-tasks on NUC2. Each atom gets a fresh context (Markov property — no history bloat). Simple queries bypass decomposition via regex heuristic.

**4-Tier Error Recovery** — When a tool call fails:
1. **FALLBACK** — Programmatic retry with a different tool. No LLM call.
2. **RETRY** — Reflector on NUC2 suggests a concrete alternative command.
3. **DEGRADE** — Circuit breaker returns whatever partial results succeeded.
4. **ESCALATE** — Terminal output tells the user what went wrong.

**Argument Aliasing** (`_ARG_ALIASES`) — Normalizes variant argument keys from the 3B model (e.g., `cmd` → `command`, `file_path` → `path`) before tool execution, improving robustness.

**Complexity-Gated Budgets** — Synthesis token budget adapts to query complexity: simple (96 tokens) → normal (220) → complex (384). Prevents wasting decode time on padding for simple answers.

**Few-Shot Library** — Dynamic injection of 1-2 relevant tool call examples into the system prompt when non-obvious tools are in the filtered set.

**Persistent HTTP Connections** — TCP connection reuse to NUC1/2/3 with keep-alive and automatic fallback on stale connections.

---

## Tools

SmolClaw has 8 tools, injected into the prompt via the llama.cpp `/v1/chat/completions` API:

| Tool | Description | Critic |
|------|-------------|--------|
| `shell` | Execute shell commands. 120s timeout. Blocked commands list. | Whitelisted for safe commands (ls, grep, df, etc.) |
| `read_file` | Read a file. Auto-stashes large files to scratchpad. | Whitelisted (read-only) |
| `write_file` | Write content to a file. Creates parent dirs. Critical file blocklist. | Requires critic approval |
| `remember` | Save a note to persistent memory. Verified via NUC3 memory gate. | Whitelisted |
| `recall` | Smart retrieval from long-term memory via NUC3. | Whitelisted |
| `scratchpad` | Retrieve auto-stashed large outputs by name. | Whitelisted |
| `web_search` | Search the web using Brave Search API. | Whitelisted |
| `calculate` | Evaluate math expressions safely. Unit stripping for natural language. | Whitelisted |

### Blocked Shell Commands

```
sudo, rm -rf /, mkfs, dd if=, > /dev/, chmod 777,
curl | sh, wget | sh, fork bomb, passwd, > /etc/,
shutdown, reboot, init 0
```

### Critical File Protection

The 3B model cannot overwrite its own source files. These are blocked at the tool argument validator level, before the critic even sees the call:

```
agent.py, agent_hackbook.py, test_harness.py, web_ui.py, doctor_claude.py
```

---

## Performance

Measured on the 3-NUC cluster with SmolLM3-3B Q4_K_M, `--threads 4`, `--flash-attn`, KV cache `q8_0`/`q4_0`:

### Cluster Throughput

| Node | Role | tok/s |
|------|------|-------|
| NUC1 | Actor | 4.2-7.7 |
| NUC2 | Critic | 7.0 |
| NUC3 | Memory | 7.1 |
| **Cluster avg** | | **6.1** |

### End-to-End Response Times (at Web UI)

| Category | v0.9.0 (before) | v0.9.1 (after) | Improvement |
|----------|-----------------|-----------------|-------------|
| **Simple avg** | 60-130s | **9.1s** | 7-14x |
| Simple min | ~3s | **0.0s** (instant) | --- |
| Simple max | ~130s | 30.4s | 4x |
| **Complex avg** | 80-150s | **50.8s** | 2-3x |
| Complex min | ~50s | 33.1s | 1.5x |
| Complex max | ~180s+ | 64.5s | 3x |
| **Overall avg** | ~90-120s | **30.0s** | **3-4x** |

### What Drives the Speed

- **Direct-dispatch** — regex-detected intents (recall, time, math) skip LLM tool selection entirely
- **Greeting fast path** — static responses for greetings, thanks, goodbyes (0.0s)
- **66% smaller system prompt** — 811 → 279 tokens, directly reduces prefill time
- **Grounding threshold tuning** — skip unnecessary NUC2 verification round-trips
- **Complexity-gated budgets** — simple queries get 96-token synthesis cap
- **Dynamic tool filtering** — only 2-4 relevant tools injected per query (saves ~300 prompt tokens)
- **Persistent HTTP connections** — TCP reuse to all NUCs
- **Critic whitelist** — safe read-only ops skip critic entirely (saves 4-10s per call)
- **Tools-free synthesis** — tool definitions omitted on final answer turn
- **SSE streaming** — synthesis tokens stream to web UI in real-time
- **Broadwell-optimized build** — llama.cpp with AVX2+FMA+F16C, flash-attn, KV cache quantization
- **CPU governor** — `performance` mode + `nice -10` priority for llama-server

### Benchmarks Validated

- **Thread count:** Tested 1, 2, 3, 4 threads. 4 threads optimal on i5-5250U (7.48 tok/s avg, +3% over 2 threads).
- **KV cache k-quantization:** Tested q8_0 vs q4_0. No speed difference (-0.5%). Keeping q8_0 for precision.
- **GBNF grammar-constrained decoding:** Tested for structured tool call output. 3x slower on NUC hardware due to FSM overhead. Infrastructure preserved, disabled in hot path.

---

## Curriculum

48 lessons across 8 modules:

| Module | Lessons | What It Tests |
|--------|---------|---------------|
| Identity | 4 | Name, hardware, personality, version awareness |
| Core Tools | 7 | Tool fluency — shell, read_file, write_file, remember, recall, calculate |
| Tool Selection | 6 | Picking the right tool for the job, avoiding wrong tools |
| Multi-Tool Chains | 5 | Linking tools: system report, grep+summarize, write+verify |
| Error Handling | 5 | Missing files, bad commands, safety blocks (sudo, rm -rf) |
| Grounding | 5 | Current events need search, local facts from code, no fabrication |
| Edge Cases & Nuance | 8 | Boundary conditions, ambiguous queries, refusal scenarios |
| Response Quality | 6 | Conciseness, personality, accuracy, no raw JSON leakage |

**Latest result: 46/48 passed (95.8%)**

The 2 remaining failures are stochastic 3B model edge cases (identity name confusion with hostname, empty write_file content), not systematic bugs.

---

## Project Structure

```
smolclaw/
├── agent.py              # The agent (LAN IPs — runs on NUC1)
├── agent_hackbook.py     # The agent (Tailscale IPs — for remote access)
├── web_ui.py             # Web chat interface (stdlib HTTP server, SSE streaming)
├── test_harness.py       # Legacy 26-scenario test suite
├── flight_analysis.py    # Flight recorder analysis tool
├── curricula/            # 48-lesson curriculum (JSON, 8 modules)
│   └── protocol.md       # Curriculum test protocol and format spec
├── curriculum_progress.json  # Run history and results
├── cluster_setup/        # Setup scripts for the 3-NUC cluster
├── memory.md             # Persistent long-term memory
├── secrets.env.example   # API key setup instructions (no real keys)
├── CLAUDE.md             # Claude Code instructions for development
├── ROADMAP.md            # Version history and future plans
├── QUICKSTART.md         # Setup guide
└── VERA_TECHNIQUES.md    # Applicable techniques from Vera 2.0
```

### Dependencies

**None.** The agent uses only Python standard library (`json`, `subprocess`, `http.client`, `re`, `pathlib`, `concurrent.futures`, `datetime`, `threading`). No pip install. No virtualenv. No requirements.txt.

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
| Grammar-constrained decoding | [GBNF, llama.cpp](https://github.com/ggml-org/llama.cpp/blob/master/grammars/README.md) | Infrastructure for forced-valid JSON output (disabled — too slow on NUC) |
| First-person prompting | Vera 2.0 genome | "My name is SmolClaw" identity framing |
| Recovery-first kernel | Vera 2.0 autonomy orchestrator | Budget/failure gates |
| Flight recorder | Vera 2.0 transition logger | JSONL audit trail |

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
| v0.6.0 | 2026-03 | 4-tier error recovery, shell preprocessor, JSON repair, 17/17 = 100% |
| v0.7.0 | 2026-03 | 3rd NUC (nizbot3) as dedicated memory node, web search (Brave API) |
| v0.8.0 | 2026-03 | Claim verification, safety discipline (sudo/rm blocking), 26 scenarios |
| v0.8.1 | 2026-03 | Anti-mantra, stuckness scoring, failure discipline state machine |
| v0.9.0 | 2026-03 | State machine dispatcher, structured I/O, smart recall, episodic memory, 26/26 = 100% |
| **v0.9.1** | **2026-03** | **System prompt compression (66%), direct-dispatch, greeting fast path, calculate tool, expanded curriculum (48 lessons), argument aliasing, GBNF infrastructure. Overall latency 90s → 30s avg. Curriculum 46/48 (95.8%).** |

---

## Known 3B Model Limitations

These are inherent to running a 3-billion parameter model. SmolClaw mitigates each one:

| Limitation | Mitigation |
|------------|------------|
| Hallucinated shell flags (`df --i-sync`) | Shell command preprocessor strips bad flags |
| Unquoted glob patterns (`"*.py"` breaks JSON) | JSON repair function |
| Narrates plans instead of acting | Direct-dispatch for known intents + 160 token budget |
| Ignores tool hints ~50% of the time | Direct-dispatch bypasses LLM entirely for high-confidence intents |
| Shell-escaped `$` in awk (`\$2`) | JSON repair converts to `$2` |
| `python` instead of `python3` | Preprocessor auto-corrects |
| `ping` without `-c` (hangs forever) | Preprocessor adds `-c 3` |
| Confuses hostname with own name | "My name is SmolClaw" at prompt start for salience |
| Natural language math ("15 times 23") | Direct-dispatch converts to Python operators before calculate |
| Overwrites own source code via write_file | Hardcoded blocklist in tool argument validator |

---

## What's Next

See [ROADMAP.md](ROADMAP.md) for full details.

**Explored and validated (not yet beneficial on NUC hardware):**
- GBNF grammar-constrained decoding — infrastructure in place, 3x slower due to FSM overhead
- KV cache k-quantization (q4_0 vs q8_0) — no speed difference, keeping q8_0

**Next up:**
- Context compression (ACON approach) for tool output before re-injection
- Speculative decoding with draft model
- Web UI overhaul with cluster health monitoring
- Fine-tuning SmolLM3 on its own successful traces (GRPO)

---

## License

SmolClaw is a personal project by [@TheOneTrueNiz](https://github.com/TheOneTrueNiz).

The model (SmolLM3-3B) is Apache 2.0 licensed by HuggingFace.

---

*$75 hardware. Real AI. No excuses.*
