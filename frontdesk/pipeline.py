"""The per-message pipeline: identify -> safety gate -> understand -> act -> enforce -> derive -> reply -> validate.

Every stage emits events (see events.py). Nothing here prints; renderers subscribe to the bus.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from frontdesk.act.llm_agent import LLMAgent
from frontdesk.act.resolution import Resolution
from frontdesk.act.rule_agent import RuleAgent
from frontdesk.config import Settings
from frontdesk.data import ClinicData, Message
from frontdesk.events import EventBus, Stopwatch
from frontdesk.llm import LLM, LLMError
from frontdesk.outcome import PHI_INTENTS, Outcome, ReplyItem, derive, enforce_followups
from frontdesk.policy.identity import Identity
from frontdesk.reply.compose import Reply, compose, wrap
from frontdesk.reply.templates import T
from frontdesk.reply.validate import validate
from frontdesk.safety import SafetyResult, combine, keyword_gate
from frontdesk.tools.client import Ledger, ToolClient
from frontdesk.understand.llm import LLMUnderstander
from frontdesk.understand.rules import RulesUnderstander
from frontdesk.understand.schema import Understanding

URGENCY_RANK = {"routine": 0, "urgent": 1, "emergency": 2}


@dataclass
class MessageResult:
    result: dict[str, Any]                       # exact brief format
    trace: dict[str, Any] = field(default_factory=dict)


class Pipeline:
    def __init__(self, settings: Settings, bus: EventBus, tools: ToolClient, data: ClinicData, llm: LLM | None):
        self.settings = settings
        self.bus = bus
        self.tools = tools
        self.data = data
        self.llm = llm
        clinic = data.clinic
        self.rules_understander = RulesUnderstander(clinic)
        self.rule_agent = RuleAgent(tools, clinic)
        self.llm_understander = LLMUnderstander(llm, clinic) if llm else None
        self.llm_agent = LLMAgent(llm, tools, clinic) if llm else None

    # ------------------------------------------------------------------ understanding (can be prefetched)
    async def understand(self, msg: Message, identity: Identity) -> tuple[Understanding, bool, str | None]:
        """Returns (understanding, degraded, error)."""
        if self.llm_understander is None:
            return self.rules_understander.understand(msg, identity), False, None
        try:
            return await self.llm_understander.understand(msg, identity), False, None
        except LLMError as e:
            return self.rules_understander.understand(msg, identity), True, str(e)

    # ------------------------------------------------------------------ main
    async def process(self, msg: Message, prefetched: tuple[Understanding, bool, str | None] | None = None
                      ) -> MessageResult:
        total = Stopwatch()
        mid = msg.id
        ledger = Ledger(mid)
        trace: dict[str, Any] = {"mode": self.settings.mode, "degraded": False, "errors": []}
        self.bus.emit("message.received", mid, stage="intake", channel=msg.channel, from_name=msg.from_name,
                      from_contact=msg.from_contact, body=msg.body, received_at=msg.received_at.isoformat())

        # 1. Identify (deterministic, via MCP)
        sw = Stopwatch()
        found = await self.tools.call(ledger, "find_patient", {
            "message_id": mid, "body": msg.body, "received_at": msg.received_at.isoformat(),
            "from_contact": msg.from_contact, "from_name": msg.from_name,
            "stated_dob": msg.stated_dob.isoformat() if msg.stated_dob else None}, by="orchestrator")
        ledger.context_id = found.get("context_id")
        identity = Identity.model_validate({k: found.get(k) for k in Identity.model_fields if k in found})
        self.bus.emit("sender.identified", mid, stage="identify", duration_ms=sw.ms, **identity.model_dump())
        trace["identity"] = identity.model_dump()

        # 2. Safety gate, part A (keywords; no LLM)
        safety = keyword_gate(msg.body)
        self.bus.emit("safety.checked", mid, stage="safety", part="A", **safety.as_dict())

        und: Understanding | None = None
        resolution: Resolution | None = None
        if safety.level != "emergency":
            # 3. Understand
            sw = Stopwatch()
            und, degraded, err = prefetched or await self.understand(msg, identity)
            if degraded:
                trace["degraded"] = True
                trace["errors"].append(f"understand: {err}")
            self.bus.emit("understanding.ready", mid, stage="understand", duration_ms=sw.ms,
                          source="rules" if (degraded or not self.llm) else "llm", **und.model_dump())
            # 3b. Safety gate, part B (LLM concern OR-ed in; idioms need an LLM all-clear)
            llm_concern = None if degraded else und.clinical_concern
            safety = combine(safety, llm_concern)
            self.bus.emit("safety.checked", mid, stage="safety", part="B", **safety.as_dict())

        if safety.level == "emergency":
            await self.tools.call(ledger, "route_to_queue", {
                "queue": "clinician_urgent", "priority": "high",
                "reason": f"POSSIBLE EMERGENCY ({', '.join(safety.hits) or safety.decided_by}): {msg.body[:200]}"},
                by="orchestrator")
            if und is None:  # emergency decided before the LLM: cheap rules intents for the record
                und = self.rules_understander.understand(msg, identity)
            if "clinical" not in und.intents:
                und.intents = [*und.intents, "clinical"]
            urgency = "emergency"
            enforced: list[str] = []
        else:
            assert und is not None
            urgency = und.urgency if und.urgency != "emergency" else "urgent"
            if safety.level == "urgent_clinical" and URGENCY_RANK[urgency] < 1:
                urgency = "urgent"
            # 4. Act (proportional: only when there is something to do with tools)
            if self._needs_agent(identity, und):
                sw = Stopwatch()
                resolution = await self._act(msg, identity, und, ledger, found, safety, trace)
                self.bus.emit("agent.resolved", mid, stage="act", duration_ms=sw.ms,
                              resolution=resolution.model_dump() if resolution else None)
            # 5a. Mandatory follow-ups (code)
            enforced = await enforce_followups(self.tools, ledger, self.data.clinic, identity, und, safety, found)
            for e in enforced:
                self.bus.emit("policy.enforced", mid, stage="outcome", step=e)

        # 5b. Derive actions from facts
        outcome = derive(identity, safety, und, ledger, resolution, enforced)
        self.bus.emit("actions.derived", mid, stage="outcome", actions=outcome.actions, routed_to=outcome.routed_to,
                      issues=outcome.resolution_issues)

        # 6. Reply + validate
        lang = (und.language if und and und.language in T else None) or \
            ("es" if found.get("preferred_language") == "Spanish" and identity.verified else "en")
        sw = Stopwatch()
        reply = await compose(outcome.items, identity.sender_name, lang,
                              self.llm if (self.llm and not trace["degraded"]) else None, mid)
        self.bus.emit("reply.composed", mid, stage="reply", duration_ms=sw.ms, methods=reply.methods,
                      language=reply.language, text=reply.text)
        check = validate(self.data, identity, safety, outcome, reply, ledger)
        if not check.ok:
            reply, outcome = await self._fail_safe(ledger, identity, lang, outcome, check.violations)
        self.bus.emit("reply.validated", mid, stage="reply", ok=check.ok, violations=check.violations)
        if reply.text:
            await self.tools.call(ledger, "draft_reply", {"text": reply.text, "language": reply.language},
                                  by="orchestrator")

        result = {
            "id": mid,
            "patient_id": identity.patient_id,
            "intents": und.intents if und else [],
            "urgency": urgency,
            "actions": outcome.actions,
            "routed_to": outcome.routed_to,
            "tool_calls": ledger.names(),
            "reply_draft": reply.text,
        }
        trace.update({
            "safety": safety.as_dict(), "understanding": und.model_dump() if und else None,
            "resolution": resolution.model_dump() if resolution else None, "enforced": enforced,
            "resolution_issues": outcome.resolution_issues, "reply_methods": reply.methods,
            "validation": {"ok": check.ok, "violations": check.violations},
            "tool_ledger": [{"name": c.name, "by": c.by, "args": c.args, "ok": c.ok, "ms": c.ms, "result": c.result}
                            for c in ledger.calls],
            "duration_ms": total.ms,
        })
        self.bus.emit("result.ready", mid, stage="result", duration_ms=total.ms, result=result,
                      degraded=trace["degraded"], validation_ok=check.ok)
        return MessageResult(result, trace)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _needs_agent(identity: Identity, und: Understanding) -> bool:
        intents = set(und.intents)
        if intents <= {"spam", "other"} or "third_party" in intents or identity.relationship == "third_party":
            return False
        if not identity.verified and identity.patient_id and intents & PHI_INTENTS:
            return False  # verification first; no tools touch the record
        return True

    async def _act(self, msg: Message, identity: Identity, und: Understanding, ledger: Ledger,
                   facts: dict[str, Any], safety: SafetyResult, trace: dict[str, Any]) -> Resolution | None:
        urgent = safety.level == "urgent_clinical"
        if self.llm_agent is not None and not trace["degraded"]:
            try:
                return await self.llm_agent.act(msg, identity, und, ledger, facts, urgent)
            except LLMError as e:
                trace["degraded"] = True
                trace["errors"].append(f"act: {e}")
                self.bus.emit("agent.degraded", msg.id, stage="act", error=str(e))
        return await self.rule_agent.act(msg, identity, und, ledger, facts, urgent)

    async def _fail_safe(self, ledger: Ledger, identity: Identity, lang: str, outcome: Outcome,
                         violations: list[str]) -> tuple[Reply, Outcome]:
        await self.tools.call(ledger, "route_to_queue", {
            "queue": "front_desk", "reason": f"Validator blocked the automatic reply: {violations[0][:200]}"},
            by="code")
        actions = list(outcome.actions)
        if "route_to_front_desk" not in actions:
            actions.append("route_to_front_desk")
        routed = list(outcome.routed_to) + ([] if "front_desk" in outcome.routed_to else ["front_desk"])
        safe = Outcome(actions, routed, [ReplyItem("routed", {"queue": "front_desk"})], outcome.enforced,
                       outcome.resolution_issues + ["validator fail-safe applied"])
        return Reply(wrap(T[lang]["fallback"], identity.sender_name, lang), lang, ["fallback"]), safe
