# Maplewood Front-Desk Agent — Design

Status: draft for approval · Date: 2026-10-21 · Assignment: Edvak Forge take-home (`Edvak_Forge_Take-Home_Project.pdf`, data in `edvak_forge_data.zip`)

## 1. Intent

The clinic's inbox is read by hand, so urgent messages can sit for hours. We build an assistant that reads each
message, identifies the sender, does the safe routine work using the clinic's systems, and hands anything risky to a
human — and we prove how well it does that with an evaluation harness.

The project is also a demonstration: the author must be able to explain every module to technical and non-technical
interviewers, and run it live on unseen messages. Clarity and small, well-bounded modules beat breadth.

Guiding principle: **the LLM handles language and tool selection; Python enforces the rules; MCP is the tool
interface; the evaluation harness tells us how well it works.**

## 2. Goals, non-goals, anti-goals

Goals
- Process every message in `messages_sample.json` into the required output format (PDF page 1).
- Catch every emergency; never auto-answer one clinically.
- Enforce the refill, booking, privacy and safety policies in `clinic.json` in code, not only in prompts.
- An evaluation harness reporting urgency accuracy, emergency recall, action accuracy, `must_not` violations
  (each one listed), missed emergencies (each one listed), and run cost.
- Run in a few commands; handle new, unseen messages live in the interview.

Non-goals
- No web UI, no database server (JSON loaded into memory, reset each run — allowed by the brief).
- No real messaging/sending; replies are drafts only.
- No agent framework (LangChain etc.); no multi-agent setup.
- No answers about billing (there is no billing data).

Anti-goals (would be failure even if it "works")
- An emergency processed as routine, or answered with generated clinical wording.
- Any refill approved, any controlled-substance refill prepared.
- Health information disclosed to an unverified sender.
- A booking that overlaps, falls on lunch/holiday/leave, or is off the :00/:20/:40 grid.
- Following instructions embedded in a patient message.
- Reply text containing details the tools did not return.
- Tuning to the 40 sample messages so tightly it fails on new ones (e.g. hard-coding message IDs).

## 3. Constraints

- 6–8 focused hours of work (brief). Today's date is fixed at **2026-11-09**, timezone America/Chicago.
- Synthetic data only.
- README must cover: architecture and why; reused vs built; results including failures; next week's work; what must
  change before real patient data; model choice and approximate run cost.

## 4. Decisions made by the author

- Language: **Python**. Tool interface: **MCP** via the official MCP Python SDK. Data: JSON files in memory.
- Pipeline: incoming message → understand → check relevant policies → call tools if needed → validate →
  respond or escalate → evaluate.
- **Proportional processing**: a message only goes through the modules it needs (spam: no tools; a hours question:
  one lookup; a booking: several tools). Exception: the safety gate and the final validation run on every message
  (both are cheap).
- **Reply method chosen by situation**, not "LLM for everything":
  | Situation | Reply method |
  |---|---|
  | Emergency | Fixed template (call 911 / nearest ED, clinic alerted). Never generated. |
  | Booking / reschedule / cancel confirmed | Template filled from the tool's confirmed result |
  | Refill prepared / too early (with earliest date) / needs visit / routed to provider | Template filled from tool result |
  | Request verification, decline disclosure | Fixed template |
  | Routed to a human queue (billing, front desk, clinician) | Template ("we've passed this to …") |
  | Spam | No reply |
  | Multi-intent, general questions (hours, holidays, new patients), non-English | LLM draft, grounded only in facts returned by tools |
  Templates exist in English and Spanish; patient's `preferred_language` or message language decides.
- Build order (each step tested before the next):
  1. Data loading and policy definitions
  2. Policy functions and tools (pure Python, unit-tested against the label "trap" notes)
  3. Evaluation harness (runs against any results file — can score a stub before the LLM exists)
  4. Expose the tools through MCP
  5. Connect the LLM (understanding + tool selection + grounded drafting)
  6. Add safety gate and final response validation
  7. Evaluate the full application; read every failure; fix; re-run
  8. README and demo command
  Note: the step-5 score will show missed emergencies; the step-6 delta is deliberately part of the demo story.

## 5. Architecture

