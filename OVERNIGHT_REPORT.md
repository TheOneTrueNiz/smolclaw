# SmolClaw Overnight Progress Report
**Date:** 2026-03-19 (started ~02:00 CDT)

## Summary

Built a test harness, found and fixed bugs, implemented latency optimizations, then built **v0.6.0 with 4-tier error recovery**. SmolClaw went from untested to **15/17 scenarios passing (88.2%)** and then gained shell command preprocessing, JSON repair, and programmatic fallbacks for v0.6.0.

---

## v0.6.0 Changes (Morning Session)

### 4-Tier Error Recovery (from Vera)
- **RETRY**: Reflector suggests alternatives (existed in v0.5)
- **FALLBACK**: When shell commands fail on a file, auto-try `read_file` — no LLM call needed
- **DEGRADE**: Circuit breaker returns partial results instead of empty failure
- **ESCALATE**: Terminal output shows what went wrong

### Shell Command Preprocessor
Catches common 3B model hallucinations before execution:
- `df --i-sync -h /` → `df -h /` (strip hallucinated flags)
- `python -c "..."` → `python3 -c "..."` (correct binary name)
- `ping 10.0.0.2` → `ping -c 3 10.0.0.2` (prevent infinite hang)

### JSON Repair
Fixes malformed tool call JSON that the 3B model generates:
- Nested quotes: `"*.py"` → `'*.py'` (find/glob patterns)
- Shell escapes: `\$2` → `$2` (awk commands)

### Per-Tool Failure Tracking
Runtime counter warns model after 3 failures on the same tool, steering it to alternatives.

### AoT Improvements
- Fixed heuristic: file paths with dots (`.py`, `.md`) no longer trigger false decomposition
- Atom validation: single-word garbage atoms get rejected, returns SIMPLE

### Other
- "Act first" system prompt — model calls tools immediately, no narration
- Tool count in system prompt — self-introspection works without grepping
- Scratchpad stash message no longer contains `<tool_call>` (was triggering output verifier)
- Reflector prompt: shorter, first-person, `/no_think`, never says ABORT
- Critic prompt: added `/no_think` to save thinking tokens on NUC2
- `MAX_TOKENS_TOOL_CALL`: 128 → 160 (3B model sometimes narrates before calling)
- Flight recorder analysis tool: `flight_analysis.py`

---

## v0.5.1 Changes (Overnight Session)

### Test Harness (Doctor_Professor pattern)
17 scenarios across 5 tiers in `test_harness.py`:

| Tier | Name | v0.5.1 Result |
|------|------|--------|
| 1 | Tool Fluency (6 scenarios) | **6/6 PASS** |
| 2 | Chain Tasks (4 scenarios) | **4/4 PASS** |
| 3 | Self-Introspection (2 scenarios) | 1/2 |
| 4 | System Diagnostics (3 scenarios) | **3/3 PASS** |
| 5 | Error Recovery (2 scenarios) | 1/2 |

### Bugs Fixed (v0.5.1)
1. **Critic false positive**: Parser now checks first word only, not full response
2. **Bare JSON tool calls**: Fallback parser catches tool calls without `<tool_call>` tags
3. **False ABORT detection**: Requires `ABORT:` or `ABORT.` with punctuation
4. **Aggressive reflector**: Stronger anti-ABORT prompt language

### Latency Optimizations (v0.5.1)
1. AoT heuristic bypass (saves 8-42s)
2. Stop sequences on critic/decompose/reflect (saves 2-35s)
3. NUC2 offload for AoT decompose + reflect (75-85% faster)
4. Critic whitelist for safe operations (0.0s)
5. --parallel 2→1 on NUC1
6. Tools-free synthesis turn (~405 tokens saved)
7. Reduced synthesis budget (256→150 tokens)

---

## Flight Recorder Analysis

112 total tool calls during testing:
- **82% success rate** overall
- Shell: 74% success (hallucinated flags are the main failure mode)
- write_file, remember, recall, scratchpad: 100% success
- Top shell failures: `df --i-sync`, unquoted grep patterns, `python` instead of `python3`
- 13 failure→recovery pairs found (model self-corrects)

---

## Files Changed

| File | Change |
|------|--------|
| `agent.py` | v0.5.1→v0.6.0: 4-tier recovery, shell preprocessor, JSON repair, per-tool tracking, AoT fixes |
| `test_harness.py` | Fixed 4 test expectations, better prompts |
| `flight_analysis.py` | New — flight recorder analysis tool |
| `ROADMAP.md` | Updated with v0.6.0, Phase 6 progress |
| `VERA_TECHNIQUES.md` | Documented applicable Vera techniques |

---

## Agent Size

| Version | Lines | Key Addition |
|---------|-------|-------------|
| v0.5 | ~800 | Autonomy kernel, test harness |
| v0.5.1 | ~930 | Latency optimizations, bug fixes |
| v0.6.0 | ~1095 | 4-tier recovery, shell preprocessor, JSON repair |

---

*SmolClaw v0.6.0 — 4-tier error recovery, $50 hardware, real AI.*
