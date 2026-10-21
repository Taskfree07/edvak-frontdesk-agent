"""Batch runner: fresh clinic state, every message in order, output artefacts on disk.

Understanding calls do not depend on clinic state, so in LLM mode they are prefetched concurrently; acting is
strictly sequential because bookings change the schedule for later messages.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from frontdesk.config import ROOT, Settings
from frontdesk.data import ClinicData, Message
from frontdesk.events import EventBus, JsonlSink, Stopwatch
from frontdesk.llm import LLM
from frontdesk.pipeline import MessageResult, Pipeline
from frontdesk.policy.identity import identify
from frontdesk.safety import keyword_gate
from frontdesk.tools.client import Ledger, ToolClient

class ConfigError(RuntimeError):
    pass


@dataclass
class RunOutput:
    out_dir: Path
    results: list[dict[str, Any]]
    traces: dict[str, dict[str, Any]]
    meta: dict[str, Any]


def check_llm_ready(settings: Settings) -> None:
    if settings.mode != "llm":
        return
    if settings.provider == "gemini":
        if not (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")):
            raise ConfigError(f"Model {settings.model} needs GEMINI_API_KEY (put it in .env). "
                              "To run without an API key use: --mode rules")
    elif not (os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")):
        raise ConfigError(f"Model {settings.model} needs ANTHROPIC_API_KEY (put it in .env). "
                          "To run without an API key use: --mode rules")


async def run_messages(settings: Settings, messages: list[Message], out_dir: Path | None = None,
                       subscribers: list[Callable] | None = None, copy_latest: bool = True) -> RunOutput:
    check_llm_ready(settings)
    data = ClinicData.load(settings.data_dir)
    out_dir = out_dir or settings.runs_dir / datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    bus = EventBus()
    sink = JsonlSink(out_dir / "traces.jsonl")
    bus.subscribe(sink)
    usage_events: list = []
    bus.subscribe(lambda ev: usage_events.append(ev) if ev.type == "llm.usage" else None)
    for s in subscribers or []:
        bus.subscribe(s)
    sw = Stopwatch()
    llm = LLM(settings, bus) if settings.mode == "llm" else None
    results: list[dict[str, Any]] = []
    traces: dict[str, dict[str, Any]] = {}
    bus.emit("run.started", None, mode=settings.mode, model=settings.model if llm else None,
             transport=settings.transport, messages=len(messages), out_dir=str(out_dir))
    try:
        async with ToolClient(settings, bus, data) as tools:
            pipe = Pipeline(settings, bus, tools, data, llm)
            prefetch: dict[str, asyncio.Task] = {}
            if llm:
                sem = asyncio.Semaphore(settings.prefetch_concurrency)

                async def pre(m: Message):
                    async with sem:
                        ident = identify(data.patients, m.from_contact, m.from_name, m.stated_dob, m.body)
                        return await pipe.understand(m, ident)

                prefetch = {m.id: asyncio.create_task(pre(m)) for m in messages
                            if keyword_gate(m.body).level != "emergency"}
            for m in messages:
                pre_und = await prefetch[m.id] if m.id in prefetch else None
                try:
                    mr: MessageResult = await pipe.process(m, pre_und)
                except Exception as e:  # never lose the whole run to one message
                    bus.emit("message.failed", m.id, error=repr(e)[:500])
                    mr = MessageResult({"id": m.id, "patient_id": None, "intents": [], "urgency": "urgent",
                                        "actions": ["route_to_front_desk"], "routed_to": ["front_desk"],
                                        "tool_calls": [], "reply_draft": None},
                                       {"errors": [repr(e)[:500]], "crashed": True})
                results.append(mr.result)
                traces[m.id] = mr.trace
            state = await tools.call(Ledger("__run__"), "export_state", {}, by="orchestrator")
    except BaseException:
        sink.close()
        raise

    meta = {"mode": settings.mode, "model": settings.model if llm else None, "transport": settings.transport,
            "messages": len(messages), "duration_s": round(sw.ms / 1000, 1),
            "llm": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in (llm.totals if llm else {}).items()},
            "degraded_messages": [k for k, t in traces.items() if t.get("degraded")],
            "llm_stopped": llm.stopped if llm else None,
            "model_switches": llm.switches if llm else [],
            "models_used": sorted({e.data["model"] for e in usage_events}) if llm else [],
            "finished_at": datetime.now().isoformat(timespec="seconds")}
    _write(out_dir / "results.json", results)
    _write(out_dir / "trace_summary.json", traces)
    _write(out_dir / "state_final.json", state)
    _write(out_dir / "run_meta.json", meta)
    if copy_latest:
        shutil.copyfile(out_dir / "results.json", ROOT / "results.json")
        latest = settings.runs_dir / "latest"
        if latest.exists():
            shutil.rmtree(latest)
        shutil.copytree(out_dir, latest)
    bus.emit("run.finished", None, **meta)
    sink.close()
    return RunOutput(out_dir, results, traces, meta)


def _write(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
