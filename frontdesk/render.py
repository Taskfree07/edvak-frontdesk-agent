"""Terminal renderer: subscribes to pipeline events and draws one panel per message (or one line in --quiet)."""

from __future__ import annotations

from typing import Any

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from frontdesk.events import Event

BY_STYLE = {"llm": "magenta", "rules": "cyan", "orchestrator": "blue", "code": "yellow"}
LEVEL_STYLE = {"emergency": "bold white on red", "urgent_clinical": "bold black on yellow", "none": "green"}


class TerminalRenderer:
    def __init__(self, console: Console | None = None, quiet: bool = False):
        self.c = console or Console()
        self.quiet = quiet
        self.buf: dict[str, list[Event]] = {}

    def __call__(self, ev: Event) -> None:
        if ev.type == "run.started":
            self.c.rule(f"[bold]Maplewood front-desk agent[/] · mode={ev.data['mode']} · "
                        f"model={ev.data.get('model') or '-'} · MCP={ev.data['transport']} · "
                        f"{ev.data['messages']} message(s)")
            return
        if ev.type == "mcp.connected":
            self.c.print(f"[dim]MCP connected ({ev.data['transport']}): {', '.join(ev.data['tools'])}[/]")
            return
        if ev.type == "llm.stopped":
            self.c.print(Panel(Text(f"{ev.data['reason']}\n\nNo more model calls will be made in this run. The "
                                    "remaining messages are processed by the rules fallback and marked [degraded]."),
                               title="[bold]ALERT: model calls stopped[/]", border_style="bold red"))
            return
        if ev.type == "run.finished":
            llm = ev.data.get("llm") or {}
            n = max(1, ev.data.get("messages") or 1)
            tok = (llm.get("input_tokens", 0) + llm.get("output_tokens", 0) + llm.get("cache_read_input_tokens", 0))
            self.c.rule(f"done in {ev.data['duration_s']} s · LLM calls {llm.get('calls', 0)} · tokens {int(tok):,} · "
                        f"cost ${llm.get('cost_usd', 0):.4f} (≈ ${llm.get('cost_usd', 0) / n:.4f}/message) · "
                        f"degraded {len(ev.data.get('degraded_messages', []))}")
            if ev.data.get("llm_stopped"):
                self.c.print(Text.assemble((" ALERT ", "bold white on red"),
                                           f" model calls stopped during this run: {ev.data['llm_stopped']}"))
            return
        if ev.message_id is None:
            return
        self.buf.setdefault(ev.message_id, []).append(ev)
        if ev.type == "message.failed":
            self.c.print(f"[bold red]{ev.message_id} FAILED[/] {ev.data.get('error')}")
        if ev.type == "result.ready":
            evs = self.buf.pop(ev.message_id, [])
            self.c.print(self._line(ev) if self.quiet else self._panel(evs, ev))

    # ------------------------------------------------------------------ views
    def _line(self, ev: Event) -> Text:
        r = ev.data["result"]
        style = "bold red" if r["urgency"] == "emergency" else "yellow" if r["urgency"] == "urgent" else "green"
        t = Text()
        t.append(f"{r['id']:<9}", style="bold")
        t.append(f"{r['urgency']:<10}", style=style)
        t.append(f"{(r['patient_id'] or '-'):<6}")
        t.append(", ".join(r["actions"]))
        if ev.data.get("degraded"):
            t.append("  [degraded]", style="yellow")
        if not ev.data.get("validation_ok", True):
            t.append("  [validator fail-safe]", style="red")
        return t

    def _panel(self, evs: list[Event], done: Event) -> Panel:
        get = lambda t: [e for e in evs if e.type == t]  # noqa: E731
        r = done.data["result"]
        rows: list[Any] = []
        msg = get("message.received")
        if msg:
            d = msg[0].data
            rows.append(Text.assemble(("from ", "dim"), (f"{d.get('from_name')} <{d.get('from_contact')}>", "bold"),
                                      (f"  via {d.get('channel')}", "dim")))
            rows.append(Text(f"“{d.get('body')}”", style="italic"))
        tbl = Table.grid(padding=(0, 1))
        tbl.add_column(style="dim", width=12)
        tbl.add_column()
        for e in get("sender.identified"):
            ok = "✔ verified" if e.data["verified"] else "✖ not verified"
            tbl.add_row("identify", f"{ok} · {e.data['relationship']} · patient {e.data.get('patient_id') or '-'}"
                                    f" · {e.data['method']}")
        for e in get("safety.checked"):
            lvl = e.data["level"]
            extra = ", ".join(e.data.get("hits") or e.data.get("urgent_hits") or e.data.get("idiom_hits") or [])
            tbl.add_row(f"safety {e.data['part']}", Text.assemble((f" {lvl} ", LEVEL_STYLE.get(lvl, "")),
                                                                  f"  {extra}  ({e.data['decided_by']})"))
        for e in get("understanding.ready"):
            d = e.data
            row = Text(f"[{d['source']}] intents={d['intents']} urgency={d['urgency']} "
                       f"concern={d['clinical_concern']} lang={d['language']}")
            if d.get("suspicious_instruction"):
                row.append(" SUSPICIOUS INSTRUCTION", style="bold red")
            tbl.add_row("understand", row)
        for e in get("tool.result"):
            st = "✔" if e.data["ok"] else "✖"
            res = e.data.get("result") or {}
            brief = res.get("status") or res.get("reason") or res.get("queue") or ""
            if "slots" in res:
                brief = f"{len(res['slots'])} slots"
            if "booked" in res:
                brief = f"{res['booked']['date']} {res['booked']['start']} {res['booked']['provider']}"
            tbl.add_row("tool", Text.assemble((f"{e.data['by']:<12}", BY_STYLE.get(e.data["by"], "")),
                                              f"{st} {e.data['tool']}  ", (str(brief), "dim")))
        for e in get("policy.enforced"):
            tbl.add_row("enforced", f"[yellow]{e.data['step']}[/]")
        for e in get("actions.derived"):
            tbl.add_row("actions", f"[bold]{', '.join(e.data['actions'])}[/]  → {', '.join(e.data['routed_to']) or '-'}")
        for e in get("reply.validated"):
            tbl.add_row("validator", "[green]pass[/]" if e.data["ok"] else f"[red]FAIL[/] {e.data['violations']}")
        usage = get("llm.usage")
        cost = sum(e.data.get("cost_usd", 0) for e in usage)
        tokens = sum(e.data.get("input_tokens", 0) + e.data.get("output_tokens", 0) +
                     e.data.get("cache_read_input_tokens", 0) for e in usage)
        rows.append(tbl)
        if r.get("reply_draft"):
            rows.append(Panel(r["reply_draft"], title="reply draft (not sent)", border_style="dim"))
        else:
            rows.append(Text("no reply", style="dim"))
        color = "red" if r["urgency"] == "emergency" else "yellow" if r["urgency"] == "urgent" else "green"
        sub = f"{done.duration_ms:.0f} ms" + \
              (f" · {len(usage)} LLM calls · {tokens:,} tokens · ${cost:.4f}" if usage else "") + \
              (" · degraded to rules" if done.data.get("degraded") else "")
        return Panel(Group(*rows), title=f"[bold]{r['id']}[/] · {r['urgency']}", subtitle=sub, border_style=color)


