# SmolClaw Quick Start

Get SmolClaw running from zero. This guide assumes you have at least one x86_64 Linux machine with 8GB+ RAM. Additional machines are optional — SmolClaw works in single-node mode, but scales to a 3-NUC cluster.

---

## 1. Build llama.cpp

SmolClaw uses `llama-server` from llama.cpp as its inference backend.

```bash
# Clone and build
git clone https://github.com/ggerganov/llama.cpp.git
cd llama.cpp
cmake -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --config Release -j$(nproc)

# Verify
./build/bin/llama-server --version
```

---

## 2. Download the Model

SmolClaw runs **SmolLM3-3B** quantized to Q4_K_M (1.9GB).

```bash
mkdir -p ~/models/smollm3-config

# Download the GGUF model
huggingface-cli download HuggingFaceTB/SmolLM3-3B-GGUF \
    smollm3-3b-q4_k_m.gguf \
    --local-dir ~/models

# Download the chat template (required for --jinja)
huggingface-cli download HuggingFaceTB/SmolLM3-3B \
    chat_template.jinja \
    --local-dir ~/models/smollm3-config
```

If you don't have `huggingface-cli`, install it with `pip install huggingface-hub` or download the files manually from [HuggingFaceTB/SmolLM3-3B-GGUF](https://huggingface.co/HuggingFaceTB/SmolLM3-3B-GGUF).

---

## 3. Start the Inference Server

### Option A: Run directly

```bash
~/llama.cpp/build/bin/llama-server \
    --model ~/models/smollm3-3b-q4_k_m.gguf \
    --host 127.0.0.1 \
    --port 8090 \
    --ctx-size 8192 \
    --parallel 1 \
    --threads $(nproc) \
    --jinja \
    --chat-template-file ~/models/smollm3-config/chat_template.jinja
```

### Option B: systemd service (recommended)

Create `~/.config/systemd/user/smollm3.service`:

```ini
[Unit]
Description=SmolLM3 Inference Server (llama.cpp)
After=network.target

[Service]
Type=simple
ExecStart=/home/YOUR_USER/llama.cpp/build/bin/llama-server \
    --model /home/YOUR_USER/models/smollm3-3b-q4_k_m.gguf \
    --host 127.0.0.1 \
    --port 8090 \
    --ctx-size 8192 \
    --parallel 1 \
    --threads 4 \
    --jinja \
    --chat-template-file /home/YOUR_USER/models/smollm3-config/chat_template.jinja
Restart=on-failure
RestartSec=5
Environment=LLAMA_LOG_DISABLE=1

[Install]
WantedBy=default.target
```

Then enable it:

```bash
systemctl --user daemon-reload
systemctl --user enable --now smollm3
loginctl enable-linger $USER    # survives logout

# Verify
curl http://127.0.0.1:8090/health
# → {"status":"ok"}
```

---

## 4. Configure SmolClaw

Edit the config section at the top of `agent.py` to match your paths:

```python
LLAMA_URL = "http://127.0.0.1:8090/v1/chat/completions"
CRITIC_URL = "http://10.0.0.2:8090/v1/chat/completions"  # or same as LLAMA_URL for single-node
MEMORY_URL = "http://10.0.0.3:8090/v1/chat/completions"  # or same as LLAMA_URL for single-node
MODEL = "smollm3-3b-q4_k_m.gguf"
HOME = Path("/home/YOUR_USER/smolclaw")
```

### Single-Node Mode

If you only have one machine, set `CRITIC_URL` and `MEMORY_URL` to the same as `LLAMA_URL`:

```python
CRITIC_URL = "http://127.0.0.1:8090/v1/chat/completions"
MEMORY_URL = "http://127.0.0.1:8090/v1/chat/completions"
```

Everything works the same — the critic, reflector, memory, and AoT decompose just share the local inference server. Performance will be slower since all roles share one server, but it's fully functional.

### 3-Node Mode (full cluster)

If you have three machines:

1. Build llama.cpp and download the model on each machine
2. Create the same systemd service but with `--host 0.0.0.0` on nodes 2 and 3 (so they accept network connections)
3. Set `CRITIC_URL` to point at NUC2's IP, `MEMORY_URL` to NUC3's IP
4. Connect the machines via ethernet (direct cable or switch)

- **NUC1 (Actor):** Runs the agent loop, proposes tool calls, executes tools
- **NUC2 (Critic):** Safety checks, AoT decomposition, reflection on failures
- **NUC3 (Memory):** Smart recall, memory verification, episodic memory, reflection

---

## 5. Run SmolClaw

### Interactive Mode

```bash
python3 agent.py
```

```
    ╔═══════════════════════════════════════════╗
    ║  🦀 SmolClaw v0.9.0                      ║
    ║  SmolLM3-3B · State Machine Cluster     ║
    ║  $75 yard sale hardware · AI for all    ║
    ╚═══════════════════════════════════════════╝

  NUC1/Actor  (local): ONLINE
  NUC2/Critic (10.0.0.2): ONLINE
  NUC3/Memory (10.0.0.3): ONLINE
  Autonomy: ACTIVE (ok)
  Daily calls: 0/1000 | Tokens: 0/500000

you > What is the uptime of this machine?

  [turn 1] thinking... 81.5s (19 tokens)
  [critic] checking 1 call(s)... 0.0s
  [tool] shell({"command": "uptime"})
  [ok] 05:35:44 up 18:10,  1 user,  load average: 4.42, 4.08, 4.45
  [turn 2] thinking... 25.2s (45 tokens)

smolclaw > The machine has been running for 18 hours and 10 minutes.
  Load average is 4.42, 4.08, 4.45. 1 user logged in.

you > exit
SmolClaw out. 🦀
```

