# CLAUDE.md — Maplewood front-desk agent

Context for any new Claude session in this repo. Read this first, then continue the work; do not restart or
redesign. The owner (Adil Akhtar) is a product engineer who must be able to explain every part of this system in
an interview and a live demo.

## 1. What this is and where it stands

- **Project:** Edvak Forge take-home: an AI agent that works a clinic's patient inbox (brief:
  `Edvak_Forge_Take-Home_Project.pdf`, data: `edvak_forge_data.zip`; both git-ignored). It reads each message,
  identifies the sender, catches emergencies, does safe routine work through MCP tools that enforce clinic policy
  in Python, hands risky things to humans, and an evaluation harness measures it.
- **Status:** submitted (GitHub link + `submission/` zip, 24 Oct 2026). Repo is **public**:
  https://github.com/Taskfree07/edvak-frontdesk-agent
- **Branches:**
  - `main` = the submitted, terminal (CLI) version. Keep it stable. Only small, tested fixes.
  - `ui-prototype` = web UI built on top of `main` (`frontdesk ui`). **This is where UI work continues.** The
    brief did not ask for a UI; it is for the live demo / walkthrough.
- **Next goal:** finish and polish the web UI on `ui-prototype` so the agent can be shown live to an audience
  (see section 7). Continue the existing UI; do not replace it.

## 2. How to run

```powershell
python -m venv .venv; .\.venv\Scripts\Activate.ps1      # Windows; macOS/Linux: . .venv/bin/activate
pip install -e ".[dev]"
Expand-Archive edvak_forge_data.zip -DestinationPath edvak_forge_data     # data is NOT in git
# .env (git-ignored): fill in the GEMINI_API_KEY and GROQ_API_KEY lines (template: .env.example)
pytest                                   # 173 offline tests on main (+6 web tests on ui-prototype)
frontdesk run --mode rules --quiet       # 40 samples, no AI, scorecard (instant, free)
frontdesk run --quiet                    # 40 samples with AI (~120 calls; pace on free tier, see below)
frontdesk run --text "Can I get my lisinopril refilled?" --contact 555-0102   # one live message
frontdesk ui                             # (ui-prototype branch) web UI at http://127.0.0.1:8765
```

Free-tier pacing for batch runs: `$env:FRONTDESK_MAX_RPM='12'; $env:FRONTDESK_CONCURRENCY='2'`.
Copy-paste test messages for every behaviour: `docs/test-commands.md`. Full guide: `docs/how-to-run.md`.

## 3. Architecture (the rules of this codebase)

**Principle: the AI proposes, Python enforces.** The LLM handles language and tool choice; every clinic rule lives in
Python inside the MCP tools; MCP is the tool interface; the eval harness measures. Plain explanation with diagrams:
`docs/how-it-works.md`, `docs/images/*.png`, `docs/architecture.drawio`.

Per-message pipeline (`frontdesk/pipeline.py`), only the steps a message needs run; safety + validator always run:
1. **Identify sender** (Python, `policy/identity.py`, via MCP `find_patient`): contact on file, name+DOB, caregiver.
2. **Safety check A** (Python, `safety.py`): red-flag regexes, each match checked in its clause. Clear wording →
   emergency now (0 AI calls, fixed 911 template, `clinician_urgent`, stop). Negated / historical / informational /
   quoted / idiom wording → deferred to the AI; **if the AI fails it escalates**.
3. **Understand** (AI, `understand/llm.py`, structured JSON `understand/schema.py`); **safety B** ORs in the AI's
   clinical concern. If degraded and clinical → `clinician_urgent` (never treated as routine).
4. **Act** (AI tool loop `act/llm_agent.py`, tools scoped per intent; `outcome.enforce_followups` adds policy-required
   steps in code). If the AI fails after a state change (book/cancel/reschedule/prepare refill), nothing is redone:
   route to `front_desk` (`pipeline._act`, `ai_actions_before_fallback`).
5. **Outcome** (`outcome.derive`): actions and queues computed from the **tool ledger**, not from AI claims.
6. **Reply** (`reply/`): EN/ES templates from confirmed tool results; AI drafts only general answers;
   `reply/validate.py` checks every reply (fixed emergency text, no PHI to unverified senders, no ungrounded
   dates/times/prices). Drafts only, never sent.

Other parts:
- **MCP tools** `tools/service.py` (rules), `mcp_server.py`, `client.py` (ledger, context-id injection). 14 tools;
  `find_patient`, `draft_reply`, `export_state` are orchestrator-only. There is deliberately **no approve-refill tool**.
