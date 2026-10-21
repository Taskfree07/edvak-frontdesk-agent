# How to run and test the front-desk agent

Commands below are for Windows PowerShell from the project folder. On macOS/Linux replace
`.\.venv\Scripts\` with `.venv/bin/`.

## 1. One-time setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Put the clinic data in place (it is not in git):

```powershell
Expand-Archive edvak_forge_data.zip -DestinationPath edvak_forge_data
```

The folder must contain `clinic.json`, `patients.json`, `medications.json`, `appointments.json`,
`messages_sample.json` and `labels_sample.json`.

Create your settings file and add **one** API key:

```powershell
Copy-Item .env.example .env
notepad .env        # fill in the GEMINI_API_KEY line (never paste keys into chat, code or commits)
```

`.env` is git-ignored, and a local pre-commit check blocks any commit that contains a key.

## 2. Check everything works without any API key

```powershell
.\.venv\Scripts\python.exe -m pytest              # all tests, offline (no network, no key)
.\.venv\Scripts\frontdesk.exe run --mode rules    # all 40 messages with the no-AI rules mode, then the scores
```

## 3. Run with the AI

```powershell
.\.venv\Scripts\frontdesk.exe run                 # all 40 sample messages + evaluation
.\.venv\Scripts\frontdesk.exe run --quiet         # same, one line per message
.\.venv\Scripts\frontdesk.exe run --ids M003,M006,M018    # just some messages
```

**Free-tier Gemini keys** have small per-minute and per-day limits. Add these to `.env` (or set them in the
shell before running):

```
FRONTDESK_MAX_RPM=12          # requests per minute (stay just under the model's limit)
FRONTDESK_CONCURRENCY=2
```

### Which model is used, and the Groq backup

- Default with a Gemini key: `gemini-3.5-flash-lite`. The header shows the whole chain, e.g.
  `AI chain: gemini-3.5-flash-lite → groq:qwen/qwen3.8-27b → groq:openai/gpt-oss-120b → …`.
- **Groq is the backup.** Put `GROQ_API_KEY` in `.env`. When Gemini times out, is busy, rate-limits for more than
  3 s, returns a network error twice, rejects the key or runs out of daily quota, that call moves to the next model
  and a yellow **MODEL SWITCH** line says why and whether it is "for this call" (temporary) or "for the rest of the
  run" (quota, bad key, retired model). Models whose provider has no key are skipped.
- Change the order with `FRONTDESK_MODEL_FALLBACKS`, or pick a model with `--model groq:openai/gpt-oss-120b`.
- A free Groq key allows only about 8,000 tokens per minute per model, roughly one full message per minute; the
  chain then moves to the next Groq model. Measured on 2026-10-21: Groq calls about 0.2-1.5 s each.

### Time limits (so nothing hangs)

- Each AI call: `FRONTDESK_CALL_TIMEOUT_S` (default 8 s). Each message overall: `FRONTDESK_MESSAGE_DEADLINE_S`
  (default 20 s). A rate-limit wait longer than `FRONTDESK_MAX_RETRY_WAIT_S` (default 3 s) switches model instead.
- If the deadline is reached, a yellow line says so and the message continues **without AI**, marked `degraded`:
  - an emergency already found is never downgraded; a possible red flag the AI did not clear is escalated;
  - any clinical message goes to a clinician the same day (`clinician_urgent`), never treated as routine;
  - if the AI had already booked/cancelled/prepared a refill, nothing is redone: the message goes to the front desk.

### Spending and token alerts

- Every message shows its AI calls, tokens and cost; the run summary shows totals and the per-message average.
- `FRONTDESK_MAX_COST_USD` (default 2.00) and optional `FRONTDESK_MAX_TOKENS` cap one run.
- When every model is out of quota, the key is rejected, or a cap is reached, a red **ALERT** box appears, no more
  AI calls are made, and the remaining messages are handled by the rules fallback and marked `[degraded]`.
- Costs are shown at paid-tier prices; a free-tier key is not charged.

## 4. Process a new message (as in the interview)

```powershell
.\.venv\Scripts\frontdesk.exe run --text "Hi, can I get my lisinopril refilled?" --contact 555-0102 --name "Robert Chen"
.\.venv\Scripts\frontdesk.exe run --text "My chest hurts and my left arm is numb" --contact 555-0103
.\.venv\Scripts\frontdesk.exe run --text "Please send my records" --contact new@example.org --name "Robert Chen" --dob 1971-11-20 --channel email
```

Options: `--contact` (phone/email; matched against the record), `--name`, `--dob YYYY-MM-DD`,
`--channel sms|portal|email`, `--at` (received time, default 2026-11-09 09:00).
For several new messages, put them in a JSON file shaped like `messages_sample.json` and run
`frontdesk run --messages my_messages.json` (add `--labels my_labels.json` to score them).

## 5. Evaluate

| Command | What it does |
|---|---|
| `frontdesk run` | Runs the 40 samples and prints the scorecard |
| `frontdesk run --repeat 3` | Three runs; mean/min/max of every metric (AI answers vary between runs) |
| `frontdesk run --messages eval_sets/heldout/messages.json --labels eval_sets/heldout/labels.json` | The 22 held-out messages (expected answers were written by AI and should be reviewed) |
| `frontdesk run --mode rules ...` | The same, without AI: the baseline to compare against |
| `frontdesk eval --run runs/<folder>` | Re-score an earlier run |
| `frontdesk show M006` | Replay every step of one message from the latest run |

The scorecard reports, in this order of importance: emergency recall (and every missed emergency), `must_not`
violations (each listed), over-escalations, urgency accuracy, action accuracy, patient-id and intent accuracy,
validator interventions, degraded messages (and how many had AI actions before the fallback), latency, AI calls,
tokens and cost.

## 6. Where the output goes

Each run writes `runs/<timestamp>/` (git-ignored, because it contains patient details):

- `results.json`: one result per message, in the format the brief asks for
- `eval_report.md` / `eval_report.json`: the scorecard
- `traces.jsonl`: every step of every message (what was checked, which tools ran, what they returned)
- `state_final.json`: the schedule, queues, prepared refills and reply drafts after the run
- `run_meta.json`: models used, model switches, AI totals, alert reason if the AI stopped

The latest full sample run is also copied to `runs/latest/` and `results.json` at the project root.

## 7. If something goes wrong

| You see | Meaning | Fix |
|---|---|---|
| `LLM mode needs GEMINI_API_KEY` | No key found | Add it to `.env` |
| Yellow **MODEL SWITCH** | One model ran out of quota; another took over | Nothing; check the summary for which models ran |
| Red **ALERT: model calls stopped** | All models exhausted, key rejected, or budget cap reached | Wait for the daily reset, use a paid key, or raise the cap |
| Many `[degraded]` messages | The AI was unavailable for those messages | See the alert; rerun later |
| `… retrying in N s` | A short rate-limit or network wait | Nothing; longer waits switch model |
| `deadline reached; continuing without AI` | No AI answer within 20 s | Check the switch lines above it; raise `FRONTDESK_MESSAGE_DEADLINE_S` for batch runs if needed |
