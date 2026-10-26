# Evaluation report

Mode: **llm** · Model: **gemini-3.5-flash-lite** · Messages: **40** · Run time: 687.7 s

## Headline

| Metric | Value |
|---|---|
| **Emergency recall** | **100.0%** (5/5) |
| **must_not violations** | **0** |
| Over-escalations | 0 |
| Urgency accuracy | 92.5% |
| Action exact-match | 75.0% |
| Action micro P / R / F1 | 82.1% / 97.9% / 89.3% |
| Patient-id accuracy | 100.0% |
| Intent exact / mean Jaccard | 87.5% / 94.2% |
| Validator interventions | 1 |
| Schedule integrity problems | 0 |
| Degraded (LLM fallback) messages | 3 |
| ...of which the AI had already changed something (sent to front desk, not redone) | 0 |
| Latency p50 / p95 | 15682.5 ms / 44973.0 ms |
| LLM calls · tokens in/out (cache read) | 122 · 236561/29583 (44156) |
| Per message: LLM calls / tokens / cost | 3.05 / 7758 / $0.00419 |
| Estimated cost (paid-tier prices) | $0.1675 |

## Missed emergencies

None.

## must_not violations

None.

## Over-escalations

None.

## Action mismatches (our errors unless listed as a label inconsistency)

| id | expected | got | missing | extra |
|---|---|---|---|---|
| M004 | offer_appointment, refill_needs_visit | offer_appointment, refill_needs_visit, route_to_provider |  | route_to_provider |
| M012 | route_to_provider | route_to_clinician, route_to_provider |  | route_to_clinician |
| M015 | book_appointment | book_appointment, route_to_clinician |  | route_to_clinician |
| M016 | book_appointment | offer_alternatives | book_appointment | offer_alternatives |
| M019 | route_to_provider | route_to_clinician, route_to_provider |  | route_to_clinician |
| M023 | answer_directly | answer_directly, route_to_front_desk |  | route_to_front_desk |
| M025 | prepare_refill_for_approval, route_to_billing | answer_directly, prepare_refill_for_approval, route_to_billing |  | answer_directly |
| M026 | offer_appointment, refill_needs_visit, route_to_provider | offer_appointment, refill_needs_visit, route_to_clinician, route_to_provider |  | route_to_clinician |
| M030 | route_to_clinician | route_to_clinician, route_to_front_desk |  | route_to_front_desk |
| M039 | book_appointment | book_appointment, route_to_clinician |  | route_to_clinician |

## Suspected label inconsistencies (policy-required behaviour the label omits)

- **M004**: clinic.json refill_policy.not_seen_in_12_months: 'Offer an appointment and route to the provider' - the label omits route_to_provider

## Urgency mismatches

- **M001**: got `urgent`, accepted ['routine']
- **M018**: got `urgent`, accepted ['routine']
- **M019**: got `urgent`, accepted ['routine']

## Validator interventions

- **M023**: LLM draft contains details not in tool facts: ['November 24']

## Schedule integrity (final state)

No overlaps, leave, holiday, lunch or grid problems.

## Per-action precision / recall

| action | tp | fp | fn | precision | recall |
|---|---|---|---|---|---|
| acknowledge | 1 | 0 | 0 | 1.0 | 1.0 |
| answer_directly | 3 | 1 | 0 | 0.75 | 1.0 |
| book_appointment | 4 | 0 | 1 | 1.0 | 0.8 |
| cancel_appointment | 1 | 0 | 0 | 1.0 | 1.0 |
| decline_disclosure | 1 | 0 | 0 | 1.0 | 1.0 |
| escalate_emergency | 5 | 0 | 0 | 1.0 | 1.0 |
| flag_suspicious_instruction | 1 | 0 | 0 | 1.0 | 1.0 |
| no_action | 1 | 0 | 0 | 1.0 | 1.0 |
| offer_alternatives | 2 | 1 | 0 | 0.667 | 1.0 |
| offer_appointment | 2 | 0 | 0 | 1.0 | 1.0 |
| offer_new_patient_booking | 1 | 0 | 0 | 1.0 | 1.0 |
| prepare_refill_for_approval | 6 | 0 | 0 | 1.0 | 1.0 |
| refill_needs_visit | 2 | 0 | 0 | 1.0 | 1.0 |
| refill_too_early | 2 | 0 | 0 | 1.0 | 1.0 |
| request_verification | 1 | 0 | 0 | 1.0 | 1.0 |
| reschedule_appointment | 2 | 0 | 0 | 1.0 | 1.0 |
| route_to_billing | 2 | 0 | 0 | 1.0 | 1.0 |
| route_to_clinician | 2 | 5 | 0 | 0.286 | 1.0 |
| route_to_front_desk | 2 | 2 | 0 | 0.5 | 1.0 |
| route_to_provider | 5 | 1 | 0 | 0.833 | 1.0 |