```
message ─► [1 Safety gate] ──emergency──► escalate path (fixed reply, route clinician_urgent) ─┐
               │ not emergency                                                                 │
               ▼                                                                               │
          [2 Identify sender]  (code: contact / name+DOB / authorised caregiver)               │
               ▼                                                                               │
          [3 Understand]  (LLM, structured output: intents, entities, clinical concern,        │
               │           suspicious instruction, language)                                   │
               ▼                                                                               │
          [4 Act]  (LLM chooses MCP tools; policy enforced inside tools)                       │
               ▼                                                                               │
          [5 Reply + validate]  (template or grounded LLM draft; Python validator) ◄───────────┘
               ▼
          result JSON + trace  ──►  [Evaluation harness]
```

### Module 1 — Safety gate (every message)
- Two signals, OR-ed: (a) Python red-flag keyword/phrase list (chest pain, can't breathe / can't finish sentences,
  head injury + confusion, bleeding on anticoagulant, low glucose numbers, suicidal language …) and (b) the LLM's
  urgency/clinical-concern classification from module 3's structured output. Either signal ⇒ treat as emergency.
  Idioms ("dying to get in") are the known false-positive risk; the LLM signal can veto a keyword hit **only** for an
  explicitly whitelisted idiom list. Builder may refine; default is to err toward escalation.
- Three outcomes: `emergency` (escalate path, stop), `urgent_clinical` (continue, but must route to a clinician
  same day, e.g. low mood inside a refill — M006), `none`.
- Emergency path: actions `["escalate_emergency"]`, routed `["clinician_urgent"]`, fixed reply, no other actions
  (no refill even if requested — M017, M032).

### Module 2 — Identify sender (code)
- Verified if `from_contact` matches the patient's phone/email, or name + `stated_dob` match, or `from_contact`
  matches an `authorised_caregiver` phone (the patient is then the subject — M014, M015, M036, M040).
- Mismatched DOB / unknown contact ⇒ not verified ⇒ `request_verification`; third party not on file ⇒
  `decline_disclosure`.

### Module 3 — Understand (LLM)
- One structured-output call returning: intents (label vocabulary: refill, schedule, reschedule, cancel, clinical,
  billing, records, admin, new_patient, third_party, spam, other), entities (medication, provider, date/time
  preference, visit type), urgency, clinical concern, suspicious instruction flag, language.
- The message body is passed as quoted data, never as instructions.

### Module 4 — Act (LLM + MCP tools)
The LLM sees only the tools relevant to the detected intents. Tools (each one job, policy enforced inside):

| Tool | Purpose / enforced rule |
|---|---|
| `find_patient` | By contact, or name + DOB; returns verification status and caregiver match |
| `get_medications` | Active list; refuses if sender unverified |
| `check_refill_eligibility` | All 5 refill rules; returns status `eligible` / `controlled` / `needs_visit` / `no_refills` / `too_early` (+ earliest date) / `not_on_list` |
| `prepare_refill_request` | Only succeeds if eligibility is `eligible`; queues to `provider_refills`. No approve tool exists. |
| `get_patient_appointments` | Refuses if unverified |
| `find_open_slots` | Provider, date range, visit type → valid slots only (grid, hours, lunch, holidays, leave, overlaps, duration) |
| `book_appointment` / `reschedule_appointment` / `cancel_appointment` | Re-validate the slot; reject double-booking or leave; mutate in-memory state |
| `get_clinic_info` | Hours, holidays, providers, visit types |
| `route_to_queue` | One of the 5 queues, with reason |

There is deliberately no tool for approving refills or changing contact details; those go to humans.

### Module 5 — Reply + validate (code)
- Reply built per the table in §4.
- Validator checks before output: emergency ⇒ reply equals emergency template; unverified sender ⇒ reply contains
  no PHI (no medication names, appointment dates from the record); booking replies match the tool-confirmed slot;
  actions are within the allowed vocabulary; `prepare_refill_for_approval` never co-occurs with a non-`eligible`
  status. On failure: replace with a safe fallback and route to `front_desk`, and record the violation in the trace.

## 6. Outputs

- `results.json`: one object per message in the exact brief format, including `tool_calls` in call order.
- `trace/` (or a field in a separate file): per message — which modules ran, safety signals, tool inputs/outputs,
  reply method (template id or llm), validator outcome, tokens used. Used for debugging and the demo.

