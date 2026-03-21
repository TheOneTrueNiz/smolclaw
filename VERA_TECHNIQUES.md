# Vera Techniques for SmolClaw

Extracted from Vera 2.0 snapshot — techniques most applicable to a 3B model agent.

## High Priority (implement next)

### Tool Selection Memory
**Source:** `src/memory/retrieval/tool_selection_memory.py`
- Track which tools succeed in which contexts (EMA, decay_factor=0.9)
- Jaccard similarity to find similar past execution contexts
- 30-50% fewer retries through learned tool preference
- Lightweight: just a JSON performance database per tool

### 4-Tier Error Recovery
**Source:** `src/orchestration/error_handler.py`
- RETRY → FALLBACK → DEGRADE → ESCALATE (SmolClaw currently only has RETRY and ABORT)
- Fallback chain mapping: e.g., `grep → read_file → shell cat`
- Fallback depth tracking to prevent cascading loops
- Transient vs permanent error classification (already partially implemented)

### Tool Output Verification
**Source:** `src/orchestration/tool_output_verifier.py`
- Check tool outputs for prompt injection patterns before feeding to model
- Risk scoring (0.0-1.0), flag at 0.3, block at 0.7
- Prevents tool outputs from hijacking 3B model reasoning

## Medium Priority

### Task Complexity Scoring
**Source:** `src/quorum/quorum_selector.py`
- Automatic 1-5 complexity assessment based on text patterns
- Multi-step detection (then, after, next, finally, first, second)
- Could replace/enhance AoT heuristic bypass

### Modular Example Rotation
**Source:** `config/vera_genome.json` + `src/core/runtime/genome_config.py`
- Fixed examples (always included) + rotating examples (random subset)
- Keeps prompt fresh while staying within token budget
- For SmolClaw: rotate shell command examples based on task type

## Lower Priority (Phase 7+)

### CommVQ 2-bit KV Cache Compression
**Source:** `src/memory/storage/commvq_compression.py`
- 87.5% context reduction — but requires additional libraries
- More relevant when running 7B model split across NUCs

### Rolling Conversation Summarization
**Source:** `src/core/runtime/vera.py` lines 2886-3025
- Summarize older messages, keep last 15
- Extract commitment tokens (todo, follow up, deadline)
- Useful for long interactive sessions

### 3-Stage Memory Pipeline
**Source:** `src/core/services/memory_service.py`
- RAGCache (10ms) → HSA (30ms) → GraphRAG (60ms)
- Sub-100ms total — but SmolClaw's memory is tiny, doesn't need this yet