- **AI providers** `llm.py` + `config.py`: Gemini (`google-genai`), Groq (OpenAI-compatible, `httpx`, model ids
  prefixed `groq:`), Anthropic (supported, unused). Callers use the Anthropic Messages shape; backends translate.
  Chain default: `gemini-3.5-flash-lite → groq:qwen/qwen3.8-27b → groq:openai/gpt-oss-120b → gemini-3-flash-preview
  → gemini-2.5-flash` (Claude never mixed with Gemini/Groq). 8 s per call, 20 s per message deadline
  (`message_deadline` contextvar), max 3 s rate-limit wait, 30 s cool-down after a timeout, transient vs permanent
  failures (`ModelUnavailable.permanent`), budget/quota alerts (`llm.stopped`).
- **Events** `events.py`: every step emits typed events (`message.received`, `sender.identified`, `safety.checked`
  A/B, `understanding.ready/degraded`, `agent.turn`, `tool.called/result`, `llm.usage/retry/model_switched/
  deadline/stopped`, `policy.enforced`, `actions.derived`, `reply.composed/validated`, `result.ready`,
  `run.started/finished`, `mcp.connected`). The terminal (`render.py`), traces (`traces.jsonl`) and the web UI all
  consume the same events. **The pipeline never prints.**
- **Runner** `runner.py`: batch runs, prefetches understanding in parallel, writes `runs/<name>/` (results.json,
  traces.jsonl, trace_summary.json, state_final.json, run_meta.json, eval_report.*). `runs/` is git-ignored.
- **Evaluation** `eval/harness.py`: emergency recall, must_not (deterministic checks), over-escalation, urgency,
  action exact/F1, degraded, latency, cost. `EVALUATION.md` is the submitted report.

## 4. How we work (follow this)

- **Understand before building.** Explain the plan in plain words first; for non-trivial changes report current
  behaviour, proposed changes, affected files and risks, and get approval ("first confirm, then change").
- **Small, verified steps.** Read the code before changing it. Add/adjust offline tests with every behaviour change
  (fake providers, no network). Run `pytest` before committing. Prove a fix with a test that fails without it.
- **Never claim without evidence.** Run the thing; check logs/traces; screenshot UIs (headless Edge, see §6).
- **Honest evaluation.** Never change labels to improve scores; never put labels or sample messages into prompts;
  keep baselines and report before/after; list every failure; say when a run is single or contaminated.
- **Safety first.** An AI failure must never clear a possible emergency; emergencies never depend on the AI;
  replies are drafts; no state-changing action is ever retried because a later AI call failed.
- **Secrets.** Keys only in `.env` (git-ignored). Never print keys (compare inside Python). A local pre-commit hook
  (`.git/hooks/pre-commit`, not versioned) blocks env files and key-shaped strings — in docs write
  `$env:GEMINI_API_KEY = 'x'` with spaces, not `KEY=value`, or the hook blocks the commit. GitHub: secret scanning
  + push protection enabled; `.github/workflows/secret-scan.yml` runs gitleaks over full history on every push.
- **Commits:** clear messages; push with
  `git -c credential.helper= -c "credential.https://github.com.helper=!gh auth git-credential" push origin <branch>`
  (the Windows credential manager otherwise hangs on a login pop-up).
- **Communication with the owner:** plain language, short, lead with the result, no jargon; he wants to understand
  and explain everything; he prefers a recommendation plus a clear question when a decision is his.

## 5. How we got here (short history)

1. Brainstormed requirements, researched agent patterns (Anthropic "building effective agents", 12-factor agents,
   tau-bench), wrote the design (`docs/design/`).
2. Built the CLI pipeline, MCP tools, rules fallback, harness (initially Claude-only), then added Gemini + spend
   guards; repo created; secrets excluded.
3. Owner decisions: M006 (low mood) → same-day clinician, no generated crisis wording; follow clinic policy over a
   label when they conflict (M004).
4. Free-tier pain → model fallback chain, quota classification, pacing; held-out set of 22 messages (AI-drafted
   labels, still to be reviewed by the owner).
5. Safety hardening: 41/41 synthetic emergency probes, 0/12 false alarms; negation/history/quote handling; Groq
   backup; timeouts + deadline; no-duplicate-action guarantee; before/after in `docs/results-before-after.md`.
6. Docs: README (run first), EVALUATION.md, `docs/how-it-works.md`, `docs/ai-usage-and-cost.md`, test commands,
   edited diagrams. Final 40-sample AI run: 5/5 emergencies, 0 must_not, 0 false alarms, 92.5% urgency, 75% exact
   actions (primary Gemini was slow that day; chain served the calls), ~$0.004/message.
