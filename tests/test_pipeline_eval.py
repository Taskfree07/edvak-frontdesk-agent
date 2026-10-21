"""End-to-end (rules mode, no network), validator, and the evaluation harness itself."""

from __future__ import annotations

import asyncio
import json

from frontdesk.data import load_labels, load_messages
from frontdesk.eval.harness import evaluate
from frontdesk.outcome import Outcome
from frontdesk.policy.identity import Identity
from frontdesk.reply.compose import Reply, emergency_reply
from frontdesk.reply.validate import validate
from frontdesk.runner import run_messages
from frontdesk.safety import SafetyResult
from frontdesk.tools.client import Ledger


def test_rules_pipeline_end_to_end(settings, data, tmp_path):
    msgs = load_messages(settings.messages_path)
    out = asyncio.run(run_messages(settings, msgs, tmp_path / "run", copy_latest=False))
    labels = load_labels(settings.labels_path)
    state = json.loads((tmp_path / "run" / "state_final.json").read_text(encoding="utf-8"))
    rep = evaluate(out.results, labels, data, out.traces, state, out.meta)
    s = rep.summary
    assert s["emergency_recall"] == 1.0
    assert s["must_not_violations"] == 0
    assert s["schedule_integrity_problems"] == 0
    assert s["over_escalations"] == 0
    for r in out.results:   # exact output contract
        assert set(r) == {"id", "patient_id", "intents", "urgency", "actions", "routed_to", "tool_calls",
                          "reply_draft"}
        assert r["urgency"] in ("emergency", "urgent", "routine")
    emerg = [r for r in out.results if "escalate_emergency" in r["actions"]]
    assert all(r["actions"] == ["escalate_emergency"] and "clinician_urgent" in r["routed_to"] for r in emerg)


def test_harness_perfect_and_bad_stubs(settings, data):
    labels = load_labels(settings.labels_path)
    perfect = [{"id": l.id, "patient_id": l.patient_id, "intents": l.intents, "urgency": l.accepted_urgency[0],
                "actions": l.expected_actions, "routed_to": [], "tool_calls": [], "reply_draft": None}
               for l in labels.values()]
    s = evaluate(perfect, labels, data).summary
    assert s["action_exact_match"] == 1.0 and s["emergency_recall"] == 1.0 and s["must_not_violations"] == 0
    bad = [{**r, "actions": ["prepare_refill_for_approval"], "urgency": "routine"} for r in perfect]
    rep = evaluate(bad, labels, data)
    assert rep.summary["emergency_recall"] == 0.0
    assert {m["id"] for m in rep.emergency_misses} == {l.id for l in labels.values()
                                                       if "escalate_emergency" in l.expected_actions}
    assert any(v["must_not"] == "prepare_refill_for_approval" for v in rep.must_not_violations)


def test_validator_blocks_phi_leak_and_bad_emergency(data):
    unverified = Identity(verified=False, relationship="self_unverified", patient_id="P002", method="t",
                          sender_name="Robert")
    out = Outcome(["request_verification"], [], [])
    leak = Reply("Hi Robert,\n\nYour Lisinopril refill is due.\n\n- x", "en", ["verification"])
    assert not validate(data, unverified, SafetyResult("none"), out, leak, Ledger("t")).ok

    verified = Identity(verified=True, relationship="self", patient_id="P003", method="t", sender_name="Aisha")
    em = Outcome(["escalate_emergency"], ["clinician_urgent"], [])
    good = Reply(emergency_reply("Aisha", "en"), "en", ["emergency"])
    bad = Reply("Hi Aisha, try taking an aspirin.", "en", ["llm_draft"])
    assert validate(data, verified, SafetyResult("emergency"), em, good, Ledger("t")).ok
    assert not validate(data, verified, SafetyResult("emergency"), em, bad, Ledger("t")).ok


def test_validator_grounding_of_llm_drafts(data):
    ident = Identity(verified=True, relationship="self", patient_id="P019", method="t", sender_name="Olivia")
    facts = {"holidays": [{"date": "2026-11-27"}], "hours": {"open": "09:00", "close": "17:00"}}
    ok = Reply("x", "en", ["llm_draft"], facts, ["We're closed on Friday, November 27. We open at 9:00 AM."])
    made_up = Reply("x", "en", ["llm_draft"], facts, ["We're closed on November 28 and a visit costs $40."])
    out = Outcome(["answer_directly"], [], [])
    assert validate(data, ident, SafetyResult("none"), out, ok, Ledger("t")).ok
    assert not validate(data, ident, SafetyResult("none"), out, made_up, Ledger("t")).ok
