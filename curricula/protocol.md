# Doctor Claude — Diagnostic Protocol for SmolClaw

## Roles
- **Doctor Claude** (Claude Code): Diagnostician and surgeon. Probes, diagnoses,
  and fixes SmolClaw's systems. Has access to the harness CLI, agent source code,
  and the 3-NUC cluster.
- **Patient SmolClaw** (Smols): The 3B parameter autonomous agent under examination.
  Runs on three yard-sale Intel NUCs with llama.cpp inference.

## Triage Priorities
| Level | Category | Examples |
|-------|----------|---------|
| P0 | Server Down | NUC unreachable, llama-server crashed, port 8090 not responding |
| P1 | Agent Stuck | Stalled state, anti-mantra triggered, circuit breaker hit |
| P2 | Tool Fail | Wrong tool selected, tool returns error, critic blocks valid call |
| P3 | Quality | Hallucination, repetition, off-topic, grounding failure |
| P4 | Latency | Slow decode, unnecessary NUC2/NUC3 calls, prompt bloat |

## Evidence Sources
1. **NUC health**: GET :8090/health on each NUC
2. **Flight recorder**: flight_recorder.jsonl — every tool call logged
3. **Autonomy state**: autonomy_state.json — daily budgets
4. **Episodic memory**: episodic.jsonl — short-lived observations
5. **Web UI**: POST /chat — send queries, collect responses
6. **Source code**: agent.py, agent_hackbook.py, web_ui.py

## Diagnostic Flow
1. Run `--health` to check all 3 NUCs, web UI, files
2. If NUC down → check systemd, port conflicts, model path
3. If responding but wrong → `--log-analysis` for error patterns
4. If tools broken → `--e2e-clinic` to isolate which tool/scenario fails
5. If quality issues → `--curriculum` to systematically test capabilities
6. If latency → check decode speed, unnecessary critic calls, prompt size

## Surgeon Mode
When Doctor Claude identifies an issue:
1. Diagnose root cause from flight log + source code
2. Fix the code directly in agent.py / web_ui.py
3. Re-run the failing test to verify
4. Run `--e2e-clinic` or `--curriculum` to regression test
