"""Command line: frontdesk run | eval | show | serve-mcp."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import sys
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table

from frontdesk.config import Settings
from frontdesk.data import ClinicData, Message, load_labels, load_messages
from frontdesk.events import Event
from frontdesk.eval.harness import evaluate_run
from frontdesk.render import TerminalRenderer, print_eval
from frontdesk.runner import ConfigError, run_messages


def _settings(a: argparse.Namespace) -> Settings:
    s = Settings()
    for k in ("mode", "transport", "model"):
        if getattr(a, k, None):
            setattr(s, k, getattr(a, k))
    if getattr(a, "data_dir", None):
        s.data_dir = Path(a.data_dir)
    return s


def cmd_run(a: argparse.Namespace, console: Console) -> int:
    s = _settings(a)
    if a.text:
        messages = [Message(id=a.id or "LIVE-001", received_at=datetime.fromisoformat(a.at), channel=a.channel,
                            from_name=a.name, from_contact=a.contact, body=a.text,
                            stated_dob=a.dob)]
    else:
        messages = load_messages(Path(a.messages) if a.messages else s.messages_path)
    if a.ids:
        wanted = {x.strip() for x in a.ids.split(",")}
        messages = [m for m in messages if m.id in wanted]
    base = Path(a.out) if a.out else s.runs_dir / datetime.now().strftime("%Y%m%d-%H%M%S")
    is_sample_run = not a.text and not a.messages   # only the full sample run updates results.json / runs/latest
    labels_path = Path(a.labels) if a.labels else (s.labels_path if is_sample_run else None)
    do_eval = bool(labels_path and labels_path.exists() and not a.no_eval)
    reports = []
    for i in range(max(1, a.repeat)):
        out_dir = base if a.repeat <= 1 else base / f"rep{i + 1}"
        try:
            out = asyncio.run(run_messages(s, messages, out_dir, [TerminalRenderer(console, quiet=a.quiet)],
                                           copy_latest=is_sample_run and not a.ids and i == 0))
        except ConfigError as e:
            console.print(f"[bold red]{e}[/]")
            return 2
        console.print(f"[dim]Artefacts: {out.out_dir}[/]")
        if do_eval:
            labels = {k: v for k, v in load_labels(labels_path).items() if k in {m.id for m in messages}}
            rep = evaluate_run(out.out_dir, labels, ClinicData.load(s.data_dir))
            if is_sample_run and not a.ids and i == 0:
                for name in ("eval_report.md", "eval_report.json"):
                    shutil.copyfile(out.out_dir / name, s.runs_dir / "latest" / name)
            print_eval(console, rep)
            console.print(f"[dim]Report: {out.out_dir / 'eval_report.md'}[/]")
            reports.append(rep.summary)
    if len(reports) > 1:
        _print_repeat(console, reports, base)
    return 0


def _print_repeat(console: Console, reports: list[dict], base: Path) -> None:
    keys = ["emergency_recall", "must_not_violations", "over_escalations", "urgency_accuracy", "action_exact_match",
            "action_micro_f1", "patient_id_accuracy", "degraded_messages", "cost_usd", "run_duration_s"]
    agg = {}
    t = Table(title=f"Across {len(reports)} runs", header_style="bold")
    for c in ("metric", "mean", "min", "max"):
        t.add_column(c, justify="left" if c == "metric" else "right")
    for k in keys:
        vals = [r[k] for r in reports if r.get(k) is not None]
        if not vals:
            continue
        agg[k] = {"mean": round(sum(vals) / len(vals), 4), "min": min(vals), "max": max(vals), "values": vals}
        t.add_row(k, f"{agg[k]['mean']:.3f}", f"{agg[k]['min']:.3f}", f"{agg[k]['max']:.3f}")
    console.print(t)
    (base / "repeat_summary.json").write_text(json.dumps(agg, indent=2), encoding="utf-8")


def cmd_eval(a: argparse.Namespace, console: Console) -> int:
    s = _settings(a)
    run_dir = Path(a.run) if a.run else s.runs_dir / "latest"
    if not (run_dir / "results.json").exists():
        console.print(f"[red]No results.json in {run_dir}. Run `frontdesk run` first.[/]")
        return 2
    labels = load_labels(Path(a.labels) if a.labels else s.labels_path)
    rep = evaluate_run(run_dir, labels, ClinicData.load(s.data_dir))
    print_eval(console, rep)
    console.print(f"[dim]Report: {run_dir / 'eval_report.md'}[/]")
    return 0


def cmd_show(a: argparse.Namespace, console: Console) -> int:
    s = _settings(a)
    run_dir = Path(a.run) if a.run else s.runs_dir / "latest"
    path = run_dir / "traces.jsonl"
    if not path.exists():
        console.print(f"[red]No traces in {run_dir}[/]")
        return 2
    r = TerminalRenderer(console)
    for line in path.read_text(encoding="utf-8").splitlines():
        d = json.loads(line)
        if d.get("message_id") == a.message_id:
            r(Event(d["type"], d["message_id"], d["data"], d.get("stage"), d.get("duration_ms"), d["ts"]))
    return 0


def cmd_serve(_: argparse.Namespace, __: Console) -> int:
    from frontdesk.tools.mcp_server import main
    main()
    return 0


def cmd_ui(a: argparse.Namespace, __: Console) -> int:
    from frontdesk.web.server import serve
    serve(_settings(a), port=a.port, open_browser=not a.no_browser)
    return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    for noisy in ("httpx", "httpcore", "google_genai", "mcp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    p = argparse.ArgumentParser(prog="frontdesk", description="Maplewood front-desk inbox agent")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="process messages (the sample file by default) and evaluate")
    r.add_argument("--messages", help="messages JSON file (default: messages_sample.json)")
    r.add_argument("--labels", help="labels JSON to evaluate against (default: the sample labels)")
    r.add_argument("--ids", help="comma-separated message ids to process")
    r.add_argument("--text", help="process ONE new message with this body")
    r.add_argument("--contact", help="sender phone/email for --text", default=None)
    r.add_argument("--name", help="sender name for --text", default=None)
    r.add_argument("--dob", help="stated date of birth for --text (YYYY-MM-DD)", default=None)
    r.add_argument("--channel", default="sms")
    r.add_argument("--at", default="2026-11-09T09:00:00-06:00", help="received_at for --text")
    r.add_argument("--id", help="message id for --text")
    r.add_argument("--quiet", action="store_true", help="one line per message")
    r.add_argument("--no-eval", action="store_true")
    r.add_argument("--repeat", type=int, default=1, help="run N times and report mean/min/max (LLM variance)")
    r.add_argument("--out", help="output directory (default runs/<timestamp>)")
    for sp in (r,):
        sp.add_argument("--mode", choices=["llm", "rules"])
        sp.add_argument("--transport", choices=["inproc", "stdio"])
        sp.add_argument("--model")
        sp.add_argument("--data-dir")

    e = sub.add_parser("eval", help="score a run against labels")
    e.add_argument("--run", help="run directory (default runs/latest)")
    e.add_argument("--labels")
    e.add_argument("--data-dir")

    sh = sub.add_parser("show", help="replay one message's trace")
    sh.add_argument("message_id")
    sh.add_argument("--run")
    sh.add_argument("--data-dir")

    sub.add_parser("serve-mcp", help="run the clinic MCP server over stdio")

    u = sub.add_parser("ui", help="open the web UI (local only, http://127.0.0.1:8765)")
    u.add_argument("--port", type=int, default=8765)
    u.add_argument("--no-browser", action="store_true", help="do not open a browser tab")
    u.add_argument("--data-dir")

    a = p.parse_args(argv)
    console = Console(highlight=False)
    return {"run": cmd_run, "eval": cmd_eval, "show": cmd_show, "serve-mcp": cmd_serve,
            "ui": cmd_ui}[a.cmd](a, console)


if __name__ == "__main__":
    raise SystemExit(main())