def print_eval(console: Console, rep: Any) -> None:
    s = rep.summary
    pct = lambda x: "n/a" if x is None else f"{x * 100:.1f}%"  # noqa: E731
    t = Table(title="Evaluation", show_header=True, header_style="bold")
    t.add_column("Metric")
    t.add_column("Value", justify="right")
    em_style = "bold green" if not s["emergencies_missed"] else "bold red"
    t.add_row("Emergency recall", f"[{em_style}]{pct(s['emergency_recall'])}[/] ({s['emergencies_expected'] - s['emergencies_missed']}/{s['emergencies_expected']})")
    t.add_row("must_not violations", f"[{'bold green' if not s['must_not_violations'] else 'bold red'}]{s['must_not_violations']}[/]")
    t.add_row("Over-escalations", str(s["over_escalations"]))
    t.add_row("Urgency accuracy", pct(s["urgency_accuracy"]))
    t.add_row("Action exact-match", pct(s["action_exact_match"]))
    t.add_row("Action micro P / R / F1", f"{pct(s['action_micro_precision'])} / {pct(s['action_micro_recall'])} / {pct(s['action_micro_f1'])}")
    t.add_row("Patient-id accuracy", pct(s["patient_id_accuracy"]))
    t.add_row("Intent exact match", pct(s["intent_exact_match"]))
    t.add_row("Validator interventions", str(s["validator_interventions"]))
    t.add_row("Suspected label inconsistencies", str(s["suspected_label_inconsistencies"]))
    t.add_row("Schedule integrity problems", str(s["schedule_integrity_problems"]))
    t.add_row("Degraded messages", str(s["degraded_messages"]))
    t.add_row("Latency p50 / p95", f"{s['latency_ms_p50']} / {s['latency_ms_p95']} ms")
    t.add_row("LLM calls · tokens", f"{s.get('llm_calls') or 0} · {s.get('total_tokens') or 0:,}")
    t.add_row("Per message: calls / tokens / cost", f"{s.get('calls_per_message') or 0} / "
              f"{s.get('tokens_per_message') or 0:,} / ${(s.get('cost_per_message_usd') or 0):.5f}")
    t.add_row("Cost (est., paid-tier prices)", f"${(s.get('cost_usd') or 0):.4f}")
    console.print(t)
    if s.get("llm_stopped"):
        console.print(Text.assemble((" ALERT ", "bold white on red"), " model calls stopped during the run, later "
                                    f"messages used the rules fallback: {s['llm_stopped']}"))
    for m in rep.emergency_misses:
        console.print(f"[bold red]MISSED EMERGENCY[/] {m['id']}: {m}")
    for v in rep.must_not_violations:
        console.print(f"[bold red]MUST_NOT[/] {v['id']} {v['must_not']}: {v['evidence']}")
    for p in rep.per_message:
        if not p.get("actions_exact", True):
            console.print(f"[yellow]mismatch[/] {p['id']}: missing {p['missing_actions']} extra {p['extra_actions']}")