7. Web UI built on `ui-prototype` (owner asked for a full-information UI), then moved off `main` because the brief
   did not require it. The 30 s cool-down fix found during UI testing stayed on `main`.

## 6. Gotchas we already hit

- Gemini rejects SDK timeouts under 10 s → the per-call limit is enforced with `asyncio.wait_for`, not http_options.
- Gemini free 429s say "billing" for both per-minute and per-day limits → classify by `quotaId`
  (`PerMinute` vs `PerDay`), not text.
- Groq free/base: 8,000 tokens/min and 200,000 tokens/day per model (≈25 full messages/day); a full message is
  ~10k tokens. Structured output and tool calls cannot be combined in one Groq request (we never need both).
- Our own pacing queue (`FRONTDESK_MAX_RPM`) must not consume the message deadline (`_throttle` extends it).
- `gemini-3.5-flash-lite` was answering in 16-18 s on 2026-10-22; with the 8 s limit the chain serves calls.
- Windows: PowerShell; `python -I` ignores `PYTHONIOENCODING` → call `sys.stdout.reconfigure(encoding="utf-8")`;
  piping CLI output through `Select-Object -First` kills the process before it writes `run_meta.json`.
- Headless Edge screenshots: `msedge --headless=new --window-size=W,H --virtual-time-budget=8000 --screenshot=...`;
  Windows enforces ~500 px minimum width (390 px screenshots are cropped, not real).
- The UI's `?replay=<run-folder>` URL opens a saved run directly (useful for screenshots and demos).

## 7. The web UI (continue here, on `ui-prototype`)

```powershell
git switch ui-prototype
git merge main            # bring in the latest main first (docs, CI, fixes)
pip install -e ".[dev]"
frontdesk ui              # opens http://127.0.0.1:8765 (127.0.0.1 only); --no-browser, --port N
```

**What exists (keep and extend):**
- `frontdesk/web/server.py`: standard-library HTTP server (no new dependencies). `POST /api/run` starts a run in a
  background thread through the same `run_messages`; `GET /api/runs/<id>/stream` is Server-Sent Events of every
  pipeline event, then `eval.ready` (scorecard), `state.ready` (human queues, refills, bookings, cancellations,
  reschedules, outbox), `run.saved`, `run.error`. `GET /api/meta` (patients, sample + held-out messages, labels,
  model chain, which keys are set — booleans only), `GET /api/runs` and `/api/runs/<name>` (replay saved runs).
  One run at a time (409 if busy). Path-traversal safe.
- `frontdesk/web/static/index.html`, `app.css`, `app.js` (vanilla JS, no CDN, works offline, light/dark):
  left panel tabs *One message* (sample picker with label hint, quick examples, patient/caregiver sender picker,
  contact/name/DOB/channel/time), *Sample 40*, *Unseen 22*, *Past runs*; *Run settings* (AI on/off, model, deadline,
  budget, pace, concurrency). Main area: run header (model chain, deadline), progress bar, live **Activity** log
  (MCP connected, retries, model switches, deadline, alerts), per-message **six-step track** (identify, safety A/B,
  understand, act with every tool call and who called it, outcome, validated reply draft) that fills in live,
  batch table of messages (click to expand), **scorecard**, and **Work handed to people** panel.
- `tests/test_web.py` (6 tests): meta without secrets, live stream of every step, emergency, scored batch + state,
  replay, bad requests and path traversal.

**Verified:** all tests pass; headless screenshots of single/batch replays in light, dark and ~500 px width; the
live path (form → click → SSE → card) driven in headless Edge via DevTools protocol in no-AI mode.

**Not yet verified / to do next:**
1. The live path **with AI** in a real browser (was interrupted): run one message with AI and watch switches and
   tokens appear live.
2. Demo polish ideas the owner may want (ask first): a presenter-friendly "demo script" list of messages, larger
   type mode, clearer "AI vs Python" legend on the track, an inbox-style list view, keyboard shortcuts.
3. Before showing live: fresh API quotas (Gemini free daily limits reset at midnight Pacific); prefer single
   messages over full 40-message runs during the demo; keep `Run settings → Use AI` toggle to show the no-AI
   fallback; have a saved run ready via `?replay=` as a backup if the network or quota fails.
4. Keep `main` free of UI code unless the owner decides to merge.

Design notes for the UI: same colour language as the diagrams (blue = Python, violet = AI, red = emergency, amber =
urgent, orange = humans, green = saved output); plain-language labels; never show API keys; never send anything.
