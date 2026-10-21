# Before / after: deterministic safety, deadlines and the Groq backup (2026-10-21)

Metrics only, no patient text. Labels were not changed. "Before" = the commit before `1aaacba`; "after" = `b5f8cd5`.
All runs use synthetic data. Nothing here is evidence of clinical safety or production readiness.

## What changed

1. **Keyword safety gate.** More red-flag patterns (heart attack, chest pressure, stroke incl. vision loss,
   choking, anaphylaxis, low sugar without numbers, seizures, unconsciousness, overdose/poisoning, bleeding on blood
   thinners or in pregnancy, explicit self-harm, meningitis/infant fever, head injury, Spanish phrases, spelling
   variants). Each match is checked in its clause: negated, historical, informational, quoted or figurative wording
   is deferred to the AI and escalates if the AI fails.
2. **Time limits.** 8 s per AI call, 20 s per message, rate-limit waits over 3 s switch model. Our own pacing queue
   (`FRONTDESK_MAX_RPM`) does not count against the deadline.
3. **Groq backup.** Gemini → Groq (`qwen/qwen3.8-27b`, `openai/gpt-oss-120b`) → other Gemini models, same tools and
   rules. A transient failure moves one call; quota / bad key / retired model drops the model for the run.

## Synthetic safety probes (keyword gate only, no AI)

| | Before | After |
|---|---|---|
| Emergency phrasings caught (41 probes) | 21 | **41** |
| Non-emergency phrasings falsely escalated (12 probes) | 6 | **0** |

The probes are permanent tests in `tests/test_safety_probes.py` (48 emergency, 20 non-emergency phrasings).

## Labelled datasets

### Sample 40 · no AI

| Metric | Before | After |
|---|---|---|
| Emergency recall | 5/5 | 5/5 |
| must_not violations | 0 | 0 |
| Over-escalations | 0 | 0 |
| Urgency accuracy | 97.5% | 97.5% |
| Action exact-match | 95.0% | 95.0% |
| Action micro F1 | 97.9% | 97.9% |

### Held-out 22 · no AI

| Metric | Before | After |
|---|---|---|
| Emergency recall | 2/4 (missed H002, H018) | **4/4** |
| must_not violations | 0 | 0 |
| Over-escalations | 0 | 0 |
| Urgency accuracy | 90.9% | **100.0%** |
| Action exact-match | 50.0% | 59.1% |
| Action micro F1 | 56.0% | 64.0% |

### Sample 40 · AI

| Metric | Before | After v1 (pacing bug) | After v2 |
|---|---|---|---|
| Emergency recall | 5/5 | 5/5 | 5/5 |
| must_not violations | 0 | 0 | 0 |
| Over-escalations | 0 | 0 | 0 |
| Urgency accuracy | 95.0% | 95.0% | 95.0% |
| Action exact-match | 85.0% | 85.0% | 77.5% |
| Action micro F1 | 94.0% | 94.0% | 91.3% |
| Degraded messages | 0 | 11 | 5 |
| Model switches | 0 | 4 | 70 |
| Models used | gemini-3.5-flash-lite | + groq qwen | gemini-3.5-flash-lite, gemini-3-flash-preview, groq qwen, groq gpt-oss-120b |
| Latency p50 / p95 (ms) | 17,287 / 64,345 | 14,764 / 29,999 | 14,917 / 38,576 |
| LLM calls · cost (paid prices) | 123 · $0.172 | 102 · $0.136 | 123 · $0.185 |

### Held-out 22 · AI

| Metric | Before | After v1 (pacing bug) | After v2 |
|---|---|---|---|
| Emergency recall | 4/4 | 4/4 | 4/4 |
| must_not violations | 0 | 0 | 0 |
| Over-escalations | 0 | 0 | 0 |
| Urgency accuracy | 100.0% | 100.0% | 100.0% |
| Action exact-match | 95.5% | 90.9% | 81.8% |
| Action micro F1 | 98.1% | 92.6% | 92.9% |
| Degraded messages | 0 | 6 | 7 |
| Model switches | 0 | 6 | 67 |
| Models used | gemini-3.5-flash-lite | + groq qwen | gemini-2.5-flash, gemini-3-flash-preview, groq qwen, groq gpt-oss-120b |
| Latency p50 / p95 (ms) | 13,348 / 42,376 | 14,842 / 29,993 | 9,787 / 37,304 |
| LLM calls · cost (paid prices) | 66 · $0.090 | 50 · $0.071 | 54 · $0.070 |

Latency in batch runs includes deliberate pacing at 12 requests/minute for free-tier keys.

## How to read the AI rows honestly

- **Safety did not regress in any run**: every emergency caught, zero `must_not` violations, zero over-escalations,
  including the runs where many AI calls failed.
- **After v1** exposed a bug: our own request pacing was counted against the 20 s deadline, so 11 + 6 messages
  finished without AI. Fixed in `b5f8cd5`, with a test that fails without the fix.
- **After v2 is not a clean accuracy comparison.** During that run Gemini `3.5-flash-lite` timed out (> 8 s) on
  about a quarter of calls and then hit its **daily free quota**, as did `gemini-2.5-flash` and
  `gemini-3-flash-preview`; both Groq models hit their free **8,000 tokens/minute** limit repeatedly. The chain kept
  the run going across four models, but 5 + 7 messages finished without AI. The drop in exact-match comes from those
  messages: they gained a `route_to_front_desk` (the AI had already done part of the work, so a human finishes it
  rather than the fallback redoing it) or the rules path's cautious clinician routing.
- A clean accuracy comparison needs a fresh daily quota or paid keys: run the commands below again.

## Live single messages (what the interview looks like)

| Situation | Measured |
|---|---|
| Emergency caught by keywords | 0.1 s, 0 AI calls |
| Refill, Gemini healthy | 7.1 s, 4 calls |
| Refill, Gemini key broken → Groq | 4.5 s, 4 calls (`gemini → groq qwen → groq gpt-oss-120b`), not degraded |
| Groq per call | about 0.2-1.5 s; Gemini about 2-3 s, occasionally > 8 s |

## Reproduce

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\frontdesk.exe run --mode rules --quiet --out runs/after-sample-rules
.\.venv\Scripts\frontdesk.exe run --mode rules --quiet --messages eval_sets/heldout/messages.json --labels eval_sets/heldout/labels.json --out runs/after-heldout-rules
$env:FRONTDESK_MAX_RPM='12'; $env:FRONTDESK_CONCURRENCY='2'
.\.venv\Scripts\frontdesk.exe run --quiet --out runs/after2-sample-llm
.\.venv\Scripts\frontdesk.exe run --quiet --messages eval_sets/heldout/messages.json --labels eval_sets/heldout/labels.json --out runs/after2-heldout-llm
```

The "before" reports are kept locally in `runs/baseline-2026-10-21/` (git-ignored because they contain patient
details); each folder has `eval_report.md`, `eval_report.json` and `run_meta.json`.
