# AI usage, cost and the plan we need

How the agent uses AI APIs, measured from real runs: how many requests a message makes, how many tokens each
request uses, what that costs, which limits the free tiers impose, and which plan a clinic would need.

All numbers below are **measured** from saved runs on the 40 sample and 22 unseen messages (2026-10-21/22) unless
marked as a projection. Prices are paid-tier list prices per million tokens, checked 2026-10-21.

## 1. Where the AI is used (and where it is not)

| Step | AI? | Requests |
|---|---|---|
| Identify sender | No (Python) | 0 |
| Safety check, clear red flags | No (Python keywords) | 0: emergencies like chest pain never wait for, or pay for, an AI call |
| Understand the message | Yes | 1 per message (structured JSON) |
| Act with clinic tools | Yes | 1 per agent turn, usually 2-4 turns |
| Reply | Mostly no (templates) | 1 only when answering a general question in free text |
| Validate, route, save | No (Python) | 0 |

The AI does what code cannot do well: read messy, multilingual messages and decide which clinic tools to use. Every
rule (refills, bookings, privacy, emergencies) stays in Python, so a cheaper or faster model can be swapped in
without changing what is allowed.

## 2. Tokens per request (measured)

| Request type | Requests per 40-message run | Average input tokens | Average output tokens | Average total | Largest seen | Average time |
|---|---|---|---|---|---|---|
| Understand | 35 | ~1,500-1,630 | ~240 | **~1,750-1,870** | 2,355 | 1.4 s |
| Agent turn (act) | 85-86 | ~2,630-2,980 | ~245-280 | **~2,870-3,260** | 6,088 | 1.8 s |
| Answer a general question | 2 | ~180-210 | ~30-80 | **~200-300** | 352 | 0.4 s |

Input dominates (about 90% of tokens): the instructions, the clinic calendar and the tool definitions are sent with
every request. Output is short because the AI returns structured data and tool calls, not prose.

## 3. Requests and tokens per message (measured)

| Message type | Share of the sample | Requests | Tokens | Example |
|---|---|---|---|---|
| Emergency caught by keywords | 5 of 40 | **0** | **0** | "crushing chest pain going into my left arm" |
| Understand only | 4 of 40 | 1 | ~1,700-1,800 | spam, thank-you, privacy refusal, identity check |
| Understand + act | 31 of 40 | ~3.8 (max 5) | ~9,800-10,800 (max ~20,000) | refills, bookings, reschedules, questions |
| **Average over all 40** | | **~3.05** | **~7,800-8,500** | |

A full 40-message run: **~122 requests, ~310,000-340,000 tokens.**

## 4. Cost (measured volume × list price)

The same measured token volume (one 40-message run) priced on each model:

| Model | Input / output price per 1M tokens | Per 40-message run | Per message |
|---|---|---|---|
| `gemini-3.5-flash-lite` (default) | $0.30 / $2.50 | **$0.15-0.17** | **~$0.004** |
| `gemini-2.5-flash` | $0.30 / $2.50 | $0.15-0.17 | ~$0.004 |
| `gemini-3-flash-preview` | $0.50 / $3.00 | $0.21-0.25 | ~$0.006 |
| `groq openai/gpt-oss-120b` (backup) | $0.15 / $0.60 | $0.06-0.07 | ~$0.0016 |
| `groq qwen/qwen3.8-27b` (backup) | $0.80 / $4.00 | $0.34-0.38 | ~$0.009 |

Measured totals: the single-model run cost **$0.172** (123 requests); the final submission run, served by the backup
chain, **$0.168** (122 requests). The app prints the cost of every message and every run, so these are not estimates.

## 5. Limits we hit on free tiers (observed)

| Provider / model | Per minute | Per day | What happened |
|---|---|---|---|
| Gemini `gemini-2.5-flash` (free) | 5 requests | ~20 requests | Daily quota gone after about 20 calls |
| Gemini `gemini-3.5-flash-lite` (free) | 15 requests | a few hundred | One full run plus tests used a day's quota; the model also slowed to 16-18 s per call on 2026-10-22 |
| Groq `qwen/qwen3.8-27b`, `openai/gpt-oss-120b` (free / base) | 30 requests, **8,000 tokens** | 1,000 requests, **200,000 tokens** | 8,000 tokens per minute is less than one full message (~10,000 tokens), so Groq throttles every minute; 200,000 tokens per day is **about 25 full messages per model** |

