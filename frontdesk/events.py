"""Typed pipeline events. Every stage publishes here; sinks render them (terminal), persist them (JSONL trace)
or stream them (the future web UI). The pipeline never prints directly."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

Sink = Callable[["Event"], None]


@dataclass
class Event:
    type: str                     # e.g. "tool.result", "safety.checked"
    message_id: str | None
    data: dict[str, Any] = field(default_factory=dict)
    stage: str | None = None      # identify | safety | understand | act | outcome | reply | result
    duration_ms: float | None = None
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="milliseconds"))

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=str, ensure_ascii=False)


class EventBus:
    def __init__(self) -> None:
        self._sinks: list[Sink] = []

    def subscribe(self, sink: Sink) -> None:
        self._sinks.append(sink)

    def emit(self, type_: str, message_id: str | None, stage: str | None = None,
             duration_ms: float | None = None, **data: Any) -> Event:
        ev = Event(type_, message_id, data, stage, duration_ms)
        for s in self._sinks:
            s(ev)
        return ev


class JsonlSink:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._f = path.open("a", encoding="utf-8")

    def __call__(self, ev: Event) -> None:
        self._f.write(ev.to_json() + "\n")
        self._f.flush()

    def close(self) -> None:
        self._f.close()


class ListSink:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def __call__(self, ev: Event) -> None:
        self.events.append(ev)

    def for_message(self, message_id: str) -> list[Event]:
        return [e for e in self.events if e.message_id == message_id]


class Stopwatch:
    def __init__(self) -> None:
        self.t0 = time.perf_counter()

    @property
    def ms(self) -> float:
        return round((time.perf_counter() - self.t0) * 1000, 1)
