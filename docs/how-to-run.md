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

### Which model is used

- Default with a Gemini key: `gemini-3.5-flash-lite`.
- If that model's daily quota runs out (or it is retired/overloaded), the app switches to the next model in
  `FRONTDESK_MODEL_FALLBACKS` (default `gemini-3-flash-preview,gemini-3.1-flash-lite,gemini-2.5-flash`) and prints
  a yellow **MODEL SWITCH** line. The run summary lists every model used.
- Pick a model yourself: `--model gemini-3-flash-preview` or `FRONTDESK_MODEL=...` in `.env`.

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
| `429` retries / slow progress | Per-minute limit; the app waits and retries | Lower `FRONTDESK_MAX_RPM` |