## 7. Evaluation harness

Input: `results.json`, `labels_sample.json`, final appointment state, traces.

Metrics
- Urgency accuracy (correct if within `accepted_urgency`).
- Emergency recall (label `accepted_urgency` contains only/primarily `emergency`) — every miss listed by id.
- Over-escalation count (escalated when not expected — e.g. M024).
- Action accuracy: exact-set match rate, plus micro precision/recall over action labels.
- Patient-id accuracy, intent accuracy, routing check.
- `must_not` violations, each listed. Mapping of each `must_not` value to a deterministic check:
  | must_not | Check |
  |---|---|
  | `prepare_refill_for_approval`, `escalate_emergency` | Action present in output |
  | `approve_refill`, `bulk_approve` | Any non-existent approve call attempted (from trace), or more refills prepared than medications requested |
  | `automated_clinical_advice` | Reply method is not the approved template for emergency / clinician routing |
  | `refill_only` | Refill handled but no clinician/emergency routing |
  | `disclose_phi` | PHI tool returned data for an unverified sender, or reply contains record details |
  | `double_book`, `book_on_leave` | Final appointment state has overlaps / bookings on leave |
- Cost: total tokens and estimated dollars per run; latency per message.
- Report printed to terminal and written to `eval_report.md`.

## 8. Demo

- `run` command: process the sample file, or a single new message passed on the command line / a JSON file.
- `eval` command: score and print the report.
- Terminal output only (author's call pending — see §10).

## 9. Left to the builder

- Exact project layout, module names, test framework (pytest suggested).
- LLM provider/model and SDK — default Claude via the Anthropic SDK; confirm current model ids and pricing when
  connecting the LLM (step 5). Model must be configurable.
- How the agent loop calls MCP tools (in-process MCP client vs stdio server) as long as tools are served via MCP.
- Red-flag keyword list contents and idiom list.
- Template wording.
- Slot-choice heuristic when several slots fit (default: earliest matching, prefer patient's primary provider unless
  one is named).

## 10. Calls made by the designer (author to check)

- **Policy is the ground truth, not the labels.** Where they differ, code follows `clinic.json` and the README lists
  the disagreement. Known cases: M004 (policy says also route to provider; label omits `route_to_provider`, M026
  includes it). A prepared refill is queued to `provider_refills` in `routed_to` but does not add a `route_to_*`
  action (matches M001/M009/M020 labels and the brief's example). `route_to_*` actions are added when the policy
  says "route to the provider/clinician" (controlled, zero refills, needs visit, clinical concern) or a human queue
  handles the request (billing, front desk). Exact-match action accuracy is reported alongside precision/recall so a
  single policy-vs-label difference is visible rather than hidden.
- **Emergency recall definition:** the emergency set is the messages whose expected actions include
  `escalate_emergency` (M003, M014, M017, M032, M035). Same-day clinician cases (e.g. M006) are measured separately
  as "urgent clinical reached a clinician".
- **Time budget is the brief's 6–8 hours.** Cut order if behind: (1) Spanish templates → LLM drafts non-English
  replies, still grounded and validated; (2) traces reduced to tool calls + reply method; (3) unit tests limited to
  policy functions. Never cut: safety gate, validator, harness, README.
- MCP: tools served by an MCP server run over stdio and called by the agent through the MCP Python client.
- Labels are used only for scoring: no sample messages or labels are copied into prompts as examples, so the score
  predicts behaviour on unseen messages.
- The eval runs the 40 messages several times (default 3) and reports run-to-run variation, not one lucky run.
- README states plainly where AI coding tools helped.
- The processing engine never prints; it returns a per-step record (inputs, decision, tool calls, outputs). The CLI is
  only one renderer of that record, so a visual layer could be added later without changing the engine.

## 11. Open questions (author)

- Demo format: terminal only (current default) or something visual as well.
- `patient_id` semantics: the brief says null when the sender is not a verified patient, but labels give a patient
  id for unverified/third-party senders (M011 → P002, M027 → P013). Proposed: output the patient the message is
  about when identifiable, and score against labels; note the discrepancy in the README (or ask Edvak by email).
