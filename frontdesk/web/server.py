"""Local web UI server (standard library only).

The page starts a run with POST /api/run and watches it with Server-Sent Events: every pipeline event (the same
events the terminal renders) is streamed as it happens, followed by the evaluation and the final clinic state.
Runs execute one at a time in a background thread, through the same `run_messages` as the CLI. Binds to
127.0.0.1 only; API keys are never sent to the page (only whether each provider has one).
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import threading
import time
import webbrowser
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from frontdesk.config import ROOT, Settings
from frontdesk.data import ClinicData, Message, load_labels, load_messages
from frontdesk.eval.harness import evaluate_run
from frontdesk.events import Event, EventBus
from frontdesk.llm import LLM, has_key
from frontdesk.runner import ConfigError, run_messages

STATIC = Path(__file__).resolve().parent / "static"
HELDOUT_MESSAGES = ROOT / "eval_sets" / "heldout" / "messages.json"
HELDOUT_LABELS = ROOT / "eval_sets" / "heldout" / "labels.json"
RUN_NAME = re.compile(r"^[\w.\-]+(/[\w.\-]+)?$")
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".png": "image/png", ".svg": "image/svg+xml"}


# ------------------------------------------------------------------------------------------------ runs
class LiveRun:
    def __init__(self, run_id: str, kind: str):
        self.id = run_id
        self.kind = kind
        self.events: list[dict[str, Any]] = []
        self.done = False
        self.cond = threading.Condition()

    def push(self, ev: dict[str, Any]) -> None:
        with self.cond:
            self.events.append(ev)
            self.cond.notify_all()

    def finish(self) -> None:
        with self.cond:
            self.done = True
            self.cond.notify_all()


class RunManager:
    def __init__(self, settings: Settings):
        self.base = settings
        self.runs: dict[str, LiveRun] = {}
        self.active: LiveRun | None = None
        self.lock = threading.Lock()

    def start(self, req: dict[str, Any]) -> LiveRun:
        with self.lock:
            if self.active and not self.active.done:
                raise RuntimeError("A run is already in progress. Wait for it to finish.")
            run_id = datetime.now().strftime("ui-%Y%m%d-%H%M%S")
            live = LiveRun(run_id, req.get("kind", "single"))
            self.runs[run_id] = live
            self.active = live
        threading.Thread(target=self._execute, args=(live, req), daemon=True).start()
        return live

    def _settings(self, req: dict[str, Any]) -> Settings:
        s = Settings()
        s.data_dir, s.runs_dir = self.base.data_dir, self.base.runs_dir
        if req.get("mode") in ("llm", "rules"):
            s.mode = req["mode"]
        if req.get("model"):
            s.model = str(req["model"])
        if req.get("deadline_s"):
            s.message_deadline_s = float(req["deadline_s"])
        if req.get("max_rpm"):
            s.max_rpm = float(req["max_rpm"])
        if req.get("concurrency"):
            s.prefetch_concurrency = int(req["concurrency"])
        if req.get("max_cost_usd"):
            s.max_cost_usd = float(req["max_cost_usd"])
        return s

    def _messages(self, s: Settings, req: dict[str, Any]) -> tuple[list[Message], dict | None, bool]:
        """(messages, labels or None, is the full sample run)."""
        kind = req.get("kind", "single")
        if kind == "single":
            dob = req.get("dob") or None
            msg = Message(id=req.get("id") or "LIVE-001",
                          received_at=datetime.fromisoformat(req.get("at") or "2026-11-09T09:00:00-06:00"),
                          channel=req.get("channel") or "sms", from_name=req.get("name") or None,
                          from_contact=req.get("contact") or None, body=req["text"], stated_dob=dob)
            return [msg], None, False
        if kind == "heldout":
            msgs, labels = load_messages(HELDOUT_MESSAGES), load_labels(HELDOUT_LABELS)
        else:
            msgs, labels = load_messages(s.messages_path), load_labels(s.labels_path)
        ids = [x.strip() for x in (req.get("ids") or "").split(",") if x.strip()]
        if ids:
            msgs = [m for m in msgs if m.id in ids]
        labels = {k: v for k, v in labels.items() if k in {m.id for m in msgs}}
        return msgs, labels, kind == "sample" and not ids

    def _execute(self, live: LiveRun, req: dict[str, Any]) -> None:
        def sink(ev: Event) -> None:
            live.push(json.loads(ev.to_json()))
        try:
            s = self._settings(req)
            messages, labels, full_sample = self._messages(s, req)
            if not messages:
                raise ValueError("No messages matched.")
            out_dir = s.runs_dir / live.id
            out = asyncio.run(run_messages(s, messages, out_dir, [sink], copy_latest=full_sample))
            if labels:
                rep = evaluate_run(out.out_dir, labels, ClinicData.load(s.data_dir))
                if full_sample:
                    for name in ("eval_report.md", "eval_report.json"):
                        shutil.copyfile(out.out_dir / name, s.runs_dir / "latest" / name)
                live.push({"type": "eval.ready", "message_id": None, "data": compact_eval(rep.to_dict())})
            live.push({"type": "state.ready", "message_id": None,
                       "data": state_summary(out.out_dir, ClinicData.load(s.data_dir))})
            live.push({"type": "run.saved", "message_id": None, "data": {"run": live.id, "dir": str(out.out_dir)}})
        except ConfigError as e:
            live.push({"type": "run.error", "message_id": None, "data": {"error": str(e)}})
        except Exception as e:  # report every failure to the page instead of dying silently
            live.push({"type": "run.error", "message_id": None, "data": {"error": f"{type(e).__name__}: {e}"}})
        finally:
            live.finish()


# ------------------------------------------------------------------------------------------------ views
def compact_eval(rep: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary": rep["summary"],
        "emergency_misses": rep["emergency_misses"],
        "must_not_violations": rep["must_not_violations"],
        "over_escalations": rep["over_escalations"],
        "label_inconsistencies": rep.get("label_inconsistencies", []),
        "validator_interventions": rep.get("validator_interventions", []),
        "schedule_integrity": rep.get("schedule_integrity", []),
        "degraded": rep.get("degraded", []),
        "mismatches": [p for p in rep["per_message"] if not p.get("actions_exact", True) or not p.get("urgency_ok", True)],
        "per_message": rep["per_message"],
    }


def state_summary(run_dir: Path, data: ClinicData) -> dict[str, Any]:
    p = run_dir / "state_final.json"
    if not p.exists():
        return {}
    st = json.loads(p.read_text(encoding="utf-8"))
    before = {a.id: a.model_dump(mode="json") for a in data.appointments}
    booked, cancelled, moved = [], [], []
    for a in st.get("appointments", []):
        old = before.get(a["id"])
        if old is None and a.get("status") == "booked":
            booked.append(a)
        elif old and a.get("status") == "cancelled" and old.get("status") == "booked":
            cancelled.append(a)
        elif old and (a["date"], a["start"], a["provider"]) != (old["date"], old["start"], old["provider"]):
            moved.append({"before": old, "after": a})
    queues: dict[str, list] = {}
    for q in st.get("queue_items", []):
        queues.setdefault(q["queue"], []).append(q)
    return {"queues": queues, "refill_requests": st.get("refill_requests", []), "outbox": st.get("outbox", []),
            "booked": booked, "cancelled": cancelled, "rescheduled": moved}


def list_runs(runs_dir: Path) -> list[dict[str, Any]]:
    out = []
    for meta_path in runs_dir.glob("**/run_meta.json"):
        d = meta_path.parent
        try:
            m = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rel = d.relative_to(runs_dir).as_posix()
        out.append({"run": rel, "mode": m.get("mode"), "model": m.get("model"), "messages": m.get("messages"),
                    "finished_at": m.get("finished_at"), "degraded": len(m.get("degraded_messages") or []),
                    "cost_usd": (m.get("llm") or {}).get("cost_usd"), "has_eval": (d / "eval_report.json").exists()})
    return sorted(out, key=lambda r: r.get("finished_at") or "", reverse=True)


def load_run(runs_dir: Path, name: str, data: ClinicData) -> dict[str, Any] | None:
    if not RUN_NAME.match(name) or ".." in name:
        return None
    d = (runs_dir / name).resolve()
    if runs_dir.resolve() not in d.parents or not (d / "run_meta.json").exists():
        return None
    events = []
    tr = d / "traces.jsonl"
    if tr.exists():
        events = [json.loads(line) for line in tr.read_text(encoding="utf-8").splitlines() if line.strip()]
    ev = d / "eval_report.json"
    return {"meta": json.loads((d / "run_meta.json").read_text(encoding="utf-8")), "events": events,
            "eval": compact_eval(json.loads(ev.read_text(encoding="utf-8"))) if ev.exists() else None,
            "state": state_summary(d, data)}


def meta(settings: Settings) -> dict[str, Any]:
    data = ClinicData.load(settings.data_dir)
    chain = LLM(settings, EventBus()).models if settings.mode == "llm" else []
    patients = []
    for p in data.patients:
        cg = p.authorised_caregiver
        patients.append({"id": p.id, "name": p.name, "phone": p.phone, "email": p.email, "dob": p.dob.isoformat(),
                         "language": p.preferred_language,
                         "caregiver": {"name": cg.name, "phone": cg.phone, "relationship": cg.relationship}
                         if cg else None})
    sample = [m.model_dump(mode="json") for m in load_messages(settings.messages_path)]
    heldout = [m.model_dump(mode="json") for m in load_messages(HELDOUT_MESSAGES)] if HELDOUT_MESSAGES.exists() else []
    labels = {k: v.model_dump() for k, v in load_labels(settings.labels_path).items()}
    return {"model": settings.model, "chain": chain, "mode": settings.mode,
            "keys": {"gemini": has_key("gemini"), "groq": has_key("groq"), "anthropic": has_key("anthropic")},
            "deadline_s": settings.message_deadline_s, "call_timeout_s": settings.call_timeout_s,
            "max_cost_usd": settings.max_cost_usd, "today": data.clinic.today.isoformat(),
            "clinic": data.clinic.clinic_name, "patients": patients, "sample": sample, "heldout": heldout,
            "labels": labels}


# ------------------------------------------------------------------------------------------------ http
def make_handler(manager: RunManager, settings: Settings):
    class Handler(BaseHTTPRequestHandler):
        server_version = "frontdesk-ui"

        def log_message(self, fmt: str, *args: Any) -> None:  # keep the terminal quiet
            return

        def _json(self, obj: Any, status: int = 200) -> None:
            body = json.dumps(obj, default=str, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _static(self, rel: str) -> None:
            path = (STATIC / (rel or "index.html")).resolve()
            if STATIC.resolve() not in path.parents or not path.is_file():
                self._json({"error": "not found"}, 404)
                return
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPES.get(path.suffix, "application/octet-stream"))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            p = unquote(url.path)
            try:
                if p == "/api/meta":
                    return self._json(meta(settings))
                if p == "/api/runs":
                    return self._json(list_runs(settings.runs_dir))
                if p.startswith("/api/runs/") and p.endswith("/stream"):
                    return self._stream(p[len("/api/runs/"):-len("/stream")])
                if p.startswith("/api/runs/"):
                    found = load_run(settings.runs_dir, p[len("/api/runs/"):], ClinicData.load(settings.data_dir))
                    return self._json(found) if found else self._json({"error": "run not found"}, 404)
                return self._static(p.lstrip("/"))
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception as e:
                return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

        def do_POST(self) -> None:  # noqa: N802
            if urlparse(self.path).path != "/api/run":
                return self._json({"error": "not found"}, 404)
            try:
                length = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(length) or b"{}")
                if req.get("kind", "single") == "single" and not (req.get("text") or "").strip():
                    return self._json({"error": "Type a message first."}, 400)
                live = manager.start(req)
                return self._json({"run": live.id})
            except RuntimeError as e:
                return self._json({"error": str(e)}, HTTPStatus.CONFLICT)
            except Exception as e:
                return self._json({"error": f"{type(e).__name__}: {e}"}, 400)

        def _stream(self, run_id: str) -> None:
            live = manager.runs.get(run_id)
            if live is None:
                return self._json({"error": "unknown run"}, 404)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            sent = 0
            last = time.monotonic()
            while True:
                with live.cond:
                    if sent >= len(live.events) and not live.done:
                        live.cond.wait(timeout=10)
                    batch = live.events[sent:]
                    done = live.done and sent + len(batch) >= len(live.events)
                try:
                    for ev in batch:
                        self.wfile.write(f"data: {json.dumps(ev, default=str, ensure_ascii=False)}\n\n".encode())
                    sent += len(batch)
                    if done:
                        self.wfile.write(b"event: end\ndata: {}\n\n")
                        self.wfile.flush()
                        return
                    if not batch and time.monotonic() - last > 9:
                        self.wfile.write(b": keep-alive\n\n")
                    last = time.monotonic() if batch else last
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    return

    return Handler


def serve(settings: Settings | None = None, port: int = 8765, open_browser: bool = True) -> None:
    settings = settings or Settings()
    manager = RunManager(settings)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(manager, settings))
    url = f"http://127.0.0.1:{port}/"
    print(f"Maplewood front-desk UI running at {url}  (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
