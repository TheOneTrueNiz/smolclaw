# SmolClaw Roadmap

**Mission:** Prove autonomous AI is accessible to everyone on yard sale hardware.

**Hardware:** 3x Intel NUC i5-5250U (4 cores @ 1.60GHz), 16GB RAM each, Pop OS 24.04
**Brain:** SmolLM3-3B (Q4_K_M, 1.9GB) via llama.cpp
**Cluster:** nizbot1 (10.0.0.1, actor) + nizbot2 (10.0.0.2, critic) + nizbot3 (10.0.0.3, memory), direct ethernet

---

## Completed

### v0.1 — Basic Agent
- Autonomous agent loop with tool calling (shell, files, memory)
- systemd service for inference server

### v0.2 — Parallel Critic & Safety
- Parallel adversarial critic validates tool calls before execution
- Structured reflection recovers from tool failures (PALADIN-inspired)
- Circuit breaker + loop detection prevent spirals
- Persistent memory across sessions

### v0.3 — Dual NUC Cluster + Optimizations
- Networked NUCs via direct ethernet (sub-ms latency)
- NUC2 as dedicated critic node (zero CPU contention)
- Slots 4→2, context 16K→8K (freed 2.5GB RAM, decode 2.5→4.3 t/s)
- /no_think kills wasted thinking tokens
- Smart token budgets: tool 128, critic 32, reflect 64, synthesis 256
- Speculative decoding tested (draft model architecture mismatch — dropped)

### v0.4 — AoT + Vera Patterns + First-Person Identity
- Atom of Thoughts: DAG decomposition for complex multi-part tasks
- First-person prompting ("I am SmolClaw" — Vera genome style)
- Scratchpad: auto-stash large outputs to files, read back on demand (no truncation)
- Failure classifier: retryable vs non-retryable errors (Vera pattern)
- Flight recorder: JSONL log of all tool calls + outcomes
- Leaner prompts: system prompt ~350 tokens (was ~650)

### v0.5 — Hardening & Autonomy
- NUC2 systemd service with linger (survives logout/reboot)
- Autonomy kernel: recovery-first with budget/failure/quiet-hours gates
- Test harness: 17 scenarios, 5 tiers (Doctor_Professor pattern from Vera)
- **Test results: 15/17 passed (88.2%)**

### v0.5.1 — Latency Optimizations
- AoT heuristic bypass: skip decomposition for simple queries (saves 8-42s)
- Stop sequences on all non-agent LLM calls (saves 2-35s per call)
- NUC2 offload: AoT decompose + reflect run on NUC2 (75-85% faster)
- Critic whitelist: safe operations skip critic entirely (0.0s)
- NUC1 --parallel 2→1 (eliminated slot contention)
- Tools-free synthesis turn (saves ~405 prompt tokens)
- Reduced synthesis budget (256→150 tokens)
- Smarter scratchpad auto-stash with tool call example
- Bug fixes: critic false positive, bare JSON fallback, false ABORT detection, aggressive reflector

### v0.6.0 — 4-Tier Error Recovery & Hardening
- 4-tier error recovery: RETRY → FALLBACK → DEGRADE → ESCALATE (from Vera)
- Shell command fallbacks: failed grep/cat/head auto-retries via read_file (no LLM call)
- DEGRADE mode: circuit breaker returns partial results instead of empty failure
- Per-tool failure tracking: warns model after 3 failures on same tool
- Smarter AoT heuristic: file paths with dots no longer trigger false decomposition
- Act-first system prompt: model calls tools immediately, no narration
- Tool count in system prompt: self-introspection works without grepping source
- MAX_TOKENS_TOOL_CALL 128→160 (3B model needs room for narration + tool call)
- **Test results: 17/17 passed (100%)**

### v0.7.0 — 3rd NUC + Web Search
- 3rd NUC (nizbot3) as dedicated memory/retrieval/reflection node
- Web search tool via Brave Search API
- Cluster: 3 nodes, 3 roles (actor, critic, memory)

### v0.8.0 — Claim Verification + Safety Tiers
- Claim verification: decomposes claims and verifies against tool output
- Safety discipline: block sudo/rm, off-topic resistance
- Test harness expanded: 26 scenarios across 8 tiers

### v0.8.1 — Failure Discipline
- Anti-mantra detection (blocks repeated failed approaches)
- Stuckness scoring
- Per-tool cooldowns after failure
- Filtered tool lists based on failure state

### v0.9.0 — State Machine Architecture (current)
- Deterministic state machine replaces while-loop agent
- States: INIT → SELECT_TOOL ↔ (CRITIC_CHECK → EXECUTE) → SYNTHESIZE → DONE
- Terminal states: ANSWER, INSUFFICIENT_EVIDENCE, TOOL_FAILURE_BLOCKING, STALLED
- Structured I/O: all state transitions are explicit and auditable
- Smart recall via NUC3 (surgical retrieval instead of dumping all memory)
- Episodic memory: short-lived observations in `episodic.jsonl`
- Memory verification gate: NUC2 critic COMMIT/REJECT before persisting
- Robust memory verify parser: handles 3B model preamble echoing
- Critical file write protection: hardcoded blocklist prevents self-overwrite
- Daily call budget raised to 1000, token budget to 500K
- **Test results: 26/26 passed (100%)**

