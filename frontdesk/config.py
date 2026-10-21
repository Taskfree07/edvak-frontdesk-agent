"""Runtime settings. Every value can be overridden by environment variable or CLI flag."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent

Mode = Literal["llm", "rules"]
Transport = Literal["inproc", "stdio"]

# USD per million tokens, used only for run-cost reporting.
# Anthropic: entered with the original build, not re-verified. Gemini: ai.google.dev/gemini-api/docs/pricing,
# paid Standard tier, checked 2026-10-21 (free-tier keys are not charged; these show what a paid key would cost).
PRICING: dict[str, dict[str, float]] = {
    "claude-opus-5-5": {"input": 4.00, "output": 20.00, "cache_read": 0.20, "cache_write": 5.00},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00, "cache_read": 0.20, "cache_write": 2.50},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125},
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50, "cache_read": 0.03, "cache_write": 0.0},
    "gemini-2.5-flash-lite": {"input": 0.10, "output": 0.40, "cache_read": 0.01, "cache_write": 0.0},
    "gemini-2.5-pro": {"input": 1.25, "output": 10.00, "cache_read": 0.125, "cache_write": 0.0},
    "gemini-3-flash-preview": {"input": 0.50, "output": 3.00, "cache_read": 0.05, "cache_write": 0.0},
    "gemini-3.5-flash-lite": {"input": 0.30, "output": 2.50, "cache_read": 0.03, "cache_write": 0.0},
    "gemini-3.1-flash-lite": {"input": 0.25, "output": 1.50, "cache_read": 0.025, "cache_write": 0.0},
    "gemini-3.8-flash": {"input": 0.75, "output": 3.75, "cache_read": 0.075, "cache_write": 0.0},
    # Groq: console.groq.com/docs/models, checked 2026-10-21.
    "groq:qwen/qwen3.8-27b": {"input": 0.80, "output": 4.00, "cache_read": 0.80, "cache_write": 0.0},
    "groq:openai/gpt-oss-120b": {"input": 0.15, "output": 0.60, "cache_read": 0.15, "cache_write": 0.0},
    "groq:openai/gpt-oss-20b": {"input": 0.075, "output": 0.30, "cache_read": 0.075, "cache_write": 0.0},
}


def provider_of(model: str) -> str:
    if model.startswith("groq:"):
        return "groq"
    return "gemini" if model.startswith("gemini") else "anthropic"


def _has(*names: str) -> bool:
    return any(os.getenv(n) for n in names)


def _default_model() -> str:
    if os.getenv("FRONTDESK_MODEL"):
        return os.environ["FRONTDESK_MODEL"]
    has_anthropic = _has("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
    if _has("GEMINI_API_KEY", "GOOGLE_API_KEY") and not has_anthropic:
        return "gemini-3.5-flash-lite"
    if _has("GROQ_API_KEY") and not has_anthropic:
        return "groq:qwen/qwen3.8-27b"
    return "claude-opus-5-5"


def _fallbacks() -> list[str]:
    raw = os.getenv("FRONTDESK_MODEL_FALLBACKS")
    if raw is not None:
        return [m.strip() for m in raw.split(",") if m.strip()]
    if provider_of(_default_model()) == "anthropic":
        return []
    # Groq first: it is the fast backup when Gemini is slow or failing; then other Gemini models.
    return ["groq:qwen/qwen3.8-27b", "groq:openai/gpt-oss-120b", "gemini-3-flash-preview", "gemini-2.5-flash"]


def _num(name: str, default: float | None) -> float | None:
    v = os.getenv(name)
    return float(v) if v not in (None, "") else default


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("FRONTDESK_DATA_DIR", ROOT / "edvak_forge_data")))
    mode: Mode = field(default_factory=lambda: os.getenv("FRONTDESK_MODE", "llm"))  # type: ignore[arg-type]
    transport: Transport = field(default_factory=lambda: os.getenv("FRONTDESK_TRANSPORT", "inproc"))  # type: ignore[arg-type]
    model: str = field(default_factory=_default_model)
    model_fallbacks: list[str] = field(default_factory=_fallbacks)
    max_cost_usd: float | None = field(default_factory=lambda: _num("FRONTDESK_MAX_COST_USD", 2.0))
    max_tokens: int | None = field(default_factory=lambda: int(_num("FRONTDESK_MAX_TOKENS", 0) or 0) or None)
    max_rpm: float | None = field(default_factory=lambda: _num("FRONTDESK_MAX_RPM", None))
    call_timeout_s: float = field(default_factory=lambda: _num("FRONTDESK_CALL_TIMEOUT_S", 8.0) or 8.0)
    max_retry_wait_s: float = field(default_factory=lambda: _num("FRONTDESK_MAX_RETRY_WAIT_S", 3.0) or 0.0)
    message_deadline_s: float | None = field(default_factory=lambda: _num("FRONTDESK_MESSAGE_DEADLINE_S", 20.0))
    prefetch_concurrency: int = field(default_factory=lambda: int(_num("FRONTDESK_CONCURRENCY", 4) or 4))
    understand_effort: str = field(default_factory=lambda: os.getenv("FRONTDESK_UNDERSTAND_EFFORT", "low"))
    agent_effort: str = field(default_factory=lambda: os.getenv("FRONTDESK_AGENT_EFFORT", "medium"))
    max_agent_turns: int = 8
    runs_dir: Path = field(default_factory=lambda: ROOT / "runs")

    @property
    def provider(self) -> str:
        return provider_of(self.model)

    @property
    def labels_path(self) -> Path:
        return self.data_dir / "labels_sample.json"

    @property
    def messages_path(self) -> Path:
        return self.data_dir / "messages_sample.json"


def price_usd(model: str, usage: dict[str, int]) -> float:
    p = PRICING.get(model)
    if not p:
        return 0.0
    return (
        usage.get("input_tokens", 0) * p["input"]
        + usage.get("output_tokens", 0) * p["output"]
        + usage.get("cache_read_input_tokens", 0) * p["cache_read"]
        + usage.get("cache_creation_input_tokens", 0) * p["cache_write"]
    ) / 1_000_000
