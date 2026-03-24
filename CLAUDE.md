# SmolClaw — Claude Code Instructions

## First: Read the Diary

**Before doing anything**, read `diary/instructions.md` then follow its reading order. The diary contains full project context, current status, pending tasks, lessons learned, and collaboration notes from prior sessions. It is the handoff document between Claude Code instances.

## Cluster Topology

SmolClaw runs on a 3-NUC headless cluster. You are editing code on the **hackbook (nizbot0)**, but the production code runs on **nizbot1 (NUC1)**.

| Node | Role | LAN IP | Tailscale IP | User |
|------|------|--------|-------------|------|
| nizbot0 | Hackbook (your workstation) | — | 100.73.228.96 | nizbot0 |
| nizbot1 | NUC1 — Actor + web_ui.py | 10.0.0.1 | 100.126.137.93 | nizbot1 |
| nizbot2 | NUC2 — Critic/grounding | 10.0.0.2 | 100.104.164.38 | nizbot2 |
| nizbot3 | NUC3 — Memory/recall | 10.0.0.3 | 100.110.49.11 | nizbot3 |

## SSH Access (from hackbook)

SSH keys are set up. Connect to any NUC from the hackbook using Tailscale IPs:

```bash
ssh nizbot1@100.126.137.93   # NUC1 — Actor, web_ui, llama-server
ssh nizbot2@100.104.164.38   # NUC2 — Critic
ssh nizbot3@100.110.49.11    # NUC3 — Memory
```

SCP for file sync:
```bash
scp agent.py agent_hackbook.py web_ui.py nizbot1@100.126.137.93:~/smolclaw/
```

Check cluster health:
```bash
for n in 100.126.137.93 100.104.164.38 100.110.49.11; do curl -s http://$n:8090/health && echo " $n"; done
```

Watch web_ui logs:
```bash
ssh nizbot1@100.126.137.93 "tail -f /tmp/web_ui.log"
```

## Two Agent Variants

- **`agent.py`** — LAN IPs (10.0.0.x). Used when web_ui.py runs on nizbot1 directly.
- **`agent_hackbook.py`** — Tailscale IPs (100.x.x.x). Used when web_ui.py runs on nizbot0 or anywhere outside the LAN. **web_ui.py auto-detects** which to import based on hostname.

Both files MUST stay in sync. Every change to agent.py must be mirrored to agent_hackbook.py (only the URL constants at the top differ).

## Deployment Workflow

After editing code on nizbot0:

1. **Test syntax**: `python3 -c "import py_compile; py_compile.compile('agent.py', doraise=True); py_compile.compile('agent_hackbook.py', doraise=True)"`
2. **Push to git**: `git add <files> && git commit && git push origin main`
3. **Sync to NUC1**: `scp agent.py agent_hackbook.py web_ui.py nizbot1@100.126.137.93:~/smolclaw/`
4. **Restart web_ui on NUC1**: `ssh nizbot1@100.126.137.93 "pkill -f 'python3.*web_ui' ; sleep 3 ; nohup python3 -u ~/smolclaw/web_ui.py > /tmp/web_ui.log 2>&1 &"`
5. **Verify**: `ssh nizbot1@100.126.137.93 "tail -5 /tmp/web_ui.log"`

Or use the helper: `./deploy.sh` (if it exists).

## Key Constraints

- **8K context window** — SmolLM3-3B has tight context. Every token in the system prompt counts.
- **4.5-7.2 tok/s** — NUC inference is slow. Minimize unnecessary LLM calls.
- **--parallel 1** — Each NUC handles one request at a time. Don't send concurrent requests to the same NUC.
- **No pip install** — NUCs run stdlib Python only. No external dependencies in agent code.
- **web_ui.py must be restarted** after code changes — it imports agent modules at startup.

## Testing

- **Doctor Claude** (`doctor_claude.py`): Diagnostic harness with E2E clinic, curriculum, and surgeon mode. Run from nizbot1 or hackbook.
- **Curriculum**: `curricula/*.json` — 6 modules, 29 lessons. Currently 29/29 passing.
- **Live test**: Hit `http://100.126.137.93:8080` from hackbook browser, or `http://10.0.0.1:8080` from NUC LAN.
- **Logs**: `ssh nizbot1@100.126.137.93 "tail -f /tmp/web_ui.log"`

## Architecture Quick Reference

- State machine: INIT -> SELECT_TOOL <-> (CRITIC_CHECK -> EXECUTE) -> SYNTHESIZE -> DONE
- Critic runs on NUC2, memory/recall on NUC3, actor/synthesis on NUC1
- Flight recorder: `flight_recorder.jsonl` — logs every tool call
- SmolClaw's own memory: `memory.md` — append-only markdown
- Conversation summaries: stored in `conversations/` dir by web_ui.py (background NUC3 summarization)

## Do NOT

- Edit code only on nizbot0 without syncing to nizbot1 — the running instance won't see changes
- Use `pkill -f web_ui` over SSH — it can kill the SSH session itself. Use `pkill -f 'python3.*web_ui'` instead
- Add external pip dependencies to agent code
- Overwrite `agent.py`, `agent_hackbook.py`, `web_ui.py`, or `doctor_claude.py` via SmolClaw's own write_file tool (they're in the blocklist)