### Current Performance (v0.9.0, 3-NUC cluster)

| Metric | Speed |
|--------|-------|
| Prefill (NUC1, cached) | 10-15 t/s |
| Decode (NUC1, 1 slot) | 2.5-4.3 t/s |
| Decode (NUC2, 1 slot) | 9.3 t/s |
| AoT decompose (NUC2) | 5.1s (was 22-42s) |
| Critic (whitelisted) | 0.0s |
| Critic (non-whitelisted) | 4-10s |
| Memory (NUC1) | ~1GB model + KV |
| Memory (NUC2/NUC3) | ~2GB model + KV |
| Tool success rate | 89% |

---

## Next: Web UI & Observability (v0.10)
*Goal: Full control panel for the cluster*

- Web UI overhaul: cluster control panel with health monitoring
- Conversation history with persistent storage
- Observability dashboard: NUC health, budget gauges, flight recorder feed
- Buttons/toggles for cluster control from the browser

---

## Phase 6: Learning & Inner Life
*Goal: SmolClaw improves itself over time*

### 6.1 — Flight recorder analysis
- Parse flight_recorder.jsonl for patterns
- Extract failure→recovery pairs as training examples
- Build few-shot prompt library from successful chains
- **Source:** Vera's `08_failure_to_recovery_dataset.py`

### 6.2 — Memory lifecycle
- Track memory footprint (scratchpad + memory.md + flight log)
- At 85% budget: summarize, compress, seal old entries
- At 100%: stop ingest, archive, emit pressure alert
- **Source:** Vera's `04_memory_lifecycle_controller.py`

### 6.3 — Proactive heartbeat
- Cron job sends periodic "check yourself" prompt
- SmolClaw reviews: disk space, stale scratchpad, flight log anomalies
- Can proactively clean up, remember patterns, optimize itself
- Paper: Proactive Agents (2501.00383)

### 6.4 — Tool scoring (ForeAgent lite)
- Score tool chains by historical success rate, timeout rate, latency
- Prefer chains with proven reliability
- Skip chains with high failure rate
- **Source:** Vera's `05_foreagent_simulator_stub.py`

---

## Phase 7: Reach (v1.0)
*Goal: SmolClaw becomes useful to humans beyond the terminal*

### 7.1 — API server
- Simple HTTP API: POST /ask → JSON response
- WebSocket for streaming
- Accessible from any device on the LAN

### 7.2 — Channel integration
- Telegram bot, Discord bot, or Matrix — pick one lean channel
- SmolClaw reachable from phone
- Message queue for async responses

### 7.3 — Skill system
- Modular tool definitions loaded on demand
- Don't inject all tools every call — match by query
- Reduces prompt size, improves tool-call accuracy

### 7.4 — 7B model via --rpc split
- Split Qwen2.5-7B or similar across all 3 NUCs
- Significantly smarter brain, ~0.3ms network overhead per token
- Quality jump from 3B→7B is substantial for complex reasoning

### 7.5 — Fine-tuning
- Collect SmolClaw's successful tool traces from flight recorder
- Fine-tune SmolLM3 on its own interactions (QLoRA)
- A model literally optimized for this exact machine and use case

---

## Key Research Papers (from /media/nizbot1/F040-0608/Research_Repo/)

| Paper | Use | Status |
|-------|-----|--------|
| MoA (2406.04692) | Proposer + aggregator architecture | Implemented (critic) |
| PALADIN (2509.25238) | Self-correcting tool failure recovery | Implemented (reflector) |
| CriticT (2506.13977) | Tool calling error taxonomy | Implemented (failure classifier) |
| Atom of Thoughts (2502.12018) | DAG decomposition for small models | Implemented (AoT) |
| Proactive Agents (2501.00383) | Heartbeat / inner life / self-prompting | Phase 6 |
| LatentMAS (2511.20639) | KV-cache sharing between agents | Phase 7 |
| Speculative Decoding survey | Draft-verify for faster generation | Tested, dropped (arch mismatch) |
| Self-Consistency CoT (2203.11171) | Majority voting over reasoning paths | Phase 7 (quorum) |

## Key Sources (from Vera 2.0)

| Module | Use | Status |
|--------|-----|--------|
| vera_genome.json | First-person prompting, identity architecture | Implemented |
| 07_autonomy_kernel_orchestrator.py | Recovery-first decision engine | Implemented |
| 02_failure_learning_ingest.py | Failure classification | Implemented |
| 04_memory_lifecycle_controller.py | Memory pressure management | Phase 6 |
| 05_foreagent_simulator_stub.py | Tool chain scoring | Phase 6 |
| 08_failure_to_recovery_dataset.py | Failure→recovery training pairs | Phase 6 |
| notebook.py (MARM) | Scratchpad workspace | Implemented |
| flight_recorder.py | Transition logging | Implemented |
| 03_autonomy_budget_signal.py | Token budget guard | Implemented |

---

*SmolClaw: $75 hardware. Real AI. No excuses.*