These limits are why the evaluation runs had to be paced at 12 requests per minute, took 11-15 minutes, and
sometimes finished a few messages without AI. They are a property of the free plans, not of the app.

## 6. What a clinic would need (projection)

The brief describes "dozens of patient messages every day". Using the measured averages (3 requests and ~8,000
tokens per message, ~$0.004 per message on `gemini-3.5-flash-lite`):

| Messages per day | Requests per day | Tokens per day | Cost per day | Cost per month (30 days) |
|---|---|---|---|---|
| 50 | ~150 | ~0.4 M | ~$0.21 | **~$6** |
| 200 | ~600 | ~1.6 M | ~$0.84 | **~$25** |
| 1,000 | ~3,000 | ~8 M | ~$4.20 | **~$125** |

Peak load is small too: 20 messages arriving within 10 minutes is about 6 requests per minute.

**What this means:** the free tiers cannot run even a small clinic reliably (Gemini's daily caps, Groq's 8,000
tokens per minute). The cost of doing it properly is tiny, a few dollars to a few tens of dollars a month, so the
right move is a paid, pay-as-you-go plan rather than squeezing free tiers.

## 7. Recommended plan

1. **Gemini API, paid Tier 1 (primary).** Link a Google Cloud billing account to the project; there is no
   subscription fee, you pay per token. Tier 1 raises the rate limits (exact per-model limits are shown in
   [AI Studio](https://aistudio.google.com/rate-limit)), with a $250 billing cap and automatic upgrades to Tier 2
   (after $100 spent + 3 days) and Tier 3 (after $1,000 + 30 days). Set a **budget alert** in Google Cloud (for
   example $25/month).
2. **Groq Developer plan (backup).** Pay-as-you-go; the backup only runs when Gemini is slow or failing, so its
   cost is small (`gpt-oss-120b` is the cheapest model we measured, ~$0.0016 per message).
3. **Keep the app's own guards on:** `FRONTDESK_MAX_COST_USD` (per-run budget, default $2), optional
   `FRONTDESK_MAX_TOKENS`, `FRONTDESK_MAX_RPM` pacing, the red ALERT when quota or budget runs out, and per-message
   cost on screen.
4. **Before real patient data:** use a provider and product covered by a signed **BAA** (for Google, Gemini on
   Vertex AI under a Google Cloud BAA rather than the AI Studio key used here; confirm the current HIPAA coverage
   with each provider), with zero data retention. See the README's "Before this touches real patient data".

## 8. Ways to cut tokens further (if volume grows)

- **Prompt caching:** about 90% of each request is the same instructions, calendar and tool list; providers that
  cache repeated prompt prefixes charge much less for those tokens.
- **Cheaper model for the understand step:** it is short and structured; `gpt-oss-120b` or a Flash-Lite model
  costs a fraction, while the agent turns keep the stronger model.
- **Fewer agent turns:** most messages need 2-4; tighter tool descriptions reduce retries.
- **Discounted batch processing** (offered by both providers) for non-urgent work such as overnight re-checks. Never
  for incoming messages or the safety step.

## 9. How to check usage yourself

- Every message panel shows `AI calls · tokens · $cost`; every run ends with totals and the per-message average.
- `runs/<folder>/run_meta.json`: totals, models used, latency per model, model switches, alert reason.
- `runs/<folder>/traces.jsonl`: one `llm.usage` event per request (model, purpose, input/output tokens, latency, cost).

Sources: [Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing) ·
[Gemini rate limits and tiers](https://ai.google.dev/gemini-api/docs/rate-limits) ·
[Groq models and pricing](https://console.groq.com/docs/models) ·
[Groq rate limits](https://console.groq.com/docs/rate-limits)