### One-Shot Mode

```bash
python3 agent.py "How much free disk space do I have?"
```

---

## 6. Run the Test Suite

```bash
# All 26 scenarios across 8 tiers
python3 test_harness.py

# Single tier
python3 test_harness.py --tier 1     # Tool Fluency (6 scenarios)
python3 test_harness.py --tier 2     # Chain Tasks (4 scenarios)
python3 test_harness.py --tier 3     # Self-Introspection (2 scenarios)
python3 test_harness.py --tier 4     # System Diagnostics (3 scenarios)
python3 test_harness.py --tier 5     # Error Recovery (2 scenarios)
python3 test_harness.py --tier 6     # Claim Verification (3 scenarios)
python3 test_harness.py --tier 7     # Safety & Discipline (4 scenarios)
python3 test_harness.py --tier 8     # Abstention & Limits (2 scenarios)

# Single scenario
python3 test_harness.py --scenario 1
```

Results are saved to `test_report.json`.

---

## 7. Analyze the Flight Recorder

Every tool call is logged to `flight_recorder.jsonl`. Analyze it with:

```bash
python3 flight_analysis.py            # full report
python3 flight_analysis.py --tools    # per-tool success rates
python3 flight_analysis.py --failures # show all failures with context
```

Example output:

```
============================================================
  SMOLCLAW FLIGHT RECORDER ANALYSIS
  135 entries | 106 OK (78.5%) | 29 failures
============================================================

Tool               OK  Fail    Rate  Top Error
------------------------------------------------------------
shell              58    25   69.9%  execution_error(19)
read_file          15     4   78.9%  execution_error(4)
write_file         12     0  100.0%  —(0)
remember            6     0  100.0%  —(0)
recall              7     0  100.0%  —(0)
scratchpad          8     0  100.0%  —(0)
```

---

## 8. Tuning

### Token Budgets

At the top of `agent.py`:

```python
MAX_TOKENS_TOOL_CALL = 160   # tokens for proposing a tool call
MAX_TOKENS_SYNTHESIS = 150   # tokens for the final answer
MAX_TOKENS_CRITIC = 32       # "SAFE" or "BLOCK"
MAX_TOKENS_REFLECT = 64      # one-sentence recovery suggestion
```

Lower = faster (fewer tokens to decode). Higher = more room for the model to think. The defaults are tuned for SmolLM3-3B.

### Safety

```python
DAILY_CALL_BUDGET = 1000      # max tool calls per day
DAILY_TOKEN_BUDGET = 500000   # max tokens per day
MAX_TURNS = 10                # max agent loop iterations
MAX_CONSECUTIVE_ERRORS = 3    # circuit breaker threshold
```

### Critic Whitelist

Safe tools that skip the critic (saves 4-10s per call):

```python
SAFE_TOOLS = {"recall", "scratchpad", "web_search", "remember"}
SAFE_SHELL_PREFIXES = (
    "uptime", "df ", "free ", "uname ", "whoami", "hostname",
    "date", "cat ", "ls ", "head ", "wc ", "du ", "ps ",
    "grep ", "find ", "pwd", "id", "echo ",
)
```

Add your own trusted commands to `SAFE_SHELL_PREFIXES` to speed things up.

---

## Troubleshooting

### "NUC1/Actor (local): OFFLINE"

The inference server isn't running.

```bash
# Check status
systemctl --user status smollm3

# Start it
systemctl --user start smollm3

# Check health
curl http://127.0.0.1:8090/health
```

### "NUC2/Critic: OFFLINE" or "NUC3/Memory: OFFLINE"

SmolClaw will fall back to local inference for that role. Everything still works, just slower (those LLM calls share NUC1's server).

To fix: check the relevant machine's service and ensure it's listening on `0.0.0.0:8090`.

### Model generates garbage or nonsense tool calls

The KV cache may be corrupted. Restart the inference server:

```bash
systemctl --user restart smollm3
```

### Agent loops on the same failed command

The anti-mantra detector catches repeated approaches, and the circuit breaker (3 consecutive errors) will stop it. If this happens often with specific prompts, add a shell tip to the system prompt.

### Memory/scratchpad growing too large

```bash
# Check sizes
du -sh flight_recorder.jsonl memory.md scratchpad/

# Clear flight log (safe — it's just analytics)
> flight_recorder.jsonl

# Clear scratchpad (safe — it's just cached output)
rm scratchpad/*.txt

# Reset autonomy counters
echo '{"date":"2026-03-21","daily_calls":0,"daily_tokens":0,"recent_failures":0,"consecutive_failures":0,"total_calls":0}' > autonomy_state.json
```

---

## Using a Different Model

SmolClaw works with any model that supports tool calling via llama.cpp's `--jinja` mode. To swap models:

1. Download a GGUF model and its chat template
2. Update the `--model` and `--chat-template-file` flags in the systemd service
3. Update `MODEL` in `agent.py`
4. Adjust token budgets if needed (larger models can use lower budgets)

Tested alternatives: any SmolLM3 quantization (Q5_K_M, Q8_0). Larger models (7B+) may need `--rpc` to split across multiple machines.

---

*SmolClaw: $75 hardware. Real AI. No excuses.*
